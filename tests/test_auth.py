"""认证层测试：口令存储、邀请码注册、会话令牌。

这些用例守的是"对外暴露"这条路径上最容易出事的地方：
注册入口能否越权改别人的口令、一个邀请码能否注册多个账号、
失败一次会不会把客户手里的邀请码白白烧掉。
"""
from __future__ import annotations

import pytest

from taxassist import auth
from taxassist import db as dbmod

GOOD_PWD = "ZhengCe-2026-!"


@pytest.fixture()
def conn(tmp_path):
    c = dbmod.connect(tmp_path / "auth.db")
    dbmod.init_db(c)
    yield c
    c.close()


@pytest.fixture(autouse=True)
def _clean_attempts():
    """登录失败计数是模块级的，跨用例会互相污染（8 次就触发限速）。"""
    auth._ATTEMPTS.clear()
    yield
    auth._ATTEMPTS.clear()


def _stored(conn, username="zhangsan"):
    return conn.execute("SELECT password_hash FROM app_user WHERE username=?",
                        (username,)).fetchone()[0]


# ---------------------------------------------------------------- 口令与账号

def test_password_is_never_stored_in_plaintext(conn):
    auth.create_user(conn, "zhangsan", GOOD_PWD)
    stored = _stored(conn)
    assert GOOD_PWD not in stored
    assert stored.startswith("scrypt$")


def test_short_password_rejected(conn):
    with pytest.raises(ValueError, match="至少 10 位"):
        auth.create_user(conn, "zhangsan", "short123")
    assert auth.user_count(conn) == 0


@pytest.mark.parametrize("name", ["ab", "a" * 33, "张三", "zhang san", "-lead", "a;drop"])
def test_invalid_username_rejected(conn, name):
    with pytest.raises(ValueError):
        auth.create_user(conn, name, GOOD_PWD)
    assert auth.user_count(conn) == 0


def test_verify_password_roundtrip(conn):
    auth.create_user(conn, "zhangsan", GOOD_PWD)
    stored = _stored(conn)
    assert auth.verify_password(GOOD_PWD, stored)
    assert not auth.verify_password(GOOD_PWD + "x", stored)
    assert not auth.verify_password("", stored)
    assert not auth.verify_password(GOOD_PWD, "垃圾数据")


def test_overwrite_true_resets_password(conn):
    auth.create_user(conn, "zhangsan", GOOD_PWD)
    auth.create_user(conn, "zhangsan", "NewPass-2026-!")
    assert auth.check_credentials(conn, "zhangsan", "NewPass-2026-!")
    assert not auth.check_credentials(conn, "zhangsan", GOOD_PWD)


def test_register_path_must_not_overwrite_existing_user(conn):
    """安全核心：拿到邀请码的人不能借注册接口改掉别人的口令。"""
    auth.create_user(conn, "victim", GOOD_PWD)
    with pytest.raises(ValueError, match="已被占用"):
        auth.create_user(conn, "victim", "Attacker-2026-!", overwrite=False)
    # 原口令必须仍然有效 —— 越权接管没发生
    assert auth.check_credentials(conn, "victim", GOOD_PWD)
    assert not auth.check_credentials(conn, "victim", "Attacker-2026-!")


# ---------------------------------------------------------------- 邀请码

def test_invite_redeem_creates_account_and_burns_code(conn):
    inv = auth.create_invite(conn, note="客户张三")
    assert auth.redeem_invite(conn, inv["code"], "kehu-zhang", GOOD_PWD) == "kehu-zhang"
    assert auth.check_credentials(conn, "kehu-zhang", GOOD_PWD)
    row = conn.execute("SELECT used_by, used_at FROM invite_code").fetchone()
    assert row["used_by"] == "kehu-zhang"
    assert row["used_at"]


def test_invite_is_single_use(conn):
    inv = auth.create_invite(conn)
    auth.redeem_invite(conn, inv["code"], "first", GOOD_PWD)
    with pytest.raises(ValueError, match="已被使用"):
        auth.redeem_invite(conn, inv["code"], "second", GOOD_PWD)
    assert auth.user_count(conn) == 1


def test_invite_accepts_sloppy_handwriting(conn):
    """客户从微信抄来的码：小写、丢了连字符、多了空格，都必须认。"""
    inv = auth.create_invite(conn)
    messy = " " + inv["code"].replace("-", "").lower() + " "
    assert auth.redeem_invite(conn, messy, "kehu-li", GOOD_PWD) == "kehu-li"


def test_unknown_invite_rejected(conn):
    with pytest.raises(ValueError, match="无效"):
        auth.redeem_invite(conn, "AAAA-BBBB-CCCC", "nobody", GOOD_PWD)
    assert auth.user_count(conn) == 0


def test_expired_invite_rejected(conn):
    inv = auth.create_invite(conn, ttl_days=-1)
    with pytest.raises(ValueError, match="过期"):
        auth.redeem_invite(conn, inv["code"], "late", GOOD_PWD)


def test_revoked_invite_rejected(conn):
    inv = auth.create_invite(conn)
    assert auth.revoke_invite(conn, inv["code"]) is True
    with pytest.raises(ValueError, match="吊销"):
        auth.redeem_invite(conn, inv["code"], "blocked", GOOD_PWD)


def test_failed_registration_does_not_burn_the_invite(conn):
    """口令不合格时邀请码必须保持可用 —— 否则客户一次手误就把码废了。"""
    inv = auth.create_invite(conn)
    with pytest.raises(ValueError, match="至少 10 位"):
        auth.redeem_invite(conn, inv["code"], "kehu-wang", "短")
    assert conn.execute("SELECT used_by FROM invite_code").fetchone()["used_by"] is None
    assert auth.redeem_invite(conn, inv["code"], "kehu-wang", GOOD_PWD) == "kehu-wang"


def test_taken_username_does_not_burn_the_invite_either(conn):
    auth.create_user(conn, "taken", GOOD_PWD)
    inv = auth.create_invite(conn)
    with pytest.raises(ValueError, match="占用"):
        auth.redeem_invite(conn, inv["code"], "taken", GOOD_PWD)
    assert conn.execute("SELECT used_by FROM invite_code").fetchone()["used_by"] is None
    assert auth.redeem_invite(conn, inv["code"], "other", GOOD_PWD) == "other"


def test_bad_username_does_not_burn_the_invite(conn):
    inv = auth.create_invite(conn)
    with pytest.raises(ValueError, match="账号需为"):
        auth.redeem_invite(conn, inv["code"], "张三", GOOD_PWD)
    assert conn.execute("SELECT used_by FROM invite_code").fetchone()["used_by"] is None


def test_list_invites_reports_state(conn):
    auth.create_invite(conn, note="待发")
    used = auth.create_invite(conn, note="已发")
    auth.redeem_invite(conn, used["code"], "someone", GOOD_PWD)
    gone = auth.create_invite(conn, note="作废")
    auth.revoke_invite(conn, gone["code"])

    by_note = {r["note"]: r for r in auth.list_invites(conn)}
    assert by_note["待发"]["state"] == "可用"
    assert by_note["已发"]["state"] == "已使用"
    assert by_note["作废"]["state"] == "已吊销"
    assert len(auth.list_invites(conn, only_available=True)) == 1


def test_invite_code_avoids_confusable_characters(conn):
    """邀请码要靠电话/微信转述，不能出现 0/O、1/I/L 这种抄错的字符。"""
    for _ in range(20):
        code = auth.create_invite(conn)["raw"]
        assert not set(code) & set("01LOSZ258B")
        assert len(code) == auth.INVITE_LEN


# ---------------------------------------------------------------- 会话令牌

def test_token_roundtrip(conn):
    auth.create_user(conn, "zhangsan", GOOD_PWD)
    assert auth.read_token(conn, auth.issue_token(conn, "zhangsan")) == "zhangsan"


def test_tampered_token_rejected(conn):
    auth.create_user(conn, "zhangsan", GOOD_PWD)
    token = auth.issue_token(conn, "zhangsan")
    assert auth.read_token(conn, token[:-2] + "xy") is None
    assert auth.read_token(conn, "garbage") is None
    assert auth.read_token(conn, "") is None
    assert auth.read_token(conn, None) is None


def test_token_expiry_enforced(conn):
    auth.create_user(conn, "zhangsan", GOOD_PWD)
    assert auth.read_token(conn, auth.issue_token(conn, "zhangsan", ttl_sec=-1)) is None


def test_password_change_invalidates_existing_sessions(conn):
    """改口令后，别处挂着的旧会话必须立刻失效。"""
    auth.create_user(conn, "zhangsan", GOOD_PWD)
    token = auth.issue_token(conn, "zhangsan")
    auth.create_user(conn, "zhangsan", "NewPass-2026-!")
    assert auth.read_token(conn, token) is None
    assert auth.read_token(conn, auth.issue_token(conn, "zhangsan")) == "zhangsan"


def test_token_for_deleted_user_rejected(conn):
    auth.create_user(conn, "zhangsan", GOOD_PWD)
    token = auth.issue_token(conn, "zhangsan")
    conn.execute("DELETE FROM app_user WHERE username='zhangsan'")
    conn.commit()
    assert auth.read_token(conn, token) is None


def test_session_secret_survives_restart(conn, tmp_path):
    """密钥入库而非内存：重启服务不该把所有已登录的浏览器踢下线。"""
    first = auth.session_secret(conn)
    assert auth.session_secret(conn) == first
    conn.close()

    again = dbmod.connect(tmp_path / "auth.db")
    try:
        assert auth.session_secret(again) == first
    finally:
        again.close()
