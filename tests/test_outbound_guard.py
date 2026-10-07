"""出网守卫（GuardedClient / OutboundGuard）—— 三条不可违反边界之一的技术护栏。

**为什么单独建这个文件**：项目的第一条边界是"客户数据绝不出本机"，而抓取
必然要访问政府网站，两者之间只隔着这一层检查。全量审计（2026-10）发现它
**此前零测试** —— 也就是说这条边界靠"没人改错"维持。一旦失效，客户名会
随检索参数或 POST 正文发到外部站点，事后无法追回，也无人会察觉。

这里不测网络行为（那会真的发请求），只测守卫本身的判断：拦得住、
拦得全（URL / 参数 / 正文三个位置）、且不误拦。
"""
from __future__ import annotations

import pytest

from taxassist.collect import http as httpmod


def test_denylist_loads_local_client_names(tmp_path, monkeypatch):
    """本地禁词表（客户名）必须被读进守卫，注释与空行不入表。"""
    f = tmp_path / "deny.txt"
    f.write_text("# 这是注释\n客户甲\n\n  客户乙  \n", encoding="utf-8")
    monkeypatch.setattr(httpmod, "FORBIDDEN_EXTRA_FILE", f)

    words = httpmod.load_denylist()
    assert "客户甲" in words
    assert "客户乙" in words          # 前后空白会被 strip
    assert "# 这是注释" not in words   # 注释行不算禁词


def _guarded(words: list[str]) -> httpmod.GuardedClient:
    """造一个只带 denylist 的守卫实例。

    绕过 __init__ 是有意的：构造函数会建真正的 httpx 客户端与限速状态，
    而这里要测的只是判断逻辑 —— 让测试不依赖网络栈。
    """
    c = httpmod.GuardedClient.__new__(httpmod.GuardedClient)
    c.denylist = words
    return c


@pytest.mark.parametrize("where", ["url", "params", "payload"])
def test_outbound_guard_blocks_client_name_everywhere(where):
    """客户名出现在**任一处**都必须拦住。

    只查 URL 是典型的假安全：客户名更可能出现在检索参数（q=…）或 POST 正文
    （要送到模型或站点校验的合同摘录）里，那些位置恰恰是真正会泄露的地方。
    """
    c = _guarded(["客户甲"])
    url, params, payload = "https://example.invalid/search", None, None
    if where == "url":
        url = "https://example.invalid/search?q=客户甲"
    elif where == "params":
        params = {"q": "客户甲"}
    else:
        payload = '{"text": "客户甲 的合同条款"}'

    with pytest.raises(httpmod.OutboundGuardError) as ei:
        c._check_outbound(url, params, payload)
    # 报错要能让人定位：带上命中的词
    assert "客户甲" in str(ei.value)


def test_outbound_guard_allows_clean_requests():
    """干净请求不得被误拦 —— 拦得太严会让抓取整体失效，同样是故障。"""
    c = _guarded(["客户甲"])
    c._check_outbound("https://example.invalid/search", {"q": "研发费用加计扣除"}, None)
    c._check_outbound("https://example.invalid/detail", None, '{"q": "增值税"}')


def test_outbound_guard_reads_the_venv_flag():
    """守卫必须真的用 denylist 判断，而不是无条件放行。

    这条防的是"检查函数被改成空实现"这类悄悄退化 —— 那种改动不会让任何
    功能测试失败，只会让边界消失。
    """
    c = _guarded(["绝不会出现的词"])
    c._check_outbound("https://example.invalid/search", {"q": "随便什么"}, None)

    c2 = _guarded(["随便什么"])
    with pytest.raises(httpmod.OutboundGuardError):
        c2._check_outbound("https://example.invalid/search", {"q": "随便什么"}, None)
