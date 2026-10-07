"""登录 / 退出 / 注册 —— 从 `web/app.py` 的 `create_app` 里拆出来。

这三个路由在认证中间件里是**豁免**的（否则没账号的人永远进不来）。它们只做
一件事：核对凭据并下发签名 Cookie。**不提供任何注册之外的写操作** —— Web 层
唯一的写操作区是 /admin（理由见 app.py 里那段说明）。

依赖用参数注入（与 assistant_routes / about_routes 同范式）。

**为什么 `_origin_ok` 等在函数内导入**：它们定义在 app.py，而 app.py 在模块级
就执行 `create_app()`、create_app 内部又要导入本模块 —— 模块级导入会成环
（详见 assistant_routes.py 里那段说明）。这里在 `register()` 里取一次，
下面各路由作为闭包捕获，不必每个函数各写一遍。
"""
from __future__ import annotations

import logging

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import auth
from .. import db as dbmod

log = logging.getLogger(__name__)


def register(app, *, templates, require_auth: bool) -> None:
    """把登录/退出/注册挂到 app 上。"""
    # 一次取出，供下面各路由闭包捕获（理由见模块头部）
    from .app import _is_local_request, _origin_ok, _read_form, _safe_next, _set_session_cookie

    def _login_view(request: Request, *, error=None, next_url="/", username="",
                    status_code=200):
        conn = dbmod.connect()
        try:
            n_users = auth.user_count(conn)
        finally:
            conn.close()
        return templates.TemplateResponse(
            request=request, name="login.html",
            context={"request": request, "error": error, "next": next_url,
                     "username": username, "n_users": n_users,
                     "exposed": require_auth, "local": _is_local_request(request),
                     "min_password": auth.MIN_PASSWORD_LEN},
            status_code=status_code)

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request, next: str = "/"):
        if not require_auth:
            return RedirectResponse("/", status_code=302)
        conn = dbmod.connect()
        try:
            if auth.read_token(conn, request.cookies.get(auth.SESSION_COOKIE, "")):
                return RedirectResponse("/", status_code=302)
        finally:
            conn.close()
        return _login_view(request, next_url=_safe_next(next))

    @app.post("/login", response_class=HTMLResponse)
    async def login_submit(request: Request):
        form = await _read_form(request)
        username = form.get("username", "").strip()
        password = form.get("password", "")
        next_url = _safe_next(form.get("next", "/"))
        if not _origin_ok(request):
            return _login_view(request, error="请求来源异常，请回到登录页重新提交",
                               next_url=next_url, username=username)
        conn = dbmod.connect()
        try:
            if auth.check_credentials(conn, username, password):
                token = auth.issue_token(conn, username)
            else:
                token = None
        finally:
            conn.close()
        if token is None:
            return _login_view(
                request, next_url=next_url, username=username,
                error="账号或口令不正确。连续输错会被暂时锁定，请稍后再试。")
        log.info("登录成功：%s", username)
        response = RedirectResponse(next_url, status_code=302)
        _set_session_cookie(response, request, token)
        return response

    @app.get("/logout")
    def logout(request: Request):
        """退出登录。用 GET 是为了顶栏一个普通链接即可完成；
        让别人把你踢下线不构成攻击收益，故不为此加表单。"""
        response = RedirectResponse("/login", status_code=302)
        response.delete_cookie(auth.SESSION_COOKIE, path="/")
        return response

    @app.get("/register", response_class=HTMLResponse)
    def register_page(request: Request, code: str = ""):
        if not require_auth:
            return RedirectResponse("/", status_code=302)
        return templates.TemplateResponse(
            request=request, name="register.html",
            context={"request": request, "error": None,
                     "code": auth.format_invite(auth.normalize_invite(code)),
                     "username": "", "exposed": require_auth,
                     "min_password": auth.MIN_PASSWORD_LEN})

    @app.post("/register", response_class=HTMLResponse)
    async def register_submit(request: Request):
        form = await _read_form(request)
        code = form.get("code", "")
        username = form.get("username", "").strip()
        password = form.get("password", "")
        password2 = form.get("password2", "")

        def view(error: str, status_code: int = 200):
            return templates.TemplateResponse(
                request=request, name="register.html",
                context={"request": request, "error": error, "username": username,
                         "code": auth.format_invite(auth.normalize_invite(code)),
                         "exposed": require_auth,
                         "min_password": auth.MIN_PASSWORD_LEN},
                status_code=status_code)

        if not _origin_ok(request):
            return view("请求来源异常，请回到注册页重新提交")
        # 按来源限速：邀请码本身无法爆破（27^12），但挡得住反复试码与刷表
        ip_key = f"reg:{(request.client.host if request.client else '?')}"
        if auth.is_rate_limited(ip_key):
            return view("注册尝试过于频繁，请稍后再试")
        if password != password2:
            auth.record_failure(ip_key)
            return view("两次输入的口令不一致")
        conn = dbmod.connect()
        try:
            name = auth.redeem_invite(conn, code, username, password)
            token = auth.issue_token(conn, name)
        except ValueError as e:
            auth.record_failure(ip_key)
            return view(str(e))
        finally:
            conn.close()
        log.info("邀请码注册成功：%s", name)
        response = RedirectResponse("/", status_code=302)   # 注册完直接进去，不再让人登一次
        _set_session_cookie(response, request, token)
        return response
