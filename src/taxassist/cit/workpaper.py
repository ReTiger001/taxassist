"""底稿导出与输入模板。

======================================================================
为什么不直接生成"申报表"
======================================================================

申报表有法定格式，且各地电子税务局口径不同。生成一张"看起来像申报表"的表，
最大的风险是让人以为可以直接报送 —— 而表格里的数字只是机械计算的初稿，
它既没有考虑亏损弥补与优惠资格，也不知道客户的行业与地方口径。

所以输出的是**工作底稿**：调整明细 + 计算过程 + 法条依据 + 未考虑事项清单。
它的用途是支撑你的判断，不是替代你的判断。

======================================================================
底稿上必须出现的四样东西
======================================================================

1. 每一行的**计算过程**，不只是结果数字
2. 法条依据，以及该依据在**本地政策库中的核查状态**（含"已废止"警示）
3. **未考虑事项清单** —— 本工具最大的风险就是被误当成完整申报
4. 输入数据与生成时间，便于复核与追溯
"""
from __future__ import annotations

from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from .engine import CitInputs, CitResult, compute
from .rules import FIELD_LABELS, RULES, rules_digest

# 输入模板的字段顺序（与 CitInputs 对应）
TEMPLATE_FIELDS: tuple[tuple[str, str], ...] = (
    ("company", "企业名称"),
    ("year", "纳税年度"),
    ("operating_revenue", "营业收入"),
    ("total_profit", "利润总额"),
    ("wages", "工资薪金总额（税收口径）"),
    ("welfare", "职工福利费发生额"),
    ("education", "职工教育经费发生额"),
    ("union", "工会经费发生额"),
    ("entertainment", "业务招待费发生额"),
    ("advertising", "广告费和业务宣传费发生额"),
    ("donation", "公益性捐赠支出"),
    ("rd_expense", "研发费用实际发生额"),
    ("fines", "税收滞纳金、罚金、罚款"),
    ("provisions", "计提的各项准备金"),
    ("extra_increase", "人工补充调增额"),
    ("extra_decrease", "人工补充调减额"),
    ("extra_note", "人工补充说明"),
)

_HEAD_FILL = PatternFill("solid", fgColor="14406B")
_HEAD_FONT = Font(color="FFFFFF", bold=True, size=11)
_TITLE_FONT = Font(bold=True, size=14)
_WARN_FONT = Font(color="9B1C22", bold=True)
_THIN = Side(style="thin", color="D0D5DA")
_BORDER = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)


def _style_header(ws, row: int, ncols: int) -> None:
    for c in range(1, ncols + 1):
        cell = ws.cell(row=row, column=c)
        cell.fill = _HEAD_FILL
        cell.font = _HEAD_FONT
        cell.alignment = Alignment(vertical="center")
        cell.border = _BORDER


def write_input_template(path: str | Path) -> Path:
    """生成输入模板（填好后再用 cit 命令生成底稿）。"""
    wb = Workbook()
    ws = wb.active
    ws.title = "输入数据"
    ws["A1"] = "企业所得税汇算清缴底稿 · 输入数据"
    ws["A1"].font = _TITLE_FONT
    ws["A2"] = "金额单位：元。只填有数据的行，未填视为 0。数据仅在本机处理。"
    ws["A2"].font = Font(color="7B8794", size=10)

    ws.append([])
    ws.append(["字段", "数值", "说明"])
    _style_header(ws, 4, 3)

    for key, label in TEMPLATE_FIELDS:
        note = ""
        rule = next((r for r in RULES if r.code == key), None)
        if rule:
            note = f"{rule.formula}｜依据：{rule.basis}"
        ws.append([label, "", note])
        if key in FIELD_LABELS:
            ws.cell(row=ws.max_row, column=1).comment = None  # 占位，保持接口简单

    ws.column_dimensions["A"].width = 32
    ws.column_dimensions["B"].width = 18
    ws.column_dimensions["C"].width = 78
    for r in range(5, ws.max_row + 1):
        ws.cell(row=r, column=3).alignment = Alignment(wrap_text=True, vertical="center")

    # 备注页：规则清单与边界
    ws2 = wb.create_sheet("规则与边界")
    ws2["A1"] = "适用规则与注意事项"
    ws2["A1"].font = _TITLE_FONT
    row = 3
    for line in rules_digest().splitlines():
        ws2.cell(row=row, column=1, value=line)
        row += 1
    row += 1
    ws2.cell(row=row, column=1, value="本工具不做的事（务必人工处理）").font = _WARN_FONT
    row += 1
    for line in (
        "· 不生成申报表，不替代申报",
        "· 不做优惠资格判定（小型微利、高新技术企业等）",
        "· 不考虑弥补以前年度亏损",
        "· 不考虑境外所得抵免与税额抵免",
        "· 不自动识别全部税会差异，需对照科目余额表核对",
    ):
        ws2.cell(row=row, column=1, value=line)
        row += 1
    ws2.column_dimensions["A"].width = 92

    path = Path(path)
    wb.save(path)
    return path


def read_inputs(path: str | Path) -> CitInputs:
    """读取填好的输入模板。"""
    wb = load_workbook(str(path), data_only=True)
    ws = wb["输入数据"] if "输入数据" in wb.sheetnames else wb.active

    label_to_key = {label: key for key, label in TEMPLATE_FIELDS}
    data: dict = {}
    for row in ws.iter_rows(min_row=1, values_only=True):
        if not row or not row[0]:
            continue
        key = label_to_key.get(str(row[0]).strip())
        if not key:
            continue
        value = row[1] if len(row) > 1 else None
        if value is None or value == "":
            continue
        data[key] = value

    for numeric_key, _label in TEMPLATE_FIELDS:
        if numeric_key in ("company", "extra_note"):
            continue
        if numeric_key in data:
            try:
                data[numeric_key] = float(data[numeric_key])
            except (TypeError, ValueError):
                # **不静默归零**：填了"100,000元"这类文本时，若当成 0，
                # compute 会因 book_amount==0 直接跳过该项 —— 底稿上完全不出现这个
                # 调整项，而表面一切正常。宁可当场报错打断，也不要交出缺项的底稿。
                label = next((lb for k, lb in TEMPLATE_FIELDS if k == numeric_key), numeric_key)
                raise ValueError(
                    f"「{label}」的值 {data[numeric_key]!r} 无法识别为数字。"
                    "请填纯数字（不带单位、千分位或全角字符）。"
                ) from None
    if "year" in data:
        try:
            data["year"] = int(float(data["year"]))
        except (TypeError, ValueError):
            data["year"] = 0
    wb.close()
    return CitInputs(**{k: v for k, v in data.items() if k in CitInputs.__dataclass_fields__})


def export_workpaper(result: CitResult, path: str | Path) -> Path:
    """把计算结果导出为底稿 Excel。"""
    wb = Workbook()

    # ---------------------------------------------------------- 调整明细
    ws = wb.active
    ws.title = "调整明细"
    ws["A1"] = (
        f"{result.inputs.company or '（未填写企业名称）'}　"
        f"{result.inputs.year or '（未填写年度）'} 年度　企业所得税纳税调整底稿"
    )
    ws["A1"].font = _TITLE_FONT
    ws["A2"] = ("本表为**计算底稿**，非申报表。请逐行复核计算过程与依据后再据以申报。")
    ws["A2"].font = _WARN_FONT

    headers = ["项目", "账载金额", "税收金额", "调增", "调减", "结转以后年度", "计算过程", "依据", "依据核查"]
    ws.append([])
    ws.append(headers)
    _style_header(ws, 4, len(headers))

    money_fmt = "#,##0.00"
    for item in result.items:
        ws.append([
            item.name, item.book_amount, item.tax_amount, item.increase,
            item.decrease, item.carryforward, item.detail, item.basis,
            item.basis_status,
        ])
        r = ws.max_row
        for c in (2, 3, 4, 5, 6):
            ws.cell(row=r, column=c).number_format = money_fmt
        if item.basis_status.startswith("库中已收录·已废止"):
            ws.cell(row=r, column=9).font = _WARN_FONT

    ws.append(["合计", None, None, result.total_increase, result.total_decrease, None, None, None, None])
    r = ws.max_row
    for c in (4, 5):
        ws.cell(row=r, column=c).font = Font(bold=True)
        ws.cell(row=r, column=c).number_format = money_fmt

    widths = {"A": 30, "B": 15, "C": 15, "D": 15, "E": 15, "F": 15, "G": 62, "H": 46, "I": 22}
    for col, w in widths.items():
        ws.column_dimensions[col].width = w
    for rr in range(5, ws.max_row + 1):
        ws.cell(row=rr, column=7).alignment = Alignment(wrap_text=True, vertical="top")
        ws.cell(row=rr, column=8).alignment = Alignment(wrap_text=True, vertical="top")

    # ---------------------------------------------------------- 汇总
    s = wb.create_sheet("汇总")
    s["A1"] = "纳税调整汇总"
    s["A1"].font = _TITLE_FONT
    rows = [
        ("利润总额", result.inputs.total_profit),
        ("加：纳税调整增加额", result.total_increase),
        ("减：纳税调整减少额", result.total_decrease),
        ("纳税调整后所得", result.taxable_income),
        (f"税率", f"{result.rate:.0%}"),
        ("应纳税额（按单一税率计算）", result.tax_payable),
    ]
    s.append([])
    for label, value in rows:
        s.append([label, value])
        rr = s.max_row
        if isinstance(value, float):
            s.cell(row=rr, column=2).number_format = money_fmt
        if label.startswith(("纳税调整后所得", "应纳税额")):
            s.cell(row=rr, column=1).font = Font(bold=True)
            s.cell(row=rr, column=2).font = Font(bold=True)
    s.column_dimensions["A"].width = 34
    s.column_dimensions["B"].width = 20

    # ---------------------------------------------------------- 输入数据
    d = wb.create_sheet("输入数据")
    d["A1"] = "本次计算所用的输入数据（便于复核与追溯）"
    d["A1"].font = _TITLE_FONT
    d.append([])
    d.append(["字段", "数值"])
    _style_header(d, 3, 2)
    for key, label in TEMPLATE_FIELDS:
        value = getattr(result.inputs, key, "")
        if value in ("", 0.0, 0, None):
            continue
        d.append([label, value])
        if isinstance(value, float):
            d.cell(row=d.max_row, column=2).number_format = money_fmt
    d.column_dimensions["A"].width = 34
    d.column_dimensions["B"].width = 24

    # ---------------------------------------------------------- 未考虑事项
    w = wb.create_sheet("未考虑事项")
    w["A1"] = "⚠ 本底稿未考虑的事项（必须人工处理）"
    w["A1"].font = _WARN_FONT
    w.append([])
    for line in result.warnings:
        w.append([f"· {line}"])
    w.append([])
    w.append(["规则数值可能随政策变动，首次使用前请核对现行规定："])
    for line in rules_digest().splitlines()[1:]:
        w.append([line])
    w.column_dimensions["A"].width = 96

    path = Path(path)
    wb.save(path)
    return path


def run_from_template(input_path: str | Path, output_path: str | Path, *, conn=None) -> CitResult:
    """从输入模板生成底稿的一站式入口。"""
    inputs = read_inputs(input_path)
    result = compute(inputs, conn=conn)
    export_workpaper(result, output_path)
    return result
