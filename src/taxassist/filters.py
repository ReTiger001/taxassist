"""相关性过滤：把与实务判断无关的内容挪出第一视野。

======================================================================
为什么必须做（实测数据，不是臆测）
======================================================================

30 天窗口抓到的 50 条内容里：

- 真正可执行的政策文件：**3 条**（政策法规栏目，带正式文号）
- 其余 47 条：税法小课堂、一图了解、漫画、视频、办税小知识、案例曝光……

不做过滤，首页和简报页会被科普内容占满。实测第一次打开界面时，
那 3 条政策公告被压在 20 条"税法小课堂"下面 —— 这样的工具，
你会在两周内关掉不再打开。

======================================================================
设计原则：过滤 ≠ 丢弃
======================================================================

所有内容都**照常入库、照常可检索**。过滤只做两件事：

1. 给内容打分类标签（实质政策 / 解读 / 新闻科普 / 其他）
2. 默认排序时把实质政策前置

这样即使分类判断错了，代价也只是"少看一眼解读"，
而不会是把一份公告永远埋掉 —— 后者在这个场景里是不可接受的。
"""
from __future__ import annotations

import re

from .config import NATIONWIDE

# 分类
SUBSTANTIVE = "实质政策"
INTERPRETATION = "解读"
NEWS = "新闻/科普"
OTHER = "其他"

# 栏目 → 是否实质政策文件
# 权威政策栏目：这些栏目的内容由源站做过分类，可以整体视为实质政策。
#
# **"地方政策"不在其中** —— 那是省级列表页整页抓取时我们自己起的栏目名，
# 内容混杂（解读、答记者问、问答、科普都在里面）。把整个栏目视为实质政策，
# 会把大量非文件内容排到最前面：实测广东 25 条里有 11 条因此被误判，
# 结果首页几乎被广东占满，而真正的全国政策文件只有 3 条。
_SUBSTANTIVE_COLUMNS = frozenset({
    "政策法规", "财税文件", "行政法规", "国务院文件",
    "税务规范性文件", "部门规章", "其他文件",
})

# 文件体裁后缀：用来识别**没有正式文号的地方政策文件**
# （省级文件多数不带"XX公告20XX年第N号"式文号，广东 28 条全部无文号）
_DOC_GENRE_SUFFIXES = (
    "的通知", "的公告", "的决定", "的办法", "的规定", "的意见",
    "的批复", "的复函", "实施细则",
)

# 标题特征词：实测这些词几乎只出现在科普/新闻稿里
# 2026-10-11 修两条**过宽**的规则（用户问"还漏什么"时扫出来的）：
#   · ``第.{1,3}期`` 把**项目批次号**当成了期刊号 —— 实测命中 18 条里 16 条是
#     真政策（「…无偿援助…（第二期）免征增值税的通知」、转发《…调整公布
#     第八期…清单的通知》）。代价不只是分类错：这条规则用于**默认排序降权**，
#     于是那批总局文件一直被压在后面。
#   · ``案例`` 命中 12 条里 8 条带正式文号（《税务行政执法案例指导工作实施办法》、
#     《研发费用加计扣除项目鉴定案例》）。收窄成只认科普式的案例说法。
# 判据是「**有正式文号就不算科普**」—— 文号是规范性文件最强的信号。
_NEWS_TITLE_RE = re.compile(
    r"(小课堂|一图了解|图解|漫画|视频|便利贴|我来讲|有回应|办税小知识|"
    r"小贴士|曝光|访谈|直播预告|征期日历|温馨提示|热点问答|"
    r"注意！|注意啦|收藏！|速看|提醒！|手把手|了解一下|算一算|"
    r"必看|秒get|看这里|话税收|热点问题|办税知识|"
    r"开学第一课|千万别踩|要点解答|答记者问|"
    r"典型案例|案例解析|案例解读|案例警示|提示案例)"
)

# 文件体裁后缀见上方的 _DOC_GENRE_SUFFIXES（原 _DOC_TITLE_RE 已合并进去，
# 避免两套规则描述同一件事）


def classify(row) -> str:
    """给一条政策记录打分类标签。

    ``row`` 为 ``sqlite3.Row`` 或 dict，需要含 ``o_column``、``p_doc_no_full``、
    ``title`` 字段（缺失按空处理）。

    判定顺序有意为之：**先看是否有正式文号**，因为文号是"这是一份可执行文件"
    最硬的标志，比栏目名更可靠（有些解读也会挂在政策法规栏目下）。
    """
    def get(key: str):
        try:
            return row[key]
        except (KeyError, IndexError, TypeError):
            return None

    column = (get("o_column") or "").strip()
    doc_no = (get("p_doc_no_full") or "").strip()
    title = (get("title") or "").strip()

    # 0) 以问号结尾的标题必然是问答/科普，不是文件 ——
    #    正式政策文件的标题不会写成问句（实测广东站混在"地方政策"栏目里的
    #    "居民企业取得的投资收益，需不需要交企业所得税？"就属于此类）
    if title.endswith(("？", "?")):
        return NEWS
    # 1) 有正式文号 → 实质政策（最可靠信号）
    if doc_no:
        return SUBSTANTIVE
    # 2) 标题像科普/新闻（先判，避免「一图了解：《某公告》主要内容」被当成实质政策）
    if _NEWS_TITLE_RE.search(title):
        return NEWS
    # 3) 解读 / 答记者问 —— **必须排在栏目判断之前**。
    #    教训："地方政策"栏目里混着大量解读，先按栏目判就会把它们当成实质政策，
    #    导致首页被解读与问答内容占满。
    if column in ("政策解读", "政策指引") or "解读" in title or "答记者问" in title:
        return INTERPRETATION
    # 4) 权威政策栏目（源站分类可信）
    if column in _SUBSTANTIVE_COLUMNS:
        return SUBSTANTIVE
    # 5) 标题是文件体裁 —— 无文号的地方政策文件靠这一条识别
    if title.endswith(_DOC_GENRE_SUFFIXES):
        return SUBSTANTIVE
    return OTHER


def is_substantive(row) -> bool:
    return classify(row) == SUBSTANTIVE


# 设计原则（原先写在已删除的 classify_all 里 —— 2026-10 全量审计确认该函数
# 零调用）：分类结果**不落库**，每次查询实时判断，避免"分类规则改了、库里还是
# 旧标签"这种不一致。规则应当随时可调，标签不该是历史包袱。


def substantive_first_sql(alias: str = "p") -> str:
    """生成"实质政策优先"的 ORDER BY 片段。

    集中在此处是为了让"什么算实质政策"只有一处定义。
    三档优先级与 ``classify`` 保持一致：
      0 = 有正式文号（最强信号）
      1 = 权威政策栏目
      2 = 标题是文件体裁（无文号的地方政策文件）
      9 = 其余（解读、科普、新闻）
    """
    cols = ",".join(f"'{c}'" for c in sorted(_SUBSTANTIVE_COLUMNS))
    genre = " OR ".join(
        f"{alias}.title LIKE '%{suffix}'" for suffix in _DOC_GENRE_SUFFIXES
    )
    return (
        f"CASE WHEN {alias}.p_doc_no_full IS NOT NULL AND {alias}.p_doc_no_full <> '' THEN 0 "
        f"WHEN {alias}.o_column IN ({cols}) THEN 1 "
        f"WHEN ({genre}) THEN 2 "
        f"ELSE 9 END"
    )


# ---------------------------------------------------------------- 税种

# 税种关键词表。刻意用"标题 + 关键词 + 标签"匹配，而不是依赖源站分类字段：
# 实测源站的 xxgk_taxPolicy 多为 ["税收政策"] 这类粗分类，填不出具体税种。
#
# 用法上有一处坑必须处理：**"土地增值税"包含"增值税"**。
# 直接子串匹配会把每一份土地增值税文件同时算作增值税文件，
# 按税种筛选时就会串味 —— 见 extract_tax_types 里的遮蔽处理。
TAX_TYPE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "增值税": ("增值税",),
    "土地增值税": ("土地增值税",),
    "企业所得税": ("企业所得税",),
    "个人所得税": ("个人所得税", "个税"),
    "消费税": ("消费税",),
    "印花税": ("印花税",),
    "房产税": ("房产税",),
    "城镇土地使用税": ("城镇土地使用税", "土地使用税"),
    "耕地占用税": ("耕地占用税",),
    "契税": ("契税",),
    "车船税": ("车船税",),
    "车辆购置税": ("车辆购置税", "车购税"),
    "资源税": ("资源税",),
    "环境保护税": ("环境保护税", "环保税"),
    "关税": ("关税",),
    "出口退税": ("出口退税", "出口退（免）税", "出口货物退"),
    "税收征管": ("征收管理", "税收征管", "纳税申报", "发票管理"),
}

# 遮蔽"土地增值税"，避免其内部子串被当成"增值税"
_LAND_VAT_MASK = "\u0001"


def _row_text(row) -> str:
    def get(key: str):
        try:
            return row[key]
        except (KeyError, IndexError, TypeError):
            return None

    return " ".join(x for x in (
        get("title") or "", get("o_keywords") or "",
        get("o_label") or "", get("o_tax_policy") or "",
    ) if x)


def extract_tax_types(row) -> list[str]:
    """识别一条记录涉及的税种（可多个）。

    长税种名先遮蔽再匹配短名，避免"土地增值税"被判成"增值税"。
    """
    text = _row_text(row)
    if not text:
        return []
    masked = text.replace("土地增值税", _LAND_VAT_MASK)
    found: list[str] = []
    for tax, keywords in TAX_TYPE_KEYWORDS.items():
        haystack = text if tax == "土地增值税" else masked
        if any(kw in haystack for kw in keywords):
            found.append(tax)
    return found


def tax_filter_sql(tax: str, alias: str = "p") -> tuple[str, list]:
    """生成税种筛选的 SQL 片段与参数。未知税种返回空片段。"""
    keywords = TAX_TYPE_KEYWORDS.get(tax)
    if not keywords:
        return "", []
    clauses, params = [], []
    for kw in keywords:
        clauses.append(
            f"({alias}.title LIKE ? OR IFNULL({alias}.o_keywords,'') LIKE ?"
            f" OR IFNULL({alias}.o_tax_policy,'') LIKE ?)")
        params += [f"%{kw}%", f"%{kw}%", f"%{kw}%"]
    return "(" + " OR ".join(clauses) + ")", params


def tax_type_counts(conn) -> dict[str, int]:
    """全库按税种统计条数（供界面筛选下拉显示数量）。"""
    counts: dict[str, int] = {}
    for row in conn.execute(
        "SELECT title, o_keywords, o_label, o_tax_policy FROM policy"
    ):
        for tax in extract_tax_types(row):
            counts[tax] = counts.get(tax, 0) + 1
    return counts


# ---------------------------------------------------------------- 地区

#: 地区分组：行政区划序 + 大区分组。
#:
#: 客户抱怨过"地区不好找"。根因是原先按**条数倒序**排：广东排第几取决于
#: 它有多少条政策，而条数天天在变，等于每次都要重新找一遍。行政区划序是
#: 固定的、人人熟悉的（华北→东北→华东→华中→华南→西南→西北），
#: 位置一旦记住就不用再找。
REGION_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("总局", (NATIONWIDE,)),
    ("华北", ("北京", "天津", "河北", "山西", "内蒙古")),
    ("东北", ("辽宁", "吉林", "黑龙江")),
    ("华东", ("上海", "江苏", "浙江", "安徽", "福建", "江西", "山东")),
    ("华中", ("河南", "湖北", "湖南")),
    ("华南", ("广东", "广西", "海南")),
    ("西南", ("重庆", "四川", "贵州", "云南", "西藏")),
    ("西北", ("陕西", "甘肃", "青海", "宁夏", "新疆")),
)


def region_counts(conn) -> list[tuple[str, int]]:
    """按地区统计条数，按**行政区划序**排列（"全国"最前）。

    供检索页的地区下拉用 —— 下拉里分组标题反而碍事，所以这里是扁平列表。
    总览页的筛选区用 region_groups()，那个带大区分组。
    """
    rows = conn.execute(
        "SELECT COALESCE(p_region, ?) r, COUNT(*) n FROM policy GROUP BY r",
        (NATIONWIDE,),
    ).fetchall()
    rank = {name: i for i, (_, names) in enumerate(REGION_GROUPS) for name in names}
    return sorted(((r["r"], r["n"]) for r in rows),
                  key=lambda x: (rank.get(x[0], len(rank)), x[0]))


def region_groups(conn) -> list[dict]:
    """按大区分组的地区统计，供总览页筛选区。

    没有政策的分组不显示（避免一堆空标题）；不在分组表里的地区归到"其他"，
    不静默丢弃 —— 将来站点新增了行政区，至少还看得见。

    **键名用 ``regions`` 而不是 ``items``**：Jinja 里 ``g.items`` 会取到 dict
    的 items **方法**（属性查找优先于键），模板里循环它直接 TypeError
    （实测踩过）。这类名字与内置方法重名的键一律要避开。
    """
    counts = dict(region_counts(conn))
    out: list[dict] = []
    for label, names in REGION_GROUPS:
        # **零条的行政区也列出来**（模板里灰化）。
        # 起因是客户核对过："总数 5K，下面各省加起来怎么也对不上" —— 数字没错，
        # 是湖北/贵州/北京当时各 0 条、被静默跳过，整行消失看起来像数据丢了。
        # 把 0 显式列出来，"对不上"就变成"能对上"，而且零条本身就是有用信息
        # （说明该省的地方性政策还没抓到）。
        out.append({"label": label,
                    "regions": [(n, counts.get(n, 0)) for n in names]})
    known = {n for _, names in REGION_GROUPS for n in names}
    rest = [(r, n) for r, n in counts.items() if r not in known]
    if rest:
        out.append({"label": "其他", "regions": sorted(rest)})
    return out


def region_filter_sql(region: str, alias: str = "p") -> tuple[str, list]:
    """地区筛选 SQL 片段。空字符串 = 全部地区，返回空片段。"""
    if not region:
        return "", []
    if region == NATIONWIDE:
        # 历史数据或未识别地区一律归入"全国"，避免它们在任何筛选下都不出现
        return f"(IFNULL({alias}.p_region, '{NATIONWIDE}') = '{NATIONWIDE}')", []
    return f"({alias}.p_region = ?)", [region]
