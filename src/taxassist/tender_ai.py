"""AI 判断招标公告"值不值得投"。

## 为什么需要

税务关键词会误抓一批"看起来像税务服务"的标：
「国家税务总局荆州市沙市区税务局**食堂服务**采购项目」——标题里有"税务局"和
"服务"，但买的是食堂承包，跟税务咨询毫无关系。列表越长，这类噪音越费眼神。

模型只要做三件事：
1. **鉴别真伪**：到底是不是税务专业服务
2. **看采购人**：企业/银行（专业外包多）> 政府机关（多为辅助服务）
3. **看时间**：截止日还剩几天，来不来得及准备标书

## 用哪个模型

`qwen3.8:27b-ud-q3`（实测 3-4 秒/条，27B 在这台机器上不慢）
27B 会让每条从 5 秒变 30 秒，150 条就是 75 分钟，不划算。
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime

import httpx

log = logging.getLogger(__name__)

MODEL = "qwen3.8:27b-ud-q3"
OLLAMA = "http://127.0.0.1:11434/api/generate"
TIMEOUT = 180

PROMPT = """你是税务师事务所的投标顾问。判断下面这条招标公告是否值得专业税务机构投标。

标题：{title}
采购人：{purchaser}
地区：{region}
来源平台：{platform}
发布：{publish}
截止：{deadline}（{days_left}）

评分标准（score）：
5 = 强推：明确的税务专业服务（汇算清缴/税务咨询/转让定价/税务尽调/涉税鉴证），采购人是企业或银行，时间充足
4 = 推荐：税务专业服务，但采购人一般或时间较紧
3 = 可考虑：与税务沾边但不核心（如"财务+税务"打包、税务培训）
2 = 边缘：只是采购方是税务相关单位，买的不是税务服务
1 = 不推荐：非税务服务（如食堂、设备、系统运维），或已截止

**关键判断规则**（这三类最容易误判）：
- 标题含"税务局"不代表是税务服务——税务局也买食堂、买设备、做装修、建谈话室
- **软件/系统开发/信息化采购不是税务服务**：「税务系统开发」「税务管理系统扩展」
  「税务信息化建设」买的是 IT 系统，不是专业税务服务 → score ≤ 2
- 标题含"税务"但核心是工程/货物/监理/维修/租赁/物业的，同样 score ≤ 2

只输出一行 JSON，不要解释：
{{"score": <1-5>, "is_tax_service": <true|false>, "reason": "<20字以内中文理由>"}}"""


def _parse(ans: str) -> tuple[int, bool, str]:
    """从模型回答里抠出 JSON。抠不出就返回 (0, False, 原文摘要)。"""
    txt = (ans or "").strip()
    m = re.search(r"\{[^{}]*\}", txt, re.S)
    if not m:
        return 0, False, txt[:60]
    try:
        j = json.loads(m.group(0))
        score = int(j.get("score") or 0)
        is_tax = bool(j.get("is_tax_service"))
        reason = str(j.get("reason") or "").strip()[:40]
        return max(0, min(5, score)), is_tax, reason
    except Exception:  # noqa: BLE001
        return 0, False, txt[:60]


def _days_left(deadline: str | None) -> str:
    if not deadline:
        return "未知"
    try:
        d = datetime.strptime(str(deadline)[:10], "%Y-%m-%d").date()
        n = (d - date.today()).days
        return f"还剩 {n} 天" if n >= 0 else f"已过期 {-n} 天"
    except ValueError:
        return "未知"


def judge(row: dict) -> tuple[int, bool, str]:
    """判断一条标。返回 ``(score 1-5, 是否税务服务, 理由)``。失败返回 (0, False, 错误)。"""
    prompt = PROMPT.format(
        title=(row.get("title") or "")[:200],
        purchaser=row.get("purchaser") or "未知",
        region=row.get("region") or "未知",
        platform=row.get("agency") or "未知",
        publish=row.get("publish_at") or "未知",
        deadline=row.get("deadline") or "未知",
        days_left=_days_left(row.get("deadline")),
    )
    try:
        r = httpx.post(OLLAMA, json={
            "model": MODEL, "prompt": prompt, "stream": False,
            "think": False,   # Qwen3 必须关思考，否则输出预算被推理吃光、返回空
            "options": {"temperature": 0.1, "num_predict": 120, "num_ctx": 2048},
        }, timeout=TIMEOUT)
        r.raise_for_status()
        return _parse(r.json().get("response") or "")
    except Exception as e:  # noqa: BLE001
        log.warning("AI 判断失败 %s：%s: %s", row.get("doc_uid"), type(e).__name__, e)
        return 0, False, f"判断失败：{type(e).__name__}"


def judge_pending(conn, *, limit: int | None = None, progress=None) -> dict:
    """批量判断还没评过的税务标。返回统计。

    只处理 ``is_tax=1 AND ai_score IS NULL`` 的条目，跑过的跳过（断点续跑）。
    """
    from . import tender as tmod

    tmod.ensure_table(conn)
    sql = ("SELECT doc_uid, title, purchaser, region, agency, publish_at, deadline"
           " FROM tender WHERE is_tax = 1 AND ai_score IS NULL"
           " ORDER BY publish_at DESC")
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = [dict(r) for r in conn.execute(sql).fetchall()]
    total = len(rows)
    stats = {"judged": 0, "failed": 0, "not_tax": 0, "by_score": {}}
    for i, row in enumerate(rows, 1):
        score, is_tax, reason = judge(row)
        if score <= 0:
            stats["failed"] += 1
            continue
        conn.execute(
            "UPDATE tender SET ai_score = ?, ai_reason = ?,"
            " ai_is_tax_service = ? WHERE doc_uid = ?",
            (score, reason, 1 if is_tax else 0, row["doc_uid"]))
        conn.commit()   # 每条即时提交：跑一两个小时的任务，进程被杀不丢成果
        stats["judged"] += 1
        stats["by_score"][score] = stats["by_score"].get(score, 0) + 1
        if not is_tax:
            stats["not_tax"] += 1
        if progress and (i % 10 == 0 or i == total):
            progress(i, total)
    return stats
