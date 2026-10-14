"""译文仓储：``translation`` 表的唯一 SQL 出口。

缓存语义（拿 src_hash 比对决定要不要重译）属于**领域逻辑**，在
`translate_llm.cached` 里；这里只提供原子操作，不做判定。

``ON CONFLICT(doc_uid, field, lang) DO UPDATE SET ... excluded.*``
在 SQLite 3.24+ 与 PostgreSQL 上语义相同，切库不用改这一句。
"""
from __future__ import annotations

from typing import Any

from . import conn as c


def get(conn, doc_uid: str, field: str, lang: str = "en"):
    """取一条译文（无则 None）。返回整行，调用方自行决定要不要比对 src_hash。"""
    return c.one(c.execute(
        conn,
        "SELECT * FROM translation WHERE doc_uid=? AND field=? AND lang=?",
        (doc_uid, field, lang)))


def by_doc(conn, doc_uid: str, lang: str | None = None):
    """一条政策的全部译文（详情页用）。给 lang 则只取该语种。"""
    if lang is None:
        return c.rows(c.execute(
            conn, "SELECT * FROM translation WHERE doc_uid=? ORDER BY field", (doc_uid,)))
    return c.rows(c.execute(
        conn, "SELECT * FROM translation WHERE doc_uid=? AND lang=? ORDER BY field",
        (doc_uid, lang)))


def save(conn, *, doc_uid: str, field: str, lang: str, src_hash: str, text: str,
         model: str | None, created_at: str) -> None:
    """写入一条译文；同一 (doc_uid, field, lang) 已存在则覆盖。

    时间戳由调用方传入 —— 仓储层不替领域层决定"这条译文算什么时候产生的"。
    """
    c.execute(
        conn,
        "INSERT INTO translation (doc_uid, field, lang, src_hash, text, model, created_at)"
        " VALUES (?,?,?,?,?,?,?)"
        " ON CONFLICT(doc_uid, field, lang) DO UPDATE SET"
        "   src_hash=excluded.src_hash, text=excluded.text,"
        "   model=excluded.model, created_at=excluded.created_at",
        (doc_uid, field, lang, src_hash, text, model, created_at))


def update_text(conn, doc_uid: str, field: str, text: str, lang: str = "en") -> int:
    """只改译文文本（术语统一、机构名规范化用）。

    **不动 src_hash**：源文没变，变的是译法 —— 改了 src_hash 会让缓存判定
    误以为源文变了，触发无谓重译。
    """
    cur = c.execute(
        conn, "UPDATE translation SET text=? WHERE doc_uid=? AND field=? AND lang=?",
        (text, doc_uid, field, lang))
    return getattr(cur, "rowcount", 0) or 0


def count(conn, field: str | None = None, lang: str | None = None) -> int:
    """计数。两种口径：按 field（title/content）或按 lang。"""
    where, params = [], []
    if field is not None:
        where.append("field=?")
        params.append(field)
    if lang is not None:
        where.append("lang=?")
        params.append(lang)
    tail = (" WHERE " + " AND ".join(where)) if where else ""
    return int(c.scalar(c.execute(
        conn, f"SELECT COUNT(*) AS n FROM translation{tail}", tuple(params)), 0))


def missing(conn, *, field: str, lang: str, limit: int = 0):
    """列出**没有该语种译文**的政策（文档编号 + 内容指纹的判定在调用方）。

    只做"左连接找不到"这一件事，避免在仓储层里塞业务筛选条件。
    """
    tail = " LIMIT ?" if limit else ""
    params: tuple[Any, ...] = (field, lang) + ((int(limit),) if limit else ())
    return c.rows(c.execute(
        conn,
        "SELECT p.doc_uid FROM policy p"
        " LEFT JOIN translation t"
        "   ON t.doc_uid = p.doc_uid AND t.field = ? AND t.lang = ?"
        " WHERE t.doc_uid IS NULL" + tail,
        params))


def delete_by_doc(conn, doc_uid: str) -> int:
    """删除某条政策的全部译文（去重合并时用）。"""
    cur = c.execute(conn, "DELETE FROM translation WHERE doc_uid=?", (doc_uid,))
    return getattr(cur, "rowcount", 0) or 0
