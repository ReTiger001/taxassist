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
from datetime import date        # 吉林列表页只给「月-日」，补年份要用
from urllib.parse import urljoin

from lxml import html as LH

from .collect.http import GuardedClient
from .collect.normalize import extract_full_doc_no, norm_text
from .db import now_iso

log = logging.getLogger(__name__)

_DATE_RE = re.compile(r"(20\d\d)-(\d{1,2})-(\d{1,2})")
# 实测还有两种不完整写法。不覆盖的话，整个省的日期都是空的（实测吉林 19/19、
# 宁夏 18/18、四川 11/11、甘肃 8/8、山西 6/6 全空）：
#   甘肃 '28 2026-09 国家税务总局关于…'      —— 只有 年-月
#   吉林 '国家税务总局关于…的公告 [09-04]'    —— 只有 月-日
# 补出来的日期不是原文写的，所以保守处理：缺日补 01、缺年补当年；
# 且它只用于排序与展示，不参与任何效力判断。
_DATE_YM_RE = re.compile(r"(20\d\d)[-/年](\d{1,2})(?![-/月\d])")
_DATE_MD_RE = re.compile(r"[\[\(（]\s*(\d{1,2})[-/月](\d{1,2})\s*[\]\)）]")


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
    ListPageAdapter(
        source_id="bj_sszc",
        region="北京",
        site_name="国家税务总局北京市税务局",
        list_url="http://beijing.chinatax.gov.cn/bjswj/c104343/sszc.shtml",
        detail_href_re=r"/bjswj/sszc/zxwj/\d{6}/[0-9a-f]{16,}\.shtml",
        base_url="http://beijing.chinatax.gov.cn",
        needs_js=True,
    ),
    ListPageAdapter(
        source_id="sh_zcfgk",
        region="上海",
        site_name="国家税务总局上海市税务局",
        list_url="http://shanghai.chinatax.gov.cn/zcfw/zcfgk/",
        # 上海按税种分子目录（zzs=增值税、grsds=个人所得税…），故税种段用通配。
        # 注意：列表里的 href 是**相对路径**（"./zzs/202609/t481485.html"）。
        # detail_href_re 匹配的是 href 原文，不是 urljoin 之后的绝对 URL，
        # 所以这里不能带 /zcfw/zcfgk/ 前缀 —— 带上就一条都匹配不到（实测踩过）。
        detail_href_re=r"\./[a-z]+/\d{6}/t\d+\.html",
        # base_url 必须是**栏目路径**而非域名根：上海列表里的链接是
        # 相对于栏目页的 "./zzs/202609/t481485.html"。用域名根拼出来会少一层
        # /zcfw/zcfgk/，变成不存在的地址。
        base_url="http://shanghai.chinatax.gov.cn/zcfw/zcfgk/",
        needs_js=True,
    ),
    ListPageAdapter(
        source_id="zj_zcwj",
        region="浙江",
        site_name="国家税务总局浙江省税务局",
        # 注意：col13300 名为「政策法规库」，但真浏览器实测那里只有备案号链接；
        # 真正的政策列表在 col13296。别照名字选栏目。
        list_url="http://zhejiang.chinatax.gov.cn/col/col13296/index.html",
        detail_href_re=r"/art/\d{4}/\d{1,2}/\d{1,2}/art_\d+_\d+\.html",
        base_url="http://zhejiang.chinatax.gov.cn",
        needs_js=True,
    ),
    ListPageAdapter(
        source_id="henan_zcwj",   # 注意：不能用 hn_ 前缀，湖南已占用 hn_zcwj
        region="河南",
        site_name="国家税务总局河南省税务局",
        list_url="https://henan.chinatax.gov.cn/zcwj/",
        detail_href_re=r"/20\d\d/\d{2}-\d{2}/\d+\.html",
        base_url="https://henan.chinatax.gov.cn",
        needs_js=True,
    ),
    ListPageAdapter(
        source_id="ah_zcfg",
        region="安徽",
        site_name="国家税务总局安徽省税务局",
        list_url="http://anhui.chinatax.gov.cn/col/col9416/index.html",
        detail_href_re=r"/art/\d{4}/\d{1,2}/\d{1,2}/art_\d+_\d+\.html",
        base_url="http://anhui.chinatax.gov.cn",
        needs_js=True,
    ),
    ListPageAdapter(
        source_id="jx_zcwj",
        region="江西",
        site_name="国家税务总局江西省税务局",
        list_url="http://jiangxi.chinatax.gov.cn/col/col31015/index.html",
        # 江西的 href 是**绝对 URL**，上海的是相对路径 "./..." ——
        # 正则只取 path 部分，所以同一套写法对两种形式都成立。
        detail_href_re=r"/art/\d{4}/\d{1,2}/\d{1,2}/art_\d+_\d+\.html",
        base_url="http://jiangxi.chinatax.gov.cn",
        needs_js=True,
    ),
    ListPageAdapter(
        source_id="shaanxi_zcwj",
        region="陕西",
        site_name="国家税务总局陕西省税务局",
        list_url="http://shaanxi.chinatax.gov.cn/col/col3899/index.html",
        detail_href_re=r"/art/\d{4}/\d{1,2}/\d{1,2}/art_\d+_\d+\.html",
        base_url="http://shaanxi.chinatax.gov.cn",
        needs_js=True,
    ),
    ListPageAdapter(
        source_id="gx_zcwj",
        region="广西",
        site_name="国家税务总局广西壮族自治区税务局",
        list_url="https://guangxi.chinatax.gov.cn/zcwj/",
        # href 是相对路径 "./zxwj/202609/t20260930_440809.html"，
        # 正则匹配 href 原文，不能带域名或上级路径（上海的教训）。
        # 同一套 CMS 下并列三个栏目：zxwj 最新文件 / zcjd 政策解读 / rdwd 热点问答。
        # 只写 zxwj 会漏掉一半（实测候选 48 条只匹配到 14 条）。
        detail_href_re=r"(?:zxwj|zcjd|rdwd)/\d{6}/t\d+_\d+\.html",
        base_url="https://guangxi.chinatax.gov.cn/zcwj/",
        needs_js=True,
    ),
    ListPageAdapter(
        source_id="yn_zcwj",
        region="云南",
        site_name="国家税务总局云南省税务局",
        list_url="http://yunnan.chinatax.gov.cn/col/col3831/index.html",
        detail_href_re=r"/art/\d{4}/\d{1,2}/\d{1,2}/art_\d+_\d+\.html",
        base_url="http://yunnan.chinatax.gov.cn",
        needs_js=True,
    ),
    ListPageAdapter(
        source_id="gz_zcwj",
        region="贵州",
        site_name="国家税务总局贵州省税务局",
        list_url="http://guizhou.chinatax.gov.cn/wjjb/",
        # 贵州按"税种/子类"分两级目录（szfl/zzs = 税收法规/增值税）
        detail_href_re=r"/wjjb/zcfgk/[a-z]+/[a-z]+/\d{6}/t\d+",
        base_url="http://guizhou.chinatax.gov.cn",
        needs_js=True,
    ),
    ListPageAdapter(
        source_id="sx_zcwj",
        region="山西",
        site_name="国家税务总局山西省税务局",
        list_url="http://shanxi.chinatax.gov.cn/zcwj",
        # 山西用 /web/detail/sx-{栏目}-{栏目}-{id} 形式，与其它省的 /art/ 不同
        detail_href_re=r"/web/detail/sx-\d+-\d+-\d+",
        base_url="http://shanxi.chinatax.gov.cn",
        needs_js=True,
    ),
    ListPageAdapter(
        source_id="hlj_zcwj",
        region="黑龙江",
        site_name="国家税务总局黑龙江省税务局",
        list_url="http://heilongjiang.chinatax.gov.cn/col/col7573/index.html",
        detail_href_re=r"/art/\d{4}/\d{1,2}/\d{1,2}/art_\d+_\d+\.html",
        base_url="http://heilongjiang.chinatax.gov.cn",
        needs_js=True,
    ),
    ListPageAdapter(
        source_id="jl_zcwj",
        region="吉林",
        site_name="国家税务总局吉林省税务局",
        list_url="http://jilin.chinatax.gov.cn/col/col6311/index.html",
        detail_href_re=r"/art/\d{4}/\d{1,2}/\d{1,2}/art_\d+_\d+\.html",
        base_url="http://jilin.chinatax.gov.cn",
        needs_js=True,
    ),
    ListPageAdapter(
        source_id="nmg_zcwj",
        region="内蒙古",
        site_name="国家税务总局内蒙古自治区税务局",
        list_url="http://neimenggu.chinatax.gov.cn/zcwj",
        # 同广西：zxwj / zcjd / rdwd 三个栏目并列，只写一个会漏大半。
        detail_href_re=r"(?:zxwj|zcjd|rdwd|tjss)/\d{6}/t\d+_\d+\.html",
        base_url="http://neimenggu.chinatax.gov.cn/zcwj/",
        needs_js=True,
    ),
    ListPageAdapter(
        source_id="gs_zcwj",
        region="甘肃",
        site_name="国家税务总局甘肃省税务局",
        list_url="http://gansu.chinatax.gov.cn/col/col4/index.html",
        detail_href_re=r"/art/\d{4}/\d{1,2}/\d{1,2}/art_\d+_\d+\.html",
        base_url="http://gansu.chinatax.gov.cn",
        needs_js=True,
    ),
    # 以下三省（河北 sszc、重庆 zcwj、海南 zcwj）实测**栏目能打开但列表取不到条目**，
    # 推测列表本身是二次异步加载（浏览器拿到的是壳）。适配器已撤下 ——
    # 留着它们每天抓取都会记一条 failed，污染 fetch_log、掩盖真实故障。
    # 要接需要先找到列表的 XHR 接口（浏览器开发者工具 → Network → XHR）。
    ListPageAdapter(
        source_id="nx_zcwj",
        region="宁夏",
        site_name="国家税务总局宁夏回族自治区税务局",
        list_url="http://ningxia.chinatax.gov.cn/col/col10983/index.html",
        detail_href_re=r"/art/\d{4}/\d{1,2}/\d{1,2}/art_\d+_\d+\.html",
        base_url="http://ningxia.chinatax.gov.cn",
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

        # 日期搜索范围逐步放宽：父元素 → 链接文本（吉林的日期在 <a> 里，
        # 不在父元素）→ URL（部分站把年月日编进路径）
        parent_text = ""
        parent = a.getparent()
        if parent is not None:
            parent_text = " ".join(parent.text_content().split())
        haystack = f"{parent_text} {_clean_title(title)} {url}"

        m = _DATE_RE.search(haystack)
        if m:
            cwrq = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
        else:
            m2 = _DATE_YM_RE.search(haystack)
            if m2:                                    # 只有年月（甘肃）
                cwrq = f"{m2.group(1)}-{int(m2.group(2)):02d}-01"
            else:
                m3 = _DATE_MD_RE.search(haystack)
                # 只有月日（吉林），年份补当年 —— 这些都是补出来的，
                # 只用于排序展示，不参与效力判断。
                cwrq = (f"{date.today().year}-{int(m3.group(1)):02d}-{int(m3.group(2)):02d}"
                        if m3 else None)

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
