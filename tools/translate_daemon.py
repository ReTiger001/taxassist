"""翻译守护：让正文翻译不依赖任何终端地一直跑下去。

============================================================
为什么需要它
============================================================

正文还有约 7800 条待译（按当前速度 30+ 小时）。而这个任务此前是由
**某个终端 / AI 会话**拉起的 —— 那个会话一关，Windows 会把控制台里的进程
一起带走，翻译就停了，而且停在哪一条没人会知道。

守护自己不带控制台（用 pythonw 启动），关任何窗口都影响不到它；
它每分钟看一眼翻译在不在，不在就按原参数拉起（全天模式）。

============================================================
三个刻意的设计
============================================================

1. **按命令行认领翻译进程，而不是"谁启动的"。** 翻译可能由守护、bat、或
   某个终端拉起。只认自己启动的那个，就会在"别人起的翻译还在跑"时再拉起
   一个 —— 两个进程抢同一块 GPU 只会都变慢（见 translate_all.py 头部：
   只有一块 GPU，串行才更快）。
2. **没有待译内容时不拉起。** 否则翻译跑完后守护会每分钟空拉一次进程。
   每次查一次库（只读、计数）远比反复起进程便宜。
3. **绝不用 CREATE_NO_WINDOW 之外的窗口方式调外部命令。** 守护自己没有
   控制台，子命令一旦需要控制台就会**弹黑框**——一个每分钟闪一次的黑框
   比什么都招人烦。

用法：
    python tools/translate_daemon.py --start     # 后台启动守护（脱离终端）
    python tools/translate_daemon.py --status    # 看守护与翻译的现状
    python tools/translate_daemon.py --stop      # 停守护（不动正在跑的翻译）
    python tools/translate_daemon.py --run       # 前台运行（调试用）
"""
from __future__ import annotations

import argparse
import ctypes
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
LOG_DIR = DATA / "logs"
DB = DATA / "taxassist.db"
DAEMON_LOG = LOG_DIR / "translate_daemon.log"
TRANSLATE_LOG = LOG_DIR / "translate_run.log"
PID_FILE = DATA / "translate_daemon.pid"
PY = ROOT / ".venv" / "Scripts" / "python.exe"
PYW = ROOT / ".venv" / "Scripts" / "pythonw.exe"

CHECK_INTERVAL = 60          # 秒。翻译掉了最多一分钟内被拉起。
STILL_ACTIVE = 259           # GetExitCodeProcess 的「还活着」常量

#: 拉起子进程用的标志。**只用 CREATE_NO_WINDOW，绝不要 DETACHED_PROCESS。**
#:
#: 实测（本机 Windows 11，A/B 六组组合都跑过）：用 DETACHED_PROCESS 启动
#: python 会**弹出一个 Windows Terminal 窗口** —— venv launcher 与真 python
#: 都一样；单独用会弹、与 CREATE_NO_WINDOW 组合也弹。只有 CREATE_NO_WINDOW
#: 不弹。两者的"脱离终端"效果等价（CREATE_NO_WINDOW 给的是一份不继承父进程
#: 的、不可见的控制台），所以没有任何理由再用 DETACHED。
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

log = logging.getLogger("translate_daemon")


def _setup_logging(to_console: bool) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [
        logging.FileHandler(DAEMON_LOG, encoding="utf-8")]
    # pythonw 下 sys.stdout 是 None，加 StreamHandler 会直接抛异常
    if to_console and sys.stdout is not None:
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S",
                        handlers=handlers, force=True)


# ---------------------------------------------------------------- 进程工具

def _pid_alive(pid: int) -> bool:
    """进程是否还活着。

    不用 ``os.kill(pid, 0)``：Windows 上它发的是 CTRL_C_EVENT 而不是
    "探活"，会真的去打断目标进程。走 OpenProcess 才是只读的。
    """
    if pid <= 0:
        return False
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def find_translate_pids() -> list[int]:
    """命令行里含 translate_all / translate_batch 的 python 进程。

    用命令行而不是 PID 文件：翻译是谁拉起的都能认领，避免重复拉起
    （见模块头部第 1 条）。PowerShell 查询约 1 秒，一分钟一次可忽略。
    """
    script = (
        "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
        "Where-Object { $_.CommandLine -match 'translate_all|translate_batch' } | "
        "Select-Object -ExpandProperty ProcessId"
    )
    try:
        done = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=90, creationflags=NO_WINDOW)
    except Exception as e:  # noqa: BLE001 - 查不到不能影响守护循环
        log.warning("查询翻译进程失败：%s", e)
        return []
    return [int(x) for x in (done.stdout or "").split() if x.strip().isdigit()]


def pending_content() -> int | None:
    """还有多少条正文没译。查不到时返回 None（当作"有待译"处理）。

    条件是**高估**的（只看有没有对应译文记录，不比对原文指纹是否变过）：
    高估只会多拉起一次进程，低估却会让翻译无声地停在这里。
    """
    try:
        import sqlite3

        conn = sqlite3.connect(f"{DB.resolve().as_uri()}?mode=ro", uri=True, timeout=30)
        try:
            return conn.execute(
                "SELECT COUNT(*) FROM policy p"
                " WHERE LENGTH(COALESCE(p.content,'')) > 0"
                "   AND NOT EXISTS (SELECT 1 FROM translation t"
                "                   WHERE t.doc_uid = p.doc_uid AND t.field = 'content')"
            ).fetchone()[0]
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001
        log.warning("查待译数失败：%s", e)
        return None


def latest_translation_at() -> str | None:
    try:
        import sqlite3

        conn = sqlite3.connect(f"{DB.resolve().as_uri()}?mode=ro", uri=True, timeout=30)
        try:
            row = conn.execute(
                "SELECT MAX(created_at) FROM translation WHERE field = 'content'"
            ).fetchone()
            return row[0] if row else None
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------- 拉起翻译

def start_translate() -> int | None:
    """脱离终端拉起翻译，输出追加到 data/logs/translate_run.log。"""
    if not PY.exists():
        log.error("找不到 %s，无法拉起翻译", PY)
        return None
    batch = ROOT / "scripts" / "translate_all.py"
    if not batch.exists():
        log.error("找不到 %s", batch)
        return None

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    # 子进程要能写日志，所以句柄不能在 Popen 之后立刻关掉自己的副本前失效；
    # Popen 会持有它自己的副本，父进程 close 掉自己这份即可。
    # **句柄要在 Popen 期间有效**：Popen 会复制一份给子进程，父进程这份
    # 用完即关（不关的话守护每拉一次翻译就漏一个句柄）。
    # 用 with 而不是 try/finally：两者关闭时机完全一致，但 with 不留"忘了关"
    # 或"关两次"的余地。
    with open(TRANSLATE_LOG, "ab", buffering=0) as handle:
        try:
            proc = subprocess.Popen(
                [str(PY), str(batch), "--anytime"],
                cwd=str(ROOT), stdin=subprocess.DEVNULL,
                stdout=handle, stderr=subprocess.STDOUT,
                creationflags=NO_WINDOW)
        except Exception as e:  # noqa: BLE001
            log.error("拉起翻译失败：%s", e)
            return None
    return proc.pid


# ---------------------------------------------------------------- 守护本体

def _read_pid() -> int | None:
    try:
        return int(PID_FILE.read_text(encoding="utf-8").strip())
    except Exception:  # noqa: BLE001
        return None


def daemon_pid() -> int | None:
    """守护自己的 PID（文件里的那个，且确认还活着）。"""
    pid = _read_pid()
    return pid if pid and _pid_alive(pid) else None


def _tick() -> None:
    pids = find_translate_pids()
    if pids:
        log.info("翻译在跑（PID %s）", ", ".join(str(p) for p in pids))
        return
    pending = pending_content()
    if pending == 0:
        log.info("没有待译正文，本轮不拉起")
        return
    pid = start_translate()
    if pid:
        log.info("翻译不在跑（待译 %s 条），已拉起 PID %s", pending, pid)


def run_daemon() -> int:
    existing = daemon_pid()
    if existing and existing != os.getpid():
        log.error("已有守护在跑（PID %s），本进程退出", existing)
        return 1

    DATA.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    log.info("守护启动，PID %s，每 %s 秒检查一次", os.getpid(), CHECK_INTERVAL)
    try:
        while True:
            try:
                _tick()
            except Exception:  # noqa: BLE001 - 单轮出错绝不能让守护退出
                log.exception("本轮检查出错（守护继续）")
            time.sleep(CHECK_INTERVAL)
    except KeyboardInterrupt:
        log.info("收到 Ctrl-C，退出")
    finally:
        if _read_pid() == os.getpid():
            PID_FILE.unlink(missing_ok=True)
    return 0


# ---------------------------------------------------------------- 命令行

def start_background() -> int:
    existing = daemon_pid()
    if existing:
        print(f"守护已在运行（PID {existing}），不重复启动。")
        return 0
    if not PYW.exists():
        print(f"找不到 {PYW}")
        return 1

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    # 守护的 stdout/stderr 必须落到文件，**不能是 DEVNULL**：
    # pythonw 没有控制台，未捕获的异常默认会消失在虚空里。上一版就是
    # DEVNULL，结果守护静默消失过一次，什么线索都没留下、只能靠猜。
    err_path = LOG_DIR / "translate_daemon.err.log"
    with open(err_path, "ab", buffering=0) as err_handle:
        subprocess.Popen([str(PYW), str(Path(__file__).resolve()), "--run"],
                         cwd=str(ROOT), stdin=subprocess.DEVNULL,
                         stdout=err_handle, stderr=subprocess.STDOUT,
                         creationflags=NO_WINDOW, close_fds=True)
    # 出 with 时父进程那份已关；子进程持有自己的副本（上面注释说的就是这件事）

    # 等它把 PID 文件写出来，好确认真的起来了（而不是静默失败）
    for _ in range(25):
        time.sleep(0.3)
        pid = daemon_pid()
        if pid:
            print(f"守护已启动（PID {pid}，无窗口，关终端不受影响）。")
            print(f"  日志：{DAEMON_LOG}")
            print("  查看：python tools/translate_daemon.py --status")
            return 0
    print(f"守护似乎没起来，请查看日志：{DAEMON_LOG} 与 {err_path}")
    return 1


def stop_daemon() -> int:
    pid = daemon_pid()
    if not pid:
        print("守护没有在运行（PID 文件里的进程已不存在）。")
        PID_FILE.unlink(missing_ok=True)
        return 0
    subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                   capture_output=True, text=True, creationflags=NO_WINDOW)
    PID_FILE.unlink(missing_ok=True)
    print(f"守护 PID {pid} 已停止。正在跑的翻译进程不受影响，会继续译完。")
    return 0


def show_status() -> int:
    if sys.stdout is not None:
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass

    dpid = daemon_pid()
    pids = find_translate_pids()
    pending = pending_content()
    latest = latest_translation_at()
    # 拼好再放进 f-string：原来写成 f"...{'…%s' % dpid if dpid else '…'}" ——
    # 同一个字符串里混两种格式化，难读也难改。
    daemon_state = f"运行中（PID {dpid}）" if dpid else "未运行"

    print("=" * 62)
    print(f"守护进程   {daemon_state}")
    print(f"翻译进程   {', '.join(str(p) for p in pids) if pids else '没有在跑'}")
    print(f"正文待译   {pending} 条" if pending is not None else "正文待译   查询失败")
    print(f"最近译文   {latest or '（无记录）'}")
    print("-" * 62)
    print(f"守护日志   {DAEMON_LOG}")
    print(f"翻译输出   {TRANSLATE_LOG}")
    if not dpid:
        print("\n守护未运行。启动：python tools/translate_daemon.py --start")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="翻译守护：脱离终端、掉了自动拉起")
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--start", action="store_true", help="后台启动守护（脱离终端）")
    group.add_argument("--run", action="store_true", help="前台运行守护（调试用）")
    group.add_argument("--status", action="store_true", help="查看守护与翻译的现状")
    group.add_argument("--stop", action="store_true", help="停止守护（不影响正在跑的翻译）")
    args = ap.parse_args()

    if args.start:
        _setup_logging(to_console=True)
        return start_background()
    if args.run:
        _setup_logging(to_console=True)
        return run_daemon()
    if args.status:
        _setup_logging(to_console=False)
        return show_status()
    return stop_daemon()


if __name__ == "__main__":
    raise SystemExit(main())
