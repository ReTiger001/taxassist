"""文号来源的守卫：官方字段优先，不再拼装。

守的是一次真实事故：我们曾按「**发文机关** + 年份 + 序号」拼文号，
拼出「国务院1984年第161号」—— 中文文号里没有这种形式。
而同一行的官方字段 ``o_doc_num`` 里就是真文号「国发〔1984〕161号」。

更糟的是曾被标为 ``high``（自称"从原文提取"）的那批：抽样发现抓的是
**页面上引用的别的文件**的文号 —— 标题是 1987 年的文件，
文号却抓成「国家税务总局公告2011年第2号」，年份都对不上。
"""
import pytest

from taxassist import backfill
from taxassist import db as dbmod


@pytest.fixture()
def conn(tmp_path):
    c = dbmod.connect(tmp_path / "test.db")
    dbmod.init_db(c)
    yield c
    c.close()


def _insert(conn, uid, title, ours, conf, official, cwrq="2026-01-01"):
    conn.execute(
        "INSERT INTO policy (doc_uid, title, p_doc_no_full, p_doc_no_confidence,"
        " o_doc_num, first_seen_at, last_seen_at, cwrq)"
        " VALUES (?,?,?,?,?,'2026-01-01','2026-01-01',?)",
        (uid, title, ours, conf, official, cwrq))
    conn.commit()


def _get(conn, uid):
    return conn.execute(
        "SELECT p_doc_no_full, p_doc_no_confidence FROM policy WHERE doc_uid=?",
        (uid,)).fetchone()


@pytest.mark.parametrize("value", [
    "国发〔1984〕161号",
    "（85）财税油政字第26号",
    "[84]财政65号",
    "国家税务总局公告2026年第19号",
    "财政部 税务总局公告2026年第28号",
])
def test_recognises_real_doc_no(value):
    assert backfill.looks_like_doc_no(value) is True


@pytest.mark.parametrize("value", [None, "", "161", "1984", "国务院", "abc", "   ", "号"])
def test_rejects_anything_that_is_not_a_doc_no(value):
    """不像文号的一律拒绝 —— 宁可留空，也不拿脏值覆盖。"""
    assert backfill.looks_like_doc_no(value) is False


def test_our_guess_is_replaced_by_the_official_value(conn):
    _insert(conn, "u1", "某暂行规定", "国务院1984年第161号", "medium", "国发〔1984〕161号")
    stats = backfill.apply_official_doc_no(conn)
    assert stats["replaced"] == 1
    row = _get(conn, "u1")
    assert row["p_doc_no_full"] == "国发〔1984〕161号"
    assert row["p_doc_no_confidence"] == "official"


def test_mis_extracted_high_value_is_also_replaced(conn):
    """标为 high 的也要换 —— 实测那批抓的是别的文件的文号。"""
    _insert(conn, "u2", "财政部税务总局关于房产税…的解释与规定",
            "国家税务总局公告2011年第2号", "high", "财税地字〔1987〕3号", "1987-01-01")
    backfill.apply_official_doc_no(conn)
    row = _get(conn, "u2")
    assert row["p_doc_no_full"] == "财税地字〔1987〕3号"
    assert "2011" not in row["p_doc_no_full"]


def test_no_official_value_clears_our_guess(conn):
    """官方没给文号时**清空**，不保留拼装值 —— 拼出来的那个看着像真的。"""
    _insert(conn, "u3", "税法小课堂：…", "国家税务总局2026年第5号", "medium", None)
    stats = backfill.apply_official_doc_no(conn)
    assert stats["cleared"] == 1
    row = _get(conn, "u3")
    assert row["p_doc_no_full"] is None
    assert row["p_doc_no_confidence"] == "none"


def test_already_empty_stays_empty(conn):
    _insert(conn, "u6", "某解读", None, "low", None)
    stats = backfill.apply_official_doc_no(conn)
    assert stats["cleared"] == 0
    assert _get(conn, "u6")["p_doc_no_full"] is None


def test_dry_run_writes_nothing(conn):
    _insert(conn, "u4", "测试", "旧的", "medium", "国发〔1984〕161号")
    stats = backfill.apply_official_doc_no(conn, dry_run=True)
    assert stats["replaced"] == 1 and stats["dry_run"] is True
    assert _get(conn, "u4")["p_doc_no_full"] == "旧的"


def test_running_twice_is_idempotent(conn):
    _insert(conn, "u5", "测试", "国务院1984年第161号", "medium", "国发〔1984〕161号")
    backfill.apply_official_doc_no(conn)
    second = backfill.apply_official_doc_no(conn)
    assert second["replaced"] == 0
    assert second["unchanged"] == 1
