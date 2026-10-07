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

from fastapi import FastAPI
from fastapi.templating import Jinja2Templates

from . import labels
from .helpers import _highlight

log = logging.getLogger(__name__)

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(HERE / "templates"))

# 数据值的英文标签注册成 Jinja 全局（见 web/labels.py）：
# 模板里写 {{ en(r.p_region) }} 即可，不用每个路由都把它塞进 context ——
# 这类值出现在分组标题、筛选下拉、结果行、徽章等十几处，逐个传太容易漏。
# 注意 `en` 这个名字会遮蔽 Jinja 内置的同名过滤器（一个"把值转成英文"的
# 老过滤器），本项目模板里没有用过它，换掉是安全的。
templates.env.globals["en"] = labels.en

#: 检索结果高亮用的 filter（模板里写成 ``{{ r.title | hl(hl_terms) }}``）。
# 这句注册留在 app.py 而不是跟着 `_highlight` 去 helpers.py：它和上面两句
# 一样属于 **Jinja 环境初始化**，而 `templates` 是 app.py 的对象 —— helpers
# 不导入 app.py（否则循环导入复活），所以由这里反向引用 helpers 的 `_highlight`。
templates.env.filters["hl"] = _highlight
# 整张表也暴露出去：助手页那部分文字是 JS 现场拼的（依据卡片里的地区、
# 栏目、效力状态），拿不到服务端渲染好的 data-en，只能把表带进页面自己查。
templates.env.globals["labels"] = labels.LABELS


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
