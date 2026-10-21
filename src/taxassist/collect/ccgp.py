"""中国政府采购网（CCGP）招标公告采集。

## 站点特征（2026-10-21 实测）

- **列表页**：``https://www.ccgp.gov.cn/cggg/{zygg|dfgg}/index_N.htm``
  - ``zygg`` 中央公告、``dfgg`` 地方公告，各 20 条/页
  - 静态分页可用（index_1 / index_2 内容不重叠）
  - 条目链接形如 ``./gkzb/202610/t20261021_27538088.htm``
    （``gkzb``=公开招标、``zbgg``=中标、``gzgg``=更正、``cjgg``=成交、``qtgg``=其他）
- **反爬**：直接 HTTP 请求返回 **403**，必须走真浏览器
  （复用 ``collect/browser.py`` 的 nodriver；省级政策的 WAF 也是它处理的）
- **详情页**：表格结构，含项目编号/项目名称/预算金额/采购单位/截止时间

## 抓取策略：标题全量、详情按需

用户要"全部入库、默认只显示税务类"。但 CCGP 每天新增数千条，全抓详情
（一条 3-5 秒浏览器）要数小时。折中：

- **所有列表条目都入库**（标题、类型、日期、URL、is_tax 标记）
- **只有命中税务关键词的才抓详情**（补项目编号/预算/截止/正文）
- 其余条目页面上给"查看原文"直接跳 CCGP

这样每天约 6 分钟跑完（列表 50 页 + 详情 20-30 条）。
"""
from __future__ import annotations

import logging
import re
import time
from urllib.parse import urljoin

from . import browser

log = logging.getLogger(__name__)

BASE = "https://www.ccgp.gov.cn"
#: 两个列表入口：中央公告 / 地方公告。
LIST_SOURCES: dict[str, str] = {
    "ccgp_zygg": f"{BASE}/cggg/zygg/index_{{n}}.htm",
    "ccgp_dfgg": f"{BASE}/cggg/dfgg/index_{{n}}.htm",
}

#: 列表条目：``href="./gkzb/202610/t20261021_27538088.htm"``
_ITEM_RE = re.compile(
    r'<a[^>]+href="(\./(\w+)/(\d{6})/t(\d{8})_(\d+)\.htm)"[^>]*>(.*?)</a>',
    re.S)

#: 页间隔：连续请求同一个站太快容易触发风控，也给对方服务器留余地。
_PAGE_SLEEP = 1.0


def _strip_tags(html: str) -> str:
    html = re.sub(r"<script.*?</script>", " ", html, flags=re.S | re.I)
    html = re.sub(r"<style.*?</style>", " ", html, flags=re.S | re.I)
    html = re.sub(r"<[^>]+>", " ", html)
    html = html.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<")
    return re.sub(r"\s+", " ", html).strip()


def fetch_list_page(source: str, page: int) -> list[dict]:
    """抓一页列表，返回条目列表（不含详情）。"""
    url = LIST_SOURCES[source].format(n=page)
    html = browser.fetch_html(url, timeout_ms=45000)
    out: list[dict] = []
    seen: set[str] = set()
    for href, ntype, _ymonth, ymd, nid, raw_title in _ITEM_RE.findall(html):
        if nid in seen:
            continue
        seen.add(nid)
        title = _strip_tags(raw_title)
        if not title or len(title) < 5:
            continue
        out.append({
            "notice_id": nid,
            "notice_type": ntype,
            "url": urljoin(url, href),
            # 20261021 → 2026-10-21
            "publish_at": (f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}" if len(ymd) == 8 else None),
            "title": title,
            "region": _region_from_title(title),
        })
    return out


#: 从标题里认地区（地方公告标题常以省市开头）。只用于展示筛选，认不出就留空。
_REGION_HINTS: tuple[tuple[str, str], ...] = (
    ("北京", "北京"), ("上海", "上海"), ("天津", "天津"), ("重庆", "重庆"),
    ("广东", "广东"), ("广州", "广东"), ("深圳", "广东"),
    ("江苏", "江苏"), ("南京", "江苏"), ("苏州", "江苏"),
    ("浙江", "浙江"), ("杭州", "浙江"), ("宁波", "浙江"),
    ("山东", "山东"), ("济南", "山东"), ("青岛", "山东"),
    ("四川", "四川"), ("成都", "四川"), ("湖北", "湖北"), ("武汉", "湖北"),
    ("湖南", "湖南"), ("长沙", "湖南"), ("河南", "河南"), ("郑州", "河南"),
    ("河北", "河北"), ("石家庄", "河北"), ("福建", "福建"), ("福州", "福建"),
    ("安徽", "安徽"), ("合肥", "安徽"), ("陕西", "陕西"), ("西安", "陕西"),
    ("辽宁", "辽宁"), ("沈阳", "辽宁"), ("大连", "辽宁"),
    ("吉林", "吉林"), ("黑龙江", "黑龙江"), ("山西", "山西"), ("江西", "江西"),
    ("广西", "广西"), ("云南", "云南"), ("贵州", "贵州"), ("海南", "海南"),
    ("甘肃", "甘肃"), ("青海", "青海"), ("宁夏", "宁夏"), ("新疆", "新疆"),
    ("内蒙古", "内蒙古"), ("西藏", "西藏"),
)


def _region_from_title(title: str) -> str | None:
    for hint, region in _REGION_HINTS:
        if hint in (title or ""):
            return region
    return None


def _grab(text: str, pattern: str) -> str | None:
    m = re.search(pattern, text)
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else None


def parse_detail(html: str) -> dict:
    """从详情页 HTML 解析结构化字段。认不出的字段留 None（页面照常展示）。"""
    txt = _strip_tags(html)
    # 项目编号：兼容"项目编号："与表格里"项目编号 | 值"两种写法
    project_no = (_grab(txt, r"项目编号[：:]\s*([^\s，。]{4,60})")
                  or _grab(txt, r"项目编号\s*\|\s*([^|]{4,60})"))
    project_name = (_grab(txt, r"项目名称[：:]\s*([^，。]{4,80})")
                    or _grab(txt, r"采购项目名称\s*\|\s*([^|]{4,80})"))
    purchaser = (_grab(txt, r"采购单位\s*\|\s*([^|]{2,60})")
                 or _grab(txt, r"采购人[：:]\s*([^，。]{2,60})")
                 or _grab(txt, r"采购人名称[：:]\s*([^，。]{2,60})"))
    agency = (_grab(txt, r"采购代理机构\s*\|\s*([^|]{2,60})")
              or _grab(txt, r"代理机构[：:]\s*([^，。]{2,60})"))
    budget_raw = (_grab(txt, r"预算金额[：:]?\s*[￥¥]?\s*([\d.,]+\s*(?:万元|元|亿元))")
                  or _grab(txt, r"预算金额\s*\|\s*[￥¥]?\s*([\d.,]+\s*(?:万元|元|亿元))"))
    deadline = (_grab(txt, r"提交投标文件截止时间[：:]\s*([^。]{4,50})")
                or _grab(txt, r"投标截止时间[：:]\s*([^。]{4,50})")
                or _grab(txt, r"响应文件提交截止时间[：:]\s*([^。]{4,50})"))
    return {
        "project_no": project_no,
        "project_name": project_name,
        "purchaser": purchaser,
        "agency": agency,
        "budget": budget_raw,
        "deadline": deadline,
        # 正文给"税务相关"的条目留完整文本（供 AI 判断要不要投）
        "content": txt,
    }


def fetch_detail(item: dict) -> dict:
    """抓一条详情，返回补充字段。失败返回空 dict（列表级信息仍然入库）。"""
    try:
        html = browser.fetch_html(item["url"], timeout_ms=45000)
        got = parse_detail(html)
        return {k: v for k, v in got.items() if v}
    except Exception as e:  # noqa: BLE001 - 单条失败不中断整批
        log.warning("详情抓取失败 %s: %s: %s", item.get("url"), type(e).__name__, e)
        return {}


def collect(conn, *, days: int = 2, max_pages: int = 60,
            detail_limit: int | None = None) -> dict:
    """采集主流程。

    ``days``：往回抓几天（按列表页日期判断何时停）。
    ``max_pages``：每个源最多翻几页（防止源站改版导致无限翻）。
    ``detail_limit``：最多抓几条详情（测试用；None = 不限制税务相关的全部）。
    """
    from .. import tender as tmod

    tmod.ensure_table(conn)
    stats = {"pages": 0, "listed": 0, "new": 0, "updated": 0,
             "tax": 0, "details": 0, "failed": 0, "by_source": {}}
    cutoff = time.time() - days * 86400

    for source in LIST_SOURCES:
        src_stats = {"pages": 0, "listed": 0, "new": 0, "tax": 0}
        for page in range(1, max_pages + 1):
            try:
                items = fetch_list_page(source, page)
            except Exception as e:  # noqa: BLE001
                log.warning("列表抓取失败 %s p%d: %s: %s", source, page, type(e).__name__, e)
                stats["failed"] += 1
                break
            stats["pages"] += 1
            src_stats["pages"] += 1
            if not items:
                break

            oldest = None
            for it in items:
                stats["listed"] += 1
                src_stats["listed"] += 1
                hits = tmod.tax_hits(it["title"])
                is_tax = 1 if hits else 0
                row = {
                    "doc_uid": tmod.make_uid(source, it["notice_id"]),
                    "source": source,
                    "notice_type": it["notice_type"],
                    "title": it["title"],
                    "url": it["url"],
                    "publish_at": it["publish_at"],
                    "region": it["region"],
                    "is_tax": is_tax,
                    "tax_hits": hits,
                }
                # 税务相关 → 抓详情（补预算/截止/正文）；其余只存列表级信息
                if is_tax and (detail_limit is None or stats["details"] < detail_limit):
                    row.update(fetch_detail(it))
                    stats["details"] += 1
                    time.sleep(0.6)
                _, is_new = tmod.upsert(conn, row)
                stats["new" if is_new else "updated"] += 1
                src_stats["new" if is_new else "updated"] += 1
                if is_tax:
                    stats["tax"] += 1
                    src_stats["tax"] += 1
                oldest = it["publish_at"]

            conn.commit()
            log.info("%s 第 %d 页：%d 条，累计新增 %d",
                     source, page, len(items), src_stats["new"])
            # 该页所有条目都比截止日早 → 后面的页更早，停
            if oldest:
                try:
                    ts = time.mktime(time.strptime(oldest, "%Y-%m-%d"))
                    if ts < cutoff:
                        break
                except ValueError:
                    pass
            time.sleep(_PAGE_SLEEP)
        stats["by_source"][source] = src_stats

    conn.commit()
    return stats
