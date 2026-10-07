"""术语一致性体检：同一个中文术语被译成了几种英文写法。

======================================================================
为什么单列一项
======================================================================

看一条译文，术语怎么写都通顺；把 6290 条摆在一起才看得出来「增值税」有的写
``Value-Added Tax``、有的写 ``VAT``、有的写 ``Added Value Tax``。对税务文件
而言，**术语不一致比个别错译更伤专业性** —— 而它恰恰是逐条审读最难发现的
那一类：每一条单看都没问题。

也不能靠人读：6290 条 × 20 个术语 = 12 万次比对，人做不了。机器做是一瞬间。

======================================================================
只统计，不判对错
======================================================================

哪个写法是官方译法**由使用者定**（他才是这一行的专业人士）。这里只把分布
摆出来 —— 分布本身就是问题清单：某个术语出现三种以上写法、或出现明显不合
规范的写法（如 ``Added Value Tax``、旧译名 ``State Administration of
Taxation``），就该定了。

一处刻意的设计：**同一条译文里同时出现全称与缩写不算问题** —— 首次写全称、
其后用缩写是规范做法。所以看到 ``VAT`` 的高占比不必紧张，要看的是有没有
**互相冲突的全称写法**。

用法：
    python scripts/audit_terms.py
    python scripts/audit_terms.py --field title      # 标题
    python scripts/audit_terms.py --min 20           # 只看样本量 ≥20 的术语
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from taxassist import db as dbmod  # noqa: E402

#: 中文术语 → 它的各种英文写法（含已知的不规范写法与旧译名）。
#: **候选要写全**：漏掉一个写法，就看不见它占了多少。
TERMS: dict[str, list[str]] = {
    "增值税": ["Value-Added Tax", "Value Added Tax", "Added Value Tax",
               "Value-added tax", "VAT"],
    "企业所得税": ["Enterprise Income Tax", "Corporate Income Tax",
                   "Enterprise Income Tax Law", "corporate income tax"],
    "个人所得税": ["Individual Income Tax", "Personal Income Tax"],
    "消费税": ["Consumption Tax", "Excise Tax"],
    "土地增值税": ["Land Appreciation Tax", "Land Value-Added Tax"],
    "印花税": ["Stamp Tax", "Stamp Duty"],
    "房产税": ["Property Tax", "Real Estate Tax", "House Tax"],
    "契税": ["Deed Tax", "Contract Tax"],
    "关税": ["Customs Duty", "Tariff", "Customs Tariff"],
    "城镇土地使用税": ["Urban Land Use Tax", "Urban Land Use Tax"],
    "纳税人": ["Taxpayer", "Tax Payer", "tax payer"],
    "扣缴义务人": ["Withholding Agent", "Withholding agent", "withholding agent"],
    "一般纳税人": ["General Taxpayer", "General VAT Taxpayer"],
    "小规模纳税人": ["Small-Scale Taxpayer", "Small-scale taxpayer",
                     "Small Scale Taxpayer"],
    "进项税额": ["Input Tax", "Input VAT", "input value-added tax"],
    "销项税额": ["Output Tax", "Output VAT"],
    "应纳税所得额": ["Taxable Income", "taxable income", "Amount of Taxable Income"],
    "国家税务总局": ["State Taxation Administration",
                     "State Administration of Taxation",
                     "State Tax Administration"],
    "财政部": ["Ministry of Finance"],
    "主管税务机关": ["Competent Tax Authority", "competent tax authority",
                     "Competent Tax Authorities", "competent tax authorities"],
    "征收管理": ["Collection and Administration", "collection and administration",
                 "Tax Collection and Administration"],
    "免税": ["Tax Exemption", "Exempt from Tax", "tax-exempt"],
    "减免税": ["Tax Reduction and Exemption", "Tax Relief",
               "Reduction and Exemption of Tax"],
    "施行": ["Come into Force", "Take Effect", "become effective"],
    "废止": ["Repeal", "Abolish", "Annul"],
}


def scan(rows, term: str, variants: list[str]):
    """含该词的译文条数、各写法的命中条数、以及**至少命中一种写法的覆盖率**。

    **候选先按大小写归一**：``Value-Added Tax`` 与 ``Value-added tax`` 是同一个
    写法的大小写差异，各报一遍只会虚增「写法种类数」，让人以为术语比实际更乱。
    覆盖率则用来判断**候选列表本身够不够** —— 覆盖率低说明绝大多数译文用了
    我没列到的写法，此时「命中了 3 种」是假象，得先补候选。
    """
    sub = [en for zh, en in rows if term in zh]
    seen, uniq = set(), []
    for v in variants:
        if v.lower() not in seen:
            seen.add(v.lower())
            uniq.append(v)
    pats = [(v, re.compile(re.escape(v), re.I)) for v in uniq]
    out = [(v, sum(1 for en in sub if p.search(en))) for v, p in pats]
    covered = sum(1 for en in sub if any(p.search(en) for _v, p in pats))
    return len(sub), out, covered


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--field", choices=("title", "content"), default="content")
    ap.add_argument("--min", type=int, default=5,
                    help="样本量低于此值的术语跳过（样本太少，比例不可信）")
    args = ap.parse_args()

    conn = dbmod.connect()
    zh_col = "p.title" if args.field == "title" else "p.content"
    rows = [(r["zh"] or "", r["en"] or "") for r in conn.execute(
        f"SELECT {zh_col} AS zh, t.text AS en FROM translation t"
        f" LEFT JOIN policy p ON p.doc_uid=t.doc_uid"
        f" WHERE t.field=? AND t.text IS NOT NULL", (args.field,))]
    conn.close()
    print(f"[{args.field}] 译文 {len(rows)} 条，逐术语统计（大小写不敏感）\n")

    flagged = []
    for term, variants in TERMS.items():
        n, counts, covered = scan(rows, term, variants)
        if n < args.min:
            continue
        shown = [(v, c) for v, c in counts if c]
        rate = covered / n * 100 if n else 0
        if not shown:
            print(f"{term}  —— 含该词的译文 {n} 条，**候选写法一条都没命中**（要人工看）\n")
            flagged.append(term)
            continue
        shown.sort(key=lambda x: -x[1])
        kinds = len(shown)
        warn = "  ← 候选不全，先补写法" if rate < 50 else ""
        mark = "  ← 需定稿" if kinds >= 3 and rate >= 50 else ""
        print(f"{term}  —— 含该词的译文 {n} 条，命中 {kinds} 种写法，"
              f"覆盖 {rate:.0f}%{warn}{mark}")
        for v, c in shown:
            bar = "█" * max(1, round(c / n * 20))
            print(f"    {v:<34} {c:>6} 条 {c/n*100:5.1f}%  {bar}")
        print()
        if kinds >= 3 and rate >= 50:
            flagged.append(term)

    print("=" * 70)
    print(f"写法 ≥3 种、需要定稿的术语：{', '.join(flagged) if flagged else '无'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
