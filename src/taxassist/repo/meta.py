"""元信息仓储：``meta`` 表的唯一 SQL 出口。

只存键值对（schema 版本、日报上次运行时间、会话密钥等）。
真正的配置在 `taxassist.config` 里，这里只放**运行期产生、需要跨进程共享**的状态。
"""
from __future__ import annotations

from ..db import now_iso
from . import conn as c


def get(conn, key: str, default: str | None = None) -> str | None:
    row = c.one(c.execute(conn, "SELECT value FROM meta WHERE key=?", (key,)))
    if row is None:
        return default
    return row["value"]


def put(conn, key: str, value: str) -> None:
    """写入或覆盖一个键（``ON CONFLICT`` 两边通用）。"""
    c.execute(
        conn,
        "INSERT INTO meta(key, value, updated_at) VALUES(?,?,?)"
        " ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, value, now_iso()))


def all(conn) -> dict[str, str]:
    """全量取出（运维自查用；键不多，不必分页）。"""
    return {r["key"]: r["value"] for r in c.rows(c.execute(conn, "SELECT key, value FROM meta"))}


def delete(conn, key: str) -> int:
    cur = c.execute(conn, "DELETE FROM meta WHERE key=?", (key,))
    return getattr(cur, "rowcount", 0) or 0
