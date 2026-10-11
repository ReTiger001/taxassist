"""相关性过滤测试。

这套规则决定"什么内容会出现在你第一眼看到的位置"，
判错的代价不对称：把解读误判成实质政策（多看一条）远比
把公告误判成科普（漏掉一份文件）轻。
因此规则一律**偏向保守**：有正式文号即视为实质政策。
"""
from __future__ import annotations

import pytest

from taxassist import filters


def test_doc_no_wins_over_column():
    """有正式文号就是实质政策，即使挂在"政策解读"栏目下。"""
    assert filters.classify({
        "o_column": "政策解读", "p_doc_no_full": "财税〔2016〕36号",
        "title": "关于全面推开营业税改征增值税试点的通知",
    }) == filters.SUBSTANTIVE


def test_science_content_is_news():
    for title in ("税法小课堂：以人民币以外的货币结算如何确定增值税销售额？",
                  "一图了解：跨境电商出口，增值税怎么缴？",
                  "漫画丨合规开具发票，需要把好三道业务关！",
                  "办税小知识：如何在电子税务局查询主管税务机关？"):
        assert filters.classify({
            "o_column": "政策解读", "p_doc_no_full": None, "title": title,
        }) == filters.NEWS, title


def test_policy_column_without_docno_still_substantive():
    """栏目是政策法规、标题也不像科普 —— 即使没识别出文号也算实质政策。"""
    assert filters.classify({
        "o_column": "政策法规", "p_doc_no_full": None,
        "title": "国家税务总局关于境内单位代扣代缴自然人增值税有关申报事项的公告",
    }) == filters.SUBSTANTIVE


def test_missing_fields_do_not_crash():
    """sqlite3.Row 缺字段或空 dict 都不应抛异常。"""
    assert filters.classify({}) == filters.OTHER
    assert filters.classify(
        {"title": None, "o_column": None, "p_doc_no_full": None}) == filters.OTHER


def test_is_substantive_helper():
    assert filters.is_substantive({"p_doc_no_full": "国家税务总局公告2026年第19号"})
    assert not filters.is_substantive({"title": "税法小课堂：增值税"})


def test_substantive_first_sql_prefers_docno_then_column():
    sql = filters.substantive_first_sql("p")
    assert "p.p_doc_no_full" in sql
    assert "政策法规" in sql
    assert sql.count("CASE") == 1


@pytest.mark.parametrize("column,title,expected", [
    ("政策法规", "关于某某事项的公告", filters.SUBSTANTIVE),
    ("财税文件", "关于某某事项的公告", filters.SUBSTANTIVE),
    ("政策解读", "关于某某事项的公告", filters.INTERPRETATION),
    # 无栏目、无文号、标题也不像文件体裁 —— 才归入"其他"
    ("", "某篇没有体裁词的随笔", filters.OTHER),
])
def test_column_fallback_classification(column, title, expected):
    row = {"o_column": column, "p_doc_no_full": None, "title": title}
    assert filters.classify(row) == expected


def test_local_policy_without_docno_is_substantive():
    """实测广东 28 条省级文件**全部没有正式文号**。

    只按文号判断，会把整批真正的地方政策文件归入"其他"并排到列表末尾 ——
    这正是最需要防的那类错误。故用体裁词结尾兜底识别。
    """
    assert filters.classify({
        "o_column": "地方政策", "p_doc_no_full": None,
        "title": "广东省人民政府关于延续实施车辆车船税具体适用税额的通知",
    }) == filters.SUBSTANTIVE
    assert filters.classify({
        "o_column": "", "p_doc_no_full": None,
        "title": "广东省人民代表大会常务委员会关于广东省耕地占用税适用税额的决定",
    }) == filters.SUBSTANTIVE


def test_science_still_wins_over_genre_word():
    """科普标题里出现"公告"二字也不能被当成实质政策。"""
    assert filters.classify({
        "o_column": "政策解读", "p_doc_no_full": None,
        "title": "一图了解：《国家税务总局关于某某事项的公告》主要内容",
    }) == filters.NEWS


# ---------------------------------------------------------------- 税种

def test_tax_type_extraction_basic():
    assert filters.extract_tax_types(
        {"title": "关于增值税进项税额抵扣的公告"}) == ["增值税"]
    assert "企业所得税" in filters.extract_tax_types(
        {"title": "关于企业所得税若干问题的公告", "o_keywords": "汇算"})


def test_land_vat_is_not_double_counted_as_vat():
    """**"土地增值税"包含"增值税"** —— 不遮蔽就会串味，
    按税种筛选时每份土增文件都会被当成增值税文件。"""
    taxes = filters.extract_tax_types({"title": "关于土地增值税清算有关问题的通知"})
    assert "土地增值税" in taxes
    assert "增值税" not in taxes


def test_land_vat_and_vat_together_when_both_present():
    taxes = filters.extract_tax_types(
        {"title": "关于土地增值税清算中增值税处理的通知"})
    assert "土地增值税" in taxes and "增值税" in taxes


def test_tax_extraction_handles_missing_fields():
    assert filters.extract_tax_types({}) == []


def test_tax_filter_sql_covers_title_and_classification_fields():
    clause, params = filters.tax_filter_sql("增值税", "p")
    assert "p.title LIKE ?" in clause
    assert len(params) == 3


def test_tax_filter_sql_unknown_tax_returns_empty():
    clause, params = filters.tax_filter_sql("不存在的税种", "p")
    assert clause == "" and params == []


# ---------------------------------------------------------------- 栏目不能整体定性

def test_local_policy_column_is_not_blanket_substantive():
    """实测教训：把"地方政策"栏目整体当成实质政策，会让广东的解读、
    答记者问、科普内容挤满首页首屏（25 条里 11 条误判），
    看起来"政策全是广东的"。该栏目是省级列表页整页抓取的产物，内容混杂，
    必须逐条靠标题特征判断。"""
    cases = [
        ("关于《国家税务总局关于电池消费税征收管理有关事项的公告》的解读",
         filters.INTERPRETATION),
        ("《广东省工程建设项目参加工伤保险办法》政策解读",
         filters.INTERPRETATION),
        ("财政部 税务总局 生态环境部有关司负责人就开展征收挥发性有机物环境保护税试点答记者问",
         filters.NEWS),
        ("财政部税政司 税务总局所得税司有关负责人就离岸信托个人所得税有关事项答记者问",
         filters.NEWS),
        ("政策性搬迁政策要点解答", filters.NEWS),
        ("开学第一课 终端消费环节这3个不合规的坑千万别踩！", filters.NEWS),
    ]
    for title, expected in cases:
        got = filters.classify(
            {"o_column": "地方政策", "p_doc_no_full": None, "title": title})
        assert got == expected, f"{title[:30]} → {got}，应为 {expected}"


def test_local_policy_real_documents_still_substantive():
    """反向保护：真正的地方政策文件（省级多为无文号）仍须识别为实质政策，
    不能因为上一处修正就把它们一起埋掉。"""
    for title in (
        "广东省人民政府关于延续实施车辆车船税具体适用税额的通知",
        "广东省人民代表大会常务委员会关于广东省耕地占用税适用税额的决定",
        "广东省财政厅关于某某事项的实施细则",
        "某省人民政府关于某某事项的公告",
    ):
        got = filters.classify(
            {"o_column": "地方政策", "p_doc_no_full": None, "title": title})
        assert got == filters.SUBSTANTIVE, f"{title[:30]} → {got}"


def test_substantive_first_sql_has_three_tiers():
    """排序必须与 classify 同构：文号 → 权威栏目 → 文件体裁。"""
    sql = filters.substantive_first_sql("p")
    assert "THEN 0" in sql and "THEN 1" in sql and "THEN 2" in sql
    assert "title LIKE '%的通知'" in sql
    assert "地方政策" not in sql.split("THEN 1")[0]  # 地方政策不在权威栏目档


def test_news_title_re_does_not_swallow_real_policies():
    """科普形态词不能吞掉真政策 —— 2026-10-11 修的两条**过宽**规则。

    **怎么发现的**：用户问"还有什么遗漏的"，我按三套判据去扫政策层里的非政策，
    结果扫出来的 90 条里混着十几份「国家税务总局关于…无偿援助…（第二期）免征
    增值税的通知」。逐个查命中原因才看清：

      · ``第.{1,3}期`` 把**项目批次号**当成了期刊号 —— 命中 18 条里 16 条带正式
        文号；转发《…调整公布第八期…清单的通知》同理。
      · ``案例`` 命中 12 条里 8 条带正式文号（《税务行政执法案例指导工作实施
        办法》、《研发费用加计扣除项目鉴定案例》）。

    代价不只是分类错：这条规则用于**默认排序降权**，于是那批总局文件一直被压在
    后面。判据定为「**有正式文号就不算科普**」—— 文号是规范性文件最强的信号。
    """
    from taxassist.filters import _NEWS_TITLE_RE

    # 不该被判成科普的（有正式文号 / 公文体裁）
    for t in (
        "国家税务总局关于联合国人口基金无偿援助生殖健康计划生育项目在华采购物资（第二期）免征增值税的通知",
        "国家税务总局转发《财政部 国家发展改革委关于调整公布第十三期节能产品政府采购清单的通知》等2个文件的通知",
        "国家税务总局关于印发《税务行政执法案例指导工作实施办法》的通知",
        "研发费用加计扣除项目鉴定案例（第三辑）",
    ):
        assert not _NEWS_TITLE_RE.search(t), f"真政策被误判成科普（会被排序降权）：{t}"

    # 该命中的（真科普 / 问答 / 图解形态）
    for t in (
        "视频来了！税务部门曝光5起骗取出口退税及税务人员违纪违法案件",
        "税法小课堂：资源税的税率是如何规定的？",
        "图解｜《扣缴办法》发布，自然人缴纳增值税更便捷了！",
        "Ai主播话税收第一期：平时正常扣个税，还需要办理年度汇算吗？",
        "个人所得税综合所得汇算清缴提示案例",
    ):
        assert _NEWS_TITLE_RE.search(t), f"真科普漏判了：{t}"
