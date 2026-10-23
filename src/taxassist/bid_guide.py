"""投标指引 —— 把「看到标」补成「知道怎么投」。

## 为什么需要这个模块

2026-10-24 用户反馈：招标信息都能看见，但点进去**不知道怎么投标** ——
看不到原文、不知道去哪报名、没有联系方式。实测数据：

- 674 条里只有 **137 条（20%）** 有原文/正文，**537 条（80%）** 只有列表级信息；
- 这 537 条几乎全来自 cebpub（官方聚合器），它的详情页被 vaptcha 挡住，程序拿不到；
- 有正文的 136 条里，**38% 含邮箱**、**61% 含 http 链接** —— 这些信息一直没被提取。

所以分两步补：
1. **有正文的**：从正文提取邮箱 + 报名方式 + 招标文件获取 + 资质要求（纯正则，不用 AI）；
2. **没正文的**：靠 `PLATFORM_LINKS` 把「平台名」映射成网址，让用户能跳过去自己看。

## 为什么用正则而不是 AI

用户明确要求"先不要启动 AI"。而且这些信息的格式高度固定（「联系人：」「电话：」
「邮箱：」「报名时间：」「投标人资格要求：」），正则命中率足够，还零成本。
AI 留给后续做「投标行动清单」那种需要理解的活。
"""
from __future__ import annotations

import html
import json
import logging
import re

log = logging.getLogger(__name__)

#: 原始发布平台名 → 网址。
#:
#: cebpub 的列表只给平台名（`transactionPlatfName`），不给网址。用户要跳过去投标
#: 就必须有这个映射。**键用平台名的稳定片段做包含匹配**（官方名称常带全角引号、
#: 空格、变更，精确匹配会漏）。
PLATFORM_LINKS: dict[str, str] = {
    "邮银易采": "https://cg.psbc.com/",
    "邮政储蓄银行": "https://cg.psbc.com/",
    "湖北联投": "https://www.ltzxgl.com.cn/",
    "南方电网": "https://www.bidding.csg.cn/",
    "铁建云链": "https://www.crccep.com/",
    "中国中铁": "https://www.crecgec.com/",
    "天府阳光": "https://www.tfygcgfw.com/",
    "江苏": "https://www.jszhaobiao.com/",
    "中国海油": "https://buy.cnooc.com.cn/",
    "国家能源": "https://www.chnenergybidding.com.cn/",
    "鞍钢": "https://ebid.ansteel.cn/",
    "国家管网": "https://www.pipechina.com.cn/",
}

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

#: 「报名」「获取招标文件」「资格要求」这几段的起止标记。
#: 公告的段落标题基本是这几种写法，取「标题 + 后面一段」就够给用户做判断。
_SECTION_PATTERNS = {
    "报名方式": re.compile(
        r"(?:报名(?:方式|时间|地点|流程)?|获取(?:招标|采购|谈判)?文件(?:方式|时间|地点)?)"
        r"\s*[:：]?\s*([^\n]{0,300})"),
    "资格要求": re.compile(
        r"(?:投标人|供应商)?(?:资格|资质)(?:要求|条件)\s*[:：]?\s*([^\n]{0,400})"),
    "递交方式": re.compile(
        r"(?:递交|提交)(?:投标|响应)?文件(?:的)?(?:方式|地点|截止时间)?\s*[:：]?\s*([^\n]{0,300})"),
}

#: 明显是页脚/导航的噪声行（提取前先剔掉）
_NOISE_RE = re.compile(r"^(?:版权所有|Copyright|技术支持|友情链接|网站地图|关于我们)")


def platform_url(agency: str | None) -> str | None:
    """平台名 → 网址。找不到返回 None（页面会退回「去官方搜索」的链接）。"""
    a = (agency or "").strip()
    if not a:
        return None
    for key, url in PLATFORM_LINKS.items():
        if key in a:
            return url
    return None


def extract_contact_email(text: str) -> str | None:
    """从正文里抽第一个邮箱（招标公告里通常只有一个）。"""
    m = _EMAIL_RE.search(text or "")
    return m.group(0) if m else None


def extract_bid_guide(text: str) -> dict:
    """从正文里抽「怎么投标」的三段关键信息。

    返回 ``{"报名方式": ..., "资格要求": ..., "递交方式": ...}``，没抽到的键不出现。
    每段截断到 300 字符 —— 给用户看关键信息，不是让他读全文。
    """
    if not text:
        return {}
    # **先解码 HTML 实体**：正文是从 HTML 转来的，`&rdquo;` `&nbsp;` 这类残留会让
    # 提取片段变成乱码（实测「递交方式」抽到 `&rdquo;选项）`）。
    clean_text = html.unescape(text)
    clean = "\n".join(
        ln.strip() for ln in clean_text.splitlines()
        if ln.strip() and not _NOISE_RE.match(ln.strip()))
    out: dict[str, str] = {}
    for label, pat in _SECTION_PATTERNS.items():
        m = pat.search(clean)
        if not m:
            continue
        v = re.sub(r"\s+", " ", m.group(1)).strip(" ：:，,。.;；")
        # 过滤残片：太短、以实体/标点开头、纯序号（如「3.1供应商应满足如下要求」
        # 这种只抄了标题半句的也算没抽到 —— 它对用户没有信息量）。
        if len(v) < 6:
            continue
        if v[0] in "&）)】]":
            continue
        if re.fullmatch(r"[\d.\s]{1,8}", v):
            continue
        if re.match(r"^[\d.]+\s*(?:供应商|投标人|应答人)?\s*(?:应|须)?\s*满足", v):
            continue
        out[label] = v[:300]
    return out


def backfill(conn, *, batch: int = 200) -> dict:
    """把已有数据补上：邮箱 / 投标指引 / 平台网址。

    只处理**还没补过**的（``contact_email IS NULL``），可断点续跑。
    纯 CPU，不调任何模型。
    """
    from . import tender as tmod

    tmod.ensure_table(conn)
    from datetime import date

    rows = conn.execute(
        "SELECT doc_uid, source_html, agency FROM tender"
        " WHERE guide_filled_at IS NULL"
        " LIMIT ?", (batch,)).fetchall()
    stats = {"total": len(rows), "email": 0, "guide": 0, "platform": 0}
    for r in rows:
        uid = r["doc_uid"]
        text = r["source_html"] or ""
        updates: dict[str, str | None] = {}
        if text:
            email = extract_contact_email(text)
            if email:
                updates["contact_email"] = email
                stats["email"] += 1
            guide = extract_bid_guide(text)
            if guide:
                updates["bid_guide"] = json.dumps(guide, ensure_ascii=False)
                stats["guide"] += 1
        purl = platform_url(r["agency"])
        if purl:
            updates["platform_url"] = purl
            stats["platform"] += 1
        updates["guide_filled_at"] = date.today().isoformat()
        sets = ", ".join(f"{k} = ?" for k in updates)
        conn.execute(f"UPDATE tender SET {sets} WHERE doc_uid = ?",
                     tuple(updates.values()) + (uid,))
    conn.commit()
    return stats


def guide_of(row) -> dict:
    """读 ``bid_guide`` 字段（JSON 串）→ dict。给模板用，解析失败返回空。"""
    raw = row.get("bid_guide") if hasattr(row, "get") else row["bid_guide"]
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:  # noqa: BLE001
        return {}
