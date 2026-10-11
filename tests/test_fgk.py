"""总局法规库检索客户端（fgk.py）测试。

**为什么单独建这个文件**：全量审计（2026-10）发现这条路径**零测试覆盖** ——
而它是库里政策的主要来源（总局 8 个栏目）。更麻烦的是它出错的形态：

    · 参数写错 → 每页都返回第 1 页 → 去重后只剩几十条，**不报错**
    · 翻页提前停 → fetched 远小于 total，**仍然记成成功**
    · 接口改版 → 结构取不到 → 要么静默空结果，要么抛得看不懂

这三种都属于"看起来成功了，实际少抓了" —— 正是最该被测试钉住的形态。
（同类 bug 项目里真发生过：province.py 里记着"7789 条只拿到 65 条"。）

这里不碰网络：用一个替身客户端按脚本返回响应，并记录每次实际发出的参数。
"""
from __future__ import annotations

from datetime import date

import pytest

from taxassist.collect import fgk

# ---------------------------------------------------------------------------
# 替身
# ---------------------------------------------------------------------------

class _FakeClient:
    """按脚本逐次返回响应，并记下每次调用收到的参数。

    ``get_json`` 在脚本用尽时**主动抛错**而不是返回空 —— 这样"多抓了一页"
    会立刻变成失败，而不是被静默容忍。
    """

    def __init__(self, *responses: dict) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    def get_json(self, url: str, params: dict | None = None,
                 headers: dict | None = None) -> dict:
        self.calls.append({"url": url, "params": dict(params or {}),
                           "headers": dict(headers or {})})
        if not self._responses:
            raise AssertionError(
                f"替身被第 {len(self.calls)} 次调用，但脚本已用尽 —— "
                "说明翻页没有在预期的地方停下")
        return self._responses.pop(0)

    def close(self) -> None:  # pragma: no cover - 仅为接口完整
        pass


def _page(n_items: int, total: int, start_id: int = 0) -> dict:
    """造一个接口形状的响应。"""
    return {"searchResultAll": {
        "total": total,
        "searchTotal": [{"docId": start_id + i} for i in range(n_items)],
    }}


# ---------------------------------------------------------------------------
# 纯函数：_dig / 窗口
# ---------------------------------------------------------------------------

def test_dig_walks_nested_dict_and_returns_none_on_miss():
    """_dig 取嵌套值；缺键、中间不是 dict 都返回 None（不抛）。"""
    d = {"a": {"b": {"c": 42}}, "lst": [1, 2]}
    assert fgk._dig(d, ("a", "b", "c")) == 42
    assert fgk._dig(d, ("a", "missing")) is None
    assert fgk._dig(d, ("a", "b", "c", "d")) is None       # 叶子不是 dict
    assert fgk._dig(d, ("lst", "0")) is None               # 中间是 list 而非 dict
    assert fgk._dig({}, ("a",)) is None


def test_incremental_window_looks_back_seven_days_inclusive():
    """增量窗口：往前 7 天，两端都取到当天最后一刻。

    **为什么要钉住"含首尾"**：窗口少一天就是少一天的政策，而且不会报错 ——
    它以"抓取成功"的形态出现在 fetch_log 里。重叠抓取靠 doc_uid 去重兜底，
    但窗口本身跨错天数，去重救不回来。
    """
    start, end = fgk.incremental_window(7, today=date(2026, 10, 11))
    assert start == "2026-10-04 00:00:00", f"起日应当是 7 天前：{start}"
    assert end == "2026-10-11 23:59:59", f"止日应当是当天末刻：{end}"


def test_year_windows_covers_every_year_inclusive():
    """按年切窗：首尾年份都含，且每年 12-31 取到末刻。"""
    ws = fgk.year_windows(2024, 2026)
    assert ws == [
        ("2024-01-01 00:00:00", "2024-12-31 23:59:59"),
        ("2025-01-01 00:00:00", "2025-12-31 23:59:59"),
        ("2026-01-01 00:00:00", "2026-12-31 23:59:59"),
    ]
    # 不传 year_to 时切到今年 —— 全量导入的兜底行为
    assert fgk.year_windows(2026)[-1][1].startswith("2026-12-31")


# ---------------------------------------------------------------------------
# search_page：参数与结构
# ---------------------------------------------------------------------------

def test_search_page_sends_pagenum_and_window():
    """**页码必须真的进参数** —— 这条是本文件最要紧的断言。

    项目里踩过同型的坑（见 province.py 的注释）：页码字段名写死成别的名字，
    于是每页都请求第 1 页，去重后只剩几十条，而状态还是 ok。
    """
    fake = _FakeClient(_page(2, 5, start_id=100))
    c = fgk.FgkClient(client=fake)
    sp = c.search_page("政策法规", "2026-01-01 00:00:00", "2026-01-31 23:59:59", 3)

    sent = fake.calls[0]["params"]
    assert sent["pageNum"] == "3"
    assert sent["column"] == "政策法规"
    assert sent["startTime"] == "2026-01-01 00:00:00"
    assert sent["endTime"] == "2026-01-31 23:59:59"
    assert sent["pageSize"] == str(fgk.FGK_PAGE_SIZE)
    assert sent["siteCode"] == fgk.FGK_SITE_CODE

    assert sp.page_num == 3
    assert sp.total == 5
    assert len(sp.items) == 2


def test_search_page_raises_actionable_error_on_shape_change():
    """接口改版时必须抛**能照着做**的错，而不是 KeyError/空结果。

    报错文本要带上"去重跑 probe_source"和实际拿到的顶层键 —— 半夜出问题的人
    需要的是下一步动作，不是一句 "KeyError: searchResultAll"。
    """
    fake = _FakeClient({"code": 200, "data": {}})
    c = fgk.FgkClient(client=fake)
    with pytest.raises(fgk.FgkResponseError) as ei:
        c.search_page("政策法规", "2026-01-01 00:00:00", "2026-01-31 23:59:59", 0)
    msg = str(ei.value)
    assert "probe_source" in msg, "报错要给出下一步动作"
    assert "code" in msg or "data" in msg, "报错要带上实际顶层键，便于判断改版形态"


# ---------------------------------------------------------------------------
# iter_window：翻页的停止条件
# ---------------------------------------------------------------------------

def test_iter_window_stops_when_total_reached():
    """抓满 total 就停，且不会多请求一页。"""
    fake = _FakeClient(_page(3, 4, start_id=0), _page(1, 4, start_id=3))
    c = fgk.FgkClient(client=fake)
    pages = list(c.iter_window("政策法规", "s", "e"))
    assert len(pages) == 2
    assert sum(len(p.items) for p in pages) == 4
    assert len(fake.calls) == 2, "抓满后不该再请求下一页"


def test_iter_window_stops_on_empty_page_even_if_total_unmet():
    """空页但 total 没抓满 → **停下**（而不是死循环），并留下警告。

    这是"悄悄少抓"的典型入口：服务端声称 100 条，翻到第 3 页返回空。
    必须停下来把缺口暴露出来，不能无限翻下去。
    """
    fake = _FakeClient(_page(2, 100, start_id=0), _page(0, 100))
    c = fgk.FgkClient(client=fake)
    pages = list(c.iter_window("政策法规", "s", "e"))
    assert len(pages) == 1, "空页不该被 yield"
    assert sum(len(p.items) for p in pages) == 2
    assert len(fake.calls) == 2, "空页之后必须停，不能再翻"


def test_iter_window_respects_max_pages():
    """max_pages 生效：调用方用它给单个窗口限量。"""
    fake = _FakeClient(*[_page(2, 999, start_id=i * 2) for i in range(3)])
    c = fgk.FgkClient(client=fake)
    pages = list(c.iter_window("政策法规", "s", "e", max_pages=2))
    assert len(pages) == 2
    assert len(fake.calls) == 2, "达到 max_pages 后不该多请求"


def test_iter_window_respects_hard_limit():
    """硬上限生效 —— 它是"参数配错导致翻不完"的最后一道闸。"""
    fake = _FakeClient(*[_page(1, 10 ** 9, start_id=i) for i in range(5)])
    c = fgk.FgkClient(client=fake)
    pages = list(c.iter_window("政策法规", "s", "e", max_pages_hard_limit=3))
    assert len(pages) == 3
    assert len(fake.calls) == 3
