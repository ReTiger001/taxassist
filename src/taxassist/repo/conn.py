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
import re
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ..config import DB_PATH, ensure_dirs

DEFAULT_BACKEND = "pg"

#: 默认后端。**已切换为 pg**（2026-10-14）。
#:
#: 为什么用默认值而不是在每个 .bat 里设环境变量：`/tmp/taprobe/` 那类自动化是
#: **直接调 `python -m taxassist ...`** 的、不经过启动脚本，环境变量方案管不到
#: 它；漏一个入口的表现就是「那个任务还在写 SQLite」，而两套库同时被写是这次
#: 改造里最难查的状态。
#:
#: 测试不受影响：`tests/conftest.py` 显式声明 `TAXASSIST_BACKEND=sqlite`
#: （用 setdefault，可被覆盖）—— 测试本就该隔离后端，而不是跟着默认值走。
#:
#: **回退**：把这一行改回 "sqlite"，或设 `TAXASSIST_BACKEND=sqlite` 覆盖。
BACKEND = (os.environ.get("TAXASSIST_BACKEND") or DEFAULT_BACKEND).strip().lower()

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

    **PG 侧必须也走兼容层** —— 曾经这里返回裸 psycopg 连接，结果是
    kb.py 的 ``sqlite_master`` 查询原样发给了 PG 并报「关系不存在」。
    兼容层不是"给写入路径用的"，它是所有出口的唯一通道。
    """
    if BACKEND == "pg":
        conn = connect_compat()
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
    """执行一条语句（占位符写 ``?``）。

    **不要在这里调 sql()** —— 连接层已经负责翻译：``CompatConnection.execute``
    会调 ``adapt_sql``，而 SQLite 连接本来就用 ``?``、不需要翻译。

    再翻一次会出事：``adapt_sql`` 的第一步是 ``%`` → ``%%``，于是已经生成的
    ``%s`` 被转义成 ``%%s``，SQL 里一个占位符都不剩，psycopg 报
    「the query has 0 placeholders but N parameters were passed」。

    这个 bug 是**端到端冒烟**抓到的 —— 单元测试跑在 SQLite 上，而 SQLite 侧
    根本不翻译（``sql()`` 在后端不是 pg 时原样返回），所以看不见它。
    """
    return conn.execute(text, tuple(params or ()))


def executemany(conn, text: str, seq_params):
    """批量执行（占位符写 ``?``）。翻译同样交给连接层，理由见 :func:`execute`。"""
    return conn.executemany(text, [tuple(p) for p in seq_params])


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


# ------------------------------------------------------------------ 兼容连接层


def adapt_sql(text: str) -> str:
    """把既有 SQL（SQLite 风格）翻成 PostgreSQL —— 只做**机械且安全**的替换。

    为什么需要它：全项目有 300+ 处 execute 调用，逐处改写既不可能也不必要。
    让这一层吸收差异，既有 SQL 原样就能跑在两个引擎上。

    替换清单（每条都有全量测试兜底）：

    * ``?`` → ``%s``（占位符）
    * ``IFNULL(`` → ``COALESCE(``（后者 SQLite 3.30+ 也支持，是两边公共写法）
    * ``PRAGMA table_info(t)`` → 查 ``information_schema`` 并**补齐成同样的
      六列**（cid, name, type, notnull, dflt_value, pk）—— 调用方普遍用
      ``row[1]`` 取列名，形状必须一致，不能只返回一列
    * ``sqlite_version()`` → ``version()``
    * ``date('now')`` → ``CURRENT_DATE``。
      **带参数形式（``date('now', '-14 days')``）故意不翻译**：PG 的 ``date()``
      只收一个参数，而正确译法要按列的**类型**决定（text 列必须转成字符串再比，
      否则报「操作符不存在: text >= timestamp」）。这种判断无法靠机械替换做对，
      所以这类写法应当在调用处改成两边通用的 SQL —— 见 ``browse_routes.daily``。
    * ``MAX(0,`` → ``GREATEST(0,``

    **故意不做**的替换，因为无法机械判定、必须人工改：
    ``INSERT OR IGNORE`` / ``datetime('now', '-N days')`` / ``changes()`` /
    ``strftime`` / ``sqlite_master`` / ``lastrowid`` / ``policy_fts``。

    注意 ``?`` 会误伤字符串字面量里的问号 —— 全库 SQL 已扫过，没有这种写法。
    """
    if BACKEND != "pg":
        return text
    s = text.strip()

    # PRAGMA table_info(t) —— 整条语句替换（形状必须与原函数返回值一致）
    m = re.fullmatch(r"PRAGMA\s+table_info\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)\s*;?", s,
                     flags=re.IGNORECASE)
    if m:
        # 表名已由上面的正则限定为 [A-Za-z_][A-Za-z0-9_]*，插值安全。
        table = m.group(1)
        return (
            "SELECT 0 AS cid, column_name AS name, data_type AS type,"
            " CASE WHEN is_nullable = 'NO' THEN 1 ELSE 0 END AS notnull,"
            " column_default AS dflt_value,"
            " CASE WHEN column_name IN ("
            "   SELECT kcu.column_name FROM information_schema.table_constraints tc"
            "   JOIN information_schema.key_column_usage kcu"
            "     ON kcu.constraint_name = tc.constraint_name AND kcu.table_name = tc.table_name"
            f"   WHERE tc.constraint_type = 'PRIMARY KEY' AND tc.table_name = '{table}')"
            " THEN 1 ELSE 0 END AS pk"
            " FROM information_schema.columns"
            f" WHERE table_name = '{table}' ORDER BY ordinal_position"
        )

    # SQLite 的「表存在吗」查询 → information_schema。
    # 形态固定（项目里两处都长这样），所以能安全地整体替换：
    # 调用方只看返回有没有行，别名保持 name 以免取值方式变化。
    m = re.fullmatch(
        r"SELECT\s+name\s+FROM\s+sqlite_master\s+"
        r"WHERE\s+type\s*=\s*'table'\s+AND\s+name\s*=\s*'([A-Za-z_][A-Za-z0-9_]*)'\s*;?",
        s, flags=re.IGNORECASE)
    if m:
        return ("SELECT table_name AS name FROM information_schema.tables"
                f" WHERE table_type = 'BASE TABLE' AND table_name = '{m.group(1)}'")

    # **顺序不能反**：
    #   1. 先把字面量里的 % 转义成 %% —— psycopg 用 %s 做占位符，裸 % 会被它
    #      当成占位符起始。而 SQL 里 `LIKE '%的通知'` 这类写法到处都是
    #      （sqlite3 用 ? 做占位符，% 就只是普通字符，从来没有这个问题）。
    #   2. 再把 ? 换成 %s。
    # 反过来做会把刚生成的 %s 也转义成 %%s，占位符全废。
    s = s.replace("%", "%%")
    s = s.replace("?", "%s")
    s = re.sub(r"\bIFNULL\s*\(", "COALESCE(", s, flags=re.IGNORECASE)
    s = re.sub(r"\bsqlite_version\s*\(\s*\)", "version()", s, flags=re.IGNORECASE)
    s = re.sub(r"\bdate\s*\(\s*'now'\s*\)", "CURRENT_DATE", s, flags=re.IGNORECASE)
    s = re.sub(r"\bMAX\s*\(\s*0\s*,", "GREATEST(0,", s, flags=re.IGNORECASE)
    return s


class CompatRow:
    """同时支持 ``row[0]`` 与 ``row["列名"]`` 的行对象。

    SQLite 的 sqlite3.Row 两者都支持，psycopg 的 dict_row 只支持键 —— 而既有
    代码里有十几处 ``fetchone()[0]``（聚合结果居多）。与其改那十几处、将来
    还可能被新代码写回去，不如让 PG 侧的行为与 SQLite 对齐。

    实现 mapping 协议（``keys()`` + ``__getitem__``）使 ``dict(row)`` 可用，
    ``__iter__`` 给整数索引路径用。
    """

    __slots__ = ("_vals", "_map")

    def __init__(self, mapping):
        self._map = mapping
        self._vals = list(mapping.values()) if hasattr(mapping, "values") else list(mapping)

    def __getitem__(self, key):
        if isinstance(key, (int, slice)):
            return self._vals[key]
        return self._map[key]

    def keys(self):
        return self._map.keys()

    def values(self):
        return self._map.values()

    def items(self):
        return self._map.items()

    def get(self, key, default=None):
        return self._map.get(key, default)

    def __contains__(self, key):
        return key in self._map

    def __iter__(self):
        return iter(self._vals)

    def __len__(self):
        return len(self._vals)

    def __repr__(self):
        return f"CompatRow({self._map!r})"


class CompatCursor:
    """兼容游标：让 PG 的结果行两种取值方式都可用。"""

    __slots__ = ("_cur",)

    def __init__(self, cur):
        self._cur = cur

    def fetchone(self):
        row = self._cur.fetchone()
        return None if row is None else CompatRow(row)

    def fetchall(self):
        return [CompatRow(r) for r in self._cur.fetchall()]

    def __iter__(self):
        for row in self._cur:
            yield CompatRow(row)

    @property
    def rowcount(self):
        return getattr(self._cur, "rowcount", -1)

    @property
    def lastrowid(self):
        # PostgreSQL 没有 lastrowid；需要主键的地方应改用 RETURNING（见 repo.fetch）
        return getattr(self._cur, "lastrowid", None)


def _raise_translated(exc):
    """把 psycopg 的异常翻成 sqlite3 的对应类型后再抛。

    为什么值得单独做一层：既有代码里有 7 处捕获 ``sqlite3.OperationalError``
    / ``sqlite3.IntegrityError`` 做容错降级 —— 例如「全新库还没建表，当作
    未登录」（auth.py）、「app_user 表可能还不存在」（billing.py）。
    PG 抛的是 psycopg 自己的类型，不被翻译的话那些容错会**静默失效**：
    本该降级成「未登录」，实际变成整个请求崩掉。翻译一层，那 7 处一行不用改。

    映射依据是**调用方的语义**，不是名字像不像：
      · 唯一键 / 外键 / 非空 / CHECK 冲突 → ``sqlite3.IntegrityError``
      · 其余数据库错误（表或列不存在、连接失败、语法错）→
        ``sqlite3.OperationalError`` —— 调用方就是这么用的：sqlite3 把
        「表不存在」也算 OperationalError，那 7 处都按「操作层面出了问题、
        降级继续」处理。

    总是抛出，不返回 —— 调用处因此不需要 return。
    """
    if isinstance(exc, sqlite3.Error):
        raise exc                                  # 已是 sqlite3 类型，原样抛

    try:
        import psycopg
    except ImportError:                            # pragma: no cover
        # 不该发生：走到这里说明用的是 pg 后端。from None 隐去导入错误本身，
        # 把原始失败原因露出来。
        raise exc from None

    integrity_errors = (
        psycopg.errors.UniqueViolation,
        psycopg.errors.ForeignKeyViolation,
        psycopg.errors.NotNullViolation,
        psycopg.errors.CheckViolation,
    )
    if isinstance(exc, integrity_errors):
        raise sqlite3.IntegrityError(str(exc)) from exc
    if isinstance(exc, psycopg.Error):
        raise sqlite3.OperationalError(str(exc)) from exc
    raise exc                                      # 非数据库异常，原样往上抛


class CompatConnection:
    """把 psycopg 连接包装成既有代码认得的形状。

    只包装既有代码真正用到的部分：execute / executemany / executescript /
    commit / rollback / close / cursor。不假装自己是 sqlite3.Connection ——
    类型判断仍然区分得开，需要真实 PG 连接时用 ``.raw``。

    **异常也一并翻译**（见 :func:`_raise_translated`）：既有代码里有 7 处
    捕获 ``sqlite3.OperationalError`` / ``IntegrityError`` 做容错降级
    （如「全新库还没建表，当作未登录」）。PG 抛的是 psycopg 自己的类型，
    不被翻译的话那些容错会静默失效 —— 那不是「报错更清楚」，而是
    「本该降级的地方变成崩溃」。
    """

    def __init__(self, raw):
        self.raw = raw

    def execute(self, text, params=None):
        try:
            return CompatCursor(self.raw.execute(adapt_sql(text), tuple(params or ())))
        except Exception as e:  # noqa: BLE001
            _raise_translated(e)

    def executemany(self, text, seq_params):
        # **psycopg3 的 Connection 没有 executemany** —— 只有 cursor 有。
        # 原实现直接 `self.raw.executemany(...)`，在 PG 上必报
        # `'Connection' object has no attribute 'executemany'`。
        # 此前全项目没有一处用 executemany，所以这个洞一直没被踩到；
        # 2026-10-20 写向量索引批量插入时第一次踩到。
        # sqlite3.Connection 是有 executemany 的，所以 SQLite 侧照旧。
        try:
            if hasattr(self.raw, "executemany"):
                return CompatCursor(
                    self.raw.executemany(adapt_sql(text), [tuple(p) for p in seq_params]))
            cur = self.raw.cursor()
            cur.executemany(adapt_sql(text), [tuple(p) for p in seq_params])
            return CompatCursor(cur)
        except Exception as e:  # noqa: BLE001
            _raise_translated(e)

    def executescript(self, script: str):
        """PG 没有 executescript：按分号拆分逐条执行。

        既有代码只在建表路径上用（db.init_db / auth / billing 的 ensure_table），
        那几处的语句里没有含分号的字符串字面量，拆分是安全的。
        """
        try:
            for stmt in _split_statements(script):
                self.raw.execute(adapt_sql(stmt))
        except Exception as e:  # noqa: BLE001
            _raise_translated(e)
        return None

    def cursor(self, *a, **kw):
        return CompatCursor(self.raw.cursor(*a, **kw))

    def commit(self):
        self.raw.commit()

    def rollback(self):
        self.raw.rollback()

    def close(self):
        self.raw.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        if exc[0] is None:
            self.raw.commit()
        else:
            self.raw.rollback()
        return False


def _split_statements(script: str) -> list[str]:
    """把一段 DDL 拆成单条语句（跳过空段）。"""
    return [s.strip() for s in script.split(";") if s.strip() and s.strip() != ""]


def connect_compat():
    """建立 PostgreSQL 连接并包装成兼容层（既有代码用它替代 sqlite3.connect）。"""
    if BACKEND != "pg":
        raise RuntimeError("connect_compat 只在 TAXASSIST_BACKEND=pg 时可用")
    import psycopg
    from psycopg.rows import dict_row

    dsn = os.environ.get("TAXASSIST_PG_DSN") or DEFAULT_PG_DSN
    # row_factory 要的是**接收游标的可调用对象**，dict_row 正是那个；
    # 传内置的 dict 会让游标初始化内部炸在 _make_row 上。
    return CompatConnection(psycopg.connect(dsn, row_factory=dict_row))
