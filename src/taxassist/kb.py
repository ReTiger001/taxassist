"""AI 检索内核：只读、结构化、可溯源。

给 AI 用的这一层**必须与网页端共用同一套检索实现**（filters.py / translate.py）。
理由很直接：如果 AI 和网页对同一个问题给出不同答案，人就再也不信任何一个。
本模块只把已有检索能力包成结构化结果，不另造一套语义。

三条硬约束：

1. **只读。** 连接一律 ``mode=ro`` + ``PRAGMA query_only``。AI 一次对话会发起
   几十次调用，绝不能让它与采集 / 翻译的长任务抢写锁 —— 那条教训见
   ``writelock.py`` 与 ``db.connect`` 里的注释。
2. **可溯源。** 每条结果都带 url；效力结论必须带来源与证据片段。README 第一条
   边界写得很清楚：不允许"无出处的结论"。
3. **给下一步。** 结果里带 hint 与分页信息，让 AI 知道还能怎么查，而不是让它
   对着 10 条结果猜库有多大、能不能按税种筛。
"""
from __future__ import annotations

import logging
import re
import sqlite3
from pathlib import Path

from . import filters
from .config import DB_PATH
from .translate import to_chinese_query

log = logging.getLogger(__name__)

#: 单次检索返回上限。AI 的上下文窗口有限，一次给太多条反而稀释注意力，
#: 而且它会「看完前 50 条就当查完了」。需要更多结果时应翻页或加筛选条件。
MAX_LIMIT = 50
DEFAULT_LIMIT = 10

#: 摘要窗口：命中词前后各取多少字符
SNIPPET_BEFORE = 100
SNIPPET_AFTER = 160

#: get_policy 默认返回的正文字符数，以及一次最多允许要多少
DEFAULT_CONTENT_CHARS = 6000
MAX_CONTENT_CHARS = 40000

#: 附件正文摘要上限（附件动辄数万字，全塞进上下文没意义）
ATTACHMENT_EXCERPT_CHARS = 1500

_WS = re.compile(r"\s+")


class KBError(RuntimeError):
    """可预期的知识库错误（库不存在 / 未初始化）。上层应转成友好提示，
    而不是把 SQLite 的原始报错抛给 AI —— 那既没用又泄露实现细节。"""


# ---------------------------------------------------------------- 连接

def connect_readonly(path: str | Path | None = None) -> sqlite3.Connection:
    """只读连接。

    用 URI 的 ``mode=ro`` 而不是「连上后不写」：前者由 SQLite 在文件层拒绝
    写入，后者靠自觉。再加 ``query_only`` 作为第二道保险（挡住同一连接上的
    临时表写入等边缘情况）。
    """
    target = Path(path) if path else DB_PATH
    if not target.exists():
        raise KBError(f"数据库不存在：{target}。先运行 `python -m taxassist initdb` 并采集数据。")
    conn = sqlite3.connect(f"{target.resolve().as_uri()}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA query_only = ON")
    return conn


def _require_tables(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='policy'"
    ).fetchone()
    if row is None:
        raise KBError("库里还没有 policy 表。先运行 `python -m taxassist initdb`。")


def _check_fts(conn: sqlite3.Connection) -> bool:
    """FTS 索引是否可用。缺失时退回 LIKE 检索而不是报错 ——
    检索能力降级可以接受，查不了不行。"""
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='policy_fts'"
    ).fetchone()
    return row is not None


# ---------------------------------------------------------------- 检索

#: 检索结果用的公共列。带 content/short_content 是为了就地切摘要 ——
#: SQLite 本地读 50 条正文只有几十毫秒，比「先查元数据、再逐条取正文找关键词」
#: 少一轮往返，代码也短得多。
_HIT_COLUMNS = (
    "p.doc_uid, p.title, p.cwrq, p.pub_date, p.p_doc_no_full, p.p_doc_no_confidence,"
    " p.pub_name, p.o_column, p.p_region, p.p_effect_status, p.p_effect_source,"
    " p.p_review_state, p.url, p.o_keywords, p.o_label, p.o_tax_policy,"
    " p.short_content, p.content"
)


def _fts_phrase(term: str) -> str:
    """把词包成 FTS5 短语。词里的引号要翻倍转义，否则会变成语法错误 ——
    用户/AI 输入里出现一个英文引号就能让整次检索失败。"""
    return '"' + term.replace('"', '""') + '"'


def _build_search(query: str, tax: str, region: str, effect: str, year: str,
                  column: str, sort: str,
                  has_fts: bool) -> tuple[str, str, list, list[str], list[str], str]:
    """组装检索条件。

    返回 ``(from_where, order, params, terms, term_hits, mode)``。
    LIMIT/OFFSET 由调用方拼 —— 计数查询不需要它们，而取数查询需要，
    放在这里会让两边的 SQL 又分叉。检索语义与网页端 ``web/app.py`` 的
    /search 保持一致（那里踩过的坑这里一个都不能重犯）：

    - trigram 分词器要求短语查询，多词之间用 AND 而不是整体加引号，
      否则「增值税 优惠」会变成必须连读的短语，必然 0 条。
    - 2 字词（"契税""关税""个税"）走 FTS 会**静默返回 0 条**，必须退回 LIKE。
    - 英文查询先过术语反查（value-added tax → 增值税）。
    """
    q_cn, term_hits = to_chinese_query(query or "")
    terms = [t for t in q_cn.split() if t]

    where: list[str] = []
    params: list = []
    mode = "filter"

    if terms and all(len(t) >= 3 for t in terms) and has_fts:
        base = "FROM policy_fts f JOIN policy p ON p.id = f.rowid"
        where.append("policy_fts MATCH ?")
        params.append(" AND ".join(_fts_phrase(t) for t in terms))
        mode = "fts"
    elif terms:
        # 含短词或 FTS 不可用：退回 LIKE（列多，用 AND 连接各词）
        base = "FROM policy p"
        clauses = []
        for t in terms:
            clauses.append("(p.title LIKE ? OR IFNULL(p.content,'') LIKE ?)")
            params += [f"%{t}%", f"%{t}%"]
        where.append("(" + " AND ".join(clauses) + ")")
        mode = "like"
    else:
        base = "FROM policy p"

    if column:
        where.append("p.o_column = ?")
        params.append(column)
    if tax:
        clause, tax_params = filters.tax_filter_sql(tax, "p")
        if clause:
            where.append(clause)
            params += tax_params
    if region:
        clause, region_params = filters.region_filter_sql(region, "p")
        if clause:
            where.append(clause)
            params += region_params
    if effect:
        where.append("p.p_effect_status = ?")
        params.append(effect)
    if year and str(year).isdigit():
        where.append("p.cwrq LIKE ?")
        params.append(f"{year}-%")

    from_where = base
    if where:
        from_where += " WHERE " + " AND ".join(where)

    # 相关性表达式与**它自己的参数**。
    #
    # 必须与 WHERE 的 params 分开：计数查询不带 ORDER BY，混在一起会导致
    # 参数个数不匹配（COUNT 会多收一个参数而报错）。
    #
    # 为什么需要它：实测过缺它的后果。搜「关税」命中 2348 条（正文里顺带
    # 提一句也算命中），而当时的默认排序是「实质政策优先 + 日期倒序」——
    # 于是前三条是《广东省增值税申报试点公告》《疾病控制机构税收优惠》
    # 《12366 热点问题解答》，**标题里一个"关税"都没有**。
    # 命中两千多条时，不给相关性等于没排序。
    rank_expr = ""
    order_params: list = []
    if mode == "fts":
        # FTS5 的 bm25() 越小越相关。权重按 FTS 表的列序给
        # （title, p_doc_no_full, pub_name, content, o_keywords）：
        # 标题命中远比正文里顺带一句重要；文号是精确标识，给次高；
        # 正文权重压到 1.0，避免长文靠体量压过标题。
        rank_expr = "bm25(policy_fts, 12.0, 8.0, 4.0, 1.0, 2.0), "
    elif mode == "like":
        # LIKE 没有打分函数，退而求其次：**标题命中排在正文命中之前**。
        # 取首个词作判据即可 —— 多词时标题全中的概率低，首要词足够区分。
        if terms:
            rank_expr = "(CASE WHEN p.title LIKE ? THEN 0 ELSE 1 END), "
            order_params.append(f"%{terms[0]}%")

    if sort == "date_asc":
        order = " ORDER BY p.cwrq ASC, p.id ASC"
    elif sort == "date_desc":
        order = " ORDER BY p.cwrq DESC, p.id DESC"
    else:
        # 默认档：相关性 → 实质政策 → 日期。
        # 保留实质性作为次级键是刻意的：不这样排，解读、答记者问、新闻会
        # 挤进来（filters.classify 的三档优先级就是为此存在的）。叠加后的
        # 效果是「在相关的前提下，实质文件在前」。
        order = (f" ORDER BY {rank_expr}"
                 f"{filters.substantive_first_sql('p')}, p.cwrq DESC, p.id DESC")

    # FROM/WHERE 单独返回：计数查询与取数查询必须共用同一段条件，
    # 否则「说命中 12 条、只给 3 条」这种不一致会直接误导 AI。
    return from_where, order, params, order_params, terms, term_hits, mode


def _clip(text: str, start: int, end: int) -> str:
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return prefix + text[start:end] + suffix


def _snippet(text: str | None, terms: list[str]) -> str:
    """取命中词附近的一段正文。找不到命中词（命中在标题/文号里）时给开头一段。"""
    if not text:
        return ""
    flat = _WS.sub(" ", text).strip()
    if not flat:
        return ""
    pos = -1
    for t in terms:
        if not t:
            continue
        i = flat.find(t)
        if i >= 0 and (pos < 0 or i < pos):
            pos = i
    if pos < 0:
        return _clip(flat, 0, SNIPPET_BEFORE + SNIPPET_AFTER)
    return _clip(flat, max(0, pos - SNIPPET_BEFORE), pos + SNIPPET_AFTER)


def _hit(row: sqlite3.Row, terms: list[str]) -> dict:
    return {
        "doc_uid": row["doc_uid"],
        "title": row["title"],
        "doc_no": row["p_doc_no_full"],
        "doc_no_confidence": row["p_doc_no_confidence"],
        "effect_status": row["p_effect_status"] or "未知",
        "effect_source": row["p_effect_source"] or "none",
        "needs_review": (row["p_review_state"] or "") == "needs_review",
        "pub_name": row["pub_name"],
        "region": row["p_region"],
        "column": row["o_column"],
        "cwrq": row["cwrq"],
        "pub_date": row["pub_date"],
        "tax_types": filters.extract_tax_types(row),
        "url": row["url"],
        "snippet": _snippet(row["short_content"] or row["content"], terms),
    }


_NO_HIT_HINT = (
    "没有命中。可以：① 换用更短的词或同义词（如「小微企业」或「小型微利企业」）；"
    "② 去掉部分关键词（多词之间是 AND 关系，词越多越窄）；"
    "③ 用 kb_overview 查看库里的税种、地区、年份、栏目的可选值；"
    "④ 如果是查文号，改用 lookup_by_doc_no。"
)


def search(query: str = "", *, tax: str = "", region: str = "", effect: str = "",
           year: str = "", column: str = "", sort: str = "relevance",
           limit: int = DEFAULT_LIMIT, offset: int = 0,
           path: str | Path | None = None) -> dict:
    """全文检索政策。空查询 + 筛选条件 = 按条件浏览（这是真实用法，要允许）。"""
    limit = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))
    offset = max(0, int(offset or 0))

    result: dict = {
        "query": query or "",
        "filters": {k: v for k, v in (("tax", tax), ("region", region),
                                      ("effect", effect), ("year", year),
                                      ("column", column)) if v},
        "sort": sort,
        "offset": offset,
        "limit": limit,
        "hits": [],
        "total_matched": 0,
        "has_more": False,
        "error": None,
    }

    conn = connect_readonly(path)
    try:
        _require_tables(conn)
        has_fts = _check_fts(conn)
        try:
            (from_where, order, params, order_params, terms, term_hits,
             mode) = _build_search(query, tax, region, effect, year, column,
                                   sort, has_fts)
        except Exception as e:  # noqa: BLE001
            log.warning("检索参数组装失败 q=%r: %s", query, e)
            result["error"] = "检索失败，请调整关键词后重试。"
            return result

        result["query_used"] = " ".join(terms) if terms else ""
        result["rewritten_terms"] = term_hits
        result["mode"] = mode

        # 总数：让 AI 知道「命中 3 条」和「命中 300 条」是两种截然不同的处境。
        # 计数与取数共用同一段 FROM/WHERE（_build_search 的返回值），
        # 这样「说命中 12 条、只给 3 条」之类的不一致在结构上就不可能发生。
        try:
            result["total_matched"] = conn.execute(
                f"SELECT COUNT(*) c {from_where}", tuple(params)).fetchone()["c"]
            # 参数按 SQL 里 ? 的出现顺序绑定：WHERE 的 → ORDER BY 的 → LIMIT/OFFSET。
            # 相关性表达式可能要一个参数（LIKE 模式用来判断标题是否命中），
            # 所以不能只用 params。计数查询不带 ORDER BY，故那边仍旧只用 params。
            rows = conn.execute(
                f"SELECT {_HIT_COLUMNS} {from_where}{order} LIMIT ? OFFSET ?",
                tuple(params + order_params + [limit, offset])).fetchall()
        except sqlite3.OperationalError as e:
            # FTS5 的语法错误消息会把表名、列名等实现细节带出来，细节写日志即可
            log.warning("检索失败 q=%r: %s", query, e)
            result["error"] = "检索失败，请调整关键词后重试。"
            return result

        result["hits"] = [_hit(r, terms) for r in rows]
        result["returned"] = len(result["hits"])
        result["has_more"] = offset + len(result["hits"]) < result["total_matched"]
        if not result["hits"]:
            result["hint"] = _NO_HIT_HINT
        elif result["has_more"]:
            result["hint"] = (f"共命中 {result['total_matched']} 条，本次返回 {len(result['hits'])} 条。"
                              f"可用 offset={offset + len(result['hits'])} 继续取，或加筛选条件收窄。")
        return result
    finally:
        conn.close()


def lookup_by_doc_no(doc_no: str, *, limit: int = 10,
                     path: str | Path | None = None) -> dict:
    """按文号精确查找（税务实务里最高频的一类查询）。

    文号在真实使用中有多种写法：``财税〔2023〕25号`` / ``财税[2023]25号`` /
    ``财税（2023）25号`` / 带空格。用户的输入不会总跟库里一致，因此两侧都做
    规范化（统一括号、去空白）再比对。

    没入库但被别的文件引用过的文号也要能查到 —— ``policy_relation.dst_doc_no``
    里存着这类"悬空引用"，直接说"没这条政策"是错的。
    """
    limit = max(1, min(int(limit or 10), MAX_LIMIT))
    result: dict = {
        "doc_no": doc_no or "",
        "normalized": _normalize_doc_no(doc_no),
        "matches": [],
        "dangling_references": [],
        "error": None,
    }
    if not _normalize_doc_no(doc_no):
        result["error"] = "文号为空。"
        return result

    conn = connect_readonly(path)
    try:
        _require_tables(conn)
        norm = _normalize_doc_no(doc_no)
        # 规范化放在 SQL 侧做：库里的写法本身就不统一，Python 侧只能规范化
        # 输入，管不了存量数据。REPLACE 链虽丑，但全表扫一遍是毫秒级。
        p_norm = _sql_normalized("COALESCE(p.p_doc_no_full, '')")
        sql = (
            f"SELECT {_HIT_COLUMNS} FROM policy p"
            f" WHERE {p_norm} = ? OR {p_norm} LIKE ?"
            f" ORDER BY {filters.substantive_first_sql('p')}, p.cwrq DESC LIMIT ?"
        )
        rows = conn.execute(sql, (norm, f"%{norm}%", limit)).fetchall()
        result["matches"] = [_hit(r, []) for r in rows]
        result["found_in_library"] = bool(result["matches"])

        rel_norm = _sql_normalized("COALESCE(r.dst_doc_no, '')")
        dangling = conn.execute(
            "SELECT DISTINCT r.dst_doc_no, r.src_doc_uid, r.relation, r.evidence,"
            " s.title AS src_title FROM policy_relation r"
            " LEFT JOIN policy s ON s.doc_uid = r.src_doc_uid"
            f" WHERE {rel_norm} = ? OR {rel_norm} LIKE ?"
            " LIMIT ?", (norm, f"%{norm}%", limit)).fetchall()
        result["dangling_references"] = [dict(r) for r in dangling]

        if not result["matches"] and not result["dangling_references"]:
            result["hint"] = ("库里没有这个文号。注意：只有已入库的政策才能查到，"
                              "也可能是文号写法不同 —— 可先用 search 按标题关键词找。")
        elif result["matches"]:
            best = result["matches"][0]
            result["effect_summary"] = (
                f"{best['doc_no'] or '(无文号)'} 当前效力：{best['effect_status']}"
                f"（依据：{best['effect_source']}"
                f"{'，待人工确认' if best['needs_review'] else ''}）。"
                "引用前请用 get_policy 核对证据片段。"
            )
        return result
    finally:
        conn.close()


#: 文号里的括号一律统一成方括号。
#:
#: **四种括号必须映射到同一个字符**，而不是各自映射成自己的半角形式 ——
#: 后者看着更"规范"，实际会漏：用户写「财税（2023）25号」，库里是
#: 「财税〔2023〕25号」，一个归成 `(2023)` 一个归成 `[2023]`，永远碰不上
#: （写测试时正是这么挂的）。归一到一个目标，任意写法都能对齐。
_DOC_NO_PUNCT: tuple[tuple[str, str], ...] = (
    ("〔", "["), ("〕", "]"),
    ("（", "["), ("）", "]"),
    ("【", "["), ("】", "]"),
    ("(", "["), (")", "]"),
)


def _sql_normalized(expr: str) -> str:
    """生成 SQL 侧的文号规范化表达式，与 ``_normalize_doc_no`` 逐字符对齐。

    两侧都做同一套替换（四种括号统一成方括号、去掉半角与全角空白），才能让
    「财税〔2023〕25号」和「财税[2023]25 号」互相匹配。写成函数而不是常量，
    是因为同一个表达式要用在多处（本表匹配、关系表悬空引用匹配），
    复制粘贴多份迟早会改漏一处。
    """
    out = expr
    for old, new in _DOC_NO_PUNCT:
        out = f"REPLACE({out},'{old}','{new}')"
    for blank in (" ", "\u3000"):
        out = f"REPLACE({out},'{blank}','')"
    return out


def _normalize_doc_no(value: str | None) -> str:
    if not value:
        return ""
    out = value.strip()
    for old, new in _DOC_NO_PUNCT:
        out = out.replace(old, new)
    return _WS.sub("", out)


# ---------------------------------------------------------------- 详情

def get_policy(doc_uid: str, *, content_offset: int = 0,
               max_chars: int = DEFAULT_CONTENT_CHARS,
               path: str | Path | None = None) -> dict | None:
    """取一条政策的完整档案：正文（可翻页）+ 效力依据 + 关系 + 附件。

    正文按段返回而不是一次性全给：政策正文常见数万字，一次塞进去会挤掉
    AI 的其余上下文，而它多数时候只需要"相关的那几段"。
    """
    max_chars = max(500, min(int(max_chars or DEFAULT_CONTENT_CHARS), MAX_CONTENT_CHARS))
    content_offset = max(0, int(content_offset or 0))

    conn = connect_readonly(path)
    try:
        _require_tables(conn)
        row = conn.execute("SELECT * FROM policy WHERE doc_uid = ?", (doc_uid,)).fetchone()
        if row is None:
            return None

        full = (row["content"] or row["zw_content"] or "").strip()
        total = len(full)
        chunk = full[content_offset:content_offset + max_chars]

        citations = conn.execute(
            "SELECT r.dst_doc_uid, r.dst_doc_no, r.evidence, r.evidence_source,"
            " r.confidence, p.title AS target_title, p.cwrq AS target_cwrq,"
            " p.p_doc_no_full AS target_doc_no, p.p_effect_status AS target_effect"
            " FROM policy_relation r LEFT JOIN policy p ON p.doc_uid = r.dst_doc_uid"
            " WHERE r.src_doc_uid = ? AND r.relation = 'cites' ORDER BY r.id",
            (doc_uid,)).fetchall()
        repealed_by = conn.execute(
            "SELECT r.src_doc_uid, r.dst_doc_no, r.evidence, r.confidence,"
            " s.title AS src_title, s.p_doc_no_full AS src_doc_no_full, s.cwrq AS src_cwrq,"
            " s.url AS src_url"
            " FROM policy_relation r LEFT JOIN policy s ON s.doc_uid = r.src_doc_uid"
            " WHERE r.relation = 'repeals' AND r.dst_doc_uid = ? ORDER BY r.id",
            (doc_uid,)).fetchall()
        repeals = conn.execute(
            "SELECT r.dst_doc_uid, r.dst_doc_no, r.evidence, r.confidence,"
            " p.title AS target_title, p.p_doc_no_full AS target_doc_no,"
            " p.p_effect_status AS target_effect"
            " FROM policy_relation r LEFT JOIN policy p ON p.doc_uid = r.dst_doc_uid"
            " WHERE r.src_doc_uid = ? AND r.relation = 'repeals' ORDER BY r.id",
            (doc_uid,)).fetchall()
        attachments = conn.execute(
            "SELECT filename, ext, url, parse_status,"
            " SUBSTR(COALESCE(parsed_text,''), 1, ?) AS text_excerpt,"
            " LENGTH(COALESCE(parsed_text,'')) AS text_len"
            " FROM attachment WHERE doc_uid = ? ORDER BY id",
            (ATTACHMENT_EXCERPT_CHARS, doc_uid)).fetchall()

        return {
            "doc_uid": row["doc_uid"],
            "title": row["title"],
            "second_title": row["second_title"],
            "doc_no": row["p_doc_no_full"],
            "doc_no_confidence": row["p_doc_no_confidence"],
            "pub_name": row["pub_name"],
            "region": row["p_region"],
            "column": row["o_column"],
            "label": row["o_label"],
            "cwrq": row["cwrq"],
            "pub_date": row["pub_date"],
            "effective_date": row["p_effective_date"],
            "url": row["url"],
            "snapshot_url": row["snapshot_url"],
            "effect": {
                "status": row["p_effect_status"] or "未知",
                "source": row["p_effect_source"] or "none",
                "reason": row["p_effect_reason"],
                "evidence": row["p_effect_evidence"],
                "needs_review": (row["p_review_state"] or "") == "needs_review",
                "official_aging": row["o_aging"],
                "official_abolish_date": row["o_abolish_date"],
            },
            "content": chunk,
            "content_offset": content_offset,
            "content_length": total,
            "content_truncated": content_offset + len(chunk) < total,
            "next_offset": (content_offset + len(chunk)) if content_offset + len(chunk) < total else None,
            "citations": [dict(r) for r in citations],
            "repealed_by": [dict(r) for r in repealed_by],
            "repeals": [dict(r) for r in repeals],
            "attachments": [dict(r) for r in attachments],
        }
    finally:
        conn.close()


# ---------------------------------------------------------------- 库概况

def overview(path: str | Path | None = None) -> dict:
    """库概况：告诉 AI 这个库里有什么、能按什么筛、数据到哪天为止。

    「数据截止到什么时候」必须显式给出 —— 政策是有时效的，AI 不知道库里
    最新到哪天，就会把三年前的数据当今天的现状用。
    """
    conn = connect_readonly(path)
    try:
        _require_tables(conn)

        total = conn.execute("SELECT COUNT(*) c FROM policy").fetchone()["c"]
        by_effect = {
            (r["p_effect_status"] or "未知"): r["c"] for r in conn.execute(
                "SELECT p_effect_status, COUNT(*) c FROM policy GROUP BY 1 ORDER BY c DESC")
        }
        by_source = {
            (r["p_effect_source"] or "none"): r["c"] for r in conn.execute(
                "SELECT p_effect_source, COUNT(*) c FROM policy GROUP BY 1 ORDER BY c DESC")
        }
        by_column = {
            r["k"]: r["c"] for r in conn.execute(
                "SELECT COALESCE(o_column,'(空)') k, COUNT(*) c FROM policy"
                " GROUP BY 1 ORDER BY c DESC")
        }
        needs_review = conn.execute(
            "SELECT COUNT(*) c FROM policy WHERE p_review_state = 'needs_review'"
        ).fetchone()["c"]

        # 日期范围与新鲜度：cwrq 是最常用的时间维度（年份筛选用它）
        date_row = conn.execute(
            "SELECT MIN(cwrq) lo, MAX(cwrq) hi FROM policy"
            " WHERE cwrq IS NOT NULL AND LENGTH(cwrq) >= 4").fetchone()
        years = [
            r["y"] for r in conn.execute(
                "SELECT DISTINCT SUBSTR(cwrq,1,4) y FROM policy"
                " WHERE cwrq IS NOT NULL AND LENGTH(cwrq) >= 4 ORDER BY y DESC")
            if r["y"] and str(r["y"]).isdigit()
        ]
        last_fetch = conn.execute(
            "SELECT source_id, mode, finished_at, window_end, status FROM fetch_log"
            " WHERE status = 'ok' ORDER BY finished_at DESC LIMIT 1").fetchone()
        latest_seen = conn.execute(
            "SELECT MAX(last_seen_at) m FROM policy").fetchone()["m"]

        result = {
            "total_policies": total,
            "by_effect_status": by_effect,
            "effect_source_legend": {
                "official": "官方标注（站点自己写的时效）",
                "inferred": "本系统依据废止公告/正文推断",
                "default": "推定有效（无任何依据，仅因未发现废止）",
                "manual": "人工确认",
                "none": "尚未判定",
            },
            "by_effect_source": by_source,
            "by_column": by_column,
            "needs_manual_review": needs_review,
            "tax_types": filters.tax_type_counts(conn),
            "regions": {r: n for r, n in filters.region_counts(conn)},
            "years": years,
            "date_range": {"earliest_cwrq": date_row["lo"] if date_row else None,
                           "latest_cwrq": date_row["hi"] if date_row else None},
            "data_freshness": {
                "last_successful_fetch": last_fetch["finished_at"] if last_fetch else None,
                "last_fetch_source": last_fetch["source_id"] if last_fetch else None,
                "last_seen_update": latest_seen,
            },
            "usage_note": (
                "检索结果须附 url 与文号引用；效力为「未知」或标记 needs_review 的，"
                "必须提示需人工核对，不得直接下结论。"
            ),
        }
        if needs_review:
            result["freshness_warning"] = (
                f"有 {needs_review} 条政策的效力待人工确认，引用这些条目时务必说明不确定性。"
            )
        return result
    finally:
        conn.close()
