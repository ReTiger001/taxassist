"""自托管静态资源：字体与语言脚本 —— 从 `web/app.py` 的 `create_app` 里拆出来。

**这里有一条不能松的安全边界，改之前请先读完**：认证中间件把 `/static/` 整个
前缀放行（见 middleware.py 里的判断），所以**挂得越宽，免认证的文件出口就越大**。
现在的做法刻意收窄到最小：

  · 字体：只 `mount` `static/fonts` 这一个**子目录** —— 里面只有两个 woff2。
  · `lang.js`：用**显式路由**而不是 mount —— 挂载的最小单位是目录，没法只暴露
    其中一个文件（挂它所在目录会把 fonts 之外的任何东西一起放出去）。
    这份脚本必须能从**未登录**状态取到：登录页用的是 auth_base.html，
    它不继承 base.html，但同样需要语言切换。

**要加新的静态资源，请逐个文件加路由**，不要图省事去挂 static/ 根目录 ——
那样等于开一个免认证的文件出口。
"""
from __future__ import annotations

from pathlib import Path

from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

HERE = Path(__file__).parent


def register(app) -> None:
    """挂上字体目录与语言脚本路由。"""
    font_dir = HERE / "static" / "fonts"
    if font_dir.is_dir():
        app.mount("/static/fonts", StaticFiles(directory=str(font_dir)),
                  name="fonts")

    lang_js = HERE / "static" / "lang.js"
    if lang_js.is_file():
        @app.get("/static/lang.js", include_in_schema=False)
        def _lang_js() -> FileResponse:
            return FileResponse(lang_js, media_type="application/javascript")
