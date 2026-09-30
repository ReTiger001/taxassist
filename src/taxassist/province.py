"""省级税局静态列表页采集器。

======================================================================
适用形态（2026-09-29 实测）
======================================================================

列表页是**静态 HTML**，条目形如::

    <a href="/gdsw/ssfggds/2026-09/22/content_a11c79b2....shtml">
        广东省人民政府关于延续实施车辆车船税具体适用税额的通知
    </a>2026-09-22

标题在链接文本里，日期在其后的兄弟文本里。

======================================================================
**不适用**于上海这类 JS 异步加载站点
======================================================================

上海的首页与列表页都不含静态条目（实测详情链接数 = 0），只有老式 WAS 搜索接口。
若把同一解析器硬套上去，会得到"抓取成功但零条目"的**假成功** ——
比报错更危险，因为它看起来像"今天没有新政策"。
本模块遇到"列表页无条目"时会**显式报错**，绝不返回空列表当成功。

======================================================================
已知限制（如实标注，不粉饰）
======================================================================

- 只解析**第一页**：分页结构因站而异，尚未适配。对日常增量够用
  （每天新增通常不过几条），但首次全量导入会不全。
- **不抓详情页正文**：省级站点详情页结构与总局不同，需另行适配；
  当前入库的是标题、日期、链接与从标题抽取的文号。
"""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from urllib.parse import urljoin

from lxml import html as LH

from .collect.http import GuardedClient
from .collect.normalize import extract_full_doc_no, norm_text
from .db import now_iso

log = logging.getLogger(__name__)

_DATE_RE = re.compile(r"(20\d{2})-(\d{2})-(\d{2})")


class ListPageError(RuntimeError):
    """列表页结构异常（无条目 / 结构变更）——必须显式失败，不可静默返回空。"""


@dataclass(frozen=True)
class ListPageAdapter:
    """一个省级静态列表页的适配参数。"""

    source_id: str
    region: str
    site_name: str
    list_url: str
    detail_href_re: str          # 详情链接的正则（用于把条目与导航链接区分开）
    base_url: str
    column: str = "地方政策"
    # 站点是否受 JS 挑战（加速乐 WAF）保护，必须用真浏览器取页面。
    # 实测：山东/福建/湖北/湖南/四川/北京六省对普通 HTTP 请求返回 412，
    # 响应体是 WAF 的挑战 JS（$_ss/$_ts/nsd 特征）。curl_cffi 能过纯 TLS
    # 指纹检测（湖北已通），但过不了这种要执行 JS 的。
    needs_js: bool = False


# 已实测可解析的省级源。新增省级源必须先跑 scripts/probe_source.py 验证，
# 再把 detail_href_re 按实际路径写进来 —— 不要凭猜测填。
ADAPTERS: tuple[ListPageAdapter, ...] = (
    ListPageAdapter(
        source_id="gd_zcwj",
        region="广东",
        site_name="国家税务总局广东省税务局",
        list_url="http://guangdong.chinatax.gov.cn/gdsw/zcwj/zcwj.shtml",
        detail_href_re=r"/gdsw/[a-z]+/\d{4}-\d{2}/\d{2}/content_[0-9a-f]+\.shtml",
        base_url="http://guangdong.chinatax.gov.cn",
    ),
    # 江苏：实测为静态列表页，详情 URL 形如 /art/2026/9/4/art_23636_13344.html，
    # 日期直接带在条目里。注意这个栏目是「本省文件 + 转载总局文件」混排 ——
    # 与广东同样的问题，跨源去重靠标题，见 pipeline.collect_provincial。
    ListPageAdapter(
        source_id="js_zcfg",
        region="江苏",
        site_name="国家税务总局江苏省税务局",
        list_url="http://jiangsu.chinatax.gov.cn/col/col8199/index.html",
        detail_href_re=r"/art/\d{4}/\d{1,2}/\d{1,2}/art_\d+_\d+\.html",
        base_url="http://jiangsu.chinatax.gov.cn",
    ),
    # ---------------------------------------------------------------
    # 以下五省受**加速乐 WAF 的 JS 挑战**保护：普通 HTTP 请求一律 412，
    # 响应体是挑战脚本（特征 $_ss / $_ts / nsd）。换请求头无效（实测三种
    # 组合结果完全一致），curl_cffi 也过不了要执行 JS 的那种。
    # 故 needs_js=True，走真浏览器（collect/browser.py）。
    # 栏目 URL 由真浏览器实测确认，2026-09-30。
    # ---------------------------------------------------------------
    ListPageAdapter(
        source_id="sd_zcwj",
        region="山东",
        site_name="国家税务总局山东省税务局",
        list_url="http://shandong.chinatax.gov.cn/col/col5/index.html",
        detail_href_re=r"/art/\d{4}/\d{1,2}/\d{1,2}/art_\d+_\d+\.html",
        base_url="http://shandong.chinatax.gov.cn",
        needs_js=True,
    ),
    ListPageAdapter(
        source_id="fj_zcfg",
        region="福建",
        site_name="国家税务总局福建省税务局",
        list_url="http://fujian.chinatax.gov.cn/sszczl/",
        detail_href_re=r"/zfxxgkzl/zfxxgkml/zcfg/[a-z]+/\d{6}/t\d{8}_\d+\.htm",
        base_url="http://fujian.chinatax.gov.cn",
        needs_js=True,
    ),
    ListPageAdapter(
        source_id="hb_zcwj",
        region="湖北",
        site_name="国家税务总局湖北省税务局",
        list_url="http://hubei.chinatax.gov.cn/hbsw/zcwj/index.html",
        detail_href_re=r"/hbsw/zcwj/[a-z]+/\d+\.htm",
        base_url="http://hubei.chinatax.gov.cn",
        needs_js=True,
    ),
    ListPageAdapter(
        source_id="hn_zcwj",
        region="湖南",
        site_name="国家税务总局湖南省税务局",
        list_url="http://hunan.chinatax.gov.cn/category/20190624092865",
        detail_href_re=r"/show/\d+",
        base_url="http://hunan.chinatax.gov.cn",
        needs_js=True,
    ),
    ListPageAdapter(
        source_id="sc_zcfg",
        region="四川",
        site_name="国家税务总局四川省税务局",
        # 注意：col19973 是"政策法规库"，但真浏览器实测那里只有 1 个链接（ICP 备案号）；
        # 真正的政策列表在 col280。这是把候选栏目逐个试出来的结论。
        list_url="https://sichuan.chinatax.gov.cn/col/col280/index.html",
        detail_href_re=r"/art/\d{4}/\d{1,2}/\d{1,2}/art_\d+_\d+\.html",
        base_url="https://sichuan.chinatax.gov.cn",
        needs_js=True,
    ),
)

ADAPTERS_BY_ID: dict[str, ListPageAdapter] = {a.source_id: a for a in ADAPTERS}


_JS_WRAP_RE = re.compile(r"document\.write\(\s*['\"]|['\"]\s*\)\s*;?")


def _clean_title(raw: str | None) -> str:
    """清洗标题里残留的 JS 外壳。

    实测：湖北的列表条目是 JS 输出的，标题形如
    ``document.write('国家税务总局关于…的公告');`` —— 不清掉就会把这段
    JS 当成政策标题入库，而且它长得就像个标题，不容易发现。
    """
    t = norm_text(raw)
    if not t:
        return ""
    return norm_text(_JS_WRAP_RE.sub("", t))


def parse_list_page(html_text: str, adapter: ListPageAdapter) -> list[dict]:
    """解析静态列表页，返回条目列表。

    条目为空时抛 ``ListPageError``：零条目意味着"页面结构变了或不是静态页"，
    必须让人知道，不能让它伪装成"今天没有新政策"。
    """
    doc = LH.fromstring(html_text)
    pattern = re.compile(adapter.detail_href_re)
    items: list[dict] = []
    seen: set[str] = set()

    for a in doc.xpath("//a[@href]"):
        href = a.get("href") or ""
        if not pattern.search(href):
            continue
        url = urljoin(adapter.base_url, href)
        if url in seen:
            continue
        seen.add(url)

        # 标题优先取 <a> 的 title 属性。
        # 实测坑：广东列表页的 <a> 里除了标题 <font>，还嵌着"文件解读"图标的
        # <em>，直接用 text_content() 会把解读标题拼到正文标题后面，得到
        # "…公告关于《…公告》的解读" 这种畸形标题。
        title = norm_text(a.get("title"))
        if not title or len(title) < 6:
            title = None
            for child in a.iterchildren():
                candidate = norm_text(child.text_content())
                if candidate and len(candidate) >= 6:
                    title = candidate
                    break
            if not title:
                title = norm_text(a.text_content())
        if not title or len(title) < 6:
            continue

        # 日期通常在链接之后的同级文本里（实测形态："标题</a>2026-09-22"）
        parent_text = ""
        parent = a.getparent()
        if parent is not None:
            parent_text = " ".join(parent.text_content().split())
        m = _DATE_RE.search(parent_text) or _DATE_RE.search(url)
        cwrq = f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None

        items.append({
            "url": url,
            "title": _clean_title(title),
            "cwrq": cwrq,
            "doc_uid": f"{adapter.source_id}:" + hashlib.md5(url.encode()).hexdigest()[:16],
        })

    if not items:
        raise ListPageError(
            f"[{adapter.source_id}] 列表页未解析出任何条目：{adapter.list_url}。"
            "页面可能已改版，或该站实际是 JS 异步加载（参见模块头部关于「假成功」的说明）。"
            "请重跑 scripts/probe_source.py 复核后再改适配参数。"
        )
    return items


def build_provincial_row(item: dict, adapter: ListPageAdapter) -> dict:
    """把列表页条目转成 policy 表的一行。"""
    doc_no = extract_full_doc_no(item.get("title"))
    return {
        "doc_uid": item["doc_uid"],
        "url": item["url"],
        "snapshot_url": None,
        "url_md5": None,
        "title": item["title"],
        "o_column": adapter.column,
        "o_site_name": adapter.site_name,
        "o_label": "地方文件",
        "p_region": adapter.region,          # 地区维度由采集器直接给出，可靠
        "cwrq": item.get("cwrq"),
        "pub_date": item.get("cwrq"),
        "p_doc_no_full": doc_no,
        "p_doc_no_confidence": "high" if doc_no else "low",
        "pub_name": adapter.site_name,
        "first_seen_at": now_iso(),
        "last_seen_at": now_iso(),
    }


def fetch_list_page(client: GuardedClient, adapter: ListPageAdapter) -> list[dict]:
    """抓取并解析一个省级列表页。

    受 JS 挑战保护的站点走**真浏览器**（见 collect/browser.py）。
    不走这条的话会拿到 412 挑战页、解析出 0 条，而"零条目即报错"会把
    它报成"页面改版"—— 掩盖真实原因。所以这里必须按适配器分流。
    """
    if adapter.needs_js:
        from .collect.browser import fetch_html

        return parse_list_page(fetch_html(adapter.list_url), adapter)

    resp = client.get(adapter.list_url)
    resp.raise_for_status()
    # 省级站点多为 UTF-8；gb18030 兜底避免个别站点乱码导致解析失败
    try:
        text = resp.content.decode("utf-8")
    except UnicodeDecodeError:
        text = resp.content.decode("gb18030", "replace")
    return parse_list_page(text, adapter)
