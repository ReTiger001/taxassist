"""CLI 冒烟测试：命令入口必须真的能跑通。

======================================================================
守的是一个刚发生的事故
======================================================================

给 `cli.main()` 加日志落盘时我用了 `Path` 却没 import，结果**所有 CLI 命令
全线 NameError** —— `status`、`collect`、`serve` 全崩，明天早上的日更会静默失败。
而**单元测试全绿**，因为它们直接调用被测试的函数，从不经过 main()
的参数解析与初始化。

一句话教训：**入口函数里的代码，只有真的从入口跑一遍才算测过。**

为什么不用 `--help` 测：argparse 遇到 --help 会立即退出，
根本走不到后面的初始化代码 —— 这个事故它照不出来。
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = ROOT / ".venv" / "Scripts" / "python.exe"
if not PY.exists():                      # 非 Windows 或未建 venv 时退回当前解释器
    PY = Path(sys.executable)


def _run(*args: str, timeout: int = 180) -> subprocess.CompletedProcess:
    # 必须显式指定 UTF-8：中文 Windows 下 text=True 默认按 GBK 解码，而 CLI 输出
    # 是 UTF-8。解码异常发生在 subprocess 的读线程里，把 stdout 变成 None ——
    # 表面症状是 "argument of type 'NoneType' is not iterable"，真错因藏在读线程。
    # 这是本项目第三次栽在 GBK/UTF-8 上（前两次：中文写进 .bat、命令行解析中文输出）。
    return subprocess.run([str(PY), "-m", "taxassist", *args],
                          capture_output=True, text=True,
                          encoding="utf-8", errors="replace",
                          cwd=str(ROOT), timeout=timeout)


def test_status_runs_through_main():
    """跑真实命令，走完整的 main() 初始化路径。

    这是唯一能照出"入口阶段 NameError"的测法 —— `--help` 会在
    argparse 阶段就退出，测不到初始化代码。
    """
    r = _run("status")
    assert r.returncode == 0, f"status 崩了：\nSTDOUT:\n{r.stdout[-500:]}\nSTDERR:\n{r.stderr[-1500:]}"
    assert "政策总数" in r.stdout


def test_help_lists_all_commands():
    """顺带确认命令注册没被破坏。"""
    r = _run("--help")
    assert r.returncode == 0, r.stderr[-800:]
    for cmd in ("collect", "enrich", "attach", "judge", "serve", "status"):
        assert cmd in r.stdout, f"{cmd} 未出现在 --help 里"


def test_search_runs():
    """search 也要经过 main()，一并冒烟。"""
    r = _run("search", "增值税")
    assert r.returncode == 0, f"search 崩了：\n{r.stderr[-1500:]}"
