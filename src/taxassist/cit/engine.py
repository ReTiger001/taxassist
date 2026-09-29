"""企业所得税汇算清缴纳税调整计算引擎。

======================================================================
先说清楚这个引擎**不做什么**（比它能做什么更重要）
======================================================================

它**不做**：完整申报表、优惠资格判定（小型微利/高新技术企业等）、
弥补亏损结转、境外所得抵免、税会差异的完整识别。

它**做**：常见调整项的机械计算 —— 输入账载金额与基数，输出税收金额、
调增调减、结转额，并给出计算过程与法条依据，供你复核。

为什么要把边界写这么死：汇算清缴出错的责任在签字人身上，
一个"看起来什么都能算"的黑箱比一个"只算清楚这十几项"的白盒危险得多。

======================================================================
关于依据核查
======================================================================

每条规则会拿 ``check_keywords`` 去**本地政策库**里找对应文件，给出三种状态：

- 库中已收录，且该文件现行有效
- 库中已收录，但该文件已被废止/失效 ← 必须警惕
- 库中未收录（不代表依据不存在，只代表本地库没有）

这样你至少能看出"这条规则背后的依据，在我自己的库里是什么状态"。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .rules import FIELD_LABELS, RULES, STANDARD_RATE, Rule

BASIS_OK = "库中已收录·有效"
BASIS_STALE = "库中已收录·已废止"
BASIS_UNKNOWN = "库中未收录"
BASIS_NO_DB = "未核查"


# ---------------------------------------------------------------- 输入

@dataclass
class CitInputs:
    """底稿输入。全部来自客户资料，只在本机处理。"""

    company: str = ""
    year: int = 0
    operating_revenue: float = 0.0   # 营业收入
    total_profit: float = 0.0        # 利润总额
    wages: float = 0.0               # 工资薪金总额（税收口径）
    welfare: float = 0.0
    education: float = 0.0
    union: float = 0.0
    entertainment: float = 0.0
    advertising: float = 0.0
    donation: float = 0.0
    rd_expense: float = 0.0
    fines: float = 0.0
    provisions: float = 0.0
    # 人工补充项（引擎无法自动识别的调整，由你填写）
    extra_increase: float = 0.0
    extra_decrease: float = 0.0
    extra_note: str = ""

    def field(self, name: str) -> float:
        return float(getattr(self, name, 0.0) or 0.0)


@dataclass
class AdjustmentItem:
    code: str
    name: str
    book_amount: float          # 账载（会计）金额
    tax_amount: float           # 税收（可扣除）金额
    increase: float             # 调增
    decrease: float             # 调减
    carryforward: float         # 结转以后年度
    basis: str                  # 法条依据
    detail: str                 # 计算过程（人可读）
    basis_status: str = BASIS_NO_DB
    basis_evidence: str = ""


@dataclass
class CitResult:
    inputs: CitInputs
    items: list[AdjustmentItem] = field(default_factory=list)
    total_increase: float = 0.0
    total_decrease: float = 0.0
    taxable_income: float = 0.0
    tax_payable: float = 0.0
    rate: float = STANDARD_RATE
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- 依据核查

def check_basis(conn, rule: Rule) -> tuple[str, str]:
    """在本地政策库中核查规则的依据，返回 (状态, 证据说明)。"""
    if conn is None:
        return BASIS_NO_DB, ""
    try:
        for kw in rule.check_keywords:
            row = conn.execute(
                "SELECT title, p_doc_no_full, p_effect_status, p_effect_source, cwrq"
                " FROM policy WHERE title LIKE ? OR IFNULL(o_keywords,'') LIKE ?"
                " ORDER BY cwrq DESC LIMIT 1",
                (f"%{kw}%", f"%{kw}%"),
            ).fetchone()
            if row is None:
                continue
            effect = row["p_effect_status"] or "未判定"
            who = row["p_doc_no_full"] or row["title"]
            # 未判定的不能算作"有效"依据 —— 否则底稿的"依据核查"列会给出
            # 虚假的安全感（独立验证代理发现：'unknown' 是真值，会落到 BASIS_OK）
            if effect in ("", "unknown", "未知", "未判定"):
                return BASIS_UNKNOWN, f"{who}｜该条尚未做效力判定，无法确认依据现行有效性"
            ev = f"{who}｜效力：{effect}（来源 {row['p_effect_source'] or '-'}）"
            return (BASIS_STALE if effect in ("已废止", "部分失效") else BASIS_OK), ev
    except Exception:  # noqa: BLE001 - 核查失败不应影响计算
        return BASIS_NO_DB, ""
    return BASIS_UNKNOWN, "本地政策库中未检索到对应文件（不代表依据不存在）"


# ---------------------------------------------------------------- 计算

def _cap_item(rule: Rule, inputs: CitInputs) -> AdjustmentItem:
    """限额类：超出限额部分调增。"""
    book = inputs.field(rule.code)
    base_label = FIELD_LABELS.get(rule.limit_base or "", rule.limit_base or "")
    base = inputs.field(rule.limit_base or "")

    limit = base * (rule.limit_ratio or 0.0)
    detail = f"{base_label} {base:,.2f} × {rule.limit_ratio:.2%} = {limit:,.2f}"

    if rule.special_ratio is not None:
        special = book * rule.special_ratio
        detail += f"；发生额 {book:,.2f} × {rule.special_ratio:.0%} = {special:,.2f}"
        limit = min(special, limit)
        detail += f"；两者孰低 → 扣除限额 {limit:,.2f}"

    tax_amount = min(book, limit)
    increase = max(book - tax_amount, 0.0)
    carry = increase if rule.carryforward else 0.0
    if carry:
        detail += f"；超限 {increase:,.2f} 准予结转以后年度扣除"
    else:
        detail += f"；超限 {increase:,.2f} 不得扣除，且不得结转"

    return AdjustmentItem(
        code=rule.code, name=rule.name, book_amount=book, tax_amount=tax_amount,
        increase=increase, decrease=0.0, carryforward=carry,
        basis=rule.basis, detail=detail,
    )


def _disallow_item(rule: Rule, inputs: CitInputs) -> AdjustmentItem:
    """不得扣除类：全额调增。"""
    book = inputs.field(rule.code)
    return AdjustmentItem(
        code=rule.code, name=rule.name, book_amount=book, tax_amount=0.0,
        increase=book, decrease=0.0, carryforward=0.0,
        basis=rule.basis,
        detail=f"账载 {book:,.2f} 全额调增（{rule.note or '不得税前扣除'}）",
    )


def _credit_item(rule: Rule, inputs: CitInputs) -> AdjustmentItem:
    """加计扣除类：按实际发生额的一定比例**调减**。"""
    book = inputs.field(rule.code)
    extra = book * (rule.limit_ratio or 0.0)
    return AdjustmentItem(
        code=rule.code, name=rule.name, book_amount=book, tax_amount=book,
        increase=0.0, decrease=extra, carryforward=0.0,
        basis=rule.basis,
        detail=(f"实际发生额 {book:,.2f} × {rule.limit_ratio:.0%} = "
                f"加计扣除 {extra:,.2f}（在据实扣除基础上另行调减）"),
    )


_DISPATCH = {"cap": _cap_item, "disallow": _disallow_item, "credit": _credit_item}


def compute(inputs: CitInputs, *, rate: float = STANDARD_RATE, conn=None) -> CitResult:
    """计算纳税调整。``conn`` 传入时额外核查各规则依据在本地库中的状态。"""
    result = CitResult(inputs=inputs, rate=rate)

    for rule in RULES:
        handler = _DISPATCH.get(rule.kind)
        if handler is None:
            continue
        item = handler(rule, inputs)
        if item.book_amount == 0 and item.increase == 0 and item.decrease == 0:
            continue                      # 未填写的项不占版面
        item.basis_status, item.basis_evidence = check_basis(conn, rule)
        result.items.append(item)

    result.total_increase = sum(i.increase for i in result.items) + inputs.extra_increase
    result.total_decrease = sum(i.decrease for i in result.items) + inputs.extra_decrease

    if inputs.extra_increase or inputs.extra_decrease:
        result.items.append(AdjustmentItem(
            code="extra", name="人工补充调整",
            book_amount=0.0, tax_amount=0.0,
            increase=inputs.extra_increase, decrease=inputs.extra_decrease,
            carryforward=0.0,
            basis="由编制人根据实际情况填列",
            detail=inputs.extra_note or "人工补充的调整项，请在底稿中注明依据",
        ))

    result.taxable_income = (
        inputs.total_profit + result.total_increase - result.total_decrease)
    result.tax_payable = max(result.taxable_income, 0.0) * rate

    # 未考虑事项必须写出来，否则底稿会被误读为"完整申报表"
    result.warnings = [
        "未考虑弥补以前年度亏损",
        "未进行优惠资格判定（小型微利、高新技术企业等税率差异需人工确认）",
        "未考虑境外所得抵免与税额抵免（如购置环保设备投资额抵免）",
        "未自动识别全部税会差异 —— 请对照科目余额表核对是否遗漏调整项",
        f"应纳税额按 {rate:.0%} 单一税率计算；若企业适用其他税率，需调整后重算",
    ]
    return result


def to_dict(result: CitResult) -> dict:
    """转成便于序列化/测试的结构。"""
    return {
        "company": result.inputs.company,
        "year": result.inputs.year,
        "rate": result.rate,
        "total_increase": result.total_increase,
        "total_decrease": result.total_decrease,
        "total_profit": result.inputs.total_profit,
        "taxable_income": result.taxable_income,
        "tax_payable": result.tax_payable,
        "warnings": result.warnings,
        "items": [
            {
                "code": i.code, "name": i.name, "book_amount": i.book_amount,
                "tax_amount": i.tax_amount, "increase": i.increase,
                "decrease": i.decrease, "carryforward": i.carryforward,
                "basis": i.basis, "detail": i.detail,
                "basis_status": i.basis_status, "basis_evidence": i.basis_evidence,
            }
            for i in result.items
        ],
    }
