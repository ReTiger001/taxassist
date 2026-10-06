"""后台定时任务：每日抓取、开机补抓、失败告警。

======================================================================
为什么"开机补抓"是必需的（而不是可选项）
======================================================================

这台电脑不是服务器，**不会 7×24 开着**。如果只在每天固定时刻跑一次任务，
关机那天就永久漏掉那天的政策 —— 而且你不会知道漏了，因为界面上
"那天没有新政策"和"那天没跑任务"看起来完全一样。

所以设计成三件事：

1. **每次程序启动先检查、必要时补抓**：读上次成功抓取到哪天，
   距今超过 1 天就把整段窗口重抓一遍。
2. **重叠窗口**：增量默认回溯 7 天，靠 doc_uid 去重 —— 多抓不会出错，
   漏抓才会。宁可重复劳动，不可静默缺失。
3. **告警显式化**：抓取不完整（fetched < reported）或失败时写入 fetch_log，
   并在 Web 首页顶部显示横幅。**不做静默降级。**
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from . import db as dbmod
from . import effect, pipeline, store, writelock

log = logging.getLogger(__name__)

DAILY_JOB_ID = "taxassist_daily"
META_LAST_RUN = "last_daily_run"
META_LAST_STATUS = "last_daily_status"


def last_success_window_end(conn) -> date | None:
    """最近一次**完整成功**抓取覆盖到哪一天（数据只到这天为止）。"""
    row = conn.execute(
        "SELECT MAX(window_end) w FROM fetch_log WHERE status = 'ok'"
    ).fetchone()
    if not row or not row["w"]:
        return None
    try:
        return datetime.fromisoformat(str(row["w"])[:10]).date()
    except ValueError:
        return None


def fetch_health(conn) -> dict:
    """抓取健康状态：供 Web 首页横幅与 CLI 使用。"""
    last_ok = conn.execute(
        "SELECT started_at, window_end FROM fetch_log WHERE status='ok'"
        " ORDER BY id DESC LIMIT 1").fetchone()
    bad = conn.execute(
        "SELECT COUNT(*) c FROM fetch_log WHERE status IN ('incomplete','failed')"
        " AND started_at >= datetime('now', '-7 days')").fetchone()["c"]
    recent_bad = conn.execute(
        "SELECT source_id, status, fetched_count, reported_total, error, started_at"
        " FROM fetch_log WHERE status IN ('incomplete','failed')"
        " ORDER BY id DESC LIMIT 3").fetchall()
    return {
        "last_ok_at": last_ok["started_at"] if last_ok else None,
        "last_window_end": last_ok["window_end"] if last_ok else None,
        "bad_last_7d": bad,
        "recent_bad": [dict(r) for r in recent_bad],
    }


def days_since_last_success(conn) -> int | None:
    last = last_success_window_end(conn)
    return None if last is None else (date.today() - last).days


# ---------------------------------------------------------------- 日更任务

def run_daily(conn=None, *, enrich_limit: int = 200, days: int = 7,
              archive: bool = True, include_provincial: bool = True) -> dict:
    """跑一轮完整日更：抓取 → 详情页 → 附件 → 效力判定。

    任一步失败都不吞掉异常，最终状态写入 meta，供界面与 CLI 查询。
    """
    # **日更也是一条写库路径，必须和 worker / provincial 子命令抢同一把锁。**
    # 不拿的后果实测过（2026-10-06 07:30）：8765 与 8772 各自带一个 scheduler，
    # 同一时刻触发两个日更，加上正在跑的 worker —— 三方并发写库，
    # ``sqlite3.OperationalError: database is locked`` 必然出现。更糟的是它发生在
    # publish 阶段：同一次日更前面 fetch 抓的 150 个源全白跑。
    #
    # timeout 取 0（拿不到就跳过本轮、不排队）：日更天天有，今天被 worker 占着
    # 就等明天，没必要让两个长任务串成一条链互相等。
    if not writelock.acquire("scheduler:daily"):
        busy = writelock.holder() or "未知任务"
        log.info("写库锁被 %s 占用，跳过本次日更（明天照常）", busy)
        return {"started_at": dbmod.now_iso(), "steps": {}, "ok": False,
                "skipped_by": busy}

    own = conn is None
    conn = conn or dbmod.connect()
    dbmod.init_db(conn)
    started = dbmod.now_iso()
    result: dict = {"started_at": started, "steps": {}, "ok": False}

    try:
        collected = pipeline.collect_incremental(conn, days=days, archive=archive)
        bad = [r for r in collected if r["status"] != "ok"]
        result["steps"]["collect"] = {
            "windows": len(collected),
            "fetched": sum(r["fetched"] for r in collected),
            "new": sum(r["new"] for r in collected),
            "incomplete": len(bad),
        }

        # 省级源：列表页在加速乐 WAF 后面必须走浏览器；它们不提供总数，
        # 完整性靠"零条目即抛错"保障（见 province.parse_list_page）。
        # 单源失败只记不抛，但要出现在 result 里，不能静默。
        # include_provincial=False 时不碰省级 —— 测试要能跑日更而不去抓网络，
        # 生产上也能在被站点限流时临时关掉这一路。
        if include_provincial:
            provincial = pipeline.collect_provincial(conn)
            result["steps"]["provincial"] = {
                "sources": len(provincial),
                "fetched": sum(r.get("fetched", 0) for r in provincial),
                "new": sum(r.get("new", 0) for r in provincial),
                "failed": len([r for r in provincial if r["status"] != "ok"]),
            }

        result["steps"]["enrich"] = pipeline.enrich_details(conn, limit=enrich_limit)
        result["steps"]["attach"] = pipeline.fetch_attachments(conn, limit=100)
        result["steps"]["judge"] = effect.judge_effects(conn)

        # 校对阶段复用 worker 的实现（不联网）：用归档快照重解析 + 补文号/施行日。
        # 两处逻辑必须是一份，否则"日更跑的校对"和"worker 跑的校对"会慢慢分叉。
        from .worker import stage_verify  # noqa: PLC0415 - 避免与 worker 循环导入

        result["steps"]["verify"] = stage_verify(conn, {"days": days})

        result["steps"]["translate"] = _translate_incremental(conn)

        result["ok"] = len(bad) == 0
        status = "ok" if result["ok"] else "incomplete"
    except Exception as e:  # noqa: BLE001 - 必须记录后抛出，不允许静默
        result["error"] = f"{type(e).__name__}: {e}"
        status = "failed"
        _record(conn, started, status, result)
        if own:
            conn.close()
        writelock.release()
        raise

    _record(conn, started, status, result)
    if own:
        conn.close()
    writelock.release()
    log.info("日更完成 status=%s %s", status, result["steps"])
    return result


def _record(conn, started: str, status: str, result: dict) -> None:
    dbmod.set_meta(conn, META_LAST_RUN, started)
    dbmod.set_meta(conn, META_LAST_STATUS, status)
    conn.commit()


# ---------------------------------------------------------------- 补抓

def catch_up(conn=None, *, max_days: int = 90) -> dict | None:
    """启动时补抓：若数据落后于今天，把整段窗口重抓一遍。

    返回补抓结果；不需要补抓时返回 None。
    重叠窗口 + doc_uid 去重保证重复抓取是安全的。
    """
    own = conn is None
    conn = conn or dbmod.connect()
    gap = days_since_last_success(conn)

    if gap is None:
        log.info("尚无成功抓取记录，执行首次增量抓取")
        days = 30
    elif gap <= 1:
        if own:
            conn.close()
        return None
    else:
        days = min(gap + 7, max_days)   # +7 天重叠，防边界遗漏
        log.warning("检测到数据落后 %d 天，执行开机补抓（窗口 %d 天）", gap, days)

    results = pipeline.collect_incremental(conn, days=days)
    summary = {
        "gap_days": gap,
        "window_days": days,
        "fetched": sum(r["fetched"] for r in results),
        "new": sum(r["new"] for r in results),
        "incomplete": sum(1 for r in results if r["status"] != "ok"),
    }
    if own:
        conn.close()
    return summary


# ---------------------------------------------------------------- 调度器

def _translate_incremental(conn, *, limit: int = 200) -> dict:
    """给新入库的政策补译文。

    为什么必须接进日更：库每天新增政策，若不补译，双语库会慢慢退化成中英混杂
    —— 昨天点开还有英文的栏目，今天多出一条纯中文，比一开始就没有英文更让人
    困惑，也更难发现是"漏译"还是"本来就没有"。

    限量 200 条/天：翻译要占 GPU，不能让它把日更流程卡死。
    模型未就绪时返回说明而不是报错 —— ollama 没开不该让整次日更失败。
    """
    from . import translate_llm as tl

    ready, why = tl.is_available(tl.DEFAULT_MODEL)
    if not ready:
        log.warning("跳过增量翻译：%s", why)
        return {"skipped": why}

    tl.ensure_table(conn)
    rows = conn.execute(
        "SELECT doc_uid, title FROM policy ORDER BY cwrq DESC LIMIT ?",
        (limit,)).fetchall()

    done = failed = 0
    for r in rows:
        src = r["title"] or ""
        if not src.strip():
            continue
        if tl.cached(conn, r["doc_uid"], "title", src) is not None:
            continue
        try:
            out = tl.translate(src, model=tl.DEFAULT_MODEL)
            tl.save(conn, r["doc_uid"], "title", src, out, model=tl.DEFAULT_MODEL)
            done += 1
        except Exception as e:  # noqa: BLE001 - 单条失败不该中断日更
            failed += 1
            log.warning("增量翻译失败 %s: %s", r["doc_uid"], e)
    return {"translated": done, "failed": failed}


def start_background_scheduler(hour: int = 7, minute: int = 30):
    """启动后台调度（供 Web 服务内嵌使用）。

    用 BackgroundScheduler 而不是 BlockingScheduler：Web 服务本身要常驻，
    调度只是它的一个后台线程，不需要独立进程。
    """
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger

    conn = dbmod.connect()
    try:
        catch_up(conn)          # 启动即补抓，不等定时点
    finally:
        conn.close()

    sched = BackgroundScheduler(timezone="Asia/Shanghai")
    sched.add_job(
        _job_run_daily, CronTrigger(hour=hour, minute=minute),
        id=DAILY_JOB_ID, replace_existing=True,
        misfire_grace_time=3600,      # 错过 1 小时内仍执行（笔记本睡眠常见）
        coalesce=True,                # 多次错过只补跑一次
    )
    sched.start()
    log.info("后台调度已启动：每日 %02d:%02d 执行日更，启动时已补抓", hour, minute)
    return sched


def _job_run_daily() -> None:
    """调度触发的日更任务。异常必须记录，不能让它静默杀掉调度线程。"""
    try:
        run_daily()
    except Exception:  # noqa: BLE001
        log.exception("日更任务失败（已记录到 fetch_log，请查看 taxassist status）")
