"""数据值的英文标签 —— 界面双语用。

**为什么要单独一张表**：界面文案用 ``data-zh="…" data-en="…"`` 成对写法就够了，
但库里的值（地区名、栏目名、税种名、效力状态）是**数据**，散落在分组标题、
筛选下拉、结果行、徽章等十几个地方。逐个手写成对属性既容易漏（实测漏过一次：
"已使用"与"已用"差一个字导致英文界面漏出中文），也容易出现同一个词两种译法。
集中一处，模板通过 Jinja 全局 ``en()`` 取。

**查表口径**：查不到就**原样返回**。宁可在英文界面看到一个中文地名，也不要
出现空白或 "None" —— 这张表是"锦上添花"，坏了不该让页面缺字。所以取用一律
走 :func:`en`，不要在模板里直接索引字典。

**译法**：以通用/官方译法为准（增值税 VAT、企业所得税 Corporate income tax、
契税 Deed tax），不自创。地区用汉语拼音（内蒙古 Inner Mongolia、西藏 Tibet
这两个用惯用英文名）。

**几个词的取舍**：
  · ``全国`` → National（不是 "Nationwide"：它在列表里是**地区这一格的取值**，
    与"北京/广东"并列，National 读起来才是同一类东西；资料库页那个大区分组
    标题另有 ``总局`` → Nationwide，两处语境不同）
  · ``政策解读`` → Explainer（不是 "Interpretation"：太长，且这一栏实际装的是
    答疑、科普、图解，Explainer 更贴）
  · ``尚未生效`` → Not yet in force（不用 "pending"：法律语境里 pending 会被
    读成"待审议"，与"已发布但未到期"不是一回事）
"""

from __future__ import annotations

#: 值 → 英文。地区、大区、栏目、效力、税种合在一张表里：这几类值互不重名，
#: 分表只会让模板里要多传几个全局，不划算。
LABELS: dict[str, str] = {
    # ── 地区（省级行政区 + 全国）────────────────────────────────────
    "全国": "National",
    "北京": "Beijing",
    "天津": "Tianjin",
    "河北": "Hebei",
    "山西": "Shanxi",
    "内蒙古": "Inner Mongolia",
    "辽宁": "Liaoning",
    "吉林": "Jilin",
    "黑龙江": "Heilongjiang",
    "上海": "Shanghai",
    "江苏": "Jiangsu",
    "浙江": "Zhejiang",
    "安徽": "Anhui",
    "福建": "Fujian",
    "江西": "Jiangxi",
    "山东": "Shandong",
    "河南": "Henan",
    "湖北": "Hubei",
    "湖南": "Hunan",
    "广东": "Guangdong",
    "广西": "Guangxi",
    "海南": "Hainan",
    "重庆": "Chongqing",
    "四川": "Sichuan",
    "贵州": "Guizhou",
    "云南": "Yunnan",
    "西藏": "Tibet",
    "陕西": "Shaanxi",          # 与山西 Shanxi 靠拼写区分，别写成一样的
    "甘肃": "Gansu",
    "青海": "Qinghai",
    "宁夏": "Ningxia",
    "新疆": "Xinjiang",

    # ── 大区分组标题（资料库页/检索页的地区分组）──────────────────
    "总局": "Nationwide",
    "华北": "North China",
    "东北": "Northeast China",
    "华东": "East China",
    "华中": "Central China",
    "华南": "South China",
    "西南": "Southwest China",
    "西北": "Northwest China",

    # ── 栏目（o_column）──────────────────────────────────────────
    "地方政策": "Local policy",
    "政策法规": "Policy & regulation",
    "政策解读": "Explainer",

    # ── 效力状态（p_effect_status）────────────────────────────────
    # 与 templates/_macros.html 的 effect_badge 保持一致 —— 那里是写死的成对
    # 属性，这里给"下拉选项 / 分组标题 / 结果行"用（它们渲染的是数据值本身）。
    "现行有效": "In force",
    "已废止": "Repealed",
    "尚未生效": "Not yet in force",
    "部分失效": "Partly repealed",
    "未判定": "Undetermined",

    # ── 税种（filters.TAX_TYPE_KEYWORDS 的键）────────────────────
    "增值税": "VAT",
    "土地增值税": "Land appreciation tax",
    "企业所得税": "Corporate income tax",
    "个人所得税": "Individual income tax",
    "消费税": "Consumption tax",
    "印花税": "Stamp duty",
    "房产税": "Property tax",
    "城镇土地使用税": "Urban land use tax",
    "耕地占用税": "Farmland occupation tax",
    "契税": "Deed tax",
    "车船税": "Vehicle and vessel tax",
    "车辆购置税": "Vehicle purchase tax",
    "资源税": "Resource tax",
    "环境保护税": "Environmental protection tax",
    "关税": "Customs duty",
    "出口退税": "Export tax rebate",
    "税收征管": "Tax administration",

    # ── 邀请码状态（admin 页）────────────────────────────────────
    "可用": "Usable",
    "已使用": "Used",
    "已用": "Used",
    "已吊销": "Revoked",
    "已过期": "Expired",
    "过期": "Expired",
}


def en(value: object) -> str:
    """取值的英文；查不到或为空就原样返回（**绝不返回 None**）。

    模板里当 Jinja 全局用：``data-en="{{ en(r.p_region) }}"``。
    """
    if value is None:
        return ""
    text = str(value)
    return LABELS.get(text, text)
