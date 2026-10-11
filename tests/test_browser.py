"""浏览器层（collect/browser.py）的测试 —— 只测**不依赖真浏览器**的那部分。

**为什么只测这些**：`fetch_html` / `fetch_many` 要真的 Chromium（44 个 needs_js
源全靠它），单测里起浏览器既慢又脆，测出来的是"这台机器有没有装浏览器"而不是
我们的代码。而 `available()` 不同 —— 它是**纯逻辑**，却承担着两条关键契约：

  ① **它自己绝不能抛**：`/health` 的存在意义就是"系统坏了也能查"，
     最需要它的时候它反而不可用，是这类自检端点最典型的失败。
  ② **报错要如实、要可执行**：审计（2026-10）记过一次误导 —— 浏览器栈的
     mkdtemp 失败被报成"页面已改版"，48 个源同时归零却指向了错方向，
     排查方向被带偏。所以这里专门钉住：**底层异常要原样带出来，
     不能被替换成一句听起来很确定的结论**。
"""
from __future__ import annotations

import sys
import types

from taxassist.collect import browser


def test_available_never_raises_and_always_explains():
    """无论环境如何，`available()` 都必须返回 ``(bool, 非空说明)``。

    在真实的这台机器上跑 —— 装没装 playwright 都成立。这条看着平淡，但它挡的
    是"自检端点自己崩了"：`/health` 靠它报模型/浏览器状态。
    """
    ok, why = browser.available()

    assert isinstance(ok, bool)
    assert isinstance(why, str) and why.strip(), "说明不能是空字符串"


def test_missing_playwright_gives_an_actionable_hint(monkeypatch):
    """没装 playwright 时，提示必须**能照着做**。

    "浏览器不可用"这种话没法执行；`pip install playwright` 可以。
    """
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)  # import 时抛 ImportError

    ok, why = browser.available()

    assert ok is False
    assert "pip install playwright" in why, f"提示不可执行：{why!r}"


def test_underlying_exception_is_reported_as_is(monkeypatch):
    """底层异常必须**原样带出来**（类型名 + 原始消息），不能被换成结论。

    这是审计记过的那次教训：浏览器栈的 mkdtemp 失败被报成"页面已改版"，
    48 个源同时归零却指向错方向。判断"是站点改版还是本机临时目录被清了"，
    靠的正是这条原始信息。
    """
    class _Boom:
        def __enter__(self):
            raise OSError("mkdtemp ENOENT: 临时目录被清掉了")

        def __exit__(self, *exc):
            return False

    mod = types.ModuleType("playwright.sync_api")
    mod.sync_playwright = lambda: _Boom()      # noqa: E731 - 替身就是要短
    monkeypatch.setitem(sys.modules, "playwright.sync_api", mod)

    ok, why = browser.available()

    assert ok is False
    assert "OSError" in why, f"异常类型丢了，排查会失去线索：{why!r}"
    assert "mkdtemp" in why, f"原始消息被替换成了结论（误导的根源）：{why!r}"


def test_chromium_not_downloaded_tells_you_the_command(tmp_path, monkeypatch):
    """Chromium 没下载时，提示里要有那条安装命令。"""
    missing = tmp_path / "不存在的 chromium"

    class _P:
        class chromium:                        # noqa: N801 - 模仿 playwright 的属性名
            executable_path = str(missing)

    class _Ctx:
        def __enter__(self):
            return _P()

        def __exit__(self, *exc):
            return False

    mod = types.ModuleType("playwright.sync_api")
    mod.sync_playwright = lambda: _Ctx()       # noqa: E731
    monkeypatch.setitem(sys.modules, "playwright.sync_api", mod)

    ok, why = browser.available()

    assert ok is False
    assert "playwright install chromium" in why, f"提示不可执行：{why!r}"


def test_ready_reports_the_binary_name(tmp_path, monkeypatch):
    """就绪时说明里带上可执行文件名 —— 便于确认用的是哪一个 Chromium。"""
    exe = tmp_path / "chrome.exe"
    exe.write_text("", encoding="utf-8")

    class _P:
        class chromium:                        # noqa: N801
            executable_path = str(exe)

    class _Ctx:
        def __enter__(self):
            return _P()

        def __exit__(self, *exc):
            return False

    mod = types.ModuleType("playwright.sync_api")
    mod.sync_playwright = lambda: _Ctx()       # noqa: E731
    monkeypatch.setitem(sys.modules, "playwright.sync_api", mod)

    ok, why = browser.available()

    assert ok is True
    assert "chrome.exe" in why, f"没报出用的是哪个可执行文件：{why!r}"
