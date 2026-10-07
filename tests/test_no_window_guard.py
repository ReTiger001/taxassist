"""守卫：会跑在无控制台环境里的子进程调用，必须带 CREATE_NO_WINDOW。

============================================================
这条测试是怎么来的（实测的教训）
============================================================

翻译进程改由守护以无控制台方式（pythonw + DETACHED）启动后，桌面开始
**不断弹命令窗**。根因是 Windows 的控制台分配规则：

    父进程**没有控制台**时，启动任何**控制台程序**（tasklist、python、
    cmd、UnRAR…）Windows 都会给它新建一个控制台窗口。

而写锁的 ``_pid_alive`` 每批拿锁都要探一次、等锁时每 0.2~2 秒探一次 ——
于是屏幕上不断闪黑框。修法只有一个：创建子进程时带 ``CREATE_NO_WINDOW``。

这里用一条静态检查把坑焊死：**src/taxassist/ 与 scripts/ 下的每个
subprocess 调用都必须带 creationflags**。这两个目录里的代码会被
``taxassist service`` 或翻译守护以无窗口方式拉起。

tools/ 不扫：那些是手动运行的运维脚本，在终端里本来就继承控制台；
start_public / start_funnel 更是**有意**让隧道进程的窗口可见。
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCAN_DIRS = (ROOT / "src" / "taxassist", ROOT / "scripts")

_CALL_RE = re.compile(r"subprocess\.(run|Popen|call)\(")


def _call_spans(text: str) -> list[tuple[int, str]]:
    """每个 subprocess 调用的位置与它的参数文本（按括号配对截取）。"""
    spans: list[tuple[int, str]] = []
    for match in _CALL_RE.finditer(text):
        depth, i = 1, match.end()
        while i < len(text) and depth > 0:
            if text[i] == "(":
                depth += 1
            elif text[i] == ")":
                depth -= 1
            i += 1
        spans.append((match.start(), text[match.end():i]))
    return spans


def test_scanner_actually_finds_calls():
    """先确认扫描器能扫到东西，否则下面的检查会空跑通过。"""
    total = sum(len(_call_spans(p.read_text(encoding="utf-8")))
                for d in SCAN_DIRS for p in d.rglob("*.py"))
    assert total >= 6, f"只扫到 {total} 个 subprocess 调用，扫描器可能坏了"


def test_all_subprocess_calls_pass_creationflags():
    offenders: list[str] = []
    for directory in SCAN_DIRS:
        for path in directory.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for pos, args in _call_spans(text):
                if "creationflags" not in args:
                    line = text[:pos].count("\n") + 1
                    offenders.append(f"{path.relative_to(ROOT)}:{line}")
    assert not offenders, (
        "这些 subprocess 调用没带 creationflags。在无控制台的父进程下"
        "（service.py 或翻译守护拉起的进程），每次调用都会弹出一个命令窗口：\n  "
        + "\n  ".join(offenders)
    )


def test_hidden_flags_follows_platform():
    from taxassist import proc

    if sys.platform.startswith("win"):
        assert proc.hidden_flags() == subprocess.CREATE_NO_WINDOW
        assert proc.hidden_flags() != 0, "Windows 上必须是真实的 CREATE_NO_WINDOW"
    else:
        assert proc.hidden_flags() == 0, "其他平台不该传 Windows 专用标志"


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="tasklist 是 Windows 专用")
def test_writelock_probe_does_not_pop_a_window(monkeypatch):
    """写锁探活是全项目最热的子进程调用点：每批拿锁一次、等锁时每 0.2~2 秒一次。

    它一旦漏掉标志，用户看到的就是"桌面不停闪黑框"。
    """
    from taxassist import proc, writelock

    captured: dict = {}

    class Done:
        stdout = "1234"

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured.update(kwargs)
        return Done()

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert writelock._pid_alive(1234) is True
    assert captured["cmd"][0].endswith("tasklist")
    assert captured.get("creationflags") == proc.hidden_flags()


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="tasklist 是 Windows 专用")
def test_writelock_probe_survives_tasklist_failure(monkeypatch):
    """探活失败必须当"进程已死"处理，不能抛出去把持锁逻辑带崩。"""
    from taxassist import writelock

    def boom(*a, **k):
        raise OSError("tasklist 起不来")

    monkeypatch.setattr(subprocess, "run", boom)
    assert writelock._pid_alive(1234) is False


# ---------------------------------------------------------------- DETACHED 禁令

#: 危险写法：把 DETACHED_PROCESS 传给 creationflags（直接或经常量间接）。
#: 注释/文档里提到它是可以的（要说明为什么不用），所以只匹配代码形态。
_BAD_FORMS = (
    re.compile(r"creationflags\s*=[^#\n]*DETACHED"),
    re.compile(r"^\s*DETACHED\s*=\s*[^\n]*DETACHED_PROCESS", re.M),
)


def test_detached_process_is_not_used():
    """禁止用 DETACHED_PROCESS 代替 CREATE_NO_WINDOW。

    实测（本机 Windows 11，A/B 六种组合）：DETACHED_PROCESS 启动 python 会
    **弹出一个 Windows Terminal 窗口** —— venv launcher 与真 python 都一样；
    单独用会弹、与 CREATE_NO_WINDOW 组合也弹。只有 CREATE_NO_WINDOW 干净。

    而两者在"脱离终端"上等价（CREATE_NO_WINDOW 给的是一份不继承父进程的、
    不可见的控制台），所以 DETACHED 是纯粹的有害项。

    这条测试是在守一个非常容易被"顺手改回去"的决定 —— 那个写法看起来更
    "像"守护进程，实际每次拉起都会在用户桌面上闪一个窗口。
    """
    offenders: list[str] = []
    for directory in (*SCAN_DIRS, ROOT / "tools"):
        for path in directory.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for pattern in _BAD_FORMS:
                for match in pattern.finditer(text):
                    line = text[:match.start()].count("\n") + 1
                    offenders.append(f"{path.relative_to(ROOT)}:{line}")
    assert not offenders, (
        "这些地方用了 DETACHED_PROCESS —— 它在本机会弹出 Windows Terminal 窗口，"
        "请改用 CREATE_NO_WINDOW（见 taxassist/proc.py）：\n  " + "\n  ".join(offenders)
    )
