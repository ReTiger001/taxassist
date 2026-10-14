"""附件仓储：``attachment`` 表的唯一 SQL 出口。

一个 doc_uid 可以挂多个附件（UNIQUE(doc_uid, url) 保证同一个 URL 只入一次）。
``parse_status`` 的生命周期：NULL（待抓）→ ok / no_text_layer / unsupported / download_failed。
"""
from __future__ import annotations

from typing import Any

from . import conn as c


def upsert(conn, *, doc_uid: str, url: str, filename: str | None, ext: str | None,
           raw_path: str | None, parsed_text: str | None, parse_status: str | None,
           parse_error: str | None, created_at: str) -> None:
    """登记一个附件。

    **已存在就不动**（``DO NOTHING``）—— 现有实现是 ``INSERT OR IGNORE``，
    语义一致：重复登记不能把已经解析好的 ``parsed_text`` 抹掉。
    """
    c.execute(
        conn,
        "INSERT INTO attachment"
        " (doc_uid, url, filename, ext, raw_path, parsed_text, parse_status, parse_error, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(doc_uid, url) DO NOTHING",
        (doc_uid, url, filename, ext, raw_path, parsed_text, parse_status, parse_error,
         created_at))


def by_doc(conn, doc_uid: str):
    """一条政策的全部附件（详情页展示用）。"""
    return c.rows(c.execute(
        conn, "SELECT * FROM attachment WHERE doc_uid=? ORDER BY id", (doc_uid,)))


def update(conn, att_id: int, values: dict) -> int:
    """按 id 更新若干列（下载成功/失败回填用）。"""
    if not values:
        return 0
    from .policy import _cols  # 复用列名校验，全套仓储只有一处白名单

    cols = _cols(values.keys())
    assignments = ",".join(f"{k}=?" for k in cols)
    params: tuple[Any, ...] = (*values.values(), int(att_id))
    cur = c.execute(conn, f"UPDATE attachment SET {assignments} WHERE id=?", params)
    return getattr(cur, "rowcount", 0) or 0


def pending(conn, *, only_failed: bool = False, limit: int = 0):
    """待抓的附件。

    ``only_failed`` 时只取上次失败的（重试路径），否则取**从未尝试**的
    （``parse_status IS NULL``）—— 两条路径分开，避免重试把新附件挤掉。
    """
    if only_failed:
        sql = "SELECT * FROM attachment WHERE parse_status IN ('download_failed','failed') ORDER BY id"
    else:
        sql = "SELECT * FROM attachment WHERE parse_status IS NULL ORDER BY id"
    params: list[Any] = []
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))
    return c.rows(c.execute(conn, sql, tuple(params)))


def status_stats(conn) -> dict[str, int]:
    """按解析状态统计（自查与进度看板用）。NULL 归到 ``pending``。"""
    rows = c.rows(c.execute(
        conn,
        "SELECT COALESCE(parse_status, 'pending') AS st, COUNT(*) AS n"
        " FROM attachment GROUP BY COALESCE(parse_status, 'pending')"))
    return {r["st"]: int(r["n"]) for r in rows}


def count_for_docs(conn, doc_uids: list[str]) -> dict[str, int]:
    """一批政策的附件数（列表页一次查完，避免 N+1）。"""
    if not doc_uids:
        return {}
    marks = ",".join("?" for _ in doc_uids)
    rows = c.rows(c.execute(
        conn,
        f"SELECT doc_uid, COUNT(*) AS n FROM attachment WHERE doc_uid IN ({marks})"
        " GROUP BY doc_uid",
        tuple(doc_uids)))
    return {r["doc_uid"]: int(r["n"]) for r in rows}


def delete_by_doc(conn, doc_uid: str) -> int:
    cur = c.execute(conn, "DELETE FROM attachment WHERE doc_uid=?", (doc_uid,))
    return getattr(cur, "rowcount", 0) or 0
