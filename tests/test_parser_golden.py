"""解析器黄金样本：把这次会话里修好的每个解析缺陷钉住。

为什么固化下来：本次一共改了七八轮解析器（正文容器、文号来源、文号年份、
施行日期表述、日期写法），每一轮都靠临时脚本逐省验证 —— 慢，而且容易漏。
把代表性的 HTML 片段与期望结果写进测试，以后一改解析器就能立刻知道有没有
把别的东西弄坏。

样本都是从真实页面截取的**最小片段**（去掉无关噪声），不是编的。
"""
from __future__ import annotations

from taxassist.collect.detail import parse_detail
from taxassist.collect.normalize import doc_no_year


def test_docno_ignores_repeal_note_block():
    """页面「注释」块里的废止公告文号，不能被当成本文文号。

    真实案例：1988 年的《关于对金融系统营业帐簿贴花问题的具体规定》被抽成了
    「国家税务总局公告2022年第14号」—— 那是宣布它全文废止的那份公告。
    """
    html = """
    <html><body>
      <div class="currency cont">财政部 国家税务总局关于对金融系统营业帐簿贴花问题的具体规定
        （1988）国税地字第28号 成文日期：1988-12-12</div>
      <div class="zs">注释 根据《国家税务总局关于实施＜中华人民共和国印花税法＞等有关事项的公告》
        （国家税务总局公告2022年第14号）规定，自2022年7月1日起，本文全文废止。</div>
    </body></html>
    """
    r = parse_detail(html, known_cwrq="1988-12-12")
    assert r.doc_no != "国家税务总局公告2022年第14号"


def test_old_style_docno_without_year_char():
    """老式文号不含「年」字，也必须能抽到。

    旧 XPath 要求元素同时含"年"和"号"，于是 `财税〔2014〕46号` 永远抽不到 ——
    库里那些值其实是 backfill 从正文补的。
    """
    html = (
        '<html><body><h5 class="actfwzh">财税〔2014〕46号</h5>'
        "<p>各省、自治区、直辖市财政厅（局）：现将有关事项通知如下。</p></body></html>"
    )
    r = parse_detail(html, known_cwrq="2014-05-27")
    assert r.doc_no == "财税〔2014〕46号"


def test_doc_no_year_only_structured_positions():
    """文号年份只看结构化位置，绝不把序号当成年份。"""
    assert doc_no_year("发改投资〔2014〕2091号") == 2014
    assert doc_no_year("财税〔2014〕46号") == 2014
    assert doc_no_year("国家税务总局公告2026年第19号") == 2026
    assert doc_no_year("（1988）国税地字第28号") == 1988
    assert doc_no_year("没有年份的字符串") is None


def test_effective_date_with_yiyu_prefix():
    """「已于…起施行」也要能抽到（旧正则只认「自」开头）。

    样本**必须带正文容器**：施行日期只在 result.body 里找，没有容器的页面
    body 为空，断言会"通过"却什么都没测到 —— 第一版样本就踩了这个坑。
    """
    html = ("<html><body><div class='article'><p>《中华人民共和国增值税法》"
            "及其实施条例已于2026年1月1日起施行。</p></div></body></html>")
    r = parse_detail(html)
    assert r.effective_date == "2026-01-01"


def test_effective_date_rejects_before_pub_date():
    """施行日期早于成文日期的，一律丢弃。"""
    html = ("<html><body><div class='article'>"
            "<p>本规定自2025年3月24日起施行。</p></div></body></html>")
    r = parse_detail(html, known_cwrq="1986-12-31")
    assert r.effective_date is None


def test_effective_date_rejects_far_future():
    """施行日期比成文晚 5 年以上的也丢弃（那多半是引用了别的文件）。"""
    html = ("<html><body><div class='article'>"
            "<p>本规定自2025年3月24日起施行。</p></div></body></html>")
    r = parse_detail(html, known_cwrq="2010-01-01")
    assert r.effective_date is None


def test_province_title_strips_date_prefix():
    """广西式「日期 + 标题」的标题要剥掉日期前缀。"""
    from taxassist.province import _clean_title

    assert _clean_title("2026-09-28 国家税务总局关于发布裁量基准的公告") == \
        "国家税务总局关于发布裁量基准的公告"
    # 只有日期、后面没内容的，保留原样 —— 让"没抓到标题"这件事在数据里看得见
    assert _clean_title("2026-09-28") == "2026-09-28"
