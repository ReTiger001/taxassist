"""仓储层测试：与现有 SQL **逐字节对比**，两个后端都覆盖。

分工：

* **SQLite 侧必跑** —— 那是生产现状，仓储层首先必须与它完全等价。
* **PG 侧只在设了 ``TAXASSIST_PG_TEST_DSN`` 时跑** —— 别的机器上没有 PG，
  缺 PG 不该让测试变红（skip 而不是 fail）。
"""
from __future__ import annotations

import os

import pytest

from taxassist import db
from taxassist.repo import attachment as R_att
from taxassist.repo import conn as rc
from taxassist.repo import fetch as R_fetch
from taxassist.repo import meta as R_meta
from taxassist.repo import policy as R_pol
from taxassist.repo import relation as R_rel
from taxassist.repo import translation as R_tr

PG_TEST_DSN = os.environ.get("TAXASSIST_PG_TEST_DSN")


@pytest.fixture()
def fresh(tmp_path):
    """全新空库：用现有的 init_db 建表，保证 schema 与生产一致。

    `translation` 等几张表**不在** `db.SCHEMA` 里，靠各模块 `ensure_table()`
    惰性建（盘点确认的现状）。测试照现状来 —— 不为了让测试好写而改生产行为。
    """
    conn = db.connect(tmp_path / "t.db")
    db.init_db(conn)
    from taxassist import translate_llm

    translate_llm.ensure_table(conn)
    yield conn
    conn.close()


# ------------------------------------------------------------------ 连接层


def test_ping_sqlite():
    assert rc.ping().startswith("sqlite:")


def test_backend_defaults_to_sqlite():
    """不设环境变量时必须是 sqlite —— 生产还在 SQLite 上跑，默认值不能偷偷变。"""
    assert rc.BACKEND == "sqlite"


def test_readonly_refuses_missing_file(tmp_path):
    """路径写错时必须报错，不能凭空建一个空库。

    「检索突然一条都查不到」的经典成因就是只读连接误建了空库。
    """
    with pytest.raises(Exception):
        rc.connect_readonly(tmp_path / "definitely-missing.db")


def test_sql_translates_placeholder_only_on_pg(monkeypatch):
    """占位符翻译只对 pg 生效；sqlite 侧原样返回。"""
    assert rc.sql("SELECT 1 FROM t WHERE a=?") == "SELECT 1 FROM t WHERE a=?"


# ------------------------------------------------------------------ 与现有 SQL 等价


def test_policy_count_matches_legacy():
    legacy = db.connect()
    rconn = rc.connect()
    try:
        expected = legacy.execute("SELECT COUNT(*) FROM policy").fetchone()[0]
        assert R_pol.count(rconn) == expected
    finally:
        legacy.close()
        rconn.close()


def test_policy_row_identical_to_legacy():
    """整行逐列相等 —— 仓储层返回的行形状不能与现有代码有任何差别。"""
    legacy = db.connect()
    rconn = rc.connect()
    try:
        uid = legacy.execute(
            "SELECT doc_uid FROM policy ORDER BY id LIMIT 1").fetchone()["doc_uid"]
        want = dict(legacy.execute("SELECT * FROM policy WHERE doc_uid=?", (uid,)).fetchone())
        got = dict(R_pol.get(rconn, uid))
        assert got == want
    finally:
        legacy.close()
        rconn.close()


def test_policy_page_matches_legacy():
    legacy = db.connect()
    rconn = rc.connect()
    try:
        want = [dict(r) for r in legacy.execute(
            "SELECT * FROM policy ORDER BY cwrq DESC LIMIT 5 OFFSET 10").fetchall()]
        got = [dict(r) for r in R_pol.page(rconn, limit=5, offset=10)]
        assert got == want
    finally:
        legacy.close()
        rconn.close()


# ------------------------------------------------------------------ 写路径（空库）


def test_meta_roundtrip_and_overwrite(fresh):
    assert R_meta.get(fresh, "nope") is None
    assert R_meta.get(fresh, "nope", "fallback") == "fallback"
    R_meta.put(fresh, "k", "v1")
    assert R_meta.get(fresh, "k") == "v1"
    R_meta.put(fresh, "k", "v2")          # ON CONFLICT DO UPDATE
    assert R_meta.get(fresh, "k") == "v2"
    assert R_meta.all(fresh)["k"] == "v2"
    assert R_meta.delete(fresh, "k") == 1


def test_policy_insert_update_touch_delete(fresh):
    row = {"doc_uid": "u1", "title": "标题", "content": "正文",
           "content_hash": "h1", "first_seen_at": "2026-01-01T00:00:00+08:00",
           "last_seen_at": "2026-01-01T00:00:00+08:00"}
    R_pol.insert(fresh, row)
    assert R_pol.exists(fresh, "u1")
    assert R_pol.get(fresh, "u1")["title"] == "标题"

    # update 不动 doc_uid / first_seen_at（那是主键与"第一次见到"的语义）
    R_pol.update(fresh, "u1", {"title": "新标题", "doc_uid": "HACK",
                               "first_seen_at": "HACK"})
    got = R_pol.get(fresh, "u1")
    assert got["title"] == "新标题"
    assert got["doc_uid"] == "u1"
    assert got["first_seen_at"] == "2026-01-01T00:00:00+08:00"

    before = R_pol.get(fresh, "u1")["fetch_count"] or 0
    R_pol.touch(fresh, "u1", "h2")
    after = R_pol.get(fresh, "u1")
    assert (after["fetch_count"] or 0) == before + 1
    assert after["content_hash"] == "h2"

    assert R_pol.delete(fresh, ["u1"]) == 1
    assert not R_pol.exists(fresh, "u1")


def test_policy_rejects_bad_column_name(fresh):
    """动态列名必须有校验 —— 53 列必须动态拼，拼接前不校验就是注入面。"""
    with pytest.raises(ValueError):
        R_pol.update(fresh, "u1", {"name=1; DROP TABLE policy; --": "x"})


def test_translation_upsert_overwrites(fresh):
    kw = dict(doc_uid="d1", field="title", lang="en", src_hash="s1",
              text="Policy One", model="m", created_at="2026-01-01T00:00:00")
    R_tr.save(fresh, **kw)
    assert R_tr.get(fresh, "d1", "title")["text"] == "Policy One"

    R_tr.save(fresh, **{**kw, "text": "Policy One (rev)"})
    assert R_tr.get(fresh, "d1", "title")["text"] == "Policy One (rev)"
    assert R_tr.count(fresh, field="title") == 1

    # update_text 只改文本，不动 src_hash（源文没变，变的是译法）
    R_tr.update_text(fresh, "d1", "title", "Policy One (fixed)")
    row = R_tr.get(fresh, "d1", "title")
    assert row["text"] == "Policy One (fixed)"
    assert row["src_hash"] == "s1"


def test_attachment_upsert_keeps_existing(fresh):
    """重复登记不能把已解析好的正文抹掉（原实现是 INSERT OR IGNORE）。"""
    kw = dict(doc_uid="d1", url="http://x/a.pdf", filename="a.pdf", ext="pdf",
              raw_path="/p", parsed_text="解析好的正文", parse_status="ok",
              parse_error=None, created_at="2026-01-01T00:00:00")
    R_att.upsert(fresh, **kw)
    R_att.upsert(fresh, **{**kw, "parsed_text": None, "parse_status": None})
    row = R_att.by_doc(fresh, "d1")[0]
    assert row["parsed_text"] == "解析好的正文"
    assert row["parse_status"] == "ok"


def test_fetch_log_start_finish(fresh):
    log_id = R_fetch.log_start(fresh, source_id="bj", mode="window",
                               started_at="2026-01-01T00:00:00",
                               window_start="2026-01-01", window_end="2026-01-02")
    assert log_id > 0
    R_fetch.log_finish(fresh, log_id, status="ok", finished_at="2026-01-01T00:05:00",
                       values={"reported_total": 10, "fetched_count": 10})
    row = R_fetch.recent(fresh, "bj", limit=1)[0]
    assert row["status"] == "ok"
    assert row["reported_total"] == 10
    assert R_fetch.incomplete(fresh) == []

    # 漏抓必须能被查出来（宁可报错，也不让"漏抓"看起来像"没政策"）
    log2 = R_fetch.log_start(fresh, source_id="bj", mode="window",
                             started_at="2026-01-02T00:00:00",
                             window_start=None, window_end=None)
    R_fetch.log_finish(fresh, log2, status="incomplete", finished_at="2026-01-02T00:05:00",
                       values={"reported_total": 10, "fetched_count": 7})
    assert len(R_fetch.incomplete(fresh)) == 1


def test_snapshot_dedup(fresh):
    kw = dict(doc_uid="d1", source_id="bj", fetched_at="2026-01-01T00:00:00",
              http_url="http://x", kind="list", rel_path="p.json",
              content_hash="h", size_bytes=10)
    R_fetch.record_snapshot(fresh, **kw)
    R_fetch.record_snapshot(fresh, **kw)      # 同内容重复落盘应被跳过
    assert R_fetch.count(fresh, "d1") == 1


def test_relation_exists_and_delete_auto(fresh):
    R_rel.add(fresh, src_doc_uid="a", dst_doc_uid=None, dst_doc_no="X〔2026〕1号",
              relation="cites", evidence="e", evidence_source="auto",
              confidence=0.5, created_at="2026-01-01T00:00:00")
    assert R_rel.exists(fresh, src_doc_uid="a", dst_doc_uid=None,
                        dst_doc_no="X〔2026〕1号", relation="cites")
    R_rel.add(fresh, src_doc_uid="a", dst_doc_uid="b", dst_doc_no=None,
              relation="repeals", evidence="e", evidence_source="manual",
              confidence=1.0, created_at="2026-01-01T00:00:00")
    assert R_rel.count(fresh) == 2

    # 重算前清掉自动推断的关系，人工标注必须留下
    assert R_rel.delete_auto(fresh, "a") == 1
    left = R_rel.by_src(fresh, "a")
    assert len(left) == 1 and left[0]["evidence_source"] == "manual"


# ------------------------------------------------------------------ PG 侧（可选）


def test_search_requires_pg_backend():
    """SQLite 后端下检索必须明确报错，不能悄悄返回空 —— 那会看起来像"没有结果"。"""
    pytest.importorskip("psycopg", reason="PG 侧才需要 psycopg")
    from taxassist.repo import search as R_search

    if rc.BACKEND == "sqlite":
        conn = rc.connect()
        try:
            with pytest.raises(R_search.NotSupportedOnSQLite):
                R_search.search(conn, "增值税")
        finally:
            conn.close()
