"""从已有正文离线补全结构化字段（不联网）。

======================================================================
为什么需要这个模块
======================================================================

全量抓取的 5019 条政策**自带正文** —— 政策法规库列表接口的 ``content``
字段对历史文件并非为空（实测 1985 年的批复、2005 年的通知都有完整正文）。
这是我在做生产化时才发现的事实，它改变了 enrich 的定位：

- **正文、施行日期、完整文号** → 能从库里已有数据算出来，不必再抓详情页；
- **附件链接、官方时效标注、关联解读** → 只有详情页才有，仍须 enrich。

原本打算为"补正文"跑两小时的 5000 次网络请求，因此缩减为不需要 ——
这对政府网站和我们自己的时间都是好事。

======================================================================
一致性要求
======================================================================

施行日期的提取规则必须与 ``collect/detail.py`` 保持一致：
同一份文件经"详情页抓取"和"离线补全"两条路径得到的结果应当相同，
否则同一字段会有两个来源、两个值，事后无法判断哪个可信。
"""
from __future__ import annotations

import logging
import re

from .collect.detail import _EFFECTIVE_RE   # 施行日期正则的唯一定义，见模块头「一致性要求」
from .collect.normalize import extract_full_doc_no, norm_text

log = logging.getLogger(__name__)

# 施行日期正则**不再本地定义**，改为从 collect/detail.py 导入（见上方 import）。
# 原先这里抄了一份、注释写着"同构"，实际早已分叉：这一份只认 20xx、必须
# "自…日起"、后缀只认施行/执行/实施；而 detail.py 那份认 19xx、前缀可选、
# 后缀含生效/适用 —— 且 detail.py 的注释记着"旧写法漏掉两成以上"。
# 于是模块头明明写着"两条路径的结果应当相同"，实际却长期用着更弱的版本，
# 同一字段两个来源两个值 —— 正是这段文档要防的情况。
# 抄一份再各自演进，是这类分叉的典型成因：注释说的是一致，代码不是。


def extract_effective_date(text: str | None) -> str | None:
    """从正文提取施行日期，如"本公告自2026年11月1日起施行" → 2026-11-01。"""
    t = norm_text(text)
    if not t:
        return None
    m = _EFFECTIVE_RE.search(t)
    if not m:
        return None
    year, month, day = (int(x) for x in m.groups())
    try:
        return f"{year:04d}-{month:02d}-{day:02d}"
    except ValueError:
        return None


def backfill_from_content(conn, *, limit: int | None = None) -> dict:
    """从已有正文补全缺失的 p_effective_date 与 p_doc_no_full。

    只填 NULL 值，**不覆盖已有值** —— 已有值可能来自详情页（更权威）
    或人工修正，自动逻辑不应把它们改掉。
    """
    sql = (
        "SELECT doc_uid, content, p_doc_no_full, p_effective_date"
        " FROM policy"
        " WHERE content IS NOT NULL AND content <> ''"
        "   AND (p_effective_date IS NULL OR p_doc_no_full IS NULL)"
    )
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = conn.execute(sql).fetchall()

    eff_updates: list[tuple[str, str]] = []
    no_updates: list[tuple[str, str, str]] = []
    for r in rows:
        if not r["p_effective_date"]:
            found = extract_effective_date(r["content"])
            if found:
                eff_updates.append((found, r["doc_uid"]))
        if not r["p_doc_no_full"]:
            doc_no = extract_full_doc_no(r["content"])
            # 长度上限：实测从正文提取到过
            # "年度企业所得税优惠政策和财关税〔2021〕4号" 这种跨句拼接的垃圾。
            # 真实文号不会超过 30 字，超过的一律不要（宁可留空也不要错的）。
            if doc_no and len(doc_no) <= 30:
                no_updates.append((doc_no, "high", r["doc_uid"]))

    # 批量写入：p_doc_no_full 在 FTS 索引里，逐条 execute 会逐条重建索引
    if eff_updates:
        conn.executemany(
            "UPDATE policy SET p_effective_date=? WHERE doc_uid=?", eff_updates)
    if no_updates:
        conn.executemany(
            "UPDATE policy SET p_doc_no_full=?, p_doc_no_confidence=? WHERE doc_uid=?",
            no_updates)
    conn.commit()

    stats = {
        "scanned": len(rows),
        "effective_date_filled": len(eff_updates),
        "doc_no_filled": len(no_updates),
    }
    log.info("离线补全完成 %s", stats)
    return stats


def recheck_doc_no(conn, *, limit: int | None = None) -> dict:
    """复核文号：① 清掉**年份不自洽**的；② 裁掉**被正文污染**的前缀。

    **① 年份不自洽（新增）**：文号里的年份比本文成文日期晚 5 年以上 ——
    那不可能是本文的文号。实测 524 条 1980–1990 年代的老政策挂上了
    "国家税务总局公告2022年第14号"这类**废止目录公告**的号（页面上的
    「注释」块写着"本文全文废止"，其中的他文文号被当成了本文文号）。
    这种情况直接清空：留空显示「—」只是缺信息，挂个格式正确、抄走就错的
    文号则会误导引用。

    **② 正文污染**：``backfill_from_content`` 明文规定只填 NULL、不覆盖已有值
    （因为已有值可能来自详情页或人工修正）。但历史上贪心匹配把正文句子写进了
    这个字段 —— "考虑在粤人社规〔2018〕15号" —— 这类值**非空**，于是永远不会
    被重算，错的会一直错下去。裁前缀而不是重新提取：重新提取会换成别的文件的
    文号，那比前缀污染更糟。
    """
    from .collect.normalize import looks_contaminated, repair_doc_no_prefix

    sql = ("SELECT doc_uid, p_doc_no_full, cwrq FROM policy"
           " WHERE p_doc_no_full IS NOT NULL AND p_doc_no_full <> ''")
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = conn.execute(sql).fetchall()

    updates: list[tuple[str, str]] = []
    clears: list[tuple[str, str]] = []
    samples: list[tuple[str, str]] = []
    contaminated = 0
    for r in rows:
        old = r["p_doc_no_full"]
        # ① 年份不自洽 → 清空
        if _doc_no_year_gap(r["cwrq"], old) >= 5:
            clears.append((r["doc_uid"], old))
            continue
        # ② 正文污染 → 裁前缀
        if not looks_contaminated(old):
            continue
        contaminated += 1
        fixed = repair_doc_no_prefix(old)
        if not fixed or fixed == old:
            continue
        updates.append((fixed, r["doc_uid"]))
        samples.append((old, fixed))

    if updates:
        conn.executemany("UPDATE policy SET p_doc_no_full=? WHERE doc_uid=?", updates)
    if clears:
        conn.executemany("UPDATE policy SET p_doc_no_full=NULL WHERE doc_uid=?",
                         [(u,) for u, _ in clears])
    if updates or clears:
        conn.commit()
    return {"scanned": len(rows), "contaminated": contaminated,
            "doc_no_repaired": len(updates), "doc_no_cleared": len(clears),
            "clear_samples": [o for _, o in clears[:10]], "samples": samples[:20]}


_DOCNO_DIGIT = re.compile(r"\d")


def _doc_no_year_gap(cwrq: str | None, doc_no: str | None) -> int:
    """文号里的年份比成文日期晚多少年。无法判断时返回 0（= 不做处理）。

    只在"文号比政策还新很多"时才有意义：政策不可能引用一份未来的文件，
    所以文号年份大于成文年份、且差距很大，基本可以断定这个文号不是本文的。
    年份解析走 normalize.doc_no_year（只看结构化位置，不看裸 4 位数）。
    """
    if not cwrq or not doc_no:
        return 0
    try:
        pub = int(str(cwrq)[:4])
    except (TypeError, ValueError):
        return 0
    from .collect.normalize import doc_no_year

    dy = doc_no_year(doc_no)
    return (dy - pub) if dy else 0


def looks_like_doc_no(value: str | None) -> bool:
    """判断一个字符串是否像完整文号。

    用来决定官方字段 ``o_doc_num`` 能不能拿来用。**宁可判为不可用** ——
    拿一个不像文号的东西去覆盖，比留空危险：错的文号看起来像真的。
    """
    v = (value or "").strip()
    if len(v) < 4 or v.isdigit():
        return False
    return "号" in v and bool(_DOCNO_DIGIT.search(v))


def apply_official_doc_no(conn, *, dry_run: bool = False) -> dict:
    """用官方列表接口的 ``o_doc_num`` 覆盖我们自己拼的文号。

    ================================================================
    这修的是一次真实事故
    ================================================================

    我们曾按「**发文机关** + 年份 + 序号」拼文号，拼出
    「国务院1984年第161号」—— 中文文号里根本没有这种形式。
    而同一行的 ``o_doc_num`` 就是官方组装好的真文号「国发〔1984〕161号」，
    用的是官方文种简称（``o_doc_type``，如「国发」「财税油政字」）。

    更严重的是曾被标为 ``high``（自称"从原文提取"）的那批：抽样发现它们抓的
    是**页面上引用的别的文件**的文号 —— 标题是 1987 年的文件，
    文号却抓成「国家税务总局公告2011年第2号」，年份都对不上。

    规则：
      - 官方字段可用   -> 用它，confidence 记为 ``official``
      - 官方字段不可用 -> **如实清空**（``none``），绝不保留我们拼出来的那个

    ``dry_run=True`` 时只统计、不写库。
    """
    rows = conn.execute(
        "SELECT id, p_doc_no_full, o_doc_num FROM policy").fetchall()
    stats = {"scanned": len(rows), "replaced": 0, "cleared": 0, "unchanged": 0}
    updates: list[tuple] = []

    for r in rows:
        official = (r["o_doc_num"] or "").strip()
        ours = (r["p_doc_no_full"] or "").strip()
        if looks_like_doc_no(official):
            if official == ours:
                stats["unchanged"] += 1
                continue
            stats["replaced"] += 1
            updates.append((official, "official", r["id"]))
        elif ours:
            stats["cleared"] += 1
            updates.append((None, "none", r["id"]))
        else:
            stats["unchanged"] += 1

    if not dry_run and updates:
        conn.executemany(
            "UPDATE policy SET p_doc_no_full=?, p_doc_no_confidence=?"
            " WHERE id=?", updates)
        conn.commit()
    stats["dry_run"] = dry_run
    log.info("文号按官方字段校正：%s", stats)
    return stats


def coverage(conn) -> dict:
    """当前各字段的填充率（用于生产验收）。"""
    total = conn.execute("SELECT COUNT(*) FROM policy").fetchone()[0]

    def filled(col: str) -> int:
        return conn.execute(
            f"SELECT COUNT(*) FROM policy WHERE {col} IS NOT NULL AND {col} <> ''"
        ).fetchone()[0]

    def ratio(n: int) -> str:
        return f"{n}/{total} ({n * 100 // max(total, 1)}%)"

    return {
        "total": total,
        "content": ratio(filled("content")),
        "doc_no": ratio(filled("p_doc_no_full")),
        "effective_date": ratio(filled("p_effective_date")),
        "aging_official": ratio(filled("o_aging")),
        "detail_fetched": ratio(filled("p_detail_fetched_at")),
        "region": ratio(filled("p_region")),
    }
