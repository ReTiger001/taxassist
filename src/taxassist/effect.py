"""效力判定与引用关系。

======================================================================
判定证据的可靠性分级（**必须按此顺序，不可越级**）
======================================================================

1. **官方详情页时效标注**（"尚未生效"/"全文失效"）—— 最可靠，但填充率低
2. 官方列表接口 ``xxgk_aging`` —— 实测填充率仅 2%，基本用不上
3. **其他文件正文里的废止声明**（"本公告自……起废止《……》"）—— 需解析，可能误判
4. 官方《失效废止的部分税务规范性文件目录》公告 —— 最权威，需单独抓取（待做）

======================================================================
三条不可妥协的设计约束
======================================================================

**约束一：默认"推定有效"必须标明是推定（``p_effect_source='default'``）。**

库里绝大多数文件确实有效，若默认标"未知"，每条都要人工确认，系统等于没用；
但若把推定显示成官方确认，就是在骗自己 —— 一份被废止的文件如果被当成有效
依据用出去，后果比"没有系统"严重得多。

**约束二：关系抽取与效力判定必须分阶段，不能放在同一个循环里。**

踩过的坑：在同一循环里"抽一条关系 → 判一条效力"会产生**顺序依赖** ——
先被判定的文件看不到后面文件对它的废止声明，于是被错误标成"现行有效"。
这是方向性错误（把失效文件当有效），比漏判严重。因此：
先扫描全部文件抽完所有关系并落库，再统一判定。

**约束三：关系去重必须在应用层做。**

踩过的坑：SQLite 中 NULL 互不相等，``UNIQUE(src, dst_uid, dst_no, relation)``
拦不住 ``dst_doc_uid`` 为 NULL 的重复行，重复判定会不断插入新关系。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date

from .collect.normalize import norm_text
from .db import now_iso

log = logging.getLogger(__name__)

# ------------------------------------------------------------------ 正则

_DOCNO_REF_RE = re.compile(
    r"([\u4e00-\u9fa5、]{2,40}(?:公告|通知|令|决定|规定|办法|通告)\s*20\d{2}\s*年\s*第\s*\d{1,4}\s*号)"
)
_DOCNO_BRACKET_REF_RE = re.compile(
    r"([\u4e00-\u9fa5]{2,20}[〔\[（(]\s*20\d{2}\s*[〕\]）)]\s*\d{1,4}\s*号)"
)

# 引用引导词：正则会把它们一起吃进机关名（"依据财税〔2016〕36号"），提取后要剥掉
_REF_PREFIX_NOISE = ("依据", "根据", "按照", "依照", "参照", "遵照", "详见", "见")

# 汉字之间的空格（PDF 抽取文本常出现，会把连续机关名切断）
_CJK_SPACE_RE = re.compile(r"(?<=[\u4e00-\u9fa5])[\s\u3000]+(?=[\u4e00-\u9fa5])")

# 废止声明模板：目标可能是文件名或文号
_REPEAL_TEMPLATES = (
    re.compile(r"废止\s*《(?P<target>[^》]{2,90})》"),
    re.compile(r"《(?P<target>[^》]{2,90})》\s*(?:同时|一并|予以)?\s*废止"),
    re.compile(r"停止执行\s*《(?P<target>[^》]{2,90})》"),
)

# 明确的"不废止"表述，用于避免方向性误判
_NEGATION_HINTS = ("不废止", "不作废止", "未废止", "不再废止")

# 跳过废止声明的**文本来源**：全国人大制定的法律全文（"中华人民共和国XX法"）。
#
# 理由（实测踩过）：这类文本极长、引用密集，其修订通过修正案而非
# "……同时废止"式条款，对它做废止抽取会大量误报 ——
# 《中华人民共和国刑法》被标成"被《中华人民共和国民法典》废止"，
# 这是**方向性错误**：把有效文件判为失效，会导致使用者误弃有效依据。
#
# 只跳过"法"结尾的；条例/规定/办法等仍参与（它们的废止条款写法更规范，
# 且"企业所得税暂行条例废止旧文"这类判定是真实有价值的）。
_SKIP_REPEAL_SRC_RE = re.compile(r"^中华人民共和国[^《》]{1,24}法$")

EFFECT_VALID = "现行有效"
EFFECT_REPEALED = "已废止"
EFFECT_PARTIAL = "部分失效"
EFFECT_PENDING = "尚未生效"
EFFECT_UNKNOWN = "未知"

_OFFICIAL_STATES = (EFFECT_VALID, EFFECT_REPEALED, EFFECT_PARTIAL, EFFECT_PENDING)


@dataclass
class Relation:
    src_doc_uid: str
    relation: str                 # cites / repeals / amends
    dst_doc_no: str | None = None
    dst_doc_uid: str | None = None
    evidence: str | None = None
    evidence_source: str = "content"
    confidence: str = "low"


@dataclass
class EffectJudgement:
    doc_uid: str
    status: str
    source: str                   # official / inferred / default
    reason: str
    confidence: str = "medium"
    evidence: str | None = None
    needs_review: bool = False
    relations: list[Relation] = field(default_factory=list)


# ------------------------------------------------------------------ 抽取

def _strip_ref_noise(value: str) -> str:
    for prefix in _REF_PREFIX_NOISE:
        if value.startswith(prefix) and len(value) > len(prefix) + 2:
            return value[len(prefix):].strip()
    return value


def _squeeze_cjk_spaces(text: str) -> str:
    """去掉汉字之间的空格。

    **为什么需要**：PDF 抽出的文本常在字间插空格（``财政部税务总 局公告2026 年第9 号``），
    而文号正则以连续汉字匹配机关名，遇到空格就断，结果只能匹配到"总局公告…"，
    丢掉"财政部 税务"。仅用于文号/引用抽取，不改动正文存储。
    """
    return _CJK_SPACE_RE.sub("", text)


def extract_doc_no_refs(text: str | None) -> list[str]:
    """从一段文本里抽取被引用的文号（去重、保持出现顺序）。"""
    t = norm_text(text)
    if not t:
        return []
    t = _squeeze_cjk_spaces(t)
    found: list[str] = []
    for pattern in (_DOCNO_REF_RE, _DOCNO_BRACKET_REF_RE):
        for m in pattern.finditer(t):
            no = _strip_ref_noise(" ".join(m.group(1).split()))
            if no and no not in found:
                found.append(no)
    return found


def _normalise_title(value: str | None) -> str:
    """标题归一化：书名号统一、去空白，用于跨记录比对。"""
    t = norm_text(value) or ""
    for a, b in (("〈", "《"), ("〉", "》"), ("﹝", "《"), ("﹞", "》")):
        t = t.replace(a, b)
    return re.sub(r"[\s\u3000]", "", t)


# 书名号引用：正文引用的主要形式是"《文件名》"而非完整文号。
# 实测依据：某公告正文写"《财政部 税务总局关于发布〈…管理办法〉的公告》（2026年第28号）"，
# 括号里只有年份+序号，用文号正则根本抓不到，而文件名与库内标题能对上。
_TITLE_REF_RE = re.compile(r"《([^《》]{2,140})》")


def extract_title_refs(text: str | None) -> list[str]:
    """抽取书名号中的文件名引用（保持出现顺序、去重）。"""
    t = norm_text(text)
    if not t:
        return []
    found: list[str] = []
    for m in _TITLE_REF_RE.finditer(t):
        ref = norm_text(m.group(1))
        if ref and len(ref) >= 4 and ref not in found:
            found.append(ref)
    return found


def _best_title_match(
    target: str,
    titles: list[tuple[str, str, str]],
    *,
    exclude: str,
    threshold: int = 90,
    skip_statutes: bool = False,
) -> tuple[str | None, str, int]:
    """在库内标题中找最匹配的一条。

    ``titles`` 为 ``(doc_uid, 归一化标题, 文号)`` 列表。
    返回 ``(匹配到的 doc_uid 或 None, 该文件的文号, 相似度分数)``。

    用 ``partial_ratio``：正文常引用文件全名的一部分（例如内层书名号里的
    "境内单位代扣代缴自然人增值税管理办法"），全名匹配会失败。

    ``skip_statutes``：跳过"中华人民共和国XX法"类候选。用于**废止关系**——
    法律只可能被另一部法律废止，不会被部门规范性文件废止，
    否则会出现"《城市房地产管理法》被财税文件废止"这种荒谬结论。
    引用关系（cites）不跳过：正文"依据《增值税法》"是正常引用。
    """
    from rapidfuzz import fuzz

    best: tuple[str | None, str, int] = (None, "", 0)
    for uid, title, docno in titles:
        if uid == exclude or not title:
            continue
        if skip_statutes and _SKIP_REPEAL_SRC_RE.match(title):
            continue
        # 解读/答记者问不是"被废止"的对象。
        # 实测：一份《…失效废止目录的公告》的**解读**被模糊匹配成了原公告，
        # 结果解读自己被标成"已废止"；而"解读"字样本该是排除信号。
        if ("解读" in title or "答记者问" in title) and "解读" not in target:
            continue
        score = int(fuzz.partial_ratio(target, title))
        if score > best[2]:
            best = (uid, docno, score)
    if best[0] and best[2] >= threshold:
        return best
    return (None, "", best[2])


def extract_repeal_targets(text: str | None) -> list[str]:
    """抽取被本文废止的目标（文件名或文号）。

    只返回**明确带废止语义**的片段；含"不废止"等否定表述的整句会被跳过，
    以免把"本条不废止"读成"废止"这类方向性错误。
    """
    t = norm_text(text)
    if not t:
        return []
    targets: list[str] = []
    for sentence in re.split(r"[。；\n]", t):
        if any(hint in sentence for hint in _NEGATION_HINTS):
            continue
        if "废止" not in sentence and "停止执行" not in sentence:
            continue
        for pattern in _REPEAL_TEMPLATES:
            for m in pattern.finditer(sentence):
                target = norm_text(m.group("target"))
                if not target or len(target) < 2:
                    continue
                if target.startswith(("本", "上述", "该", "原")):
                    continue
                if target not in targets:
                    targets.append(target)
    return targets


def _normalise_doc_no(value: str | None) -> str:
    """文号归一化，用于跨记录比对（去空白、统一括号、去"中华人民共和国"前缀）。"""
    v = norm_text(value) or ""
    v = v.replace("中华人民共和国", "")
    v = re.sub(r"[\s\u3000]", "", v)
    for a, b in (("（", "("), ("）", ")"), ("〔", "("), ("〕", ")"), ("[", "("), ("]", ")")):
        v = v.replace(a, b)
    return v


# ------------------------------------------------------------------ 关系落库

def _insert_relation(conn, rel: Relation) -> bool:
    """插入关系，已存在则跳过。返回是否真的插入了新行。

    为什么不用数据库 UNIQUE 约束去重：SQLite 中 NULL 互不相等，
    ``UNIQUE(src, dst_uid, dst_no, relation)`` 拦不住 ``dst_doc_uid`` 为 NULL
    的重复行（未匹配到库内文件的关系 dst_uid 就是 NULL），重复判定会不断插新行。
    """
    exists = conn.execute(
        "SELECT 1 FROM policy_relation WHERE src_doc_uid=? AND relation=?"
        " AND IFNULL(dst_doc_uid,'')=? AND IFNULL(dst_doc_no,'')=? LIMIT 1",
        (rel.src_doc_uid, rel.relation, rel.dst_doc_uid or "", rel.dst_doc_no or ""),
    ).fetchone()
    if exists:
        return False
    conn.execute(
        "INSERT INTO policy_relation"
        "(src_doc_uid, dst_doc_uid, dst_doc_no, relation, evidence, evidence_source,"
        " confidence, created_at) VALUES(?,?,?,?,?,?,?,?)",
        (rel.src_doc_uid, rel.dst_doc_uid, rel.dst_doc_no, rel.relation,
         rel.evidence, rel.evidence_source, rel.confidence, now_iso()),
    )
    return True


# ------------------------------------------------------------------ 主流程

# "失效废止目录"类公告：标题里点名要废止一批文件，但清单在**附件表格**里
_CATALOG_TITLE_RE = re.compile(r"(失效废止|废止和修改|修改和失效废止|废止部分)")


def _catalog_rows(text: str):
    """从目录表格文本（xls 解析所得，"|" 分隔）提取 (标题, 文号)。"""
    for line in text.split("\n"):
        cells = [c.strip() for c in line.split("|") if c.strip()]
        if len(cells) < 2:
            continue
        title = next((c for c in cells if len(c) >= 10 and "关于" in c), None)
        doc_no = next((c for c in cells if re.search(r"〔\s*\d{4}\s*〕", c)), None)
        if title or doc_no:
            yield title, doc_no


def apply_repeal_catalogs(conn) -> dict:
    """从「失效废止目录」公告的附件里提取被废止文件，建立废止关系。

    **为什么必须单独做**：这类公告的正文只有一句"现予公布"，废止清单放在
    附件 xls 里（解析成 ``|`` 分隔的表格文本）。现有正文句式匹配
    （"……同时废止"）完全抓不到，导致**官方明示废止的文件在库里仍标"现行有效"**。
    这是独立验证代理发现的最高危问题（例：某公告点名废止 100+ 份文件，
    库内产生 0 条废止关系）。

    关系来源标记为 ``official_catalog``：它是官方目录点名，可靠性高于
    从正文模糊推断的 ``content`` 来源，界面上按"官方"呈现。
    """
    catalogs = conn.execute(
        "SELECT doc_uid, title FROM policy"
        " WHERE title LIKE '%失效废止%' OR title LIKE '%废止和修改%'"
        "    OR title LIKE '%废止部分%'"
    ).fetchall()

    by_docno: dict[str, str] = {}
    for r in conn.execute(
        "SELECT doc_uid, p_doc_no_full FROM policy WHERE p_doc_no_full IS NOT NULL"
    ):
        key = _normalise_doc_no(r["p_doc_no_full"])
        if key:
            by_docno[key] = r["doc_uid"]

    # 标题索引：**必须靠标题匹配**。实测文号匹配率仅 1%（4/360）——
    # 目录里是原始文号（"国税发〔2010〕1号"），而库内 95% 的 p_doc_no_full
    # 是"机关+年份+序号"的拼装品，两者永远对不上。
    titles: list[tuple[str, str, str]] = []
    for r in conn.execute("SELECT doc_uid, title, p_doc_no_full FROM policy"):
        titles.append((r["doc_uid"], _normalise_title(r["title"]), r["p_doc_no_full"] or ""))

    targets = matched = 0
    for cat in catalogs:
        attachments = conn.execute(
            "SELECT parsed_text FROM attachment"
            " WHERE doc_uid = ? AND parsed_text IS NOT NULL",
            (cat["doc_uid"],),
        ).fetchall()
        seen: set[str] = set()
        for att in attachments:
            for title, doc_no in _catalog_rows(att["parsed_text"]):
                key = _normalise_doc_no(doc_no) if doc_no else ""
                marker = key or _normalise_title(title)
                if not marker or marker in seen:
                    continue
                seen.add(marker)
                targets += 1

                dst_uid = by_docno.get(key) if key else None
                if dst_uid is None and title:
                    dst_uid, _matched_no, _score = _best_title_match(
                        _normalise_title(title), titles, exclude=cat["doc_uid"],
                        threshold=92, skip_statutes=True)
                if dst_uid:
                    matched += 1
                _insert_relation(conn, Relation(
                    src_doc_uid=cat["doc_uid"], relation="repeals",
                    dst_doc_uid=dst_uid, dst_doc_no=doc_no,
                    evidence=f"官方失效废止目录点名：{title or doc_no}",
                    evidence_source="official_catalog",
                    confidence="high" if dst_uid else "medium",
                ))
    conn.commit()
    log.info("官方废止目录：%d 份公告，点名 %d 份文件，匹配到库内 %d 份",
             len(catalogs), targets, matched)
    return {"catalogs": len(catalogs), "targets": targets, "matched": matched}


def judge_effects(conn, *, fuzzy_threshold: int = 88) -> dict:
    """对所有政策做一轮效力判定与关系抽取。

    三个阶段，顺序不可调换：
    1. 扫描全部文件正文，抽取引用与废止关系
    2. 关系落库（应用层去重）
    3. 基于**完整**关系集判定效力（官方标注 > 被废止 > 推定有效）
    """
    from rapidfuzz import fuzz

    # 关系是**派生数据**：每次重跑先清掉自动派生的关系。
    # 实测教训：修正文号提取规则后，旧的错误引用（如被 PDF 空格截断的
    # "总局公告2026 年第9 号"）仍留在库里，界面继续显示错误结论。
    # 保留人工确认（manual）与官方目录点名（official_catalog）两类关系 ——
    # 后者来自官方《失效废止目录》附件，不是本系统的推断，重建时应保留。
    conn.execute(
        "DELETE FROM policy_relation"
        " WHERE IFNULL(evidence_source,'') NOT IN ('manual','official_catalog')")
    conn.commit()

    # 阶段 0：先把官方《失效废止目录》里的点名废止关系落库（来源最权威）
    catalog_stats = apply_repeal_catalogs(conn)

    # 施行日期在未来的也要识别出来：官方时效标注缺失时，这类文件会被误判为
    # "现行有效"（实测：某管理办法 2026-11-01 才施行，而其配套公告已标"尚未生效"，
    # 两者矛盾 —— 用户若照前者办事就是适用了尚未生效的规定）
    today = date.today().isoformat()
    rows = conn.execute(
        "SELECT doc_uid, title, p_doc_no_full, o_aging, p_effective_date FROM policy"
    ).fetchall()

    by_docno: dict[str, str] = {}
    titles: list[tuple[str, str, str]] = []      # (doc_uid, 归一化标题, 文号)
    for r in rows:
        key = _normalise_doc_no(r["p_doc_no_full"])
        if key:
            by_docno[key] = r["doc_uid"]
        titles.append((r["doc_uid"], _normalise_title(r["title"]), r["p_doc_no_full"] or ""))

    # ---------------- 阶段 1：抽取（不判定）
    relation_rows: list[Relation] = []
    for r in rows:
        doc_uid = r["doc_uid"]
        own_no = _normalise_doc_no(r["p_doc_no_full"])
        own_title = _normalise_title(r["title"])
        # 法律全文不做废止声明抽取（理由见 _SKIP_REPEAL_SRC_RE）
        skip_repeal = bool(_SKIP_REPEAL_SRC_RE.match(r["title"] or ""))

        # 扫描范围 = 正文 + 附件解析文本。
        # 踩过的坑：只扫正文时引用数为 0 —— 关键引用（如"财政部税务总局公告2026年第9号"）
        # 恰恰写在 PDF 附件里；而正文里的引用多为简写，用文号正则抓不到。
        parts: list[str] = []
        body = conn.execute(
            "SELECT content, zw_content FROM policy WHERE doc_uid=?", (doc_uid,)
        ).fetchone()
        if body:
            parts += [body["content"] or "", body["zw_content"] or ""]
        for att in conn.execute(
            "SELECT parsed_text FROM attachment WHERE doc_uid=? AND parsed_text IS NOT NULL",
            (doc_uid,),
        ):
            parts.append(att["parsed_text"] or "")
        text = "\n".join(p for p in parts if p)

        # 引用形式一：书名号中的文件名（正文引用的主要形式）
        for title_ref in extract_title_refs(text):
            key = _normalise_title(title_ref)
            if not key or key == own_title:
                continue
            dst_uid, dst_no, score = _best_title_match(
                key, titles, exclude=doc_uid, threshold=max(fuzzy_threshold, 90))
            relation_rows.append(Relation(
                src_doc_uid=doc_uid, relation="cites", dst_doc_uid=dst_uid,
                dst_doc_no=dst_no or title_ref,
                evidence=(f"正文引用《{title_ref}》" if dst_uid
                          else f"正文引用《{title_ref}》（未匹配到库内文件，最高相似度 {score}）"),
                evidence_source="content",
                confidence="high" if dst_uid else "low",
            ))

        # 引用形式二：完整文号
        for cited in extract_doc_no_refs(text):
            key = _normalise_doc_no(cited)
            if key == own_no:
                continue  # 自引用不算引用
            relation_rows.append(Relation(
                src_doc_uid=doc_uid, relation="cites",
                dst_doc_uid=by_docno.get(key), dst_doc_no=cited,
                evidence=f"正文引用 {cited}", evidence_source="content", confidence="medium",
            ))

        # 废止声明（法律全文跳过，理由见 _SKIP_REPEAL_SRC_RE）
        for target in (() if skip_repeal else extract_repeal_targets(text)):
            target_no = _normalise_doc_no(target)
            dst_uid = by_docno.get(target_no)
            dst_no = target
            if dst_uid is None:
                dst_uid, matched_no, _score = _best_title_match(
                    _normalise_title(target), titles, exclude=doc_uid,
                    threshold=fuzzy_threshold, skip_statutes=True)
                if dst_uid:
                    dst_no = matched_no or target
            relation_rows.append(Relation(
                src_doc_uid=doc_uid, relation="repeals", dst_doc_no=dst_no, dst_doc_uid=dst_uid,
                evidence=f"废止声明：{target}", evidence_source="content",
                confidence="high" if dst_uid else "low",
            ))

    unique: dict[tuple, Relation] = {}
    for rel in relation_rows:
        unique.setdefault(
            (rel.src_doc_uid, rel.relation, rel.dst_doc_uid or "", rel.dst_doc_no or ""), rel)
    relation_rows = list(unique.values())

    # ---------------- 阶段 2：落库
    inserted = 0
    for rel in relation_rows:
        if _insert_relation(conn, rel):
            inserted += 1
    conn.commit()

    # ---------------- 阶段 3：判定（此时关系集已完整）
    # 官方目录点名废止的：直接按"官方"计，可靠性高于任何推断
    officially_repealed = {
        row[0] for row in conn.execute(
            "SELECT DISTINCT dst_doc_uid FROM policy_relation"
            " WHERE relation='repeals' AND evidence_source='official_catalog'"
            "   AND dst_doc_uid IS NOT NULL")
    }
    repealed_uids = {
        row[0] for row in conn.execute(
            "SELECT DISTINCT dst_doc_uid FROM policy_relation"
            " WHERE relation='repeals' AND dst_doc_uid IS NOT NULL")
    }

    judgements: list[EffectJudgement] = []
    for r in rows:
        doc_uid = r["doc_uid"]
        aging = norm_text(r["o_aging"])
        if aging in _OFFICIAL_STATES:
            judgements.append(EffectJudgement(
                doc_uid, aging, "official", f"官方时效标注：{aging}", "high",
            ))
        elif doc_uid in officially_repealed:
            judgements.append(EffectJudgement(
                doc_uid, EFFECT_REPEALED, "official",
                "被官方《失效废止目录》点名废止", "high",
                evidence="来源：国家税务总局公布的失效废止文件目录（附件）",
            ))
        elif doc_uid in repealed_uids:
            src = conn.execute(
                "SELECT s.p_doc_no_full, s.title, rel.evidence"
                " FROM policy_relation rel LEFT JOIN policy s ON s.doc_uid = rel.src_doc_uid"
                " WHERE rel.relation='repeals' AND rel.dst_doc_uid=? LIMIT 1", (doc_uid,)
            ).fetchone()
            who = ((src["p_doc_no_full"] or src["title"]) if src else None) or "另一份文件"
            judgements.append(EffectJudgement(
                doc_uid, EFFECT_REPEALED, "inferred",
                f"被《{who}》废止", "medium",
                evidence=src["evidence"] if src else None,
            ))
        elif r["p_effective_date"] and r["p_effective_date"] > today:
            judgements.append(EffectJudgement(
                doc_uid, EFFECT_PENDING, "inferred",
                f"施行日期 {r['p_effective_date']} 尚未到达，现在还不生效", "high",
            ))
        else:
            judgements.append(EffectJudgement(
                doc_uid, EFFECT_VALID, "default",
                "未发现废止证据，推定有效（非官方确认，关键结论请自行复核）",
                "medium", needs_review=False,
            ))

    # 批量写入：几千条逐条 execute + 每条触发 FTS 重建会慢到不可用
    # （实测 5090 条判定超过 2 分钟未完，故改为 executemany）
    conn.executemany(
        "UPDATE policy SET p_effect_status=?, p_effect_source=?, p_effect_reason=?,"
        " p_effect_evidence=?, p_review_state=? WHERE doc_uid=?",
        [(j.status, j.source, j.reason, j.evidence,
          "needs_review" if j.needs_review else "auto", j.doc_uid)
         for j in judgements],
    )
    conn.commit()

    # ---------------- 阶段 4：标记待人工确认
    #
    # 只标真正需要人看的，**不标"推定有效"**：
    # 库里绝大多数条目都是推定有效，全标等于没标，反而会淹没真正的问题。
    # 目前只标一类：推定废止（inferred）—— 影响最大，判错会把失效文件当有效依据用。
    #
    # **悬空引用（引用的文件不在库中）不标 needs_review**：
    # 它反映的是数据覆盖问题（可能漏抓，也可能是《增值税法》这类本就不属于本库
    # 范围的引用），与"效力判定存疑"是两码事。实测把两者混在一起时，
    # 队列立刻被"政策解读"类文件占满，真正需要看的效力问题被埋掉。
    # 悬空引用改由 dangling_citations 统计与详情页展示。
    conn.execute(
        "UPDATE policy SET p_review_state='needs_review'"
        " WHERE p_effect_source='inferred' AND p_effect_status=?", (EFFECT_REPEALED,))
    conn.commit()

    dangling = conn.execute(
        "SELECT COUNT(*) FROM policy_relation"
        " WHERE relation='cites' AND dst_doc_uid IS NULL").fetchone()[0]
    needs_review = conn.execute(
        "SELECT COUNT(*) FROM policy WHERE p_review_state='needs_review'").fetchone()[0]

    by_source: dict[str, int] = {}
    for j in judgements:
        by_source[j.source] = by_source.get(j.source, 0) + 1
    by_status: dict[str, int] = {}
    for j in judgements:
        by_status[j.status] = by_status.get(j.status, 0) + 1

    return {
        "judged": len(judgements),
        "by_source": by_source,
        "by_status": by_status,
        "relations": inserted,
        "repeal_relations": sum(1 for r in relation_rows if r.relation == "repeals"),
        "repeal_matched": sum(1 for r in relation_rows
                              if r.relation == "repeals" and r.dst_doc_uid),
        "citations": sum(1 for r in relation_rows if r.relation == "cites"),
        "dangling_citations": dangling,
        "needs_review": needs_review,
    }


# ------------------------------------------------------------------ 查询

def citations_of(conn, doc_uid: str) -> list[dict]:
    """本文引用了哪些文件（含未入库的悬空引用）。"""
    rows = conn.execute(
        "SELECT dst_doc_uid, dst_doc_no, evidence, confidence FROM policy_relation"
        " WHERE src_doc_uid=? AND relation='cites' ORDER BY id", (doc_uid,)
    ).fetchall()
    return [dict(r) for r in rows]


def repealed_by(conn, doc_uid: str) -> list[dict]:
    """本文被谁废止 —— 实务里最常问的问题。"""
    rows = conn.execute(
        "SELECT r.src_doc_uid, r.dst_doc_no, r.evidence, r.confidence,"
        " p.title, p.p_doc_no_full"
        " FROM policy_relation r LEFT JOIN policy p ON p.doc_uid = r.src_doc_uid"
        " WHERE r.relation='repeals' AND r.dst_doc_uid=? ORDER BY r.id", (doc_uid,)
    ).fetchall()
    return [dict(r) for r in rows]


def pending_review(conn, limit: int = 50) -> list[dict]:
    """待人工确认队列：系统判不准、或判定依据较弱的条目。"""
    rows = conn.execute(
        "SELECT doc_uid, cwrq, title, p_doc_no_full, p_effect_status,"
        " p_effect_source, p_effect_reason"
        " FROM policy"
        " WHERE p_review_state='needs_review'"
        "    OR (p_effect_source='inferred' AND p_effect_status=? )"
        " ORDER BY cwrq DESC LIMIT ?",
        (EFFECT_REPEALED, limit),
    ).fetchall()
    return [dict(r) for r in rows]
