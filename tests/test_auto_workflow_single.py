"""单实例闸门测试。

为什么这道闸门非有不可：``writelock`` 在 PG 后端下**直接返回 True、不建文件锁**
（互斥交给数据库行级锁），所以它挡不住"同一条工作流起两份"。两份工作流并行
是数据问题而不是并发问题——它们写同一个 ``auto_workflow.json``、共用
``wf_*.log``（``run()`` 用 "w" 打开，互相截断），还各自去重译同一批条目。
2026-10-16 凌晨实测撞上过。

这里钉住四件事：
  ① 已有活着的实例 → 拒绝启动，且**不覆盖**对方的 PID 文件
  ② 被 kill 留下的残留 PID 文件（进程已不在）→ 自动接管，不需要人工清理
  ③ PID 文件损坏 → 当没有，并清掉
  ④ release 只清自己的，不误删别人的
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


@pytest.fixture()
def aw(tmp_path, monkeypatch):
    """载入 scripts/auto_workflow.py，并把闸门文件重定向到临时目录。

    必须重定向：本机真的有一个工作流在跑，它的 PID 文件就在 data/worker_locks/ 下，
    测试若用真实路径会跟生产互相干扰。
    """
    spec = importlib.util.spec_from_file_location("aw_under_test", SCRIPTS / "auto_workflow.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    lock = tmp_path / "auto_workflow.pid"
    monkeypatch.setattr(mod, "_lock_path", lambda: lock)
    return mod


def test_no_lock_means_not_running(aw):
    assert aw.running_pid() is None


def test_acquire_then_self_is_not_blocking(aw):
    assert aw.acquire_single() is True
    # 自己的 PID 不该挡自己（否则 release 前再查一次就"已在运行"了）
    assert aw.running_pid() is None
    aw.release_single()
    assert not aw._lock_path().exists()


def test_live_other_instance_blocks_startup(aw):
    """核心用例：别人的 PID 文件、且进程活着 → 必须拒绝，且不覆盖。"""
    aw._lock_path().parent.mkdir(parents=True, exist_ok=True)
    aw._lock_path().write_text("424242", encoding="utf-8")

    from taxassist import writelock

    original = writelock._pid_alive
    writelock._pid_alive = lambda pid: pid == 424242      # 假装那个进程活着
    try:
        assert aw.running_pid() == 424242
        # 关键：不能把对方的 PID 改写成自己的
        assert aw._lock_path().read_text(encoding="utf-8") == "424242"
    finally:
        writelock._pid_alive = original


def test_stale_pid_file_is_taken_over(aw):
    """被 kill -9 留下的残留：进程已不在 → 自动接管。"""
    aw._lock_path().parent.mkdir(parents=True, exist_ok=True)
    aw._lock_path().write_text("999999", encoding="utf-8")

    from taxassist import writelock

    original = writelock._pid_alive
    writelock._pid_alive = lambda pid: False
    try:
        assert aw.running_pid() is None
        assert aw.acquire_single() is True
        assert aw._lock_path().read_text(encoding="utf-8") == str(__import__("os").getpid())
    finally:
        writelock._pid_alive = original


def test_corrupt_pid_file_is_ignored_and_cleaned(aw):
    aw._lock_path().parent.mkdir(parents=True, exist_ok=True)
    aw._lock_path().write_text("not-a-pid", encoding="utf-8")
    assert aw.running_pid() is None
    assert not aw._lock_path().exists(), "损坏的 PID 文件应当被清掉"


def test_release_only_removes_own_lock(aw):
    aw._lock_path().parent.mkdir(parents=True, exist_ok=True)
    aw._lock_path().write_text("424242", encoding="utf-8")
    aw.release_single()                                   # 不是自己的
    assert aw._lock_path().exists(), "别人的 PID 文件不该被删"
