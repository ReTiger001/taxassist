"""编排层：抓取 → 清洗 → 入库 → 归档 → 日志。

**完整性契约（本模块存在的理由）**

每次抓取结束后，``fetched_count`` 必须等于接口声称的 ``reported_total``。
不等则把 fetch_log 标为 ``incomplete`` 并告警。

为什么这条契约不能省：政府网站的典型故障不是"报错"，而是**静默成功**——
HTTP 200、JSON 结构正常、但内容为空或只返回了一部分。没有这条校验，
你会安静地以为"今天没有新政策"，而真相是抓取坏了。
对税务工作来说，漏掉一份公告的代价远大于多跑一次抓取。
"""
from __future__ import annotations

import logging

from . import effect, store
from .collect.detail import fetch_detail
from .collect.fgk import (
    COLUMNS,
    FGK_SEARCH_API,
    FgkClient,
    incremental_window,
    year_windows,
)
from .collect.http import GuardedClient
from .collect.normalize import build_policy_row

log = logging.getLogger(__name__)

DEFAULT_COLUMNS = ("政策法规", "政策解读", "政策指引")


def collect_window(
    conn,
    client: FgkClient,
    *,
    column: str,
    start: str,
    end: str,
    source_id: str | None = None,
    max_pages: int | None = None,
    archive: bool = True,
) -> dict:
    """抓取一个栏目在一个日期窗口内的全部条目。

    返回统计字典；异常时先写日志再抛出（不允许留下没有终态的 fetch_log）。
    """
    source_id = source_id or COLUMNS.get(column, f"fgk_{column}")
    log_id = store.log_fetch_start(conn, source_id, "incremental" if max_pages else "window",
                                   start, end)

    reported_total: int | None = None
    fetched = new = updated = 0
    malformed = 0
    pages = 0
    error: str | None = None
    status: str | None = None

    try:
        for page in client.iter_window(column, start, end, max_pages=max_pages):
            if reported_total is None:
                reported_total = page.total
            pages += 1

            if archive:
                store.archive_page(
                    conn, source_id=source_id, http_url=FGK_SEARCH_API,
                    column=column, page_num=page.page_num,
                    window_start=start, window_end=end, payload=page.raw,
                )

            for item in page.items:
                row = build_policy_row(item)
                if row is None:
                    malformed += 1
                    log.warning("[%s] 条目缺少 id/title，已跳过：%s", column, str(item)[:160])
                    continue
                result = store.upsert_policy(conn, row)
                fetched += 1
                if result == "new":
                    new += 1
                elif result == "updated":
                    updated += 1
            conn.commit()
    except Exception as e:  # noqa: BLE001 - 需要落日志后原样抛出
        error = f"{type(e).__name__}: {e}"
        store.log_fetch_finish(
            conn, log_id, reported_total=reported_total, fetched_count=fetched,
            new_count=new, updated_count=updated, status="failed", error=error,
        )
        raise

    status = store.log_fetch_finish(
        conn, log_id, reported_total=reported_total, fetched_count=fetched,
        new_count=new, updated_count=updated,
    )

    summary = {
        "column": column, "source_id": source_id,
        "window": [start, end], "pages": pages,
        "reported_total": reported_total, "fetched": fetched,
        "new": new, "updated": updated, "malformed": malformed,
        "status": status,
    }
    if status == "incomplete":
        log.warning("[%s] 窗口 %s~%s 抓取不完整：fetched=%s reported=%s",
                    column, start[:10], end[:10], fetched, reported_total)
    return summary


def collect_incremental(
    conn,
    *,
    days: int = 7,
    columns=DEFAULT_COLUMNS,
    max_pages: int | None = None,
    archive: bool = True,
) -> list[dict]:
    """每日增量：回溯若干天（默认 7 天重叠窗口）靠 doc_uid 去重，避免漏抓。"""
    start, end = incremental_window(days)
    out = []
    with FgkClient() as client:
        for column in columns:
            out.append(collect_window(
                conn, client, column=column, start=start, end=end,
                max_pages=max_pages, archive=archive,
            ))
    return out


def collect_full(
    conn,
    *,
    year_from: int = 1984,
    columns=DEFAULT_COLUMNS,
    max_pages: int | None = None,
    archive: bool = True,
) -> list[dict]:
    """首次全量导入：按年切窗口，逐年抓取（单窗口过大时翻页过多）。"""
    out = []
    with FgkClient() as client:
        for column in columns:
            for start, end in year_windows(year_from):
                out.append(collect_window(
                    conn, client, column=column, start=start, end=end,
                    max_pages=max_pages, archive=archive,
                ))
    return out


def summarize(results: list[dict]) -> str:
    """把抓取结果汇总成一行人类可读的结论（供 CLI 与告警使用）。

    必须同时吃两种结果形状：总局的按「栏目 × 时间窗」，省级的按「源」——
    后者没有 column / window / new / updated 这些键。早先这里直接下标取值，
    于是**任何一个省级源失败，整个 provincial 命令都会崩在汇总这一步**
    （KeyError: 'new'），连"哪个源失败了"都打不出来，看着像命令行坏了。
    """
    bad = [r for r in results if r.get("status") != "ok"]
    fetched = sum(r.get("fetched") or 0 for r in results)
    new = sum(r.get("new") or 0 for r in results)
    updated = sum(r.get("updated") or 0 for r in results)
    head = f"共 {len(results)} 个窗口：抓取 {fetched} 条（新增 {new}、更新 {updated}）"
    if bad:
        parts = []
        for r in bad:
            label = r.get("column") or r.get("source_id") or "?"
            win = r.get("window")
            span = f"{win[0][:10]}~{win[1][:10]} " if win else ""
            parts.append(f"{label} {span}{r.get('status')}"
                         f"({r.get('fetched') or 0}/{r.get('reported_total')})")
        return f"{head}；**异常 {len(bad)} 个**：{'; '.join(parts)}"
    return f"{head}；全部完整"


# ---------------------------------------------------------------- 详情页补充

class _PrefetchedClient:
    """给 fetch_detail 用的适配器：内容来自一次性并发预抓的页面字典。

    fetch_detail 只用到 ``client.get(url).content`` 与 ``raise_for_status()``，
    所以这里做最小适配即可 —— 解析逻辑一行都不用改。

    为什么这么绕：省级站点（**含其详情页**）在加速乐 WAF 后面，普通 HTTP 一律
    412，必须走浏览器；但**逐条**新建浏览器是错的 —— 每条都要重过一遍 WAF
    挑战，实测 8-10 秒/条、267 条要 40 多分钟而且会卡死。改成先并发抓完
    （同域名共享 cookie，挑战每条域名只过一次）再逐条解析，快一两个数量级。
    """

    class _Resp:
        __slots__ = ("content",)

        def __init__(self, html: str) -> None:
            self.content = html.encode("utf-8")

        def raise_for_status(self) -> None:
            return None

    def __init__(self, pages: dict[str, "str | BaseException"]) -> None:
        self._pages = pages

    def get(self, url: str, **_kw) -> "_PrefetchedClient._Resp":
        item = self._pages.get(url)
        if item is None:
            raise RuntimeError(f"详情页未预取：{url}")
        if isinstance(item, BaseException):
            raise item
        return self._Resp(item)


def enrich_details(
    conn,
    *,
    limit: int = 50,
    only_missing: bool = True,
    source_id: str = "fgk_detail",
) -> dict:
    """抓取政策详情页，补齐正文、完整文号、官方时效、施行日期、附件。

    **逐条容错**：单条失败只记录并继续 —— 批量任务里一条坏链接
    不该让其余条目白跑，但失败条目必须出现在返回值里，不能静默吞掉。
    """
    import hashlib

    from .config import RAW_DIR

    sql = ("SELECT doc_uid, url, cwrq FROM policy "
           "WHERE url IS NOT NULL AND url <> ''")
    if only_missing:
        sql += " AND (content IS NULL OR p_detail_fetched_at IS NULL)"
    sql += " ORDER BY cwrq DESC LIMIT ?"
    rows = conn.execute(sql, (limit,)).fetchall()

    ok = failed = updated = attachments = 0
    errors: list[str] = []
    if not rows:
        return {"requested": 0, "ok": 0, "failed": 0, "updated": 0,
                "attachments": 0, "errors": []}

    # 省级详情页也在同一套 WAF 后面，必须走浏览器。但**逐条**新建浏览器是
    # 错的：每条都要重过一遍挑战，8-10 秒/条且会卡死。改成先并发抓完
    # （同域名共享 cookie）再逐条解析 —— 解析逻辑一行不用改。
    # 判据用 doc_uid 含冒号（省级源形如 "gd_zcwj:xxxx"），因为这里只取了
    # doc_uid 与 url 两列，没有 p_region。
    prov_urls = [r["url"] for r in rows if ":" in (r["doc_uid"] or "")]
    prefetched = None  # 有省级条目时是 _PrefetchedClient
    if prov_urls:
        from .collect.browser import fetch_many

        log.info("并发预抓 %d 个省级详情页…", len(prov_urls))
        pages = fetch_many(prov_urls)
        hit = sum(1 for v in pages.values() if not isinstance(v, BaseException))
        log.info("预抓完成：成功 %d / 失败 %d", hit, len(pages) - hit)
        prefetched = _PrefetchedClient(pages)

    with GuardedClient() as client:
        for row in rows:
            use_client = prefetched if ":" in (row["doc_uid"] or "") else client
            try:
                # cwrq 交给解析器做文号自洽校验：详情页上未必解析得出成文日期，
                # 而列表页给的日期是可靠的（见 detail.parse_detail 的 known_cwrq）。
                raw, detail = fetch_detail(use_client, row["url"],
                                           known_cwrq=row["cwrq"])
            except Exception as e:  # noqa: BLE001 - 单条失败不影响整批
                failed += 1
                errors.append(f"{row['doc_uid']}: {type(e).__name__}: {e}")
                log.warning("详情页抓取失败 %s: %s", row["url"], e)
                continue

            ok += 1
            rel = store.archive_payload(source_id, f"detail_{row['doc_uid']}", raw)
            store.record_snapshot(
                conn, doc_uid=row["doc_uid"], source_id=source_id,
                http_url=row["url"], kind="detail_html",
                rel_path=str(rel.relative_to(RAW_DIR)),
                payload_hash=hashlib.sha256(raw).hexdigest()[:16],
                size_bytes=len(raw),
            )
            if store.apply_enrichment(conn, row["doc_uid"], detail) == "updated":
                updated += 1
            for att in detail.attachments:
                store.upsert_attachment(conn, row["doc_uid"], att)
                attachments += 1
            conn.commit()
            if ok % 50 == 0:
                log.info("详情页进度 %d/%d（失败 %d）", ok, len(rows), failed)

    return {
        "requested": len(rows), "ok": ok, "failed": failed,
        "updated": updated, "attachments": attachments, "errors": errors,
    }


# ---------------------------------------------------------------- 快照重解析

def reparse_details_from_snapshots(conn, *, limit: int = 0) -> dict:
    """从磁盘上归档的详情页快照重新解析 —— **不联网**。

    用途：解析器改进之后（比如补上了省级站的正文容器选择器、修掉了
    "第一个命中就 break 会挡住真正文"的逻辑），把已经抓回来的页面重跑一遍
    就该生效。为此重新抓 250 多个页面要几分钟，也白给对方站点添流量，
    而原始 HTML 本来就在归档里（raw_snapshot 指向 data/raw 下的 gzip）。

    同一个 doc_uid 会有多份快照（每次抓取都归档），只认最新那份。
    """
    import gzip
    from pathlib import Path

    from .collect.detail import parse_detail
    from .config import RAW_DIR

    rows = conn.execute(
        "SELECT s.rel_path, s.doc_uid, p.url, p.cwrq FROM raw_snapshot s "
        "JOIN policy p ON p.doc_uid = s.doc_uid "
        "WHERE s.kind = 'detail_html' ORDER BY s.id DESC"
    ).fetchall()

    seen: set[str] = set()
    updated = failed = missing = 0
    errors: list[str] = []
    for rel, doc_uid, url, cwrq in rows:
        if doc_uid in seen:
            continue
        seen.add(doc_uid)
        if limit and len(seen) > limit:
            break
        path = Path(RAW_DIR) / rel
        if not path.exists():
            missing += 1
            continue
        try:
            raw = gzip.decompress(path.read_bytes()).decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001 - 单条坏快照不该拖垮整批
            failed += 1
            errors.append(f"{doc_uid}: 快照读取失败 {type(e).__name__}")
            continue
        try:
            detail = parse_detail(raw, base_url=url or "")
            if store.apply_enrichment(conn, doc_uid, detail) == "updated":
                updated += 1
        except Exception as e:  # noqa: BLE001
            failed += 1
            errors.append(f"{doc_uid}: {type(e).__name__}: {e}")
    conn.commit()

    return {
        "scanned": len(seen), "updated": updated, "failed": failed,
        "missing": missing, "errors": errors[:20],
    }


# ---------------------------------------------------------------- 附件

def fetch_attachments(conn, *, limit: int = 20, only_pending: bool = True) -> dict:
    """下载并解析待处理附件（申报表 / 填报说明 / 管理办法）。

    逐条容错；解析结果按状态区分，**扫描件（no_text_layer）不算失败**——
    它需要人工打开或后续 OCR，与"解析器坏了"是两回事。
    """
    from .collect.attachments import download, parse_attachment, safe_filename
    from .config import RAW_DIR

    sql = (
        "SELECT a.id, a.doc_uid, a.url, a.filename, a.ext, p.title "
        "FROM attachment a LEFT JOIN policy p ON p.doc_uid = a.doc_uid "
        "WHERE a.url IS NOT NULL AND a.url <> ''"
    )
    if only_pending:
        # **也重试 unsupported**：解析器升级后（例如接通 WPS 处理老式文档），
        # 当初"读不了"的文件应该再试一次 —— 否则修复永远不会生效，
        # 那 1380 条会一直是 unsupported，而代码明明已经能解析它们了。
        sql += " AND (a.parse_status = 'pending' OR a.parse_status = 'unsupported')"
    sql += " ORDER BY a.id LIMIT ?"
    rows = conn.execute(sql, (limit,)).fetchall()

    stats = {"requested": len(rows), "ok": 0, "no_text_layer": 0,
             "unsupported": 0, "failed": 0, "bytes": 0, "errors": []}
    if not rows:
        return stats

    with GuardedClient() as client:
        for row in rows:
            ext = (row["ext"] or "").lower()
            name = safe_filename(row["filename"] or f"attachment.{ext}", f"attachment.{ext}")
            if ext and not name.lower().endswith(f".{ext}"):
                name = f"{name}.{ext}"
            # doc_uid 可能含冒号（省级源形如 "gd_zcwj:5e05cb6e..."），
            # 而 Windows 文件名不允许冒号 —— 直接当作目录名会抛 NotADirectoryError，
            # 导致省级源的全部附件下载失败（实测踩到）。
            dest = (RAW_DIR / "attachments"
                    / safe_filename(str(row["doc_uid"]), "unknown") / name)
            try:
                size = download(client, row["url"], dest)
            except Exception as e:  # noqa: BLE001
                stats["failed"] += 1
                stats["errors"].append(f"{name}: 下载失败 {type(e).__name__}: {e}")
                conn.execute(
                    "UPDATE attachment SET parse_status='download_failed', parse_error=? WHERE id=?",
                    (f"{type(e).__name__}: {e}"[:300], row["id"]),
                )
                conn.commit()
                continue

            stats["bytes"] += size
            text, status = parse_attachment(dest)
            if status == "ok":
                stats["ok"] += 1
            elif status in ("no_text_layer", "unsupported"):
                stats[status] += 1
                stats["errors"].append(f"{name}: {status}")
            else:
                stats["failed"] += 1
                stats["errors"].append(f"{name}: {status}")

            conn.execute(
                "UPDATE attachment SET raw_path=?, parsed_text=?, parse_status=?, parse_error=NULL"
                " WHERE id=?",
                (str(dest.relative_to(RAW_DIR)), text, status, row["id"]),
            )
            conn.commit()

    return stats


# ---------------------------------------------------------------- 省级源

def collect_provincial(conn, *, source_ids: list[str] | None = None) -> list[dict]:
    """抓取省级静态列表页源。

    与总局源的完整性校验方式不同：省级列表页**不提供 total**，所以
    ``reported_total`` 留空（表示"该源本来就没有总数"），
    完整性由"零条目即抛错"来保障 —— 见 province.parse_list_page。
    """
    from .province import ADAPTERS, build_provincial_row, fetch_list_pages

    adapters = [a for a in ADAPTERS if not source_ids or a.source_id in source_ids]
    out: list[dict] = []

    with GuardedClient() as client:
        for adapter in adapters:
            log_id = store.log_fetch_start(conn, adapter.source_id, "list_page")
            try:
                items, truncated = fetch_list_pages(client, adapter)
            except Exception as e:  # noqa: BLE001 - 单源失败不阻断其它源
                store.log_fetch_finish(
                    conn, log_id, reported_total=None, fetched_count=0,
                    status="failed", error=f"{type(e).__name__}: {e}")
                log.warning("省级源抓取失败 %s: %s", adapter.source_id, e)
                out.append({"source_id": adapter.source_id, "region": adapter.region,
                            "status": "failed", "fetched": 0, "error": str(e)})
                continue

            new = updated = skipped = 0
            for item in items:
                row = build_provincial_row(item, adapter)

                # 跨源去重：同一份文件可能既在总局库、又被省级站转载
                # （实测：广东站转载了多份总局公告，不去重会出现两条标题完全
                # 相同的记录，让人以为系统重复了）。保留先入库的那条。
                #
                # **判重必须连成文日期一起看 —— 同名不等于重复。**
                # 实测：总局库里有 14 条"关于调整增值税纳税申报有关事项的
                # 公告"，是 2011–2026 年间逐年发布的**不同修订版本**（日期、
                # 内容、效力各不相同）。只按标题判重会把它们当成一条；更糟
                # 的是省级站转载的若是较新版本，会被误判成重复而丢弃。
                duplicate = conn.execute(
                    "SELECT 1 FROM policy WHERE title = ? AND doc_uid <> ?"
                    " AND IFNULL(cwrq,'') = IFNULL(?,'') LIMIT 1",
                    (row["title"], row["doc_uid"], row.get("cwrq")),
                ).fetchone()
                if duplicate is not None:
                    skipped += 1
                    continue

                result = store.upsert_policy(conn, row)
                if result == "new":
                    new += 1
                elif result == "updated":
                    updated += 1
            conn.commit()

            # **不再硬编码 "ok"**：超时截断时必须记 incomplete 并写明原因。
            # 否则"只抓了一半"与"抓全了"在 fetch_log 里完全一样，读的人会
            # 以为今天就这么多 —— 而配套的那几条 status='running'（开始了
            # 但从未结束）更是连"结束了没"都看不出来。
            if truncated:
                status = store.log_fetch_finish(
                    conn, log_id, reported_total=None, fetched_count=len(items),
                    new_count=new, updated_count=updated, status="incomplete",
                    error=f"总时长超限（{adapter.max_seconds} 秒），"
                          f"已抓 {len(items)} 条后停止翻页")
            else:
                status = store.log_fetch_finish(
                    conn, log_id, reported_total=None, fetched_count=len(items),
                    new_count=new, updated_count=updated, status="ok")
            out.append({"source_id": adapter.source_id, "region": adapter.region,
                        "status": status, "fetched": len(items),
                        "new": new, "updated": updated,
                        "skipped_duplicates": skipped, "truncated": truncated})

    # 抓完立即判定：否则新入库条目的效力状态会一直停在 'unknown'，
    # 界面上显示"未判定"，看起来像系统坏了（实测发生过）。
    if any(r.get("status") == "ok" for r in out):
        try:
            effect.judge_effects(conn)
        except Exception as e:  # noqa: BLE001 - 判定失败不应丢失抓取结果
            log.warning("抓取后自动判定失败（可稍后手动跑 taxassist judge）：%s", e)
    return out
