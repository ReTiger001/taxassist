"""通用详情补全 —— 对招标公告的原始链接（source_url）抓正文/联系方式/时间。

## 为什么需要「通用」这一层

招标公告分散在 124 个原始平台（邮储、中国海油、甘肃智慧阳光、各央企自建平台…），
逐个写适配器成本极高。但它们有一个共性：**source_url 指向的是发布方的详情页**。
所以先做一层通用的「打开链接 → 提取正文 → 抽联系方式」，能覆盖一批平台，
剩下的再按平台单独适配。

## 为什么要用浏览器

实测 69 条 source_url 里只有 37% 能直抓：其余是 SPA（`#/` 路由，HTML 里没有内容）、
反爬 406、502。浏览器渲染能解决 SPA 这一类。

## 提取策略

不依赖各平台的 DOM 结构（那要逐个适配），而是：
1. 渲染后取全文文本
2. **找最长的一段连续文本块**当正文（导航/页脚通常短而碎）
3. 用正则抽电话、邮箱、联系人、日期 —— 这些格式全国通用
"""
from __future__ import annotations

import asyncio
import logging
import re

log = logging.getLogger(__name__)

#: 电话：座机（带区号）/ 手机 / 400 客服。招标公告里的联系方式基本是这几种。
_PHONE_RE = re.compile(
    r"(?:(?:0\d{2,3}[-\s]?\d{7,8}(?:[-\s]?\d{1,5})?)|(?:1[3-9]\d{9})|(?:400[-\s]?\d{3}[-\s]?\d{4}))")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
#: 联系人：常见写法「联系人：张三」「项目联系人: 李四」
_PERSON_RE = re.compile(r"(?:联系人|项目联系人|采购联系人)\s*[:：]\s*([\u4e00-\u9fa5]{2,4})")
#: 报名/开标时间
_SIGNUP_RE = re.compile(r"(?:报名|获取.{0,6}文件).{0,10}?[:：]?\s*(20\d{2}[-/年]\d{1,2}[-/月]\d{1,2})")
_TENDER_RE = re.compile(r"(?:开标|投标截止|递交.{0,6}截止).{0,10}?[:：]?\s*(20\d{2}[-/年]\d{1,2}[-/月]\d{1,2})")

#: 明显是导航/页脚/按钮的短行，拼进正文会污染内容
_NOISE_PAT = re.compile(
    r"^(?:首页|上一页|下一页|返回|登录|注册|搜索|更多|详情|打印|关闭|分享|"
    r"版权所有|Copyright|技术支持|关于我们|联系我们)$", re.I)


def html_to_text(html: str) -> str:
    """HTML → 朴素文本。保留换行以便判断"最长连续文本块"。"""
    s = re.sub(r"<script.*?</script>", " ", html or "", flags=re.S | re.I)
    s = re.sub(r"<style.*?</style>", " ", s, flags=re.S | re.I)
    s = re.sub(r"</(?:p|div|tr|li|h\d|section|article)>", "\n", s, flags=re.I)
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"</t[dh]>", " ", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = (s.replace("&nbsp;", " ").replace("&amp;", "&")
          .replace("&lt;", "<").replace("&gt;", ">").replace("&#39;", "'"))
    s = re.sub(r"[ \t\u3000]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n", s)
    return s.strip()


def extract_body(text: str, min_len: int = 200) -> str:
    """从全文里挑「正文」：取最长的一段连续非空行块。

    比固定 CSS 选择器可靠得多 —— 各平台 DOM 完全不同，但正文总是页面上
    最长、最连续的那块文本（导航和页脚都是零散短行）。
    """
    lines = [ln.strip() for ln in (text or "").split("\n")]
    best: list[str] = []
    cur: list[str] = []
    for ln in lines:
        keep = bool(ln) and not _NOISE_PAT.match(ln) and len(ln) > 2
        if keep:
            cur.append(ln)
        else:
            if sum(len(x) for x in cur) > sum(len(x) for x in best):
                best = cur
            cur = []
    if sum(len(x) for x in cur) > sum(len(x) for x in best):
        best = cur
    body = "\n".join(best).strip()
    return body if len(body) >= min_len else (body or text.strip())


def extract_fields(text: str) -> dict:
    """从正文里抽联系方式与时间（格式全国通用，不依赖平台）。"""
    t = text or ""
    phones = []
    for m in _PHONE_RE.findall(t):
        p = m.strip()
        if p not in phones:
            phones.append(p)
    persons = []
    for m in _PERSON_RE.findall(t):
        if m not in persons:
            persons.append(m)
    emails = []
    for m in _EMAIL_RE.findall(t):
        if m not in emails:
            emails.append(m)
    signup = _SIGNUP_RE.search(t)
    tender = _TENDER_RE.search(t)
    contact_bits = []
    if persons:
        contact_bits.append("联系人：" + "、".join(persons[:2]))
    if phones:
        contact_bits.append("电话：" + "、".join(phones[:2]))
    if emails:
        contact_bits.append("邮箱：" + emails[0])
    return {
        "contact_person": persons[0] if persons else None,
        "contact_phone": phones[0] if phones else None,
        "contact_text": "；".join(contact_bits) or None,
        "signup_time": signup.group(1) if signup else None,
        "tender_time": tender.group(1) if tender else None,
    }


def fetch_with_browser(url: str, wait_ms: int = 6000) -> str:
    """用真浏览器打开 URL，返回渲染后的 HTML。失败返回空串。"""
    try:
        import nodriver as uc
    except ImportError:
        log.warning("未安装 nodriver，无法渲染 SPA")
        return ""

    from .browser import _find_browser_binary

    async def _run() -> str:
        browser = await uc.start(headless=True,
                                 browser_executable_path=_find_browser_binary())
        try:
            page = await browser.get(url)
            await page.sleep(wait_ms / 1000)
            return await page.get_content()
        finally:
            browser.stop()

    try:
        return asyncio.run(_run())
    except Exception as e:  # noqa: BLE001
        log.warning("浏览器抓取失败 %s：%s: %s", url, type(e).__name__, e)
        return ""


def fetch_source_detail(url: str) -> dict:
    """抓一条原始链接的详情。返回 ``{source_html, contact_*, signup_time, tender_time}``。"""
    if not url:
        return {}
    html = fetch_with_browser(url)
    if not html:
        return {}
    text = html_to_text(html)
    body = extract_body(text)
    if not body:
        return {}
    got = extract_fields(text)
    got["source_html"] = body
    return {k: v for k, v in got.items() if v}


def fetch_all_pending(conn, *, limit: int | None = None, progress=None) -> dict:
    """对**有 source_url 但还没有正文**的条目补详情。

    断点续跑：只处理 ``source_html IS NULL AND source_url IS NOT NULL``。
    """
    from .. import tender as tmod

    tmod.ensure_table(conn)
    sql = ("SELECT doc_uid, source_url, title FROM tender"
           " WHERE source_url IS NOT NULL AND source_url != ''"
           "   AND (source_html IS NULL OR source_html = '')"
           " ORDER BY publish_at DESC")
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = [dict(r) for r in conn.execute(sql).fetchall()]
    total = len(rows)
    stats = {"total": total, "fetched": 0, "failed": 0, "no_contact": 0}

    for i, row in enumerate(rows, 1):
        got = fetch_source_detail(row["source_url"])
        if not got:
            stats["failed"] += 1
        else:
            got["doc_uid"] = row["doc_uid"]
            got["source"] = "cebpub" if not row["doc_uid"].startswith("zlbx:") else "zlbx"
            got["title"] = row["title"]
            got["is_tax"] = 1
            if not got.get("contact_phone"):
                stats["no_contact"] += 1
            tmod.upsert(conn, got)
            conn.commit()   # 浏览器抓取慢（每条约 10-20s），逐条提交防丢
            stats["fetched"] += 1
        if progress and (i % 5 == 0 or i == total):
            progress(i, total)
    return stats
