"""一条命令管住整套系统：`taxassist service start` / `stop` / `status`。

**为什么需要它**：以前启动要双击 `启动Taxassist.bat` —— 它开三个控制台窗口，
关窗口就是停服务，而且没有一个地方能问"现在到底跑没跑、跑得怎么样"。
这个模块把 web 与 worker 变成**无窗口、可停止、可查状态**的托管进程：

    taxassist service start     起（已经在跑的不重复起）
    taxassist service stop      停（按 PID 精准收工，不误杀别的 python）
    taxassist service status    看（端口 / PID / HTTP 探活 / 日志尾部）

**ollama 不在此模块启动**：它的可执行文件路径因机器而异（本机是
``D:\\longvideocrater\\Ollama\\ollama.exe``），把这种本机路径写进仓库既不对也不
安全。这里的做法是**检测 11434 端口**：没在跑就提示用哪个 bat 起，或者让使用者
设 ``TAXASSIST_OLLAMA_EXE`` 环境变量，那时本模块代劳并同样隐藏窗口。

**PID 与日志都放 ``data/logs/``**：``data/`` 已在 .gitignore 里（含 ``*.log``），
这些运行时状态不该进仓库。

**Windows 上的一个坑（这里刻意避开）**：``os.kill(pid, 0)`` 在 POSIX 是"探活"，
在 Windows 却会**真的终止那个进程** —— Windows 没有信号语义，CPython 把它映射成
TerminateProcess。所以下面探活用 ``tasklist``、停止用 ``taskkill``，平台分支写清楚。
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE.parent.parent              # 仓库根（D:\EY-project）
LOG_DIR = ROOT / "data" / "logs"
WEB_PORT_DEFAULT = 8765
OLLAMA_PORT = 11434
IS_WIN = sys.platform.startswith("win")


# ------------------------------------------------------------------ 基础探针

def _port_open(port: int, host: str = "127.0.0.1") -> bool:
    """端口在监听吗。0.4 秒超时 —— 本机探测不需要更长。"""
    with socket.socket() as s:
        s.settimeout(0.4)
        return s.connect_ex((host, port)) == 0


def _pid_file(name: str) -> Path:
    return LOG_DIR / f"service-{name}.pid"


def _read_pid(name: str) -> int | None:
    p = _pid_file(name)
    if not p.is_file():
        return None
    try:
        return int(p.read_text(encoding="ascii").strip())
    except (ValueError, OSError):
        return None


def _alive(pid: int | None) -> bool:
    """那个 PID 还活着吗。

    **Windows 上不能用 os.kill(pid, 0)** —— 它会真的杀掉进程（Windows 没有
    信号语义，CPython 把 signal 0 也映射成 TerminateProcess）。故此处走
    tasklist；POSIX 才用 os.kill 的 0 号信号。
    """
    if not pid:
        return False
    if IS_WIN:
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
                capture_output=True, text=True, errors="replace",
                timeout=8,
                creationflags=subprocess.CREATE_NO_WINDOW).stdout
        except (OSError, subprocess.SubprocessError):
            return False
        return str(pid) in out
    try:
        os.kill(pid, 0)          # POSIX：0 号信号只探活，不递送
    except OSError:
        return False
    return True


def _kill(pid: int) -> bool:
    """终止进程及其子进程。返回是否成功发出。"""
    if IS_WIN:
        try:
            r = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                               capture_output=True, text=True, errors="replace",
                               timeout=15,
                               creationflags=subprocess.CREATE_NO_WINDOW)
        except (OSError, subprocess.SubprocessError):
            return False
        return r.returncode == 0
    import signal
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return False
    return True


def _hidden_popen(name: str, argv: list[str]) -> int:
    """把子进程无窗口地拉起来，日志与 PID 都落到 data/logs/。

    **只用 CREATE_NO_WINDOW，不能加 DETACHED_PROCESS。** 原先两个一起用，
    但实测（本机 Windows 11）DETACHED_PROCESS 会让 python 子进程**弹出一个
    Windows Terminal 窗口** —— 单独用会弹、与 NO_WINDOW 组合也弹。
    CREATE_NO_WINDOW 本身就给了子进程一份不继承自本进程的、不可见的控制台，
    "关掉终端服务照样跑"它已经满足，DETACHED 是多余且有害的。
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    # 这两行**故意不接 with**：句柄要留给子进程当 stdout/stderr，本函数返回后
    # 不能关（关了子进程就没法写日志了）。生命周期由操作系统在进程退出时回收。
    out = open(  # noqa: SIM115
        LOG_DIR / f"{name}.out.log", "a", encoding="utf-8", buffering=1)
    err = open(  # noqa: SIM115
        LOG_DIR / f"{name}.err.log", "a", encoding="utf-8", buffering=1)
    flags = 0
    if IS_WIN:
        flags = subprocess.CREATE_NO_WINDOW
    proc = subprocess.Popen(argv, cwd=str(ROOT), stdout=out, stderr=err,
                            stdin=subprocess.DEVNULL, creationflags=flags,
                            close_fds=True)
    _pid_file(name).write_text(str(proc.pid), encoding="ascii")
    return proc.pid


def _http_ok(port: int, timeout: float = 4.0) -> bool:
    """HTTP 层探活：端口开着不等于服务能答（可能还在启动）。"""
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health",
                                    timeout=timeout) as r:
            return r.status == 200
    except Exception:      # noqa: BLE001 - 探活失败的任何原因都算"没起来"
        return False


def _tail(path: Path, n: int = 3) -> list[str]:
    if not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return [ln for ln in lines[-n:] if ln.strip()]


# ------------------------------------------------------------------ 三个动作

def start(*, port: int = WEB_PORT_DEFAULT, expose: bool = False) -> int:
    """起 web 与 worker（已经在跑的不重复起）。"""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    py = sys.executable
    started: list[str] = []
    skipped: list[str] = []

    # --- ollama：只检测，不擅自启动（路径是本机私有的） ---
    if _port_open(OLLAMA_PORT):
        print(f"  [ok]   ollama 已在 {OLLAMA_PORT} 上运行")
    else:
        exe = os.environ.get("TAXASSIST_OLLAMA_EXE", "").strip()
        if exe and Path(exe).is_file():
            models = os.environ.get("TAXASSIST_OLLAMA_MODELS", "").strip()
            env = dict(os.environ)
            if models:
                env["OLLAMA_MODELS"] = models
            env.setdefault("OLLAMA_KEEP_ALIVE", "30m")
            _hidden_popen("ollama", [exe, "serve"])
            print("  [起]   ollama（来自 TAXASSIST_OLLAMA_EXE）")
            started.append("ollama")
        else:
            print(f"  [!]    ollama 没在跑（{OLLAMA_PORT} 无响应）")
            print("        助手与翻译会不可用。启动它：双击 启动Ollama.bat，")
            print("        或设 TAXASSIST_OLLAMA_EXE 后由本命令代劳。")

    # --- web ---
    if _port_open(port):
        print(f"  [ok]   web 已在 {port} 上监听（跳过）")
        skipped.append("web")
    else:
        argv = [py, "-m", "taxassist", "serve", "--port", str(port)]
        if expose:
            argv.append("--expose")
        pid = _hidden_popen("web", argv)
        started.append(f"web(pid {pid})")
        print(f"  [起]   web  -> http://127.0.0.1:{port}/   pid {pid}")

    # --- worker ---
    wpid = _read_pid("worker")
    if _alive(wpid):
        print(f"  [ok]   worker 已在运行（pid {wpid}，跳过）")
        skipped.append("worker")
    else:
        # 与 launcher 一致：**不带 translate** —— 翻译有 11:00-19:00 时间窗
        # 约束（scripts/translate_all.py），worker 的 translate 阶段不受它管。
        pid = _hidden_popen("worker", [
            py, "-m", "taxassist", "worker", "--stage", "fetch,publish,verify"])
        started.append(f"worker(pid {pid})")
        print(f"  [起]   worker pid {pid}")

    print()
    if started:
        print(f"已启动 {len(started)} 项。稍等几秒再 status —— web 需要一点时间 bind。")
    else:
        print("全部已在运行，未做改动。")
    print(f"日志：{LOG_DIR}")
    return 0


def stop(*, port: int = WEB_PORT_DEFAULT, wait: float = 12.0) -> int:
    """停掉 web 与 worker（按 PID，只动我们起的那些）。"""
    stopped: list[str] = []

    # --- worker：优先请求优雅停止（它有 --stop，会收工当前轮） ---
    wpid = _read_pid("worker")
    if _alive(wpid):
        # encoding 必须显式写 utf-8：worker 的输出里有中文，而 text=True 在中文
        # Windows 上按 GBK 解码 —— 实测直接抛 UnicodeDecodeError，stop 就断在
        # 收输出这一步（PID 与日志都已处理，但用户看到的是满屏 traceback）。
        # errors="replace" 再兜一层：将来输出里出现任何非 UTF-8 字节也不会崩。
        subprocess.run([sys.executable, "-m", "taxassist", "worker", "--stop"],
                       cwd=str(ROOT), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=20,
                       creationflags=(subprocess.CREATE_NO_WINDOW if IS_WIN else 0))
        deadline = time.time() + wait
        while time.time() < deadline and _alive(wpid):
            time.sleep(0.5)
        if _alive(wpid):                       # 优雅没成，只好硬停
            _kill(wpid)
            time.sleep(1.0)
        _pid_file("worker").unlink(missing_ok=True)
        stopped.append(f"worker(pid {wpid})")
        print(f"  [停]   worker pid {wpid}")
    else:
        print("  [ok]   worker 没在跑")

    # --- web：没有优雅停止协议，按 PID 停自己起的那个 ---
    wpid = _read_pid("web")
    if _alive(wpid):
        _kill(wpid)
        deadline = time.time() + 8
        while time.time() < deadline and _port_open(port):
            time.sleep(0.4)
        _pid_file("web").unlink(missing_ok=True)
        stopped.append(f"web(pid {wpid})")
        print(f"  [停]   web pid {wpid}")
    elif _port_open(port):
        # 端口有人但 PID 不是我们的 —— 多半是 bat 或手工起的，别替用户杀
        print(f"  [!]   {port} 上有人在听，但不是本命令启动的（没有 PID 记录）")
        print("        那是 启动Taxassist.bat 或手工进程 —— 请关它的窗口，我不替您杀。")
    else:
        print("  [ok]   web 没在跑")

    print()
    print(f"已停止 {len(stopped)} 项。" if stopped else "没有需要停的。")
    return 0


def status(*, port: int = WEB_PORT_DEFAULT) -> int:
    """一眼看清：谁在跑、日志尾部说了什么。"""
    print("taxassist 运行状态")
    print("=" * 46)

    # ollama
    if _port_open(OLLAMA_PORT):
        print(f"  ollama   [运行]  端口 {OLLAMA_PORT}")
    else:
        print("  ollama   [未运行]  助手与翻译不可用")

    # web
    if _port_open(port):
        pid = _read_pid("web")
        who = f"pid {pid}" if _alive(pid) else "非本命令启动"
        ok = _http_ok(port)
        print(f"  web      [运行]  端口 {port}（{who}）"
              f"  /health {'200' if ok else '无响应'}")
        if not ok:
            print("           端口开着但 /health 不通 —— 可能还在启动，或已卡住")
    else:
        print(f"  web      [未运行]  端口 {port} 无监听")

    # worker
    wpid = _read_pid("worker")
    if _alive(wpid):
        print(f"  worker   [运行]  pid {wpid}")
    else:
        print("  worker   [未运行]  抓取/校对/上架不会自己推进")

    print()
    for name in ("ollama", "web", "worker"):
        for stream in ("out", "err"):
            for ln in _tail(LOG_DIR / f"{name}.{stream}.log", 2):
                print(f"  {name}.{stream}: {ln[:110]}")
    print()
    print(f"日志目录：{LOG_DIR}")
    return 0
