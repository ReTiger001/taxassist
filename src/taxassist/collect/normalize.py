"""字段清洗：把官方原始字段转成规范值。

原则：**清洗只做可验证的机械转换，不做语义猜测。**
凡涉及"这份文件还有效吗"这类语义判断，一律留给 effect 模块，
并且必须带证据与置信度——因为它直接决定你据以申报的依据是否成立。
"""
from __future__ import annotations

import json
import re

from ..config import NATIONWIDE
from ..db import now_iso

_WS_RE = re.compile(r"[\s\u3000\u00a0]+")
_CN_NUM = {"零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def norm_text(v) -> str | None:
    """归一空值：全角空格、零宽字符、空列表都算 None。"""
    if v is None:
        return None
    if isinstance(v, (list, tuple)):
        parts = [norm_text(x) for x in v]
        parts = [p for p in parts if p]
        return "、".join(parts) if parts else None
    s = str(v)
    s = s.replace("\u200b", "").replace("\ufeff", "")
    s = _WS_RE.sub(" ", s).strip()
    return s or None


def norm_date(v) -> str | None:
    """日期归一为 YYYY-MM-DD。

    官方给的形态实测是 ``2026-09-29 00:00:00``；也容忍 ``2026-09-29``、
    ``2026/9/29``、``2026年9月29日``。
    """
    s = norm_text(v)
    if not s:
        return None
    m = re.match(r"(\d{4})\D{1,2}(\d{1,2})\D{1,2}(\d{1,2})", s)
    if not m:
        return None
    y, mo, d = (int(x) for x in m.groups())
    try:
        return f"{y:04d}-{mo:02d}-{d:02d}"
    except ValueError:
        return None


def norm_json_list(v) -> list[str]:
    """官方部分字段是 JSON 数组字符串，如 ``["税收政策"]``，也有纯字符串/空串。"""
    s = norm_text(v)
    if not s:
        return []
    if s.startswith("["):
        try:
            parsed = json.loads(s)
            if isinstance(parsed, list):
                return [x for x in (norm_text(i) for i in parsed) if x]
        except json.JSONDecodeError:
            pass
    return [s]


def dumps_list(items: list[str]) -> str | None:
    return json.dumps(items, ensure_ascii=False) if items else None


# ------------------------------------------------------------------ 文号

# 文号形态一：标题里直接写全 —— "……公告2021年第10号"
# 注意：多机关联合发文时机关名用空格分隔（"甲 乙 丙 国家税务总局公告…"），
# 所以文号要从机关序列的起点一直取到"号"为止，不能只取紧邻的那个机关名。
_DOCNO_TAIL_RE = re.compile(r"(?P<year>20\d{2})\s*年\s*第\s*(?P<seq>\d{1,4})\s*号")

# 文号形态二：括号式 —— "（财税〔2016〕36号）"
_BRACKET_DOCNO_RE = re.compile(
    r"(?P<agency>[\u4e00-\u9fa5]{2,20})[〔\[（(]\s*(?P<year>20\d{2})\s*[〕\]）)]\s*"
    r"(?P<seq>\d{1,4})\s*号"
)

# 机关名序列的截断标记：出现这些词说明前面是标题正文而非发文机关
_ISSUER_STOPS = ("关于", "的", "，", "。", "；", "：", "（", "(", "〔", "[", "《", "》", ":")

# 纯体裁词不构成文号前缀（避免把 "公告2021年第3号" 当成完整文号）
_PURE_GENRE_RE = re.compile(r"^(公告|通知|令|决定|办法|规定|意见|通告)$")

# ------------------------------------------------------------------ 正文污染裁剪
#
# 正文里引用他文时，文号前面那段是句子而不是机关名，贪婪向左匹配会把整句吃进来。
# 实测污染样本（全库 11 条）：
#   "其企业所得税优惠政策可以按照财税〔2005〕2号"
#   "考虑在粤人社规〔2018〕15号"
#   "展期内销售的进口展品继续按照财关税〔2019〕38号"
#   "注释 根据国家税务总局公告2018年第33号"
# 这些词只在正文里出现，机关名里不会用；黑名单刻意收得很紧，
# 宁可漏裁也不要把"人力资源和社会保障部"这类真机关名裁坏。
_NOISE_WORDS = ("考虑", "注释", "根据", "按照", "以及", "享受", "继续",
                "可以", "并且", "据此", "其中", "如下", "规定", "有关")
# 只在正文里出现的单字。**刻意不含"关"**："海关总署"是常见发文机关，
# 按字符挡会把它误伤成"关总署"。
_NOISE_CHARS = "其并要在据照受续以可"

# 机关名的尾部特征：以这些结尾的候选按"机关名"对待，规则更宽 ——
# 否则"国家数据局"这类碰巧含噪声字的真机关名会被裁坏。
_ORG_TAIL = ("总局", "管理局", "局", "部", "委", "院", "署", "厅", "会", "府",
             "办公室", "办公厅", "中心", "银行", "分行")


def _has_noise(text: str) -> bool:
    return any(w in text for w in _NOISE_WORDS) or any(c in text for c in _NOISE_CHARS)


def _usable(candidate: str) -> bool:
    if any(w in candidate for w in _NOISE_WORDS):
        return False
    if candidate.endswith(_ORG_TAIL):
        # 机关名：只看首字，避免"据国家税务总局"这种粘了一个噪声字的
        return candidate[0] not in _NOISE_CHARS
    return not any(c in candidate for c in _NOISE_CHARS)


def strip_body_noise(text: str) -> str:
    """把文号前缀裁成真正的机关名 / 文号简称。

    整串干净就原样返回（**长机关名不能被右截** —— "中华人民共和国工业和信息化部
    国家发展改革委 财政部 国家税务总局公告" 有 35 字，按长度截会把机关名砍掉一半）；
    否则从右往左取最长的可用后缀。
    """
    s = (text or "").strip()
    if len(s) <= 2:
        return s
    for n in range(len(s), 1, -1):
        candidate = s[-n:]
        if _usable(candidate):
            return candidate
    return s[-2:]


# 文号主体的起点：〔年〕式，或"2018年第33号"式。
# **判定与修复必须共用同一个切分点**，否则会出现"判定为脏、却裁不动"的记录。
_DOCNO_BODY_RE = re.compile(r"[〔\[（(]|\d{4}\s*年\s*第\s*\d+\s*号")


def looks_contaminated(doc_no: str | None) -> bool:
    """文号里的机关前缀是否混进了正文用词。

    用来找出历史上被污染、但**值非空**因而永远不会被 backfill 重算的记录。
    """
    if not doc_no:
        return False
    m = _DOCNO_BODY_RE.search(doc_no)
    prefix = doc_no[:m.start()] if m else doc_no
    return _has_noise(prefix)


def repair_doc_no_prefix(doc_no: str) -> str | None:
    """只裁掉文号里混进来的正文前缀，**保留原本的文号主体**。无需修复时返回 None。

    为什么不重新从正文提取：正文里常常引用**多处**文号，重新 search 取到的第一处
    未必是本记录那一份 —— 实测把"根据财企〔2002〕266号"换成了另一个文件的
    "财企〔2001〕820号"，比原来的前缀污染更糟。错的只是前缀，主体本来就是对的。
    """
    if not doc_no:
        return None
    m = _DOCNO_BODY_RE.search(doc_no)
    if not m:
        return None
    prefix, body = doc_no[:m.start()], doc_no[m.start():]
    cleaned = strip_body_noise(prefix)
    if cleaned == prefix:
        return None
    return f"{cleaned}{body}"


def _issuer_start(title: str, pos: int) -> int | None:
    """定位 "2026年第9号" 之前的发文机关序列起始下标；无法确定时返回 None。

    实测背景：联合发文标题形如
    ``中华人民共和国工业和信息化部 国家发展改革委 财政部 国家税务总局公告2021年第10号``。
    早期用非贪婪正则匹配，结果只抓到最后一个机关名（国家税务总局），
    丢掉其余发文机关 —— 文号引用会不完整。
    """
    prefix = title[:pos]
    lower = 0
    for stop in _ISSUER_STOPS:
        i = prefix.rfind(stop)
        if i >= 0:
            lower = max(lower, i + len(stop))
    seg = prefix[lower:]
    stripped = seg.lstrip(" \u3000、")
    start = lower + (len(seg) - len(stripped))
    body = title[start:pos].strip()
    if not body or _PURE_GENRE_RE.match(body):
        return None
    if not re.fullmatch(r"[\u4e00-\u9fa5A-Za-z0-9 \u3000、()（）]{2,80}", body):
        return None
    cleaned = strip_body_noise(body)
    if cleaned != body:
        # 裁掉正文前缀后必须把起点右移，否则文号里会留着"根据""按照"这类正文词
        found = title.find(cleaned, start, pos)
        if found < 0:
            return None
        return found
    return start


def build_doc_no_full(
    title: str | None,
    doc_year: str | None,
    doc_no_raw: str | None,
    pub_name: str | None,
) -> tuple[str | None, str]:
    """尽力重建完整文号，返回 (文号, 置信度)。

    **为什么必须重建**：官方 API 的 ``govDoc.docNo`` 实测只是序号（如 "19"），
    直接当文号用会导致检索和引用全错。

    置信度：
    - high   —— 标题里直接写全了（如 "……公告2021年第10号"）
    - medium —— 由 年份 + 序号 + 发文机关 拼出
    - low    —— 信息不全，只能给部分拼接结果或 None
    """
    t = norm_text(title) or ""

    m = _DOCNO_TAIL_RE.search(t)
    if m:
        start = _issuer_start(t, m.start())
        if start is not None:
            seg = " ".join(t[start:m.end()].split())
            if 4 <= len(seg) <= 120:
                return seg, "high"

    m2 = _BRACKET_DOCNO_RE.search(t)
    if m2:
        return (f"{m2.group('agency')}〔{m2.group('year')}〕{m2.group('seq')}号", "high")

    year = norm_text(doc_year) or ""
    # 官方 docYear 常为空；退而从标题或成文年份推断
    if not year:
        my = re.search(r"(20\d{2})", t)
        year = my.group(1) if my else ""

    seq = norm_text(doc_no_raw) or ""
    issuer = norm_text(pub_name) or ""
    if year and seq and seq.isdigit():
        if issuer:
            # 源站 pubName 用逗号分隔多机关（"财政部,国家税务总局"），
            # 而公文里机关名之间是空格 —— 照搬会得到一个现实中不存在的文号形态。
            # 注意：这类拼装文号的置信度只能是 medium —— 它由"机关+年份+序号"拼出，
            # 不等于原文里的真实文号（真实文号形如"财税〔2013〕57号"）。
            return f"{issuer.replace(',', ' ')}{year}年第{seq}号", "medium"
        return f"{year}年第{seq}号", "low"
    return None, "low"


def extract_full_doc_no(text: str | None) -> str | None:
    """从任意文本里抽取**第一处**完整文号（保留多机关前缀）。

    详情页把文号写在标题下方，形如 ``财政部 税务总局公告2026年第28号``。
    早期 detail.py 直接用"机关名+体裁+年份+序号"的单段正则，结果只匹配到
    "税务总局公告2026年第28号"，**丢掉了前面的"财政部"** ——
    与联合发文文号不完整是同一个毛病。此处复用 ``_issuer_start`` 向前取全机关序列。
    """
    t = norm_text(text)
    if not t:
        return None
    m = _DOCNO_TAIL_RE.search(t)
    if m:
        start = _issuer_start(t, m.start())
        if start is not None:
            seg = " ".join(t[start:m.end()].split())
            # 上限 30 字：实测从正文提取到过跨句拼接的垃圾
            # （"年度企业所得税优惠政策和财关税〔2021〕4号"），真实文号都很短。
            # 放在这个共用函数里，详情页抓取与离线补全两条路径同时受约束。
            if 4 <= len(seg) <= 30:
                return seg
    m2 = _BRACKET_DOCNO_RE.search(t)
    if m2:
        # agency 用的是贪婪汉字串，同样会把正文吃进来，一并裁剪
        agency = strip_body_noise(m2.group("agency"))
        return f"{agency}〔{m2.group('year')}〕{m2.group('seq')}号"
    return None


# ------------------------------------------------------------------ 行构造

def build_policy_row(item: dict) -> dict | None:
    """把 API 返回的条目转成 policy 表的一行。

    只做字段映射与清洗；缺关键字段（doc_uid / title）返回 None，
    由调用方计数并报警——**静默丢数据是不可接受的**。
    """
    doc_uid = norm_text(item.get("id"))
    title = norm_text(item.get("title"))
    if not doc_uid or not title:
        return None

    gov_doc = item.get("govDoc") or {}
    doc_year = norm_text(gov_doc.get("docYear"))
    doc_no_raw = norm_text(gov_doc.get("docNo"))
    # 源站 pubName 用逗号连接多机关（"财政部,国家税务总局"），公文里是空格 ——
    # 直接显示会很难看，也让"发文机关"这一列失去专业感
    pub_name = (norm_text(item.get("pubName")) or "").replace(",", " ").strip() or None

    doc_no_full, confidence = build_doc_no_full(title, doc_year, doc_no_raw, pub_name)

    tax_policy = norm_json_list(item.get("xxgk_taxPolicy"))
    industries = norm_json_list(item.get("industries")) or norm_json_list(item.get("industrytypename"))

    row = {
        "doc_uid": doc_uid,
        "url": norm_text(item.get("url")),
        "snapshot_url": norm_text(item.get("snapshotUrl")),
        "url_md5": norm_text(item.get("urlMD5")),
        "title": title,
        "second_title": norm_text(item.get("secondTitle")),
        "o_column": norm_text(item.get("column")),
        "o_label": norm_text(item.get("label")),
        "o_typename": norm_text(item.get("typename")),
        "o_site_name": norm_text(item.get("siteName")),
        "cwrq": norm_date(item.get("cwrq")),
        "pub_date": norm_date(item.get("pubDate")),
        "o_abolish_date": norm_date(item.get("xxgk_abolishDate")),
        "o_doc_no_raw": doc_no_raw,
        "o_doc_num": norm_text(gov_doc.get("docNum")),
        "o_doc_type": norm_text(gov_doc.get("docType")),
        "o_doc_year": doc_year,
        "p_doc_no_full": doc_no_full,
        "p_doc_no_confidence": confidence,
        "pub_name": pub_name,
        # 注意：xxgk_effectLevel 实测是"文件类型"而非效力等级，故存入 o_file_type
        "o_file_type": norm_text(item.get("xxgk_effectLevel")),
        "o_aging": norm_text(item.get("xxgk_aging")),
        "o_tax_policy": dumps_list(tax_policy),
        "o_tax_discount": norm_text(item.get("xxgk_taxDiscount")),
        "o_industry_type": norm_text(item.get("xxgk_industryType")),
        "o_taxpayer_type": norm_text(item.get("xxgk_taxpayerType")),
        "o_policy_file_type": norm_text(item.get("xxgk_policyFileType")),
        "o_revise_type": norm_text(item.get("xxgk_reviseType")),
        "o_resolve_type": norm_text(item.get("xxgk_resolveType")),
        "o_related_policy": norm_text(item.get("xxgk_relatedPolicyFileName")),
        # keywords 实测有两种形态：list（["增值税"]）与 JSON 字符串（'[""]'）。
        # 统一清洗后再存，否则界面会把 '[""]' 这种原始 JSON 当作关键词显示出来。
        "o_keywords": norm_text(norm_json_list(item.get("keywords"))),
        "o_industries": dumps_list(industries),
        "content": norm_text(item.get("content")),
        "zw_content": norm_text(item.get("zwcontent")),
        "short_content": norm_text(item.get("shortContent")),
        # 地区：本函数处理总局源，固定为"全国"；省级源见 province.build_provincial_row
        "p_region": NATIONWIDE,
        "first_seen_at": now_iso(),
        "last_seen_at": now_iso(),
    }
    return row
