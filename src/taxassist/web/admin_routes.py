"""后台（账号管理）—— 从 `web/app.py` 的 `create_app` 里拆出来。

**为什么单独一个模块**：这是 Web 层**唯一**的写操作区，规则也最多（只对超管
开放、全部 POST、全部做同源校验），混在 700 行的 create_app 里很难一眼看全。
拆出来之后，这块的边界与约束都在一处。

依赖用参数注入（与 assistant_routes / about_routes / auth_routes 同范式）。
对 app.py 的模块级辅助在 register() 里取一次，供下面各路由闭包捕获 ——
app.py 在模块级就执行 create_app()，模块级导入会成环（详见 assistant_routes）。
"""
from __future__ import annotations

import logging
from urllib.parse import urlencode

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import auth
from .. import db as dbmod
from .helpers import _origin_ok, _read_form

log = logging.getLogger(__name__)

def register(app, *, templates, require_auth: bool) -> None:
    """把 /admin 五个路由挂到 app 上。"""
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
            from .. import scheduler

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
            try:
                inv = auth.create_invite(
                    conn,
                    note=(form.get("note") or "").strip(), ttl_days=days or None,
                    grants_role=(auth.ROLE_OWNER if form.get("role") == auth.ROLE_OWNER
                                 else auth.ROLE_MEMBER))
            except ValueError as exc:
                # 备注过长等输入问题：**把消息原样显示给使用者**，而不是
                # 500 —— 这类消息本来就是写给使用者看的（与账号名校验一致）。
                return _admin_back(err=str(exc))
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
        # 防呆一：不能对自己动手。超管把自己删了之后谁也恢复不了 ——
        # 用户原话：「我要是取消了自己的超管，那哪还有超管呢？」
        # 所以规则是只能处置**其他**账号，自己这个超管动不了。
        me = getattr(request.state, "user", None)
        if target and target == me:
            return _admin_back(err="不能删除自己的账号 —— 请让另一位超级管理员操作")
        conn = dbmod.connect()
        try:
            # 防呆二：不能删掉最后一个超级管理员，等于把自己锁在门外。
            # 有上面那条之后理论上到不了这里（自己动不了 ⇒ 至少留一个），
            # 留着是兜底：将来若加了别的改角色路径，这条仍能守住"后台进得去"。
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
        # 防呆一：不能改自己的角色。超管把自己降成普通成员后，后台就再没人
        # 能把它改回来 —— 这不是"多一层确认"，是唯一能防止自锁的检查。
        # 只能给**其他**账号升/降级。
        me = getattr(request.state, "user", None)
        if target and target == me:
            return _admin_back(err="不能修改自己的角色 —— 请让另一位超级管理员操作")
        conn = dbmod.connect()
        try:
            # 防呆二：最后一个超管不能取消（兜底，理由同上）
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

