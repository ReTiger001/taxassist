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

锁文件里记 PID 与用途；若持有者进程已不存在（崩溃 / 被 kill），
视为过期锁可抢占 —— 否则一次崩溃会让系统永久停摆。
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from .config import DATA_DIR

LOCK_FILE = DATA_DIR / "write.lock"


def _pid_alive(pid: int) -> bool:
    """进程是否还活着（Windows 用 tasklist，POSIX 用 kill -0）。"""
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            import subprocess
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True, timeout=10).stdout
            return str(pid) in out
        os.kill(pid, 0)
        return True
    except Exception:  # noqa: BLE001 - 判断失败一律当"已死"，避免死锁
        return False


def holder() -> str | None:
    """当前持有者描述（``pid:who``），无人持有时返回 None 并清理过期锁。"""
    if not LOCK_FILE.exists():
        return None
    try:
        raw = LOCK_FILE.read_text(encoding="utf-8").strip()
        pid_s, _, who = raw.partition(":")
        pid = int(pid_s)
    except Exception:  # noqa: BLE001 - 文件损坏当成过期
        LOCK_FILE.unlink(missing_ok=True)
        return None
    if not _pid_alive(pid):
        LOCK_FILE.unlink(missing_ok=True)
        return None
    return raw


def acquire(who: str, *, timeout: float = 0) -> bool:
    """尝试取得写锁。``timeout`` 秒内没拿到就放弃返回 False。

    拿不到锁**不是错误** —— 调用方应该跳过本轮而不是报错退出：
    守候循环下一轮还会再来。
    """
    deadline = time.time() + max(0.0, timeout)
    while True:
        cur = holder()
        if cur is None:
            LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = LOCK_FILE.with_suffix(f".{os.getpid()}.tmp")
            tmp.write_text(f"{os.getpid()}:{who}", encoding="utf-8")
            try:
                # 原子替换：避免两个进程同时写进同一个锁文件
                os.replace(tmp, LOCK_FILE)
                return True
            except OSError:
                tmp.unlink(missing_ok=True)
        if time.time() >= deadline:
            return False
        time.sleep(min(2.0, max(0.2, deadline - time.time())))


def release(who_owned_by: int | None = None) -> None:
    """释放写锁。默认只释放自己持有的那把。"""
    pid = os.getpid() if who_owned_by is None else who_owned_by
    try:
        raw = LOCK_FILE.read_text(encoding="utf-8").strip()
        if raw.partition(":")[0] == str(pid):
            LOCK_FILE.unlink(missing_ok=True)
    except FileNotFoundError:
        pass
