"""企业所得税汇算清缴调整规则。

======================================================================
这些数字必须能追溯、也必须能被你核对
======================================================================

规则里的限额比例（14%、8%、2%、60%、5‰、12%、100% 等）**不是我的判断，
而是法条与文件规定**，每条规则都写明依据。但要说清两件事：

1. **规则数值可能已变**。政策会调整（例如研发费用加计扣除比例历史上
   从 50% → 75% → 100% 变过三次）。本文件的数值按编写时的有效规定录入，
   **首次使用前请核对一遍**，尤其是加计扣除比例与小规模优惠。
2. **规则与政策库联动**：每条规则的 ``check_keywords`` 会去本地政策库检索，
   底稿上标明"依据在库中是否有对应文件、该文件现行是否有效"。
   库里查不到时**明确标注为未验证**，而不是假装有依据。

所以底稿给出的不是"结论"，是**可核对的初稿**。最终判断权在你。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Kind = Literal["cap", "disallow", "credit", "deduct"]

# 适用的企业所得税税率。标准税率 25%；其他情形（小型微利、高新技术企业等）
# 需按企业实际资格确认后填写 —— 本工具不做资格判定。
STANDARD_RATE = 0.25


@dataclass(frozen=True)
class Rule:
    """一条纳税调整规则。"""

    code: str
    name: str
    kind: Kind
    basis: str                       # 法规依据（人可读）
    formula: str                     # 限额公式说明（人可读，底稿上会显示）
    limit_ratio: float | None = None # 相对基准的比例（14% → 0.14）
    limit_base: str | None = None    # 基准字段名：wages / operating_revenue / total_profit
    special_ratio: float | None = None   # 特殊比例（业务招待费 60%）
    carryforward: bool = False       # 超限部分是否可结转以后年度
    check_keywords: tuple[str, ...] = ()
    note: str = ""


# ⚠ 修改本表前先读模块头部的两条说明。
RULES: tuple[Rule, ...] = (
    Rule(
        code="entertainment",
        name="业务招待费",
        kind="cap",
        basis="《企业所得税法实施条例》第四十三条",
        formula="按发生额 60% 扣除，且不超过当年营业收入的 5‰（两者孰低）",
        limit_ratio=0.005, limit_base="operating_revenue", special_ratio=0.60,
        check_keywords=("业务招待费", "招待费"),
        note="双限额取小；超过部分不得扣除，也不得结转以后年度",
    ),
    Rule(
        code="advertising",
        name="广告费和业务宣传费",
        kind="cap",
        basis="《企业所得税法实施条例》第四十四条",
        formula="不超过当年营业收入 15%（部分行业 30%，需按所属行业确认）",
        limit_ratio=0.15, limit_base="operating_revenue", carryforward=True,
        check_keywords=("广告费和业务宣传费", "广告费", "业务宣传费"),
        note="超限部分准予结转以后纳税年度扣除；化妆品/医药/饮料制造等行业比例不同",
    ),
    Rule(
        code="welfare",
        name="职工福利费",
        kind="cap",
        basis="《企业所得税法实施条例》第四十条",
        formula="不超过工资薪金总额 14%",
        limit_ratio=0.14, limit_base="wages",
        check_keywords=("职工福利费",),
    ),
    Rule(
        code="education",
        name="职工教育经费",
        kind="cap",
        basis="《企业所得税法实施条例》第四十二条及财税〔2018〕51号",
        formula="不超过工资薪金总额 8%",
        limit_ratio=0.08, limit_base="wages", carryforward=True,
        check_keywords=("职工教育经费",),
        note="超限部分准予结转以后纳税年度扣除",
    ),
    Rule(
        code="union",
        name="工会经费",
        kind="cap",
        basis="《企业所得税法实施条例》第四十一条",
        formula="不超过工资薪金总额 2%",
        limit_ratio=0.02, limit_base="wages",
        check_keywords=("工会经费",),
    ),
    Rule(
        code="donation",
        name="公益性捐赠支出",
        kind="cap",
        basis="《企业所得税法》第九条",
        formula="不超过年度利润总额 12%",
        limit_ratio=0.12, limit_base="total_profit", carryforward=True,
        check_keywords=("公益性捐赠", "捐赠"),
        note="须为通过公益性社会组织或县级以上人民政府的捐赠；超限部分准予结转三年",
    ),
    Rule(
        code="fines",
        name="税收滞纳金、罚金、罚款和被没收财物损失",
        kind="disallow",
        basis="《企业所得税法》第十条第（三）（四）项",
        formula="全额调增",
        check_keywords=("滞纳金", "罚款"),
        note="行政性罚款不得扣除；经营性罚款（如违约金）可扣除，需区分",
    ),
    Rule(
        code="provisions",
        name="未经核定的准备金支出",
        kind="disallow",
        basis="《企业所得税法》第十条第（七）项",
        formula="全额调增",
        check_keywords=("准备金",),
        note="资产减值准备等会计计提数不得税前扣除，实际发生时再按税法确认",
    ),
    Rule(
        # ⚠ code 必须与 CitInputs 的字段名一致：引擎按 code 去输入对象里取值。
        # 不一致会导致该项**永远取到 0**，静默算出一份漏掉加计扣除的底稿 ——
        # 这个 bug 真实发生过（原 code 写成了 rd_super_deduction）。
        code="rd_expense",
        name="研发费用加计扣除",
        kind="credit",
        basis="财政部 税务总局公告2023年第7号（比例历经多次调整，务必核对现行规定）",
        formula="未形成无形资产的，按实际发生额 100% 加计扣除",
        limit_ratio=1.00, limit_base="rd_expense",
        check_keywords=("研发费用", "加计扣除"),
        note="**比例最易变动，务必以现行文件为准**；负面清单行业不适用",
    ),
)

RULES_BY_CODE: dict[str, Rule] = {r.code: r for r in RULES}

# 输入字段 → 中文名（用于底稿与提示）
FIELD_LABELS: dict[str, str] = {
    "operating_revenue": "营业收入",
    "total_profit": "利润总额",
    "wages": "工资薪金总额",
    "welfare": "职工福利费发生额",
    "education": "职工教育经费发生额",
    "union": "工会经费发生额",
    "entertainment": "业务招待费发生额",
    "advertising": "广告费和业务宣传费发生额",
    "donation": "公益性捐赠支出",
    "rd_expense": "研发费用实际发生额",
    "fines": "税收滞纳金、罚款等",
    "provisions": "计提的各项准备金",
}


def rules_digest() -> str:
    """规则清单摘要（便于在底稿首页与 CLI 里展示，提醒用户核对）。"""
    lines = ["适用规则清单（限额比例务必按现行规定核对）："]
    for r in RULES:
        lines.append(f"  · {r.name}：{r.formula}")
    return "\n".join(lines)
