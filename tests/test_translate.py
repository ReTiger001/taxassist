"""中英双语检索的守卫。

核心行为三条，都不能退化：
1. 英文术语映射成中文后**真的能命中政策**（只测映射对不对没意义）
2. 中文输入**不得被改动**（反向映射只该动英文）
3. 无对应术语的外文词**不得被猜**成某个中文词 —— 猜错比不猜危险得多
"""
import pytest

from taxassist.translate import to_chinese_query


@pytest.mark.parametrize("en,zh", [
    ("value-added tax", "增值税"),
    ("enterprise income tax", "企业所得税"),
    ("super-deduction", "加计扣除"),
    ("stamp duty", "印花税"),
    ("small-scale taxpayer", "小规模纳税人"),
    ("tax treaty", "税收协定"),
    ("annual final settlement", "汇算清缴"),
    ("export tax rebate", "出口退税"),
])
def test_english_term_maps_to_chinese(en, zh):
    cn, hits = to_chinese_query(en)
    assert cn == zh
    assert zh in hits


@pytest.mark.parametrize("variant", ["VALUE-ADDED TAX", "Value-Added Tax", "value-added Tax"])
def test_mapping_is_case_insensitive(variant):
    cn, hits = to_chinese_query(variant)
    assert cn == "增值税"
    assert hits == ["增值税"]


@pytest.mark.parametrize("zh", ["增值税", "出口退税", "企业所得税", "加计扣除"])
def test_chinese_input_is_never_rewritten(zh):
    """中文原样返回，命中列表为空 —— 反向映射只该动英文。"""
    assert to_chinese_query(zh) == (zh, [])


def test_unknown_foreign_terms_are_not_guessed():
    """没有对应术语的外文词必须原样返回。

    猜一个近似词，用户会以为搜的是 A、实际搜的是 B，而且毫无提示 ——
    这种静默错误比搜不到更危险。
    """
    cn, hits = to_chinese_query("quantum flux")
    assert cn == "quantum flux"
    assert hits == []


def test_mixed_query_preserves_the_unmapped_part():
    cn, hits = to_chinese_query("value-added tax 优惠")
    assert "增值税" in cn
    assert "优惠" in cn
    assert hits == ["增值税"]


def test_multiple_terms_in_one_query():
    cn, hits = to_chinese_query("value-added tax and stamp duty")
    assert "增值税" in cn and "印花税" in cn
    assert set(hits) == {"增值税", "印花税"}


def test_empty_and_blank_queries():
    assert to_chinese_query("") == ("", [])
    assert to_chinese_query("   ")[1] == []
