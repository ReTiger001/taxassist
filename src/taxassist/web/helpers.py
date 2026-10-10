"""Web 层的公共辅助 —— 从 `web/app.py` 搬出来的一批模块级函数。

**为什么单独一个模块**：这些函数（HTML 高亮、表单/JSON 读取、同源校验、
会话 Cookie、两个 SQL 便捷函数、公开路径表……）原先住在 `app.py` 里，而
`app.py` **在模块级就执行 `app = create_app()`** —— 于是各路由模块想用它们，
只能在自己的 `register()` 里做「函数内导入」绕开循环导入。抽到这里循环就断了。

**本模块绝不能导入 `app.py`**（这条得守住，否则循环导入复活）；各路由模块
改成从本模块做常规的模块级导入。
"""
from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

from fastapi import Request
from fastapi.responses import Response

from .. import auth, filters
from .. import db as dbmod


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


# 实质政策优先的排序片段统一由 filters 模块生成。
# "什么算实质政策"只该有一处定义，否则 Web、CLI、日报会各排各的，
# 你在不同页面看到的顺序会不一致。
#
# 实测教训：不这样排时，界面第一眼全是"税法小课堂""一图了解""漫画"，
# 真正要看的政策文件（带文号的公告）被压在下面 —— 打开界面看不到重点。
_SUBSTANTIVE_FIRST = filters.substantive_first_sql("p")

# 无需登录即可访问的路径：登录/注册页自身、浏览器自动请求的 favicon，
# 以及两个"给客户看"的页面（介绍、价格）。
# **只放必需的这几项** —— 每多放一个，就是一处没有门锁的入口。
#: 免登录可访问的路径。
#:
#: 介绍页放在这里是有意的：它不含任何政策数据，是给潜在使用者看的第一眼 ——
#: "要登录才能看介绍"等于把人挡在门外，而第一眼被拦住的人不会再回来。
PUBLIC_PATHS = frozenset({"/login", "/register", "/logout", "/favicon.ico",
                          "/about",
                          # 价格页同理，而且更硬：客户决定买之前的第一步就是看
                          # "能拿到什么、多少钱、有什么坑"。这一条曾漏在外面 ——
                          # 顶栏一直挂着「服务与价格」入口，未登录点进去却被弹回
                          # 登录页（实测），潜在客户连价目表都看不到。
                          # 它与政策数据无关（只有能力说明与价格），放行不泄露库内容。
                          "/pricing",
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


