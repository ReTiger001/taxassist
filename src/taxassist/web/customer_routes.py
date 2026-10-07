"""客户自助后台（`/account`）—— 普通成员看自己的余额、用量与 API 接入。

**为什么单独一个模块**：`/admin/*` 是超管专属（管**别人**），这里管**自己**。
两者权限模型正好相反 —— 放同一个模块里，将来某次改动很容易把 `is_owner`
检查复制到错误的位置，而那种错误在代码上是看不出来的（只是少了一行）。

**账户与计费客户怎么对应**：**同名**。注册时 `auth_routes` 已经调过
`billing.ensure_customer` 建好了同名账户，所以这里只按 `request.state.user` 取。

**这里不做 `is_owner` 检查**：页面本身对任何登录用户开放，它只读
`request.state.user` 自己名下的数据 —— 拿不到别人的余额，也改不了别人的。
"""
from __future__ import annotations

import logging
from urllib.parse import urlencode

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .. import billing
from .. import db as dbmod
from .helpers import _origin_ok, _read_form

log = logging.getLogger(__name__)


def register(app, *, templates, require_auth: bool) -> None:
    """把 /account 挂到 app 上（与其它模块同范式：依赖参数注入）。"""

    def _me(request: Request) -> str:
        """当前登录用户名 —— 同时也是计费客户名（注册时已建同名账户）。"""
        return (getattr(request.state, "user", None) or "").strip()

    def _render(request: Request, msg: str = "", err: str = ""):
        name = _me(request)
        conn = dbmod.connect()
        try:
            # 兜底补建：在"注册即开户"上线**之前**注册的老账号名下还没有计费
            # 客户。这里补一个，否则他打开这页只会看到"查无此客户"，却不知道
            # 为什么、也不知道找谁。ensure_customer 对已存在的名字什么都不改。
            billing.ensure_customer(conn, name)
            rows = billing.usage_summary(conn, name)
            calls = billing.recent_calls(conn, name, limit=40)
            keys = billing.list_keys(conn, name)
        finally:
            conn.close()
        return templates.TemplateResponse(
            request=request, name="account.html",
            context={"request": request, "user": name,
                     "is_owner": getattr(request.state, "is_owner", False),
                     "exposed": require_auth, "lang": "zh",
                     "acct": rows[0] if rows else None,
                     "calls": calls, "keys": keys, "msg": msg, "err": err})

    def _back(msg: str = "", err: str = "") -> RedirectResponse:
        """PRG：POST 完回 GET，避免刷新重复提交。"""
        qs = urlencode({k: v for k, v in (("msg", msg), ("err", err)) if v})
        return RedirectResponse("/account" + ("?" + qs if qs else ""),
                                status_code=303)

    @app.get("/account", response_class=HTMLResponse)
    def account_page(request: Request, msg: str = "", err: str = ""):
        """我的账户：两个余额、用量明细、Key 管理、怎么充值、怎么接入。"""
        return _render(request, msg=msg, err=err)

    @app.get("/account/docs", response_class=HTMLResponse)
    def account_docs(request: Request):
        """API / MCP 接入文档（面向客户）。

        **不查库、不查余额**：它是一页静态说明，任何登录用户都能看。放在
        `/account/` 下而不是顶层 `/docs`，因为它是「拿到 Key 之后怎么用」的
        一部分 —— 与余额、用量是同一件事的三个面，客户在同一个地方找。
        """
        name = _me(request)
        return templates.TemplateResponse(
            request=request, name="docs.html",
            context={"request": request, "user": name,
                     "is_owner": getattr(request.state, "is_owner", False),
                     "exposed": require_auth, "lang": "zh"})

    @app.get("/account/mcp.json", response_class=JSONResponse)
    def account_mcp_json():
        """给客户下载/复制的 MCP 配置模板。

        **为什么是模板而不是填好的**：两处必须由客户自己填 ——
        ① 服务器地址：取决于他从哪台机器访问，本机的 127.0.0.1 在别人那里
           不是同一个东西；② 密钥：库里只有 sha256，服务端**拿不回明文**，
           所以没有任何办法替他填上。
        模板里用 <主机:端口> 与 tk_你的密钥 两个占位符标出这两处。

        两种写法都给：
        · url + headers —— Cursor / Claude Code / VS Code 支持 HTTP transport
        · command + args —— Claude Desktop 只支持 stdio，必须用 npx mcp-remote
          桥接一道（这是社区为此专门做的代理）
        """
        return JSONResponse({
            "mcpServers": {
                "taxassist": {
                    "url": "http://<主机:端口>/api/mcp",
                    "headers": {"Authorization": "Bearer tk_你的密钥"},
                },
                "taxassist-via-mcp-remote": {
                    "command": "npx",
                    "args": ["-y", "mcp-remote", "http://<主机:端口>/api/mcp",
                             "--header", "Authorization: Bearer tk_你的密钥"],
                },
            }
        })

    @app.post("/account/key", response_class=HTMLResponse)
    async def account_issue_key(request: Request):
        """申请一枚 API Key —— **申请即发**。

        为什么不用审批：**余额为 0 时接口一律 402，拿着 Key 也调不动**，所以
        先给 Key 没有风险；等超管充完值它自然就能用了。多一道审批只会让客户
        卡在"想要个 Key 试试"这一步上。

        Key 只在这一次响应里出现 —— 库里存的是 sha256，离开本页就再也拿不到，
        丢了只能重新申请（然后吊销旧的）。
        """
        if not _origin_ok(request):
            return _back(err="请求来源异常，请回本页重新提交")
        name = _me(request)
        label = ((await _read_form(request)).get("label") or "").strip()[:40]
        conn = dbmod.connect()
        try:
            raw = billing.create_key(conn, name, label=label)
        finally:
            conn.close()
        # 明文排在 msg 里回跳 —— 只出现这一次，见 docstring
        return _back(msg=f"已签发 API Key（只显示这一次，请立刻抄下）：{raw}")

    @app.post("/account/key/revoke", response_class=HTMLResponse)
    async def account_revoke_key(request: Request):
        """吊销自己名下的一枚 Key。**只能吊销自己的**（见 billing.revoke_key_by_id）。"""
        if not _origin_ok(request):
            return _back(err="请求来源异常，请回本页重新提交")
        name = _me(request)
        key_id = (await _read_form(request)).get("key_id") or ""
        conn = dbmod.connect()
        try:
            ok = billing.revoke_key_by_id(conn, name, key_id)
        finally:
            conn.close()
        if not ok:
            return _back(err="没有找到这枚 Key（可能已被吊销）")
        log.info("客户 %s 自助吊销了 Key %s", name, key_id)
        return _back(msg="已吊销。这枚 Key 立刻失效，其调用将返回 401。")
