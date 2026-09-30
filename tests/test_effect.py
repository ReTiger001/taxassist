"""效力判定与引用关系测试。

最重要的一条不变量：**默认判定必须标明来源是 default（推定），不能伪装成 official。**

理由：一份被废止的文件如果被当成有效依据用出去，后果比"没有系统"严重得多。
所以每条效力状态都必须能回答"这是官方说的，还是我们推的"。
"""
from __future__ import annotations

import pytest

from taxassist import db as dbmod
from taxassist import effect, store


@pytest.fixture()
def conn(tmp_path):
    c = dbmod.connect(tmp_path / "t.db")
    dbmod.init_db(c)
    yield c
    c.close()


def _policy(uid: str, title: str, **over) -> dict:
    base = {
        "doc_uid": uid, "title": title, "cwrq": "2026-01-01",
        "url": f"http://x/{uid}.html", "pub_name": "国家税务总局",
    }
    base.update(over)
    return base


# ------------------------------------------------------------------ 抽取

def test_extract_doc_no_refs_from_body():
    text = ("按照《财政部 税务总局关于增值税征税具体范围有关事项的公告》"
            "（财政部 税务总局公告2026年第9号）执行。")
    refs = effect.extract_doc_no_refs(text)
    assert any("2026年第9号" in r for r in refs)


def test_extract_doc_no_refs_bracket_style():
    refs = effect.extract_doc_no_refs("依据财税〔2016〕36号文件的规定办理。")
    assert "财税〔2016〕36号" in refs


def test_extract_repeal_target_from_title():
    text = "本公告自2027年1月1日起施行，《国家税务总局关于旧事项处理问题的公告》同时废止。"
    targets = effect.extract_repeal_targets(text)
    assert any("旧事项" in t for t in targets)


def test_negated_repeal_is_not_extracted():
    """『不废止』绝不能读成『废止』——这是会导致错误结论的方向性错误。"""
    assert effect.extract_repeal_targets("本条不废止《国家税务总局关于某某事项的公告》。") == []
    assert effect.extract_repeal_targets("上述规定未废止《某文件》。") == []


def test_doc_no_normalisation_makes_comparison_robust():
    a = effect._normalise_doc_no("国家税务总局公告2026年第19号")
    b = effect._normalise_doc_no("中华人民共和国国家税务总局公告 2026 年第 19 号")
    assert a == b


# ------------------------------------------------------------------ 判定

def test_official_aging_takes_precedence(conn):
    store.upsert_policy(conn, _policy("u1", "某公告", o_aging="尚未生效"))
    stats = effect.judge_effects(conn)
    row = conn.execute("SELECT p_effect_status, p_effect_source FROM policy WHERE doc_uid='u1'").fetchone()
    assert row["p_effect_status"] == "尚未生效"
    assert row["p_effect_source"] == "official"
    assert stats["by_source"]["official"] == 1


def test_default_valid_is_labelled_as_default_not_official(conn):
    """没有废止证据时推定有效，但来源必须是 default 且理由写明"非官方确认"。"""
    store.upsert_policy(conn, _policy("u1", "某公告", content="正文内容，无废止表述。"))
    effect.judge_effects(conn)
    row = conn.execute(
        "SELECT p_effect_status, p_effect_source, p_effect_reason FROM policy WHERE doc_uid='u1'"
    ).fetchone()
    assert row["p_effect_status"] == "现行有效"
    assert row["p_effect_source"] == "default"          # 不是 official
    assert "推定" in row["p_effect_reason"]


def test_repeal_marks_target_as_repealed(conn):
    """A 文废止 B 文 → B 的效力应变为已废止，且来源为 inferred。"""
    store.upsert_policy(conn, _policy(
        "old", "国家税务总局关于旧事项处理问题的公告", p_doc_no_full="国家税务总局公告2020年第5号"))
    store.upsert_policy(conn, _policy(
        "new", "国家税务总局关于新事项的公告",
        p_doc_no_full="国家税务总局公告2026年第19号",
        content="现将有关事项公告如下。《国家税务总局关于旧事项处理问题的公告》同时废止。"))

    stats = effect.judge_effects(conn)
    assert stats["repeal_relations"] >= 1

    row = conn.execute("SELECT p_effect_status, p_effect_source FROM policy WHERE doc_uid='old'").fetchone()
    assert row["p_effect_status"] == "已废止"
    assert row["p_effect_source"] == "inferred"

    # 反向查询："这条政策被谁废止了" —— 实务里最常问的问题
    back = effect.repealed_by(conn, "old")
    assert len(back) == 1
    assert back[0]["src_doc_uid"] == "new"


def test_self_repeal_detected_with_explicit_subject():
    """正文载明本文被废止 —— 官方就是把失效依据写在正文里的。"""
    ok, why = effect.detect_self_repealed(
        "根据《中华人民共和国企业所得税暂行条例》（国务院令第137号），"
        "自1994年1月1日起本法规全文废止。")
    assert ok is True
    assert "废止" in why


def test_self_repeal_detected_without_subject():
    """主语省略的写法（标题目即主语），实测比带主语的还多。"""
    ok, _ = effect.detect_self_repealed(
        "根据《国务院关于废止2000年底以前发布的部分行政法规的决定》"
        "（国务院令第319号），全文废止")
    assert ok is True


def test_repealing_another_file_is_not_self_repeal():
    """"《某办法》全文废止"指的是别人 —— 误判成自述会把现行文件标成已废止。"""
    ok, _ = effect.detect_self_repealed("《财政部关于某事项的管理办法》全文废止")
    assert ok is False


def test_negation_is_respected_for_self_repeal():
    ok, _ = effect.detect_self_repealed("本公告不废止现行有效的规定。")
    assert ok is False


def _policy(uid, title, **kw):
    row = {"doc_uid": uid, "title": title, "url": f"http://x/{uid}",
           "first_seen_at": "2026-01-01", "last_seen_at": "2026-01-01"}
    row.update(kw)
    return row


def test_judge_marks_self_repealed_as_repealed(conn):
    """走完整 judge 流程也要能判出来（不能只在单元测试里成立）。"""
    store.upsert_policy(conn, _policy(
        "old", "中华人民共和国国营企业所得税条例（草案）实施细则",
        content="依据《财政部关于公布废止和失效的财政规章目录（第六批）的通知》"
                "（财法字[1997]44号），本实施细则全文废止。"))
    stats = effect.judge_effects(conn)
    assert stats["by_source"].get("inferred", 0) >= 1
    row = conn.execute("SELECT p_effect_status FROM policy WHERE doc_uid='old'").fetchone()
    assert row["p_effect_status"] == "已废止", row["p_effect_status"]


def test_citation_relation_recorded(conn):
    store.upsert_policy(conn, _policy("a", "甲公告", content="依据《乙公告》（国家税务总局公告2026年第9号）执行。"))
    store.upsert_policy(conn, _policy("b", "乙公告", p_doc_no_full="国家税务总局公告2026年第9号"))
    effect.judge_effects(conn)
    cites = effect.citations_of(conn, "a")
    assert cites, "应至少产生一条引用关系"
    assert any(c["dst_doc_no"] and "2026年第9号" in c["dst_doc_no"] for c in cites)


def test_title_reference_matched_by_filename(conn):
    """正文用《文件名》引用、括号里只写"（2026年第28号）"时也要能匹配上。

    实测背景：某公告正文写作
    「《财政部 税务总局关于发布〈…管理办法〉的公告》（2026年第28号）」
    —— 括号里没有机关名和体裁词，纯文号正则抓不到，必须靠文件名匹配。
    """
    store.upsert_policy(conn, _policy(
        "main", "国家税务总局关于申报事项的公告",
        content="根据《财政部 税务总局关于发布〈境内单位代扣代缴自然人增值税管理办法〉的公告》"
                "（2026年第28号）有关规定，现予公布。"))
    store.upsert_policy(conn, _policy(
        "target", "财政部 税务总局关于发布《境内单位代扣代缴自然人增值税管理办法》的公告",
        p_doc_no_full="财政部 税务总局公告2026年第28号"))
    effect.judge_effects(conn)
    cites = effect.citations_of(conn, "main")
    matched = [c for c in cites if c["dst_doc_uid"] == "target"]
    assert matched, f"应按文件名匹配到 target，实际关系：{cites}"


def test_attachment_text_is_scanned_for_citations(conn):
    """关键引用可能只出现在附件里 —— 附件解析文本必须纳入扫描范围。"""
    store.upsert_policy(conn, _policy("a", "甲公告", content=None))
    store.upsert_policy(conn, _policy(
        "b", "财政部 税务总局关于增值税征税具体范围有关事项的公告",
        p_doc_no_full="财政部 税务总局公告2026年第9号"))
    conn.execute(
        "INSERT INTO attachment(doc_uid, url, filename, ext, parsed_text, parse_status, created_at)"
        " VALUES('a','http://x/a.pdf','办法.pdf','pdf',?,'ok','2026-01-01')",
        ("第一条 符合规定的应税交易的具体范围，按照《财政部 税务总局关于增值税征税具体范围"
         "有关事项的公告》（财政部 税务总局公告2026年第9号）执行。",),
    )
    conn.commit()
    effect.judge_effects(conn)
    cites = effect.citations_of(conn, "a")
    assert cites, "附件里的引用没有被扫描到"


def test_self_citation_ignored(conn):
    """文件引用自己的文号不应产生自引用关系。"""
    store.upsert_policy(conn, _policy(
        "self", "国家税务总局关于某事项的公告",
        p_doc_no_full="国家税务总局公告2026年第19号",
        content="本公告（国家税务总局公告2026年第19号）自发布之日起施行。"))
    effect.judge_effects(conn)
    assert effect.citations_of(conn, "self") == []


def test_judge_is_idempotent(conn):
    """重复判定不应重复插入关系记录。"""
    store.upsert_policy(conn, _policy("a", "甲公告", content="依据《乙公告》（国家税务总局公告2026年第9号）执行。"))
    store.upsert_policy(conn, _policy("b", "乙公告", p_doc_no_full="国家税务总局公告2026年第9号"))
    effect.judge_effects(conn)
    first = conn.execute("SELECT COUNT(*) FROM policy_relation").fetchone()[0]
    effect.judge_effects(conn)
    second = conn.execute("SELECT COUNT(*) FROM policy_relation").fetchone()[0]
    assert first == second
