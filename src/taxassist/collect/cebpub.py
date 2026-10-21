"""中国招标投标公共服务平台（cebpubservice.com）采集 —— **官方上游数据源**。

## 为什么这个源最重要

按《电子招标投标办法》，所有招标公告都必须同步到这个平台。所以它覆盖的是
**全部企业招标平台**（中国海油、甘肃智慧阳光、安徽省能源集团、中国招标采购
云中心…），比任何第三方聚合站都全。实测搜「税务」有 **497 条有效期内公告**。

## 怎么绕过验证码 —— 不绕，走它自己的「首次免验证」

页面 JS（`jiaoyigongkai_shuakuai.js`）里写着：

    if (isFirstQuery) { isFirstQuery = false; executeBusinessQuery(); }  // 免验证
    else { withVerification(...) }                                       // 要 vaptcha

所以只要**新开一个浏览器实例**（`isFirstQuery` 是页面级变量），第一次查询就
免验证。代价是每次查询要付一次浏览器启动（约 15-20 秒），换来**无额度限制、
无验证码破解**。

关键调用：直接执行页面内部的 `performSearchRequest(null)` —— 复用它自己的
请求构造（参数一个都不用猜），而不是手拼 API。

## 已知限制

- 一次浏览器实例**只稳定拿到第一页**（15 条）。翻页变量是闭包，外部改不动，
  所以用**多关键词覆盖**：10 个税务相关词 × 15 条 = 约 150 条/轮。
- 搜索页按「招标公告」类型查，结果天然都是可投标阶段。
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time

log = logging.getLogger(__name__)

SEARCH_URL = ("https://www.cebpubservice.com/ctpsp_iiss/"
              "searchbusinesstypebeforedooraction/getSearch.do")

#: 用来覆盖"税务服务标"的关键词。每个词一次浏览器查询。
SEARCH_KEYWORDS: list[str] = [
    "税务服务", "税务咨询", "汇算清缴", "涉税服务", "税务代理",
    "税务尽调", "税务顾问", "涉税鉴证", "税务筹划", "转让定价",
]

_ROWS_RE = re.compile(r'<tbody id="tenderBulletin">(.*?)</tbody>', re.S)
_TR_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
_TD_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
_TAG_RE = re.compile(r"<[^>]+>")


def _clean(html: str) -> str:
    return re.sub(r"\s+", " ", _TAG_RE.sub("", html or "")).strip()


def parse_rows(html: str) -> list[dict]:
    """从搜索结果表格里解析条目。

    每行 6 列：公告名称 / 所属专业 / 所属区域 / 所属平台 / 公告发布时间 / 结束时间。
    """
    m = _ROWS_RE.search(html or "")
    if not m:
        return []
    out: list[dict] = []
    for tr in _TR_RE.findall(m.group(1)):
        cells = [_clean(td) for td in _TD_RE.findall(tr)]
        if len(cells) < 6 or not cells[0]:
            continue
        title = cells[0]
        # 区域形如「【北京市】」「【甘肃省 市辖区】」——取括号内第一段
        region_raw = cells[2]
        mr = re.search(r"【([^】]+)】", region_raw)
        region = (mr.group(1).split()[0] if mr else region_raw) or None
        out.append({
            "title": title,
            "industry": cells[1] or None,
            "region": region,
            "platform": cells[3] or None,
            "publish_at": cells[4] or None,
            "deadline": cells[5] or None,
        })
    return out


async def _search_async(keyword: str, wait_sec: float = 9.0) -> list[dict]:
    """开一个浏览器实例，走「首次免验证」查询一个关键词，返回解析后的条目。"""
    import nodriver as uc

    from .browser import _find_browser_binary

    browser = await uc.start(headless=True,
                             browser_executable_path=_find_browser_binary())
    try:
        page = await browser.get(SEARCH_URL)
        await page.sleep(5)
        # 直接调页面内部函数：参数由它自己构造，我们只填关键词
        js = (
            "(function(){var el=document.getElementById('keySearchValue');"
            "if(el){el.value=" + repr(keyword).replace("'", '"') + ";}"
            "if(typeof performSearchRequest==='function'){"
            "performSearchRequest(null); return 'called';}"
            "return 'no-fn';})()"
        )
        got = await page.evaluate(js)
        if got != "called":
            log.warning("页面内搜索函数不可用：%r", got)
            return []
        await page.sleep(wait_sec)
        html = await page.get_content()
        return parse_rows(html)
    finally:
        browser.stop()


def search(keyword: str, wait_sec: float = 9.0) -> list[dict]:
    """同步入口：查一个关键词。"""
    try:
        return asyncio.run(_search_async(keyword, wait_sec=wait_sec))
    except Exception as e:  # noqa: BLE001 - 单关键词失败不中断整批
        log.warning("查询「%s」失败：%s: %s", keyword, type(e).__name__, e)
        return []


def _doc_uid(row: dict) -> str:
    """详情页没有稳定 id 暴露在列表里，用「标题+平台+发布时间」哈希当唯一键。"""
    raw = f"{row.get('title')}|{row.get('platform')}|{row.get('publish_at')}"
    return "cebpub:" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def to_tender_row(row: dict) -> dict:
    """映射成 tender 表的一行。"""
    return {
        "doc_uid": _doc_uid(row),
        "source": "cebpub",
        "notice_type": "gkzb",
        # 搜索页固定按「招标公告」类型查 → 都是可投标阶段
        "bid_process": "招标",
        "title": row.get("title") or "",
        "purchaser": None,          # 列表页没有采购人字段，详情页才需要再抓
        "region": row.get("region"),
        "publish_at": row.get("publish_at"),
        "deadline": row.get("deadline"),
        # 平台名放到 agency（页面上"代理机构"一栏显示，比丢掉有用）
        "agency": row.get("platform"),
        "is_tax": 1,
        "tax_hits": ["税务服务"],
        "summary": f"{row.get('industry') or ''} | {row.get('platform') or ''}".strip(" |"),
    }


def collect(conn, *, keywords: list[str] | None = None,
            sleep_between: float = 2.0) -> dict:
    """采集主流程：逐关键词查询 → 去重 → 入库。"""
    from .. import tender as tmod

    tmod.ensure_table(conn)
    kws = keywords or SEARCH_KEYWORDS
    stats = {"keywords": 0, "fetched": 0, "new": 0, "updated": 0,
             "duplicates": 0, "failed": 0}
    for kw in kws:
        rows = search(kw)
        stats["keywords"] += 1
        if not rows:
            stats["failed"] += 1
            log.warning("关键词「%s」无结果", kw)
            continue
        for r in rows:
            row = to_tender_row(r)
            stats["fetched"] += 1
            # 去重：先看自己是否存在，不存在再按标题找重复
            self_exists = conn.execute(
                "SELECT 1 FROM tender WHERE doc_uid = ?", (row["doc_uid"],)).fetchone()
            if not self_exists:
                dup = tmod.find_duplicate(conn, row["title"], row.get("purchaser"))
                if dup:
                    stats["duplicates"] += 1
                    row["doc_uid"] = dup
            _, is_new = tmod.upsert(conn, row)
            stats["new" if is_new else "updated"] += 1
        conn.commit()
        log.info("关键词「%s」：%d 条，累计新增 %d", kw, len(rows), stats["new"])
        time.sleep(sleep_between)
    return stats
