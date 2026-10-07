"""翻译守护的行为测试。

守护是「防下线」的唯一保障：它错了，翻译停了不会有任何人知道。
所以这里锁住的是**决策逻辑**（什么时候该拉起、什么时候不该），
而不是去真的启动进程 —— 测试绝不能碰真翻译（会抢 GPU）。

用例覆盖四种处境：已在跑 / 没在跑且有待译 / 没在跑且没待译 / 查不到待译数。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def daemon():
    """加载 tools/translate_daemon.py（它不在包内，用文件方式导入）。"""
    path = ROOT / "tools" / "translate_daemon.py"
    spec = importlib.util.spec_from_file_location("translate_daemon_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------- 决策逻辑

def test_tick_does_not_start_when_already_running(daemon, monkeypatch):
    """翻译在跑时绝不能再拉一个 —— 两个进程抢一块 GPU 只会都变慢。"""
    monkeypatch.setattr(daemon, "find_translate_pids", lambda: [111, 222])
    started: list = []
    monkeypatch.setattr(daemon, "start_translate", lambda: started.append(True))
    daemon._tick()
    assert started == []


def test_tick_starts_when_idle_with_pending(daemon, monkeypatch):
    """翻译掉了且还有待译 —— 这正是「掉了自动拉起」要生效的场合。"""
    monkeypatch.setattr(daemon, "find_translate_pids", list)
    monkeypatch.setattr(daemon, "pending_content", lambda: 42)
    started: list = []
    monkeypatch.setattr(daemon, "start_translate", lambda: started.append(True) or 12345)
    daemon._tick()
    assert started == [True]


def test_tick_does_not_start_when_nothing_pending(daemon, monkeypatch):
    """全部译完之后不能再每分钟空拉一次进程。"""
    monkeypatch.setattr(daemon, "find_translate_pids", list)
    monkeypatch.setattr(daemon, "pending_content", lambda: 0)
    started: list = []
    monkeypatch.setattr(daemon, "start_translate", lambda: started.append(True))
    daemon._tick()
    assert started == []


def test_tick_starts_when_pending_count_unknown(daemon, monkeypatch):
    """查不到待译数时按「有待译」处理。

    高估只会多拉起一次（翻译自己会跳过已译的，很快就退出）；
    低估却会让翻译无声地停在那里 —— 两种错的代价不对称。
    """
    monkeypatch.setattr(daemon, "find_translate_pids", list)
    monkeypatch.setattr(daemon, "pending_content", lambda: None)
    started: list = []
    monkeypatch.setattr(daemon, "start_translate", lambda: started.append(True) or 1)
    daemon._tick()
    assert started == [True]


# ---------------------------------------------------------------- 进程工具

def test_pid_alive_rejects_bogus_pids(daemon):
    """不存在的 PID 必须是 False，不能抛异常（守护循环里任何异常都很危险）。"""
    assert daemon._pid_alive(0) is False
    assert daemon._pid_alive(-1) is False
    assert daemon._pid_alive(999_999_999) is False


def test_pid_alive_true_for_self(daemon):
    import os

    assert daemon._pid_alive(os.getpid()) is True


def test_find_translate_pids_parses_powershell_output(daemon, monkeypatch):
    class Result:
        stdout = "1234\n5678\n"

    monkeypatch.setattr(daemon.subprocess, "run", lambda *a, **k: Result())
    assert daemon.find_translate_pids() == [1234, 5678]


def test_find_translate_pids_survives_failure(daemon, monkeypatch):
    """PowerShell 不可用/超时时返回空列表，绝不能把守护带崩。"""
    def boom(*a, **k):
        raise OSError("powershell 起不来")

    monkeypatch.setattr(daemon.subprocess, "run", boom)
    assert daemon.find_translate_pids() == []


def test_find_translate_pids_ignores_non_numeric_noise(daemon, monkeypatch):
    class Result:
        stdout = "警告：某行噪音\n1234\n\n  5678  \n"

    monkeypatch.setattr(daemon.subprocess, "run", lambda *a, **k: Result())
    assert daemon.find_translate_pids() == [1234, 5678]


# ---------------------------------------------------------------- 待译计数

def test_pending_content_returns_none_when_db_missing(daemon, monkeypatch, tmp_path):
    """库不存在时返回 None（而不是 0）—— 0 的含义是「译完了、别拉了」，
    与「我查不到」完全不同，混淆这两个会让翻译静默停摆。"""
    monkeypatch.setattr(daemon, "DB", tmp_path / "不存在.db")
    assert daemon.pending_content() is None


# ---------------------------------------------------------------- 启动前提

def test_scripts_and_interpreter_exist(daemon):
    """守护要拉起的脚本与解释器确实在（路径写错的话守护永远拉不起翻译）。"""
    assert daemon.PY.exists(), f"找不到 {daemon.PY}"
    assert daemon.PYW.exists(), f"找不到 {daemon.PYW}"
    assert (daemon.ROOT / "scripts" / "translate_all.py").exists()


def test_start_translate_uses_anytime(daemon, monkeypatch, tmp_path):
    """拉起参数必须是 --anytime：用户选的就是全天模式。

    这条守的是一个具体决定：默认带时间窗（11-19）会在窗口外一直等，
    看上去就像"防下线没生效"。
    """
    captured: dict = {}

    class FakeProc:
        pid = 4242

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return FakeProc()

    monkeypatch.setattr(daemon, "TRANSLATE_LOG", tmp_path / "run.log")
    monkeypatch.setattr(daemon.subprocess, "Popen", fake_popen)
    pid = daemon.start_translate()

    assert pid == 4242
    assert "--anytime" in captured["cmd"]
    assert captured["cmd"][1].endswith("translate_all.py")
    # 拉起时必须带 CREATE_NO_WINDOW：既是"脱离终端"（不掉线），也是"不弹窗"。
    # 用 DETACHED_PROCESS 会弹出 Windows Terminal 窗口 —— 实测，见
    # tests/test_no_window_guard.py 里的 A/B 结论。
    assert captured["kwargs"]["creationflags"] == daemon.NO_WINDOW
    assert daemon.NO_WINDOW != 0, "Windows 上必须带真实的 CREATE_NO_WINDOW"
