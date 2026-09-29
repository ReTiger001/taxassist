"""详情页提取测试。

这些断言都来自对真实页面的实测（2026-09-29），
若官方改版导致失败，**先重跑探测再改断言**，不要为了让测试变绿而放宽校验。
"""
from __future__ import annotations

from taxassist.collect.detail import parse_detail

# 取自真实详情页的关键片段（国家税务总局公告2026年第19号）
SAMPLE = """
<html><head><title>国家税务总局政策法规库</title>
<script>var _trackData = 1;</script></head>
<body>
<div class="currency cont">
  <div class="container">
    <p class="fontsize">字体：【大】【中】【小】</p>
    <p class="collect">收藏 订阅 已推送，请在“个人中心-我的订阅”中查看</p>
    <p class="arc_date">尚未生效 成文日期：2026-09-04</p>
    <div class="article">
      <p>注释</p>
      <p>根据《中华人民共和国增值税法》、《中华人民共和国增值税法实施条例》以及《财政部 税务总局关于发布〈境内单位代扣代缴自然人增值税管理办法〉的公告》（2026年第28号）有关规定，国家税务总局制定了《境内单位代扣代缴自然人增值税及附加税费申报表》及其附列资料，现予公布。</p>
      <p>本公告自2026年11月1日起施行。</p>
      <p>特此公告。</p>
      <p>附件：1.《境内单位代扣代缴自然人增值税及附加税费申报表》及其附列资料</p>
    </div>
    <p>国家税务总局关于境内单位代扣代缴自然人增值税有关申报事项的公告</p>
    <p>国家税务总局公告2026年第19号</p>
    <div class="artsets">
      <a href="/zcfgk/c100015/c5252179/content.html">关于《国家税务总局关于境内单位代扣代缴自然人增值税有关申报事项的公告》的解读</a>
    </div>
    <a href="5252176/files/《境内单位代扣代缴自然人增值税及附加税费申报表》及其附列资料.xls">申报表</a>
    <a href="5252176/files/填报说明.doc">填报说明</a>
    <p>【打印】 【下载】 纠错或建议</p>
  </div>
</div>
</body></html>
"""


def test_extract_body_and_metadata():
    r = parse_detail(SAMPLE, base_url="http://fgk.chinatax.gov.cn/zcfgk/c100012/c5252176/content.html")
    assert r.body is not None
    assert "根据《中华人民共和国增值税法》" in r.body
    assert "本公告自2026年11月1日起施行" in r.body
    # 界面噪音必须被剔除
    assert "字体：【大】【中】【小】" not in r.body
    assert "当前位置" not in r.body
    assert "收藏 订阅" not in r.body

    # 官方时效来自 p.arc_date
    assert r.aging_official == "尚未生效"
    assert r.cwrq == "2026-09-04"

    # 完整文号 —— 列表接口的 docNo 只有序号 "19"，这里必须是完整文号
    assert r.doc_no == "国家税务总局公告2026年第19号"

    # 施行日期从正文抽取
    assert r.effective_date == "2026-11-01"


def test_attachments_are_absolutised():
    r = parse_detail(SAMPLE, base_url="http://fgk.chinatax.gov.cn/zcfgk/c100012/c5252176/content.html")
    exts = {a["ext"] for a in r.attachments}
    assert "xls" in exts and "doc" in exts
    assert all(a["url"].startswith("http://") for a in r.attachments)


def test_related_documents_captured():
    r = parse_detail(SAMPLE, base_url="http://x/")
    assert any("的解读" in t for t in r.related_titles)


def test_body_reference_docno_does_not_masquerade_as_own_docno():
    """正文里引用的其他文号不能冒充本文文号。

    本文文号 "国家税务总局公告2026年第19号" 在页面中出现，
    正文引用的是 "（2026年第28号）"（无体裁词），不应被误取。
    """
    r = parse_detail(SAMPLE, base_url="http://x/")
    assert "第28号" not in (r.doc_no or "")


def test_aging_keywords_normalised():
    html = '<div class="currency cont"><p class="arc_date">全文失效 成文日期：2010-01-01</p></div>'
    assert parse_detail(html).aging_official == "已废止"


def test_missing_fields_are_none_not_empty_string():
    body = "根据《中华人民共和国增值税法实施条例》有关规定，现将有关事项公告如下，请遵照执行。"
    r = parse_detail(f"<html><body><div class='article'><p>{body}</p></div></body></html>")
    assert r.body is not None
    assert r.doc_no is None
    assert r.aging_official is None
    assert r.effective_date is None


def test_navigation_label_is_not_treated_as_body():
    """实测踩过的坑：导航标签"相关政策文件"（6 字）曾被当作正文写入全库 12 条记录。

    这类污染比抓不到更危险——它看起来像成功。
    """
    html = '<div class="article"><p>相关政策文件</p></div>'
    assert parse_detail(html).body is None


def test_short_body_rejected_by_quality_gate():
    html = '<div class="article"><p>简短标签</p></div>'
    assert parse_detail(html).body is None


def test_real_body_passes_quality_gate():
    text = "根据《中华人民共和国增值税法》及其实施条例有关规定，现将有关事项公告如下，自2026年11月1日起施行。"
    r = parse_detail(f'<div class="article"><p>{text}</p></div>')
    assert r.body is not None
    assert len(r.body) >= 30
