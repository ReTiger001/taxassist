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

from .collect.normalize import extract_full_doc_no, norm_text

log = logging.getLogger(__name__)

# 与 collect/detail.py 的 _EFFECTIVE_RE 同构（见模块头部的"一致性要求"）
_EFFECTIVE_RE = re.compile(
    r"自\s*(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日起(?:施行|执行|实施)"
)


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
    """修补**被正文污染**的文号：只裁前缀，保留原主体。

    为什么需要单独一个函数：``backfill_from_content`` 明文规定只填 NULL、不覆盖
    已有值（因为已有值可能来自详情页或人工修正）。但历史上贪心匹配把正文句子
    写进了这个字段 —— "考虑在粤人社规〔2018〕15号" —— 这类值**非空**，于是永远
    不会被重算，错的会一直错下去。

    为什么是裁前缀而不是重新提取：见 ``repair_doc_no_prefix`` 的说明 ——
    重新提取会换成别的文件的文号，那比前缀污染更糟。
    """
    from .collect.normalize import looks_contaminated, repair_doc_no_prefix

    sql = ("SELECT doc_uid, p_doc_no_full FROM policy"
           " WHERE p_doc_no_full IS NOT NULL AND p_doc_no_full <> ''")
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = conn.execute(sql).fetchall()

    updates: list[tuple[str, str]] = []
    samples: list[tuple[str, str]] = []
    contaminated = 0
    for r in rows:
        old = r["p_doc_no_full"]
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
        conn.commit()
    return {"scanned": len(rows), "contaminated": contaminated,
            "doc_no_repaired": len(updates), "samples": samples[:20]}


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
