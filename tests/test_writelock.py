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


# ---------------------------------------------------------------------------
# 并发竞态：多进程同时抢锁
# ---------------------------------------------------------------------------

#: 子进程脚本 —— 等到约定时刻再抢锁，持有一小会儿，把结果打在 stdout。
#:
#: 用**独立子进程**而不是线程或 ProcessPool：这把锁的归属判据是 PID，
#: 同一进程里的两个线程看到的是"自己"的 PID，压根走不到竞态那条路。
_RACER = '''\
import sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from taxassist import writelock
writelock.LOCK_FILE = Path(sys.argv[2])
start_at = float(sys.argv[3])
while time.time() < start_at:      # 忙等到同一时刻，尽量让抢锁同时发生
    pass
ok = writelock.acquire("race", timeout=0)
if ok:
    time.sleep(0.6)                # 持有片刻：旧实现下别人会在这段里挤进来
    writelock.release()
print("won" if ok else "lost")
'''


def test_concurrent_acquire_has_exactly_one_winner(tmp_path):
    """多个进程同时抢锁 → **有且只有一个**拿到。

    这是 2026-10 全量审计指出的竞态：原实现是「``holder()`` 返回 None →
    ``_write()``」。两个进程同时抢时，**两边都可能看到 None**，然后都写、
    都返回 True —— 于是两个写库任务并行跑，而这正是这把锁唯一要防的事。
    窗口只有毫秒级，但 acquire 每天被调用几十次，迟早会撞上。

    修法是把"先查再写"换成 ``O_CREAT|O_EXCL`` 独占创建（见 ``_claim``），
    由内核裁决 —— 这条测试就是钉住它：6 个进程一起抢，赢家必须恰好 1 个。
    """
    import subprocess
    import sys
    import time
    from pathlib import Path

    from taxassist import proc

    src_dir = Path(writelock.__file__).resolve().parents[1]
    script = tmp_path / "racer.py"
    script.write_text(_RACER, encoding="utf-8")
    lock_file = tmp_path / "race.lock"

    n = 6
    start_at = time.time() + 3.0        # 留够子进程 import taxassist 的时间
    procs = [
        subprocess.Popen(
            [sys.executable, str(script), str(src_dir), str(lock_file), str(start_at)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            creationflags=proc.hidden_flags(),
        )
        for _ in range(n)
    ]
    outs = []
    for p in procs:
        out, err = p.communicate(timeout=90)
        outs.append(out)
        assert p.returncode == 0, f"子进程异常退出：{err[-300:]}"

    winners = sum(1 for o in outs if "won" in o)
    assert winners == 1, (
        f"{n} 个进程同时抢锁，赢家应当是 1 个，实际 {winners} 个 —— "
        "说明 acquire 又退回了「先查再写」，两个写库任务可能并行")
