"""CLI 层测试：控制台编码兜底与邀请码命令。

守的是"命令行在最后一步崩掉"这类问题 —— Windows 控制台默认 GBK 代码页，
警告符号会让 print 抛 UnicodeEncodeError，而对外暴露时正要打印这些警告，
等于在最需要提醒的时候程序挂掉（实测：--host 0.0.0.0 启动即退出）。
"""
from __future__ import annotations

import io
import sys
from argparse import Namespace

import pytest

from taxassist import auth, cli
from taxassist import db as dbmod


def test_gbk_stream_would_really_fail_without_the_fix():
    """反向确认：这个坑真实存在，不是假想出来的。"""
    stream = io.TextIOWrapper(io.BytesIO(), encoding="gbk", errors="strict")
    with pytest.raises(UnicodeEncodeError):
        stream.write("⚠")


def test_console_setup_makes_warning_symbols_printable(monkeypatch):
    """模拟 Windows 控制台（GBK 严格模式）跑一遍修复后的输出。"""
    stream = io.TextIOWrapper(io.BytesIO(), encoding="gbk", errors="strict")
    monkeypatch.setattr(sys, "stdout", stream)

    cli._setup_console()
    print("⚠ 正在对外提供访问")
    sys.stdout.flush()

    assert "⚠".encode() in stream.buffer.getvalue()


@pytest.fixture()
def db_path(tmp_path, monkeypatch):
    monkeypatch.setattr(dbmod, "DB_PATH", tmp_path / "cli.db")
    conn = dbmod.connect()
    dbmod.init_db(conn)
    conn.close()
    return tmp_path / "cli.db"


def _invite_args(**over) -> Namespace:
    base = {"note": "", "days": 30, "list": False, "available": False,
            "revoke": None, "owner": False}
    base.update(over)
    return Namespace(**base)


def test_invite_command_creates_a_code(db_path, capsys):
    assert cli.cmd_invite(_invite_args(note="客户张三", days=30)) == 0
    out = capsys.readouterr().out

    conn = dbmod.connect()
    try:
        rows = auth.list_invites(conn)
    finally:
        conn.close()
    assert len(rows) == 1
    assert rows[0]["state"] == "可用"
    assert rows[0]["note"] == "客户张三"
    assert rows[0]["display"] in out          # 打印出来的就是库里的那一份


def test_invite_zero_days_means_never_expires(db_path):
    assert cli.cmd_invite(_invite_args(days=0)) == 0
    conn = dbmod.connect()
    try:
        assert auth.list_invites(conn)[0]["expires_at"] is None
    finally:
        conn.close()


def test_invite_owner_flag_issues_an_admin_code(db_path, capsys):
    """管理员码：使用者自己设口令，注册出来直接能进后台。"""
    assert cli.cmd_invite(_invite_args(owner=True, note="给自己")) == 0
    out = capsys.readouterr().out
    assert "管理员邀请码" in out
    conn = dbmod.connect()
    try:
        assert auth.list_invites(conn)[0]["grants_role"] == auth.ROLE_OWNER
    finally:
        conn.close()


def test_invite_without_owner_flag_is_a_plain_code(db_path):
    assert cli.cmd_invite(_invite_args()) == 0
    conn = dbmod.connect()
    try:
        assert auth.list_invites(conn)[0]["grants_role"] == auth.ROLE_MEMBER
    finally:
        conn.close()


def test_invite_list_filters_available_only(db_path, capsys):
    conn = dbmod.connect()
    try:
        used = auth.create_invite(conn)["code"]
        auth.create_invite(conn)
        auth.redeem_invite(conn, used, "someone", "ZhengCe-2026-!")
    finally:
        conn.close()

    assert cli.cmd_invite(_invite_args(list=True)) == 0
    assert capsys.readouterr().out.count("  ") >= 2

    assert cli.cmd_invite(_invite_args(list=True, available=True)) == 0
    out = capsys.readouterr().out
    assert "[可用]" in out
    assert "[已使用]" not in out


def test_invite_revoke(db_path, capsys):
    conn = dbmod.connect()
    try:
        code = auth.create_invite(conn)["code"]
    finally:
        conn.close()

    assert cli.cmd_invite(_invite_args(revoke=code)) == 0
    conn = dbmod.connect()
    try:
        assert auth.list_invites(conn)[0]["state"] == "已吊销"
    finally:
        conn.close()


def test_invite_revoke_unknown_code_reports_failure(db_path):
    assert cli.cmd_invite(_invite_args(revoke="AAAA-BBBB-CCCC")) == 1


def test_invite_revoke_returns_code_for_scripts(db_path):
    """吊销返回值要能被脚本判断：成功 0、没吊销到 1。"""
    conn = dbmod.connect()
    try:
        code = auth.create_invite(conn)["code"]
        auth.revoke_invite(conn, code)
    finally:
        conn.close()
    assert cli.cmd_invite(_invite_args(revoke=code)) == 1


def test_expose_flag_enables_auth_even_on_loopback():
    """隧道/反向代理的场景：后端只监听 127.0.0.1，但必须仍启用认证。

    只按绑定地址判断的话，一按标准做法配隧道（后端绑本机），门锁就没了。
    """
    assert cli._is_exposed("127.0.0.1", False) is False     # 纯本机
    assert cli._is_exposed("localhost", False) is False
    assert cli._is_exposed("127.0.0.1", True) is True       # 走隧道
    assert cli._is_exposed("0.0.0.0", False) is True        # 直接对外
    assert cli._is_exposed("192.168.1.10", False) is True   # 局域网


# ---------------------------------------------------------------- 撤销访问权

def test_userdel_removes_account_and_kills_its_session(db_path, capsys):
    conn = dbmod.connect()
    try:
        auth.create_user(conn, "kehu-zhang", "ZhengCe-2026-!")
        token = auth.issue_token(conn, "kehu-zhang")
        assert auth.read_token(conn, token) == "kehu-zhang"
    finally:
        conn.close()

    assert cli.cmd_userdel(Namespace(username="kehu-zhang")) == 0
    conn = dbmod.connect()
    try:
        assert auth.user_count(conn) == 0
        # 对方浏览器里那个 Cookie 立即作废，不需要额外黑名单
        assert auth.read_token(conn, token) is None
    finally:
        conn.close()
    assert "已删除" in capsys.readouterr().out


def test_userdel_unknown_account_reports_failure(db_path):
    assert cli.cmd_userdel(Namespace(username="nobody")) == 1
