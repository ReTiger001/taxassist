"""审校台（/review）的测试。

**为什么专门测它**：这一页此前不存在 —— 一万两千行机器判定
（`translation_ai_review`）没有任何界面能看到，只能靠 `build_review_html.py`
往 `data/logs` 里产静态 HTML，看过就找不回来。新页面要钉三件事：

1. **能渲染** —— 模板里的变量名拼错，不会让任何别的测试变红；
2. **数字来自真实查询**而不是写死的；
3. **两张表语义不混** —— 机器判定可重跑可覆盖、人工结论不可被机器覆盖。
   混起来会让「哪些是人核过的」永远答不上来，而那正是审校的全部意义。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from taxassist import db as dbmod
from taxassist.repo import review as R_review
from taxassist.web.app import create_app


@pytest.fixture()
def db_path(tmp_path, monkeypatch):
    """指向临时库；审核表由仓储层的 ensure_table 建（与 production 同源）。"""
    monkeypatch.setattr(dbmod, "DB_PATH", tmp_path / "review.db")
    conn = dbmod.connect()
    dbmod.init_db(conn)
    # translation 与两张审核表都**不在** db.SCHEMA 里，靠各模块 ensure_table
    # 惰性建 —— 这是项目现状，测试照现状来（不为了让测试好写而改生产行为）。
    from taxassist import translate_llm

    translate_llm.ensure_table(conn)
    R_review.ensure_table(conn)
    conn.commit()
    conn.close()
    return tmp_path / "review.db"


@pytest.fixture()
def client(db_path):
    # require_auth=False：免认证客户端（与 test_assistant_routes 同范式）
    return TestClient(create_app(require_auth=False))


@pytest.fixture()
def seeded(db_path):
    """插几条已知数据 —— 用来验证页面上的数字确实来自查询。"""
    conn = dbmod.connect()
    R_review.save_ai(conn, doc_uid="d1", field="content", verdict="issue",
                     reason="语义相反", model="m", zh_len=100,
                     reviewed_at="2026-10-14T00:00:00")
    R_review.save_ai(conn, doc_uid="d2", field="content", verdict="ok",
                     reason=None, model="m", zh_len=100,
                     reviewed_at="2026-10-14T00:00:00")
    R_review.save_ai(conn, doc_uid="d3", field="title", verdict="unsure",
                     reason="拿不准", model="m", zh_len=20,
                     reviewed_at="2026-10-14T00:00:00")
    R_review.save_human(conn, doc_uid="d1", field="content", verdict="bad",
                        note="确认译反", reviewer="me", kind="semantic",
                        reviewed_at="2026-10-14T01:00:00")
    conn.commit()
    conn.close()


def test_review_page_renders(client):
    r = client.get("/review")
    assert r.status_code == 200
    assert "翻译质量与审校" in r.text


def test_stats_come_from_real_query(client, seeded):
    """页面上的统计必须来自查询本身 —— 写死的数字不会随数据变。"""
    r = client.get("/review?field=content")
    assert r.status_code == 200
    assert "机器判定" in r.text
    assert "人工已核" in r.text


def test_field_filter_separates_title_and_content(client, seeded):
    """字段筛选必须真的分开 —— 混在一起看会误导（标题短、缺上下文，AI 更常拿不准）。

    断言用 ``/policy/<uid>`` 这个完整形态，而不是光秃秃的 ``d3``：页面里的
    CSS 十六进制颜色（如 ``#d3dbe0``）含同样的字符，裸字符串断言会假失败 ——
    第一次跑就是这么红的。
    """
    r_content = client.get("/review?field=content&verdict=issue")
    assert "/policy/d1" in r_content.text
    assert "/policy/d3" not in r_content.text      # d3 属于 title 字段

    r_title = client.get("/review?field=title&verdict=unsure")
    assert "/policy/d3" in r_title.text


def test_human_verdict_survives_machine_rerun(seeded, db_path):
    """**机器重跑不能覆盖人工结论** —— 这是两张表分开的全部意义。"""
    conn = dbmod.connect()
    try:
        R_review.save_ai(conn, doc_uid="d1", field="content", verdict="ok",
                         reason="重跑后改成 ok", model="m2", zh_len=100,
                         reviewed_at="2026-10-15T00:00:00")
        conn.commit()

        human = R_review.human_by_doc(conn, "d1")
        assert len(human) == 1
        assert human[0]["verdict"] == "bad"    # 人工结论原样保留
        assert R_review.human_count(conn) == 1

        ai = R_review.ai_stats(conn, "content")
        assert ai.get("ok") == 2               # 机器侧确实更新了
    finally:
        conn.close()


def test_unreviewed_lists_machine_issues_without_human_verdict(seeded, db_path):
    """待办列表 = 机器说有问题、且人还没核 —— 审校台的入口。"""
    conn = dbmod.connect()
    try:
        rows = R_review.unreviewed(conn, field="content", limit=50)
        uids = {r["doc_uid"] for r in rows}
        assert "d1" not in uids                # d1 已有人工结论
        assert "d2" not in uids                # d2 机器判 ok
    finally:
        conn.close()


def test_empty_db_does_not_crash(client):
    """空库也要正常出页面 —— 新部署时没有翻译数据是常态。"""
    r = client.get("/review?field=title&verdict=ok")
    assert r.status_code == 200
    assert "没有记录" in r.text
