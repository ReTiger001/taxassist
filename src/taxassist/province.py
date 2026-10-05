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
from dataclasses import dataclass, replace
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
# URL 里的完整日期。四川列表页只显示「月-日」（<span>09-28</span>），哪条
# 正则都不认；但详情 URL 里有站点自己写的年月日：
# /art/2026/9/28/art_19973_21901.html。比「补当年」准 —— 那是站点的事实，
# 不是我们的推断。这条对江苏/黑龙江/宁夏等一切 /art/YYYY/M/D/ 站点兜底。
_DATE_YMD_SLASH_RE = re.compile(r"(20\d\d)/(\d{1,2})/(\d{1,2})(?!\d)")
# 裸「月-日」，无括号。山西/四川的 <span>09-28</span> 就是这种 ——
# 两边都不带括号，_DATE_MD_RE 认不出。用前后边界（行首/空白/标签）限定，
# 并在调用处校验 月≤12、日≤31，避免把 "3-5 个工作日" 这类当日期。
_DATE_MD_BARE_RE = re.compile(r"(?:^|[\s>])(\d{1,2})[-/月](\d{1,2})(?:[\s<]|$)")


class ListPageError(RuntimeError):
    """列表页结构异常（无条目 / 结构变更）——必须显式失败，不可静默返回空。"""


# 源配置已切到 province_sources（见该文件头部的说明）：
# 这里 re-export，外部 `from .province import ADAPTERS` 等用法不变。
from .province_sources import (  # noqa: F401
    ADAPTERS,
    ADAPTERS_BY_ID,
    ListPageAdapter,
)



_JS_WRAP_RE = re.compile(r"document\.write\(\s*['\"]|['\"]\s*\)\s*;?")
# 天津等站的 <a title="[发文机关]标题"> 把发文机关塞进方括号前缀，
# 不清掉标题就变成"[国家税务总局]关于…的公告"。只去**开头**的一对方括号，
# 标题正文里的书名号/方括号不动。
_TITLE_BRACKET_PREFIX_RE = re.compile(r"^\s*[\[【][^\]】]{2,30}[\]】]\s*")
# 广西的列表把日期和标题挤在同一段 <a> 文本里："2026-09-28 国家税务总局关于…"。
# 不清掉就成了"日期当标题"（实测 7 条入库后标题只剩日期）。
# 只在后面还有足够长的正文时才剥 —— 纯日期标题（那种情况标题本来就没抓到）
# 留着更能暴露问题，而不是悄悄变成空标题。
_TITLE_DATE_PREFIX_RE = re.compile(r"^\s*20\d\d[-/年]\d{1,2}[-/月]\d{1,2}日?\s*[-—－]?\s*")
# 「整个字符串就是一个日期」—— 用来识别日期格子，别把它当标题。
_DATE_ONLY_RE = re.compile(r"^\[?20\d\d[-/年]\d{1,2}[-/月]\d{1,2}日?\]?$")


def _clean_title(raw: str | None) -> str:
    """清洗标题里残留的外壳。

    两种都是实际踩到的：
    - 湖北的列表条目是 JS 输出的，标题形如
      ``document.write('国家税务总局关于…的公告');`` —— 不清掉就会把这段
      JS 当成政策标题入库，而且它长得就像个标题，不容易发现；
    - 天津的 ``<a title="[国家税务总局天津市税务局]市医保局…">``，发文机关
      被塞在方括号里，不清掉标题就成了"[国家税务总局]关于…的公告"。
    """
    t = norm_text(raw)
    if not t:
        return ""
    t = norm_text(_JS_WRAP_RE.sub("", t))
    t = _TITLE_BRACKET_PREFIX_RE.sub("", t)
    # 剥掉"日期 + 标题"里的日期前缀（广西）。剥完太短说明本来就只有日期，
    # 那就保留原样 —— 让"没抓到标题"这件事在数据里看得见。
    stripped = _TITLE_DATE_PREFIX_RE.sub("", t)
    if len(stripped) >= 6:
        t = stripped
    return t


# layui 表格站：**数据全塞在 <script> 的 datajson.push({...}) 里**，
# DOM 只渲染当前页。实测湖北政策法规库 DOM 10 条 / 脚本 2749 条，差 275 倍 ——
# 只按 DOM 解析会安静地少抓 99%，而且页面"看起来是好的"，极难发现。
_LAYUI_ENTRY_RE = re.compile(r"datajson\.push\(\s*\{(?P<body>.*?)\}\s*\)", re.S)
_LAYUI_TITLE_RE = re.compile(r'href="([^"]+)"[^>]*>(.*?)</a>', re.S)
_LAYUI_DATE_RE = re.compile(r'"publishDate"\s*:\s*[\'"]([^\'"]+)[\'"]')


def _parse_layui_datajson(html_text: str, pattern: re.Pattern,
                          adapter: ListPageAdapter) -> list[dict]:
    """从 layui 的 datajson 脚本数据里取条目；页面没有这种脚本时返回空表。"""
    items: list[dict] = []
    seen: set[str] = set()
    for m in _LAYUI_ENTRY_RE.finditer(html_text):
        block = m.group("body")
        t = _LAYUI_TITLE_RE.search(block)
        if not t:
            continue
        href, raw_title = t.group(1), t.group(2)
        if not pattern.search(href):
            continue
        title = _clean_title(norm_text(re.sub(r"<[^>]+>", "", raw_title)))
        if not title or len(title) < 6:
            continue
        url = urljoin(adapter.base_url, href)
        if url in seen:
            continue
        seen.add(url)
        cwrq = None
        d = _LAYUI_DATE_RE.search(block)
        if d:
            mm = _DATE_RE.search(d.group(1)) or _DATE_YMD_SLASH_RE.search(d.group(1))
            if mm:
                cwrq = f"{mm.group(1)}-{int(mm.group(2)):02d}-{int(mm.group(3)):02d}"
        items.append({
            "url": url,
            "title": title,
            "cwrq": cwrq,
            "doc_uid": f"{adapter.source_id}:" + hashlib.md5(url.encode()).hexdigest()[:16],
        })
    return items


def _parse_js_url_arrays(html_text: str, pattern: "re.Pattern[str]",
                         adapter: ListPageAdapter) -> list[dict]:
    """从页内 JS 数组里取条目（陕西型）。

    陕西的「政策法规库」把**全部 419 篇**整批渲染进页内脚本：
        function showNews_60113(start_num){
          var urls=new Array(); var headers=new Array(); ...
          urls[i]='/art/2026/9/28/art_15172_773877.html';
          headers[i]='标题文字';
    而 ``<a href>`` 里只有 15 条 —— 只读 DOM 会漏掉 96%。

    这类站的分页是**前端假分页**（javascript:void(0) + 页内 showNews(N)），
    数据本身一次给全，所以把数组读出来就行，不必跑 JS。
    日期从 URL 里的 /art/YYYY/M/D/ 取。
    """
    urls = re.findall(r"urls\[\w*\]\s*=\s*['\"]([^'\"]+)['\"]", html_text)
    if not urls:
        return []
    heads = re.findall(r"headers\[\w*\]\s*=\s*['\"]([^'\"]*)['\"]", html_text)
    date_re = re.compile(r"/art/(\d{4})/(\d{1,2})/(\d{1,2})/")
    items: list[dict] = []
    seen: set[str] = set()
    for idx, href in enumerate(urls):
        if not pattern.search(href):
            continue
        url = urljoin(adapter.base_url, href)
        if url in seen:
            continue
        seen.add(url)
        title = norm_text(heads[idx]) if idx < len(heads) else ""
        cwrq = ""
        m = date_re.search(url)
        if m:
            cwrq = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
        items.append({
            "url": url,
            "title": title,
            "cwrq": cwrq,
            # doc_uid 的口径必须与另外两处解析器完全一致（源ID + URL 的 md5
            # 前 16 位），否则跨源去重与 upsert 的键对不上。
            "doc_uid": f"{adapter.source_id}:"
                       + hashlib.md5(url.encode()).hexdigest()[:16],
        })
    if items:
        log.info("%s：从页内 JS 数组解析出 %d 条（DOM 里只有少数几条）",
                 adapter.source_id, len(items))
    return items


def parse_list_page(html_text: str, adapter: ListPageAdapter) -> list[dict]:
    """解析静态列表页，返回条目列表。

    条目为空时抛 ``ListPageError``：零条目意味着"页面结构变了或不是静态页"，
    必须让人知道，不能让它伪装成"今天没有新政策"。
    """
    doc = LH.fromstring(html_text)
    pattern = re.compile(adapter.detail_href_re)

    # 先试 layui 的脚本数据：命中就直接返回（数据比 DOM 全得多，见上面注释）
    layui_items = _parse_layui_datajson(html_text, pattern, adapter)
    if layui_items:
        return layui_items

    # 再试页内 JS 数组（陕西型：419 篇全在 urls[] 里，DOM 只有 15 条）。
    # 要求 ≥10 条才采用 —— urls[]= 是通用写法，别站可能用它存无关链接，
    # 数量阈值能挡掉那种误伤。
    js_items = _parse_js_url_arrays(html_text, pattern, adapter)
    if len(js_items) >= 10:
        return js_items

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
            # iterchildren() 会返回**注释节点**（HtmlComment），对它调
            # text_content() 会抛 "ValueError: Input object is not an XML element"。
            # 天津站就栽在这（整省采集因它崩掉）。注释节点的 tag 不是字符串，
            # 用 isinstance(c.tag, str) 滤掉。
            for child in (c for c in a.iterchildren() if isinstance(c.tag, str)):
                candidate = norm_text(child.text_content())
                if not candidate or len(candidate) < 6:
                    continue
                # 纯日期的格子不是标题。广西的条目是
                # ``<a><span>2026-09-28</span> 国家税务总局关于…</a>``，
                # 取"第一个够长的子元素"会把日期当标题（实测 7 条入库成纯日期）。
                if _DATE_ONLY_RE.match(candidate.strip()):
                    continue
                title = candidate
                break
            if not title:
                title = norm_text(a.text_content())
        if not title or len(title) < 6:
            continue

        # 日期搜索范围逐步放宽：父元素 → **不含链接的兄弟节点**
        # → 链接文本（吉林的日期在 <a> 里，不在父元素）→ URL（部分站把年月日编进路径）
        parent_text = ""
        siblings: list = []    # 不含详情链接的兄弟节点：日期常在这些格子里
        parent = a.getparent()
        if parent is not None:
            parent_text = " ".join(parent.text_content().split())
            if not _DATE_RE.search(parent_text):
                # 日期可能在兄弟节点里。实测重庆是
                # ``<dl><dd><a>标题</a></dd><dd>2026-09-04</dd></dl>`` ——
                # 父元素只有标题，日期在隔壁 <dd>。
                # 只收**不含详情链接**的兄弟：含链接的是隔壁条目，
                # 收进来就会给它安上别人的日期。
                gp = parent.getparent()
                if gp is not None:
                    # 同样要滤掉注释节点：**sib.xpath(...) 对 HtmlComment 也会抛**
                    # "ValueError: Input object is not an XML element" ——
                    # 天津站栽的是这一行，不是 text_content 那两处。
                    siblings = [sib for sib in gp.iterchildren()
                                if isinstance(sib.tag, str) and sib is not parent
                                and not sib.xpath(".//a[@href]")]
                    joined = " ".join(s.text_content() for s in siblings).strip()
                    if joined and len(joined) < 120:
                        parent_text = f"{parent_text} {joined}"
        haystack = f"{parent_text} {_clean_title(title)} {url}"

        m = _DATE_RE.search(haystack) or _DATE_YMD_SLASH_RE.search(haystack)
        if m:
            cwrq = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
        else:
            m2 = _DATE_YM_RE.search(haystack)
            if m2:                                    # 只有年月（甘肃）
                cwrq = f"{m2.group(1)}-{int(m2.group(2)):02d}-01"
            else:
                # 只有月日（吉林 [09-04]、山西 <span>09-28</span>），
                # 年份补当年 —— 这些都是补出来的，只用于排序展示，
                # 不参与效力判断。
                m3 = _DATE_MD_RE.search(haystack)
                if m3 is None:
                    # 认「某个格子的文本**整个**就是月-日」（山西的
                    # <span>09-28</span>、四川的 <span>09-28</span>）。
                    # 用 fullmatch 而不是 search：search 会把"3-5 个工作日"
                    # 当成 3 月 5 日 —— 数字都在合理范围内，范围校验拦不住。
                    inner = ([c for c in parent.iterchildren()
                              if isinstance(c.tag, str)]
                             if parent is not None else [])
                    for node in [*inner, *siblings]:
                        m3 = _DATE_MD_BARE_RE.fullmatch(
                            " ".join(node.text_content().split()))
                        if m3:
                            break
                if m3 and not (1 <= int(m3.group(1)) <= 12
                               and 1 <= int(m3.group(2)) <= 31):
                    m3 = None
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
    # 文号来源按可靠性排序：**接口给的官方文号 > 正文里出现的 > 标题里提取的**。
    # 标题里提取的多半是"机关+年份+序号"的拼装品；正文里的才是原文写出来的。
    doc_no = (item.get("doc_no")
              or extract_full_doc_no(item.get("content"))
              or extract_full_doc_no(item.get("title")))
    conf = "official" if item.get("doc_no") else ("high" if doc_no else "low")
    return {
        "doc_uid": item["doc_uid"],
        "url": item["url"],
        "snapshot_url": None,
        "url_md5": None,
        "title": item["title"],
        # 接口型源（北京）在列表阶段就带回了正文，直接落库 ——
        # 省掉逐条抓详情页（7789 条按 1.5 秒/条算要三个多小时）。
        # content_hash 由 store.upsert_policy 统一计算，这里不用管。
        "content": item.get("content"),
        "o_column": adapter.column,
        "o_site_name": adapter.site_name,
        "o_label": "地方文件",
        "p_region": adapter.region,          # 地区维度由采集器直接给出，可靠
        "cwrq": item.get("cwrq"),
        "pub_date": item.get("cwrq"),
        "p_doc_no_full": doc_no,
        "p_doc_no_confidence": conf,
        "pub_name": adapter.site_name,
        "first_seen_at": now_iso(),
        "last_seen_at": now_iso(),
    }


def _fetch_json_api(client: GuardedClient, adapter: ListPageAdapter) -> list[dict]:
    """按页拉 POST JSON 接口（页面只渲染第一页的站靠它拿全量）。

    页数上限由适配器的 api_pages 控制；某页失败就停在那里并把已拿到的返回，
    不假装拿到了全部 —— 缺多少由调用方的 fetch_log 去记。
    """
    if not adapter.api_url or not adapter.api_pages:
        return []
    title_f, url_f, date_f = adapter.api_fields
    out: list[dict] = []
    seen: set[str] = set()
    for page in range(1, adapter.api_pages + 1):
        body = dict(adapter.api_body or {})
        # 页码字段名各站不同：贵州是 pageNo，北京是 PageNumber。
        # 写死一个名字会导致**每页都请求第 1 页**，去重后只剩几十条 ——
        # 而且不报错、状态还是 ok（实测踩过：7789 条只拿到 65 条）。
        body[adapter.api_page_field] = page
        try:
            data = client.post_json(adapter.api_url, body, max_retries=1)
        except Exception as e:  # noqa: BLE001 - 翻页失败不该丢掉已拿到的
            log.warning("接口翻页中断 %s page=%s: %s", adapter.source_id, page, e)
            break
        rows = data
        for key in adapter.api_list_path:
            rows = (rows or {}).get(key) if isinstance(rows, dict) else None
        rows = rows or []
        if not rows:
            break
        for row in rows:
            href = str(row.get(url_f) or "").strip()
            if not href and adapter.api_id_field:
                # 接口不给链接（北京只有 id）：合成伪 URL 供 doc_uid 用。
                # 这类条目的正文由 api_content_field 直接带出来，不依赖详情页。
                href = f"kb:{row.get(adapter.api_id_field)}"
            if not href:
                continue
            title = norm_text(str(row.get(title_f) or ""))
            if not title or len(title) < 6:
                continue
            url = href if href.startswith("kb:") \
                else urljoin(adapter.base_url, href)
            if url in seen:
                continue
            seen.add(url)
            m = _DATE_RE.search(str(row.get(date_f) or "")) \
                or _DATE_YMD_SLASH_RE.search(str(row.get(date_f) or ""))
            cwrq = (f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
                    if m else None)
            item = {
                "url": url,
                "title": title,
                "cwrq": cwrq,
                "doc_uid": f"{adapter.source_id}:"
                           + hashlib.md5(url.encode()).hexdigest()[:16],
            }
            if adapter.api_content_field:
                raw_body = str(row.get(adapter.api_content_field) or "")
                # 接口给的正文有两种形态：北京 answer 是纯文本，
                # 贵州 f_202161645127 是 HTML（<div class="trs_editor_view">…）。
                # 统一去过标签再入库，否则 HTML 标签会污染检索。
                if "<" in raw_body:
                    raw_body = re.sub(r"<[^>]+>", " ", raw_body)
                body = norm_text(raw_body)
                if body:
                    item["content"] = body
            if adapter.api_docno_field:
                no = norm_text(str(row.get(adapter.api_docno_field) or ""))
                if no:
                    item["doc_no"] = no
            out.append(item)
    return out


def fetch_list_pages(client: GuardedClient, adapter: ListPageAdapter,
                     ) -> tuple[list[dict], bool]:
    """抓取适配器配置的**所有**列表页并合并去重。

    为什么要支持多个：省级站的「最新文件」是**固定展示最近一二十条的单页列表**
    （实测河南/辽宁/广东都没有翻页控件），历史政策分散在按税种分的子栏目里。

    单个子栏目空/失败**不算整源失败**，全都拿不到才算 —— "零条目即报错"这条
    防线针对的是"栏目 URL 猜错或页面改版"，而不是"某个子栏目恰好没内容"。

    返回 ``(items, truncated)``。``truncated`` 为真表示**因超过总时长上限
    （``adapter.max_seconds``）中途停止**，拿到的不是全部。

    **为什么要显式返回它、而不是静默截断**：调用方（pipeline）以前把
    ``status`` 硬编码成 "ok"，于是"只抓了一半"与"抓全了"在 fetch_log 里
    长得一模一样；再加上几条 status='running' 且无结束时间的记录，事后
    根本分不清哪个源不完整、为什么。抓不全可以接受，**不知道自己没抓全
    才致命**。
    """
    import time
    from dataclasses import replace

    started = time.monotonic()
    limit = adapter.max_seconds
    truncated = False

    urls = [adapter.list_url, *adapter.extra_urls]
    # 静态分页（河北 index_1.html … index_330.html）：展开成额外列表页。
    # 与"接口分页"的区别是这些页是**真静态 HTML**，不用扒接口、也不用浏览器交互。
    if adapter.page_url_template and adapter.page_count:
        for i in range(1, adapter.page_count + 1):
            u = adapter.page_url_template.format(n=i)
            if u != adapter.list_url:
                urls.append(u)
    seen: set[str] = set()
    out: list[dict] = []
    errors: list[str] = []

    # 先走 JSON 接口：接口里的数据比页面全得多（贵州页面 15 条 / 接口 4934 条）
    for item in _fetch_json_api(client, adapter):
        if item["url"] not in seen:
            seen.add(item["url"])
            out.append(item)
    for url in urls:
        # **每抓一页前查总时长**：这是"卡死"的兜底。单页已有超时
        # （timeout_ms / 浏览器 wait_ms），但 N 页累加没有上限 ——
        # 河北在没有上限时单个源跑过 3474 秒。
        if limit and (time.monotonic() - started) > limit:
            truncated = True
            log.warning("[%s] 总时长超限（%.0f 秒），停止翻页：已抓 %d 条，"
                        "还有 %d 个列表页未抓",
                        adapter.source_id, limit, len(out),
                        len(urls) - urls.index(url))
            break
        try:
            items = fetch_list_page(client, replace(adapter, list_url=url))
        except Exception as e:  # noqa: BLE001 - 单个子栏目失败不该拖垮整个源
            errors.append(f"{url}（{type(e).__name__}）")
            log.warning("子栏目抓取失败 %s: %s", url, e)
            continue
        for item in items:
            if item["url"] in seen:
                continue
            seen.add(item["url"])
            out.append(item)
    if not out:
        raise ListPageError(
            f"[{adapter.source_id}] 配置的 {len(urls)} 个列表页都没有解析出条目："
            + "；".join(errors or list(urls))
            + "。页面可能已改版，或该栏目实际是 JS 异步加载。")
    return out, truncated


def fetch_list_page(client: GuardedClient, adapter: ListPageAdapter) -> list[dict]:
    """抓取并解析一个省级列表页。

    受 JS 挑战保护的站点走**真浏览器**（见 collect/browser.py）。
    不走这条的话会拿到 412 挑战页、解析出 0 条，而"零条目即报错"会把
    它报成"页面改版"—— 掩盖真实原因。所以这里必须按适配器分流。
    """
    if adapter.needs_js:
        from .collect.browser import fetch_html

        # 挑战等待与导航超时按源可调：有的站挑战就是慢（见 ListPageAdapter）
        kw = {}
        if adapter.wait_ms:
            kw["wait_ms"] = adapter.wait_ms
        if adapter.timeout_ms:
            kw["timeout_ms"] = adapter.timeout_ms
        return parse_list_page(
            fetch_html(adapter.list_url, use_nodriver=adapter.use_nodriver, **kw),
            adapter)

    resp = client.get(adapter.list_url)
    resp.raise_for_status()
    # 省级站点多为 UTF-8；gb18030 兜底避免个别站点乱码导致解析失败
    try:
        text = resp.content.decode("utf-8")
    except UnicodeDecodeError:
        text = resp.content.decode("gb18030", "replace")
    return parse_list_page(text, adapter)
