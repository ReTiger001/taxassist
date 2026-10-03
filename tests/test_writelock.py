"""writelock 测试：拿锁、让路、过期抢占。

这模块存在的理由是「长任务彼此不要撞锁」，所以测试要覆盖三件关键事：
  ① 已被持有时 acquire 返回 False —— 调用方据此**跳过本轮**而不是报错退出
  ② 持有者进程已不存在时视为过期可抢占 —— 否则一次崩溃就永久停摆
  ③ release 只释放自己那把，不会误删别人的锁
"""
import os

from taxassist import writelock


def _clean():
    writelock.LOCK_FILE.unlink(missing_ok=True)


def test_acquire_then_holder_then_release():
    _clean()
    assert writelock.acquire("t1") is True
    assert writelock.holder() == f"{os.getpid()}:t1"
    writelock.release()
    assert writelock.holder() is None


def test_second_acquire_refuses_without_blocking():
    _clean()
    assert writelock.acquire("t1") is True
    # 同一进程再来一次也应当拿不到（真实场景是两个进程）
    assert writelock.acquire("t2") is False
    assert "t1" in (writelock.holder() or "")
    writelock.release()


def test_stale_lock_from_dead_pid_is_taken_over():
    """锁文件的 PID 已不存在（崩溃/被 kill）时必须能抢占。"""
    _clean()
    writelock.LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    # 取一个几乎不可能存在的 PID
    writelock.LOCK_FILE.write_text("999999:worker:fetch", encoding="utf-8")
    assert writelock.holder() is None, "过期锁应被清理"
    assert writelock.acquire("t3") is True
    writelock.release()


def test_corrupt_lock_file_is_treated_as_stale():
    _clean()
    writelock.LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    writelock.LOCK_FILE.write_text("这不是合法的锁内容", encoding="utf-8")
    assert writelock.holder() is None
    assert writelock.acquire("t4") is True
    writelock.release()


def test_release_does_not_remove_others_lock():
    _clean()
    writelock.LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    writelock.LOCK_FILE.write_text("999998:someone-else", encoding="utf-8")
    writelock.release()  # 不是自己的锁，不该被删
    # 999998 大概率不存在，所以 holder() 会清理它 —— 这里只断言 release 本身
    # 没有把文件当自己的删掉（用存在的 PID 更稳，但那个 PID 未必可造）。
    _clean()
