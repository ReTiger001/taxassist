"""政策详情页抓取与结构化提取。

======================================================================
实测结论（2026-09，改动前先复核）
======================================================================

1. 详情页（``content.html``）是**静态 HTML**，关键内容都在页面源码里。
   （曾一度误判为 JS 异步加载：某条公告的最大文本块只有 493 字符，
   但那是因为该公告本身很短——教训是别用"文本长度"判断页面是否需要渲染。）

2. 正文段落 = ``div.article`` 下**无 class 的 ``<p>``**；
   外层 ``div.currency.cont`` 会混入导航（"当前位置…"）、分享按钮、订阅提示。

3. 官方时效标注写在 ``p.arc_date``，形如 ``尚未生效 成文日期：2026-09-04``。
   这是列表接口 ``xxgk_aging``（填充率仅 2%）拿不到的部分。

4. 完整文号出现在标题下方（如 ``国家税务总局公告2026年第19号``），
   **比列表接口的 ``govDoc.docNo``（只有序号 "19"）可靠得多**。

5. 附件是相对路径（``5252176/files/xxx.pdf``），需与详情页 URL 拼接。

6. 页面里还有"关联解读 / 关联文件 / 关联问答"区块 —— 引用关系图的原料，
   但需登录后展开，静态页里可能只有标题，故此处只做尽力提取。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin

from lxml import html as LH

from .http import GuardedClient
from .normalize import extract_full_doc_no, norm_text

log = logging.getLogger(__name__)

# 文号抽取统一走 normalize.extract_full_doc_no（支持多机关前缀，见该函数注释）

# 官方时效关键词 → 归一状态
_AGING_MAP = (
    ("尚未生效", "尚未生效"),
    ("已废止", "已废止"),
    ("全文废止", "已废止"),
    ("全文失效", "已废止"),
    ("部分失效", "部分失效"),
    ("部分废止", "部分失效"),
    ("失效", "已废止"),
    ("废止", "已废止"),
    ("现行有效", "现行有效"),
    ("有效", "现行有效"),
)

_DATE_IN_AGING_RE = re.compile(r"成文日期[:：]\s*(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})")

# 施行日期："本公告自2026年11月1日起施行"
_EFFECTIVE_RE = re.compile(
    r"自\s*(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日起(?:施行|执行|实施)"
)

_ATTACH_EXT_RE = re.compile(r"\.(pdf|docx?|xlsx?|pptx?|zip|rar|wps|et)(?:\?|$)", re.I)

# 正文里需要剔除的界面噪音
_NOISE_PREFIXES = (
    "当前位置：", "字体：", "分享到：", "收藏", "订阅", "语音播报", "扫一扫",
    "【打印】", "纠错或建议", "用户登录", "忘记密码", "登 录", "没有账号",
    "关联解读", "关联文件", "关联问答", "网站纠错", "责任编辑",
)

# 整段完全等于这些词的一律不是正文（实测踩过：导航标签"相关政策文件"
# 曾被当作正文写进库，全库 12 条记录被污染）
_NOISE_EXACT = frozenset({
    "相关政策文件", "相关解读", "关联解读", "关联文件", "关联问答",
    "注释", "分享", "打印", "下载", "收藏", "订阅", "返回顶部", "正文",
})

# 正文最低长度：低于此值视为"这个页面没有政策正文"，
# 宁可不回写，也不要往库里塞界面噪音。
MIN_BODY_CHARS = 30


@dataclass
class DetailResult:
    """详情页提取结果。字段为 None 表示"页面上确实没有"，不是"提取失败"。"""

    body: str | None = None
    doc_no: str | None = None
    aging_official: str | None = None
    cwrq: str | None = None
    effective_date: str | None = None
    attachments: list[dict] = field(default_factory=list)
    related_titles: list[str] = field(default_factory=list)


def _clean_paragraphs(nodes) -> list[str]:
    out: list[str] = []
    for node in nodes:
        text = norm_text(node.text_content())
        if not text or len(text) < 2:
            continue
        if text in _NOISE_EXACT:
            continue
        if any(text.startswith(p) for p in _NOISE_PREFIXES):
            continue
        out.append(text)
    return out


def _norm_cn_date(y: str, m: str, d: str) -> str | None:
    try:
        return f"{int(y):04d}-{int(m):02d}-{int(d):02d}"
    except ValueError:
        return None


def parse_detail(html_text: str, base_url: str = "") -> DetailResult:
    """从详情页 HTML 提取正文、文号、官方时效、施行日期、附件。"""
    doc = LH.fromstring(html_text)
    for bad in doc.xpath("//script | //style | //noscript"):
        parent = bad.getparent()
        if parent is not None:
            parent.remove(bad)

    result = DetailResult()

    # ---------------------------------------------------------- 正文
    body_el = None
    for xpath in (
        '//div[contains(@class,"article")]',
        '//div[contains(@class,"currency") and contains(@class,"cont")]',
        '//div[contains(@class,"detials")]',
    ):
        els = doc.xpath(xpath)
        if els:
            body_el = max(els, key=lambda e: len(e.text_content()))
            break

    if body_el is not None:
        paragraphs = _clean_paragraphs(body_el.xpath(".//p"))
        if not paragraphs:
            whole = norm_text(body_el.text_content())
            if whole:
                paragraphs = [whole]
        body = "\n".join(paragraphs)
        # 质量门槛：太短说明抓到的是界面标签而非政策正文。
        # 教训：曾把导航标签"相关政策文件"（6 字）当作正文写入全库 12 条记录。
        result.body = body if len(body) >= MIN_BODY_CHARS else None

    # ---------------------------------------------------------- 官方时效 + 成文日期
    for el in doc.xpath('//*[contains(@class,"arc_date")]'):
        text = norm_text(el.text_content())
        if not text:
            continue
        for keyword, status in _AGING_MAP:
            if keyword in text:
                result.aging_official = status
                break
        m = _DATE_IN_AGING_RE.search(text)
        if m:
            result.cwrq = _norm_cn_date(*m.groups())

    # ---------------------------------------------------------- 文号
    # **必须逐元素提取，不能拿整页文本去匹配。**
    # 踩过的坑：整页文本会把标题和文号挤在一起（"…的公告 国家税务总局公告2026年第19号"），
    # 标题里的"关于…的"会让机关序列被截断，结果文号提取成 None。
    # 这里只考察自身文本较短（<150 字）的元素，避免把正文段落当文号候选。
    for el in doc.xpath('//*[contains(., "年") and contains(., "号")]'):
        own = " ".join("".join(el.itertext()).split())
        if not own or len(own) > 150:
            continue
        candidate = extract_full_doc_no(own)
        if candidate:
            result.doc_no = candidate
            break

    # ---------------------------------------------------------- 施行日期
    if result.body:
        me = _EFFECTIVE_RE.search(result.body)
        if me:
            result.effective_date = _norm_cn_date(*me.groups())

    # ---------------------------------------------------------- 附件
    seen: set[str] = set()
    for a in doc.xpath("//a[@href]"):
        href = a.get("href") or ""
        if not _ATTACH_EXT_RE.search(href):
            continue
        full = urljoin(base_url, href) if base_url else href
        # 只接受 http/https。抓来的 URL 会直接渲染成 <a href>，而 autoescape
        # 拦不住 `javascript:` 这类 scheme —— 源站被攻破、或明文 HTTP 抓取被
        # 中间人篡改时，用户一点就以本站身份执行脚本（同源，可调 /admin 提权）。
        # 24 个省级源里有 20 个是明文 http://，这条不是理论风险。
        if not full.lower().startswith(("http://", "https://")):
            continue
        if full in seen:
            continue
        seen.add(full)
        name = norm_text(a.text_content()) or href.rsplit("/", 1)[-1]
        ext = _ATTACH_EXT_RE.search(href)
        result.attachments.append({
            "url": full,
            "filename": name,
            "ext": (ext.group(1).lower() if ext else ""),
        })

    # ---------------------------------------------------------- 关联文件标题（尽力）
    for el in doc.xpath('//*[contains(@class,"artsets")]//a'):
        t = norm_text(el.text_content())
        if t and len(t) > 4:
            result.related_titles.append(t)

    return result


def fetch_detail(client: GuardedClient, url: str) -> tuple[bytes, DetailResult]:
    """抓取并解析一个详情页。返回 (原始字节, 提取结果)。"""
    resp = client.get(url)
    resp.raise_for_status()
    raw = resp.content
    # 站点为 UTF-8；显式解码避免 httpx 猜测出错
    html_text = raw.decode("utf-8", "replace")
    return raw, parse_detail(html_text, base_url=url)
