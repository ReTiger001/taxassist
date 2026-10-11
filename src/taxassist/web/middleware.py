"""两个 HTTP 中间件：认证闸门与安全响应头 —— 从 `web/app.py` 拆出来。

**为什么单独一个模块**：认证中间件是"对外暴露时的唯一门锁"（源码原话），
这一段里包含本机放行、静态资源白名单、`basic` 与 `page` 两种模式，以及
"白名单页也要解析登录态"这条踩过坑的规则 —— 它值得一个文件，而不是埋在
七百行的 `create_app` 中间。

**注册顺序不能改**：后注册的中间件在外层，安全头必须在认证之后注册，才能
覆盖认证中间件直接返回的那个 302（那个响应不经过内层）。
"""
from __future__ import annotations

import base64
import logging
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import auth
from .. import db as dbmod
from .helpers import PUBLIC_PATHS

log = logging.getLogger(__name__)

def register(app, *, require_auth: bool, auth_mode: str) -> None:
    """注册两个中间件（顺序见模块头部说明）。"""
    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        """对外暴露时的唯一门锁。

        ``require_auth=False``（仅监听本机）时完全放行，本机使用不受打扰。
        page 模式下未登录一律 302 到登录页并带上 next，登录后回到原页面。
        """
        request.state.user = None
        request.state.is_owner = False
        # 路由自己也要能判断"现在是不是对外模式"。助手的两条 API 需要它：
        # `/api/` 前缀在下面被**整体放行**（理由是"它们自己有 Bearer 鉴权"），
        # 但助手是**页面功能**、没有 Key 机制 —— 不给它这条信息，它就没法
        # 决定该不该要求登录。
        request.state.require_auth = require_auth
        if not require_auth:
            # 只监听本机时，坐在这台电脑前的就是本人 —— 后台对他开放
            request.state.is_owner = True
            return await call_next(request)

        path = request.url.path
        # 精确匹配 /static 及其子路径，不用裸 startswith("/static") ——
        # 后者会把 "/static/..%2fadmin" 这类路径也放过去。
        #
        # **这一段现在真的在放行静态文件**：create_app 里 mount 了
        # /static/fonts（自托管字体）。之所以安全，是因为挂载点只到
        # fonts、里面只有两个 woff2；哪天挂了更宽的目录，这里就变成
        # 认证绕过入口。下面两个 ".." 检查是第二道闸，但挡不住 URL
        # 编码的变体（如 %2e%2e%2f）—— 真正的保证是"别挂宽"。
        # `/api/` 也放行 —— **它有自己的一套鉴权**（Authorization: Bearer tk_…），
        # 与页面的会话认证是两回事。不放行的话，客户用 Key 调 API 只会收到
        # 302 跳登录页：实测（2026-10）在免认证的测试站上一切正常，一上
        # 开了认证的正式站全部 302，API 等于不可用。
        # 注意下面"尽力解析登录态"那段只对 PUBLIC_PATHS 生效，所以 /api/*
        # 不会为每个请求多查一次库。
        if (path in PUBLIC_PATHS or path == "/static"
                or path.startswith("/static/") or path.startswith("/api/")):
            if ".." not in path and "\\" not in path:
                # **白名单页也要尽力解析登录态，只是不拦截。**
                # 原来这里直接 call_next，request.state.user 从未被设置，
                # 于是已登录的人在 /about 被当成未登录：顶栏显示"登录"按钮、
                # 搜索框消失（用户实测踩到 —— 其它标签都正常，唯独关于页
                # "掉登录"）。公开页对未登录访客开放，不代表它该对已登录的
                # 人装不认识。
                # 静态资源不解析：每个字体请求都连一次库纯属浪费。
                if (path in PUBLIC_PATHS
                        or path.startswith("/api/assistant/")) \
                        and auth_mode != "basic":
                    conn = dbmod.connect()
                    try:
                        user = auth.read_token(
                            conn, request.cookies.get(auth.SESSION_COOKIE, ""))
                        if user:
                            request.state.user = user
                            request.state.is_owner = auth.is_owner(conn, user)
                    finally:
                        conn.close()
                return await call_next(request)

        if auth_mode == "basic":
            header = request.headers.get("authorization", "")
            if header.lower().startswith("basic "):
                try:
                    raw = base64.b64decode(header[6:]).decode("utf-8")
                    username, _, password = raw.partition(":")
                except Exception:  # noqa: BLE001 - 畸形凭据一律当失败
                    username = password = ""
                conn = dbmod.connect()
                try:
                    if auth.check_credentials(conn, username, password):
                        request.state.user = username
                        request.state.is_owner = auth.is_owner(conn, username)
                        return await call_next(request)
                finally:
                    conn.close()
            return HTMLResponse(
                "<h1>需要登录</h1><p>请输入账号与口令。</p>",
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="taxassist"'},
            )

        conn = dbmod.connect()
        try:
            user = auth.read_token(conn, request.cookies.get(auth.SESSION_COOKIE, ""))
            owner = auth.is_owner(conn, user) if user else False
        finally:
            conn.close()
        if user:
            request.state.user = user
            request.state.is_owner = owner
            return await call_next(request)
        # 注意 quote 的 safe 里必须保留 %：query 已经是百分号编码形式，
        # 再编码一次会变成 %25（双重编码），进 next 的地址就废了。
        target = request.url.path
        if request.url.query:
            target = f"{target}?{request.url.query}"
        return RedirectResponse(f"/login?next={quote(target, safe='%')}", status_code=302)

    # 后注册的中间件在外层。安全头特意放在最后注册，才能覆盖认证中间件
    # 直接返回的那个 302（它不经过内层）。
    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        return response
