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

#: 31 个省级地区编码（官方搜索接口的 ``searchArea`` 参数值）。
#:
#: **必须用 6 位编码**：传中文"北京市"服务端会忽略该条件、返回全国结果
#: （实测：'北京市'→498 条全量，'110000'→61 条北京）。这决定了「按地区遍历」
#: 能不能生效。
AREA_CODES: dict[str, str] = {
    "110000": "北京", "120000": "天津", "130000": "河北", "140000": "山西",
    "150000": "内蒙古", "210000": "辽宁", "220000": "吉林", "230000": "黑龙江",
    "310000": "上海", "320000": "江苏", "330000": "浙江", "340000": "安徽",
    "350000": "福建", "360000": "江西", "370000": "山东", "410000": "河南",
    "420000": "湖北", "430000": "湖南", "440000": "广东", "450000": "广西",
    "460000": "海南", "500000": "重庆", "510000": "四川", "520000": "贵州",
    "530000": "云南", "540000": "西藏", "610000": "陕西", "620000": "甘肃",
    "630000": "青海", "640000": "宁夏", "650000": "新疆",
}

#: 官方查询接口（POST，form-urlencoded）。参数必须**全部出现**（缺一个就返回空，
#: 哪怕那个参数是空值）—— 这是 2026-10-22 调了半天返回 0 条的原因。
_API_URL = ("https://www.cebpubservice.com/ctpsp_iiss/"
            "searchbusinesstypebeforedooraction/getStringMethod.do")
_API_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 Chrome/120 Safari/537.36",
    "Referer": ("https://www.cebpubservice.com/ctpsp_iiss/"
                "searchbusinesstypebeforedooraction/getSearch.do"),
    "X-Requested-With": "XMLHttpRequest",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
}


def _api_body(keyword: str, area: str = "") -> str:
    """拼请求体。**空字段也必须带上**（见 _API_URL 注释）。"""
    from urllib.parse import quote

    return (
        f"searchName={quote(keyword)}&searchArea={quote(area)}"
        "&searchIndustry=&centerPlat="
        f"&businessType={quote('招标公告')}"
        "&searchTimeStart=&searchTimeStop=&timeTypeParam="
        "&bulletinIssnTime=&bulletinIssnTimeStart=&bulletinIssnTimeStop="
        "&pageNo=1&row=15"
    )


def api_search(client, keyword: str, area: str = "",
               retries: int = 2) -> list[dict]:
    """直调官方查询接口（第 1 页，15 条）。

    **比浏览器快 100 倍**（0.1 秒 vs 20 秒）。限制是只能拿第 1 页——翻页在
    服务端要 vaptcha token；但「地区 × 关键词」组合能绕过这个限制：每个组合
    都是独立的第 1 页查询。

    **有频率限制**：连续几百次后返回非 JSON（空/拦截页）。实测 640 次后被拦，
    所以调用方要把 ``sleep_between`` 放大，并且这里对非 JSON 响应做短退避重试。
    """
    for attempt in range(retries + 1):
        try:
            r = client.post(_API_URL, content=_api_body(keyword, area), timeout=30)
            j = r.json()
            items = (j.get("object") or {}).get("returnlist") or []
            break
        except Exception as e:  # noqa: BLE001
            if attempt >= retries:
                log.debug("查询「%s/%s」失败（已重试）：%s: %s",
                          keyword, area, type(e).__name__, e)
                return []
            time.sleep(1.5 * (attempt + 1))   # 退避：被限流时等一会儿再试
    else:
        return []
    out: list[dict] = []
    for it in items:
        title = (it.get("bulletinName") or it.get("title")
                 or it.get("businessObjectName") or "")
        if not title:
            continue
        out.append({
            "title": str(title),
            "industry": it.get("industryName") or it.get("industry"),
            "region": AREA_CODES.get(area) or it.get("areaName"),
            "platform": it.get("transactionPlatfName") or it.get("platfName"),
            "publish_at": str(it.get("bulletinIssnTime") or it.get("publishTime") or "")[:10],
            "deadline": str(it.get("bulletinEndTime") or "")[:10],
        })
    return out


#: 用来覆盖"税务服务标"的关键词。
#:
#: 覆盖三类：①通用服务名（税务服务/税务咨询）②具体业务（汇算清缴/转让定价/
#: 税务尽调）③交付形态（税务筹划/税务审计/税务合规）。
#: 与 31 个地区编码做笛卡尔积 = 620 次查询，每次约 0.1 秒。
SEARCH_KEYWORDS: list[str] = [
    "税务服务", "税务咨询", "汇算清缴", "涉税服务", "税务代理",
    "税务尽调", "税务顾问", "涉税鉴证", "税务筹划", "转让定价",
    "税务审计", "涉税审计", "税务合规", "税收风险", "税务争议",
    "纳税申报", "税务外包", "税务健康检查", "税务复核", "税收筹划",
]

#: 采购人提取：标题通常以"XX有限公司/集团/局/院"开头。
#:
#: 列表页没有采购人字段，详情页要过验证码（`showDetails` 里是 `withVerification`，
#: 不是"首次免验证"），所以从标题**启发式推断** —— 比显示空白有用得多。
_PURCHASER_RE = re.compile(
    r"^(.{2,40}?(?:股份有限公司|有限责任公司|有限公司|集团公司|集团|公司|"
    r"研究院|事务所|大学|医院|中心|管理局|税务局|局|院|厂))")


def extract_purchaser(title: str) -> str | None:
    """从公告标题里推断采购人。认不出返回 None（页面显示"—"）。"""
    m = _PURCHASER_RE.match((title or "").strip())
    return m.group(1) if m else None

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
    title = row.get("title") or ""
    return {
        "doc_uid": _doc_uid(row),
        "source": "cebpub",
        "notice_type": "gkzb",
        # 搜索页固定按「招标公告」类型查 → 都是可投标阶段
        "bid_process": "招标",
        "title": title,
        # 采购人从标题推断（列表页没有该字段，详情页要验证码）
        "purchaser": extract_purchaser(title),
        "region": row.get("region"),
        "publish_at": row.get("publish_at"),
        "deadline": row.get("deadline"),
        # 平台名放到 agency（页面上「代理机构」一栏显示，比丢掉有用）
        "agency": row.get("platform"),
        "is_tax": 1,
        "tax_hits": ["税务服务"],
        "summary": f"{row.get('industry') or ''} | {row.get('platform') or ''}".strip(" |"),
    }


def collect(conn, *, keywords: list[str] | None = None,
            areas: dict[str, str] | None = None,
            sleep_between: float = 0.5) -> dict:
    """采集主流程：**地区 × 关键词**组合，全部走 API 直调。

    为什么按地区遍历：官方接口**只能拿第 1 页**（翻页要 vaptcha token），
    而每个「地区+关键词」组合都是独立的第 1 页查询 —— 31 省 × 20 词 = 620 次
    查询约 1-2 分钟，能把全国约 500 条税务标覆盖掉大部分。

    ``areas={}`` 表示只查全国（不带地区筛选，20 次）。
    """
    import httpx

    from .. import tender as tmod

    tmod.ensure_table(conn)
    kws = keywords or SEARCH_KEYWORDS
    # areas 为 None → 遍历 31 省；传 {} → 只查全国
    area_map: dict[str, str] = AREA_CODES if areas is None else areas
    stats = {"queries": 0, "fetched": 0, "new": 0, "updated": 0,
             "duplicates": 0, "failed": 0}
    with httpx.Client(headers=_API_HEADERS, timeout=30,
                      follow_redirects=True) as client:
        # 先查一次全国（不带地区），拿到不受地区归属影响的全量首页
        tasks: list[tuple[str, str]] = [("", "")] if area_map else []
        tasks += [(code, name) for code, name in area_map.items()]
        for area_code, area_name in tasks:
            for kw in kws:
                rows = api_search(client, kw, area_code)
                stats["queries"] += 1
                if not rows:
                    continue
                for r in rows:
                    row = to_tender_row(r)
                    if area_name and not row.get("region"):
                        row["region"] = area_name
                    stats["fetched"] += 1
                    # 去重：先看自己是否存在，不存在再按标题找重复
                    self_exists = conn.execute(
                        "SELECT 1 FROM tender WHERE doc_uid = ?",
                        (row["doc_uid"],)).fetchone()
                    if not self_exists:
                        dup = tmod.find_duplicate(conn, row["title"],
                                                  row.get("purchaser"))
                        if dup:
                            stats["duplicates"] += 1
                            row["doc_uid"] = dup
                    _, is_new = tmod.upsert(conn, row)
                    stats["new" if is_new else "updated"] += 1
                conn.commit()
                time.sleep(sleep_between)
    log.info("官方源采集完成：%d 次查询 / 抓 %d 条 / 新增 %d",
             stats["queries"], stats["fetched"], stats["new"])
    return stats
