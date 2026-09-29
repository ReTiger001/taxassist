"""存储层测试：upsert 状态机与归档。"""
from __future__ import annotations

import pytest

from taxassist import db as dbmod
from taxassist import store


@pytest.fixture()
def conn(tmp_path):
    c = dbmod.connect(tmp_path / "test.db")
    dbmod.init_db(c)
    yield c
    c.close()


def _row(**over):
    base = {
        "doc_uid": "uid-1",
        "url": "http://example.test/a.html",
        "title": "关于测试事项的公告",
        "cwrq": "2026-01-01",
        "pub_date": "2026-01-01",
        "pub_name": "国家税务总局",
        "content": "正文内容",
    }
    base.update(over)
    return base


def test_upsert_new_then_unchanged_then_updated(conn):
    assert store.upsert_policy(conn, _row()) == "new"

    # 同一内容再抓一次 → unchanged，但抓取计数增加
    assert store.upsert_policy(conn, _row()) == "unchanged"
    row = conn.execute("SELECT fetch_count, last_changed_at FROM policy WHERE doc_uid='uid-1'").fetchone()
    assert row["fetch_count"] == 2
    assert row["last_changed_at"] is None

    # 正文变化 → updated，并记录变更时间
    assert store.upsert_policy(conn, _row(content="正文内容（修订）")) == "updated"
    row = conn.execute(
        "SELECT content, last_changed_at, fetch_count FROM policy WHERE doc_uid='uid-1'").fetchone()
    assert row["content"] == "正文内容（修订）"
    assert row["last_changed_at"] is not None
    assert row["fetch_count"] == 2  # 更新路径不增加 fetch_count


def test_content_hash_ignores_volatile_fields():
    """last_seen_at / fetch_count 变化不应触发'内容更新'，否则每天都是 updated。"""
    a = _row()
    b = _row()
    b["last_seen_at"] = "2099-01-01T00:00:00"
    b["fetch_count"] = 99
    assert store.content_hash(a) == store.content_hash(b)


def test_fts_index_synced_by_triggers(conn):
    store.upsert_policy(conn, _row(title="研发费用税前加计扣除政策指引", content="集成电路和工业母机企业"))
    hits = conn.execute(
        "SELECT COUNT(*) FROM policy_fts WHERE policy_fts MATCH ?", ('"加计扣除"',)
    ).fetchone()[0]
    assert hits == 1


def test_updating_non_indexed_field_keeps_fts_intact(conn):
    """效力判定只改 p_effect_status 等非索引字段，不应破坏全文索引。

    为什么值得单独测：为了让 5000+ 条的判定不慢到不可用，
    ``policy_au`` 触发器加了 WHEN 条件（只在索引字段变化时才重建索引）。
    条件一旦写错，UPDATE 后索引会丢数据 —— 而且搜不到东西时很难联想到触发器。
    """
    store.upsert_policy(conn, _row(title="研发费用税前加计扣除政策指引"))
    assert conn.execute(
        "SELECT COUNT(*) FROM policy_fts WHERE policy_fts MATCH ?", ('"加计扣除"',)
    ).fetchone()[0] == 1

    conn.execute("UPDATE policy SET p_effect_status='已废止', p_effect_source='official'"
                 " WHERE doc_uid='uid-1'")
    conn.commit()

    assert conn.execute(
        "SELECT COUNT(*) FROM policy_fts WHERE policy_fts MATCH ?", ('"加计扣除"',)
    ).fetchone()[0] == 1, "改效力状态后索引被破坏"


def test_updating_title_does_refresh_fts(conn):
    """改标题必须刷新索引，否则会搜不到新标题（WHEN 条件的另一半）。"""
    store.upsert_policy(conn, _row(title="某个不会再出现的旧标题"))
    conn.execute("UPDATE policy SET title='增值税留抵退税新政策公告' WHERE doc_uid='uid-1'")
    conn.commit()

    assert conn.execute(
        "SELECT COUNT(*) FROM policy_fts WHERE policy_fts MATCH ?", ('"留抵退税"',)
    ).fetchone()[0] == 1, "改标题后索引未刷新"


def test_fetch_log_records_incompleteness(conn):
    log_id = store.log_fetch_start(conn, "fgk_zcfg", "incremental", "2026-09-01", "2026-09-07")
    status = store.log_fetch_finish(
        conn, log_id, reported_total=25, fetched_count=10, new_count=10)
    # 声称 25 条只抓到 10 条 —— 必须是 incomplete，绝不能是 ok
    assert status == "incomplete"
    row = conn.execute("SELECT * FROM fetch_log WHERE id=?", (log_id,)).fetchone()
    assert row["status"] == "incomplete"
    assert row["reported_total"] == 25
    assert row["fetched_count"] == 10


def test_fetch_log_ok_when_counts_match(conn):
    log_id = store.log_fetch_start(conn, "fgk_zcfg", "incremental")
    assert store.log_fetch_finish(conn, log_id, reported_total=3, fetched_count=3) == "ok"


def test_archive_page_writes_gzip_and_index(tmp_path, conn, monkeypatch):
    monkeypatch.setattr(store, "RAW_DIR", tmp_path)
    monkeypatch.setattr(store, "ensure_dirs", lambda: None)
    rel = store.archive_page(
        conn, source_id="fgk_zcfg", http_url="http://x.test/api",
        column="政策法规", page_num=0,
        window_start="2026-09-01 00:00:00", window_end="2026-09-07 23:59:59",
        payload={"searchResultAll": {"total": 1}},
    )
    path = tmp_path / rel
    assert path.exists() and path.suffix == ".gz"
    import gzip, json
    with gzip.open(path, "rb") as fh:
        assert json.loads(fh.read())["searchResultAll"]["total"] == 1
    n = conn.execute("SELECT COUNT(*) FROM raw_snapshot WHERE kind='api_json_page'").fetchone()[0]
    assert n == 1
