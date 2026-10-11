"""省级静态列表页解析测试。

最重要的一条：**零条目必须抛错**。
如果返回空列表当成功，界面会显示"今天没有新政策"，
而真相是页面改版了、或者那其实是个 JS 异步页 —— 这是最危险的失败模式。
"""
from __future__ import annotations

import pytest

from taxassist import province

ADAPTER = province.ListPageAdapter(
    source_id="test_src",
    region="测试省",
    site_name="测试省税务局",
    list_url="http://example.test/list.shtml",
    detail_href_re=r"/gdsw/[a-z]+/\d{4}-\d{2}/\d{2}/content_[0-9a-f]+\.shtml",
    base_url="http://example.test",
)

LIST_HTML = """
<html><body>
<div class="nav"><a href="/index.shtml">首页</a><a href="/about.shtml">机构</a></div>
<div class="list">
  <a href="/gdsw/ssfggds/2026-09/22/content_a11c79b2a8814e64958a7fcd83f710fd.shtml">广东省人民政府关于延续实施车辆车船税具体适用税额的通知</a>2026-09-22
  <a href="/gdsw/ssfggds/2026-09/22/content_0a76e06c8f7e4b7b99ae80222e39856d.shtml">广东省人民政府关于调整车辆车船税具体适用税额的通知</a>2026-09-22
</div>
</body></html>
"""


def test_parse_extracts_title_date_url():
    items = province.parse_list_page(LIST_HTML, ADAPTER)
    assert len(items) == 2
    assert items[0]["cwrq"] == "2026-09-22"
    assert "车船税" in items[0]["title"]
    assert items[0]["url"] == (
        "http://example.test/gdsw/ssfggds/2026-09/22/"
        "content_a11c79b2a8814e64958a7fcd83f710fd.shtml")
    assert items[0]["doc_uid"].startswith("test_src:")


def test_zero_items_raises_instead_of_silent_empty():
    with pytest.raises(province.ListPageError) as exc:
        province.parse_list_page("<html><body><p>页面改版了</p></body></html>", ADAPTER)
    assert "未解析出任何条目" in str(exc.value)


def test_navigation_links_excluded():
    items = province.parse_list_page(LIST_HTML, ADAPTER)
    assert all("index.shtml" not in i["url"] for i in items)


def test_doc_uid_is_stable_for_same_url():
    """同一 URL 每次解析得到同一 doc_uid，否则每天都会当成新政策入库。"""
    a = province.parse_list_page(LIST_HTML, ADAPTER)
    b = province.parse_list_page(LIST_HTML, ADAPTER)
    assert [x["doc_uid"] for x in a] == [x["doc_uid"] for x in b]


def test_build_row_extracts_docno_from_title():
    item = {
        "doc_uid": "test_src:abc", "url": "http://example.test/a.shtml",
        "title": "广东省财政厅关于某某事项的通知（粤财〔2026〕10号）",
        "cwrq": "2026-01-01",
    }
    row = province.build_provincial_row(item, ADAPTER)
    assert row["p_doc_no_full"] == "粤财〔2026〕10号"
    assert row["p_doc_no_confidence"] == "high"
    assert row["o_column"] == "地方政策"
    assert row["cwrq"] == "2026-01-01"


def test_build_row_handles_missing_docno():
    item = {"doc_uid": "test_src:abc", "url": "http://x/a.shtml",
            "title": "关于某某事项的通知", "cwrq": "2026-01-01"}
    row = province.build_provincial_row(item, ADAPTER)
    assert row["p_doc_no_full"] is None
    assert row["p_doc_no_confidence"] == "low"


LIST_HTML_WITH_ICON = """
<html><body><div class="list">
  <a href="/gdsw/zjfg/2026-09/07/content_c101490a72ea479f872d639466d3ef3b.shtml"
     title="国家税务总局关于境内单位代扣代缴自然人增值税有关申报事项的公告" target="_blank">
     <font>国家税务总局关于境内单位代扣代缴自然人增值税有关申报事项的公告</font>
     <em class="wjjdIcon">关于《国家税务总局关于境内单位代扣代缴自然人增值税有关申报事项的公告》的解读</em>
  </a>2026-09-07
</div></body></html>
"""


def test_icon_tooltip_is_not_appended_to_title():
    """实测坑：广东的 <a> 里嵌着"文件解读"图标 <em>，用 text_content() 会把
    解读标题拼到公告标题后面，得到「…公告关于《…公告》的解读」这种畸形标题。
    应优先取 <a> 的 title 属性（那里是干净完整的标题）。"""
    items = province.parse_list_page(LIST_HTML_WITH_ICON, ADAPTER)
    assert len(items) == 1
    assert items[0]["title"] == "国家税务总局关于境内单位代扣代缴自然人增值税有关申报事项的公告"
    assert "的解读" not in items[0]["title"]


def test_title_falls_back_to_first_child_when_no_title_attr():
    html = ('<div><a href="/gdsw/zjfg/2026-09/07/content_c101490a72ea479f872d639466d3ef3b.shtml">'
            '<font>某个足够长的标题文本</font><em>图标提示不该进来</em></a></div>')
    items = province.parse_list_page(html, ADAPTER)
    assert items[0]["title"] == "某个足够长的标题文本"


# ---------------------------------------------------------------- 日期写法
#
# 各省把日期放在完全不同的位置、用完全不同的写法。这里的每一条都对应一个
# 实际站点（四川/重庆/山西），它们曾让整省的日期全空。

def _date_adapter(pattern: str) -> province.ListPageAdapter:
    return province.ListPageAdapter(
        source_id="test_dates", region="测试省", site_name="测试省税务局",
        list_url="http://example.test/list.html",
        detail_href_re=pattern, base_url="http://example.test")


def test_date_from_url_when_list_shows_only_month_day():
    """四川：列表只给「月-日」，完整年月日在详情 URL 里。

    用站点自己写在 URL 里的年月日，而不是拿"当年"去补 —— 那是原文，
    不是我们的推断。
    """
    html = """
    <ul><li><span>09-28</span>
      <a href="http://example.test/art/2026/9/28/art_19973_21901.html">国家税务总局关于发布裁量基准的公告</a>
    </li></ul>
    """
    items = province.parse_list_page(
        html, _date_adapter(r"/art/\d{4}/\d{1,2}/\d{1,2}/art_\d+_\d+\.html"))
    assert items[0]["cwrq"] == "2026-09-28"


def test_date_in_sibling_node():
    """重庆：``<dl><dd><a>标题</a></dd><dd>2026-09-04</dd></dl>``。

    父元素里只有标题，日期在隔壁兄弟节点 —— 只看父元素会整省全空。
    """
    html = """
    <dl><dd><a href="http://example.test/cqtax/202609/t20260904_1.html">国家税务总局关于某某事项的公告</a></dd>
        <dd>2026-09-04</dd></dl>
    """
    items = province.parse_list_page(
        html, _date_adapter(r"/cqtax/\d{6}/t\d+_\d+\.html"))
    assert items[0]["cwrq"] == "2026-09-04"


def test_bare_month_day_without_year_is_left_empty():
    """山西：``<li><p><a>标题</a></p><span>09-28</span></li>``，URL 里没有年月日。

    **年份认不出来就不填。**
    这里原来叫 ``..._falls_back_to_current_year``，断言补 ``date.today().year``，
    理由是「只用于排序展示，不参与效力判断」。那个理由站不住：入库之后它就是
    一个**看起来完全正常的成文日期**，页面、导出、按日期判断时效的路径全都当真。
    实测后果：山西 8 条因此被标成 2026 年。
    留空只是少一个排序键，填错会误导引用。
    """
    html = """
    <ul><li><p>
      <a href="http://example.test/web/detail/sx-11400-545-1824976" title="国家税务总局关于某某事项的公告">国家税务总局关于某某事项的公告</a>
    </p><span>09-28</span></li></ul>
    """
    items = province.parse_list_page(
        html, _date_adapter(r"/web/detail/sx-\d+-\d+-\d+"))
    assert items[0]["cwrq"] is None


def test_sibling_text_that_is_not_a_date_is_ignored():
    """「3-5 个工作日」不是日期。

    它的两个数字都在合理范围内（月 3、日 5），所以光靠范围校验拦不住 ——
    必须要求那个格子的文本**整个**就是日期。
    """
    html = """
    <dl><dd><a href="http://example.test/cqtax/202609/t20260904_1.html">国家税务总局关于某某事项的公告</a></dd>
        <dd>3-5 个工作日</dd></dl>
    """
    items = province.parse_list_page(
        html, _date_adapter(r"/cqtax/\d{6}/t\d+_\d+\.html"))
    assert items[0]["cwrq"] is None


def test_date_not_taken_from_neighbouring_entry():
    """隔壁条目（含详情链接的兄弟）的日期不能被安到这一条头上。"""
    html = """
    <ul>
      <li><a href="http://example.test/cqtax/202609/t20260904_1.html">国家税务总局关于某某事项的公告</a></li>
      <li><a href="http://example.test/cqtax/202608/t20260801_2.html">国家税务总局关于另一事项的公告</a><span>08-01</span></li>
    </ul>
    """
    items = province.parse_list_page(
        html, _date_adapter(r"/cqtax/\d{6}/t\d+_\d+\.html"))
    assert len(items) == 2
    assert items[0]["cwrq"] is None          # 这条自己的格子里没有日期
    assert items[1]["cwrq"] is None          # 只有月日、没有年份 → 留空（见上一条测试的说明）


# ---------------------------------------------------------------------------
# 页内 JS 数组型（陕西）：419 篇全在 urls[] 里，DOM 只给 15 条。
# 这一组是为了防住一个真出过的 bug：新解析器漏了 doc_uid，
# pytest 全绿但采集一跑就 KeyError（测试没覆盖"解析器输出 → build_row"的接口）。
# ---------------------------------------------------------------------------
JS_ADAPTER = province.ListPageAdapter(
    source_id="js_src",
    region="测试省",
    site_name="测试省税务局",
    list_url="http://example.test/lib.html",
    detail_href_re=r"/art/\d{4}/\d{1,2}/\d{1,2}/art_\d+_\d+\.html",
    base_url="http://example.test",
)


def _js_page(n: int) -> str:
    """造一个陕西式页面：``urls[]`` 里有 n 条，DOM 里只有 1 条。"""
    arr = "".join(
        f"urls[i]='/art/2026/9/{i % 28 + 1}/art_15172_{700000 + i}.html';"
        f"headers[i]='第 {i} 号测试公告';"
        for i in range(n))
    return (
        "<html><body>"
        "<a href='/art/2026/9/1/art_15172_700001.html'>DOM 里的一条</a>"
        "<script>function showNews_60113(start_num){var urls=new Array();"
        f"var headers=new Array();var i=0;{arr}}}"
        "</script></body></html>")


def test_js_array_parsed_when_dom_has_only_a_few():
    """DOM 只给 1 条，但 urls[] 里有 40 条 —— 40 条都要解析出来。"""
    items = province.parse_list_page(_js_page(40), JS_ADAPTER)
    assert len(items) == 40
    assert all(i["url"].startswith("http://example.test/art/") for i in items)
    assert items[0]["cwrq"].startswith("2026-09-"), "日期应从 URL 的 /art/ 段取"


def test_js_array_items_carry_doc_uid():
    """doc_uid 必须在解析器这一层带上 —— 写库时读它，缺了就 KeyError。"""
    items = province.parse_list_page(_js_page(12), JS_ADAPTER)
    assert all(i.get("doc_uid") for i in items)
    assert all(i["doc_uid"].startswith("js_src:") for i in items)


def test_js_array_items_survive_build_row():
    """端到端：解析器的输出要能直接喂给 build_provincial_row。

    这是真出过的 bug —— 解析器漏 doc_uid，295 项测试全绿，采集一跑就崩。
    """
    items = province.parse_list_page(_js_page(12), JS_ADAPTER)
    row = province.build_provincial_row(items[0], JS_ADAPTER)
    assert row["doc_uid"] == items[0]["doc_uid"]
    assert row["p_region"] == "测试省"


def test_js_array_ignored_when_too_few():
    """少于 10 条不采用 —— urls[]= 是通用写法，别站可能存无关链接。"""
    items = province.parse_list_page(_js_page(3), JS_ADAPTER)
    assert len(items) == 1, "应回退到 DOM 解析"
    assert "DOM" in items[0]["title"]


def test_adapter_source_ids_are_unique():
    """source_id 必须唯一 —— 2026-10 全量审计发现的**真实缺陷**。

    同一个 source_id 被定义两次时两条路的行为不一致：
      · ``ADAPTERS``（tuple）里两份都在，遍历时**两份都跑**（重复抓同一个栏目）
      · ``ADAPTERS_BY_ID``（dict）只保留**后**一份，于是"按 id 取配置"拿到的
        可能恰好是配置更少的那份

    实测后果：``gs_zcwj`` 被定义两次，后一份没有 ``extra_urls`` —— 按 id 取
    配置的那条路一直在丢 col9689 / col36 三个源，且**不报错**。

    实测三组（jx_zcwj / hlj_zcwj / gs_zcwj），合并后适配器 81 → 78。
    这条断言防止它再发生。
    """
    from taxassist import province_sources as psmod

    ids = [a.source_id for a in psmod.ADAPTERS]
    dup = sorted({i for i in ids if ids.count(i) > 1})
    assert not dup, (
        f"source_id 重复：{dup} —— ADAPTERS_BY_ID 会静默丢弃前一份配置，"
        "而两份的 extra_urls 往往互补（见本测试的 docstring）")
    assert len(psmod.ADAPTERS_BY_ID) == len(psmod.ADAPTERS), (
        f"ADAPTERS {len(psmod.ADAPTERS)} 个，但 ADAPTERS_BY_ID 只有 "
        f"{len(psmod.ADAPTERS_BY_ID)} 个")
