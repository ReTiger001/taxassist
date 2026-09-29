"""编排层测试：完整性契约（本项目的核心不变量）。"""
from __future__ import annotations

import pytest

from taxassist import db as dbmod
from taxassist import pipeline


@pytest.fixture()
def conn(tmp_path):
    c = dbmod.connect(tmp_path / "t.db")
    dbmod.init_db(c)
    yield c
    c.close()


class FakePage:
    def __init__(self, page_num: int, total: int, items: list[dict]):
        self.column = "政策法规"
        self.page_num = page_num
        self.total = total
        self.items = items
        self.raw = {"fake": True, "pageNum": page_num}


class FakeClient:
    def __init__(self, pages: list[FakePage]):
        self.pages = pages

    def iter_window(self, column, start, end, *, max_pages=None, max_pages_hard_limit=3000):
        yield from self.pages


def _item(i: int) -> dict:
    return {
        "id": f"uid-{i}",
        "title": f"关于第 {i} 项事项的公告",
        "cwrq": "2026-09-01 00:00:00",
        "column": "政策法规",
        "url": f"http://example.test/{i}.html",
    }


def test_collect_marks_ok_when_all_fetched(conn):
    client = FakeClient([FakePage(0, 2, [_item(1), _item(2)])])
    res = pipeline.collect_window(
        conn, client, column="政策法规", start="2026-09-01 00:00:00",
        end="2026-09-07 23:59:59", archive=False)
    assert res["status"] == "ok"
    assert res["fetched"] == 2 and res["reported_total"] == 2
    assert res["new"] == 2


def test_collect_marks_incomplete_when_server_reports_more(conn):
    """接口说窗口内有 25 条，只抓到 10 条 —— 必须标 incomplete 而不是 ok。

    这是本项目最重要的一条不变量：漏抓必须显式暴露，
    否则"今天没新政策"会成为一句危险的假话。
    """
    client = FakeClient([FakePage(0, 25, [_item(i) for i in range(10)])])
    res = pipeline.collect_window(
        conn, client, column="政策法规", start="2026-09-01 00:00:00",
        end="2026-09-07 23:59:59", archive=False)
    assert res["status"] == "incomplete"
    assert res["fetched"] == 10 and res["reported_total"] == 25
    assert "不完整" in pipeline.summarize([res]) or "异常" in pipeline.summarize([res])


def test_collect_records_failure_and_reraises(conn):
    class Boom(FakeClient):
        def iter_window(self, *a, **kw):
            raise RuntimeError("模拟接口故障")
            yield  # pragma: no cover

    with pytest.raises(RuntimeError):
        pipeline.collect_window(
            conn, Boom([]), column="政策法规",
            start="2026-09-01 00:00:00", end="2026-09-07 23:59:59", archive=False)

    row = conn.execute(
        "SELECT status, error FROM fetch_log ORDER BY id DESC LIMIT 1").fetchone()
    assert row["status"] == "failed"
    assert "接口故障" in row["error"]


def test_collect_skips_malformed_items_without_silent_loss(conn):
    """缺 id/title 的条目要计数，而不是静默丢弃。"""
    good = _item(1)
    bad = {"title": "缺 id 的条目"}
    client = FakeClient([FakePage(0, 2, [good, bad])])
    res = pipeline.collect_window(
        conn, client, column="政策法规",
        start="2026-09-01 00:00:00", end="2026-09-07 23:59:59", archive=False)
    assert res["malformed"] == 1
    assert res["fetched"] == 1
    # 声称 2 条、实际入库 1 条 → 必须体现为不完整
    assert res["status"] == "incomplete"


def test_summarize_reports_clean_run(conn):
    client = FakeClient([FakePage(0, 1, [_item(1)])])
    res = pipeline.collect_window(
        conn, client, column="政策法规",
        start="2026-09-01 00:00:00", end="2026-09-07 23:59:59", archive=False)
    text = pipeline.summarize([res])
    assert "全部完整" in text
    assert "新增 1" in text
