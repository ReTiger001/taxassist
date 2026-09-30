"""summarize 必须同时吃「总局窗口」与「省级源」两种结果形状。

背景：它原先对结果 dict 直接下标取值（r["new"]、r["column"]、r["window"]），
而这些键省级结果没有 —— 于是**任何一个省级源失败，整个 provincial 命令
都会崩在汇总这一步**（KeyError: 'new'），连"哪个源失败了"都打不出来，
看起来像命令行本身坏了。这三个用例把这个前提钉住。
"""
from taxassist.pipeline import summarize


def test_province_failure_result_does_not_crash():
    """省级失败结果（无 column / window / new / updated）不应让汇总崩溃。"""
    rows = [{
        "source_id": "shaanxi_zcwj", "region": "陕西", "status": "failed",
        "fetched": 0, "error": "ListPageError: 列表页未解析出任何条目",
    }]
    out = summarize(rows)
    assert "异常 1 个" in out
    assert "shaanxi_zcwj" in out, "要能说清是哪个源失败了"


def test_province_ok_result():
    rows = [{
        "source_id": "jl_zcwj", "region": "吉林", "status": "ok",
        "fetched": 65, "new": 21, "updated": 44, "skipped_duplicates": 0,
    }]
    out = summarize(rows)
    assert "抓取 65 条" in out
    assert "新增 21" in out
    assert "全部完整" in out


def test_national_window_shape_still_works():
    """总局形状（column + window）不受这次健壮化影响。"""
    rows = [
        {"column": "总局文件", "window": ("2026-09-01", "2026-09-30"),
         "status": "ok", "fetched": 100, "new": 5, "updated": 3,
         "reported_total": 100},
        {"column": "总局文件", "window": ("2026-08-01", "2026-08-31"),
         "status": "partial", "fetched": 40, "new": 0, "updated": 0,
         "reported_total": 120},
    ]
    out = summarize(rows)
    assert "异常 1 个" in out
    assert "总局文件" in out
    assert "2026-08-01~2026-08-31" in out


def test_mixed_shapes_in_one_batch():
    """一次调用里同时有总局与省级结果（多源采集时的真实形状）。"""
    rows = [
        {"column": "总局文件", "window": ("2026-09-01", "2026-09-30"),
         "status": "ok", "fetched": 30, "new": 2, "updated": 1,
         "reported_total": 30},
        {"source_id": "gd_zcwj", "region": "广东", "status": "failed",
         "fetched": 0, "error": "boom"},
    ]
    out = summarize(rows)
    assert "抓取 30 条" in out
    assert "异常 1 个" in out
    assert "gd_zcwj" in out
