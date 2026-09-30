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
#
# 注意：有些省级条目**本来就没有文字正文**。实测吉林「社保费征收常见问题的
# 回应」等 20 条、广东「税路通·粤通四海」等 24 条，正文区里只有模板占位符
# 「用于文章仅有视频时保存」和分享按钮 —— 它们本身就是视频/图片内容。
# 这类页面的无障碍容器（barrierfree_container）里确实有 400+ 字，但那是
# 导航 + 页脚 + 来源 + 字号按钮，把它当正文写进去比留空更糟。
# 所以这些条目保持无正文是**正确行为**，不要当成"解析器又坏了"去修。
MIN_BODY_CHARS = 30
# 挑选正文容器时的门槛：太短的容器是界面元素，不是正文。
# 实测吉林页面上有个内容很少的 TRS_Editor 空壳，排在真正文容器
# （container content，821 字）前面 —— "第一个命中就 break"会把真正文挡在
# 外面，这正是吉林 19 条里 16 条抓不到正文的原因。两个门槛语义不同：
# 这个决定"拿哪个容器"，MIN_BODY_CHARS 决定"拿到的东西够不够格当正文"。
_BODY_MIN_HINT = 100


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
    fallback = None      # 所有选择器都没找到够量容器时的保底
    # 正文容器。省级站与总局用的**不是一套模板** —— 这正是"20 个省 267 条
    # 政策全都抓不到正文"的根因之一（另一个是详情页没走浏览器）。
    # 实测 452 个省级详情页快照的覆盖：id="zoom" 164、content 105、con 51、
    # TRS_Editor 42、article 39、container content 32、info-cont 11。
    # id="zoom" 与 TRS_Editor 是中国政府站（TRS / 方正 CMS）的通用产物，
    # 跨省命中率最高；content / con 最泛，所以放最后兜底。
    for xpath in (
        '//*[@id="zoom"]',
        # 天津用这两个 id（注意后者在原站就是这么拼的，不是笔误）
        '//*[@id="htmlContent"]',
        '//*[@id="conntentNR"]',
        '//*[contains(@class,"TRS_Editor")]',
        '//div[contains(@class,"article")]',
        '//div[contains(@class,"currency") and contains(@class,"cont")]',
        '//div[contains(@class,"detials")]',
        '//div[contains(@class,"info-cont")]',
        '//div[contains(@class,"container") and contains(@class,"content")]',
        '//div[contains(@class,"content")]',
        '//div[contains(@class,"con")]',
    ):
        els = doc.xpath(xpath)
        if not els:
            continue
        # 内容够量的优先。但**不能因为短就当作没找到** —— 短正文的政策
        # 确实存在（上海的 138 字通知），而且吉林页面上那个空壳 TRS_Editor
        # 恰恰证明了另一面：它在前面，真正文（container content，821 字）
        # 在后面，所以这里不 break，继续往下试，同时留个保底。
        good = [e for e in els
                if len(" ".join(e.itertext()).strip()) >= _BODY_MIN_HINT]
        if good:
            body_el = max(good, key=lambda e: len(e.text_content()))
            break
        if fallback is None:
            fallback = max(els, key=lambda e: len(e.text_content()))
    if body_el is None:
        body_el = fallback

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
