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
import re
import uuid
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urlsplit

from fastapi import FastAPI, Query, Request
from fastapi.responses import (HTMLResponse, JSONResponse, RedirectResponse,
                               Response)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import db as dbmod
from ..translate import to_chinese_query
from .. import auth, filters

log = logging.getLogger(__name__)

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(HERE / "templates"))


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
            "attachments": _one("SELECT COUNT(*) c FROM attachment")["c"],
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
                rank_expr = "bm25(policy_fts, 12.0, 8.0, 4.0, 1.0, 2.0), "
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
        citations = _rows(
            "SELECT r.dst_doc_uid, r.dst_doc_no, r.evidence, r.confidence,"
            " p.title AS target_title, p.cwrq AS target_cwrq,"
            " p.p_effect_status AS target_effect"
            " FROM policy_relation r LEFT JOIN policy p ON p.doc_uid = r.dst_doc_uid"
            " WHERE r.src_doc_uid=? AND r.relation='cites' ORDER BY r.id", (doc_uid,))
        repealed = _rows(
            "SELECT r.src_doc_uid, r.dst_doc_no, r.evidence, r.confidence,"
            " s.title AS src_title, s.p_doc_no_full AS src_doc_no_full, s.cwrq AS src_cwrq"
            " FROM policy_relation r LEFT JOIN policy s ON s.doc_uid = r.src_doc_uid"
            " WHERE r.relation='repeals' AND r.dst_doc_uid=? ORDER BY r.id", (doc_uid,))
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
        tr_title = _one(
            "SELECT text, model, created_at FROM translation"
            " WHERE doc_uid=? AND field='title' AND lang='en'", (doc_uid,))
        tr_content = _one(
            "SELECT text, model, created_at FROM translation"
            " WHERE doc_uid=? AND field='content' AND lang='en'", (doc_uid,))
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
    #
    # 这三个路由在中间件里是豁免的（否则没账号的人永远进不来）。
    # 它们只做一件事：核对凭据并下发签名 Cookie。**不提供任何注册之外的写操作。**

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

    # ------------------------------------------------------------ 介绍页

    @app.get("/about", response_class=HTMLResponse)
    def about_page(request: Request):
        """中英双语介绍页。

        **不需要登录**：它不含任何政策数据，是给潜在使用者看的第一眼 ——
        "要登录才能看介绍"等于把人挡在门外。双语切换在前端做（见模板里的
        script），切换不刷新页面、不丢滚动位置。
        """
        me = getattr(request.state, "user", None)
        return templates.TemplateResponse(
            request=request, name="about.html",
            context={"request": request, "user": me,
                     "is_owner": bool(me and getattr(me, "role", "") == "owner"),
                     "exposed": require_auth, "lang": "zh"})

    # ------------------------------------------------------------ 后台（账号管理）
    #
    # 这是 Web 层**唯一**的写操作区，理由：一旦对外提供服务，如果只有命令行能管
    # 账号，那么"撤销某人的访问"就必须先坐到这台电脑前 —— 而客户合作结束、
    # 设备丢失这类事往往等不了。所以这里放开写，但三件事一个都不能少：
    #   ① 只对超级管理员开放；② 全部 POST；③ 全部做同源校验（挡跨站表单）。

    def _owner_ok(request: Request) -> bool:
        return bool(getattr(request.state, "is_owner", False))

    def _admin_back(msg: str = "", err: str = "") -> RedirectResponse:
        """POST 之后一律重定向（PRG），避免刷新时重复提交。"""
        query = urlencode({k: v for k, v in (("msg", msg), ("err", err)) if v})
        return RedirectResponse(f"/admin?{query}" if query else "/admin", status_code=303)

    def _admin_context(request: Request, **extra) -> dict:
        conn = dbmod.connect()
        try:
            users = auth.list_users(conn)
            invites = auth.list_invites(conn)
            # 数据健康：原先显示在总览页，但"抓取未完整完成""用命令行重跑"
            # 这些话面向的是运维者，客户看不懂、也不该看到 —— 移到只对超管
            # 可见的后台。数据来源与总览页一致，页面不再各自算一遍。
            # scheduler 与总览页一样在函数内导入（模块顶部刻意没导它）。
            from .. import scheduler  # noqa: PLC0415

            health = scheduler.fetch_health(conn)
            gap = scheduler.days_since_last_success(conn)
        finally:
            conn.close()
        return {"request": request, "users": users, "invites": invites,
                "user": getattr(request.state, "user", None), "is_owner": True,
                "exposed": require_auth, "min_password": auth.MIN_PASSWORD_LEN,
                "health": health, "gap": gap,
                **extra}

    @app.get("/admin", response_class=HTMLResponse)
    def admin_page(request: Request, msg: str = "", err: str = ""):
        if not _owner_ok(request):
            return HTMLResponse(
                "<h1>无权访问</h1><p>后台只对超级管理员开放。</p>", status_code=403)
        return templates.TemplateResponse(
            request=request, name="admin.html",
            context=_admin_context(request, msg=msg, err=err))

    @app.post("/admin/invite", response_class=HTMLResponse)
    async def admin_create_invite(request: Request):
        if not _owner_ok(request):
            return HTMLResponse("<h1>无权访问</h1>", status_code=403)
        if not _origin_ok(request):
            return _admin_back(err="请求来源异常，请回后台重新提交")
        form = await _read_form(request)
        try:
            days = int(form.get("days") or 30)
        except ValueError:
            days = 30
        conn = dbmod.connect()
        try:
            inv = auth.create_invite(
                conn, note=(form.get("note") or "").strip(), ttl_days=days or None,
                grants_role=(auth.ROLE_OWNER if form.get("role") == auth.ROLE_OWNER
                             else auth.ROLE_MEMBER))
        finally:
            conn.close()
        log.info("后台生成邀请码：%s", inv["note"] or "(无备注)")
        tail = f"，有效期至 {inv['expires_at'][:10]}" if inv["expires_at"] else "，不过期"
        kind = ("管理员邀请码（注册后能进后台）"
                if inv["grants_role"] == auth.ROLE_OWNER else "邀请码")
        return _admin_back(msg=f"已生成{kind} {inv['code']}{tail}")

    @app.post("/admin/invite/revoke", response_class=HTMLResponse)
    async def admin_revoke_invite(request: Request):
        if not _owner_ok(request):
            return HTMLResponse("<h1>无权访问</h1>", status_code=403)
        if not _origin_ok(request):
            return _admin_back(err="请求来源异常，请回后台重新提交")
        code = (await _read_form(request)).get("code", "")
        conn = dbmod.connect()
        try:
            ok = auth.revoke_invite(conn, code)
        finally:
            conn.close()
        if ok:
            return _admin_back(msg=f"已吊销 {code}")
        return _admin_back(err=f"未吊销 {code}：不存在、已经被使用过、或已经吊销过")

    @app.post("/admin/user/delete", response_class=HTMLResponse)
    async def admin_delete_user(request: Request):
        if not _owner_ok(request):
            return HTMLResponse("<h1>无权访问</h1>", status_code=403)
        if not _origin_ok(request):
            return _admin_back(err="请求来源异常，请回后台重新提交")
        target = ((await _read_form(request)).get("username") or "").strip()
        conn = dbmod.connect()
        try:
            # 防呆：删掉最后一个超级管理员，等于把自己锁在门外
            if auth.is_owner(conn, target) and auth.owner_count(conn) <= 1:
                return _admin_back(err="这是最后一个超级管理员，删掉就没人能进后台了")
            removed = auth.delete_user(conn, target)
        finally:
            conn.close()
        if not removed:
            return _admin_back(err=f"没有找到账号 {target}")
        log.info("后台撤销访问权：%s", target)
        return _admin_back(msg=f"已删除账号 {target}，其登录状态立即失效")

    @app.post("/admin/user/role", response_class=HTMLResponse)
    async def admin_set_role(request: Request):
        if not _owner_ok(request):
            return HTMLResponse("<h1>无权访问</h1>", status_code=403)
        if not _origin_ok(request):
            return _admin_back(err="请求来源异常，请回后台重新提交")
        form = await _read_form(request)
        target = (form.get("username") or "").strip()
        want_owner = form.get("role") == auth.ROLE_OWNER
        conn = dbmod.connect()
        try:
            if (not want_owner and auth.is_owner(conn, target)
                    and auth.owner_count(conn) <= 1):
                return _admin_back(err="这是最后一个超级管理员，不能取消其权限")
            changed = auth.set_role(
                conn, target, auth.ROLE_OWNER if want_owner else auth.ROLE_MEMBER)
        finally:
            conn.close()
        if not changed:
            return _admin_back(err=f"没有找到账号 {target}")
        return _admin_back(msg=f"{target} 已{'设为' if want_owner else '取消'}超级管理员")

    # 健康自检与税务助手的路由已拆到 assistant_routes（见该模块头部）：
    # 依赖用参数注入 —— ctx 是上面那个依赖 require_auth 的闭包，
    # templates 是模块级的 Jinja2Templates。
    from .assistant_routes import register as _register_assistant

    _register_assistant(app, ctx=ctx, templates=templates)
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
