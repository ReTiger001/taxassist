"""写库互斥锁：让长时间写库的任务彼此让路。

======================================================================
为什么需要
======================================================================

翻译正文一条要 20 秒，攒批提交的窗口可达百秒；而 fetch / publish 阶段
也在写同一个 SQLite。两者并发时实测撞过多次：

    sqlite3.OperationalError: database is locked
    （worker 在 init_db 的 DROP TRIGGER 上、在 record_snapshot 上都撞过）

已经修过三处缓解（提交间隔 20→5 条、init_db 加退避重试、
translate_llm.save 支持 commit=False），但**根因不是重试不够**：
SQLite 没有跨进程的"写锁租约"，长事务窗口一旦超过别人的
``busy_timeout``（120s），对方就是**等待超时失败**，重试只是把失败推迟。

而正文翻译本来也不必与抓取并发 —— 一块 GPU 只跑得动一路翻译。
所以用文件锁让它们**串行**：

    翻译进程   acquire("writer", timeout=600) 成功后才开工
    worker     每轮 acquire("writer", timeout=0)，拿不到就跳过本轮

======================================================================
为什么是「心跳租约」而不是只判 PID
======================================================================

原实现只校验 PID（进程不在了就视为过期锁）。这挡住了"崩溃 / 被 kill"，
**挡不住"进程活着但卡死"** —— 死循环、卡在某个系统调用、被挂起，都让
进程"存在但不动"。实测有过 worker 卡死 10 小时仍握着写锁、整个系统停摆。

所以锁里带上**心跳时间戳**：持有者定期刷新，超过租约时长没刷新就视为
过期，别人可以抢占。判据变成两条，满足任一即抢占：

    ① 进程已不存在   （原逻辑）
    ② 心跳超时       （新逻辑）

为什么选"抢占"而不是"等它自己恢复"：写库是关键路径，一个卡死的持有者
挡住的是整个系统；而误抢的代价最多是让一个真卡死的进程白跑（它本来就
不会再写成功）。

======================================================================
持有者的义务
======================================================================

拿到锁之后**必须定期调 ``heartbeat()``**（建议每 ``HEARTBEAT_SEC`` 秒一次，
租约是它的 3 倍，容忍两次丢拍）。不刷心跳的后果是**锁会被别人抢走** ——
比不刷更糟。长时间任务应在每个批次/阶段结束时刷一次。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time

from .config import DATA_DIR

log = logging.getLogger(__name__)

LOCK_FILE = DATA_DIR / "write.lock"

#: 租约时长（秒）：心跳超过它没刷新，就视为持有者已卡死，可被抢占。
#: 取 5 分钟的依据：写库任务的单次"不刷心跳"间隔应当短于它 ——
#: worker 在阶段之间刷、翻译每批提交后刷，都能轻松满足。
LEASE_SEC = 300.0

#: 建议的心跳间隔（秒）。租约的 1/3，容忍两次丢拍。
HEARTBEAT_SEC = 100.0

#: 后台心跳线程。``acquire`` 成功后自动启动，``release`` 时停止。
#:
#: **为什么要自动刷而不是让调用方自己刷**：心跳漏刷的后果是**锁被别人
#: 抢走**，于是两个进程同时写库 —— 那正是这把锁要防的事，比多一个守护
#: 线程严重得多。而 worker 的 ``run_round`` 一跑就是十几分钟、中间还有
#: 若干不写库的等待，指望每个调用方都记得"每 100 秒刷一次"不现实。
_beat_thread: threading.Thread | None = None
_beat_stop = threading.Event()


def _beat_loop() -> None:
    """后台刷心跳，直到被要求停止、或发现自己已不持锁。"""
    pid = os.getpid()
    while not _beat_stop.wait(HEARTBEAT_SEC):
        if not heartbeat(pid):
            # 锁已不在自己手里（被抢了或已被释放）→ 停刷并让调用方知道
            log.warning("心跳刷不上：锁已不在本进程手里，停止心跳线程")
            return


def _start_beat() -> None:
    global _beat_thread
    _beat_stop.clear()
    if _beat_thread is not None and _beat_thread.is_alive():
        return
    _beat_thread = threading.Thread(target=_beat_loop, daemon=True,
                                    name="writelock-beat")
    _beat_thread.start()


def _pid_alive(pid: int) -> bool:
    """进程是否还活着（Windows 用 tasklist，POSIX 用 kill -0）。

    **注意它只回答"进程在不在"，不回答"还活着吗"** —— 后者要靠心跳。

    ``creationflags`` 不是可有可无的：调用方（翻译进程）是无控制台启动的，
    没这个标志时每次探活都会**弹出一个命令窗口**（见 taxassist.proc 头部）。
    这里是全项目最热的一条探活路径 —— 每批拿锁一次、等锁时每 0.2~2 秒一次，
    漏掉的后果就是桌面不停闪黑框。
    """
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            import subprocess

            from . import proc
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True, timeout=10,
                creationflags=proc.hidden_flags()).stdout
            return str(pid) in out
        os.kill(pid, 0)
        return True
    except Exception:  # noqa: BLE001 - 判断失败一律当"已死"，避免死锁
        return False


def _read() -> dict | None:
    """读锁文件。返回 ``{"pid", "who", "beat"}``；不存在或读不出返回 None。

    用 JSON 而不是原来的 ``pid:who`` 纯文本：``who`` 里本来就带冒号
    （``worker:fetch,publish``），再拼上心跳段就分不清哪段是哪段。
    顺带也解决了旧格式的迁移 —— 解析失败一律当"过期"，见 ``holder()``。
    """
    if not LOCK_FILE.exists():
        return None
    try:
        info = json.loads(LOCK_FILE.read_text(encoding="utf-8"))
        return {"pid": int(info["pid"]),
                "who": str(info.get("who") or ""),
                "beat": float(info.get("beat") or 0.0)}
    except Exception:  # noqa: BLE001 - 损坏或旧格式，交给 holder() 当过期处理
        return None


def _write(info: dict) -> None:
    """原子写锁文件：先写临时文件再 replace，避免两个进程写进同一份。"""
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = LOCK_FILE.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    try:
        os.replace(tmp, LOCK_FILE)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def holder() -> str | None:
    """当前持有者描述（``pid:who``）；无有效持有者时返回 None 并清理过期锁。

    两条过期判据（任一满足即抢占）：
      ① 进程已不存在  —— 崩溃 / 被 kill
      ② 心跳超时      —— 进程存在但卡死（新增，见模块头部说明）
    """
    info = _read()
    if info is None:
        # 文件在但读不出 → 损坏或旧格式（没有 beat 字段）。一律当过期：
        # 改完这版代码后新进程写的都是新格式，旧格式只可能来自旧进程，
        # 而旧进程跑的是没有心跳的代码，抢占它是对的。
        if LOCK_FILE.exists():
            log.warning("写锁文件无法解析（旧格式或损坏），按过期处理并清理")
            LOCK_FILE.unlink(missing_ok=True)
        return None
    if not _pid_alive(info["pid"]):
        log.warning("写锁持有者进程 %d 已不存在，清理过期锁", info["pid"])
        LOCK_FILE.unlink(missing_ok=True)
        return None
    stale = time.time() - info["beat"]
    if stale > LEASE_SEC:
        log.warning("写锁心跳超时（%.0f 秒未刷新，超过租约 %.0f 秒），"
                    "视为持有者卡死，抢占：pid=%d who=%s",
                    stale, LEASE_SEC, info["pid"], info["who"])
        LOCK_FILE.unlink(missing_ok=True)
        return None
    return f"{info['pid']}:{info['who']}"


def _claim(info: dict) -> bool:
    """**独占创建**锁文件 —— 抢锁只能有一个赢家。

    为什么不能"先查再写"：原实现是「``holder()`` 返回 None → ``_write()``」。
    两个进程同时抢锁时，**两边都可能看到 None**，然后都写、都返回 True ——
    于是两个写库任务并行跑，而这正是这把锁唯一要防的事。窗口只有毫秒级，
    但 acquire 每天被调用几十次（工作流每步 + 人工命令 + 调度），迟早会撞上。

    独占创建把裁决权交给内核：``O_CREAT|O_EXCL`` 在文件已存在时必然失败，
    所以"谁先创建成功谁持有"是原子的。拿不到就回到调用方的循环里重新判断 ——
    那时 ``holder()`` 会看到对方写的那个持有者，该等就等。
    """
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(info, ensure_ascii=False))
    except OSError:
        # 写失败要撤掉自己的占位，否则那把锁变成一个谁都读不出的空文件
        LOCK_FILE.unlink(missing_ok=True)
        raise
    return True


def acquire(who: str, *, timeout: float = 0) -> bool:
    """尝试取得写锁。``timeout`` 秒内没拿到就放弃返回 False。

    拿不到锁**不是错误** —— 调用方应该跳过本轮而不是报错退出：
    守候循环下一轮还会再来。
    """
    deadline = time.time() + max(0.0, timeout)
    while True:
        # holder() 顺带清理过期锁（进程没了 / 心跳超时），返回 None 表示
        # "眼下没有有效持有者"；随后用**独占创建**去抢。这两步之间可能被
        # 别人抢先 —— 那时 _claim 返回 False，循环回去就会看到对方。
        if holder() is None:
            try:
                if _claim({"pid": os.getpid(), "who": who, "beat": time.time()}):
                    _start_beat()      # 自动续租，调用方不必记得刷心跳
                    return True
            except OSError as exc:
                log.warning("写锁落盘失败（%s），本轮放弃", exc)
                return False
        if time.time() >= deadline:
            return False
        time.sleep(min(2.0, max(0.2, deadline - time.time())))


def heartbeat(who_owned_by: int | None = None) -> bool:
    """刷新心跳。**持有者必须定期调用**，否则锁会被别人抢走。

    返回 True 表示刷新成功；False 表示锁已不在自己手里（被别人抢了或
    文件没了）—— 调用方见到 False 应当**停止当前工作**，不要再写库。
    """
    pid = os.getpid() if who_owned_by is None else who_owned_by
    info = _read()
    if info is None or info["pid"] != pid:
        return False
    info["beat"] = time.time()
    try:
        _write(info)
        return True
    except OSError:
        return False


def release(who_owned_by: int | None = None) -> None:
    """释放写锁。默认只释放自己持有的那把。"""
    pid = os.getpid() if who_owned_by is None else who_owned_by
    if pid == os.getpid():
        # 先停心跳线程再删文件：反过来的话线程可能在删除后又把文件写回去，
        # 于是"已释放"的锁复活，别人再也拿不到。
        _beat_stop.set()
    info = _read()
    if info is not None and info["pid"] == pid:
        LOCK_FILE.unlink(missing_ok=True)
