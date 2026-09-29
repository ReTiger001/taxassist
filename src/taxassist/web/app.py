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

import logging
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urlsplit

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from .. import db as dbmod
from .. import auth, filters

log = logging.getLogger(__name__)

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(HERE / "templates"))

# 实质政策优先的排序片段统一由 filters 模块生成。
# "什么算实质政策"只该有一处定义，否则 Web、CLI、日报会各排各的，
# 你在不同页面看到的顺序会不一致。
#
# 实测教训：不这样排时，界面第一眼全是"税法小课堂""一图了解""漫画"，
# 真正要看的政策文件（带文号的公告）被压在下面 —— 打开界面看不到重点。
_SUBSTANTIVE_FIRST = filters.substantive_first_sql("p")

# 无需登录即可访问的路径：登录/注册页自身，加上浏览器自动请求的 favicon。
# **只放这三个** —— 每多放一个，就是一处没有门锁的入口。
PUBLIC_PATHS = frozenset({"/login", "/register", "/logout", "/favicon.ico"})


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


async def _read_form(request: Request) -> dict:
    """解析 application/x-www-form-urlencoded 表单。

    不引入 python-multipart：只为一个登录表单不值得多一个依赖，
    而本应用要求断网可用、依赖越少越好。
    """
    try:
        body = await request.body()
    except Exception:  # noqa: BLE001 - 读不到就当空表单，交由校验去报错
        return {}
    if len(body) > 8192:
        return {}
    return {k: v[0] for k, v in
            parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True).items()}


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
    app = FastAPI(title="税务智能知识助手", docs_url=None, redoc_url=None)

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
        if path in PUBLIC_PATHS or path.startswith("/static/"):
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
        base = {
            "request": request,
            "q": request.query_params.get("q", ""),
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

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        from .. import scheduler

        conn = dbmod.connect()
        try:
            health = scheduler.fetch_health(conn)
            gap = scheduler.days_since_last_success(conn)
            regions = filters.region_counts(conn)
        finally:
            conn.close()

        stats = {
            "total": _one("SELECT COUNT(*) c FROM policy")["c"],
            "valid": _one("SELECT COUNT(*) c FROM policy WHERE p_effect_status='现行有效'")["c"],
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
        return templates.TemplateResponse(
            request=request, name="index.html",
            context=ctx(request, stats=stats, recent=recent, by_column=by_column,
                        health=health, gap=gap, regions=regions))

    # ------------------------------------------------------------ 检索

    @app.get("/search", response_class=HTMLResponse)
    def search(request: Request, q: str = Query("", max_length=120),
               column: str = "", tax: str = "", region: str = "",
               limit: int = Query(50, ge=1, le=200)):
        rows, error = [], None
        terms = [t for t in q.split() if t]
        # 允许"不输关键词、只按栏目/税种/地区浏览" —— 筛选本身就是真实用法
        if terms or column or tax or region:
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
            sql += f" ORDER BY {_SUBSTANTIVE_FIRST}, p.cwrq DESC LIMIT ?"
            params.append(limit)
            try:
                rows = _rows(sql, tuple(params))
            except Exception as e:  # noqa: BLE001 - 检索出错要让人看见
                error = f"检索失败：{e}"

        columns = [r["v"] for r in _rows(
            "SELECT DISTINCT o_column v FROM policy WHERE o_column IS NOT NULL")]
        conn = dbmod.connect()
        try:
            tax_counts = filters.tax_type_counts(conn)
            regions = filters.region_counts(conn)
        finally:
            conn.close()
        return templates.TemplateResponse(
            request=request, name="search.html",
            context=ctx(request, results=rows, error=error, columns=columns,
                        column=column, limit=limit, tax=tax, tax_counts=tax_counts,
                        region=region, regions=regions))

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
        attachments = _rows(
            "SELECT filename, ext, url, parse_status,"
            " LENGTH(COALESCE(parsed_text,'')) AS text_len"
            " FROM attachment WHERE doc_uid=? ORDER BY id", (doc_uid,))
        snapshots = _rows(
            "SELECT kind, fetched_at, rel_path, size_bytes FROM raw_snapshot"
            " WHERE doc_uid=? ORDER BY id DESC LIMIT 5", (doc_uid,))
        return templates.TemplateResponse(
            request=request, name="detail.html",
            context=ctx(request, p=policy, citations=citations, repealed=repealed,
                        attachments=attachments, snapshots=snapshots))

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
        finally:
            conn.close()
        return {"request": request, "users": users, "invites": invites,
                "user": getattr(request.state, "user", None), "is_owner": True,
                "exposed": require_auth, "min_password": auth.MIN_PASSWORD_LEN,
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

    return app


app = create_app()
