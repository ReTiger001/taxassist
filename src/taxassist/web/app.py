"""本地 Web 应用：政策检索 / 详情 / 每日简报。

======================================================================
安全边界（不可放宽）
======================================================================

1. **默认只绑定 127.0.0.1** —— 库里含内部效力判断与检索痕迹，对外暴露必须
   显式指定 ``--host``，并在启动时看到风险清单。暴露时**认证是唯一那道门锁**
   （见 auth.py），且**必须配 HTTPS**：没有 TLS，口令在公网路径上是明文。
2. **不引用任何 CDN 或外部资源** —— 断网也能用，且避免任何形式的外部请求，
   与"数据不出本机"的承诺保持一致。
3. **Web 层只读** —— 写操作集中在 pipeline/store，便于审计与复现；
   让界面能改数据会引入"谁改的、依据什么"的追溯问题，收益不值这个风险。
   唯一的例外是账号：注册与登录会话是本层职责（见 auth.py）。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import (
    Response,
)
from fastapi.templating import Jinja2Templates

from .. import auth, filters
from .. import db as dbmod
from . import labels

log = logging.getLogger(__name__)

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(HERE / "templates"))

# 数据值的英文标签注册成 Jinja 全局（见 web/labels.py）：
# 模板里写 {{ en(r.p_region) }} 即可，不用每个路由都把它塞进 context ——
# 这类值出现在分组标题、筛选下拉、结果行、徽章等十几处，逐个传太容易漏。
# 注意 `en` 这个名字会遮蔽 Jinja 内置的同名过滤器（一个"把值转成英文"的
# 老过滤器），本项目模板里没有用过它，换掉是安全的。
templates.env.globals["en"] = labels.en
# 整张表也暴露出去：助手页那部分文字是 JS 现场拼的（依据卡片里的地区、
# 栏目、效力状态），拿不到服务端渲染好的 data-en，只能把表带进页面自己查。
templates.env.globals["labels"] = labels.LABELS


def _highlight(text: str, terms: list[str]):
    """把命中的关键词包成 ``<mark>``，供检索结果高亮。

    **先按命中位置切片、再对每段分别转义**：用户输入绝不能直接拼进 HTML。
    这样形如 ``<script>`` 的查询只会变成字面文本，不会被当成标签。
    """
    from markupsafe import Markup, escape

    if not text:
        return Markup("")
    if not terms:
        return escape(text)

    low = text.lower()
    spans: list[list[int]] = []
    for t in terms:
        tl = (t or "").lower()
        if not tl:
            continue
        start = 0
        while True:
            i = low.find(tl, start)
            if i < 0:
                break
            spans.append([i, i + len(tl)])
            start = i + len(tl)
    if not spans:
        return escape(text)

    spans.sort()
    merged: list[list[int]] = []
    for s, e in spans:
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])

    out = []
    pos = 0
    for s, e in merged:
        out.append(escape(text[pos:s]))
        out.append(Markup("<mark>"))
        out.append(escape(text[s:e]))
        out.append(Markup("</mark>"))
        pos = e
    out.append(escape(text[pos:]))
    return Markup("").join(out)


#: 检索结果高亮用的 filter（模板里写成 ``{{ r.title | hl(hl_terms) }}``）
templates.env.filters["hl"] = _highlight

# 实质政策优先的排序片段统一由 filters 模块生成。
# "什么算实质政策"只该有一处定义，否则 Web、CLI、日报会各排各的，
# 你在不同页面看到的顺序会不一致。
#
# 实测教训：不这样排时，界面第一眼全是"税法小课堂""一图了解""漫画"，
# 真正要看的政策文件（带文号的公告）被压在下面 —— 打开界面看不到重点。
_SUBSTANTIVE_FIRST = filters.substantive_first_sql("p")

# 无需登录即可访问的路径：登录/注册页自身，加上浏览器自动请求的 favicon。
# **只放这三个** —— 每多放一个，就是一处没有门锁的入口。
#: 免登录可访问的路径。
#:
#: 介绍页放在这里是有意的：它不含任何政策数据，是给潜在使用者看的第一眼 ——
#: "要登录才能看介绍"等于把人挡在门外，而第一眼被拦住的人不会再回来。
PUBLIC_PATHS = frozenset({"/login", "/register", "/logout", "/favicon.ico",
                          "/about",
                          # 系统自检。放这里是为了"未登录也能探活"（监控、
                          # 隧道排障）。它只返回运行状态 —— 条数、时间戳、
                          # PID、模型可用性 —— **不含客户数据与政策正文**。
                          "/health"})


def _is_local_request(request: Request) -> bool:
    """请求是否来自本机。

    有些提示只有站长用得上（比如"先执行 python -m taxassist invite 生成邀请码"），
    显示给公网访客既无用，又平白暴露了这个服务跑在本机 —— 所以按来源区分。
    """
    host = request.client.host if request.client else ""
    return host in ("127.0.0.1", "::1", "localhost")


def _safe_next(value: str) -> str:
    """只接受站内路径 —— 否则 next 参数会变成开放重定向跳板。"""
    if not value or not value.startswith("/") or value.startswith("//") or "\\" in value:
        return "/"
    return value


def _is_https(request: Request) -> bool:
    """判断浏览器到本站这一段是不是 HTTPS。

    HTTPS 隧道（Cloudflare Tunnel / frp / nginx）会把原始协议放在
    X-Forwarded-Proto 里，本地直连时则是 http —— 据此决定 Cookie 的 Secure 标志，
    避免本机 http://localhost 调试时因 Secure 导致 Cookie 被浏览器丢弃。
    """
    proto = request.headers.get("x-forwarded-proto", "")
    return proto.split(",")[0].strip().lower() == "https" or request.url.scheme == "https"


def _set_session_cookie(response: Response, request: Request, token: str) -> None:
    response.set_cookie(
        auth.SESSION_COOKIE, token, max_age=auth.SESSION_TTL_SEC,
        httponly=True,          # JS 取不到，降低 XSS 下的损失
        samesite="lax",         # 跨站 POST 不带 Cookie，挡掉绝大多数 CSRF
        secure=_is_https(request),
        path="/",
    )


async def _read_form(request: Request, max_bytes: int = 8192) -> dict:
    """解析 application/x-www-form-urlencoded 表单。

    不引入 python-multipart：只为一个登录表单不值得多一个依赖，
    而本应用要求断网可用、依赖越少越好。

    **必须边读边限流**：`await request.body()` 会先把**整个**请求体收进内存
    再返回，把大小检查放在它之后等于没有检查 —— 匿名 chunked POST 持续灌字节
    就能把进程打爆（安全审计确认的未认证 DoS，而这台机器同时是工作机）。
    所以改用 stream() 逐块累加，一超限立即中止，不再继续读。
    """
    chunks: list[bytes] = []
    total = 0
    try:
        async for chunk in request.stream():
            total += len(chunk)
            if total > max_bytes:
                return {}          # 超限即放弃，别再往下读
            chunks.append(chunk)
    except Exception:  # noqa: BLE001 - 读不到就当空表单，交由校验去报错
        return {}
    body = b"".join(chunks)
    return {k: v[0] for k, v in
            parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True).items()}


async def _read_json(request: Request, max_bytes: int = 8192) -> dict:
    """解析 JSON 请求体，**边读边限流**。

    与 ``_read_form`` 同一个理由：``await request.body()`` 会先把整个请求体
    收进内存，把大小检查放在它之后等于没检查。上传接口把上限放宽到 60MB
    （base64 后的文件），仍必须逐块累加、一超限立即中止。
    """
    chunks: list[bytes] = []
    total = 0
    try:
        async for chunk in request.stream():
            total += len(chunk)
            if total > max_bytes:
                return {}
            chunks.append(chunk)
    except Exception:  # noqa: BLE001 - 读不到当空对象，交由调用方校验
        return {}
    try:
        data = json.loads(b"".join(chunks).decode("utf-8", "replace"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _origin_ok(request: Request) -> bool:
    """同源校验，挡跨站表单提交（CSRF）。

    浏览器对跨站 POST 一定会带上 Origin；非浏览器客户端（脚本、curl）不带，
    也不受 CSRF 影响，故缺失时放行。
    """
    origin = request.headers.get("origin") or request.headers.get("referer") or ""
    if not origin:
        return True
    try:
        return urlsplit(origin).netloc == request.headers.get("host", "")
    except ValueError:
        return False


def _rows(sql: str, params: tuple = ()) -> list:
    conn = dbmod.connect()
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _one(sql: str, params: tuple = ()):
    conn = dbmod.connect()
    try:
        return conn.execute(sql, params).fetchone()
    finally:
        conn.close()


def create_app(require_auth: bool = False, auth_mode: str = "page") -> FastAPI:
    """组装 Web 应用。

    ``require_auth`` 为真时（绑定非 127.0.0.1）启用认证闸门；
    ``auth_mode`` 决定闸门形式：``page`` 为登录页 + 签名 Cookie，
    ``basic`` 为 HTTP Basic（兼容脚本调用，代价是无法登出、无注册入口）。
    """
    # openapi_url 也要关：默认会暴露完整接口清单（含所有 /admin/* 路由），
    # 登录后的任何成员都能拿到，属于不必要的信息泄露。
    app = FastAPI(title="税务智能知识助手", docs_url=None, redoc_url=None,
                  openapi_url=None)

    # 静态资源与模板上下文各自拆成了模块：
    #   · static_assets —— 字体与语言脚本，那里写着"别挂宽"的安全边界
    #   · context —— ctx 函数，被六个路由模块共用，是页面变量的唯一定义处
    from .context import make_ctx
    from .static_assets import register as _register_static

    _register_static(app)
    ctx = make_ctx(require_auth)


    # 健康自检、税务助手、关于页、登录/注册的路由已拆到独立模块
    # （见各模块头部）：依赖用参数注入 —— ctx 是上面那个依赖 require_auth 的
    # 闭包，templates 是模块级的 Jinja2Templates，require_auth 是 create_app 的入参。
    from .about_routes import register as _register_about
    from .admin_routes import register as _register_admin
    from .api_routes import register as _register_api
    from .assistant_routes import register as _register_assistant
    from .auth_routes import register as _register_auth
    from .billing_routes import register as _register_billing
    from .browse_routes import register as _register_browse
    from .middleware import register as _register_middleware

    # 中间件最先接上：它们包裹整个应用
    _register_middleware(app, require_auth=require_auth,
                         auth_mode=auth_mode)
    _register_assistant(app, ctx=ctx, templates=templates)
    _register_browse(app, ctx=ctx, templates=templates)
    _register_about(app, templates=templates, require_auth=require_auth)
    _register_auth(app, templates=templates, require_auth=require_auth)
    _register_admin(app, templates=templates, require_auth=require_auth)
    _register_billing(app, templates=templates, require_auth=require_auth)
    _register_api(app, require_auth=require_auth)
    return app


# ============================================================
# 这个模块级变量的默认值必须是 require_auth=True
# ============================================================
# 它只在 `uvicorn taxassist.web.app:app` 这类**绕开 CLI** 的启动方式下被用到。
# 一旦取默认值 require_auth=False，含义就是「无认证 + is_owner=True」
# （见 create_app 里 `if not require_auth:` 那段）——整站连同 /admin 对全网敞开。
# 换句话说：换个启动方式就等于把后台交出去。
#
# 正常部署走 `python -m taxassist serve`，它显式传参，不受这里影响。
app = create_app(require_auth=True)
