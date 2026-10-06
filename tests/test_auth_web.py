"""Web 认证全链路测试：拦截、注册、登录、登出、跳转安全。

这些用例守的是"对外暴露"这条路径上最容易出事的几处：
未登录能否看到内容、拿到邀请码能否越权接管别人账号、
next 参数能否被当成开放重定向跳板、登出是否真的清掉了会话。
"""
from __future__ import annotations

import base64
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from taxassist import auth
from taxassist import db as dbmod
from taxassist.web.app import create_app

GOOD_PWD = "ZhengCe-2026-!"


@pytest.fixture(autouse=True)
def _clean_attempts():
    """限速计数是模块级的，跨用例会互相污染（累计 8 次就触发锁定）。"""
    auth._ATTEMPTS.clear()
    yield
    auth._ATTEMPTS.clear()


@pytest.fixture()
def db_path(tmp_path, monkeypatch):
    """把整个应用指向临时库 —— 测试绝不碰真实数据。"""
    monkeypatch.setattr(dbmod, "DB_PATH", tmp_path / "web.db")
    conn = dbmod.connect()
    dbmod.init_db(conn)
    conn.close()
    return tmp_path / "web.db"


@pytest.fixture()
def client(db_path):
    """对外模式：登录页 + 签名 Cookie，且关闭自动跟随跳转以便断言 302。"""
    return TestClient(create_app(require_auth=True, auth_mode="page"),
                      follow_redirects=False)


@pytest.fixture()
def local_client(db_path):
    """本机模式：无认证，直接可读。"""
    return TestClient(create_app(require_auth=False), follow_redirects=False)


def _invite() -> str:
    conn = dbmod.connect()
    try:
        return auth.create_invite(conn, note="测试")["code"]
    finally:
        conn.close()


def _seed_user(username: str = "owner") -> None:
    conn = dbmod.connect()
    try:
        auth.create_user(conn, username, GOOD_PWD)
    finally:
        conn.close()


def _login(client, username="owner", password=GOOD_PWD, **kw):
    return client.post("/login", data={"username": username, "password": password}, **kw)


def _register(client, code, username, password=GOOD_PWD):
    return client.post("/register", data={"code": code, "username": username,
                                          "password": password, "password2": password})


# --------------------------------------------- 白名单页的登录态（曾踩坑）

def test_about_page_shows_login_state_for_signed_in_user(client):
    """关于页在白名单里，但它必须认出已登录的人。

    用户实测踩到：点「关于」后顶栏变成「登录」按钮、搜索框也消失，
    而其它标签都正常。根因是认证中间件对白名单路径直接 call_next、
    从不解析 session，request.state.user 恒为 None —— 公开页对未登录
    访客开放，不代表它该对已登录的人装不认识。
    """
    _seed_user("owner")
    _login(client)
    r = client.get("/about")
    assert r.status_code == 200
    assert "退出" in r.text             # 已登录 → 显示"退出"
    assert ">登录</a>" not in r.text    # 而不是"登录"按钮
    assert 'class="quick"' in r.text    # 搜索框也应在


def test_about_page_stays_open_to_anonymous_with_nav(client):
    """同时不能把未登录访客挡出去，且导航必须在。

    导航若随登录状态消失，未登录访客点进关于页就没有任何返回入口
    —— 这正是先前"点关于就回不去"的原因。
    """
    r = client.get("/about")
    assert r.status_code == 200
    assert "税务智能知识助手" in r.text
    assert "<nav>" in r.text


# ---------------------------------------------------------------- 闸门

def test_anonymous_is_redirected_to_login(client):
    for path in ("/", "/search", "/daily", "/policy/uid-1"):
        r = client.get(path)
        assert r.status_code == 302, path
        assert r.headers["location"].startswith("/login")


def test_next_parameter_preserves_the_original_target(client):
    r = client.get("/search?q=增值税")
    location = r.headers["location"]
    assert location.startswith("/login?next=")
    nxt = parse_qs(urlsplit(location).query)["next"][0]
    # next 指回原页面，且它自身携带的检索词完好（没被双重编码毁掉）
    assert urlsplit(nxt).path == "/search"
    assert parse_qs(urlsplit(nxt).query)["q"] == ["增值税"]


def test_local_mode_has_no_gate(local_client):
    assert local_client.get("/").status_code == 200


def test_login_and_register_pages_are_reachable_without_an_account(client):
    assert client.get("/login").status_code == 200
    assert client.get("/register").status_code == 200


def test_wrong_password_is_rejected_with_a_chinese_reason(client):
    _seed_user()
    r = _login(client, password="wrong-pass-2026")
    assert r.status_code == 200
    assert "账号或口令不正确" in r.text


def test_unknown_account_is_rejected(client):
    r = _login(client, username="nobody")
    assert r.status_code == 200
    assert "账号或口令不正确" in r.text


def test_login_success_issues_a_session_cookie(client):
    _seed_user()
    r = _login(client)
    assert r.status_code == 302
    assert r.headers["location"] == "/"
    assert auth.SESSION_COOKIE in r.cookies


def test_logged_in_user_can_browse_and_sees_own_name(client):
    _seed_user()
    _login(client)
    r = client.get("/")
    assert r.status_code == 200
    assert "owner" in r.text          # 顶栏显示当前账号
    assert "退出" in r.text


def test_all_pages_render_for_a_logged_in_user(client):
    """空库也要能把每页渲染出来 —— 模板里少一个变量的代价是整页 500。"""
    _seed_user()
    _login(client)
    for path in ("/", "/search", "/daily"):
        r = client.get(path)
        assert r.status_code == 200, f"{path} -> {r.status_code}"


def test_logout_clears_the_session(client):
    _seed_user()
    _login(client)
    assert client.get("/logout").status_code == 302
    assert client.get("/").status_code == 302


def test_login_page_redirects_when_already_logged_in(client):
    _seed_user()
    _login(client)
    r = client.get("/login")
    assert r.status_code == 302
    assert r.headers["location"] == "/"


# ---------------------------------------------------------------- 注册

def test_register_creates_account_and_signs_in(client):
    r = _register(client, _invite(), "kehu-zhang")
    assert r.status_code == 302
    assert r.headers["location"] == "/"
    # 注册完直接可用，不必再登一次
    assert client.get("/").status_code == 200
    assert "kehu-zhang" in client.get("/").text


def test_register_accepts_sloppy_handwriting(client):
    code = _invite().replace("-", "").lower()
    assert _register(client, code, "kehu-li").status_code == 302


def test_register_requires_the_two_passwords_to_match(client):
    code = _invite()
    r = client.post("/register", data={"code": code, "username": "kehu-wang",
                                       "password": GOOD_PWD, "password2": GOOD_PWD + "x"})
    assert r.status_code == 200
    assert "不一致" in r.text
    # 手误不该烧掉客户手里的邀请码
    assert _register(client, code, "kehu-wang").status_code == 302


def test_register_without_a_valid_invite_is_impossible(client):
    """核心安全断言：没有有效邀请码就进不来。"""
    r = _register(client, "AAAA-BBBB-CCCC", "nobody")
    assert r.status_code == 200
    assert "无效" in r.text
    assert client.get("/").status_code == 302


def test_register_rejects_short_password(client):
    code = _invite()
    r = _register(client, code, "kehu-short", password="short123")
    assert r.status_code == 200
    assert "至少 10 位" in r.text
    assert _register(client, code, "kehu-short").status_code == 302


def test_an_invite_registers_exactly_one_account(client):
    code = _invite()
    assert _register(client, code, "first").status_code == 302
    other = TestClient(create_app(require_auth=True, auth_mode="page"),
                       follow_redirects=False)
    r = _register(other, code, "second")
    assert r.status_code == 200
    assert "已被使用" in r.text
    assert other.get("/").status_code == 302


def test_register_cannot_take_over_an_existing_account(client):
    """核心安全断言：拿邀请码去注册一个已存在的账号名，不得改掉对方口令。"""
    _seed_user("victim")
    r = _register(client, _invite(), "victim", password="Attacker-2026-!")
    assert r.status_code == 200
    assert "已被占用" in r.text

    fresh = TestClient(create_app(require_auth=True, auth_mode="page"),
                       follow_redirects=False)
    assert _login(fresh, "victim", GOOD_PWD).status_code == 302
    assert _login(fresh, "victim", "Attacker-2026-!").status_code == 200


def test_revoked_invite_is_refused(client):
    conn = dbmod.connect()
    try:
        inv = auth.create_invite(conn)
        auth.revoke_invite(conn, inv["code"])
    finally:
        conn.close()
    r = _register(client, inv["code"], "kehu-blocked")
    assert r.status_code == 200
    assert "吊销" in r.text


# ---------------------------------------------------------------- 跳转安全

@pytest.mark.parametrize("bad", ["https://evil.example/", "//evil.example/",
                                 "/\\evil.example"])
def test_open_redirect_is_neutralised(client, bad):
    _seed_user()
    assert 'name="next" value="/"' in client.get("/login", params={"next": bad}).text


def test_login_post_ignores_an_external_next(client):
    _seed_user()
    r = client.post("/login", data={"username": "owner", "password": GOOD_PWD,
                                    "next": "https://evil.example/"})
    assert r.status_code == 302
    assert r.headers["location"] == "/"


def test_cross_site_form_submission_is_refused(client):
    _seed_user()
    r = client.post("/login", data={"username": "owner", "password": GOOD_PWD},
                    headers={"Origin": "https://evil.example"})
    assert r.status_code == 200
    assert "请求来源异常" in r.text


# ---------------------------------------------------------------- 兼容与响应头

def test_basic_mode_still_works_for_scripts(db_path):
    _seed_user()
    c = TestClient(create_app(require_auth=True, auth_mode="basic"),
                   follow_redirects=False)
    r = c.get("/")
    assert r.status_code == 401
    assert r.headers["www-authenticate"].startswith("Basic")

    token = base64.b64encode(f"owner:{GOOD_PWD}".encode()).decode()
    assert c.get("/", headers={"Authorization": f"Basic {token}"}).status_code == 200


def test_security_headers_present_on_every_response(client):
    r = client.get("/login")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    # 连认证中间件直接返回的 302 也要带上
    assert client.get("/").headers["x-content-type-options"] == "nosniff"


def test_session_cookie_is_hardened(client):
    _seed_user()
    r = _login(client)
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=lax" in cookie
    # 本机 http 调试下不能带 Secure，否则浏览器会直接丢掉这个 Cookie
    assert "secure" not in cookie


def test_setup_hint_is_shown_only_to_local_visitors(db_path):
    """公网访客不该看到"在本机执行 python -m taxassist invite"这类站长专用提示：
    对他们毫无用处，还暴露了这个服务跑在本机。"""
    app = create_app(require_auth=True, auth_mode="page")
    remote = TestClient(app, follow_redirects=False, client=("203.0.113.9", 51234))
    local = TestClient(app, follow_redirects=False, client=("127.0.0.1", 51234))

    remote_text = remote.get("/login").text
    assert "taxassist invite" not in remote_text
    assert "尚未开通" in remote_text

    assert "taxassist invite" in local.get("/login").text


def test_mode_badge_tells_the_truth(client, local_client):
    """对外暴露时还标"本地"就是在骗使用者。"""
    _seed_user()
    _login(client)
    assert "对外" in client.get("/").text
    assert "本地" in local_client.get("/").text


# ---------------------------------------------------------------- 后台账号管理

def _owner_client(db_path, username: str = "boss"):
    conn = dbmod.connect()
    try:
        auth.create_user(conn, username, GOOD_PWD, role=auth.ROLE_OWNER)
    finally:
        conn.close()
    client = TestClient(create_app(require_auth=True, auth_mode="page"),
                        follow_redirects=False)
    assert _login(client, username).status_code == 302
    return client


def _invite_rows():
    conn = dbmod.connect()
    try:
        return auth.list_invites(conn)
    finally:
        conn.close()


def test_anonymous_cannot_reach_admin(client):
    r = client.get("/admin")
    assert r.status_code == 302
    assert r.headers["location"].startswith("/login")


def test_member_cannot_reach_admin(client):
    _seed_user("member1")          # 默认就是普通成员
    _login(client, "member1")
    assert client.get("/admin").status_code == 403


def test_owner_can_reach_admin(db_path):
    c = _owner_client(db_path)
    r = c.get("/admin")
    assert r.status_code == 200
    assert "boss" in r.text


def test_local_mode_admin_is_open(db_path):
    """只监听本机时，坐在这台电脑前的就是本人。"""
    c = TestClient(create_app(require_auth=False), follow_redirects=False)
    assert c.get("/admin").status_code == 200


def test_admin_can_create_an_invite_from_the_page(db_path):
    c = _owner_client(db_path)
    r = c.post("/admin/invite", data={"note": "客户李四", "days": "7"})
    assert r.status_code == 303          # PRG：刷新不会重复生成
    rows = _invite_rows()
    assert len(rows) == 1
    assert rows[0]["note"] == "客户李四"
    assert rows[0]["state"] == "可用"


def test_invite_created_in_admin_actually_works(db_path):
    """后台发出的码必须真的能注册 —— 这是这条链路的终点。"""
    c = _owner_client(db_path)
    c.post("/admin/invite", data={"note": "给客户", "days": "30"})
    code = _invite_rows()[0]["display"]

    fresh = TestClient(create_app(require_auth=True, auth_mode="page"),
                       follow_redirects=False)
    assert _register(fresh, code, "client-a").status_code == 302
    assert fresh.get("/").status_code == 200


def test_admin_can_revoke_an_invite(db_path):
    c = _owner_client(db_path)
    conn = dbmod.connect()
    try:
        code = auth.create_invite(conn)["code"]
    finally:
        conn.close()
    assert c.post("/admin/invite/revoke", data={"code": code}).status_code == 303
    assert _invite_rows()[0]["state"] == "已吊销"


def test_admin_can_delete_a_member(db_path):
    _seed_user("temp-user")
    c = _owner_client(db_path)
    assert c.post("/admin/user/delete", data={"username": "temp-user"}).status_code == 303
    conn = dbmod.connect()
    try:
        assert [u["username"] for u in auth.list_users(conn)] == ["boss"]
    finally:
        conn.close()


def test_admin_cannot_delete_the_last_owner(db_path):
    """删掉最后一个超级管理员，等于把自己锁在门外。"""
    c = _owner_client(db_path)
    assert c.post("/admin/user/delete", data={"username": "boss"}).status_code == 303
    conn = dbmod.connect()
    try:
        assert auth.owner_count(conn) == 1
    finally:
        conn.close()


def test_admin_can_promote_and_demote(db_path):
    _seed_user("colleague")
    c = _owner_client(db_path)
    assert c.post("/admin/user/role",
                  data={"username": "colleague", "role": "owner"}).status_code == 303
    conn = dbmod.connect()
    try:
        assert auth.is_owner(conn, "colleague")
    finally:
        conn.close()

    assert c.post("/admin/user/role",
                  data={"username": "colleague", "role": "member"}).status_code == 303
    conn = dbmod.connect()
    try:
        assert not auth.is_owner(conn, "colleague")
    finally:
        conn.close()


def test_last_owner_cannot_demote_itself(db_path):
    c = _owner_client(db_path)
    assert c.post("/admin/user/role",
                  data={"username": "boss", "role": "member"}).status_code == 303
    conn = dbmod.connect()
    try:
        assert auth.owner_count(conn) == 1
    finally:
        conn.close()


def test_admin_writes_reject_cross_site_forms(db_path):
    """后台是本站唯一能改数据的地方，跨站表单必须打不进来。"""
    c = _owner_client(db_path)
    r = c.post("/admin/invite", data={"note": "x", "days": "7"},
               headers={"Origin": "https://evil.example"})
    assert r.status_code == 303
    assert _invite_rows() == []


def test_promotion_changes_what_the_session_can_do(db_path):
    """刚被提升为管理员的人，不用重新登录就能进后台（角色是每次请求现查的）。"""
    _seed_user("colleague2")
    member = TestClient(create_app(require_auth=True, auth_mode="page"),
                        follow_redirects=False)
    _login(member, "colleague2")
    assert member.get("/admin").status_code == 403

    c = _owner_client(db_path)
    c.post("/admin/user/role", data={"username": "colleague2", "role": "owner"})
    assert member.get("/admin").status_code == 200


# ---------------------------------------------------------------- 管理员邀请码

def test_owner_invite_grants_admin_role_after_self_registration(db_path):
    """管理员邀请码：使用者自己设账号与口令，注册出来直接就是超管 ——
    这样给别人开管理员权限时，发码的人始终不知道对方的口令。"""
    conn = dbmod.connect()
    try:
        code = auth.create_invite(conn, note="给自己", grants_role=auth.ROLE_OWNER)["code"]
    finally:
        conn.close()

    fresh = TestClient(create_app(require_auth=True, auth_mode="page"),
                       follow_redirects=False)
    assert _register(fresh, code, "the-boss").status_code == 302
    assert fresh.get("/admin").status_code == 200       # 注册完直接能进后台
    conn = dbmod.connect()
    try:
        assert auth.is_owner(conn, "the-boss") is True
    finally:
        conn.close()


def test_regular_invite_does_not_grant_admin(db_path):
    conn = dbmod.connect()
    try:
        code = auth.create_invite(conn)["code"]
    finally:
        conn.close()
    fresh = TestClient(create_app(require_auth=True, auth_mode="page"),
                       follow_redirects=False)
    _register(fresh, code, "just-a-user")
    assert fresh.get("/admin").status_code == 403


def test_admin_page_can_issue_owner_invites(db_path):
    c = _owner_client(db_path)
    r = c.post("/admin/invite",
               data={"note": "给合伙人", "days": "30", "role": "owner"})
    assert r.status_code == 303
    rows = _invite_rows()
    assert rows[0]["grants_role"] == "owner"
    assert rows[0]["state"] == "可用"


def test_admin_page_renders_invite_without_expiry(db_path):
    """不过期的邀请码（days=0）在后台列表里必须能渲染。

    回归：双语改造时把 ``{{ i.expires_at[:10] if i.expires_at else '不过期' }}``
    拆成了 ``{{ i.expires_at[:10] }}`` + 一个补文案的 span，于是 expires_at 为
    None（不过期的码）时抛 TypeError: 'NoneType' object is not subscriptable，
    整个 /admin 500。只有真建过「不过期」的码才会走到这个分支，所以单独测。
    """
    c = _owner_client(db_path)
    r = c.post("/admin/invite",
               data={"note": "长期有效", "days": "0", "role": "member"})
    assert r.status_code == 303
    rows = _invite_rows()
    assert rows[0]["expires_at"] is None        # 前提：确实没有失效时间
    assert c.get("/admin").status_code == 200


# ---------------------------------------------------------------- 全站双语

def test_templates_carry_bilingual_attributes():
    """所有模板都要接双语 —— 直接查模板源文件。

    这条防的是"新写/改了一个模板，忘了接双语"。查源文件而不是查渲染结果，
    是因为渲染结果依赖库里有没有数据：夹具库是空的，详情页一取就 404，
    这样测出来的是"库是空的"，不是"模板没接双语"。

    登录/注册页用 auth_base.html（不继承 base.html），曾经最容易漏 ——
    它连语言脚本都要单独引入。
    """
    from pathlib import Path
    tdir = (Path(__file__).resolve().parent.parent
            / "src" / "taxassist" / "web" / "templates")
    names = ("base.html", "auth_base.html", "index.html", "search.html",
             "daily.html", "detail.html", "assistant.html", "about.html",
             "admin.html", "login.html", "register.html", "_macros.html")
    for name in names:
        src = (tdir / name).read_text(encoding="utf-8")
        if name == "about.html":
            # 关于页是**整块切换**（.lang-block[data-lang]：中英两段完整内容
            # 切显示），不是元素级替换，所以它的标记本来就不是 data-zh/data-en。
            # 这里按它自己的机制检查，免得为了过测试去改一个没问题的页面。
            assert "lang-block" in src and "data-lang" in src, "about.html 缺少整块切换标记"
            continue
        assert "data-zh=" in src, f"{name} 没有 data-zh"
        assert "data-en=" in src, f"{name} 没有 data-en"


def test_pages_render_and_carry_bilingual_attributes(db_path):
    """页面能渲染，并且带上双语属性。"""
    c = _owner_client(db_path)
    for path in ("/", "/library", "/search", "/daily", "/about"):
        r = c.get(path)
        assert r.status_code == 200, path
        assert 'data-zh="' in r.text and 'data-en="' in r.text, f"{path} 没有双语属性"
    # 登录/注册页：用**未登录**的 client（已登录访问会被 302 掉，拿不到正文），
    # 且必须引入共享语言脚本 —— 否则未登录访客第一眼看到的页面没有语言切换。
    anon = TestClient(create_app(require_auth=True, auth_mode="page"),
                      follow_redirects=False)
    for path in ("/login", "/register"):
        r = anon.get(path)
        assert r.status_code == 200, path
        assert 'data-zh="' in r.text and 'data-en="' in r.text, f"{path} 没有双语属性"
        assert "/static/lang.js" in r.text, f"{path} 没有引入语言脚本"
