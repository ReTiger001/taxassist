"""工作流守护：Ollama 或工作流挂了就自动拉起。

## 为什么需要

2026-10-17 ~ 18 期间 **Ollama 被外部终止过三次**（10-17 23:24、10-18 04:07、
以及更早一次），每次都连带 `auto_workflow` 一起死掉，最长一次空了 **11 小时
20 分钟**才被发现。Ollama 日志里没有任何 error/panic —— 它只是"消失"了
（内存压力下被系统回收或被误关），所以工作流那边表现为"连接失败后卡住"。

本脚本每 5 分钟巡检一次，只做两件事：

  - Ollama 的 11434 不可达  → 重启 `ollama serve`（带 OLLAMA_MODELS）
  - 工作流进程不在          → 重启 `auto_workflow`

**它不改任何数据**。修复结果是逐条即时提交的，所以重启不丢进度。

## 它不会做的事（刻意）

- 不杀进程、不动数据库、不碰别的项目（`D:\\Klab` 与本项目无关）
- 不重复启动：工作流自己有单实例闸门，多启动一次会被它自己拒绝；
  守护自己也有 PID 闸门，避免两个守护互相踩

用法（后台常驻，脱离控制台）::

    .venv\\Scripts\\python.exe scripts\\guardian.py
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = ROOT / ".venv" / "Scripts" / "python.exe"
OLLAMA_EXE = Path(r"D:\longvideocrater\Ollama\ollama.exe")
OLLAMA_MODELS = r"D:\models"
LOG_DIR = ROOT / "data" / "logs"
GUARD_PID = ROOT / "data" / "worker_locks" / "guardian.pid"
WF_PID = ROOT / "data" / "worker_locks" / "auto_workflow.pid"
OLLAMA_URL = "http://127.0.0.1:11434/api/tags"
CHECK_SEC = 300

sys.path.insert(0, str(ROOT / "src"))
from taxassist import proc as procutil  # noqa: E402

LOG_DIR.mkdir(parents=True, exist_ok=True)
log = logging.getLogger("guardian")
log.setLevel(logging.INFO)
_fh = logging.FileHandler(LOG_DIR / "guardian.log", encoding="utf-8")
_fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
log.addHandler(_fh)
_sh = logging.StreamHandler()
_sh.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
log.addHandler(_sh)


def _pid_alive(pid: int) -> bool:
    """复用 writelock 的实现（已处理 Windows tasklist / POSIX kill 差异）。"""
    from taxassist import writelock

    return writelock._pid_alive(pid)


def ollama_alive(timeout: float = 5.0) -> bool:
    try:
        with urllib.request.urlopen(OLLAMA_URL, timeout=timeout):
            return True
    except Exception:  # noqa: BLE001 - 任何异常都当不可用
        return False


def workflow_pid() -> int | None:
    """工作流在跑则返回 PID；PID 文件是残留（进程已死）则返回 None。"""
    if not WF_PID.exists():
        return None
    try:
        pid = int(WF_PID.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    return pid if _pid_alive(pid) else None


def start_ollama() -> None:
    env = {**os.environ, "OLLAMA_MODELS": OLLAMA_MODELS, "OLLAMA_KEEP_ALIVE": "30m"}
    # 文件句柄要交给子进程长期持有，不能用 with（with 会在 Popen 后立刻关闭它）
    out = open(LOG_DIR / "ollama.out.log", "a", encoding="utf-8", errors="replace")  # noqa: SIM115
    err = open(LOG_DIR / "ollama.err.log", "a", encoding="utf-8", errors="replace")  # noqa: SIM115
    subprocess.Popen(
        [str(OLLAMA_EXE), "serve"], cwd=str(ROOT), env=env,
        stdout=out, stderr=err, creationflags=procutil.hidden_flags(),
    )
    log.info("已重启 ollama serve（OLLAMA_MODELS=%s）", OLLAMA_MODELS)


def start_workflow() -> None:
    # 用追加模式打开日志：`auto_workflow` 自己的 print 走 stdout
    fh = open(LOG_DIR / "auto_workflow.log", "a", encoding="utf-8", errors="replace")  # noqa: SIM115
    subprocess.Popen(
        [str(PY), "scripts/auto_workflow.py", "--hours", "0-24"],
        cwd=str(ROOT), stdout=fh, stderr=subprocess.STDOUT,
        creationflags=procutil.hidden_flags(),
    )
    log.info("已重启 auto_workflow")


def acquire_guard() -> bool:
    """守护自身单实例（否则多个守护会同时拉起工作流）。"""
    GUARD_PID.parent.mkdir(parents=True, exist_ok=True)
    if GUARD_PID.exists():
        try:
            pid = int(GUARD_PID.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            pid = 0
        if pid and pid != os.getpid() and _pid_alive(pid):
            return False
        GUARD_PID.unlink(missing_ok=True)
    GUARD_PID.write_text(str(os.getpid()), encoding="utf-8")
    return True


def release_guard() -> None:
    try:
        if GUARD_PID.exists() and GUARD_PID.read_text(encoding="utf-8").strip() == str(os.getpid()):
            GUARD_PID.unlink(missing_ok=True)
    except OSError:
        pass


def main() -> int:
    if not acquire_guard():
        log.warning("已有守护在运行，本次退出")
        return 0
    log.info("守护启动（PID %d，每 %d 秒巡检一次）", os.getpid(), CHECK_SEC)
    try:
        while True:
            try:
                if not ollama_alive():
                    log.warning("Ollama 不可达 → 重启")
                    start_ollama()
                    # 给 Ollama 一点启动时间，下一轮再判工作流
                    time.sleep(30)
                    continue
                if workflow_pid() is None:
                    log.warning("工作流不在 → 重启")
                    start_workflow()
                    time.sleep(30)
                    continue
            except Exception as e:  # noqa: BLE001 - 守护自身不能死
                log.exception("巡检异常：%s", e)
            time.sleep(CHECK_SEC)
    except KeyboardInterrupt:
        log.info("守护收到中断，退出")
    finally:
        release_guard()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
