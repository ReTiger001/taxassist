"""关于页（/about）—— 从 `web/app.py` 的 `create_app` 里拆出来。

**为什么单独一个模块**：`create_app` 里堆了 22 个路由闭包、728 行，读的人得在
一个函数里翻很久才能找到想看的那段。这个页面最独立（不用 `ctx()`、不碰任何
政策数据），所以先拆它、顺便验证拆分范式。

**依赖用参数注入**（与 `assistant_routes` 同）：`templates` 是模块级的
Jinja2Templates；`require_auth` 是 create_app 的入参，决定顶栏徽章写"本地"还是
"对外"、未登录时是否显示登录入口 —— 它原本是闭包变量，拆出来后必须显式传入。
"""
from __future__ import annotations

from fastapi import Request
from fastapi.responses import HTMLResponse


def register(app, *, templates, require_auth: bool) -> None:
    """把 /about 挂到 app 上。"""

    @app.get("/about", response_class=HTMLResponse)
    def about_page(request: Request):
        """中英双语介绍页。

        **不需要登录**：它不含任何政策数据，是给潜在使用者看的第一眼 ——
        "要登录才能看介绍"等于把人挡在门外。双语切换在前端做（见模板里的
        script），切换不刷新页面、不丢滚动位置。
        """
        me = getattr(request.state, "user", None)
        # is_owner 必须从 request.state 取 —— 中间件解析会话时已经写好了
        # （见 app.auth_middleware）。原来这里写的是
        #     bool(me and getattr(me, "role", "") == "owner")
        # 把 me 当成对象了，而 request.state.user 是**用户名字符串**，
        # 对字符串取 role 永远得到空串 → is_owner 恒为 False → 关于页连
        # 超级管理员都不显示「后台」按钮（用户实测踩到）。
        return templates.TemplateResponse(
            request=request, name="about.html",
            context={"request": request, "user": me,
                     "is_owner": getattr(request.state, "is_owner", False),
                     "exposed": require_auth, "lang": "zh"})

    @app.get("/pricing", response_class=HTMLResponse)
    def pricing_page(request: Request):
        """中英双语「服务与价格」页。

        **同样不需要登录**：客户在决定买之前要看清楚能拿到什么、怎么算钱、
        怎么接进来 —— 放在后台里等于"先买再看"。

        内容纪律：这一页写的是**怎么算**（按次、两项分开、成功才扣、余额预充），
        不写**多少钱** —— 单价会变，写死在页面里迟早对不上，具体数字见合同。
        文案里的成本量级（首字约 2 秒、整篇约 20 秒）是实测值，不是估计。
        """
        me = getattr(request.state, "user", None)
        return templates.TemplateResponse(
            request=request, name="pricing.html",
            context={"request": request, "user": me,
                     "is_owner": getattr(request.state, "is_owner", False),
                     "exposed": require_auth, "lang": "zh"})
