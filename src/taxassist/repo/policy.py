"""政策仓储：``policy`` 表的唯一 SQL 出口。

## 分层约定

本模块**只管 SQL 与行形状**，不放业务规则。像「什么标题算非正式内容」那种
正则判定（`store.NONOFFICIAL_TITLE`）是**领域逻辑**，留在 store.py ——
它会在业务演进中频繁调整，而 SQL 不该跟着动。

   调用方（pipeline / effect / cli / web）
        ↓
   领域逻辑（store.py / kb.py / effect.py / billing.py）
        ↓
   仓储层（本包）            ← 只有这里写 SQL
        ↓
   conn.py                   ← 唯一知道 SQLite / PostgreSQL 的地方

## 写 SQL 的规矩

* 占位符一律 ``?``（由 `conn.sql` 翻方言）。
* 取列一律 ``row["列名"]``，**不要** ``row[0]`` —— SQLite 侧是 sqlite3.Row、
  PG 侧是 dict_row，只有按列名取值两边一致。
* 动态列名（policy 有 53 列，必须动态拼）一律先过 :func:`_col` 校验。
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from typing import Any

from ..db import now_iso
from . import conn as c

#: 列名白名单形态。动态拼列名是必须的（53 列不可能逐条写死），
#: 但拼接前必须校验，否则列名一旦来自外部输入就是注入面。
_COL_OK = re.compile(r"^[a-z_][a-z0-9_]*$")

#: policy 表允许整行 upsert 的列（其余为自增主键等，不参与写入）。
_SKIP_ON_INSERT: frozenset[str] = frozenset()

#: 更新时永不覆盖的列：doc_uid 是主键，first_seen_at 是"第一次见到"的语义。
_NEVER_UPDATE: frozenset[str] = frozenset({"doc_uid", "first_seen_at"})


def _col(name: str) -> str:
    """校验并返回一个列名（动态 SQL 的唯一入口）。"""
    if not _COL_OK.match(name):
        raise ValueError(f"非法列名：{name!r}")
    return name


def _cols(names: Iterable[str]) -> list[str]:
    return [_col(n) for n in names]


# ------------------------------------------------------------------ 读


def get(conn, doc_uid: str):
    """按 doc_uid 取一条（无则 None）。"""
    return c.one(c.execute(conn, "SELECT * FROM policy WHERE doc_uid = ?", (doc_uid,)))


def get_meta_by_uid(conn, doc_uid: str):
    """只取判定用的两个字段（入库路径用，避免 select * 拖回 53 列）。"""
    return c.one(c.execute(
        conn, "SELECT id, content_hash FROM policy WHERE doc_uid = ?", (doc_uid,)))


def exists(conn, doc_uid: str) -> bool:
    return c.one(c.execute(conn, "SELECT 1 AS x FROM policy WHERE doc_uid = ?", (doc_uid,))) is not None


def count(conn, where_sql: str = "", params: Sequence[Any] = ()) -> int:
    """计数。``where_sql`` 由调用方给（内部代码，不是用户输入）。"""
    tail = f" WHERE {where_sql}" if where_sql else ""
    return int(c.scalar(c.execute(conn, f"SELECT COUNT(*) AS n FROM policy{tail}", tuple(params)), 0))


def page(conn, *, where_sql: str = "", params: Sequence[Any] = (),
         order_sql: str = "cwrq DESC", limit: int = 20, offset: int = 0):
    """分页取多行。order/where 由调用方给常量片段（内部代码）。"""
    tail = f" WHERE {where_sql}" if where_sql else ""
    return c.rows(c.execute(
        conn,
        f"SELECT * FROM policy{tail} ORDER BY {order_sql} LIMIT ? OFFSET ?",
        tuple(params) + (int(limit), int(offset)),
    ))


def by_uids(conn, doc_uids: Sequence[str]):
    """按一组 doc_uid 取回（用于批量核对/导出）。"""
    if not doc_uids:
        return []
    marks = ",".join("?" for _ in doc_uids)
    return c.rows(c.execute(conn, f"SELECT * FROM policy WHERE doc_uid IN ({marks})", tuple(doc_uids)))


# ------------------------------------------------------------------ 写


def insert(conn, row: dict) -> None:
    """插入一条完整政策行（动态列）。"""
    cols = _cols([k for k in row if k not in _SKIP_ON_INSERT])
    marks = ",".join("?" for _ in cols)
    c.execute(conn, f"INSERT INTO policy({','.join(cols)}) VALUES({marks})",
              tuple(row[k] for k in cols))


def update(conn, doc_uid: str, values: dict, *, touch_changed_at: bool = True) -> int:
    """更新若干列；默认同时写 last_changed_at（与现行 upsert 行为一致）。

    返回受影响行数。**不做空值过滤** —— 是否用空值覆盖是业务判断，
    由调用方（store.upsert_policy 的 drop_null 逻辑）决定，仓储层只管执行。
    """
    items = [(k, v) for k, v in values.items() if k not in _NEVER_UPDATE]
    if not items:
        return 0
    cols = _cols([k for k, _ in items])
    assignments = ",".join(f"{k}=?" for k in cols)
    tail = ", last_changed_at=?" if touch_changed_at else ""
    params = [v for _, v in items]
    if touch_changed_at:
        params.append(now_iso())
    params.append(doc_uid)
    cur = c.execute(conn, f"UPDATE policy SET {assignments}{tail} WHERE doc_uid=?", tuple(params))
    return getattr(cur, "rowcount", 0) or 0


def touch(conn, doc_uid: str, content_hash: str | None = None) -> int:
    """只刷新「又见到一次」：last_seen_at、fetch_count+1（可选更新哈希）。"""
    if content_hash is None:
        cur = c.execute(conn, "UPDATE policy SET last_seen_at=?, fetch_count=fetch_count+1"
                              " WHERE doc_uid=?", (now_iso(), doc_uid))
    else:
        cur = c.execute(conn, "UPDATE policy SET last_seen_at=?, fetch_count=fetch_count+1,"
                              " content_hash=? WHERE doc_uid=?",
                        (now_iso(), content_hash, doc_uid))
    return getattr(cur, "rowcount", 0) or 0


def set_fields(conn, doc_uids: Sequence[str], values: dict) -> int:
    """把同一组值写给多条（批量回填用，避开逐条往返）。"""
    if not doc_uids or not values:
        return 0
    cols = _cols(values.keys())
    assignments = ",".join(f"{k}=?" for k in cols)
    marks = ",".join("?" for _ in doc_uids)
    params = list(values.values()) + list(doc_uids)
    cur = c.execute(conn, f"UPDATE policy SET {assignments} WHERE doc_uid IN ({marks})", tuple(params))
    return getattr(cur, "rowcount", 0) or 0


def delete(conn, doc_uids: Sequence[str]) -> int:
    """按 doc_uid 删除（去重/清理用，调用方负责先归档证据）。"""
    if not doc_uids:
        return 0
    marks = ",".join("?" for _ in doc_uids)
    cur = c.execute(conn, f"DELETE FROM policy WHERE doc_uid IN ({marks})", tuple(doc_uids))
    return getattr(cur, "rowcount", 0) or 0
