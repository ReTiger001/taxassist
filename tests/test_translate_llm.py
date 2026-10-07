"""翻译层的缓存正确性（translate_llm）—— 此前零测试。

不测网络：`translate()` 要真连 ollama，不适合单元测试。这里测的是**缓存三件套**
（指纹 / 读取 / 写入）—— 它们决定"译文有没有配错原文"。

最要紧的一条是**原文改过之后旧译文必须失效**：缓存靠 `src_hash` 判定，
这条一旦坏掉，详情页会拿旧译文配新原文，而且**完全静默** —— 页面看起来
一切正常，只有逐字比对才发现内容对不上。翻译是阅读辅助，错配的辅助比没有更糟。
"""
from __future__ import annotations

import pytest

from taxassist import db as dbmod
from taxassist import translate_llm as tl


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    """临时库连接。

    这组测试只用 connection，不需要整个 Web 应用 —— 所以 fixture 比
    test_auth_web 里那个更轻。
    """
    monkeypatch.setattr(dbmod, "DB_PATH", tmp_path / "trans.db")
    c = dbmod.connect()
    dbmod.init_db(c)
    tl.ensure_table(c)
    yield c
    c.close()


def test_hash_is_stable_and_distinguishes_inputs():
    """指纹必须稳定（否则缓存永远命中不了）且能区分不同原文。"""
    assert tl._hash("同一段文字") == tl._hash("同一段文字")
    assert tl._hash("甲") != tl._hash("乙")
    # 空值也要有确定结果，不能抛异常
    assert tl._hash("") == tl._hash(None)


def test_save_then_cached_roundtrip(conn):
    """写入后能取回同一条译文。"""
    tl.save(conn, "doc1", "title", "关于增值税的公告", "Announcement on VAT")
    assert tl.cached(conn, "doc1", "title", "关于增值税的公告") == "Announcement on VAT"


def test_cache_invalidated_when_source_changes(conn):
    """**原文变了，旧译文必须失效** —— 这是整个缓存机制的核心。

    返回 None 表示"需要重译"。若这里返回了旧译文，页面就会出现
    "新中文配旧英文"，而且不会有任何报错。
    """
    tl.save(conn, "doc1", "title", "旧标题", "Old title")
    assert tl.cached(conn, "doc1", "title", "旧标题") == "Old title"
    # 原文被重新抓取后变了（同一 doc_uid、同一字段）
    assert tl.cached(conn, "doc1", "title", "新标题") is None


def test_cached_returns_none_for_unknown_entry(conn):
    """没译过就是 None（而不是空串或异常）—— 调用方据此决定要不要译。"""
    assert tl.cached(conn, "从未见过", "title", "任意原文") is None


def test_save_is_upsert_not_duplicate(conn):
    """同一 (doc_uid, field, lang) 只保留一条 —— 靠 UNIQUE 约束 + UPSERT。

    重译很常见（改了模型、原文更新），重复行会让"取一条"变成不确定行为。
    """
    tl.save(conn, "doc1", "title", "原文", "第一版译文")
    tl.save(conn, "doc1", "title", "原文", "第二版译文")
    n = conn.execute("SELECT COUNT(*) FROM translation WHERE doc_uid='doc1'").fetchone()[0]
    assert n == 1
    assert tl.cached(conn, "doc1", "title", "原文") == "第二版译文"


def test_fields_are_cached_independently(conn):
    """标题与正文各自独立 —— 只译了标题时，正文不能被误当成已译。"""
    tl.save(conn, "doc1", "title", "标题", "Title")
    assert tl.cached(conn, "doc1", "title", "标题") == "Title"
    assert tl.cached(conn, "doc1", "content", "正文") is None


def test_progress_reports_shape(conn):
    """进度统计的字段名是外部契约（CLI 与界面都按它取值）。"""
    conn.execute(
        "INSERT INTO policy(doc_uid,title,content,first_seen_at,last_seen_at,fetch_count)"
        " VALUES('p1','有正文','正文内容','2026-01-01','2026-01-01',1)")
    conn.execute(
        "INSERT INTO policy(doc_uid,title,content,first_seen_at,last_seen_at,fetch_count)"
        " VALUES('p2','无正文',NULL,'2026-01-01','2026-01-01',1)")
    conn.commit()
    tl.save(conn, "p1", "title", "有正文", "Has content")

    p = tl.progress(conn)
    assert p["total_policies"] == 2
    assert p["policies_with_content"] == 1     # 只有 p1 有正文
    assert p["translated_titles"] == 1
    assert p["translated_contents"] == 0


def test_ensure_table_is_idempotent(conn):
    """重复建表不得报错（每次启动、每个脚本都会调它）。"""
    tl.ensure_table(conn)
    tl.ensure_table(conn)
