"""时间窗测试：翻译只在指定时段跑。

用打桩的 datetime.now 测边界，否则测试结果会随真实时间变化。
"""
import importlib.util
import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def ta():
    spec = importlib.util.spec_from_file_location(
        "translate_all_mod", ROOT / "scripts" / "translate_all.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _at(ta, hour: int) -> None:
    """把模块里的 datetime 换成固定时刻。"""
    class _Frozen:
        @staticmethod
        def now():
            return datetime(2026, 10, 4, hour, 30)
    ta.datetime = _Frozen


def test_window_boundaries(ta):
    """11-19 的含义：11 点开始、19 点结束（含 11，不含 19）。"""
    for hour, want in ((0, False), (10, False), (11, True), (14, True),
                       (18, True), (19, False), (23, False)):
        _at(ta, hour)
        assert ta._in_window("11-19") is want, f"{hour} 点应为 {want}"


def test_cross_midnight_window(ta):
    """跨天写法 23-7 也要能用（万一作息改了）。"""
    for hour, want in ((22, False), (23, True), (3, True), (6, True),
                       (7, False), (12, False)):
        _at(ta, hour)
        assert ta._in_window("23-7") is want, f"{hour} 点应为 {want}"


def test_bad_spec_does_not_block(ta):
    """规格写错时不加限制 —— 宁可跑，也不该把翻译永久卡住。"""
    _at(ta, 3)
    assert ta._in_window("乱写") is True
    assert ta._in_window("") is True


def test_module_actually_has_datetime():
    """冒烟：模块真的能 import 到 datetime。

    为什么要这一条：上面几个测试都用打桩把 ``ta.datetime`` 换掉了 ——
    那**恰好会掩盖"模块里根本没有 datetime 这个名字"**。实测就是这么漏掉
    一个 NameError 的（翻译进程一启动就崩）。所以补一条不打扰动的检查。
    """
    import subprocess
    out = subprocess.run(
        [sys.executable, "-c",
         "import importlib.util,sys;"
         f"s=importlib.util.spec_from_file_location('t', r'{ROOT / 'scripts' / 'translate_all.py'}');"
         "m=importlib.util.module_from_spec(s);s.loader.exec_module(m);"
         "print(m._in_window('11-19'))"],
        capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr[-400:]
    assert out.stdout.strip() in ("True", "False")
