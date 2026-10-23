"""南方电网（bidding.csg.cn）采集器 —— 静态 HTML，无需浏览器和验证码。

## 站点特征（2026-10-23 实测）

- **列表**：``https://www.bidding.csg.cn/{栏目}/index.jhtml``，分页 ``index_N.jhtml``
  - ``zbgg`` 招标公告（35 条/页，2288 页）、``zbcg`` 采购公告、``fzbgg`` 非招标公告
  - **纯静态 HTML**：直抓即可，不用浏览器、不用 API 逆向
- **详情**：``/{栏目}/{id}.jhtml``，正文 6000+ 字符，含招标人/截止/开标/地址

## 为什么这个平台特别有价值

南方电网的招标公告里有大量「税务咨询/涉税服务/汇算清缴」类采购（电网企业每年
都要选聘税务服务中介），而且**完全公开可抓** —— 比央企自建 SPA 平台省事得多。
"""
from __future__ import annotations

import logging
import re
import time

import httpx

log = logging.getLogger(__name__)

BASE = "https://www.bidding.csg.cn"
#: 栏目 → 列表路径。只收可投标的公告类栏目。
COLUMNS: dict[str, str] = {
    "zbgg": "/zbgg/index.jhtml",     # 招标公告
    "zbcg": "/zbcg/index.jhtml",     # 采购公告
    "fzbgg": "/fzbgg/index.jhtml",   # 非招标公告
}

_HDR = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 Chrome/120 Safari/537.36"}

_ITEM_RE = re.compile(r'href="(/(?:zbgg|zbcg|fzbgg)/\d+\.jhtml)"[^>]*>\s*([^<]{8,150})\s*</a>')

#: 只认**真实号码格式**：区号第 2 位不能是 0（否则会把项目编号「0000620...」
#: 当成座机），且必须落在词边界上（避免从长数字串中间截一段）。
#: 2026-10-23 实测：南方电网正文里的项目编号 000062002311896 被误抓成电话。
_PHONE_RE = re.compile(
    r"\b(?:(?:0[1-9]\d{1,2}[-\s]?\d{7,8}(?:[-\s]?\d{1,5})?)|(?:1[3-9]\d{9}))\b")
_PERSON_RE = re.compile(r"(?:联系人|项目联系人)\s*[:：]\s*([\u4e00-\u9fa5]{2,4})")
_DATE_RE = re.compile(r"(20\d{2}[-/年]\d{1,2}[-/月]\d{1,2})")


def _strip(html: str) -> str:
    s = re.sub(r"<script.*?</script>", " ", html or "", flags=re.S | re.I)
    s = re.sub(r"<style.*?</style>", " ", s, flags=re.S | re.I)
    s = re.sub(r"</(?:p|div|tr|li|h\d)>", "\n", s, flags=re.I)
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"</t[dh]>", " ", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = (s.replace("&nbsp;", " ").replace("&amp;", "&").replace("&#xa0;", " ")
          .replace("&lt;", "<").replace("&gt;", ">"))
    s = re.sub(r"[ \t\u3000]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n", s)
    return s.strip()


def fetch_list_page(column: str, page: int = 1, timeout: int = 25) -> list[dict]:
    """抓一页列表。返回 ``[{title, url, column}]``。"""
    url = BASE + COLUMNS[column]
    if page > 1:
        url = url.replace("/index.jhtml", f"/index_{page}.jhtml")
    try:
        r = httpx.get(url, headers=_HDR, timeout=timeout, follow_redirects=True)
        if r.status_code != 200:
            return []
        seen: set[str] = set()
        out: list[dict] = []
        for path, title in _ITEM_RE.findall(r.text):
            t = re.sub(r"\s+", " ", title).strip()
            if len(t) < 10 or path in seen:
                continue
            seen.add(path)
            out.append({"title": t, "url": BASE + path, "column": column})
        return out
    except Exception as e:  # noqa: BLE001
        log.warning("列表失败 %s p%d：%s: %s", column, page, type(e).__name__, e)
        return []


def fetch_detail(url: str, timeout: int = 25) -> dict:
    """抓详情页：正文 + 联系人 + 电话 + 日期。"""
    try:
        r = httpx.get(url, headers=_HDR, timeout=timeout, follow_redirects=True)
        if r.status_code != 200:
            return {}
        text = _strip(r.text)
        person_m = _PERSON_RE.search(text)
        phones = [p for p in _PHONE_RE.findall(text) if not p.replace("-", "").startswith("400")]
        publish_m = re.search(r"发布时间[:：]\s*(20\d{2}-\d{2}-\d{2})", text)
        deadline_m = re.search(r"(?:投标文件递交截止时间|递交截止时间|截止时间)[:：]?\s*"
                               r"(20\d{2}[-/年]\d{1,2}[-/月]\d{1,2})", text)
        open_m = re.search(r"开标时间[:：]?\s*(20\d{2}[-/年]\d{1,2}[-/月]\d{1,2})", text)
        return {
            "source_html": text,
            "contact_person": person_m.group(1) if person_m else None,
            "contact_phone": phones[0] if phones else None,
            "publish_at": publish_m.group(1) if publish_m else None,
            "deadline": _norm_date(deadline_m.group(1)) if deadline_m else None,
            "tender_time": _norm_date(open_m.group(1)) if open_m else None,
        }
    except Exception as e:  # noqa: BLE001
        log.debug("详情失败 %s：%s: %s", url, type(e).__name__, e)
        return {}


def _norm_date(s: str) -> str:
    """2026年11月12日 / 2026/11/12 → 2026-11-12。"""
    m = re.match(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})", s or "")
    if not m:
        return s
    return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"


def collect(conn, *, columns: list[str] | None = None, max_pages: int = 30,
            tax_keywords: list[str] | None = None,
            sleep_between: float = 0.4) -> dict:
    """采集主流程：翻页 → 标题过滤税务 → 抓详情 → 入库。"""
    from .. import tender as tmod

    tmod.ensure_table(conn)
    cols = columns or list(COLUMNS)
    kws = tax_keywords or ["税务", "涉税", "纳税", "税收", "汇算清缴", "转让定价",
                           "土地增值税", "增值税", "企业所得税", "个人所得税",
                           "涉税服务", "税务咨询"]
    stats = {"pages": 0, "listed": 0, "matched": 0, "new": 0,
             "updated": 0, "details": 0, "failed": 0}

    for col in cols:
        for page in range(1, max_pages + 1):
            items = fetch_list_page(col, page)
            stats["pages"] += 1
            if not items:
                break
            stats["listed"] += len(items)
            for it in items:
                title = it["title"]
                if not any(k in title for k in kws):
                    continue
                stats["matched"] += 1
                doc_uid = "csg:" + it["url"].rsplit("/", 1)[-1].replace(".jhtml", "")
                row = {
                    "doc_uid": doc_uid, "source": "csg", "notice_type": "gkzb",
                    "bid_process": "招标", "title": title,
                    "agency": "南方电网供应链统一服务平台",
                    "is_tax": 1, "tax_hits": [k for k in kws if k in title][:3],
                    "url": it["url"], "source_url": it["url"],
                }
                det = fetch_detail(it["url"])
                if det:
                    for k, v in det.items():
                        if v:
                            row[k] = v
                    stats["details"] += 1
                else:
                    stats["failed"] += 1
                self_exists = conn.execute(
                    "SELECT 1 FROM tender WHERE doc_uid = ?", (doc_uid,)).fetchone()
                if not self_exists:
                    dup = tmod.find_duplicate(conn, title, row.get("purchaser"))
                    if dup:
                        row["doc_uid"] = dup
                        # 审计 B4：重定向后 row["source"] 仍是"来方"，会覆盖原记录的
                        # 源归属。正确做法是**改回原记录的 source** —— 不能直接删掉：
                        # upsert 走 INSERT 时 source 为 NULL 会触发 NOT NULL 约束。
                        orig = conn.execute(
                            "SELECT source FROM tender WHERE doc_uid = ?",
                            (dup,)).fetchone()
                        if orig:
                            row["source"] = orig["source"]
                _, is_new = tmod.upsert(conn, row)
                stats["new" if is_new else "updated"] += 1
                time.sleep(sleep_between)
            conn.commit()
            log.info("南方电网 %s 第 %d 页：%d 条，累计匹配 %d",
                     col, page, len(items), stats["matched"])
            time.sleep(sleep_between)
    return stats
