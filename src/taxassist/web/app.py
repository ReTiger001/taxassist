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
import sqlite3
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urlsplit

from fastapi import FastAPI, Query, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import auth, filters, kb
from .. import db as dbmod
from ..translate import to_chinese_query
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

    # 字体文件：**只挂 fonts 这一个子目录**。
    # 认证中间件把 /static/ 整个放行（见下面 PUBLIC_PATHS 那段判断），所以
    # 挂得比 fonts 更宽就等于开一个免认证的文件出口 —— 只暴露几个 woff2
    # 是安全的，挂 static/ 根目录不是。目录不存在时静默跳过，字体回退到
    # 系统字体，功能不受影响。
    font_dir = HERE / "static" / "fonts"
    if font_dir.is_dir():
        app.mount("/static/fonts", StaticFiles(directory=str(font_dir)),
                  name="fonts")

    # 语言脚本：**精确到这一个文件**，理由同上 —— /static/ 整个前缀免认证，
    # 挂 static/ 根目录就等于开一个免认证的文件出口。
    # 用显式路由而不是 mount，是因为挂载的最小单位是目录，没法只暴露其中一个
    # 文件（挂 lang.js 所在目录会把 fonts 之外的任何东西一起放出去）。
    # 这份脚本必须能从**未登录**状态取到：登录页用的是 auth_base.html，
    # 它不继承 base.html，但同样需要语言切换。
    lang_js = HERE / "static" / "lang.js"
    if lang_js.is_file():
        @app.get("/static/lang.js", include_in_schema=False)
        def _lang_js() -> FileResponse:
            return FileResponse(lang_js, media_type="application/javascript")

    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        """对外暴露时的唯一门锁。

        ``require_auth=False``（仅监听本机）时完全放行，本机使用不受打扰。
        page 模式下未登录一律 302 到登录页并带上 next，登录后回到原页面。
        """
        request.state.user = None
        request.state.is_owner = False
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
        if path in PUBLIC_PATHS or path == "/static" or path.startswith("/static/"):
            if ".." not in path and "\\" not in path:
                # **白名单页也要尽力解析登录态，只是不拦截。**
                # 原来这里直接 call_next，request.state.user 从未被设置，
                # 于是已登录的人在 /about 被当成未登录：顶栏显示"登录"按钮、
                # 搜索框消失（用户实测踩到 —— 其它标签都正常，唯独关于页
                # "掉登录"）。公开页对未登录访客开放，不代表它该对已登录的
                # 人装不认识。
                # 静态资源不解析：每个字体请求都连一次库纯属浪费。
                if path in PUBLIC_PATHS and auth_mode != "basic":
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
                import base64

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

    def ctx(request: Request, **kw) -> dict:
        def url_with(**changes) -> str:
            """把当前查询串改几个参数后的 URL（快捷筛选用）。

            为什么放在后端算：快捷筛选是 <a> 链接，点一下必须带上**当前其它
            筛选条件** —— 改年份不该把地区丢掉。在模板里拼 query 容易漏参数，
            这里统一处理；顺带清掉 limit，改条件就回到第一页。
            """
            args = {k: v for k, v in request.query_params.items() if v}
            for key, val in changes.items():
                if val in (None, ""):
                    args.pop(key, None)
                else:
                    args[key] = str(val)
            args.pop("limit", None)
            return "/search" + ("?" + urlencode(args) if args else "")

        base = {
            "request": request,
            "q": request.query_params.get("q", ""),
            "url_with": url_with,
            # 顶栏据此显示当前账号与"退出"；本机模式（无认证）下为 None
            "user": getattr(request.state, "user", None),
            # 是否对外提供访问：决定顶栏徽章写"本地"还是"对外"，
            # 以及未登录时是否显示登录入口
            "exposed": require_auth,
            # 顶栏据此显示"后台"入口。在中间件里已算好，避免每页多查一次库。
            "is_owner": getattr(request.state, "is_owner", False),
        }
        base.update(kw)
        return base

    # ------------------------------------------------------------ 首页

    @app.get("/library", response_class=HTMLResponse)
    def index(request: Request):
        """政策库总览。

        **路径是 /library 而不是 /** —— 首页留给税务助手。用户实测后的判断：
        这个站的核心是"把一件事丢进来、拿到合规判断"，而不是"浏览政策列表"；
        把人先领到一堆文件面前，等于让他自己去找答案。政策库降为第二入口。
        """
        from .. import scheduler

        conn = dbmod.connect()
        try:
            health = scheduler.fetch_health(conn)
            gap = scheduler.days_since_last_success(conn)
            regions = filters.region_counts(conn)
            region_groups = filters.region_groups(conn)
        finally:
            conn.close()

        stats = {
            "total": _one("SELECT COUNT(*) c FROM policy")["c"],
            "valid": _one("SELECT COUNT(*) c FROM policy WHERE p_effect_status='现行有效'")["c"],
            # 现行有效的**依据分布** —— 图例里要写明"多少条有官方标注、多少条
            # 只是未发现废止证据"。客户不该只看到一个笼统的"现行有效 4423"：
            # 这两个数字的可靠性差一个量级，混在一起看就是误导。
            "valid_official": _one(
                "SELECT COUNT(*) c FROM policy WHERE p_effect_status='现行有效'"
                " AND p_effect_source='official'")["c"],
            "valid_default": _one(
                "SELECT COUNT(*) c FROM policy WHERE p_effect_status='现行有效'"
                " AND p_effect_source='default'")["c"],
            "pending": _one("SELECT COUNT(*) c FROM policy WHERE p_effect_status='尚未生效'")["c"],
            "repealed": _one("SELECT COUNT(*) c FROM policy WHERE p_effect_status='已废止'")["c"],
            "review": _one("SELECT COUNT(*) c FROM policy WHERE p_review_state='needs_review'")["c"],
            # **只算真正解析成功的**。原来这里是 COUNT(*) 全表 —— 而页面上
            # 那个数字的标签写的是「已解析附件」，于是 4275 里混进了
            # BadZipFile 388、download_failed 372、no_text_layer 243 等等，
            # 与实际能检索到的正文数（3244）差了 1031 条，名实不符。
            # 全量测试比对「页面显示 vs 库内真值」时揪出来的。
            "attachments": _one(
                "SELECT COUNT(*) c FROM attachment"
                " WHERE parse_status='ok'")["c"],
            "relations": _one("SELECT COUNT(*) c FROM policy_relation")["c"],
            "last_fetch": _one(
                "SELECT started_at FROM fetch_log ORDER BY id DESC LIMIT 1"),
        }
        recent = _rows(
            "SELECT p.doc_uid, p.cwrq, p.title, p.p_doc_no_full, p.p_doc_no_confidence,"
            " p.o_column, p.p_region, p.p_effect_status, p.p_effect_source, p.url"
            f" FROM policy p ORDER BY {_SUBSTANTIVE_FIRST}, p.cwrq DESC LIMIT 25")
        by_column = _rows(
            "SELECT COALESCE(o_column,'(未知)') v, COUNT(*) c FROM policy"
            " GROUP BY v ORDER BY c DESC")
        by_effect = _rows(
            "SELECT COALESCE(p_effect_status,'未判定') v, COUNT(*) c FROM policy"
            " GROUP BY v ORDER BY c DESC")
        return templates.TemplateResponse(
            request=request, name="index.html",
            context=ctx(request, stats=stats, recent=recent, by_column=by_column,
                        by_effect=by_effect,
                        health=health, gap=gap, regions=regions,
                        region_groups=region_groups))

    # ------------------------------------------------------------ 检索

    @app.get("/search", response_class=HTMLResponse)
    def search(request: Request, q: str = Query("", max_length=120),
               column: str = "", tax: str = "", region: str = "",
               effect: str = "", year: str = "", sort: str = "relevance",
               limit: int = Query(50, ge=1, le=200)):
        rows, error = [], None
        # 命中总数：模板要显示「共 N 条」，N 必须是**命中总数**而非返回条数。
        # 无条件浏览时保持 None，模板据此不显示这一行。
        matched_total = None
        # 英文查询先过术语反向映射：搜 "value-added tax" 等同于搜 "增值税"。
        # 这样不必先把 5090 条标题全译一遍，英文词也能命中中文政策。
        q_cn, term_hits = to_chinese_query(q)
        terms = [t for t in q_cn.split() if t]
        # 允许"不输关键词、只按条件浏览" —— 筛选本身就是真实用法。
        # effect / year 也必须算作筛选条件：否则"只看已废止"这种纯筛选会走进
        # 空分支返回 0 条（实测：选「已废止」得 0 条，而库里有 1042 条）。
        if terms or column or tax or region or effect or year:
            if terms and all(len(t) >= 3 for t in terms):
                sql = (
                    "SELECT p.doc_uid, p.cwrq, p.title, p.p_doc_no_full, p.p_doc_no_confidence,"
                    " p.pub_name,"
                    " p.o_column, p.p_region, p.p_effect_status, p.p_effect_source, p.url"
                    " FROM policy_fts f JOIN policy p ON p.id = f.rowid"
                    " WHERE policy_fts MATCH ?"
                )
                # 多词用 AND 连接。整体加引号会变成**短语查询**，
                # "增值税 优惠" 必然 0 条 —— 独立验证代理发现的问题。
                params: list = [" AND ".join(f'"{t}"' for t in terms)]
            elif terms:
                # 含 2 字词（"契税""关税""个税"）：trigram 分词器对 <3 字符的查询
                # 返回空结果，走 FTS 会**静默返回 0 条**，因此退回 LIKE。
                like_clause = " AND ".join(
                    "(p.title LIKE ? OR IFNULL(p.content,'') LIKE ?)" for _ in terms)
                sql = (
                    "SELECT p.doc_uid, p.cwrq, p.title, p.p_doc_no_full, p.p_doc_no_confidence,"
                    " p.pub_name,"
                    " p.o_column, p.p_region, p.p_effect_status, p.p_effect_source, p.url"
                    f" FROM policy p WHERE ({like_clause})"
                )
                params = []
                for t in terms:
                    params += [f"%{t}%", f"%{t}%"]
            else:
                sql = (
                    "SELECT p.doc_uid, p.cwrq, p.title, p.p_doc_no_full, p.pub_name,"
                    " p.o_column, p.p_region, p.p_effect_status, p.p_effect_source, p.url"
                    " FROM policy p WHERE 1=1"
                )
                params = []
            if column:
                sql += " AND p.o_column = ?"
                params.append(column)
            if tax:
                clause, tax_params = filters.tax_filter_sql(tax, "p")
                if clause:
                    sql += f" AND {clause}"
                    params += tax_params
            if region:
                clause, region_params = filters.region_filter_sql(region, "p")
                if clause:
                    sql += f" AND {clause}"
                    params += region_params
            # 效力状态与年份 —— 实际工作中最常用的两个限定
            # （"只看现行有效的""只看今年的"）。
            if effect:
                sql += " AND p.p_effect_status = ?"
                params.append(effect)
            if year.isdigit():
                sql += " AND p.cwrq LIKE ?"
                params.append(f"{year}-%")
            # 相关性排序：必须与 kb.py 的 _build_search 用同一套口径。
            # 那边给 AI/MCP 用、这里给网页用，两处一旦分叉，同一个查询在
            # 网页和 API 里会给出不同顺序，使用者无从判断哪个可信。
            rank_expr = ""
            rank_params: list = []
            if terms and all(len(t) >= 3 for t in terms):
                # FTS5 的 bm25() 越小越相关；权重按 FTS 表列序
                # （title, p_doc_no_full, pub_name, content, o_keywords）。
                rank_expr = f"bm25(policy_fts, {kb.BM25_WEIGHTS}), "
            elif terms:
                # LIKE 没有打分函数：退一步让**标题命中排在正文命中之前**。
                rank_expr = "(CASE WHEN p.title LIKE ? THEN 0 ELSE 1 END), "
                rank_params.append(f"%{terms[0]}%")

            # 命中总数：在追加 LIMIT 之前先算。
            # 原来模板用的是 results|length，于是搜「关税」页面写「共 50 条」，
            # 而实际命中 2348 —— 措辞把人误导成"库里就这么多"。
            # 注意 count 不带 ORDER BY，所以不能把 rank_params 算进去。
            try:
                count_sql = "SELECT COUNT(*) c FROM " + sql.split(" FROM ", 1)[1]
                matched_total = _one(count_sql, tuple(params))["c"]
            except Exception as e:  # noqa: BLE001 - 计数失败不该让整页挂掉
                log.warning("命中计数失败 q=%r: %s", q, e)
                matched_total = None

            if sort == "date_asc":
                sql += " ORDER BY p.cwrq ASC, p.id ASC LIMIT ?"
            elif sort == "date_desc":
                sql += " ORDER BY p.cwrq DESC, p.id DESC LIMIT ?"
            else:
                sql += (f" ORDER BY {rank_expr}{_SUBSTANTIVE_FIRST},"
                        " p.cwrq DESC LIMIT ?")
            # 参数按 ? 在 SQL 里的顺序：WHERE 的 → ORDER BY 的 → LIMIT。
            params += rank_params
            params.append(limit)
            try:
                rows = _rows(sql, tuple(params))
            except Exception as e:  # noqa: BLE001 - 检索出错要让人看见
                # 不回显原始异常：FTS5 的语法错误消息会把检索实现细节
                # （表名、列名、查询语法）泄露到页面上。细节写日志即可。
                log.warning("检索失败 q=%r: %s", q, e)
                error = "检索失败，请调整关键词后重试。"

        columns = [r["v"] for r in _rows(
            "SELECT DISTINCT o_column v FROM policy WHERE o_column IS NOT NULL")]
        conn = dbmod.connect()
        try:
            tax_counts = filters.tax_type_counts(conn)
            regions = filters.region_counts(conn)
            effect_counts = [
                ((r["p_effect_status"] or "未判定"), r["c"])
                for r in _rows("SELECT p_effect_status, COUNT(*) c FROM policy"
                               " GROUP BY 1 ORDER BY c DESC")
            ]
            years = [
                r["y"] for r in _rows(
                    "SELECT DISTINCT SUBSTR(cwrq,1,4) y FROM policy"
                    " WHERE cwrq IS NOT NULL AND LENGTH(cwrq) >= 4"
                    " ORDER BY y DESC")
                if r["y"] and str(r["y"]).isdigit()
            ]
        finally:
            conn.close()
        return templates.TemplateResponse(
            request=request, name="search.html",
            context=ctx(request, results=rows, error=error, columns=columns,
                        column=column, limit=limit, tax=tax, tax_counts=tax_counts,
                        region=region, regions=regions,
                        effect=effect, effect_counts=effect_counts,
                        year=year, years=years, sort=sort,
                        matched_total=matched_total,
                        hl_terms=terms, term_hits=term_hits, q_original=q))

    # ------------------------------------------------------------ 详情

    @app.get("/policy/{doc_uid:path}", response_class=HTMLResponse)
    def policy_detail(request: Request, doc_uid: str):
        policy = _one("SELECT * FROM policy WHERE doc_uid = ?", (doc_uid,))
        if policy is None:
            return HTMLResponse("<h1>404</h1><p>没有这条政策。</p>", status_code=404)
        # 关系查询改为引用 kb.py 的**唯一一份定义**。
        # 原先这里抄了一份，少了 r.evidence_source、p.p_doc_no_full AS target_doc_no
        # 与 s.url 三个字段 —— 结果是同一份政策，走 MCP 接口能看到引用文号、
        # 走网页看不到（2026-10 全量审计发现的分叉）。
        citations = _rows(kb.SQL_CITATIONS, (doc_uid,))
        repealed = _rows(kb.SQL_REPEALED_BY, (doc_uid,))
        # 附件正文：只取前 2 万字渲染 —— 有的申报表附件单篇就好几万字，
        # 全塞进页面会让详情页变得极慢。完整文本仍在库里（attachment.parsed_text），
        # 需要全文时另行导出，页面上会注明已截断。
        attachments = _rows(
            "SELECT filename, ext, url, parse_status,"
            " LENGTH(COALESCE(parsed_text,'')) AS text_len,"
            " SUBSTR(COALESCE(parsed_text,''), 1, 20000) AS text_excerpt"
            " FROM attachment WHERE doc_uid=? ORDER BY id", (doc_uid,))
        snapshots = _rows(
            "SELECT kind, fetched_at, rel_path, size_bytes FROM raw_snapshot"
            " WHERE doc_uid=? ORDER BY id DESC LIMIT 5", (doc_uid,))
        # 英文译文（机器翻译）。模板里必须标注来源 —— 法律文本的译文被当成
        # 官方英文版本引用，是会出事的。
        #
        # **表可能不存在**：translation 由翻译脚本自己 ensure，不在 init_db 的
        # 建表清单里。所以没跑过翻译的库（含全新初始化的库、没升级的老库）
        # 查它会抛 "no such table: translation"。
        # 缺译文只是少一行标注，**不该让整个详情页 500** —— 这里降级为"没有译文"。
        # 只捕获 OperationalError：别的 SQL 问题（写错列名之类）仍要暴露出来。
        try:
            tr_title = _one(
                "SELECT text, model, created_at FROM translation"
                " WHERE doc_uid=? AND field='title' AND lang='en'", (doc_uid,))
            tr_content = _one(
                "SELECT text, model, created_at FROM translation"
                " WHERE doc_uid=? AND field='content' AND lang='en'", (doc_uid,))
        except sqlite3.OperationalError:
            log.warning("translation 表不可用，详情页按“无译文”渲染：%s", doc_uid)
            tr_title = tr_content = None
        return templates.TemplateResponse(
            request=request, name="detail.html",
            context=ctx(request, p=policy, citations=citations, repealed=repealed,
                        attachments=attachments, snapshots=snapshots,
                        tr_title=tr_title, tr_content=tr_content))

    # ------------------------------------------------------------ 每日简报

    @app.get("/daily", response_class=HTMLResponse)
    def daily(request: Request, days: int = Query(14, ge=1, le=365)):
        rows = _rows(
            "SELECT p.doc_uid, p.cwrq, p.pub_date, p.title, p.p_doc_no_full,"
            " p.p_doc_no_confidence, p.pub_name,"
            " p.o_column, p.p_effect_status, p.p_effect_source, p.url,"
            " (SELECT COUNT(*) FROM attachment a WHERE a.doc_uid=p.doc_uid) AS n_attach"
            " FROM policy p"
            " WHERE p.cwrq >= date('now', ?)"
            f" ORDER BY {_SUBSTANTIVE_FIRST}, p.cwrq DESC",
            (f"-{days} days",))
        # 按日期归并分组。**不能依赖"相邻即同组"**：排序是先按实质政策优先、
        # 再按日期，同一天的政策法规与解读会被隔开，顺序分组会产出重复的日期组头
        # （实测：同一天可能出现"09-22 · 6 条"与"09-22 · 2 条"两个组）。
        buckets: dict[str, list] = {}
        for r in rows:
            buckets.setdefault(r["cwrq"] or "(无日期)", []).append(r)
        grouped = sorted(buckets.items(), key=lambda kv: kv[0], reverse=True)
        return templates.TemplateResponse(
            request=request, name="daily.html",
            context=ctx(request, grouped=grouped, days=days, total=len(rows)))

    # ------------------------------------------------------------ 登录 / 注册
    # 健康自检、税务助手、关于页、登录/注册的路由已拆到独立模块
    # （见各模块头部）：依赖用参数注入 —— ctx 是上面那个依赖 require_auth 的
    # 闭包，templates 是模块级的 Jinja2Templates，require_auth 是 create_app 的入参。
    from .about_routes import register as _register_about
    from .admin_routes import register as _register_admin
    from .assistant_routes import register as _register_assistant
    from .auth_routes import register as _register_auth

    _register_assistant(app, ctx=ctx, templates=templates)
    _register_about(app, templates=templates, require_auth=require_auth)
    _register_auth(app, templates=templates, require_auth=require_auth)
    _register_admin(app, templates=templates, require_auth=require_auth)
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
