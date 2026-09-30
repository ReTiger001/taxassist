"""用 Playwright 抓「JS 挑战」保护的省级税局站点。

======================================================================
为什么需要它
======================================================================

实测六个省对普通 HTTP 请求返回 412，响应体是**加速乐（Jiasule）WAF 的 JS 挑战**：

    山东 server: wswaf   <script>$_ss=window['$_ss'];$_ss.nsd=100343;...
    北京 server: ******   <script>$_ts=window['$_ts'];$_ts.nsd=109776;...
    湖南 server: core     <script src="/MMB1_.../ZYbKRgIknL7izCT.js"> + set-cookie

机制是：首次请求返回 412 + 一段 JS → 浏览器执行 JS 算出 cookie → 带 cookie
重试才放行。所以换请求头无效（实测三种组合结果完全一致），
curl_cffi 能过纯 TLS 指纹检测（湖北已通）但过不了要执行 JS 的。

真浏览器是正解：它正常执行挑战、拿到 cookie，然后我们取 HTML 交给现有的
`province.parse_list_page` 解析 —— 解析逻辑不用重写。

**不走「逆向 WAF 的 JS」那条路**：GitHub 上确有 jiasule 类仓库，但
① 脆弱（WAF 一更新就失效）；② 属绕过安全机制的灰色地带。用浏览器则是
以正常用户的方式访问公开信息。
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# 真实浏览器的 UA。Playwright 默认 UA 带 "HeadlessChrome"，会被识别。
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# 挑战 JS 执行 + cookie 下发通常在一两秒内完成；给足余量但不是无限等。
_CHALLENGE_WAIT_MS = 6000


def fetch_html(url: str, *, timeout_ms: int = 45000,
               wait_ms: int = _CHALLENGE_WAIT_MS) -> str:
    """用真浏览器取页面 HTML。失败抛异常，由调用方记为 failed。

    Playwright 是可选依赖：只用得到它的省级源才需要装。
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:  # pragma: no cover - 环境相关
        raise RuntimeError(
            "未安装 playwright：pip install playwright 且 playwright install chromium"
        ) from e

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=[
            # 关掉最明显的自动化特征。这不是为了"欺骗"，而是因为
            # 默认 headless 的 UA 与特征会触发挑战死循环，正常用户反而进不去。
            "--disable-blink-features=AutomationControlled",
        ])
        try:
            ctx = browser.new_context(user_agent=_UA, locale="zh-CN",
                                      viewport={"width": 1440, "height": 900})
            page = ctx.new_page()
            page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
            # 等挑战脚本跑完、cookie 落定。不依赖具体选择器 ——
            # 各站的挑战实现不同，等时间比等元素可靠。
            page.wait_for_timeout(wait_ms)
            html = page.content()
        finally:
            browser.close()

    if not html or len(html) < 500:
        raise RuntimeError(f"页面内容过短（{len(html) if html else 0} 字节），可能仍被拦")
    return html


def available() -> tuple[bool, str]:
    """Playwright 与 Chromium 是否就绪 —— 未就绪时给出可执行的修复提示。"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False, "未安装 playwright（pip install playwright）"
    try:
        with sync_playwright() as p:
            path = p.chromium.executable_path
        import os
        if not path or not os.path.exists(path):
            return False, "Chromium 未下载（playwright install chromium）"
        return True, f"就绪（{path.split(os.sep)[-1]}）"
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"
