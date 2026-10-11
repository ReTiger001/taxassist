"""db.migrate / db.init_db 的测试 —— 审计 P1：这条路径此前**零断言**。

**为什么专门测它**：``init_db`` 是每个进程启动的第一件事（worker / web / CLI
全都调它）。它失败不是"某个功能不好用"，而是整个系统起不来。

而它最典型的失败形态，本会话真实撞到**三处同类的"两条建库路径不一致"**：
SCHEMA 的建表语句与 migrate() 的补列清单，缺哪一边都会缺列，而缺列的表现
一律是**启动即失败**（视图/索引引用它），不是某个功能降级：

  ① ``is_official`` 只在 SCHEMA 里、不在 migrate() 里
     → 新库有、老库没有 → init_db 建 policy_official 视图时
       ``no such column: is_official`` → 全站挂
  ② ``p_doc_no_year``/``p_doc_no_seq``/``p_translated_at`` 只在 migrate() 里、
     不在 SCHEMA 里 → 新建的库要靠 ALTER 补出来，两条路径列集不一致
  ③ ``idx_policy_region`` 建在迁移补出来的 ``p_region`` 上，而索引语句原先排在
     ``migrate()`` **之前** → 老库在建索引那一步就 ``no such column: p_region``，
     迁移根本没机会跑，**库永远升不上来**

所以这里钉三件事：
  · 「后加列」必须同时出现在 SCHEMA 与 migrate() 里
  · SCHEMA 里的索引必须全部被拆出去、放到迁移之后执行
  · 缺了后加列的**老库**跑一遍 init_db 之后必须真的可用
    （列补回来、视图能查、索引建起来、数据不丢）
"""
from __future__ import annotations

import sqlite3

from taxassist import db as dbmod

#: **后加的列** —— 每一项都必须同时出现在 SCHEMA 的建表语句与 migrate() 里。
#: 新增一列时往这里补一行；忘了补任何一边，下面的测试就会失败。
_LATER_COLUMNS = (
    "is_official",
    "p_effective_date",
    "p_detail_fetched_at",
    "p_region",
    "p_doc_no_year",
    "p_doc_no_seq",
    "p_translated_at",
)


def _fresh_db_columns() -> set[str]:
    """用一个全新的库取 policy 的列集 —— 走的是 SCHEMA 建表这条路。"""
    conn = sqlite3.connect(":memory:")
    conn.executescript(dbmod.SCHEMA)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(policy)")}
    conn.close()
    return cols


def _make_legacy_db(conn: sqlite3.Connection) -> None:
    """把库改造成「后加列被引入之前」的样子。

    **为什么用 DROP COLUMN 而不是拿文本过滤改 SCHEMA**：第一版就是这么写的，
    结果造出来的 SQL 语法不合法（列的行尾带注释、最后一列还不能有逗号），
    测到的是"我的 fixture 坏了"而不是迁移。交给 SQLite 自己删，删出来的形状
    就是真的老库形状。

    只跑建表段（不含索引）：老库不会有"后加的索引"，而且有索引引用这些列时
    SQLite 会拒绝 DROP COLUMN。
    """
    tables_sql, _ = dbmod._split_schema(dbmod.SCHEMA)
    conn.executescript(tables_sql)
    for col in _LATER_COLUMNS:
        conn.execute(f"ALTER TABLE policy DROP COLUMN {col}")
    conn.commit()


def test_later_columns_exist_in_both_build_paths(monkeypatch):
    """「后加列」必须同时在 SCHEMA 与 migrate() 里 —— 缺哪边都是启动即失败。

    这是 ① 与 ② 两个真实缺陷的通用形态：只在一处定义，另一条路径就会缺列。
    """
    seen: list[tuple[str, str]] = []
    real = dbmod._ensure_column

    def spy(conn, table, column, ddl):
        seen.append((table, column))
        return real(conn, table, column, ddl)

    monkeypatch.setattr(dbmod, "_ensure_column", spy)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row          # migrate 里按列名取值，必须是 Row
    conn.executescript(dbmod.SCHEMA)
    dbmod.migrate(conn)
    conn.close()

    managed = {c for t, c in seen if t == "policy"}
    in_schema = _fresh_db_columns()

    missing_in_schema = sorted(set(_LATER_COLUMNS) - in_schema)
    missing_in_migrate = sorted(set(_LATER_COLUMNS) - managed)
    assert not missing_in_schema, (
        f"这些列没写进 SCHEMA 的建表语句：{missing_in_schema} —— "
        "新建的库只能靠 ALTER 补出来，两条建库路径不一致")
    assert not missing_in_migrate, (
        f"这些列没写进 migrate()：{missing_in_migrate} —— "
        "老库（恢复备份 / 换机）永远补不上这一列")


def test_schema_split_moves_every_index_after_migration():
    """SCHEMA 里的索引必须**全部**被拆出来 —— 漏一条，老库就可能建索引时炸。

    索引可能引用迁移补出来的列；只要有一条索引语句留在建表段里，它就会在
    migrate() 之前执行（见 ③）。
    """
    tables_sql, indexes_sql = dbmod._split_schema(dbmod.SCHEMA)
    assert "CREATE INDEX" not in tables_sql.upper(), \
        "建表段里还留着 CREATE INDEX —— 它会在 migrate() 之前执行"
    n_before = len(dbmod._INDEX_STMT_RE.findall(dbmod.SCHEMA))
    n_after = len(dbmod._INDEX_STMT_RE.findall(indexes_sql))
    assert n_before == n_after, f"索引条数对不上：原文 {n_before}、拆出 {n_after}"
    assert n_after > 0, "一条索引都没拆出来 —— 正则或 SCHEMA 结构变了"


def test_init_db_upgrades_a_legacy_db(tmp_path):
    """**端到端**：缺了后加列的老库，跑一遍 init_db 之后必须真的可用。

    这是 ① 与 ③ 复现出来的那条路径：列补回来、视图能查、索引建起来、
    原来的数据一条不丢。任何一环缺失，实际后果都是"恢复备份之后全站不可用"。
    """
    conn = sqlite3.connect(tmp_path / "legacy.db")
    conn.row_factory = sqlite3.Row
    _make_legacy_db(conn)

    cols_before = {r[1] for r in conn.execute("PRAGMA table_info(policy)")}
    unexpected = sorted(set(_LATER_COLUMNS) & cols_before)
    assert not unexpected, f"前提不成立：老库里不该有这些列 {unexpected}"

    # policy 有四个 NOT NULL 且无默认值的列：doc_uid / title / first_seen_at /
    # last_seen_at —— 造数据时必须给全，否则测的是"我的 fixture 不合法"。
    conn.execute(
        "INSERT INTO policy (doc_uid, title, content, cwrq, first_seen_at, last_seen_at)"
        " VALUES (?,?,?,?,?,?)",
        ("t:1", "国家税务总局关于某某事项的公告", "正文内容", "2020-01-01",
         "2020-01-01T00:00:00+08:00", "2020-01-01T00:00:00+08:00"))
    conn.commit()

    dbmod.init_db(conn)

    cols_after = {r[1] for r in conn.execute("PRAGMA table_info(policy)")}
    still_missing = sorted(set(_LATER_COLUMNS) - cols_after)
    assert not still_missing, f"init_db 之后这些列仍然缺失：{still_missing}"

    # 视图能查 —— 它依赖 is_official，正是 ① 挂掉的地方
    n = conn.execute("SELECT COUNT(*) FROM policy_official").fetchone()[0]
    assert n == 1, f"视图查不到数据（{n} 条）—— 升级过程中数据丢了？"

    # 索引建起来了 —— 正是 ③ 挂掉的地方
    idx = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='policy'")}
    assert "idx_policy_region" in idx, \
        f"索引没建起来（现有 {sorted(idx)}）—— 拆分后漏跑索引段了"

    conn.close()
