"""检索仓储：全文检索的唯一出口。

两个后端用**截然不同**的机制，这里统一成一个接口：

* **PostgreSQL → PGroonga**：``&@~`` 查询 + ``pgroonga_score()`` 排序。
  中文由 Groonga 自己分词，**不需要**查询预处理。
* **SQLite → FTS5**：``MATCH`` + ``bm25()``，需要中文查询预处理
  （现状在 `kb.py`，检索质量有已知缺陷，见下）。

## 为什么切到 PG 是检索质量的**提升**（探针实测）

全库 14518 条真实政策、20 个真实政策术语、**以 LIKE 为真值裁判**：

| 查询词 | FTS5（现状） | PGroonga | LIKE 真值 |
|---|---|---|---|
| 税率 | **0** | **2222** | 2222 |
| 国家税务总局 | **14167** | **12279** | 12279 |

* **FTS5 二字词全部漏召回**：它用 trigram（3-gram），二字词根本无法索引。
  系统里所有二字查询（税率、减免、申报…）现在都是坏的。
* **FTS5 长词反而虚高**：把「国家」「税务」「总局」拆开各自匹配，混进大量噪音。
* **PGroonga 18/20 与真值精确一致**，另 2 个各多 1 条是分词带来的合理召回扩展。

## 迁移期约定

SQLite 侧的检索**仍然走 `kb.py` 的现有实现**（它已被网站、助手、MCP 多处调用，
且带 <3 字回退等细节），本模块的 :func:`search` 在 sqlite 后端下会明确报错，
提醒调用方走 kb —— 避免出现两套行为不完全一致的检索路径悄悄并存。
"""
from __future__ import annotations

from typing import Any

from . import conn as c


class NotSupportedOnSQLite(NotImplementedError):
    """SQLite 后端下请走 kb.py 的现有检索实现（见模块文档）。"""


def score_supported() -> bool:
    """当前后端是否支持相关度排序。"""
    return c.BACKEND == "pg"


def search(conn, query: str, *, limit: int = 20, offset: int = 0,
           fields: tuple[str, ...] = ("title", "content")):
    """全文检索，按相关度倒序返回 ``policy`` 行（多一个 ``score`` 列）。

    ``fields`` 决定在哪些列上匹配；默认标题 + 正文（与现有网站检索口径一致）。
    """
    query = (query or "").strip()
    if not query:
        return []
    if c.BACKEND != "pg":
        raise NotSupportedOnSQLite(
            "SQLite 后端请用 kb.search（本模块只在 PostgreSQL + PGroonga 下工作）")

    allowed = {"title", "content", "kw", "o_keywords", "p_doc_no_full", "pub_name"}
    cols = [f for f in fields if f in allowed]
    if not cols:
        raise ValueError(f"没有可检索的列：{fields!r}")

    where = " OR ".join(f"p.{col} &@~ ?" for col in cols)
    params: list[Any] = [query] * len(cols)
    sql = (
        "SELECT p.*, pgroonga_score(p.tableoid, p.ctid) AS score"
        f" FROM policy p WHERE {where}"
        " ORDER BY score DESC, p.cwrq DESC"
        " LIMIT ? OFFSET ?"
    )
    params += [int(limit), int(offset)]
    return c.rows(c.execute(conn, sql, tuple(params)))


def count(conn, query: str, *, fields: tuple[str, ...] = ("title", "content")) -> int:
    """命中总数（分页要显示「共 N 条」）。"""
    query = (query or "").strip()
    if not query:
        return 0
    if c.BACKEND != "pg":
        raise NotSupportedOnSQLite("SQLite 后端请用 kb 的计数路径")
    allowed = {"title", "content", "kw", "o_keywords", "p_doc_no_full", "pub_name"}
    cols = [f for f in fields if f in allowed]
    if not cols:
        return 0
    where = " OR ".join(f"p.{col} &@~ ?" for col in cols)
    return int(c.scalar(c.execute(
        conn, f"SELECT COUNT(*) AS n FROM policy p WHERE {where}", tuple([query] * len(cols))), 0))


def highlight(conn, text: str, query: str, *, open_tag: str = "<mark>",
              close_tag: str = "</mark>") -> str:
    """命中词高亮（PGroonga 内置）。SQLite 侧没有等价物。"""
    if c.BACKEND != "pg":
        return text
    row = c.one(c.execute(
        conn, "SELECT pgroonga_highlight_html(?, ?, ARRAY[?]::text[]) AS h",
        (text, open_tag, query)))
    return row["h"] if row else text


def snippet(conn, text: str, query: str, *, width: int = 200) -> list[str]:
    """命中处摘要（PGroonga 内置），返回若干片段。"""
    if c.BACKEND != "pg":
        return [text[:width]]
    rows = c.rows(c.execute(
        conn, "SELECT pgroonga_snippet_html(?, ?, ?) AS s",
        (text, query, int(width))))
    if not rows:
        return []
    val = rows[0]["s"]
    return list(val) if val else []
