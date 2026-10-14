"""连接与方言层 —— 仓储层里**唯一**知道底层是 SQLite 还是 PostgreSQL 的地方。

## 为什么要这一层

生产现在跑在 SQLite 上（翻译工作流还在写库），不能中途换引擎把生产打断；
而目标是把所有直接 SQL 收进仓储层，**切换时只改这里一个文件**。

## 写 SQL 的规矩（仓储层其余模块必须遵守）

只要按这些写法写，同一句 SQL 在 SQLite 和 PostgreSQL 上都能跑，
切库时除本文件外一行都不用改：

* **占位符一律写 `?`** —— 由 `sql()` 翻成目标方言（PG 用 `%s`）。
* 用 ``COALESCE(x, y)`` 而不是 ``IFNULL(x, y)``（SQLite 3.30+ 与 PG 都支持）。
* 用 ``INSERT ... ON CONFLICT ... DO NOTHING`` 而不是 ``INSERT OR IGNORE``
  （SQLite 3.24+ 与 PG 都支持）。
* 用 ``CASE WHEN x > 0 THEN x ELSE 0 END`` 而不是 ``GREATEST``（SQLite 没有 GREATEST）。
* **取列一律用 ``row["列名"]``，不要用 ``row[0]``** —— SQLite 侧是 ``sqlite3.Row``、
  PG 侧是 ``dict_row``，只有按列名取值两边一致。
* 事务用 :func:`transaction`，不要依赖驱动的隐式事务语义（两边默认行为不同）。

## 切库怎么切

    set TAXASSIST_BACKEND=pg
    set TAXASSIST_PG_DSN=host=127.0.0.1 dbname=taxassist user=taxassist

不设 ``TAXASSIST_BACKEND`` 则仍是 SQLite，与今天的行为完全一致。
"""
from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ..config import DB_PATH, ensure_dirs

BACKEND = (os.environ.get("TAXASSIST_BACKEND") or "sqlite").strip().lower()

# 默认 DSN：本机、正式库、专用角色。密码走 %APPDATA%\postgresql\pgpass.conf，不写进代码。
DEFAULT_PG_DSN = "host=127.0.0.1 dbname=taxassist user=taxassist"


def sql(text: str) -> str:
    """把仓储层写的 SQL 翻成目标方言。

    只做占位符替换：仓储层里**没有**任何 SQL 字符串字面量含 ``?``
    （复核过 store/kb/billing/auth 的现行 SQL），所以这个简单替换是安全的。
    """
    if BACKEND == "pg":
        return text.replace("?", "%s")
    return text


# ------------------------------------------------------------------ 连接


def connect(path: str | Path | None = None):
    """建立读写连接。

    SQLite 侧的参数与 ``db.connect`` 逐字一致（busy_timeout 120 秒那两条实测注释
    保留在那里，此处不重复）—— 迁移期间两者必须能互换，否则行为会漂。
    """
    if BACKEND == "pg":
        return _connect_pg()
    return _connect_sqlite(path)


def connect_readonly(path: str | Path | None = None):
    """只读连接。

    网站与检索侧用：它们只查不写，拿只读身份能避免误写，
    也让 PG 侧将来可以换成真正的只读账号。
    """
    if BACKEND == "pg":
        conn = _connect_pg()
        conn.execute("SET default_transaction_read_only = on")
        return conn
    target = Path(path) if path else DB_PATH
    # SQLite 只读：uri 模式，绝不建库（路径写错时应当报错而不是凭空造一个空库）
    escaped = str(target).replace("?", "%3f").replace("#", "%23").replace("\\", "/")
    uri = f"file:{escaped}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA query_only = ON")
    return conn


def _connect_sqlite(path: str | Path | None = None):
    ensure_dirs()
    target = Path(path) if path else DB_PATH
    conn = sqlite3.connect(str(target), timeout=120)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 120000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _connect_pg():
    import psycopg  # 延迟导入：不装 psycopg 也能继续跑 SQLite 侧
    from psycopg.rows import dict_row

    dsn = os.environ.get("TAXASSIST_PG_DSN") or DEFAULT_PG_DSN
    return psycopg.connect(dsn, row_factory=dict_row)


# ------------------------------------------------------------------ 取数帮助


def rows(cur) -> list[Any]:
    """把游标结果取成列表（两边驱动都支持 fetchall）。"""
    return list(cur.fetchall())


def one(cur):
    """取一行，没有则 None。"""
    return cur.fetchone()


def scalar(cur, default=None):
    """取第一行第一列的值（用 fetchone 而非 fetchall，省一次分配）。"""
    row = cur.fetchone()
    if row is None:
        return default
    # 按序取值：这里拿的是聚合结果（COUNT/SUM），列名不重要，两边都支持 values()
    try:
        return list(row.values())[0] if hasattr(row, "values") else row[0]
    except Exception:
        return row[0] if row is not None else default


# ------------------------------------------------------------------ 事务


@contextmanager
def transaction(conn) -> Iterator[Any]:
    """显式事务。

    SQLite 侧 sqlite3 默认是隐式事务（DML 自动开、需要 commit），
    psycopg 默认也是隐式但**语义不同**（autocommit=False 时才开）。
    仓储层不依赖这些默认值：进入即开事务，正常退出提交，异常回滚。
    """
    if BACKEND == "pg":
        with conn.transaction():
            yield conn
        return
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


# ------------------------------------------------------------------ 语句执行


def execute(conn, text: str, params: Sequence[Any] | None = None):
    """执行一条语句（占位符写 ``?``）。"""
    return conn.execute(sql(text), tuple(params or ()))


def executemany(conn, text: str, seq_params):
    """批量执行（占位符写 ``?``）。"""
    return conn.executemany(sql(text), [tuple(p) for p in seq_params])


def ping() -> str:
    """自检：返回当前后端与连通性摘要，给测试和运维用。"""
    if BACKEND == "pg":
        conn = connect()
        try:
            cur = execute(conn, "SELECT version()")
            return "pg: " + str(scalar(cur, ""))[:60]
        finally:
            conn.close()
    conn = connect()
    try:
        cur = execute(conn, "SELECT sqlite_version()")
        return "sqlite: " + str(scalar(cur, ""))
    finally:
        conn.close()
