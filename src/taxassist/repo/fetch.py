"""抓取仓储：``fetch_log`` 与 ``raw_snapshot`` 的唯一 SQL 出口。

两条不可妥协的规则（沿用 store.py 的口径）：

1. **完整性可验证**：fetch_log 同时记 ``reported_total`` 与 ``fetched_count``，
   两者不等即 ``incomplete`` —— 宁可报错，也不让"漏抓"看起来像"没政策"。
2. **归档原始响应**：抓取当时官方返回了什么就存什么；政策网站改版、
   文件被撤下时，这是唯一能证明"当时依据是什么"的证据。

``raw_snapshot`` 的 UNIQUE(doc_uid, kind, content_hash) 让同一份内容重复落盘时
自动跳过（``DO NOTHING``，原实现是 ``INSERT OR IGNORE``）。
"""
from __future__ import annotations

from typing import Any

from . import conn as c

# ------------------------------------------------------------------ fetch_log


def log_start(conn, *, source_id: str, mode: str, started_at: str,
              window_start: str | None, window_end: str | None) -> int:
    """开一条抓取记录，返回自增 id（收尾时要用）。

    用 ``RETURNING id`` 取回主键 —— SQLite 3.35+ 与 PG 都支持，
    不必依赖 ``cursor.lastrowid``（PG 侧没有这个属性）。
    """
    row = c.one(c.execute(
        conn,
        "INSERT INTO fetch_log (source_id, mode, started_at, window_start, window_end, status)"
        " VALUES (?,?,?,?,?,?) RETURNING id",
        (source_id, mode, started_at, window_start, window_end, "running")))
    return int(row["id"])


def log_finish(conn, log_id: int, *, status: str, finished_at: str,
               values: dict | None = None) -> int:
    """收尾一条抓取记录：写状态、结束时间与各项计数。"""
    payload: dict[str, Any] = {"status": status, "finished_at": finished_at}
    if values:
        payload.update(values)
    from .policy import _cols

    cols = _cols(payload.keys())
    assignments = ",".join(f"{k}=?" for k in cols)
    cur = c.execute(conn, f"UPDATE fetch_log SET {assignments} WHERE id=?",
                    (*payload.values(), int(log_id)))
    return getattr(cur, "rowcount", 0) or 0


def recent(conn, source_id: str | None = None, limit: int = 20):
    """最近的抓取记录（健康检查与日报用）。最新的在最前。"""
    if source_id is None:
        return c.rows(c.execute(
            conn, "SELECT * FROM fetch_log ORDER BY started_at DESC LIMIT ?", (int(limit),)))
    return c.rows(c.execute(
        conn, "SELECT * FROM fetch_log WHERE source_id=? ORDER BY started_at DESC LIMIT ?",
        (source_id, int(limit))))


def incomplete(conn, limit: int = 20):
    """reported_total 与 fetched_count 不符的记录 —— 漏抓的证词。"""
    return c.rows(c.execute(
        conn,
        "SELECT * FROM fetch_log"
        " WHERE reported_total IS NOT NULL AND fetched_count IS NOT NULL"
        "   AND reported_total > fetched_count"
        " ORDER BY started_at DESC LIMIT ?",
        (int(limit),)))


def since(conn, started_after: str):
    """某时间点之后的抓取统计（日报用）。"""
    return c.rows(c.execute(
        conn, "SELECT * FROM fetch_log WHERE started_at >= ? ORDER BY started_at", (started_after,)))


# ------------------------------------------------------------------ raw_snapshot


def record_snapshot(conn, *, doc_uid: str, source_id: str, fetched_at: str,
                    http_url: str, kind: str, rel_path: str, content_hash: str,
                    size_bytes: int) -> None:
    """登记一份归档（响应体本身写在磁盘，这里只记索引）。

    同一 (doc_uid, kind, content_hash) 重复登记自动跳过 —— 内容没变就不必再存一份。
    """
    c.execute(
        conn,
        "INSERT INTO raw_snapshot"
        " (doc_uid, source_id, fetched_at, http_url, kind, rel_path, content_hash, size_bytes)"
        " VALUES (?,?,?,?,?,?,?,?)"
        " ON CONFLICT(doc_uid, kind, content_hash) DO NOTHING",
        (doc_uid, source_id, fetched_at, http_url, kind, rel_path, content_hash, size_bytes))


def snapshots_of(conn, doc_uid: str):
    """一条政策的全部归档（详情页的"证据"区）。"""
    return c.rows(c.execute(
        conn, "SELECT * FROM raw_snapshot WHERE doc_uid=? ORDER BY fetched_at DESC", (doc_uid,)))


def count(conn, doc_uid: str | None = None) -> int:
    if doc_uid is None:
        return int(c.scalar(c.execute(conn, "SELECT COUNT(*) AS n FROM raw_snapshot"), 0))
    return int(c.scalar(c.execute(
        conn, "SELECT COUNT(*) AS n FROM raw_snapshot WHERE doc_uid=?", (doc_uid,)), 0))


def delete_by_doc(conn, doc_uid: str) -> int:
    cur = c.execute(conn, "DELETE FROM raw_snapshot WHERE doc_uid=?", (doc_uid,))
    return getattr(cur, "rowcount", 0) or 0
