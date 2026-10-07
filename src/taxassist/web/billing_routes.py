"""客户与余额（后台）—— `/admin/customers`。

**为什么单独一个页面而不是塞进现有后台**：现有 `/admin` 管的是"谁能进这个站"
（账号与邀请码），这一页管的是"谁能调 API、还剩多少次"。两件事的读者都是站长，
但对象不同（人 vs 客户），混在一页会让两边都看不清。

**只有超管能进**：与 `/admin` 同一条纪律 —— 余额是钱，不能给普通成员看。

依赖用参数注入（与其它路由模块同范式）。
"""
from __future__ import annotations

import logging
from urllib.parse import urlencode

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import billing
from .. import db as dbmod
from .helpers import _origin_ok, _read_form

log = logging.getLogger(__name__)

def register(app, *, templates, require_auth: bool) -> None:
    """把 /admin/customers 挂到 app 上。"""
    def _back(msg: str = "", err: str = "") -> RedirectResponse:
        query = urlencode({k: v for k, v in (("msg", msg), ("err", err)) if v})
        return RedirectResponse(f"/admin/customers?{query}" if query else "/admin/customers",
                                status_code=303)

    def _is_owner(request: Request) -> bool:
        return bool(getattr(request.state, "is_owner", False))

    def _context(request: Request, **extra) -> dict:
        conn = dbmod.connect()
        try:
            rows = billing.usage_summary(conn)
            calls = {r["name"]: billing.recent_calls(conn, r["name"], 8) for r in rows}
        finally:
            conn.close()
        return {"request": request, "exposed": require_auth,
                "is_owner": True, "lang": "zh",
                "customers": rows, "calls": calls, **extra}

    @app.get("/admin/customers", response_class=HTMLResponse)
    def customers_page(request: Request, msg: str = "", err: str = ""):
        if not _is_owner(request):
            return HTMLResponse("<h1>无权访问</h1>", status_code=403)
        return templates.TemplateResponse(
            request=request, name="customers.html",
            context=_context(request, msg=msg, err=err))

    @app.post("/admin/customers/add", response_class=HTMLResponse)
    async def customers_add(request: Request):
        """建客户 / 加余额 —— **收款走人工充值，所以这一条是必需的**。"""
        if not _is_owner(request):
            return HTMLResponse("<h1>无权访问</h1>", status_code=403)
        if not _origin_ok(request):
            return _back(err="请求来源异常，请回后台重新提交")
        form = await _read_form(request)
        name = (form.get("name") or "").strip()
        kind = (form.get("kind") or "").strip()
        note = (form.get("note") or "").strip()
        try:
            amount = int((form.get("amount") or "0").strip() or 0)
        except ValueError:
            return _back(err="数量要填整数")
        if not name:
            return _back(err="客户名不能为空")
        if len(name) > 40:
            return _back(err="客户名最多 40 个字符")
        # **"只建客户"不需要计费种类** —— 种类是加额才用得到的。
        # 原来这个检查写在 create 分支之前，于是"只建客户"必被拦下，回一句
        # "请选择计费种类（检索 / 助手）" —— 而那个表单里根本没有选择框。
        # 端到端测试时表现为：三次 POST 全返回 303（PRG），但库里什么都没有。
        create_only = form.get("action") == "create"
        if not create_only and kind not in (billing.KIND_SEARCH, billing.KIND_ASSISTANT):
            return _back(err="请选择计费种类（检索 / 助手）")

        conn = dbmod.connect()
        try:
            billing.ensure_tables(conn)
            if create_only:
                billing.create_customer(conn, name, note=note)
                log.info("后台新建客户：%s", name)
                return _back(msg=f"已建客户 {name}（余额为 0，请加额后再发 Key）")
            after = billing.add_balance(conn, name, kind, amount)
        except ValueError as exc:
            return _back(err=str(exc))
        finally:
            conn.close()
        label = "检索" if kind == billing.KIND_SEARCH else "助手"
        return _back(msg=f"{name} 的{label}余量已调整为 {after}（本次 {amount:+d}）")

    @app.post("/admin/customers/adjust", response_class=HTMLResponse)
    async def customers_adjust(request: Request):
        """行内加额：直接从客户名单那一行改余额。

        **为什么单独一条路由**：/add 要同时管"建客户"和"加额"，所以客户名是
        一堆输入框里的一个 —— 要给名单里第 7 个客户加额，得先把名字抄下来再
        填进去（抄错就是"没有这个客户"）。这条只认 name / kind / amount，
        模板在每一行里带上隐藏的客户名，点一下就到账。
        """
        if not _is_owner(request):
            return HTMLResponse("<h1>无权访问</h1>", status_code=403)
        if not _origin_ok(request):
            return _back(err="请求来源异常，请回后台重新提交")
        form = await _read_form(request)
        name = (form.get("name") or "").strip()
        kind = (form.get("kind") or "").strip()
        if kind not in (billing.KIND_SEARCH, billing.KIND_ASSISTANT):
            return _back(err="请选择计费种类（检索 / 助手）")
        try:
            amount = int((form.get("amount") or "").strip() or 0)
        except ValueError:
            return _back(err="数量要填整数")
        conn = dbmod.connect()
        try:
            after = billing.add_balance(conn, name, kind, amount)
        except ValueError as exc:
            return _back(err=str(exc))
        finally:
            conn.close()
        label = "检索" if kind == billing.KIND_SEARCH else "助手"
        log.info("后台行内加额：%s 的%s %+d → %d", name, label, amount, after)
        return _back(msg=f"{name} 的{label}余量已调整为 {after}（本次 {amount:+d}）")

    @app.post("/admin/customers/key", response_class=HTMLResponse)
    async def customers_key(request: Request):
        """给客户签一枚 API Key。**明文只在这一次返回**，库里只有哈希。"""
        if not _is_owner(request):
            return HTMLResponse("<h1>无权访问</h1>", status_code=403)
        if not _origin_ok(request):
            return _back(err="请求来源异常，请回后台重新提交")
        form = await _read_form(request)
        name = (form.get("name") or "").strip()
        if not name:
            return _back(err="缺少客户名")
        conn = dbmod.connect()
        try:
            if not billing.get_balance(conn, name)["exists"]:
                return _back(err=f"没有这个客户：{name}")
            raw = billing.create_key(conn, name, label=(form.get("label") or "").strip())
        finally:
            conn.close()
        return _back(msg=f"已为 {name} 签发 Key（请立刻抄下，本页离开后无法再看到）：{raw}")
