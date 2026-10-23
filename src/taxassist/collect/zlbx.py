"""知了标讯（zhiliaobiaoxun）API 采集器 —— 税务服务标讯的真实来源。

## 为什么换到这个源

CCGP（中国政府采购网）实测 400 条标题里 **0 条税务服务标** —— 它是政府采购网，
主体是设备/工程。税务服务采购的买方是企业（国企/上市公司），标发在企业招投标
平台。实测知了标讯用「税务服务/税务咨询/汇算清缴/涉税服务」搜到 **32764 条**，
返回的全是真标（雅砻江水电税务咨询、新疆农牧投涉税服务选聘、惠州天然气汇算
清缴鉴证…）。

## API

- 基础：``https://mcp-server.zhiliaobiaoxun.com/api_v2/{工具名}``
- 认证：``X-API-Key: zlbx_...``
- 注册：``POST https://ai.zhiliaobiaoxun.com/web-api/internal/auto-register``
  自动注册新设备送 **100 次免费调用**（绑定手机号再送 100）
- 本模块只调 ``search_bids``（列表级，字段够用）；``get_bid_detail`` 要额外消耗
  额度，正文按需再抓

## 额度意识

100 次免费额度不多。``search_bids`` 一次最多返 50 条，所以：
- 每天增量只抓最近 1-2 天 → 1-2 次调用/天，够用一两个月
- 不要盲目翻几十页（每次翻页都是一次调用）
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import socket
import subprocess
import time
from datetime import date, timedelta
from pathlib import Path

import httpx

from .. import proc as procutil

log = logging.getLogger(__name__)

API_BASE = "https://mcp-server.zhiliaobiaoxun.com/api_v2"
REGISTER_URL = "https://ai.zhiliaobiaoxun.com/web-api/internal/auto-register"
KEY_FILE = Path("data/zlbx_key.txt")

#: 用来搜索"税务服务标"的关键词。分两组：主词定领域，交付形态词定"买服务"。
TAX_QUERY_KEYWORDS: list[str] = [
    "税务服务", "税务咨询", "涉税服务", "汇算清缴",
    "税务代理", "涉税鉴证", "税务筹划", "转让定价", "税务尽调", "税务顾问",
]


def load_key() -> str | None:
    """读取 API Key：环境变量优先，其次本地文件。"""
    k = (os.environ.get("ZLBX_API_KEY") or "").strip()
    if k:
        return k
    try:
        if KEY_FILE.exists():
            k = KEY_FILE.read_text(encoding="utf-8").strip()
            return k or None
    except Exception as e:  # noqa: BLE001
        log.warning("读取 zlbx key 失败：%s", e)
    return None


def save_key(key: str) -> None:
    KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    KEY_FILE.write_text(key.strip(), encoding="utf-8")
    log.info("已保存知了标讯 API Key 到 %s", KEY_FILE)


def _device_features() -> dict:
    """采集设备特征（自动注册用）。任何一项失败都回退空串，不阻断注册。"""
    mac_hash = ""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "$n=(Get-NetAdapter | Where-Object {$_.Status -eq 'Up' -and "
             "$_.HardwareInterface} | Sort-Object ifIndex | Select-Object -First 1);"
             " if($n){$n.MacAddress}"],
            capture_output=True, text=True, timeout=30,
            creationflags=procutil.hidden_flags()).stdout.strip()
        if out:
            mac_hash = hashlib.sha256(
                out.replace("-", "").replace(":", "").lower().encode()).hexdigest()
    except Exception as e:  # noqa: BLE001
        log.debug("mac 采集失败：%s", e)
    return {
        "hostname": socket.gethostname(),
        "platform": "win32" if os.name == "nt" else "linux",
        "arch": os.environ.get("PROCESSOR_ARCHITECTURE", ""),
        "username": os.environ.get("USERNAME") or os.environ.get("USER", ""),
        "home_path": os.environ.get("USERPROFILE") or os.environ.get("HOME", ""),
        "mac_hash": mac_hash,
    }


def auto_register() -> str | None:
    """自动注册设备账号，返回 api_key（新设备送 100 次免费调用）。"""
    try:
        r = httpx.post(REGISTER_URL, json={
            "device_features": _device_features(),
            "agent_kind": "claude-code", "agent_version": "1.0.0",
            "skill_version": "tender-search/1.0",
        }, timeout=30)
        j = r.json()
        key = j.get("api_key")
        if key:
            save_key(key)
            log.info("知了标讯注册成功，剩余免费调用 %s 次", j.get("remaining_calls"))
            return key
        log.warning("注册返回无 api_key：%s", j)
    except Exception as e:  # noqa: BLE001
        log.warning("知了标讯自动注册失败：%s: %s", type(e).__name__, e)
    return None


def ensure_key() -> str | None:
    """拿一个可用 key：已有则用，没有则注册。"""
    return load_key() or auto_register()


def search_bids(key: str, keywords: list[str], *, days: int = 7,
                page: int = 1, page_size: int = 50,
                bid_type: str = "招标") -> dict:
    """调 ``search_bids``。返回原始响应 dict（含 data.items / total）。"""
    begin = (date.today() - timedelta(days=days)).isoformat()
    payload = {
        "keywords": keywords,
        "match_modes": ["title", "sm"],
        "bid_type": bid_type,
        "begin_date": begin,
        "page": page,
        "page_size": min(max(page_size, 1), 50),
    }
    r = httpx.post(f"{API_BASE}/search_bids",
                   headers={"X-API-Key": key, "Content-Type": "application/json"},
                   json=payload, timeout=45)
    r.raise_for_status()
    return r.json()


def get_bid_detail(key: str, bid_id) -> dict | None:
    """调 ``get_bid_detail`` 拿单条完整详情。

    返回的 ``data`` 里有用户要的三样东西（2026-10-23 实测）：
    - ``source``：正文 HTML
    - ``source_url``：原文链接（ctbpsp.com 的详情页）
    - ``caller_contact`` / ``caller_person``：联系方式与联系人
      （**可能部分打码**，例如 ``057*****6187``；平台对匿名用户做了脱敏）
    - ``signup_time`` / ``tender_time``：报名/投标时间

    **每次调用消耗一次额度**（免费 100 次），所以只在需要时调、且不重复调。
    """
    try:
        r = httpx.post(f"{API_BASE}/get_bid_detail",
                       headers={"X-API-Key": key, "Content-Type": "application/json"},
                       json={"bid_id": int(bid_id) if str(bid_id).isdigit() else bid_id},
                       timeout=60)
        r.raise_for_status()
        j = r.json()
        return j.get("data") if j.get("success") else None
    except Exception as e:  # noqa: BLE001
        log.warning("get_bid_detail 失败 %s：%s: %s", bid_id, type(e).__name__, e)
        return None


def html_to_text(html: str) -> str:
    """把正文 HTML 转成朴素文本（保留段落换行）。"""
    if not html:
        return ""
    s = re.sub(r"<script.*?</script>", " ", html, flags=re.S | re.I)
    s = re.sub(r"<style.*?</style>", " ", s, flags=re.S | re.I)
    s = re.sub(r"</(?:p|div|tr|br|li|h\d)>", "\n", s, flags=re.I)
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"</t[dh]>", " | ", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = s.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n", s)
    return s.strip()


def fetch_details(conn, *, limit: int | None = None, progress=None) -> dict:
    """给 zlbx 来源的条目补详情（正文 / 原文链接 / 联系方式 / 时间）。

    **额度意识**：每次 get_bid_detail 消耗一次免费调用。默认只补还没补过的
    （``detail_fetched_at IS NULL``），可断点续跑。
    """
    from .. import tender as tmod

    tmod.ensure_table(conn)
    key = ensure_key()
    if not key:
        raise RuntimeError("没有可用的知了标讯 API Key")

    sql = ("SELECT doc_uid, title FROM tender"
           " WHERE source = 'zlbx' AND detail_fetched_at IS NULL"
           " ORDER BY publish_at DESC")
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = [dict(r) for r in conn.execute(sql).fetchall()]
    total = len(rows)
    stats = {"total": total, "fetched": 0, "failed": 0, "no_contact": 0}

    for i, row in enumerate(rows, 1):
        bid_id = str(row["doc_uid"]).split(":", 1)[-1]
        data = get_bid_detail(key, bid_id)
        if not data:
            stats["failed"] += 1
            continue
        body = html_to_text(str(data.get("source") or ""))
        phone = (data.get("caller_contact") or data.get("agency_contact") or "")
        person = (data.get("caller_person") or data.get("agency_person") or "")
        if not phone and not person:
            stats["no_contact"] += 1
        tmod.upsert(conn, {
            "doc_uid": row["doc_uid"],
            "source": "zlbx",
            "title": row["title"],
            "is_tax": 1,
            "source_url": data.get("source_url") or data.get("url"),
            "contact_person": person or None,
            "contact_phone": phone or None,
            "signup_time": data.get("signup_time") or None,
            "tender_time": data.get("tender_time") or None,
            # **deadline 必须回填**：列表 API 没有 deadline 字段，导致 101 条 zlbx
            # 全部空 → notice.due_tenders() 硬性要求 deadline 非空 → **企业税务标
            # （最该提醒的那类）一条都进不了截止日邮件**。审计 B3。
            # 用投标/开标时间近似投标截止日（可在邮件里注明）。
            "deadline": (str(data.get("tender_time") or "")[:10] or None),
            "source_html": body or None,
            # 详情里若带了更准的时间/采购人，顺手补上
            "publish_at": (data.get("pub_time") or "")[:10] or None,
            "purchaser": data.get("caller_name") or None,
            "agency": data.get("agency_name") or None,
            "detail_fetched_at": date.today().isoformat(),
        })
        conn.commit()   # 每条即时提交：额度是按次消耗的，进程被杀不能丢成果
        stats["fetched"] += 1
        if progress and (i % 10 == 0 or i == total):
            progress(i, total)
        time.sleep(0.3)
    return stats


def _rows_from_items(items: list[dict]) -> list[dict]:
    """把 API 条目映射成 tender 表的行。"""
    out: list[dict] = []
    for it in items:
        bid_id = it.get("bid_id") or it.get("id")
        if not bid_id:
            continue
        money_wan = it.get("money_wan")
        out.append({
            "doc_uid": f"zlbx:{bid_id}",
            "source": "zlbx",
            "notice_type": "gkzb" if (it.get("bid_type") == "招标") else "zbgg",
            # 公告阶段：招标/预招标/采购意向（可投） vs 中标结果/中标候选人/合同（已结束）
            "bid_process": it.get("bid_process"),
            "title": it.get("title") or "",
            "project_no": str(bid_id),
            "purchaser": it.get("caller_name"),
            "region": it.get("province"),
            "publish_at": it.get("pub_time"),
            "url": it.get("url"),
            "budget": (float(money_wan) * 10000) if money_wan else None,
            # 搜索词本身就是税务领域 → 一律标税务相关
            "is_tax": 1,
            "tax_hits": [k for k in ("税务", "涉税", "汇算清缴", "转让定价")
                         if k in (it.get("title") or "")] or ["税务服务"],
            "summary": "、".join(it.get("sm_names") or []),
        })
    return out


def collect(conn, *, days: int = 7, max_pages: int = 2,
            page_size: int = 50) -> dict:
    """采集主流程：搜索税务服务标 → 写入 tender 表。

    ``max_pages`` 每次翻页都消耗一次免费额度，默认只翻 2 页（100 条）。
    """
    from .. import tender as tmod

    tmod.ensure_table(conn)
    key = ensure_key()
    if not key:
        raise RuntimeError("没有可用的知了标讯 API Key（自动注册也失败了）")

    stats = {"pages": 0, "fetched": 0, "new": 0, "updated": 0,
             "total_matched": 0, "failed": 0, "duplicates": 0}
    for page in range(1, max_pages + 1):
        try:
            resp = search_bids(key, TAX_QUERY_KEYWORDS, days=days, page=page,
                               page_size=page_size)
        except Exception as e:  # noqa: BLE001
            log.warning("知了标讯第 %d 页失败：%s: %s", page, type(e).__name__, e)
            stats["failed"] += 1
            break
        data = resp.get("data") or {}
        items = data.get("items") or []
        if page == 1:
            stats["total_matched"] = int(data.get("total") or 0)
        if not items:
            break
        stats["pages"] += 1
        for row in _rows_from_items(items):
            stats["fetched"] += 1
            # **去重**：知了标讯聚合多平台，同一公告会被重复收录（bid_id 不同、
            # 标题与日期完全相同）。实测 100 条里 6 组重复，不去重页面会看到
            # 一模一样的两条。
            #
            # 判定顺序很重要：**先看自己是否已存在**，只有自己不存在时才去找
            # 别的重复记录。反过来写（先 find_duplicate）会把"已存在条目自己的
            # 更新"重定向到另一条重复记录上，导致原记录的字段（如 bid_process）
            # 永远补不上 —— 这正是 2026-10-22 踩到的坑。
            self_exists = conn.execute(
                "SELECT 1 FROM tender WHERE doc_uid = ?", (row["doc_uid"],)).fetchone()
            if not self_exists:
                dup = tmod.find_duplicate(conn, row.get("title") or "",
                                          row.get("purchaser"))
                if dup:
                    stats["duplicates"] += 1
                    row["doc_uid"] = dup
            _, is_new = tmod.upsert(conn, row)
            stats["new" if is_new else "updated"] += 1
        conn.commit()
        log.info("知了标讯第 %d 页：%d 条，累计新增 %d",
                 page, len(items), stats["new"])
        time.sleep(1.0)
    return stats
