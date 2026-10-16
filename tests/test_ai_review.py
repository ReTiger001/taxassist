"""``scripts/ai_review.py`` 的选数与分块逻辑测试。

为什么补这个文件：2026-10-16 给 ai_review 加分块审核时，`pick_pending` 的参数
个数写错了（`max_chars=None` 时仍压入一个参数 → 「2 placeholders but 3 parameters」），
而**当时这个模块一条测试都没有**，所以只能等生产工作流跑起来才炸出来
（当轮 AI 审核整个 rc=1 跳过）。这条路径专门处理超长文档，跑一次才暴露一次，
靠人工验证必然反复踩。

这些测试**不调模型**：`review_long` 的 `ask` 被 monkeypatch 掉，
`pick_pending` 只查临时 SQLite 库。所以必须全绿才允许提交。
"""
from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def air(tmp_path, monkeypatch):
    """载入 ai_review.py，并把库指到临时 SQLite 文件。"""
    monkeypatch.setenv("TAXASSIST_BACKEND", "sqlite")
    db = tmp_path / "t.db"
    monkeypatch.setattr("taxassist.config.DB_PATH", db)

    spec = importlib.util.spec_from_file_location("air_under_test", ROOT / "scripts" / "ai_review.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    conn.executescript(
        "CREATE TABLE policy (doc_uid TEXT PRIMARY KEY, title TEXT, content TEXT);"
        "CREATE TABLE translation (doc_uid TEXT, field TEXT, lang TEXT, text TEXT,"
        "  src_hash TEXT, model TEXT, created_at TEXT);"
        "CREATE TABLE translation_ai_review (doc_uid TEXT, field TEXT, verdict TEXT,"
        "  reason TEXT, model TEXT, zh_len INTEGER, reviewed_at TEXT,"
        "  PRIMARY KEY (doc_uid, field));"
    )
    conn.commit()
    return mod, conn


def _add(conn, uid, zh, en="translated"):
    conn.execute("INSERT INTO policy(doc_uid, title, content) VALUES(?,?,?)", (uid, uid, zh))
    conn.execute(
        "INSERT INTO translation(doc_uid, field, lang, text, created_at) VALUES(?,?,?,?,?)",
        (uid, "content", "en", en, "2026-10-16T00:00:00"))
    conn.commit()


# --------------------------------------------------------------- pick_pending

def test_pick_pending_with_max_chars(air):
    """带上限：只取该长度区间内的。"""
    mod, conn = air
    _add(conn, "short", "短" * 100)
    _add(conn, "long", "长" * 30000)
    rows = mod.pick_pending(conn, "content", 10, 20000)
    assert [r["doc_uid"] for r in rows] == ["short"]


def test_pick_pending_without_max_chars(air):
    """**回归用例**：max_chars=None 时参数个数必须与占位符一致。

    原 bug：这里 SQL 去掉了长度占位符，但参数仍压入一个，于是
    「the query has 2 placeholders but 3 parameters were passed」，
    整轮 AI 审核 rc=1 被跳过。
    """
    mod, conn = air
    _add(conn, "short", "短" * 100)
    _add(conn, "long", "长" * 30000)
    rows = mod.pick_pending(conn, "content", 10, None)
    assert {r["doc_uid"] for r in rows} == {"short", "long"}


def test_pick_pending_limit_is_applied(air):
    mod, conn = air
    for i in range(5):
        _add(conn, f"p{i}", "正" * 100)
    assert len(mod.pick_pending(conn, "content", 3, None)) == 3
    assert len(mod.pick_pending(conn, "content", 3, 20000)) == 3


def test_pick_pending_skips_already_reviewed(air):
    mod, conn = air
    _add(conn, "done", "正" * 100)
    conn.execute("INSERT INTO translation_ai_review(doc_uid, field, verdict, reviewed_at)"
                 " VALUES('done','content','ok','2026-10-17T00:00:00')")
    conn.commit()
    assert mod.pick_pending(conn, "content", 10, None) == []


# --------------------------------------------------------------- 分块

def test_chunk_pair_is_lossless_and_proportional(air):
    """切块必须**一个字都不丢**，且两文本按比例对齐。

    丢字会让模型把"没看到"当成"原文没写" —— 与 NUM_CTX 静默截断同一个坑。
    样本刻意用较长句子：切点优先回退到句末标点，若样本每几字一个标点，
    块长会被回退路径压得很短，测不出真实比例（第一版测试样本就栽在这）。
    """
    mod, _ = air
    unit = "这是税务政策的一段正文，用于验证按比例切块。"
    zh = unit * 400                       # 20 字 × 400 = 8000 字
    en = ("This is a sentence of tax policy text. " * 400)  # 39 字符 × 400
    parts = mod._chunk_pair(zh, en, 3000)
    assert "".join(p[0] for p in parts) == zh
    assert "".join(p[1] for p in parts) == en
    # 每块译文长度应大致与原文块等比例（容差 0.15，因为切点会回退到句末）
    for cz, ce in parts:
        assert abs(len(ce) / len(en) - len(cz) / len(zh)) < 0.15


def test_chunk_pair_edge_cases(air):
    mod, _ = air
    for z, e in (("", ""), ("短。", "Short."), ("字" * 12000, "c" * 48000)):
        parts = mod._chunk_pair(z, e, 12000)
        assert "".join(p[0] for p in parts) == z
        assert "".join(p[1] for p in parts) == e


def test_chunk_pair_never_exceeds_chunk_size(air):
    """真正的硬约束：**任何一块都不能超过 chunk 上限**（超了就会被静默截断）。"""
    mod, _ = air
    zh = "句。" * 20000   # 60000 字
    parts = mod._chunk_pair(zh, zh, 12000)
    assert "".join(p[0] for p in parts) == zh
    assert max(len(p[0]) for p in parts) <= 12000
    # 块数由切点回退决定，不锁死具体值；但不该少到离谱
    assert 4 <= len(parts) <= 6


# --------------------------------------------------------------- 结论合并

def test_review_long_all_ok(air, monkeypatch):
    mod, _ = air
    monkeypatch.setattr(mod, "ask", lambda zh, en, **k: "OK\n没有发现问题")
    v, reason = mod.review_long("字" * 30000, "c" * 120000, 12000)
    assert (v, reason) == ("ok", "")


def test_review_long_any_issue_wins(air, monkeypatch):
    """任一块有问题 → 整条有问题，且标出在第几块（不静默丢掉问题）。"""
    mod, _ = air
    calls = {"n": 0}

    def fake(zh, en, **k):
        calls["n"] += 1
        return "OK" if calls["n"] != 2 else "问题：数字不一致"

    monkeypatch.setattr(mod, "ask", fake)
    v, reason = mod.review_long("字" * 30000, "c" * 120000, 12000)
    assert v == "issue"
    assert "第 2 块" in reason


def test_review_long_unsure_propagates(air, monkeypatch):
    mod, _ = air
    monkeypatch.setattr(mod, "ask", lambda zh, en, **k: "不确定：看不出来")
    v, _ = mod.review_long("字" * 30000, "c" * 120000, 12000)
    assert v == "unsure"


def test_review_long_chunk_failure_is_unsure_not_ok(air, monkeypatch):
    """**关键**：某块失败必须报 unsure，绝不能因为其余块 OK 就报通过。"""
    mod, _ = air
    calls = {"n": 0}

    def fake(zh, en, **k):
        calls["n"] += 1
        if calls["n"] == 2:
            raise TimeoutError("boom")
        return "OK"

    monkeypatch.setattr(mod, "ask", fake)
    v, reason = mod.review_long("字" * 30000, "c" * 120000, 12000)
    assert v == "unsure"
    assert "第 2" in reason


# --------------------------------------------------------------- 修正校验与落库

def test_fix_ok_accepts_reasonable_fix(air):
    mod, _ = air
    old = "The rate is 15% for 2026, cap 100,000 yuan."
    fixed = "The rate is 15% for 2026, and the cap is 100,000 yuan."
    assert mod._fix_ok(old, fixed) is True


def test_fix_ok_rejects_too_short(air):
    """过短多半是模型只回了片段 —— 必须拒绝，否则会把半截译文写进库。"""
    mod, _ = air
    old = "x" * 1000
    assert mod._fix_ok(old, "x" * 100) is False


def test_fix_ok_rejects_lost_numbers(air):
    """数字保留率是"数字被打错/整段被截断"最直接的信号。"""
    mod, _ = air
    old = "rates 10% 20% 30% 40% 50% amounts 111 222 333 444 555"
    fixed = "rates 10% 20% 30% 40% 50% amounts 1 2 3 4 5"
    assert mod._fix_ok(old, fixed) is False


def test_fix_ok_without_numbers_only_checks_length(air):
    mod, _ = air
    old = "no digits here at all, just words"
    assert mod._fix_ok(old, "no digits here at all, just some words") is True


def test_apply_fix_updates_text_and_review_row(air):
    """落库必须同时改译文与审核记录，且**不能动 created_at**。

    created_at 若变新，条目会因为「译文比审核记录新」被无限重审 ——
    这正是修复流程最容易踩的坑。
    """
    mod, conn = air
    _add(conn, "u1", "原文", "old translation")
    before = conn.execute("SELECT created_at FROM translation WHERE doc_uid='u1'").fetchone()[0]

    mod._apply_fix(conn, "u1", "content", "brand new translation", "数字译错", 2)
    conn.commit()

    t = conn.execute(
        "SELECT text, model, created_at FROM translation WHERE doc_uid='u1'").fetchone()
    r = conn.execute(
        "SELECT verdict, reason, model, zh_len FROM translation_ai_review"
        " WHERE doc_uid='u1' AND field='content'").fetchone()
    assert t["text"] == "brand new translation"
    assert t["model"].endswith("+fix")
    assert t["created_at"] == before, "created_at 不能变，否则会被无限重审"
    assert r["verdict"] == "issue" and r["reason"].startswith("[已修复]")
    assert r["model"].endswith("+fix")
    assert r["zh_len"] == 2

