"""政策标题的英译：术语表 + 结构化模板。

======================================================================
为什么不用机器翻译模型
======================================================================

全库存量约 5090 条标题。本机没有可用的翻译模型（也没装 ollama/GPU 推理栈），
云端翻译 API 涉及费用，而正文全量约 1700 万字 —— 全量精译在成本上不成立。

但中文政策标题**高度模式化**：

    [发文机关] 关于 [事由] 的 [文种]
    财政部 税务总局关于增值税小规模纳税人减免增值税政策的公告

这类结构占了绝大多数，用「术语表 + 模板」能覆盖大部分，且**完全离线、零成本、
可复现**（同一标题永远得到同一译文，便于建立索引）。

======================================================================
诚实的边界（必须写进页面，不能省）
======================================================================

**这是模板翻译，不是专业法律翻译。** 它保证术语一致、结构可读，
但不保证法律含义的精确对应。因此：

- 页面上必须标明「机器辅助翻译，不作为官方英文文本引用」
- 中文原文永远是权威版本
- 未收录的词保留原样，并在覆盖率里体现（低覆盖的标题会标出来）

宁可显示「部分未译」，也不要编一个看起来通顺但意思偏了的英文。
"""
from __future__ import annotations

import re

# ---------------------------------------------------------------- 术语表
#
# 只收两类：① 发文机关；② 高频税务术语与文种。
# 收词原则：**宁缺毋滥** —— 译错的专有名词比不译危害大。

AGENCIES: dict[str, str] = {
    "全国人民代表大会常务委员会": "Standing Committee of the National People's Congress",
    "全国人民代表大会": "National People's Congress",
    "国务院": "State Council",
    "财政部": "Ministry of Finance",
    "国家税务总局": "State Taxation Administration",
    "海关总署": "General Administration of Customs",
    "国家发展和改革委员会": "National Development and Reform Commission",
    "商务部": "Ministry of Commerce",
    "工业和信息化部": "Ministry of Industry and Information Technology",
    "科学技术部": "Ministry of Science and Technology",
    "人力资源和社会保障部": "Ministry of Human Resources and Social Security",
    "住房和城乡建设部": "Ministry of Housing and Urban-Rural Development",
    "自然资源部": "Ministry of Natural Resources",
    "生态环境部": "Ministry of Ecology and Environment",
    "交通运输部": "Ministry of Transport",
    "农业农村部": "Ministry of Agriculture and Rural Affairs",
    "国家市场监督管理总局": "State Administration for Market Regulation",
    "中国证券监督管理委员会": "China Securities Regulatory Commission",
    "中国人民银行": "People's Bank of China",
    "国家外汇管理局": "State Administration of Foreign Exchange",
    "国务院办公厅": "General Office of the State Council",
    "税务总局": "State Taxation Administration",
    "国家税务总局办公厅": "General Office of the State Taxation Administration",
}

GENRES: dict[str, str] = {
    "公告": "Announcement",
    "通知": "Notice",
    "办法": "Measures",
    "暂行条例": "Interim Regulations",
    "条例": "Regulations",
    "实施细则": "Implementation Rules",
    "规定": "Provisions",
    "决定": "Decision",
    "批复": "Official Reply",
    "函": "Letter",
    "意见": "Opinions",
    "规则": "Rules",
    "目录": "Catalogue",
    "指南": "Guidelines",
    "指引": "Guidelines",
    "法律": "Law",
    "政策": "Policy",
    "解读": "Interpretation",
    "答记者问": "Q&A with the Press",
}

# 税务术语。按长度倒序匹配，避免「增值税」先于「土地增值税」命中。
TERMS: dict[str, str] = {
    "土地增值税": "land appreciation tax",
    "增值税": "value-added tax",
    "企业所得税": "enterprise income tax",
    "个人所得税": "individual income tax",
    "消费税": "consumption tax",
    "印花税": "stamp duty",
    "房产税": "property tax",
    "契税": "deed tax",
    "车船税": "vehicle and vessel tax",
    "资源税": "resource tax",
    "环境保护税": "environmental protection tax",
    "城市维护建设税": "urban maintenance and construction tax",
    "耕地占用税": "cultivated land occupation tax",
    "城镇土地使用税": "urban land use tax",
    "关税": "customs duty",
    "车辆购置税": "vehicle purchase tax",
    "船舶吨税": "tonnage tax",
    "烟叶税": "tobacco leaf tax",
    "加计扣除": "super-deduction",
    "留抵退税": "VAT credit refund",
    "小规模纳税人": "small-scale taxpayer",
    "一般纳税人": "general taxpayer",
    "纳税人": "taxpayer",
    "扣缴义务人": "withholding agent",
    "纳税申报": "tax filing",
    "汇算清缴": "annual final settlement",
    "预缴": "advance payment",
    "税收优惠": "tax incentives",
    "减免税": "tax relief",
    "免税": "tax exemption",
    "退税": "tax refund",
    "出口退税": "export tax rebate",
    "税收征管": "tax collection and administration",
    "发票": "invoice",
    "增值税专用发票": "VAT special invoice",
    "电子发票": "electronic invoice",
    "税收协定": "tax treaty",
    "转让定价": "transfer pricing",
    "关联交易": "related-party transaction",
    "研发费用": "R&D expenses",
    "高新技术企业": "high-tech enterprise",
    "小微企业": "small and micro enterprise",
    "个体工商户": "sole proprietorship",
    "非居民企业": "non-resident enterprise",
    "居民企业": "resident enterprise",
    "境外所得": "foreign-source income",
    "税收抵免": "tax credit",
    "特别纳税调整": "special tax adjustment",
    "核定征收": "assessed collection",
    "查账征收": "audit-based collection",
    "代扣代缴": "withholding",
    "委托代征": "entrusted collection",
    "社会保险费": "social insurance premium",
    "文化事业建设费": "cultural undertaking construction fee",
    "教育费附加": "education surcharge",
    "地方教育附加": "local education surcharge",
    "申报表": "tax return form",
    "征收管理": "collection and administration",
    "优惠政策": "preferential policy",
    "延续实施": "continuation",
    "执行": "implementation",
    "废止": "repeal",
    "修改": "amendment",
    "失效": "invalidation",
    "二手车": "used vehicle",
    "新能源汽车": "new energy vehicle",
    "资源综合利用": "comprehensive resource utilization",
    "创业投资": "venture capital",
    "天使投资": "angel investment",
    "股权激励": "equity incentive",
    "技术转让": "technology transfer",
    "跨境": "cross-border",
    "服务贸易": "trade in services",
    "综合保税区": "comprehensive bonded zone",
    "自由贸易试验区": "pilot free trade zone",
}

# 法律名整条收录：这类标题没有「关于…的…」结构，逐词拼会读不通。
# 整条译名以官方英文名称为准。
LAWS: dict[str, str] = {
    "中华人民共和国税收征收管理法": "Law of the People's Republic of China on the Administration of Tax Collection",
    "中华人民共和国企业所得税法": "Enterprise Income Tax Law of the People's Republic of China",
    "中华人民共和国个人所得税法": "Individual Income Tax Law of the People's Republic of China",
    "中华人民共和国增值税法": "Value-Added Tax Law of the People's Republic of China",
    "中华人民共和国印花税法": "Stamp Duty Law of the People's Republic of China",
    "中华人民共和国资源税法": "Resource Tax Law of the People's Republic of China",
    "中华人民共和国契税法": "Deed Tax Law of the People's Republic of China",
    "中华人民共和国城市维护建设税法": "Urban Maintenance and Construction Tax Law of the People's Republic of China",
    "中华人民共和国环境保护税法": "Environmental Protection Tax Law of the People's Republic of China",
    "中华人民共和国车船税法": "Vehicle and Vessel Tax Law of the People's Republic of China",
    "中华人民共和国车辆购置税法": "Vehicle Purchase Tax Law of the People's Republic of China",
    "中华人民共和国船舶吨税法": "Tonnage Tax Law of the People's Republic of China",
    "中华人民共和国耕地占用税法": "Cultivated Land Occupation Tax Law of the People's Republic of China",
    "中华人民共和国烟叶税法": "Tobacco Leaf Tax Law of the People's Republic of China",
    "中华人民共和国行政处罚法": "Administrative Penalty Law of the People's Republic of China",
    "中华人民共和国行政复议法": "Administrative Reconsideration Law of the People's Republic of China",
    "中华人民共和国民事诉讼法": "Civil Procedure Law of the People's Republic of China",
    "中华人民共和国个人信息保护法": "Personal Information Protection Law of the People's Republic of China",
    "中华人民共和国个人独资企业法": "Sole Proprietorship Enterprise Law of the People's Republic of China",
    "中华人民共和国税收征收管理法实施细则": "Detailed Rules for the Implementation of the Law of the People's Republic of China on the Administration of Tax Collection",
}

# 结构词：出现在「关于……的……」里，需要按英文语序重排。
# body 用 .*? 而非 .+?：标题去掉发文机关后常常**直接以「关于」开头**，
# 要求至少一个字符会让整条规则失效（实测覆盖率因此腰斩）。
_ABOUT_RE = re.compile(
    r"^(?P<body>.*?)(?:关于|关于印发|关于发布)?(?P<subject>.+?)的"
    r"(?P<genre>公告|通知|批复|函|决定|意见|办法|规定|规则|目录|指南|指引|解读|通报|解答)$")
_PLAIN_RE = re.compile(
    r"^(?P<body>.*?)(?P<genre>公告|通知|批复|函|决定|意见|办法|规定|规则|法律|条例|目录|指南|指引)$")
_DOCNO_RE = re.compile(r"[（(]?\s*[\u4e00-\u9fa5]{0,12}[〔\[（(]\s*20\d{2}\s*[〕\]）)]\s*\d{1,4}\s*号\s*[）)]?")


def _split_agencies(text: str) -> tuple[list[str], str]:
    """从标题开头切出发文机关序列，返回 (机关英文列表, 剩余文本)。"""
    remaining = text.strip()
    found: list[str] = []
    changed = True
    while changed:
        changed = False
        for zh in sorted(AGENCIES, key=len, reverse=True):
            if remaining.startswith(zh):
                found.append(AGENCIES[zh])
                remaining = remaining[len(zh):].lstrip(" \u3000、")
                changed = True
                break
    return found, remaining


def _translate_terms(text: str) -> tuple[str, int, int]:
    """按术语表替换；返回 (结果, 已译词数, 未译汉字数)。

    法律名（LAWS）与普通术语合并后**按长度倒序**匹配：长的先命中，
    否则「企业所得税」会抢在「中华人民共和国企业所得税法」前面被替换，
    结果拼出一句读不通的英文。
    """
    out = text
    hits = 0
    table = {**LAWS, **TERMS}
    for zh in sorted(table, key=len, reverse=True):
        if zh in out:
            out = out.replace(zh, table[zh])
            hits += 1
    # 数一下还剩多少没译的汉字（用于算覆盖率）
    leftover = len(re.findall(r"[\u4e00-\u9fa5]", out))
    return out, hits, leftover


def translate_title(zh_title: str) -> tuple[str, float]:
    """把中文政策标题译成英文，返回 ``(译文, 覆盖率 0~1)``。

    覆盖率 = 已识别的汉字数 / 标题里的汉字总数。低于 0.6 的标题在界面上会被标出来，
    提示"部分未译"—— 与其编一句通顺但可能错误的英文，不如老实标注。
    """
    if not zh_title or not zh_title.strip():
        return "", 0.0

    original = zh_title.strip()
    total_han = len(re.findall(r"[\u4e00-\u9fa5]", original)) or 1

    # 文号先摘出来单独处理，不参与翻译
    docnos = _DOCNO_RE.findall(original)
    work = _DOCNO_RE.sub("", original).strip()

    agencies, rest = _split_agencies(work)

    m = _ABOUT_RE.match(rest)
    if m:
        subject, hits_s, left_s = _translate_terms(m.group("subject"))
        genre = GENRES.get(m.group("genre"), m.group("genre"))
        translated = f"{genre} on {subject}"
    else:
        m2 = _PLAIN_RE.match(rest)
        if m2:
            subject, hits_s, left_s = _translate_terms(m2.group("body"))
            genre = GENRES.get(m2.group("genre"), m2.group("genre"))
            translated = f"{genre} on {subject}" if subject else genre
        else:
            translated, hits_s, left_s = _translate_terms(rest)

    prefix = ", ".join(agencies)
    result = f"{prefix} — {translated}" if prefix else translated
    if docnos:
        result = f"{result} {docnos[0].strip()}"

    translated_han = max(total_han - left_s, 0)
    coverage = min(translated_han / total_han, 1.0)
    return result.strip(), round(coverage, 3)


DISCLAIMER = "机器辅助翻译，仅供参考；以中文原文为准，不作为官方英文文本引用。"


# ---------------------------------------------------------------- 反向映射：英文 → 中文
#
# 用途：让英文查询**直接命中文政策**，而不必先把 5090 条标题全译一遍。
# 中文用户搜 "value-added tax" 时，我们把它映射回"增值税"再走原有检索。
# 这样做还有一个好处：即使将来有了英文标题，这套映射仍然有用 ——
# 用户用的措辞未必与我们的译文完全一致。

_EN_TO_ZH: dict[str, str] = {}
for _zh, _en in {**TERMS, **LAWS}.items():
    _EN_TO_ZH.setdefault(_en.lower(), _zh)


def to_chinese_query(query: str) -> tuple[str, list[str]]:
    """把查询里的英文术语换成对应中文。

    返回 ``(改写后的查询, 命中的中文术语)``。没命中就原样返回；调用方可以据此
    在界面上说明「已按术语对照把 value-added tax 理解为 增值税」，让用户知道
    为什么搜英文却出来中文结果 —— 透明度比结果本身更容易被误读。
    """
    if not query:
        return query, []
    work = query
    hits: list[str] = []
    for en in sorted(_EN_TO_ZH, key=len, reverse=True):
        pattern = re.compile(re.escape(en), re.IGNORECASE)
        if pattern.search(work):
            zh = _EN_TO_ZH[en]
            if zh not in hits:
                hits.append(zh)
            work = pattern.sub(zh, work)
    return work.strip(), hits
