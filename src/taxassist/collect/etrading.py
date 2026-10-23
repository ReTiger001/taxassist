"""新点e交易（etrading.cn）采集器 —— 搜索 API 直接给正文，无需浏览器。

## 怎么找到的

用浏览器 + CDP 抓 `www.etrading.cn/jyxx/transaction.html` 的请求，
发现它调 `POST /inteligentsearch_wz/rest/esinteligentsearch/getFullTextDataNew`。
请求体是完整的 ES 查询 JSON（`wd` = 关键词、`pn` = 页码、`time` = 日期范围）。

返回 `{"code":200,"content":"<JSON字符串>"}` —— 注意 `content` **是字符串**，
要二次 `json.loads`。里面的 `result.records[]` 每条都带：

- `title`（含 `<em>` 高亮标签，要剥掉）
- **`content`（正文！）** —— 这是最关键的，其他平台大多拿不到
- `infoid`、`infod`（专区）、`categorynum`

## 覆盖

cebpub 里来自「新点e交易」的税务标有 20 条。
"""
from __future__ import annotations

import html
import json
import logging
import re
import time

import httpx

log = logging.getLogger(__name__)

BASE = "https://www.etrading.cn"
SEARCH_URL = BASE + "/inteligentsearch_wz/rest/esinteligentsearch/getFullTextDataNew"

_HDR = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 Chrome/120 Safari/537.36",
    "Content-Type": "application/json",
    "Referer": BASE + "/jyxx/transaction.html",
    "Origin": BASE,
}

_EM_RE = re.compile(r"</?em[^>]*>")
_PHONE_RE = re.compile(r"\b(?:(?:0[1-9]\d{1,2}[-\s]?\d{7,8})|(?:1[3-9]\d{9}))\b")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_PERSON_RE = re.compile(r"(?:联系人|项目联系人)\s*[:：]\s*([\u4e00-\u9fa5]{2,4})")


def make_body(keyword: str, page: int = 0, size: int = 20,
              begin: str = "2025-01-01 00:00:00",
              end: str = "2026-12-31 23:59:59") -> dict:
    return {
        "token": "", "pn": page, "rn": size, "sdt": "", "edt": "",
        "wd": keyword, "inc_wd": "", "exc_wd": "",
        "fields": "title;infod", "cnum": "002", "sort": "{webdate:0}",
        "ssort": "title", "cl": 500, "terminal": "",
        "condition": [
            {"equal": "002", "fieldName": "categorynum", "isLike": "true", "likeType": "2"},
            {"equal": "", "fieldName": "infod", "isLike": "true", "likeType": "2"},
        ],
        "time": [{"fieldName": "webdate", "startTime": begin, "endTime": end}],
        "highlights": "title;content", "statistics": None, "unionCondition": None,
        "accuracy": "", "noParticiple": "", "searchRange": None, "isBusiness": "1",
    }


def search(keyword: str, page: int = 0, size: int = 20, timeout: int = 30) -> dict:
    """调搜索 API。返回 ``{"total": n, "records": [...]}``。"""
    try:
        r = httpx.post(SEARCH_URL, headers=_HDR, json=make_body(keyword, page, size),
                       timeout=timeout)
        j = r.json()
        content = j.get("content")
        if isinstance(content, str):
            content = json.loads(content)
        if not isinstance(content, dict):
            return {"total": 0, "records": []}
        res = content.get("result") or {}
        return {"total": int(res.get("totalcount") or 0),
                "records": res.get("records") or []}
    except Exception as e:  # noqa: BLE001
        log.warning("新点搜索失败 kw=%s p%d：%s: %s", keyword, page, type(e).__name__, e)
        return {"total": 0, "records": []}




#: 非招标阶段（中标/成交/流标/终止）—— 只收还能投的
_FINISHED_WORDS = ("中标", "成交", "流标", "废标", "终止", "结果公告", "结果公示",
                   "候选人", "合同")


def fetch_detail(linkurl: str, timeout: int = 25) -> dict:
    """抓详情页的完整正文 + 联系方式。

    API 返回的 `content` 只是**摘要**（144-503 字）；完整正文在详情页
    （实测 1254 字，含联系人/电话/邮箱/截止/开标/资格要求）。
    """
    if not linkurl:
        return {}
    url = linkurl if linkurl.startswith("http") else BASE + linkurl
    try:
        r = httpx.get(url, headers={"User-Agent": _HDR["User-Agent"]},
                      timeout=timeout, follow_redirects=True)
        if r.status_code != 200:
            return {}
        s = re.sub(r"<script.*?</script>", " ", r.text, flags=re.S | re.I)
        s = re.sub(r"<style.*?</style>", " ", s, flags=re.S | re.I)
        s = re.sub(r"</(?:p|div|tr|li|h\d)>", "\n", s, flags=re.I)
        s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
        s = re.sub(r"<[^>]+>", "", s)
        s = html.unescape(s)
        s = re.sub(r"[ \t\u3000]+", " ", s)
        s = re.sub(r"\n\s*\n+", "\n", s).strip()
        persons = list(dict.fromkeys(_PERSON_RE.findall(s)))
        phones = [x for x in dict.fromkeys(_PHONE_RE.findall(s))
                  if not x.replace("-", "").startswith("400")]
        emails = list(dict.fromkeys(_EMAIL_RE.findall(s)))
        return {
            "detail_url": url,
            "detail_html": s,
            "detail_person": persons[0] if persons else None,
            "detail_phone": phones[0] if phones else None,
            "detail_email": emails[0] if emails else None,
        }
    except Exception as e:  # noqa: BLE001
        log.debug("新点详情失败 %s：%s: %s", url, type(e).__name__, e)
        return {}


def _clean_title(t: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(_EM_RE.sub("", t or ""))).strip()


def _clean_text(s: str) -> str:
    s = re.sub(r"<script.*?</script>", " ", s or "", flags=re.S | re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t\u3000]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n", s)
    return s.strip()


def to_row(rec: dict) -> dict:
    """一条 API 记录 → tender 行。"""
    title = _clean_title(str(rec.get("title") or ""))
    body = _clean_text(str(rec.get("content") or ""))
    infoid = str(rec.get("infoid") or "")
    persons = list(dict.fromkeys(_PERSON_RE.findall(body)))
    phones = [p for p in dict.fromkeys(_PHONE_RE.findall(body)) if not p.replace("-", "").startswith("400")]
    emails = list(dict.fromkeys(_EMAIL_RE.findall(body)))
    return {
        "doc_uid": f"etrading:{infoid}" if infoid else f"etrading:{hash(title)}",
        "source": "etrading",
        "notice_type": "gkzb",
        "bid_process": "招标",
        "title": title,
        "project_no": infoid or None,
        "agency": str(rec.get("infod") or "新点e交易"),
        "region": str(rec.get("infod") or "") or None,
        "is_tax": 1,
        "tax_hits": ["税务"],
        "source_html": body or None,
        # 详情页地址 + 发布时间（API 的 linkurl 是相对路径）
        "source_url": (BASE + str(rec.get("linkurl"))) if rec.get("linkurl") else None,
        "publish_at": str(rec.get("webdate") or "")[:10] or None,
        "deadline": str(rec.get("youxiaodate") or "")[:10] or None,
        "contact_person": persons[0] if persons else None,
        "contact_phone": phones[0] if phones else None,
        "contact_email": emails[0] if emails else None,
    }


def collect(conn, *, keywords: list[str] | None = None,
            max_pages: int = 15, sleep_between: float = 0.5) -> dict:
    """采集主流程：按关键词搜索 → 转换 → 入库。

    新点的搜索结果里**每条都带正文**，所以不需要再单独抓详情页。
    """
    from .. import tender as tmod

    tmod.ensure_table(conn)
    kws = keywords or ["税务", "涉税服务", "汇算清缴", "税务咨询"]
    stats = {"keywords": 0, "pages": 0, "records": 0, "new": 0, "updated": 0}

    for kw in kws:
        stats["keywords"] += 1
        seen_ids: set[str] = set()
        for page in range(0, max_pages):
            data = search(kw, page=page, size=20)
            recs = data["records"]
            if not recs:
                break
            stats["pages"] += 1
            stats["records"] += len(recs)
            for rec in recs:
                row = to_row(rec)
                if not row["title"] or row["doc_uid"] in seen_ids:
                    continue
                # 只收还能投的（中标/成交/流标这类跳过）
                if any(w in row["title"] for w in _FINISHED_WORDS):
                    continue
                seen_ids.add(row["doc_uid"])
                # 抓详情页拿完整正文（摘要只有 144-503 字）
                det = fetch_detail(str(rec.get("linkurl") or ""))
                if det:
                    row["source_html"] = det.get("detail_html") or row["source_html"]
                    row["contact_person"] = det.get("detail_person") or row.get("contact_person")
                    row["contact_phone"] = det.get("detail_phone") or row.get("contact_phone")
                    row["contact_email"] = det.get("detail_email") or row.get("contact_email")
                    stats["details"] = stats.get("details", 0) + 1
                _, is_new = tmod.upsert(conn, row)
                stats["new" if is_new else "updated"] += 1
                time.sleep(0.3)
            conn.commit()
            if len(recs) < 20:
                break
            time.sleep(sleep_between)
        log.info("新点 %s 完成：累计新增 %d", kw, stats["new"])
    return stats
