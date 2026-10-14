"""附件仓储：``attachment`` 表的唯一 SQL 出口。

一个 doc_uid 可以挂多个附件（UNIQUE(doc_uid, url) 保证同一个 URL 只入一次）。
``parse_status`` 的生命周期：NULL（待抓）→ ok / no_text_layer / unsupported / download_failed。
"""
from __future__ import annotations

from typing import Any

from ..db import now_iso
from . import conn as c


def upsert(conn, *, doc_uid: str, url: str, filename: str | None, ext: str | None,
           raw_path: str | None, parsed_text: str | None, parse_status: str | None,
           parse_error: str | None, created_at: str) -> None:
    """登记一个附件。

    **已存在就不动**（``DO NOTHING``）—— 现有实现是 ``INSERT OR IGNORE``，
    语义一致：重复登记不能把已经解析好的 ``parsed_text`` 抹掉。
    """
    c.execute(
        conn,
        "INSERT INTO attachment"
        " (doc_uid, url, filename, ext, raw_path, parsed_text, parse_status, parse_error, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(doc_uid, url) DO NOTHING",
        (doc_uid, url, filename, ext, raw_path, parsed_text, parse_status, parse_error,
         created_at))


def by_doc(conn, doc_uid: str):
    """一条政策的全部附件（详情页展示用）。"""
    return c.rows(c.execute(
        conn, "SELECT * FROM attachment WHERE doc_uid=? ORDER BY id", (doc_uid,)))


def update(conn, att_id: int, values: dict) -> int:
    """按 id 更新若干列（下载成功/失败回填用）。"""
    if not values:
        return 0
    from .policy import _cols  # 复用列名校验，全套仓储只有一处白名单

    cols = _cols(values.keys())
    assignments = ",".join(f"{k}=?" for k in cols)
    params: tuple[Any, ...] = (*values.values(), int(att_id))
    cur = c.execute(conn, f"UPDATE attachment SET {assignments} WHERE id=?", params)
    return getattr(cur, "rowcount", 0) or 0


def pending(conn, *, only_failed: bool = False, only_pending: bool = True, limit: int = 0):
    """待抓的附件（**不做领取**，外部串行时用）。"""
    sql = f"SELECT * FROM attachment WHERE {_pending_condition(only_failed, only_pending)} ORDER BY id"
    params: list[Any] = []
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))
    return c.rows(c.execute(conn, sql, tuple(params)))


def _pending_condition(only_failed: bool, only_pending: bool = True) -> str:
    """待处理条件 —— 与 ``pipeline.fetch_attachments`` 的选择口径**逐字一致**。

    两处口径一旦分叉，"能领的"（仓储层）与"能取的"（流水线）就变成两批
    不同的记录，而这种错误不会报错、只会让一批附件永远轮不到。

    ``url`` 必须非空：没有地址的行连下载都无从谈起，混进来只会白占一批名额。

    三种口径（对应 CLI 的默认 / --all / --retry-failed）：
      · ``only_failed``  —— 只重试失败过的
      · ``only_pending`` —— 未完成的三类（默认）
      · 都不给            —— 不过滤

    **为什么 unsupported 与 download_failed 也在默认重试范围内**（原注释）：

      · ``unsupported`` —— 解析器升级后（例如接通 WPS 处理老式文档），当初
        "读不了"的文件应该再试一次，否则修复永远不会生效，那 1380 条会一直
        是 unsupported，而代码明明已经能解析它们了。
      · ``download_failed`` —— 372 条里有 345 条是 WAF 412（hubei/liaoning/
        jilin 等站），而浏览器兜底能过挑战：实测同一条 URL，httpx 得到 412、
        兜底取回 16493 字节的真 docx。那批失败记录都写在兜底上线之前，
        所以从未被重试过。

    **为什么 failed:* 单独成一条重试路径**：那 388 条 BadZipFile 全是 .docx，
    而 .docx 本身就是 ZIP 容器 —— 报这个说明当初下到的不是有效文件（多半是
    站点返回的错误页被当附件存了下来）。默认的 only_pending 不含它们，
    它们从未被重试过 —— 这就是那条命令存在的理由。
    """
    if only_failed:
        # 只重试**失败过**的。特意不含另外两类：`ok`（已经成功，重下纯属浪费）
        # 与 `no_text_layer`（扫描件缺的是文字层，不是下载或解析环节坏了 ——
        # 重试多少次都一样）。
        return ("url IS NOT NULL AND url <> ''"
                " AND (parse_status LIKE 'failed:%' OR parse_status = 'download_failed')")
    if only_pending:
        return ("url IS NOT NULL AND url <> ''"
                " AND (parse_status = 'pending' OR parse_status = 'unsupported'"
                " OR parse_status = 'download_failed')")
    return "url IS NOT NULL AND url <> ''"


def _lease_cutoff(seconds: int) -> str:
    """租约失效时刻（ISO 字符串）。

    用字符串比较而不是时间函数：``claimed_at`` 就是 ISO 文本，
    **ISO 8601 的字典序等于时间序**，所以两边数据库用同一套比较都成立。
    """
    import datetime

    t = datetime.datetime.now().astimezone() - datetime.timedelta(seconds=seconds)
    return t.replace(microsecond=0).isoformat()


def claim_batch(conn, *, only_failed: bool = False, only_pending: bool = True,
                limit: int = 20, lease_seconds: int = 1800):
    """**领取**一批待处理附件，返回领到的那批。

    这是「让不同 Worker 同时抓附件」的关键。两种后端：

    * **PostgreSQL**：``FOR UPDATE SKIP LOCKED`` 选行，同一条 UPDATE 里打上
      租约时间戳。两个 worker 同时调用会各领各的 —— 不是"一个等工作完"，
      而是**跳过已被领走的行**。进程崩在中间也不会永久卡死：租约过期后
      那一条自动重新可领（所以用时间戳而不是新增"处理中"状态）。
    * **SQLite**：没有 ``SKIP LOCKED``，退化成普通查询 —— 行为与今天完全一致
      （外部仍靠 writelock 串行）。切库后自动获得并发能力。

    下载本身是幂等的，所以租约过期导致偶尔重复处理是无害的；
    反过来（永久卡死一条记录）才是真正要避免的。
    """
    if c.BACKEND != "pg":
        return pending(conn, only_failed=only_failed, only_pending=only_pending,
                       limit=limit)

    cutoff = _lease_cutoff(lease_seconds)
    cond = _pending_condition(only_failed, only_pending)
    sql = (
        "WITH claimed AS ("
        "  SELECT id FROM attachment"
        f"  WHERE {cond} AND (claimed_at IS NULL OR claimed_at < ?)"
        "  ORDER BY id LIMIT ?"
        "  FOR UPDATE SKIP LOCKED"
        ")"
        " UPDATE attachment a SET claimed_at = ?"
        " FROM claimed c WHERE a.id = c.id"
        " RETURNING a.*"
    )
    return c.rows(c.execute(conn, sql, (cutoff, int(limit), now_iso())))


def status_stats(conn) -> dict[str, int]:
    """按解析状态统计（自查与进度看板用）。NULL 归到 ``pending``。"""
    rows = c.rows(c.execute(
        conn,
        "SELECT COALESCE(parse_status, 'pending') AS st, COUNT(*) AS n"
        " FROM attachment GROUP BY COALESCE(parse_status, 'pending')"))
    return {r["st"]: int(r["n"]) for r in rows}


def count_for_docs(conn, doc_uids: list[str]) -> dict[str, int]:
    """一批政策的附件数（列表页一次查完，避免 N+1）。"""
    if not doc_uids:
        return {}
    marks = ",".join("?" for _ in doc_uids)
    rows = c.rows(c.execute(
        conn,
        f"SELECT doc_uid, COUNT(*) AS n FROM attachment WHERE doc_uid IN ({marks})"
        " GROUP BY doc_uid",
        tuple(doc_uids)))
    return {r["doc_uid"]: int(r["n"]) for r in rows}


def delete_by_doc(conn, doc_uid: str) -> int:
    cur = c.execute(conn, "DELETE FROM attachment WHERE doc_uid=?", (doc_uid,))
    return getattr(cur, "rowcount", 0) or 0
