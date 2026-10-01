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
    # 用 nodriver 而不是 Playwright。两个浏览器栈的反检测强度不同：
    # 实测西藏税局在 Playwright 下只返回 39 字节空壳，nodriver 能拿到 50009 字节。
    use_nodriver: bool = False
    # 挑战等待时长（毫秒）。留 0 用 browser 模块的默认值。
    # 有些站的挑战就是比别的慢：实测辽宁在默认 6 秒下拿不到内容，给到 12 秒
    # 才出 72781 字节的首页。这类站必须能单独配，否则会一直"抓不到"。
    wait_ms: int = 0
    # 导航超时（毫秒）。留 0 用默认。
    timeout_ms: int = 0
    # 额外列表页。省级站的「最新文件」栏目是**固定展示最近一二十条的单页列表**
    # （实测河南/辽宁/广东都没有翻页控件），历史政策分散在按税种分的
    # 「政策法规库」子栏目里。所以一个源要能配多个列表页并合并去重。
    extra_urls: tuple[str, ...] = ()
    # ---- POST JSON 接口源 ----
    # 有的站列表页**只渲染第一页**，历史数据全在 AJAX 接口后面。
    # 实测贵州政策法规库：页面渲染 15 条，而库里有 4934 条。
    # 接口参数与字段名是从浏览器 network 里抓的（字段名是混淆的，每站不同），
    # 所以由适配器逐项指定，不猜。
    api_url: str = ""
    api_body: dict | None = None          # 固定参数（含 customFilter 等）
    api_pages: int = 0                    # 拉多少页；0 表示该源不用接口
    api_fields: tuple[str, str, str] = ("", "", "")   # (标题, 链接, 日期) 字段名
    #: 响应里列表所在的路径。贵州是 ("data","list")，北京是 ("Response","Data","List")
    api_list_path: tuple[str, ...] = ("data", "list")
    #: 接口**不给链接**时，用它合成一个伪 URL（北京只有 id，正文直接在响应里）
    api_id_field: str = ""
    #: 正文所在字段名。给了就直接入库 —— 省掉逐条抓详情页
    #: （北京接口一次性返回 7789 条的完整正文，这是最省的一条路）
    api_content_field: str = ""
    #: 官方文号字段名。接口给的往往就是**原文真文号**，比从标题里正则提取的
    #: 可靠得多 —— 北京 fwzh 字段 3155 条全带（如"国家税务总局公告2026年第20号"）。
    api_docno_field: str = ""
    api_page_field: str = "pageNo"     # 页码字段名（贵州 pageNo / 北京 PageNumber）
    #: 静态分页模板。河北的分页是 index_1.html / index_2.html…（**纯静态**，
    #: 不是 AJAX），所以不用扒接口，按页抓即可。{n} 替换成 1..page_count。
    page_url_template: str = ""
    page_count: int = 0


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
        # 注：站内检索（searchResult.html?siteCode=…&searchWord=…）在**浏览器**里
        # 能拿到 29 条匹配本省正则的政策，但真实采集走 HTTP 直连只得到空壳、
        # 一条也解析不出 —— 它的结果列表是 JS 渲染的。而本适配器的 needs_js
        # 为 False（普通栏目页是静态的），fetch_list_pages 给每个子栏目套用
        # 同一个 needs_js，两者无法共存，故不配该检索页。
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
        # 站内检索（汉王 jrobot），实测「个税」返回 12 条匹配本省正则的政策。
        extra_urls=(
            "http://shandong.chinatax.gov.cn/jrobot/search.do"
            "?webid=1&q=%E4%B8%AA%E7%A8%8E&pos=title%2Ccontent&analyzeType=1&category=",
        ),
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
        # 政策法规库（zcfgk）**本身就是文章列表页**，不是导航页 —— 实测一次
        # 给出 2586 篇，按税种分目录（/zcfgk/zzs/1593613.htm）。
        # 原正则 /hbsw/zcwj/[a-z]+/\d+\.htm 只认两层，匹配不到三层目录，
        # 现象是"没解析出条目"，极易被误判成"页面改版"。
        # zxwj（最新文件，两层路径）也一并收，用交替覆盖两种形态。
        list_url="http://hubei.chinatax.gov.cn/hbsw/zcwj/zcfgk/index.html",
        detail_href_re=r"/hbsw/zcwj/(?:zcfgk/[a-z]+|zxwj)/\d+\.htm",
        base_url="http://hubei.chinatax.gov.cn",
        needs_js=True,
        # 政策法规库页面要渲染 840KB 才出内容，默认 6 秒只拿到空壳
        # （现象是"没解析出条目"，很容易被误判成页面改版）。实测约 10 秒够。
        wait_ms=11000,
    ),
    ListPageAdapter(
        source_id="hn_zcwj",
        region="湖南",
        site_name="国家税务总局湖南省税务局",
        # 指向「最新文件」列表页，而不是原来的聚合页 /category/20190624092865。
        # 那个聚合页把「最新文件 / 政策解读 / **热点问答** / 图解税收」混在一起，
        # 于是"我们准备新开办一家企业，请问办理税务登记需要收费吗？"这类
        # 咨询问答也进了政策库 —— 内容没错，但它不是政策，混进来会干扰检索。
        list_url="http://hunan.chinatax.gov.cn/lists/20190725027557",
        detail_href_re=r"/show/\d+",
        # ------------------------------------------------------------------
        # 【已探明，暂不接入】湖南的政策全量在「12366 知识库」检索系统里：
        #
        #   POST https://hunan.chinatax.gov.cn/hngsww/
        #        FullTextSearchBLH_getpagedata.do
        #   Content-Type: application/x-www-form-urlencoded
        #   page=1&pageSize=10&treeVal=1&xzqh=&zlType=&zlflag=1
        #        &keywords=&pxlx=&pxzd=
        #
        # 实测：共 8276 条 / 828 页。每条结果的 ID 前缀标明了层级 ——
        #   1430000002… = 湖南省局（省级）
        #   0000000002… = 总局
        # 栏目分「税收法规 / 全国涉税政策 / 省级涉税政策 / 市级 / 县级」。
        #
        # **为什么不接入**：该接口有 JS 挑战 WAF（返回 412 + MMB1_*.js），
        # GuardedClient 的 curl_cffi **过不去**（已实测），只能走真浏览器。
        # 828 页 × 约 10 秒/页 ≈ 2.3 小时，且现有 fetch_html 只支持 GET，
        # 要新增"浏览器内提交表单"的能力才做得到。
        #
        # 取舍：湖南当前已有 5 条真政策（来自「最新文件」），日更够用；
        # 全量回填的性价比低于其它省，暂缓。
        # 若日后要做：给 fetch_html 加 submit_form，或在页面里 eval 提交。
        # ------------------------------------------------------------------
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
        list_url="http://beijing.chinatax.gov.cn/bjswj/sszc/zxwj/cs_li.shtml",
        detail_href_re=r"/bjswj/sszc/zxwj/\d{6}/[0-9a-f]+\.shtml",
        base_url="http://beijing.chinatax.gov.cn",
        needs_js=True,
        # ------------------------------------------------------------------
        # 【待接入】北京另有一个独立的「税费知识库」（Vue SPA），**7789 条**，
        # 字段比任何现有源都全 —— 带官方真文号与有效性标注：
        #
        #   POST https://znhd.beijing.chinatax.gov.cn:8443/zsknsrd/api/
        #        zsknsrdsjjsService/search/v1/listKnowledge
        #   {"Field":180,"SortBy":"UpdateTime","PageNumber":1,"PageSize":10,
        #    "Order":"desc","Range":[1,2,6],"Ztfl":[],"Yxx":[],"Zsqy":[12703],
        #    "Zssx":[[],[]],"Text":""}
        #
        # 响应路径与贵州不同：列表在 Response.Data.List（贵州是 data.list），
        # 总数在 Response.Data.Total。**没有链接字段** —— 正文直接给在 answer
        # 里，所以要用 id 合成伪 URL。
        #
        # 字段（22 个，实测）：
        #   question = 标题            answer = 正文
        #   fwzh     = 发文字号（官方真文号）  fwrq   = 发文日期
        #   yxx      = 有效性（数值，需解码）  fjmc   = 附件文件名列表
        #   zssx     = 所属税种         ztfl   = 主题分类
        #   id       = 722957          code   = F260930001698781
        #
        # 坑：httpx 直连会被服务端断开（Server disconnected without sending
        # a response），必须走 GuardedClient 的 curl_cffi —— 已用 post_json
        # 实测通过（Total=7789）。
        # ------------------------------------------------------------------
        # 7789 条 ÷ 50/页 ≈ 156 页，取 160 留余量。
        api_url="https://znhd.beijing.chinatax.gov.cn:8443/zsknsrd/api/"
                "zsknsrdsjjsService/search/v1/listKnowledge",
        api_pages=160,
        api_page_field="PageNumber",       # 北京用 PageNumber（贵州是 pageNo）
        api_list_path=("Response", "Data", "List"),
        api_fields=("question", "", "fwrq"),   # 无链接字段 → 用 api_id_field
        api_id_field="id",
        api_content_field="answer",            # 正文直接带出来，不再逐条抓详情
        api_docno_field="fwzh",                # 官方真文号（全库文号质量的解药）
        api_body={
            "Field": 180,
            "PageSize": 50,
            "SortBy": "UpdateTime",
            "Order": "desc",
            "Range": [1, 2, 6],
            "Ztfl": [],
            "Yxx": [],
            "Zsqy": [12703],
            "Zssx": [[], []],
            "Text": "",
        },
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
        # 站内检索（jsearchfront），实测「发票」返回 11 条匹配本省正则的政策。
        extra_urls=(
            "http://zhejiang.chinatax.gov.cn/searchfront/search.do"
            "?websiteid=330100000000000&tpl=1&q=%E5%8F%91%E7%A5%A8",
        ),
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
        # 站内检索（汉王 jrobot），实测「社保」返回 35 条匹配本省正则的政策。
        extra_urls=(
            "http://anhui.chinatax.gov.cn/jrobot/search.do"
            "?q=%E7%A4%BE%E4%BF%9D&pagemode=result&appid=&webid=39&style=1&ck=0&category=all",
        ),
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
        # 站内检索接口 jsearchfront/search.do 是标准 URL 查询形式、无加密，
        # 实测「增值税」一词就返回 20 条、含 2020 年的历史政策 —— 这正是
        # 「最新文件」栏目（只有最近一二十条）拿不到的部分。详情格式与现有
        # 正则一致（/art/YYYY/M/D/art_N_N.html），所以多个关键词各配一次即可。
        extra_urls=(
            "http://jiangxi.chinatax.gov.cn/jsearchfront/search.do"
            "?websiteid=360000000000000&tpl=23&q=%E7%A8%8E%E5%8A%A1",           # 税务
            "http://jiangxi.chinatax.gov.cn/jsearchfront/search.do"
            "?websiteid=360000000000000&tpl=23&q=%E5%A2%9E%E5%80%BC%E7%A8%8E",  # 增值税
            "http://jiangxi.chinatax.gov.cn/jsearchfront/search.do"
            "?websiteid=360000000000000&tpl=23&q=%E4%BC%81%E4%B8%9A%E6%89%80%E5%BE%97%E7%A8%8E",  # 企业所得税
        ),
    ),
    ListPageAdapter(
        source_id="shaanxi_zcwj",
        region="陕西",
        site_name="国家税务总局陕西省税务局",
        list_url="http://shaanxi.chinatax.gov.cn/col/col3899/index.html",
        detail_href_re=r"/art/\d{4}/\d{1,2}/\d{1,2}/art_\d+_\d+\.html",
        base_url="http://shaanxi.chinatax.gov.cn",
        needs_js=True,
        # 列表页是异步渲染的，默认 6 秒只拿到空壳（2295 字节、0 条链接）。
        # 实测 10 秒仍不够（3241 字节），**14 秒**才出内容（41461 字节、34 条）。
        # 这是第 4 个栽在同一处的省（辽宁 12s、新疆 14s、吉林 10s）——
        # 默认的「挑战等待」6 秒对这类站点普遍偏低。没有这条，陕西会每天
        # 报一次 ListPageError，看起来像"站点改版了"，其实只是等太短。
        wait_ms=14000,
        # 90 秒而非 60：该站导航偶发很慢（实测出现过 60 秒还没到
        # domcontentloaded 而超时，同一次会话里另一次却几秒就返回）。
        timeout_ms=90000,
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
        # 「最新文件」栏有**静态分页**（/zcwj/zxwj/index_N.html）——
        # 实测页内有 15 个文章链接 + 10 个 index_N.html 分页链接。
        # 原先只抓 /zcwj/ 首页，所以只有 13 条。
        page_url_template="https://guangxi.chinatax.gov.cn/zcwj/zxwj/index_{n}.html",
        page_count=60,
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
        # ------------------------------------------------------------------
        # 【待实现】贵州政策法规库有 **4934 条**，但页面每页只渲染 15 条、
        # 翻页走 AJAX，所以现在只能抓到 8 条。接口已抓到并验证可直连：
        #
        #   POST https://guizhou.chinatax.gov.cn/irs/front/list
        #   Content-Type: application/json
        #   {"pageNo":1,"pageSize":15,"tenantId":71,
        #    "tableName":"t_179d132472b",
        #    "searchFields":[], "isPage":true,
        #    "sorts":[{"sortField":"save_time","sortOrder":"DESC"}],
        #    "customFilter":{"operator":"or","properties":[
        #        {"property":"f_202163742494","operator":"eq","value":<下面这串>}]}}
        #
        # 税种子类 ID（26 个，缺一个就少收一个税种）：
        #   5901201, 5899601, 5899600, 5899602, 5899603, 5899604, 5899605,
        #   5899606, 5899607, 5899608, 5899609, 5899610, 5992803, 5899611,
        #   5899612, 5899613, 5899614, 5899615, 5899616, 5899617, 5899618,
        #   5899619, 5899620, 5899621, 5899622, 5901201
        #
        # 响应形如 {"success":true,"data":{"list":[{...}]}}，**已确认的字段映射**：
        #   f_202163261554   = 标题
        #   doc_pub_url      = 正文链接
        #   f_202163870059   = 发文单位
        #   save_time        = 日期（"2022-04-20 10:15:36"）
        #   f_202161645127   = 正文 HTML
        #   f_202163742494   = 税种子类 ID（customFilter 里那个 value）
        # 不带 customFilter 会返回**全站**内容（含减税降费专题等），必须带上。
        #
        # 已接入下面的 api_* 配置。4934 条 ÷ 50/页 ≈ 99 页，取 110 留余量。
        api_url="https://guizhou.chinatax.gov.cn/irs/front/list",
        api_pages=110,
        api_fields=("f_202163261554", "doc_pub_url", "save_time"),
        # 正文与文号：接口里其实都有，第一次接的时候只配了标题与链接。
        # 正文在 f_202161645127（HTML），文号不在字段里 —— 由 build_provincial_row
        # 从正文提取（比从标题提取的拼装品可靠）。
        api_content_field="f_202161645127",
        api_body={
            "pageSize": 50,
            "tenantId": 71,
            "tableName": "t_179d132472b",
            "searchFields": [],
            "isPage": True,
            "sorts": [{"sortField": "save_time", "sortOrder": "DESC"}],
            "customFilter": {
                "operator": "or",
                "properties": [
                    {"property": "f_202163742494", "operator": "eq", "value": 5901201},
                    {"property": "f_202163742494", "operator": "eq", "value": 5899601},
                    {"property": "f_202163742494", "operator": "eq", "value": 5899600},
                    {"property": "f_202163742494", "operator": "eq", "value": 5899602},
                    {"property": "f_202163742494", "operator": "eq", "value": 5899603},
                    {"property": "f_202163742494", "operator": "eq", "value": 5899604},
                    {"property": "f_202163742494", "operator": "eq", "value": 5899605},
                    {"property": "f_202163742494", "operator": "eq", "value": 5899606},
                    {"property": "f_202163742494", "operator": "eq", "value": 5899607},
                    {"property": "f_202163742494", "operator": "eq", "value": 5899608},
                    {"property": "f_202163742494", "operator": "eq", "value": 5899609},
                    {"property": "f_202163742494", "operator": "eq", "value": 5899610},
                    {"property": "f_202163742494", "operator": "eq", "value": 5992803},
                    {"property": "f_202163742494", "operator": "eq", "value": 5899611},
                    {"property": "f_202163742494", "operator": "eq", "value": 5899612},
                    {"property": "f_202163742494", "operator": "eq", "value": 5899613},
                    {"property": "f_202163742494", "operator": "eq", "value": 5899614},
                    {"property": "f_202163742494", "operator": "eq", "value": 5899615},
                    {"property": "f_202163742494", "operator": "eq", "value": 5899616},
                    {"property": "f_202163742494", "operator": "eq", "value": 5899617},
                    {"property": "f_202163742494", "operator": "eq", "value": 5899618},
                    {"property": "f_202163742494", "operator": "eq", "value": 5899619},
                    {"property": "f_202163742494", "operator": "eq", "value": 5899620},
                    {"property": "f_202163742494", "operator": "eq", "value": 5899621},
                    {"property": "f_202163742494", "operator": "eq", "value": 5899622},
                    {"property": "f_202163742494", "operator": "eq", "value": 5901201},
                ],
            },
        },
        # ------------------------------------------------------------------
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
        # 站内检索（jsearchfront），实测「发票」返回 23 条匹配本省正则的政策。
        extra_urls=(
            "http://heilongjiang.chinatax.gov.cn/jsearchfront/search.do"
            "?websiteid=230000000000000&pg=&p=&tpl=&category=&q=%E5%8F%91%E7%A5%A8&submit=",
        ),
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
        # 站内检索（jsearchfront），实测「发票」返回 **40** 条匹配本省正则的政策。
        # 这条 URL 是从**栏目页**（不是首页）提取到的 —— 首页的搜索是全站检索，
        # 返回的多是新闻动态；栏目页的搜索才落到政策上。
        #
        # 两点必须与探测环境一致，否则"试调可用、真实采集失败"（已踩过）：
        # ① 用 http（与 base_url 一致）；
        # ② wait_ms 提到 10 秒 —— 检索结果是异步渲染的，默认 6 秒只拿到空壳。
        wait_ms=10000,
        extra_urls=(
            "http://jilin.chinatax.gov.cn/jsearchfront/search.do"
            "?websiteid=111000000135000&p=1&q=%E5%8F%91%E7%A5%A8",
        ),
    ),
    ListPageAdapter(
        source_id="nmg_zcwj",
        region="内蒙古",
        site_name="国家税务总局内蒙古自治区税务局",
        list_url="http://neimenggu.chinatax.gov.cn/zcwj",
        # 两种路径形态必须都认：zcwj 下是 <栏目>/<年月>/t<日期>_<编号>.html；
        # 而首页那个「政策法规库」（/zcfgk/zcfgk）是 <数字>/t<日期>_<编号>.html
        # —— 只写一种就会整块漏掉。
        detail_href_re=(r"(?:(?:zxwj|zcjd|rdwd|tjss)/\d{6}/t\d+_\d+"
                        r"|zcfgk/zcfgk/\d+/t\d+_\d+)\.html"),
        base_url="http://neimenggu.chinatax.gov.cn/zcwj/",
        needs_js=True,
        # 首页那个「政策法规库」：页内**直接给文章**（不像黑龙江/江西/甘肃
        # 那样是检索壳页），所以直接当列表页抓
        extra_urls=("https://neimenggu.chinatax.gov.cn/zcfgk/zcfgk",),
    ),
    ListPageAdapter(
        source_id="hlj_zcwj",
        region="黑龙江",
        site_name="国家税务总局黑龙江省税务局",
        # **不要用「政策法规库」**：那是个 jsearchfront 检索壳页，
        # q= 只返回固定 22 条，且实测 p / page / pageNo / pageNum /
        # currentPage / pn / start 七种分页参数**全部无效**（返回同一批）。
        # 真正的政策列表在首页 →「政策文件」这个 col 栏目里，页内直接给文章。
        list_url="http://heilongjiang.chinatax.gov.cn/col/col7573/index.html",
        detail_href_re=r"/art/\d+/\d+/\d+/art_\d+_\d+\.html",
        base_url="http://heilongjiang.chinatax.gov.cn",
        needs_js=True,
        # 「最新文件」是同一站里更接近政策本体的栏目（实测政策文件栏只有
        # 几十条单页，而最新文件是持续更新的入口）
        extra_urls=("http://heilongjiang.chinatax.gov.cn/col/col16798/index.html",),
    ),
    ListPageAdapter(
        source_id="gs_zcwj",
        region="甘肃",
        site_name="国家税务总局甘肃省税务局",
        # 同黑龙江：走「政策文件」栏，不用那个 jsearchfront 壳页
        list_url="http://gansu.chinatax.gov.cn/col/col4/index.html",
        detail_href_re=r"/art/\d+/\d+/\d+/art_\d+_\d+\.html",
        base_url="http://gansu.chinatax.gov.cn",
        needs_js=True,
        # 「税收规范性文件」是甘肃省局发布规范性文件的**正式栏目** ——
        # 这才是省级政策本体的所在，比泛泛的"政策文件"栏准确得多
        extra_urls=("http://gansu.chinatax.gov.cn/col/col9689/index.html",
                    "http://gansu.chinatax.gov.cn/col/col36/index.html"),
    ),
    ListPageAdapter(
        source_id="jx_zcwj",
        region="江西",
        site_name="国家税务总局江西省税务局",
        # 同黑龙江/甘肃（江西的「政策法规库」也是 jsearchfront 壳页，
        # websiteid=360000000000000 但那套只给固定 10 条）
        list_url="http://jiangxi.chinatax.gov.cn/col/col31015/index.html",
        detail_href_re=r"/art/\d+/\d+/\d+/art_\d+_\d+\.html",
        base_url="http://jiangxi.chinatax.gov.cn",
        needs_js=True,
        extra_urls=("http://jiangxi.chinatax.gov.cn/col/col31884/index.html",),
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
        # 站内检索（汉王 jrobot），实测「发票」返回 15 条匹配本省正则的政策。
        extra_urls=(
            "http://ningxia.chinatax.gov.cn/jrobot/search.do"
            "?webid=17&pg=12&p=1&tpl=1&category=&q=%E5%8F%91%E7%A5%A8&pos=&od=&date=&date=",
        ),
        needs_js=True,
    ),
    ListPageAdapter(
        source_id="hainan_zcwj",
        region="海南",
        site_name="国家税务总局海南省税务局",
        list_url="http://hainan.chinatax.gov.cn/zcwj",
        # 海南的链接形如 /xxgk_6_1/30167423.html（信息公开）。
        # 同一页还有 ssxc_（税收宣传）与 gzcy_（关注产业）两类，那些不是政策文件，
        # 所以正则只收 xxgk_ 前缀 —— 栏目边界比想象中松，得挑。
        detail_href_re=r"/xxgk_\d+_\d+/\d+\.html",
        base_url="http://hainan.chinatax.gov.cn",
        needs_js=True,
    ),
    # 重庆：真实栏目在 /cqtax/ 下，**不是根路径**。
    # 根路径首页能过 WAF（65790 字节）且里面有 ./zcwj/ 这类相对链接，但直接
    # 访问 /zcwj/ 只回 220 字节的挑战页 —— WAF 只放行它认识的路径。
    # 实测 /cqtax/zcwj/zxwj/ 与 /cqtax/zcwj/zcjd/ 各回 23KB 静态列表。
    # base_url 必须写成列表页自身（带尾斜杠）：条目 href 是 ./202609/t…html，
    # urljoin 要按列表页目录拼，写成裸域名会拼到根路径上去。
    ListPageAdapter(
        source_id="cq_zxwj",
        region="重庆",
        site_name="国家税务总局重庆市税务局",
        list_url="https://chongqing.chinatax.gov.cn/cqtax/zcwj/zxwj/",
        detail_href_re=r"\./\d{6}/t\d+_\d+\.html",
        base_url="https://chongqing.chinatax.gov.cn/cqtax/zcwj/zxwj/",
        needs_js=True,
    ),
    ListPageAdapter(
        source_id="cq_zcjd",
        region="重庆",
        site_name="国家税务总局重庆市税务局",
        list_url="https://chongqing.chinatax.gov.cn/cqtax/zcwj/zcjd/",
        detail_href_re=r"\./\d{6}/t\d+_\d+\.html",
        base_url="https://chongqing.chinatax.gov.cn/cqtax/zcwj/zcjd/",
        needs_js=True,
    ),
    # 西藏：域名 xizang.chinatax.gov.cn。实测 Playwright 与 nodriver 在这一页
    # 拿到完全相同的结果（23802 字节、18 个详情链接），所以用 Playwright ——
    # 不必为它单独走 nodriver 那条异步链路。
    # 栏目来自首页：政策文件 col5332 / 最新文件 col5350 / 政策解读 col5346。
    ListPageAdapter(
        source_id="xizang_zcwj",
        region="西藏",
        site_name="国家税务总局西藏自治区税务局",
        list_url="https://xizang.chinatax.gov.cn/col/col5350/index.html",
        detail_href_re=r"/art/\d{4}/\d{1,2}/\d{1,2}/art_\d+_\d+\.html",
        base_url="https://xizang.chinatax.gov.cn",
        needs_js=True,
        # 再带上「政策解读」栏目：单靠「最新文件」只有最近的一二十条，
        # 多配一个栏目就多一份覆盖面（见 ListPageAdapter.extra_urls）。
        # 「政策解读」有效（+24 条）；本站「政策法规库」(col5510) 是 JS 检索页，
        # 静态列表解析不出条目（实测 0 条），故只配了前者。
        extra_urls=(
            "https://xizang.chinatax.gov.cn/col/col5346/index.html",
            # 站内检索（汉王 jrobot），实测「社保费」返回 20 条匹配本省正则的政策。
            "https://xizang.chinatax.gov.cn/jrobot/search.do"
            "?q=%E7%A4%BE%E4%BF%9D%E8%B4%B9&x=31&y=19&webid=1&pg=12&p=1&tpl=&category=xzsw",
        ),
    ),
    # 辽宁：**默认 6 秒的挑战等待不够** —— 那样只拿到空壳，看起来像"站点抓不到"。
    # 给到 12 秒才出内容（列表页 43744 字节、52 个详情链接）。
    # 这也是排查河北/新疆时的教训：先怀疑等待时间，再怀疑站点的反爬强度。
    ListPageAdapter(
        source_id="liaoning_zcwj",
        region="辽宁",
        site_name="国家税务总局辽宁省税务局",
        list_url="https://liaoning.chinatax.gov.cn/col/col2000/index.html",
        detail_href_re=r"/art/\d{4}/\d{1,2}/\d{1,2}/art_\d+_\d+\.html",
        base_url="https://liaoning.chinatax.gov.cn",
        # 站内检索（jsearchfront），实测「合规经营」返回 35 条匹配本省正则的政策。
        # total=7124 是该检索的总条数 —— 辽宁全站政策量不小，这条路径值得保留。
        extra_urls=(
            "https://liaoning.chinatax.gov.cn/jsearchfront/search.do"
            "?websiteid=210103020004000&searchid=10&pg=10&p=1&tpl=7&total=7124"
            "&q=%E5%90%88%E8%A7%84%E7%BB%8F%E8%90%A5&pq=&oq=&eq=&pos=&begin=&end=",
        ),
        needs_js=True,
        wait_ms=12000,
        timeout_ms=90000,
    ),
    # 新疆：**默认 6 秒的挑战等待同样不够**（和辽宁一个毛病，所以之前一直被
    # 判成"空壳"）。给到 14 秒拿到 85125 字节的首页、列表页 27343 字节。
    # 另外注意它的详情链接是 **.htm**（三字母），别省的 .html 正则到这里
    # 一条都匹配不上 —— 所以统一写成 \.html?。
    ListPageAdapter(
        source_id="xinjiang_zcwj",
        region="新疆",
        site_name="国家税务总局新疆维吾尔自治区税务局",
        list_url="https://xinjiang.chinatax.gov.cn/sszc/zxwj/",
        detail_href_re=r"\./\d{6}/t\d+_\d+\.html?",
        base_url="https://xinjiang.chinatax.gov.cn/sszc/zxwj/",
        needs_js=True,
        wait_ms=14000,
        timeout_ms=90000,
        # 列表页有**静态分页**（index_1.htm …）—— 实测页内同时有 15 个文章链接
        # 和 11 个 index_N.htm 分页链接。只抓第一页会漏掉全部历史政策。
        page_url_template="https://xinjiang.chinatax.gov.cn/sszc/zxwj/index_{n}.htm",
        page_count=60,
    ),
    # 新疆的政策解读，与重庆一样单独接一个源
    ListPageAdapter(
        source_id="xinjiang_zcjd",
        region="新疆",
        site_name="国家税务总局新疆维吾尔自治区税务局",
        list_url="https://xinjiang.chinatax.gov.cn/sszc/zcjd/",
        detail_href_re=r"\./\d{6}/t\d+_\d+\.html?",
        base_url="https://xinjiang.chinatax.gov.cn/sszc/zcjd/",
        needs_js=True,
        wait_ms=14000,
        timeout_ms=90000,
    ),
    # 天津：有三处坑，缺一个都抓不到。
    # ① 列表在 **iframe** 里（u_zlmViewMx.action），直接抓主页面只有 0 个链接；
    # ② iframe 的 src 是**不以 / 开头的相对路径**，得 urljoin 到主页面地址；
    # ③ 详情链接后缀是 **.shtml**（不是 .html），且无前导斜杠，形如
    #    11200000000/0300/030004/03000419/20260907165858609.shtml
    #    —— 所以正则写 \.s?html?，base_url 用裸域名（ACTION 的目录就是 /）。
    ListPageAdapter(
        source_id="tianjin_zxwj",
        region="天津",
        site_name="国家税务总局天津市税务局",
        list_url=("https://tianjin.chinatax.gov.cn/u_zlmViewMx.action"
                  "?fjdm=11200000000&lmdm=030001&downbz=null"),
        detail_href_re=r"\d{11}/\d{4}/\d{6}/\d{8}/\d+\.s?html?",
        base_url="https://tianjin.chinatax.gov.cn/",
        # 站内检索走第三方（汉王 jpaas），实测「增值税」返回 **62** 条匹配本省
        # 正则的政策 —— 是本次全量探测里最丰的一个，而天津原本只有 2 条。
        extra_urls=(
            "https://so.hanweb.com/api-gateway/jpaas-jsearch-web-server/search"
            "?q=%E5%A2%9E%E5%80%BC%E7%A8%8E&serviceId=N1r3zjtvh9u2299aJ5j5E",
        ),
        needs_js=True,
        wait_ms=20000,
        timeout_ms=120000,
    ),
    # 河北：三处关键，缺一处就"抓不到"。
    # ① 入口在 **/hbsw/** 下 —— 根路径返回的是 60 字节的 JS 跳转页
    #    （location.href="/hbsw/index.html"），直接抓根路径等于什么都没有；
    # ② 它的证书与域名不匹配，所以用 **http**；
    # ③ 列表条目**写在 <script> 里的 JS 字符串里** ——
    #    ``var doctitle = '<a href="./202609/t...html">标题</a>'``，
    #    再由 document.write 输出。lxml 看到的 script 内容是纯文本、不是
    #    元素，所以 HTTP 直连版解析出来是 **0 条**（真实页面一个链接不少）。
    #    必须用浏览器渲染成真 DOM 才能解析。
    #    这个坑很隐蔽：页面字节数看着正常，正则也能搜到链接，只有 DOM 里没有。
    ListPageAdapter(
        source_id="hebei_zxwj",
        region="河北",
        site_name="国家税务总局河北省税务局",
        list_url="http://hebei.chinatax.gov.cn/hbsw/sszc/zxwj/",
        # 两种路径形态都要认：普通列表页给相对路径（./202609/t…），
        # 无障碍版检索接口给绝对路径（hebei.chinatax.gov.cn/hbsw/sszc/zxwj/…）。
        detail_href_re=r"(?:\./|hebei\.chinatax\.gov\.cn/hbsw/sszc/zxwj/)"
                       r"\d{6}/t\d+_\d+\.html",
        base_url="http://hebei.chinatax.gov.cn/hbsw/sszc/zxwj/",
        needs_js=True,
        # 普通列表页的 index_N.html **只到第 66 页**（约 990 条）就没了，
        # 而站点声明 4938 条。全量的 330 页在**无障碍版**的 TRS 检索系统里
        # （GET 参数分页）—— 这一点是从浏览器里"尾页"链接找到的，
        # 首页分页控件只显示到"尾页"却不会告诉你它指向别处。
        page_url_template="http://wzyy.hebei.chinatax.gov.cn/was5/web/search?"
                          "&channelid=245955&searchword=docchannel=45812"
                          "&perpage=15&page={n}",
        page_count=330,
    ),
    ListPageAdapter(
        source_id="hebei_zcjd",
        region="河北",
        site_name="国家税务总局河北省税务局",
        list_url="http://hebei.chinatax.gov.cn/hbsw/sszc/zcjd/",
        detail_href_re=r"\./\d{6}/t\d+_\d+\.html",
        base_url="http://hebei.chinatax.gov.cn/hbsw/sszc/zcjd/",
        needs_js=True,
    ),
    # 青海：**列表是 JS 渲染的** —— 同一份 HTML 用 HTTP 直连只拿到栏目壳
    # （一个详情链接都没有），走浏览器渲染后才有 10-16 条。
    # 它的证书同样与域名不匹配，所以用 http。
    # 详情形如 /web/zxfg/202609/<32 位十六进制>.shtml。
    ListPageAdapter(
        source_id="qinghai_zxfg",
        region="青海",
        site_name="国家税务总局青海省税务局",
        list_url="http://qinghai.chinatax.gov.cn/web/zxfg/xxgk_fdzd_list.shtml",
        detail_href_re=r"/web/(?:zxfg|zcjd|zcfg)/\d{6}/[0-9a-f]{32}\.shtml",
        base_url="http://qinghai.chinatax.gov.cn",
        needs_js=True,
        wait_ms=12000,
        # 注：本站「政策法规库」(/web/zcfgk/fgk.shtml) 是 JS 检索页，
        # 静态列表解析不出条目（实测 0 条），故未配 extra_urls。
    ),
    ListPageAdapter(
        source_id="qinghai_zcfg",
        region="青海",
        site_name="国家税务总局青海省税务局",
        list_url="http://qinghai.chinatax.gov.cn/web/zcfg/zcwj.shtml",
        detail_href_re=r"/web/(?:zxfg|zcjd|zcfg)/\d{6}/[0-9a-f]{32}\.shtml",
        base_url="http://qinghai.chinatax.gov.cn",
        needs_js=True,
        wait_ms=12000,
    ),
)

ADAPTERS_BY_ID: dict[str, ListPageAdapter] = {a.source_id: a for a in ADAPTERS}


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
                    siblings = [sib for sib in gp.iterchildren()
                                if sib is not parent and not sib.xpath(".//a[@href]")]
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
                    inner = list(parent.iterchildren()) if parent is not None else []
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


def fetch_list_pages(client: GuardedClient, adapter: ListPageAdapter) -> list[dict]:
    """抓取适配器配置的**所有**列表页并合并去重。

    为什么要支持多个：省级站的「最新文件」是**固定展示最近一二十条的单页列表**
    （实测河南/辽宁/广东都没有翻页控件），历史政策分散在按税种分的子栏目里。

    单个子栏目空/失败**不算整源失败**，全都拿不到才算 —— "零条目即报错"这条
    防线针对的是"栏目 URL 猜错或页面改版"，而不是"某个子栏目恰好没内容"。
    """
    from dataclasses import replace

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
    return out


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
