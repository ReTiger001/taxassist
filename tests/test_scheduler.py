"""后台调度与告警测试。

核心不变量：**抓取不完整必须能被查询到、并显示在界面上。**
"静默漏抓"是本项目最危险的失败模式 —— 它看起来和"今天没有新政策"一模一样。
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from taxassist import db as dbmod
from taxassist import scheduler, store


@pytest.fixture()
def conn(tmp_path):
    c = dbmod.connect(tmp_path / "t.db")
    dbmod.init_db(c)
    yield c
    c.close()


def test_health_is_clean_on_empty_db(conn):
    h = scheduler.fetch_health(conn)
    assert h["last_ok_at"] is None
    assert h["bad_last_7d"] == 0


def test_incomplete_fetch_surfaces_in_health(conn):
    log_id = store.log_fetch_start(conn, "fgk_zcfg", "incremental",
                                   "2026-09-01", "2026-09-07")
    store.log_fetch_finish(conn, log_id, reported_total=25, fetched_count=10)

    h = scheduler.fetch_health(conn)
    assert h["bad_last_7d"] >= 1
    assert h["recent_bad"][0]["status"] == "incomplete"
    assert h["recent_bad"][0]["reported_total"] == 25


def test_failed_fetch_surfaces_in_health(conn):
    log_id = store.log_fetch_start(conn, "fgk_zcfg", "incremental")
    store.log_fetch_finish(conn, log_id, reported_total=None, fetched_count=0,
                           error="模拟网络故障")
    h = scheduler.fetch_health(conn)
    assert any(r["status"] == "failed" for r in h["recent_bad"])


def test_last_success_ignores_incomplete_runs(conn):
    """落后天数只能依据**完整成功**的抓取计算，不能被 incomplete 骗过。

    否则一次"抓到 5/50 条"的不完整抓取会被当成已完成，补抓永不触发。
    """
    good = store.log_fetch_start(conn, "fgk_zcfg", "incremental",
                                 "2026-09-01", "2026-09-10")
    store.log_fetch_finish(conn, good, reported_total=3, fetched_count=3)
    bad = store.log_fetch_start(conn, "fgk_zcfg", "incremental",
                                "2026-09-11", "2026-09-20")
    store.log_fetch_finish(conn, bad, reported_total=50, fetched_count=5)

    assert scheduler.last_success_window_end(conn) == date(2026, 9, 10)


def test_days_since_last_success_is_none_when_never_fetched(conn):
    assert scheduler.days_since_last_success(conn) is None


def test_catch_up_does_nothing_when_current(conn, monkeypatch):
    today = date.today().isoformat()
    log_id = store.log_fetch_start(conn, "s", "incremental", today, today)
    store.log_fetch_finish(conn, log_id, reported_total=0, fetched_count=0)
    monkeypatch.setattr(
        scheduler.pipeline, "collect_incremental",
        lambda *a, **k: pytest.fail("数据已最新，不应触发补抓"))
    assert scheduler.catch_up(conn) is None


def test_catch_up_triggers_and_overlaps_when_stale(conn, monkeypatch):
    old_day = (date.today() - timedelta(days=10)).isoformat()
    log_id = store.log_fetch_start(conn, "s", "incremental", old_day, old_day)
    store.log_fetch_finish(conn, log_id, reported_total=0, fetched_count=0)

    seen: dict = {}

    def fake_collect(conn, days=7, **kw):
        seen["days"] = days
        return []

    monkeypatch.setattr(scheduler.pipeline, "collect_incremental", fake_collect)
    result = scheduler.catch_up(conn)

    assert result is not None
    assert result["gap_days"] == 10
    # 窗口要重叠 7 天，防止边界遗漏
    assert seen["days"] == 17


def test_run_daily_records_status_in_meta(conn, monkeypatch):
    monkeypatch.setattr(scheduler.pipeline, "collect_incremental",
                        lambda *a, **k: [{"status": "ok", "fetched": 2, "new": 2,
                                          "column": "政策法规", "window": ["a", "b"]}])
    monkeypatch.setattr(scheduler.pipeline, "enrich_details",
                        lambda *a, **k: {"requested": 0, "ok": 0, "failed": 0,
                                         "updated": 0, "attachments": 0, "errors": []})
    monkeypatch.setattr(scheduler.pipeline, "fetch_attachments",
                        lambda *a, **k: {"requested": 0, "ok": 0, "failed": 0,
                                         "no_text_layer": 0, "unsupported": 0,
                                         "bytes": 0, "errors": []})
    monkeypatch.setattr(scheduler.effect, "judge_effects",
                        lambda *a, **k: {"judged": 0})

    result = scheduler.run_daily(conn, include_provincial=False)
    assert result["ok"] is True
    assert dbmod.get_meta(conn, scheduler.META_LAST_STATUS) == "ok"


def test_run_daily_marks_incomplete_when_collect_partial(conn, monkeypatch):
    monkeypatch.setattr(scheduler.pipeline, "collect_incremental",
                        lambda *a, **k: [{"status": "incomplete", "fetched": 5,
                                          "new": 5, "column": "政策法规",
                                          "window": ["a", "b"]}])
    monkeypatch.setattr(scheduler.pipeline, "enrich_details",
                        lambda *a, **k: {"requested": 0, "ok": 0, "failed": 0,
                                         "updated": 0, "attachments": 0, "errors": []})
    monkeypatch.setattr(scheduler.pipeline, "fetch_attachments",
                        lambda *a, **k: {"requested": 0, "ok": 0, "failed": 0,
                                         "no_text_layer": 0, "unsupported": 0,
                                         "bytes": 0, "errors": []})
    monkeypatch.setattr(scheduler.effect, "judge_effects", lambda *a, **k: {"judged": 0})

    result = scheduler.run_daily(conn, include_provincial=False)
    assert result["ok"] is False
    assert dbmod.get_meta(conn, scheduler.META_LAST_STATUS) == "incomplete"
