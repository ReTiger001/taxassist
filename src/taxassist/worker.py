"""后台工作者：把「抓取 / 校对 / 翻译 / 上架」交给独立进程长跑。

==============================================================================
为什么要有它
==============================================================================

这四件事都是长跑：一次省级抓取几分钟、正文翻译几十小时。靠人（或 AI）一条条
手动触发，既费神又费 token —— 更糟的是一旦没人盯着，库就停在原地不动。

所以把每件事做成一个**守候阶段**：独立进程、按轮跑、失败不中断、中断可续。
人只需要偶尔看一眼 `--status`。

==============================================================================
四个阶段
==============================================================================

| 阶段 | 干什么 | 为什么单列 |
| --- | --- | --- |
| fetch | 总局增量 + 31 个省级源的列表 | 先解决"有没有新政策" |
| publish | enrich 详情页（正文/文号/日期/附件）+ judge 效力判定 | 不跑它，新条目在界面上一片"未知" |
| verify | reparse 重解析归档快照 + backfill 补文号/施行日 + 覆盖率体检 | 解析器或规则改进后，用存量数据把结论补齐 |
| translate | 标题 + 正文译成英文（**限量/轮**） | 占 GPU、极慢，不限量会把前三阶段饿死 |

顺序有讲究：**先入库、再补全、最后翻译** —— 翻译最贵，只翻已经定稿的记录。

==============================================================================
命令
==============================================================================

    python -m taxassist worker --stage all          # 四阶段轮转，守着跑
    python -m taxassist worker --stage fetch        # 只跑抓取
    python -m taxassist worker --stage all --once   # 只跑一轮（适合计划任务）
    python -m taxassist worker --status             # 现在跑到哪了
    python -m taxassist worker --stop               # 优雅停止

==============================================================================
几条硬约束（踩过坑才写上的）
==============================================================================

1. **单实例**：同一阶段同时只允许一个进程跑。否则两个进程争 SQLite 写锁，
   表现是"两边都卡住" —— 实测翻译与效力判定互相等锁，各自慢十倍以上。
   用 PID 锁文件实现，进程死了锁自动失效。
2. **不吞异常**：每阶段的异常记进状态文件后继续下一阶段，但必须能在
   `--status` 里看到。悄悄失败比失败更糟。
3. **翻译限量**：默认每轮 300 条。不限量的话它一轮跑几十小时，其余阶段
   永远轮不上。
4. **优雅停止**：靠 data/worker.stop 文件。Windows 上信号不可靠，
   文件是唯一跨进程稳定的做法。
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from . import db as dbmod
from . import effect, pipeline
from .config import DATA_DIR

log = logging.getLogger(__name__)

STATUS_FILE = DATA_DIR / "worker_status.json"
STOP_FILE = DATA_DIR / "worker.stop"
LOCK_DIR = DATA_DIR / "worker_locks"
#: 解析器（detail.py / normalize.py）的改动指纹，决定要不要重跑 reparse
FP_FILE = DATA_DIR / "parser_fingerprint.txt"


def _parser_fingerprint() -> str:
    """detail.py 与 normalize.py 有没有改过。

    用文件大小 + mtime 拼一个短哈希 —— 比读全文便宜，判断"解析逻辑是否变过"
    够用。全量重解析 5400 条要 27 秒，每轮都跑等于每天白烧二十多分钟。
    """
    import hashlib
    from pathlib import Path as _P

    from .collect import detail, normalize

    parts: list[str] = []
    for mod in (detail, normalize):
        p = _P(getattr(mod, "__file__", "") or "")
        try:
            st = p.stat()
            parts.append(f"{p.name}:{st.st_size}:{int(st.st_mtime)}")
        except OSError:
            parts.append(f"{p.name}:?")
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:12]

#: 阶段顺序：先入库、再补全、最后翻译
STAGE_ORDER: tuple[str, ...] = ("fetch", "publish", "verify", "translate")

#: 默认参数，都能被 CLI 覆盖
DEFAULTS: dict[str, Any] = {
    "days": 7,               # 总局增量窗口（天）
    "enrich_limit": 200,     # 每轮最多补多少条详情页
    "translate_limit": 300,  # 每轮最多译多少条 —— 必须限量（见模块头部第 3 条）
}


# ---------------------------------------------------------------- 单实例锁

def _pid_alive(pid: int) -> bool:
    """判断进程是否还活着。跨平台，且不依赖 psutil。"""
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION,
                                          False, pid)
            if not handle:
                return False
            kernel32.CloseHandle(handle)
            return True
        os.kill(pid, 0)
        return True
    except Exception:  # noqa: BLE001 - 判活失败一律当"已死"，宁可接管也别卡住
        return False


def acquire_lock(stage: str) -> bool:
    """占住某个阶段的单实例锁；已被活进程占着就返回 False。"""
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    path = LOCK_DIR / f"{stage}.pid"
    if path.exists():
        try:
            pid = int(path.read_text(encoding="utf-8").strip())
        except (ValueError, OSError):
            pid = 0
        if _pid_alive(pid):
            return False
        log.warning("阶段 %s 的锁属于已退出的进程 %s，接管", stage, pid)
    path.write_text(str(os.getpid()), encoding="utf-8")
    return True


def release_lock(stage: str) -> None:
    path = LOCK_DIR / f"{stage}.pid"
    try:
        if path.exists() and path.read_text(encoding="utf-8").strip() == str(os.getpid()):
            path.unlink()
    except OSError:
        pass


# ---------------------------------------------------------------- 四个阶段

def _stage_fetch(conn, cfg: dict) -> dict:
    """抓取：总局增量 + 全部省级源。"""
    collected = pipeline.collect_incremental(conn, days=cfg["days"], archive=True)
    incomplete = [r for r in collected if r["status"] != "ok"]

    provincial = pipeline.collect_provincial(conn)
    prov_failed = [r["source_id"] for r in provincial if r["status"] != "ok"]

    return {
        "总局窗口": len(collected),
        "总局抓取": sum(r["fetched"] for r in collected),
        "总局新增": sum(r["new"] for r in collected),
        "总局不完整": len(incomplete),
        "省级源数": len(provincial),
        "省级抓取": sum(r.get("fetched", 0) for r in provincial),
        "省级新增": sum(r.get("new", 0) for r in provincial),
        "省级失败源": prov_failed,
    }


def _stage_publish(conn, cfg: dict) -> dict:
    """上架：补详情页 → 附件 → 效力判定，让新条目可读、可判定、可检索。"""
    enrich = pipeline.enrich_details(conn, limit=cfg["enrich_limit"])
    attach = pipeline.fetch_attachments(conn, limit=100)
    judged = effect.judge_effects(conn)
    return {
        "详情页请求": enrich["requested"],
        "详情页成功": enrich["ok"],
        "详情页失败": enrich["failed"],
        "附件成功": attach.get("ok", 0),
        "判定条数": judged.get("judged", 0),
    }


def stage_verify(conn, cfg: dict) -> dict:
    """校对：用归档快照重解析 + 补文号与施行日 + 覆盖率体检。

    这三件都不联网，跑起来很快，所以每轮都跑 —— 它们兜住的是"解析器改进了
    但存量数据还是旧结论"这类问题。
    """
    from . import backfill

    # 重解析只在解析器改过时才跑（见 _parser_fingerprint）；改过或首次则跑完记指纹。
    fp = _parser_fingerprint()
    try:
        last_fp = FP_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        last_fp = ""
    if last_fp == fp and not cfg.get("force_reparse"):
        reparsed = {"scanned": 0, "updated": 0, "failed": 0, "missing": 0}
        note = "解析器未改动，跳过重解析"
    else:
        reparsed = pipeline.reparse_details_from_snapshots(conn)
        try:
            FP_FILE.write_text(fp, encoding="utf-8")
        except OSError:
            pass
        note = None

    filled = backfill.backfill_from_content(conn)
    cover = backfill.coverage(conn)
    out = {
        "重解析扫描": reparsed["scanned"],
        "重解析更新": reparsed["updated"],
        "重解析失败": reparsed["failed"],
        "补出文号": filled.get("doc_no_filled", 0),
        "补出施行日": filled.get("effective_date_filled", 0),
        "覆盖率": cover,
    }
    if note:
        out["说明"] = note
    return out


def _stage_translate(conn, cfg: dict) -> dict:
    """翻译：标题 + 正文译成英文。**严格限量**，否则会饿死其它阶段。"""
    from . import translate_llm as tl

    limit = cfg["translate_limit"]
    ready, why = tl.is_available(tl.DEFAULT_MODEL)
    if not ready:
        return {"跳过": why}

    tl.ensure_table(conn)
    done = skipped = failed = 0
    last_log = time.time()

    # 标题：短、便宜，优先补齐（缺标题译文最容易被用户看到）
    rows = conn.execute(
        "SELECT doc_uid, title FROM policy WHERE title IS NOT NULL "
        "AND TRIM(title) <> '' ORDER BY cwrq DESC LIMIT ?", (limit,)).fetchall()
    for r in rows:
        if done >= limit:
            break
        src = r["title"] or ""
        if tl.cached(conn, r["doc_uid"], "title", src) is not None:
            skipped += 1
            continue
        try:
            out = tl.translate(src, model=tl.DEFAULT_MODEL)
            # 攒批提交：每条一 commit 会让翻译与其它阶段频繁争抢 SQLite 写锁
            # （实测两边都慢十倍）。每 20 条落一次盘。
            tl.save(conn, r["doc_uid"], "title", src, out,
                    model=tl.DEFAULT_MODEL, commit=False)
            done += 1
            if done % 20 == 0:
                conn.commit()
        except Exception as e:  # noqa: BLE001 - 单条失败不该中断整轮
            failed += 1
            log.warning("标题翻译失败 %s: %s", r["doc_uid"], e)
        # 进度按**时间**打点，不按条数：单条翻译要几十秒，按条数打点会让日志
        # 长时间完全静默（实测 15 分钟一行没有），看起来像卡死 —— 这次就因此
        # 误判过一回。
        now = time.time()
        if now - last_log >= 60:
            log.info("翻译进度：已译 %d / 跳过 %d / 失败 %d", done, skipped, failed)
            last_log = now
    conn.commit()

    return {"已译": done, "跳过": skipped, "失败": failed,
            "模型": tl.DEFAULT_MODEL}


STAGES: dict[str, Callable[[Any, dict], dict]] = {
    "fetch": _stage_fetch,
    "publish": _stage_publish,
    "verify": stage_verify,
    "translate": _stage_translate,
}


# ---------------------------------------------------------------- 状态与停止

def write_status(payload: dict) -> None:
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATUS_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8")


def read_status() -> dict:
    if not STATUS_FILE.exists():
        return {"回合": 0, "阶段": {}, "说明": "还没跑过"}
    try:
        return json.loads(STATUS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return {"错误": f"状态文件读不出来：{type(e).__name__}: {e}"}


def request_stop() -> None:
    # 目录可能还不存在（首次跑就要求停止）—— 少了这行 write_text 会抛
    # FileNotFoundError，而"停止"失败是最不该发生的失败。
    STOP_FILE.parent.mkdir(parents=True, exist_ok=True)
    STOP_FILE.write_text(datetime.now().isoformat(timespec="seconds"),
                         encoding="utf-8")


def clear_stop() -> None:
    try:
        STOP_FILE.unlink()
    except OSError:
        pass


def stop_requested() -> bool:
    return STOP_FILE.exists()


# ---------------------------------------------------------------- 主循环

def run_round(conn, stages: tuple[str, ...], cfg: dict) -> dict:
    """跑一轮：按顺序执行各阶段，单个阶段出错不影响其余。"""
    out: dict[str, dict] = {}
    for stage in stages:
        if stop_requested():
            out[stage] = {"跳过": "收到停止请求"}
            continue
        if not acquire_lock(stage):
            out[stage] = {"跳过": "已有同类 worker 在跑（单实例锁）"}
            continue
        started = time.time()
        try:
            out[stage] = STAGES[stage](conn, cfg)
        except Exception as e:  # noqa: BLE001 - 记下来继续，不能悄悄失败
            log.exception("阶段 %s 失败", stage)
            out[stage] = {"错误": f"{type(e).__name__}: {e}"}
        finally:
            release_lock(stage)
        out[stage]["耗时秒"] = round(time.time() - started, 1)
    return out


def run_forever(*, stages: tuple[str, ...] = STAGE_ORDER,
                interval_sec: int = 1800, max_rounds: int | None = None,
                cfg: dict | None = None) -> dict:
    """守候循环：一轮接一轮地跑，直到收到停止请求或达到轮数上限。"""
    cfg = {**DEFAULTS, **(cfg or {})}
    clear_stop()
    rounds = 0
    stopped_early = False
    while max_rounds is None or rounds < max_rounds:
        if stop_requested():
            stopped_early = True
            break
        rounds += 1
        started_at = datetime.now().isoformat(timespec="seconds")
        log.info("=== 第 %d 轮开始 %s（阶段 %s）===", rounds, started_at, stages)
        conn = dbmod.connect()
        dbmod.init_db(conn)
        try:
            result = run_round(conn, stages, cfg)
        finally:
            conn.close()
        write_status({
            "回合": rounds,
            "开始": started_at,
            "结束": datetime.now().isoformat(timespec="seconds"),
            "阶段": result,
            "参数": cfg,
            "循环阶段": list(stages),
        })
        log.info("=== 第 %d 轮结束：%s ===", rounds, result)
        if max_rounds is not None and rounds >= max_rounds:
            break
        if stop_requested():
            stopped_early = True
            break
        # 睡眠切成小段，这样 --stop 能在一分钟内被听见
        for _ in range(max(1, interval_sec)):
            if stop_requested():
                stopped_early = True
                break
            time.sleep(1)
    return {"回合": rounds, "提前停止": stopped_early}
