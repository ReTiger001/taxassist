"""政策关系仓储：``policy_relation`` 表的唯一 SQL 出口。

关系类型（cites / repeals / amends / supersedes）来自效力判定（effect.py）与
官方目录导入，`evidence_source` 记录判据来源。

**注意 SQLite 与 PG 的一个语义差异已被规避**：SQLite 里 NULL 彼此不相等，
所以 ``UNIQUE(src, dst_uid, dst_no, relation)`` 遇到 dst_uid/dst_no 为 NULL 时
不会判冲突 —— 现有 effect.py 因此手写「先 SELECT 查重再 INSERT」而不用 ON CONFLICT。
这里沿用同样的做法，两边行为一致。
"""
from __future__ import annotations

from typing import Any

from . import conn as c


def exists(conn, *, src_doc_uid: str, dst_doc_uid: str | None, dst_doc_no: str | None,
           relation: str) -> bool:
    """是否已有同一条关系（dst 为空时按 IS NULL 比较，避免 NULL != NULL）。"""
    conds = ["src_doc_uid=?", "relation=?"]
    params: list[Any] = [src_doc_uid, relation]
    for name, val in (("dst_doc_uid", dst_doc_uid), ("dst_doc_no", dst_doc_no)):
        if val is None:
            conds.append(f"{name} IS NULL")
        else:
            conds.append(f"{name}=?")
            params.append(val)
    row = c.one(c.execute(
        conn, f"SELECT 1 AS x FROM policy_relation WHERE {' AND '.join(conds)}", tuple(params)))
    return row is not None


def add(conn, *, src_doc_uid: str, dst_doc_uid: str | None, dst_doc_no: str | None,
        relation: str, evidence: str | None, evidence_source: str | None,
        confidence: float | None, created_at: str) -> None:
    """写一条关系（调用方先用 :func:`exists` 查重）。"""
    c.execute(
        conn,
        "INSERT INTO policy_relation"
        " (src_doc_uid, dst_doc_uid, dst_doc_no, relation, evidence, evidence_source,"
        "  confidence, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (src_doc_uid, dst_doc_uid, dst_doc_no, relation, evidence, evidence_source,
         confidence, created_at))


def by_src(conn, doc_uid: str):
    """这条政策**指向**谁（它引用了谁 / 废止了谁）。"""
    return c.rows(c.execute(
        conn, "SELECT * FROM policy_relation WHERE src_doc_uid=? ORDER BY relation", (doc_uid,)))


def by_dst(conn, doc_uid: str):
    """谁**指向**这条政策（被谁引用 / 被谁废止）。"""
    return c.rows(c.execute(
        conn, "SELECT * FROM policy_relation WHERE dst_doc_uid=? ORDER BY relation", (doc_uid,)))


def delete_auto(conn, src_doc_uid: str, keep_sources: tuple[str, ...] = ("manual", "official_catalog")) -> int:
    """清掉某条政策的**自动推断**关系，保留人工与官方目录来源的。

    效力判定每次重跑前调用 —— 推断结果要能重算，但人工标注不能被覆盖。
    """
    marks = ",".join("?" for _ in keep_sources)
    params: tuple[Any, ...] = (src_doc_uid, *keep_sources)
    if keep_sources:
        cur = c.execute(
            conn,
            "DELETE FROM policy_relation WHERE src_doc_uid=?"
            f" AND (evidence_source IS NULL OR evidence_source NOT IN ({marks}))",
            params)
    else:
        cur = c.execute(conn, "DELETE FROM policy_relation WHERE src_doc_uid=?", (src_doc_uid,))
    return getattr(cur, "rowcount", 0) or 0


def count(conn, relation: str | None = None) -> int:
    if relation is None:
        return int(c.scalar(c.execute(conn, "SELECT COUNT(*) AS n FROM policy_relation"), 0))
    return int(c.scalar(c.execute(
        conn, "SELECT COUNT(*) AS n FROM policy_relation WHERE relation=?", (relation,)), 0))
