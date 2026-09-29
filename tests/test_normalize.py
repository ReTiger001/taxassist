"""字段清洗测试：这些转换错了会导致检索和引用全错。"""
from __future__ import annotations

import pytest

from taxassist.collect.normalize import (
    build_doc_no_full,
    build_policy_row,
    extract_full_doc_no,
    looks_contaminated,
    norm_date,
    norm_json_list,
    norm_text,
    repair_doc_no_prefix,
    strip_body_noise,
)


def test_norm_text_handles_fullwidth_and_empty():
    assert norm_text("  \u3000  ") is None
    assert norm_text("") is None
    assert norm_text(None) is None
    assert norm_text(" 增值税\u3000政策 ") == "增值税 政策"
    assert norm_text(["", "  ", "税收政策"]) == "税收政策"
    assert norm_text([]) is None


def test_norm_date_variants():
    # 官方实测形态
    assert norm_date("2026-09-29 00:00:00") == "2026-09-29"
    assert norm_date("2026-09-29") == "2026-09-29"
    assert norm_date("2026/9/3") == "2026-09-03"
    assert norm_date("2026年9月3日") == "2026-09-03"
    assert norm_date("") is None
    assert norm_date("无日期") is None


def test_norm_json_list():
    assert norm_json_list('["税收政策"]') == ["税收政策"]
    assert norm_json_list('["税收政策","其他"]') == ["税收政策", "其他"]
    assert norm_json_list("税收政策") == ["税收政策"]
    assert norm_json_list('[""]') == []
    assert norm_json_list("") == []


def test_doc_no_from_title_is_high_confidence():
    """标题里直接写全文号 —— 不能用 docNo 的序号冒充。"""
    no, conf = build_doc_no_full(
        "中华人民共和国工业和信息化部 国家发展改革委 财政部 国家税务总局公告2021年第10号",
        None, "10", "工业和信息化部",
    )
    assert conf == "high"
    assert "2021年第10号" in no
    assert no.startswith("中华人民共和国工业和信息化部")


def test_doc_no_bracket_style_high_confidence():
    no, conf = build_doc_no_full("关于全面推开营业税改征增值税试点的通知（财税〔2016〕36号）",
                                 None, None, None)
    assert conf == "high"
    assert no == "财税〔2016〕36号"


def test_doc_no_rebuilt_from_parts_is_not_high():
    """官方 docNo 只是序号，拼出来的文号必须降级置信度，不能标为 high。"""
    no, conf = build_doc_no_full("关于某某事项的公告", "2026", "19", "国家税务总局")
    assert no == "国家税务总局2026年第19号"
    assert conf == "medium"

    no2, conf2 = build_doc_no_full("关于某某事项的公告", "", "19", "")
    assert conf2 == "low"


def test_doc_no_absent_returns_none():
    no, conf = build_doc_no_full("税法小课堂：以人民币以外的货币结算如何确定增值税销售额？",
                                 None, None, "国家税务总局办公厅")
    assert no is None
    assert conf == "low"


def test_extract_full_doc_no_keeps_all_issuers():
    """多机关联合发文的文号必须保留全部发文机关。

    实测背景：详情页文号 "财政部 税务总局公告2026年第28号" 曾被解析成
    "税务总局公告2026年第28号"，丢掉"财政部"——文号引用不完整在实务中是硬伤。
    """
    assert extract_full_doc_no("财政部 税务总局公告2026年第28号") == "财政部 税务总局公告2026年第28号"
    assert extract_full_doc_no("国家税务总局公告2026年第19号") == "国家税务总局公告2026年第19号"
    assert extract_full_doc_no("财政部 税务总局 中国证监会公告2026年第26号") == \
        "财政部 税务总局 中国证监会公告2026年第26号"
    assert extract_full_doc_no("本文没有任何文号") is None


def test_extract_full_doc_no_bracket_style():
    assert extract_full_doc_no("依据（财税〔2016〕36号）执行") == "财税〔2016〕36号"


def test_build_policy_row_rejects_incomplete_items():
    assert build_policy_row({"title": "无 id"}) is None
    assert build_policy_row({"id": "x"}) is None


def test_build_policy_row_maps_official_fields():
    item = {
        "id": "5252705_26bbd3abe52f4b67a7ba845c8312c299_bm29000002",
        "title": "国家税务总局关于境内单位代扣代缴自然人增值税有关申报事项的公告",
        "cwrq": "2026-09-04 00:00:00",
        "pubDate": "2026-09-04 00:05:00",
        "pubName": "国家税务总局",
        "column": "政策法规",
        "url": "http://fgk.chinatax.gov.cn/zcfgk/c102416/c5252176/content.html",
        "urlMD5": "52bee310abe0fcf4fff7db80635c27e8",
        "govDoc": {"docNo": "19", "docNum": "", "docType": "", "docYear": ""},
        # 实测：xxgk_effectLevel 是"文件类型"而非效力等级
        "xxgk_effectLevel": "税务规范性文件",
        "xxgk_aging": "尚未生效",
        "xxgk_taxPolicy": '["税收政策"]',
        "keywords": ["增值税"],
    }
    row = build_policy_row(item)
    assert row is not None
    assert row["cwrq"] == "2026-09-04"
    assert row["pub_date"] == "2026-09-04"
    assert row["o_file_type"] == "税务规范性文件"      # 不是"效力等级"
    assert row["o_aging"] == "尚未生效"                # 官方时效标注
    assert row["o_doc_no_raw"] == "19"                 # 只是序号
    assert row["p_doc_no_full"] is None                # 未能重建完整文号


# ---------------------------------------------------------------- 正文污染裁剪
#
# 下面这些 raw 全部取自本地库里的真实污染记录（全库 11 条）。
# 共同点：文号前面粘着正文句子，因为提取是向左贪婪匹配的。

@pytest.mark.parametrize("raw,expected", [
    ("其企业所得税优惠政策可以按照财税", "财税"),
    ("考虑在粤人社规", "粤人社规"),
    ("要根据国税发", "国税发"),
    ("根据财企", "财企"),
    ("根据国发", "国发"),
    ("按照财税", "财税"),
    ("并按照财税", "财税"),
    ("以及享受财关税", "财关税"),
    ("展期内销售的进口展品继续按照财关税", "财关税"),
    ("注释 根据国家税务总局", "国家税务总局"),
])
def test_strip_body_noise_removes_prose_prefix(raw, expected):
    assert strip_body_noise(raw) == expected


@pytest.mark.parametrize("agency", [
    "国家税务总局",
    "财政部 税务总局",
    "中华人民共和国财政部",
    "人力资源和社会保障部",       # 含"和""为"这类常见字，绝不能被误裁
    "国家进出口商品检验局",
    "广东省人民政府",
    "国家税务总局办公厅",
    "国家税务总局 国家工商行政管理局",
])
def test_strip_body_noise_leaves_real_agencies_alone(agency):
    """黑名单刻意收得很紧：把真机关名裁坏比漏裁几条更糟。"""
    assert strip_body_noise(agency) == agency


def test_extract_full_doc_no_drops_prose_prefix():
    """正文里引用他文时抓到的文号，不能带着"按照""根据"这类句子成分。"""
    text = "……的所得税处理，其企业所得税优惠政策可以按照财税〔2005〕2号规定执行……"
    assert extract_full_doc_no(text) == "财税〔2005〕2号"


def test_extract_full_doc_no_with_prose_before_agency():
    text = "注释 根据国家税务总局公告2018年第33号的规定执行"
    assert extract_full_doc_no(text) == "国家税务总局公告2018年第33号"


def test_extract_full_doc_no_keeps_multi_agency_prefix():
    """裁剪不能伤到正常的联合发文文号。"""
    assert extract_full_doc_no("财政部 税务总局公告2026年第28号") == \
        "财政部 税务总局公告2026年第28号"


def test_extract_full_doc_no_keeps_plain_agency_prefix():
    assert extract_full_doc_no("国家税务总局公告2026年第19号") == \
        "国家税务总局公告2026年第19号"


# ---------------------------------------------------------------- 文号前缀修复

@pytest.mark.parametrize("raw,expected", [
    ("要根据国税发〔2004〕125号", "国税发〔2004〕125号"),
    ("根据财企〔2002〕266号", "财企〔2002〕266号"),
    ("考虑在粤人社规〔2018〕15号", "粤人社规〔2018〕15号"),
    ("以及享受财关税〔2021〕4号", "财关税〔2021〕4号"),
    ("注释 根据国家税务总局公告2018年第33号", "国家税务总局公告2018年第33号"),
    ("注释 依据国家税务总局公告2014年第63号", "国家税务总局公告2014年第63号"),
])
def test_repair_doc_no_prefix_only_trims_the_prefix(raw, expected):
    """关键保证：只裁前缀，**文号主体一个字符都不能变**。

    早期版本改成从正文重新提取，结果把"根据财企〔2002〕266号"换成了另一个
    文件的"财企〔2001〕820号" —— 正文里常引用多处文号，重新取第一处并不等于
    本记录那一份。错的只是前缀，主体本来就是对的。
    """
    assert repair_doc_no_prefix(raw) == expected


def test_repair_doc_no_prefix_leaves_clean_values_alone():
    assert repair_doc_no_prefix("国家税务总局公告2026年第19号") is None
    assert repair_doc_no_prefix("财政部 税务总局公告2026年第28号") is None
    assert repair_doc_no_prefix("粤府〔2017〕103号") is None


def test_looks_contaminated_flags_only_prose_prefixes():
    assert looks_contaminated("考虑在粤人社规〔2018〕15号") is True
    assert looks_contaminated("其企业所得税优惠政策可以按照财税〔2005〕2号") is True
    assert looks_contaminated("注释 根据国家税务总局公告2018年第33号") is True
    assert looks_contaminated("国家税务总局公告2026年第19号") is False
    assert looks_contaminated("财政部 税务总局公告2026年第28号") is False
    assert looks_contaminated("粤府〔2017〕103号") is False
    assert looks_contaminated("国家税务总局 国家工商行政管理局1989年第113号") is False
    assert looks_contaminated(None) is False


def test_contamination_check_and_repair_agree():
    """判定与裁切必须用同一个切分点，否则会出现"判定为脏却裁不动"的死角。"""
    for value in ("注释 根据国家税务总局公告2018年第33号",
                  "要根据国税发〔2004〕125号",
                  "考虑在粤人社规〔2018〕15号"):
        assert looks_contaminated(value) is True
        assert repair_doc_no_prefix(value) is not None
