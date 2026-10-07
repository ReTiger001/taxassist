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
#: HTML 实体在原文里是**字面**存在的（采集后没解码干净）。抽数字前必须剥掉，
#: 否则 ``&#8203;`` 会被当成数字 8203 —— 实测踩到：一批「数字缺失」报警
#: 全部来自它，而译文里当然不可能有这个数。（同类：&nbsp; &ldquo; &#39;）
ENTITY = re.compile(r"&[#A-Za-z0-9]+;")
# 数字 + 紧跟的中文数量单位。单位是**必须**一起看的，理由见 missing_numbers。
# 「多/余」夹在数字与单位之间是中文的常规写法（"140多万元"），必须一并吃掉 ——
# 否则单位识别不到，换算变体就生成不出来，规范译文 "1.4 million yuan" 会被
# 误判成数字缺失（实测踩到过）。
NUM_UNIT = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(?:多|余)?\s*(万元|亿元|万|亿)?")
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
    for m in NUM.finditer(ENTITY.sub(" ", text or "")):
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
    for m in NUM_UNIT.finditer(ENTITY.sub(" ", zh or "")):
        n = m.group(1).replace(",", "").rstrip(".")
        if not n:
            continue
        # 原文里的「2.1987年6月1日」实际是列表序号「2.」+「1987年6月1日」粘在
        # 一起（抓下来的文本没保留空白）。只检查后半段 —— 否则译文老老实实写了
        # 1987，也会被判成「1987 缺失」。
        if re.fullmatch(r"\d{1,2}\.\d{4}", n):
            n = n.split(".", 1)[1]
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


def number_drop(zh: str, en: str, min_nums: int = 8,
                ratio: float = 0.75) -> tuple[int, int] | None:
    """原文与译文的数字**个数**对比 —— 抓「成批数字消失」。

    为什么不靠逐项比对（A4 的做法）：中文数量单位换算会让规范译文里根本找不到
    原文那个数（「6000万元」→ "60 million yuan"），逐项比对必然出假阳性。
    而**个数**不受换算影响 —— 换算只是换个写法，数还是那些数。个数骤降才真正
    指向「内容被成段省略或概括」。

    阈值是拍的（少于 8 个数字的文档不判，降幅不到 25% 不判）：样本太少时个数
    波动本来就大。**宁可漏报不可误报** —— 误报会淹没真问题。
    """
    zn, en_n = norm_nums(zh), norm_nums(en)
    if len(zn) < min_nums:
        return None
    if len(en_n) < len(zn) * ratio:
        return len(zn), len(en_n)
    return None


#: 译文里的「模型自述」—— 它没译完，而是加了一句说明。实测抓到的原话：
#:   「以下是上述文本的中文翻译：」
#:   「## 注意：由于文本内容较长，以上翻译仅涵盖了部分内容。如需完整翻译，请提供全部文本。」
#: 这类污染**比错译更危险**：模型的元评论被当成政策正文交给了读者。
#:
#: **第一版写得太宽，报了 320 条（5.09%），抽样发现大半是假阳性**：`I can`、
#: `I will`、`The entire text` 这些在政策译文里完全正常（原文是第一人称、
#: 或网页上有 "Read the Entire Text" 按钮）。收紧到只认**明确的元评论句式** ——
#: 「以下是…翻译」「由于…较长」「如需完整」「仅提供了部分」这类。
META_TALK = re.compile(
    r"以下是.{0,16}(翻译|文本|内容)|以下为.{0,12}(翻译|译文)|"
    r"由于.{0,12}(较长|过长|篇幅)|如需(完整|全文|获取)|"
    r"仅(提供|涵盖|翻译)了?(部分|章节)|以上(翻译|内容)仅|"
    r"(?:please|kindly) provide|"
    r"only (?:part|a portion) of (?:the|this)|"
    r"(?:full|complete|entire) (?:text|content) (?:is|was) (?:too long|not provided)|"
    r"未能(提供|完成|翻译)|翻译(如下|结果)[:：]",
    re.I)


def has_meta_talk(en: str) -> str:
    m = META_TALK.search(en or "")
    return m.group(0)[:50] if m else ""


def looks_truncated(en: str) -> bool:
    """是不是「话没说完」。**不看结尾有没有句号**。

    第一版按「结尾不是句末标点」判，正文命中 4458 条（70.87%）—— 全错：政策
    正文的最后一段是落款（发文机关 + 日期），公文惯例本就不带句号，标题更是
    从来不带。真正该抓的是「话说到一半」：以逗号收尾，或以连词/介词收尾 ——
    num_predict 用尽时正是这样断的。
    """
    t = (en or "").rstrip()
    if not t:
        return False
    if re.search(r"[,;:，、；：]$", t):
        return True
    return bool(re.search(
        r"\b(and|or|the|of|in|to|with|for|by|as|that|which|shall)\s*$", t, re.I))


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
                             ("A1", "A2", "A3", "A4", "A5", "A6", "A7", "A8",
                              "A9", "B1", "C1")}
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

            nd = number_drop(zh, en)
            if nd:
                hits["A8"].append({**rec, "zh_nums": nd[0], "en_nums": nd[1]})

        # A3 **只对正文有意义**（标题从来不带句号），且只看「话说到一半」——
        # 不看结尾有没有句号，落款按公文惯例本就不带。详见 looks_truncated。
        if field == "content" and looks_truncated(en):
            hits["A3"].append({**rec, "tail": en.rstrip()[-50:]})

        rep = repeated_sentences(en)
        if rep:
            hits["A5"].append({**rec, "n": len(rep), "detail": rep[0][:60]})

        meta = has_meta_talk(en)
        if meta:
            hits["A9"].append({**rec, "detail": meta})

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
    "A8": "数字成批消失（个数骤降）",
    "A9": "译文混入模型自述（没译完就说明）",
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

        # **并集**：各检查项会重叠 —— 结构丢失的条目往往同时也「数字成批消失」、
        # 也「译文过短」。把各类相加会严重高估问题规模，而「到底多少条有问题」
        # 这个数只能去重后得到。
        by_uid: dict[str, set] = {}
        for k, lst in res["hits"].items():
            for h in lst:
                by_uid.setdefault(h["uid"], set()).add(k)
        n_bad = len(by_uid)
        print(f"\n  去重后：{n_bad} 条至少命中一项"
              f"（{n_bad / n * 100:.2f}% of {n}）" if n else "  无数据")

    out = Path(args.json)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    print(f"\n报告已写入 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
