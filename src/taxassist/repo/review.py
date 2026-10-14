"""审核仓储：``translation_ai_review`` 与 ``translation_review`` 的唯一 SQL 出口。

**两张表语义完全不同，不能混用**：

* ``translation_ai_review`` —— **机器判定**。可随时重跑覆盖，只用于筛选与排序，
  不能作为"这条译文已核验"的依据。
* ``translation_review`` —— **人工判定**。是结论，要被引用和追溯，
  机器结果**永不覆盖**它（写入前必须确认这是人给的）。
"""
from __future__ import annotations

from typing import Any

from . import conn as c

_AI = "translation_ai_review"
_HUMAN = "translation_review"


def ensure_table(conn) -> None:
    """建两张审核表（幂等，**仅 SQLite 侧**）。

    此前这两张表的 DDL 分别写在 `scripts/ai_review.py` 与
    `scripts/import_review.py` 里 —— 定义权在脚本手上，于是「新库不跑那个脚本
    就没有表」（`browse_routes` 为此写过降级）。收进仓储层，让表与它的读写
    代码住在一起，测试也能直接建。

    PostgreSQL 侧的表由 `src/taxassist/pg/schema.sql` 建（那份是转换来的
    真实 DDL），所以这里只在 sqlite 后端执行 —— `AUTOINCREMENT` 是 SQLite
    专有语法，原样发给 PG 会报错。
    """
    if c.BACKEND == "pg":
        return
    c.execute(
        conn,
        "CREATE TABLE IF NOT EXISTS translation_ai_review ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " doc_uid TEXT NOT NULL,"
        " field TEXT NOT NULL,"
        " verdict TEXT NOT NULL,"          # ok / issue / unsure
        " reason TEXT,"
        " model TEXT,"
        " zh_len INTEGER,"                 # 原文长度：指纹比对之外的第二个线索
        " reviewed_at TEXT NOT NULL,"
        " UNIQUE(doc_uid, field))")
    c.execute(
        conn,
        "CREATE TABLE IF NOT EXISTS translation_review ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " doc_uid TEXT NOT NULL,"
        " field TEXT NOT NULL,"
        " verdict TEXT NOT NULL,"          # ok（通过）/ bad（有问题）
        " note TEXT,"
        " reviewer TEXT,"
        " kind TEXT,"                      # 机器当时挑它的类别
        " reviewed_at TEXT NOT NULL,"
        " UNIQUE(doc_uid, field))")


# ------------------------------------------------------------------ 机器判定


def save_ai(conn, *, doc_uid: str, field: str, verdict: str, reason: str | None,
            model: str | None, zh_len: int | None, reviewed_at: str) -> None:
    """写入/覆盖一条 AI 判定（同一 doc_uid+field 只留最新的）。"""
    c.execute(
        conn,
        "INSERT INTO translation_ai_review"
        " (doc_uid, field, verdict, reason, model, zh_len, reviewed_at)"
        " VALUES (?,?,?,?,?,?,?)"
        " ON CONFLICT(doc_uid, field) DO UPDATE SET"
        "   verdict=excluded.verdict, reason=excluded.reason, model=excluded.model,"
        "   zh_len=excluded.zh_len, reviewed_at=excluded.reviewed_at",
        (doc_uid, field, verdict, reason, model, zh_len, reviewed_at))


def ai_stats(conn, field: str | None = None) -> dict[str, int]:
    """按判定值统计（ok / issue / unsure …）。"""
    if field is None:
        rows = c.rows(c.execute(
            conn, f"SELECT verdict, COUNT(*) AS n FROM {_AI} GROUP BY verdict"))
    else:
        rows = c.rows(c.execute(
            conn, f"SELECT verdict, COUNT(*) AS n FROM {_AI} WHERE field=? GROUP BY verdict",
            (field,)))
    return {r["verdict"]: int(r["n"]) for r in rows}


def ai_by_verdict(conn, verdict: str, field: str | None = None, limit: int = 0):
    """按判定值取明细（审校台用）。"""
    sql = f"SELECT * FROM {_AI} WHERE verdict=?"
    params: list[Any] = [verdict]
    if field is not None:
        sql += " AND field=?"
        params.append(field)
    sql += " ORDER BY reviewed_at DESC"
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))
    return c.rows(c.execute(conn, sql, tuple(params)))


# ------------------------------------------------------------------ 人工判定


def save_human(conn, *, doc_uid: str, field: str, verdict: str, note: str | None,
               reviewer: str | None, kind: str | None, reviewed_at: str) -> None:
    """写入一条**人工**判定（导入审校结果用）。

    只由人给的输入调用 —— 绝不要拿 AI 的结果走这个入口，
    否则"哪些是人核过的"这个问题就再也答不上来了。
    """
    c.execute(
        conn,
        "INSERT INTO translation_review"
        " (doc_uid, field, verdict, note, reviewer, kind, reviewed_at)"
        " VALUES (?,?,?,?,?,?,?)"
        " ON CONFLICT(doc_uid, field) DO UPDATE SET"
        "   verdict=excluded.verdict, note=excluded.note, reviewer=excluded.reviewer,"
        "   kind=excluded.kind, reviewed_at=excluded.reviewed_at",
        (doc_uid, field, verdict, note, reviewer, kind, reviewed_at))


def human_stats(conn) -> dict[str, int]:
    rows = c.rows(c.execute(
        conn, f"SELECT verdict, COUNT(*) AS n FROM {_HUMAN} GROUP BY verdict"))
    return {r["verdict"]: int(r["n"]) for r in rows}


def human_count(conn) -> int:
    return int(c.scalar(c.execute(conn, f"SELECT COUNT(*) AS n FROM {_HUMAN}"), 0))


def human_by_doc(conn, doc_uid: str):
    return c.rows(c.execute(
        conn, f"SELECT * FROM {_HUMAN} WHERE doc_uid=?", (doc_uid,)))


def unreviewed(conn, *, field: str, limit: int = 50):
    """有 AI 判定为 issue、但**还没有人工结论**的条目 —— 审校台的待办来源。"""
    return c.rows(c.execute(
        conn,
        f"SELECT a.* FROM {_AI} a"
        f" LEFT JOIN {_HUMAN} h ON h.doc_uid = a.doc_uid AND h.field = a.field"
        " WHERE a.field=? AND a.verdict<>'ok' AND h.doc_uid IS NULL"
        " ORDER BY a.reviewed_at DESC LIMIT ?",
        (field, int(limit))))
