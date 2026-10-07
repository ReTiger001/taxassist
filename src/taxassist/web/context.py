"""模板上下文（`ctx`）—— 从 `web/app.py` 的 `create_app` 里拆出来。

`ctx(request, **extra)` 是每个页面路由都要用的那个函数：拼出模板需要的公共变量
（当前查询词、`url_with` 快捷筛选、当前账号、是否对外、是否管理员）。

**为什么单独一个模块**：它被**六个**路由模块共用（browse / about / auth /
admin / assistant / billing），却是 create_app 里的一个闭包 —— 想知道"模板到底
拿到哪些变量"得翻进七百行的函数里找。抽出来之后，页面上下文的定义只有一处。

**唯一的闭包依赖是 `require_auth`**，所以做成工厂函数：`make_ctx(require_auth)`，
由 create_app 造好再注入给各路由模块（与其它模块的参数注入范式一致）。
"""
from __future__ import annotations

from urllib.parse import urlencode

from fastapi import Request


def make_ctx(require_auth: bool):
    """造出 ctx 函数（原先它是 create_app 里的闭包）。"""

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

    return ctx
