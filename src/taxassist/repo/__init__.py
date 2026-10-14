"""领域仓储层：全项目的直接 SQL 收在这里，写规则只此一处。

为什么要这一层（三条理由，与「并发」无关 —— 并发已由数据库本身解决）：

1. **迁移只改一层**：SQL 方言集中在 `conn`，换库时不必去改十几个脚本。
2. **约束集中**：「写入必须走哪条路」有唯一答案，不再散落各处。
3. **AI 好读**：读懂这一层就知道系统的全部读写路径，不必读遍所有脚本。

分层：

    调用方（pipeline / effect / cli / web）
        ↓
    领域逻辑（store.py / kb.py / effect.py / billing.py）
        ↓
    仓储层（本包）                ← 只有这里写 SQL
        ↓
    conn.py                       ← 唯一知道 SQLite / PostgreSQL 的地方

模块职责：``policy`` / ``translation`` / ``attachment`` / ``fetch`` /
``relation`` / ``review`` / ``meta`` / ``search`` 各管一张或一组表。

**待补**：``billing``（customer / api_key / usage_log）与 ``auth``
（app_user / invite_code）—— 它们不在「多写入方争锁」的矛盾路径上
（12 个写锁入口没有一个碰这两组表），排在切库时一并收拢。

写法规矩见 `conn` 模块文档：占位符写 ``?``、取列用 ``row["列名"]``、
用 COALESCE / ON CONFLICT / CASE WHEN 这些两边通用的写法。
"""
from __future__ import annotations

from . import (
    attachment,
    conn,
    fetch,
    meta,
    policy,
    relation,
    review,
    search,
    translation,
)

__all__ = [
    "attachment",
    "conn",
    "fetch",
    "meta",
    "policy",
    "relation",
    "review",
    "search",
    "translation",
]
