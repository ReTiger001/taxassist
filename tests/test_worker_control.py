"""后台工作者（worker）的**控制面**：状态文件与停止信号 —— 此前零测试。

不测 `run_forever` 那种长跑编排（需要真实网络与几十分钟，属端到端范畴）。
这里测的是控制面，理由：它坏掉不会让任何功能测试变红，但会让"**叫不停一个
跑飞的抓取任务**"—— 而那恰恰是最需要叫停的时候。
"""
from __future__ import annotations

import os

import pytest

from taxassist import worker as w


@pytest.fixture()
def files(tmp_path, monkeypatch):
    """把状态文件与停止文件指到临时目录 —— 绝不碰真实 data/。"""
    monkeypatch.setattr(w, "STATUS_FILE", tmp_path / "worker_status.json")
    monkeypatch.setattr(w, "STOP_FILE", tmp_path / "worker_stop")
    return tmp_path


def test_stop_signal_roundtrip(files):
    """停止信号：请求 → 生效 → 清除 → 失效。"""
    assert w.stop_requested() is False
    w.request_stop()
    assert w.stop_requested() is True
    w.clear_stop()
    assert w.stop_requested() is False


def test_request_stop_works_before_any_run(files, monkeypatch):
    """目录还不存在时也要能请求停止。

    回归（原注释里记的坑）：`write_text` 少了 `mkdir` 会抛 FileNotFoundError，
    而"停止失败"是最不该发生的失败 —— 首次运行就想取消是很常见的动作。
    """
    nested = files / "还没建过的目录" / "更深一层"
    monkeypatch.setattr(w, "STOP_FILE", nested / "stop")
    w.request_stop()
    assert w.stop_requested() is True


def test_clear_stop_is_idempotent(files):
    """没在跑时清理停止信号不得报错（重复调用是正常路径）。"""
    w.clear_stop()
    w.clear_stop()


def test_status_roundtrip_keeps_chinese_readable(files):
    """状态要能原样读回，且中文**不被转义成 \\uXXXX**。

    这个文件是给人看的（运维直接打开读），转义了就等于不可读。
    """
    w.write_status({"回合": 3, "阶段": {"抓取": {"ok": True}}, "说明": "进行中"})
    raw = w.STATUS_FILE.read_text(encoding="utf-8")
    assert "抓取" in raw, "中文被转义了，直接打开状态文件会看不懂"
    assert w.read_status()["回合"] == 3


def test_read_status_before_first_run(files):
    """还没跑过时给可读的默认值，而不是异常。"""
    s = w.read_status()
    assert s["回合"] == 0
    assert "还没跑过" in s["说明"]


def test_read_status_reports_corruption_instead_of_raising(files):
    """状态文件损坏要如实报告，不抛异常 —— 它可能正被另一个进程写着。"""
    w.STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    w.STATUS_FILE.write_text("{ 这不是合法 JSON", encoding="utf-8")
    s = w.read_status()
    assert "错误" in s, f"损坏的状态文件应当报错而不是抛异常，实际返回 {s}"


def test_pid_alive_basics():
    """判活：非法 pid 一律当已死；自己一定是活的。

    "判活失败当已死"是有意设计（见源码注释）—— 宁可接管别人的锁，
    也好过因为判活本身出错而永久卡住。
    """
    assert w._pid_alive(0) is False
    assert w._pid_alive(-1) is False
    assert w._pid_alive(os.getpid()) is True
