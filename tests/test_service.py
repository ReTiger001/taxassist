"""`taxassist service` 的两条安全属性。

这个模块会**起进程、停进程**，所以它的测试重点不是"功能对不对"，而是
"它会不会伤到不该动的东西"——运维命令最忌讳这件事。
"""
from __future__ import annotations

from taxassist import service


def test_status_is_read_only_and_never_fails(tmp_path, monkeypatch):
    """没有任何服务在跑时，status 也要能跑通、返回 0。

    守的是「看一眼不会出事」：用户只想看看状态，命令就不该顺手改动什么，
    也不该因为"什么都没在跑"而报错。
    """
    monkeypatch.setattr(service, "LOG_DIR", tmp_path / "logs")
    # 指向不可能有人监听的端口，模拟"什么都没跑"
    assert service.status(port=1) == 0


def test_stop_does_not_kill_anything_without_a_pid_record(tmp_path, monkeypatch):
    """没有 PID 记录时，stop 必须什么都不杀。

    这是本模块最重要的安全属性：用户机器上跑着别的 python 进程是常态，
    一个"停止服务"命令把它们一起带走是不可接受的。stop 只动自己在
    data/logs/service-*.pid 里记下的那些 PID。
    """
    monkeypatch.setattr(service, "LOG_DIR", tmp_path / "logs")
    assert service.stop(port=1) == 0
    # 目录都没建过 —— 说明它连写都没写
    assert not (tmp_path / "logs").exists()
