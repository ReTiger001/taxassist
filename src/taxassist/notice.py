"""截止日邮件提醒 —— 每天把「即将截止的税务标」发到你的邮箱。

## 为什么需要

招标最痛的不是"找不到"，是"**看到时已经来不及**"。系统里 114 条可投标里，
每天都有几条临近截止；靠人记不可能，所以要自动推。

## 配置（两种方式，任选）

**方式 1：环境变量**
```
TAXASSIST_SMTP_HOST=smtp.qq.com
TAXASSIST_SMTP_PORT=465
TAXASSIST_SMTP_USER=you@qq.com
TAXASSIST_SMTP_PASS=授权码        # QQ/163 用「授权码」，不是登录密码
TAXASSIST_SMTP_TO=you@qq.com      # 多个收件人用逗号分隔
```

**方式 2：本地文件** ``data/mail_config.json``（已被 .gitignore 覆盖，不会进版本库）
```json
{
  "smtp_host": "smtp.qq.com",
  "smtp_port": 465,
  "smtp_user": "you@qq.com",
  "smtp_pass": "授权码",
  "to": ["you@qq.com"]
}
```

QQ 邮箱授权码获取：QQ 邮箱 → 设置 → 账户 → 开启 SMTP 服务 → 生成授权码。
163 邮箱类似。企业邮箱用企业 IT 给的信息。

## 用法

    .venv\\Scripts\\python.exe -m taxassist tender-notify            # 发 3 天内到期的
    .venv\\Scripts\\python.exe -m taxassist tender-notify --days 7   # 发 7 天内到期的
    .venv\\Scripts\\python.exe -m taxassist tender-notify --dry-run  # 只打印不发（验证配置）
"""
from __future__ import annotations

import json
import logging
import os
import re
import smtplib
import ssl
from datetime import date, timedelta
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

log = logging.getLogger(__name__)

CONFIG_FILE = Path("data/mail_config.json")


def load_config() -> dict | None:
    """读邮件配置：环境变量优先，其次本地文件。都没配返回 None。"""
    env = {
        "smtp_host": os.environ.get("TAXASSIST_SMTP_HOST", "").strip(),
        "smtp_port": os.environ.get("TAXASSIST_SMTP_PORT", "").strip(),
        "smtp_user": os.environ.get("TAXASSIST_SMTP_USER", "").strip(),
        "smtp_pass": os.environ.get("TAXASSIST_SMTP_PASS", "").strip(),
        "to": os.environ.get("TAXASSIST_SMTP_TO", "").strip(),
    }
    if env["smtp_host"] and env["smtp_user"] and env["smtp_pass"] and env["to"]:
        return {
            "smtp_host": env["smtp_host"],
            "smtp_port": int(env["smtp_port"] or 465),
            "smtp_user": env["smtp_user"],
            "smtp_pass": env["smtp_pass"],
            "to": [x.strip() for x in env["to"].split(",") if x.strip()],
        }
    try:
        if CONFIG_FILE.exists():
            j = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            if j.get("smtp_host") and j.get("smtp_user") and j.get("smtp_pass") and j.get("to"):
                j.setdefault("smtp_port", 465)
                if isinstance(j["to"], str):
                    j["to"] = [x.strip() for x in j["to"].split(",") if x.strip()]
                return j
    except Exception as e:  # noqa: BLE001
        log.warning("读取邮件配置失败：%s", e)
    return None


def due_tenders(conn, days: int = 3, *, only_biddable: bool = True) -> list[dict]:
    """查「未来 N 天内截止」的税务标。"""
    today = date.today()
    until = today + timedelta(days=days)
    where = [
        "t.is_tax = 1",
        "t.deadline IS NOT NULL",
        "t.deadline != ''",
        "t.deadline >= ?",
        "t.deadline <= ?",
    ]
    params: list = [today.isoformat(), until.isoformat()]
    if only_biddable:
        where.append("(t.ai_is_tax_service IS NULL OR t.ai_is_tax_service = 1)")
        where.append("(t.bid_process IS NULL OR t.bid_process NOT IN"
                     " ('中标结果', '中标候选人', '合同'))")
    sql = ("SELECT t.doc_uid, t.title, t.purchaser, t.region, t.deadline,"
           " t.contact_person, t.contact_phone, t.url, t.source_url,"
           " t.ai_score, t.source"
           " FROM tender t WHERE " + " AND ".join(where) +
           " ORDER BY t.deadline ASC, COALESCE(t.ai_score, 0) DESC")
    rows = [dict(r) for r in conn.execute(sql, tuple(params)).fetchall()]
    # **按标题去重**：同一标会同时出现在 cebpub / zlbx / 平台 CMS 里（不同 doc_uid
    # 但标题相同）。邮件里重复列两条会让收件人以为有两个标。保留排在前面的
    # （ORDER BY 已让 AI 分高的、截止早的优先）。
    seen: set[str] = set()
    out: list[dict] = []
    for r in rows:
        key = re.sub(r"\s+", "", str(r.get("title") or ""))
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def build_html(rows: list[dict], days: int) -> str:
    """生成邮件正文（HTML 表格）。"""
    today = date.today()
    parts = [
        "<html><body style=\"font-family:-apple-system,Segoe UI,Microsoft YaHei,sans-serif;font-size:14px\">",
        f"<h2>税务服务标 · 未来 {days} 天内到期提醒</h2>",
        f"<p>共 <b>{len(rows)}</b> 条即将截止（按截止日排序）。数据来自本机税务智能知识助手。</p>",
        "<table border=\"1\" cellpadding=\"6\" cellspacing=\"0\" "
        "style=\"border-collapse:collapse;font-size:13px;width:100%\">",
        "<tr style=\"background:#f5f5f5\"><th>剩余</th><th>截止日</th><th>标题</th>"
        "<th>采购人</th><th>地区</th><th>联系方式</th><th>推荐</th></tr>",
    ]
    for r in rows:
        try:
            d = date.fromisoformat(str(r["deadline"])[:10])
            left = (d - today).days
        except Exception:  # noqa: BLE001
            left = "?"
        urgent = isinstance(left, int) and left <= 3
        color = "#c0392b" if urgent else "#333"
        contact = " ".join(x for x in (r.get("contact_person"), r.get("contact_phone")) if x) or "—"
        link = r.get("source_url") or r.get("url") or ""
        title = str(r.get("title") or "")
        title_html = f'<a href="{link}">{title}</a>' if link else title
        parts.append(
            f"<tr><td style=\"color:{color};font-weight:700\">{left} 天</td>"
            f"<td>{r.get('deadline')}</td>"
            f"<td>{title_html}</td>"
            f"<td>{r.get('purchaser') or '—'}</td>"
            f"<td>{r.get('region') or '—'}</td>"
            f"<td>{contact}</td>"
            f"<td>{r.get('ai_score') or '—'}</td></tr>")
    parts.append("</table>")
    parts.append("<p style=\"color:#888;font-size:12px;margin-top:16px\">"
                 "本邮件由本地系统自动发送，不经过任何第三方。可在 data/mail_config.json 或环境变量中修改收件人。</p>")
    parts.append("</body></html>")
    return "\n".join(parts)


def send_mail(cfg: dict, subject: str, html: str) -> None:
    """发邮件。465 端口走 SSL，587 端口走 STARTTLS。"""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = cfg["smtp_user"]
    msg["To"] = ", ".join(cfg["to"])
    msg.attach(MIMEText(html, "html", "utf-8"))

    host = cfg["smtp_host"]
    port = int(cfg.get("smtp_port") or 465)
    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=30,
                              context=ssl.create_default_context()) as s:
            s.login(cfg["smtp_user"], cfg["smtp_pass"])
            s.sendmail(cfg["smtp_user"], cfg["to"], msg.as_string())
    else:
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.starttls(context=ssl.create_default_context())
            s.login(cfg["smtp_user"], cfg["smtp_pass"])
            s.sendmail(cfg["smtp_user"], cfg["to"], msg.as_string())
    log.info("邮件已发送：%s → %s", subject, cfg["to"])


def notify(conn, *, days: int = 3, dry_run: bool = False) -> dict:
    """主流程：查即将截止的标 → 发邮件。

    返回 ``{"count": n, "sent": bool}``。没有即将截止的就不发（避免空邮件骚扰）。
    """
    rows = due_tenders(conn, days)
    if not rows:
        log.info("未来 %d 天内没有到期的税务标，跳过发送", days)
        return {"count": 0, "sent": False}

    subject = f"【税务标提醒】{len(rows)} 条将在 {days} 天内截止"
    html = build_html(rows, days)
    if dry_run:
        print(f"[dry-run] 将发送给：{load_config() or '（未配置 SMTP）'}")
        print(f"[dry-run] 主题：{subject}")
        print(f"[dry-run] 共 {len(rows)} 条：")
        for r in rows[:20]:
            print(f"  · {r['deadline']}  {str(r.get('title'))[:60]}")
        return {"count": len(rows), "sent": False}

    cfg = load_config()
    if not cfg:
        raise RuntimeError(
            "没有配置 SMTP。请在 data/mail_config.json 填 smtp_host/smtp_user/"
            "smtp_pass/to，或设 TAXASSIST_SMTP_* 环境变量。")
    send_mail(cfg, subject, html)
    return {"count": len(rows), "sent": True}
