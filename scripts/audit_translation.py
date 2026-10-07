"""翻译质量体检：把「错在哪、错多少」变成能数的数。

======================================================================
为什么需要它
======================================================================

``translate_llm.translate()`` 除了「输出非空」之外**没有任何校验**，译文直接
入库。所以「已译的几万条里有多少有问题」这件事此前无从回答 —— 只能靠偶遇。
机构名那一类就是这样撞见的：复核时发现标题里有 「State Taxation
Administration **and** the Jiangsu…」这种并列写法，一查才知道 13945 条标题里
有 546 条（3.9%）是错的。**一类错误能撞见，别类错误也能** —— 问题是撞见
多少算多少，还是主动全量扫一遍。

本脚本做后者：对已入库的全部译文跑**机器可判定的硬检查**（不评判文风、不谈
信达雅），每类给出命中数与样例。**只读库，一个字都不写。**

======================================================================
检查项与「误报」的取舍
======================================================================

A 类 —— 命中即错，不需要人工判断：
  A1 残留汉字    译文里还有中文字符 → 漏译
  A2 译文过短    英文/中文 字符数之比低于阈值 → 漏译或截断
  A3 疑似截断    结尾不是句末标点（num_predict 用尽会这样断）
  A4 数字缺失    原文里的阿拉伯数字在译文里找不到（**只报 3 位以上**：
                 2024、1000、365 这种几乎不可能被写成英文数词；长度 2 的
                 会被写成 fifteen，报出来全是误报，故分档列出）
  A5 重复片段    同一句话在译文里出现多次 → 模型复读
  A6 空 / 极短

B 类 —— 复用已跑通的规则，含中文门槛：
  B1 机构名误拆  与 scripts/fix_org_names.py 同一套规则。这里报的是
                 **修完之后仍剩下的**（含那道收敛保护主动跳过的）。

C 类 —— 完整性：
  C1 指纹不符    译文的 src_hash 与当前原文对不上 → 缓存早该失效

用法：
    python scripts/audit_translation.py                 # 标题 + 正文，全部
    python scripts/audit_translation.py --field title    # 只看标题
    python scripts/audit_translation.py --samples 5      # 每类多打印几条样例

报告另存 data/logs/audit_translation.json（供后续比对、跟踪修复进度）。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from taxassist import db as dbmod  # noqa: E402
from taxassist import translate_llm as tl  # noqa: E402

try:
    # 复用修机构名那套规则（含 ZH_ONE 中文门槛与收敛保护），不另写一份 ——
    # 两处规则一旦分叉，修的和查的就会各说各话。
    import fix_org_names as fog  # noqa: E402
except Exception:  # noqa: BLE001
    fog = None

HAN = re.compile(r"[\u4e00-\u9fff]")
# 句末标点：英文与全角都算。列表项、引号结尾也算正常收尾。
TAIL_OK = set(".!?。！？…\"'”’)]}）】:;")
NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")
# 数字 + 紧跟的中文数量单位。单位是**必须**一起看的，理由见 missing_numbers。
NUM_UNIT = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(万元|亿元|万|亿)?")
SCALE_WORD = re.compile(r"\b(million|billion|thousand)\b", re.I)


def _clean(d) -> str:
    s = format(d.normalize(), "f")
    return s.rstrip("0").rstrip(".") if "." in s else s


def norm_nums(text: str) -> set[str]:
    """抽出文本里的阿拉伯数字，归一化后成集合。

    去掉千分位逗号 —— 原文写 1,000、译文写 1000 是同一件事，不该报警；
    再去掉尾部的小数点（"第 41 条." 这种断句残留）。
    """
    out = set()
    for m in NUM.finditer(text or ""):
        v = m.group(0).replace(",", "").rstrip(".")
        if not v:
            continue
        out.add(v)
        # 抓取噪音：原文里「1.2016年4月30日」实际是列表序号「1.」+「2016年」粘在一起。
        # 译文正常写 2016，第一版却因为原文 token 是 "1.2016" 而报缺失。
        if re.fullmatch(r"\d{1,2}\.\d{4,}", v):
            out.add(v.split(".", 1)[1])
    return out


def _with_unit_variants(nstr: str, unit: str) -> set[str]:
    """把一个带中文单位的数字展开成「等价写法」集合。

    **不做这一步，这个指标就没法用**：中文写「6000万元」，规范的英文写法是
    "60 million yuan" —— 两个串里没有一个共同数字。第一版检查把这类**完全
    正确**的翻译全报成「数字缺失」。
    """
    from decimal import Decimal, InvalidOperation

    out = {nstr}
    try:
        n = Decimal(nstr.replace(",", ""))
    except InvalidOperation:
        return out
    if unit in ("万元", "万"):
        yuan = n * 10_000
    elif unit in ("亿元", "亿"):
        yuan = n * 100_000_000
    else:
        return out
    out.add(_clean(yuan))                          # 写全（15,000 元）
    out.add(_clean(yuan / Decimal(1_000_000)))     # million
    out.add(_clean(yuan / Decimal(1_000_000_000)))  # billion
    return {x for x in out if x and x != "0"}


def missing_numbers(zh: str, en: str) -> tuple[list[str], list[str]]:
    """原文有、译文没有的数字。返回 (高置信, 待查)。

    高置信 = 3 位以上或带数量单位的数字；待查 = 2 位数字（可能被写成
    fifteen 这类英文数词，属正常译法，故分档）。
    """
    en_nums = norm_nums(en)
    strong, weak = [], []
    for m in NUM_UNIT.finditer(zh or ""):
        n = m.group(1).replace(",", "").rstrip(".")
        if not n:
            continue
        unit = m.group(2) or ""
        cands = _with_unit_variants(n, unit)
        if any(c in en_nums or any(c in e for e in en_nums) for c in cands):
            continue
        # 换算过的大数只有在译文里真有 million/billion 时才认（否则 "6000万元"
        # 的候选里那个 "60" 会跟译文里随便一个 60 撞上，白白放过一条真错）。
        if SCALE_WORD.search(en) and any(
                c in en_nums for c in cands if len(c) <= 3):
            continue
        (strong if (len(n) >= 3 or unit) else weak).append(n + unit)
    return strong, weak


# ---- 结构丢失：模型把长清单「概括」掉（最危险的一类，译文读起来通顺完整）
ITEM_ZH = re.compile(
    r"(?:^|\n)\s*(?:[（(]\s*[一二三四五六七八九十]{1,3}\s*[)）]"
    r"|\d{1,3}\s*[.、．]|第[一二三四五六七八九十百]{1,4}条)")
ITEM_EN = re.compile(
    r"(?:^|\n)\s*(?:\d{1,3}\s*[.、)]|Article\s+\d+|\(\d{1,3}\)|\([a-z]\))")


def lost_items(zh: str, en: str) -> tuple[int, int] | None:
    """原文条目数 vs 译文条目数。差距悬殊 = 内容被概括掉了。

    为什么单列一项：字数比（A2）只能抓住「整篇被压缩」。**局部被概括**时
    总字数没异常，但条目数会露馅 —— 26108 字的《西部地区鼓励类产业目录》
    译成 3216 字是一种，一个 300 条的中段被压成一句话也是一种。
    """
    zi = len(ITEM_ZH.findall(zh or ""))
    ei = len(ITEM_EN.findall(en or ""))
    if zi >= 8 and ei < zi * 0.5:
        return zi, ei
    return None


def repeated_sentences(en: str, min_len: int = 40) -> list[str]:
    """**紧邻**重复的句子 —— 模型复读的特征。

    注意「紧邻」这个限定：同一句话在文件不同位置各出现一次是**原文就有的**
    （中文政策常在多个条款里重复引用「后续事项按第 X 条处理」）。第一版没
    限定紧邻，报出 455 条，逐条看全是原文本来就重复的，**假阳性 100%**。
    """
    parts = [s.strip() for s in re.split(r"(?<=[.!?])\s+", en or "")]
    out = []
    for a, b in zip(parts, parts[1:], strict=False):
        if len(a) >= min_len and a == b:
            out.append(a)
    return out


def audit(field: str, samples: int, connect=dbmod.connect) -> dict:
    """跑一个字段的体检。返回统计 + 每类样例。"""
    conn = connect()
    try:
        zh_col = "p.title" if field == "title" else "p.content"
        rows = conn.execute(
            f"SELECT t.id, t.doc_uid, t.text AS en, t.src_hash, {zh_col} AS zh"
            f" FROM translation t LEFT JOIN policy p ON p.doc_uid = t.doc_uid"
            f" WHERE t.field=? AND t.text IS NOT NULL", (field,)).fetchall()
    finally:
        conn.close()

    hits: dict[str, list] = {k: [] for k in
                             ("A1", "A2", "A3", "A4", "A5", "A6", "A7", "B1", "C1")}
    n = len(rows)
    for r in rows:
        en, zh = r["en"] or "", r["zh"] or ""
        rec = {"id": r["id"], "uid": r["doc_uid"]}

        if not en.strip():
            hits["A6"].append({**rec, "detail": "译文为空"})
            continue
        if len(en.strip()) < 10:
            hits["A6"].append({**rec, "detail": en[:60]})

        han = HAN.findall(en)
        if han:
            hits["A1"].append({**rec, "n_han": len(han),
                               "detail": "".join(han[:12])})

        if zh:
            ratio = len(en) / max(len(zh), 1)
            if ratio < 0.4:
                hits["A2"].append({**rec, "ratio": round(ratio, 3),
                                   "zh_len": len(zh), "en_len": len(en)})

            strong, weak = missing_numbers(zh, en)
            if strong:
                hits["A4"].append({**rec, "missing": strong[:8]})

            li = lost_items(zh, en)
            if li:
                hits["A7"].append({**rec, "zh_items": li[0], "en_items": li[1]})

        # A3 **只对正文有意义**。第一版把标题也扫进来，13945 条命中 9762 条
        # （70%）—— 全是误报：政策标题本来就不带句号（「…Tax Rates for
        # Vehicle and Vessel Taxes」是正确的标题）。一个误报 70% 的指标比没有
        # 指标更糟，它会掩盖真问题。
        if field == "content" and en.rstrip()[-1] not in TAIL_OK:
            hits["A3"].append({**rec, "tail": en.rstrip()[-50:]})

        rep = repeated_sentences(en)
        if rep:
            hits["A5"].append({**rec, "n": len(rep), "detail": rep[0][:60]})

        # B1：修完仍剩下的机构名误拆（规则同 fix_org_names，含中文门槛）
        if fog is not None and fog.ZH_ONE.search(zh) and fog.PAT.search(en):
            hits["B1"].append({**rec, "detail": fog.PAT.search(en).group(0)[:80]})

        # C1：原文变过但译文没重译 —— 指纹对不上
        if zh and r["src_hash"] != tl._hash(zh):
            hits["C1"].append({**rec, "note": "src_hash 与当前原文不符"})

    return {"field": field, "total": n, "hits": hits}


LABELS = {
    "A1": "残留汉字（漏译）",
    "A2": "译文过短（字符比<0.4）",
    "A3": "疑似截断（结尾非句末标点）",
    "A4": "原文数字缺失（3位以上，高置信）",
    "A5": "重复片段（复读）",
    "A6": "空 / 极短译文",
    "A7": "结构丢失（条目数远少于原文）",
    "B1": "机构名并列误拆（修后仍剩）",
    "C1": "指纹不符（原文已变未重译）",
}


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--field", choices=("title", "content", "both"), default="both")
    ap.add_argument("--samples", type=int, default=3)
    ap.add_argument("--json", default="data/logs/audit_translation.json")
    args = ap.parse_args()

    fields = ["title", "content"] if args.field == "both" else [args.field]
    report = {}
    for f in fields:
        res = audit(f, args.samples)
        report[f] = res
        n = res["total"]
        print(f"\n{'='*66}\n[{f}] 已译 {n} 条\n{'='*66}")
        for k, label in LABELS.items():
            lst = res["hits"][k]
            pct = f"{len(lst)/n*100:.2f}%" if n else "-"
            flag = "" if not lst else "  ←"
            print(f"  {k} {label:<32} {len(lst):>6} 条  {pct:>7}{flag}")
            for s in lst[:args.samples]:
                extra = {kk: vv for kk, vv in s.items() if kk not in ("id", "uid")}
                print(f"       · {s['uid']}  {extra}")

    out = Path(args.json)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    print(f"\n报告已写入 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
