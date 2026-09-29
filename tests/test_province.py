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
