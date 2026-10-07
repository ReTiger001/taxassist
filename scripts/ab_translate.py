"""A/B 实测：同一批文本跑多组翻译参数，用数据决定用哪一组。

======================================================================
为什么必须实测
======================================================================

法律文本翻译该用多少温度、提示词里要不要塞术语约束 —— 这两件事都能讲出
道理来支持相反的结论。而正文有 13946 条，参数选错就是整体返工。所以把
「我觉得」换成「我测过」：同一批文本、多组参数、同一套**已被验伪的**检查项。

两组样本，缺一不可：
  · 正例 —— 机检已命中的条目（残留汉字 / 数字缺失 / 结构丢失 / 严重压缩）
  · 负例 —— 随机抽的干净条目
只用正例会得出「改什么都比现状好」；只用负例则什么都测不出来。

评测复用 ``scripts/audit_translation.py`` 的检查函数：它们是逐条人工验伪过
的（A3 误报 70%、A5 假阳性 100% 都在那里被纠正），不另写一份 —— 两把尺子
会量出两个互相矛盾的结论。

**不写库**：译文只落 ``data/logs/ab_translate_<field>.json``。

======================================================================
自变量：一次只动一个，看清楚是谁在起作用
======================================================================

    B0  官方模板 + 官方采样(0.7)      ← 现状基线
    B1  官方模板 + temperature 0.1    ← 单独看温度
    B2  加约束提示词 + 官方采样(0.7)   ← 单独看提示词
    B3  加约束提示词 + temperature 0.1 ← 组合

B0→B1 是温度的效果，B0→B2 是提示词的效果。跳开这层对照，看到「B3 最好」
也不知道该归功于谁。

用法：
    python scripts/ab_translate.py --field title --n 24      # 标题（秒级）
    python scripts/ab_translate.py --field content --n 10    # 正文（慢，分钟级）
    python scripts/ab_translate.py --field title --only B0,B1
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from audit_translation import HAN, lost_items, missing_numbers  # noqa: E402

from taxassist import db as dbmod  # noqa: E402
from taxassist import translate_llm as tl  # noqa: E402

#: 约束版提示词。**骨架保持官方那句不变**（模型是靠它进入翻译模式的），
#: 只在其后追约束，且约束用中文 —— 模型卡里的示例指令就是中文。
P1 = ("把下面的文本翻译成英文，不要额外解释。\n"
      "要求：逐段完整翻译，不得概括、省略、合并或缩减任何内容；"
      "数字、文号、日期必须与原文一一对应；"
      "机构名称是一个完整的名称，不得拆分成两个或多个机构。\n\n"
      "{text}")

CONFIGS: dict[str, dict] = {
    "B0-官方模板-t0.7": dict(prompt=None, sampling=None),
    "B1-官方模板-t0.1": dict(sampling={"temperature": 0.1}),
    "B2-加约束-t0.7": dict(prompt=P1),
    "B3-加约束-t0.1": dict(prompt=P1, sampling={"temperature": 0.1}),
}

#: 正例来源的类别，按危险度排序（结构丢失/漏译最危险，优先取样）
POS_ORDER = ("A7", "A1", "A2", "A4", "B1")


def pick_samples(conn, field: str, n: int, seed: int = 20261007):
    """正例与负例各半。正例按类别轮转，保证不被某一类刷屏。"""
    rng = random.Random(seed)
    rep_path = Path("data/logs/audit_translation.json")
    rep = json.loads(rep_path.read_text(encoding="utf-8")).get(field, {})
    hits = rep.get("hits", {})

    pools = {k: [h["id"] for h in hits.get(k, [])] for k in POS_ORDER}
    for v in pools.values():
        rng.shuffle(v)

    n_pos = n // 2
    picked: list[dict] = []
    depth = max((len(v) for v in pools.values()), default=0)
    for rnd in range(depth):
        for k in POS_ORDER:
            if rnd < len(pools[k]):
                picked.append({"id": pools[k][rnd], "why": k})
                if len(picked) >= n_pos:
                    break
        if len(picked) >= n_pos:
            break

    # 负例：从**任何一项检查都没命中**的条目里随机抽
    hit_ids = {h["id"] for k, v in hits.items() for h in v}
    clean = [r["id"] for r in conn.execute(
        "SELECT id FROM translation WHERE field=? AND text IS NOT NULL", (field,))
        if r["id"] not in hit_ids]
    rng.shuffle(clean)
    picked += [{"id": i, "why": "干净"} for i in clean[:n - len(picked)]]

    rows = []
    zh_col = "p.title" if field == "title" else "p.content"
    for s in picked:
        r = conn.execute(
            f"SELECT t.id, t.doc_uid, t.text AS old_en, {zh_col} AS zh"
            f" FROM translation t LEFT JOIN policy p ON p.doc_uid=t.doc_uid"
            f" WHERE t.id=?", (s["id"],)).fetchone()
        if r and (r["zh"] or "").strip():
            rows.append({**s, "uid": r["doc_uid"], "zh": r["zh"],
                         "old_en": r["old_en"] or ""})
    return rows


def evaluate(zh: str, en: str, field: str) -> dict:
    """跑一套检查，返回可对比的数。**不判对错，只报事实。**"""
    strong, _weak = missing_numbers(zh, en)
    out = {"han": len(HAN.findall(en)), "missing": strong[:6],
           "ratio": round(len(en) / max(len(zh), 1), 3)}
    if field == "content":
        out["items"] = lost_items(zh, en)
    return out


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--field", choices=("title", "content"), default="title")
    ap.add_argument("--n", type=int, default=24, help="样本条数（正负各半）")
    ap.add_argument("--only", default="", help="只跑指定配置，逗号分隔，如 B0,B1")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    ready, why = tl.is_available(tl.DEFAULT_MODEL)
    print(f"模型状态：{why}")
    if not ready:
        return 1

    conn = dbmod.connect()
    samples = pick_samples(conn, args.field, args.n)
    conn.close()
    print(f"样本 {len(samples)} 条（"
          f"正例 {sum(1 for s in samples if s['why'] != '干净')} / "
          f"负例 {sum(1 for s in samples if s['why'] == '干净')}）")

    cfgs = {k: v for k, v in CONFIGS.items()
            if not args.only or k.split("-")[0] in args.only.split(",")}
    print(f"配置 {len(cfgs)} 组 × {len(samples)} 条 = {len(cfgs)*len(samples)} 次翻译\n")

    results = []
    t_all = time.time()
    for i, s in enumerate(samples, 1):
        rec = {"id": s["id"], "uid": s["uid"], "why": s["why"],
               "zh_len": len(s["zh"]), "zh_head": s["zh"][:100],
               "old": evaluate(s["zh"], s["old_en"], args.field), "runs": {}}
        for name, cfg in cfgs.items():
            t0 = time.time()
            try:
                if args.field == "title":
                    en = tl.translate(s["zh"], prompt=cfg.get("prompt"),
                                      sampling=cfg.get("sampling"))
                else:
                    en = tl.translate_long(s["zh"], prompt=cfg.get("prompt"),
                                           sampling=cfg.get("sampling"))
                rec["runs"][name] = {**evaluate(s["zh"], en, args.field),
                                     "sec": round(time.time() - t0, 1),
                                     "en": en}
            except Exception as e:  # noqa: BLE001
                rec["runs"][name] = {"error": f"{type(e).__name__}: {e}",
                                     "sec": round(time.time() - t0, 1)}
        results.append(rec)
        old = rec["old"]
        line = (f"[{i}/{len(samples)}] {rec['why']:<4} {rec['uid'][:26]:<26}"
                f" 现状:汉字{old['han']} 缺数{len(old['missing'])} 比{old['ratio']}")
        print(line + f"\n    ZH {rec['zh_head'][:80]}")
        for name in cfgs:
            r = rec["runs"][name]
            if "error" in r:
                print(f"    {name:<18} 失败 {r['error'][:60]}")
            else:
                print(f"    {name:<18} 汉字{r['han']} 缺数{len(r['missing'])}"
                      f" 比{r['ratio']} {r['sec']}s"
                      + (f" 条目{r['items']}" if r.get("items") else "")
                      + f"  {r['en'][:70]}")

    # ---- 汇总
    print("\n" + "=" * 78)
    print(f"汇总（{args.field}，{len(samples)} 条，用时 {(time.time()-t_all)/60:.1f} 分钟）")
    print("=" * 78)
    print(f"{'配置':<20}{'残留汉字':>9}{'数字缺失':>9}{'平均长度比':>11}{'异常条数':>9}")
    print(f"{'现状(库内译文)':<20}{sum(r['old']['han'] for r in results):>9}"
          f"{sum(len(r['old']['missing']) for r in results):>9}"
          f"{sum(r['old']['ratio'] for r in results)/len(results):>11.3f}"
          f"{'—':>9}")
    for name in cfgs:
        ok = [r["runs"][name] for r in results if "error" not in r["runs"][name]]
        if not ok:
            print(f"{name:<20}{'全部失败':>9}")
            continue
        bad = sum(1 for r in ok if r["han"] or r["missing"] or r.get("items"))
        print(f"{name:<20}{sum(r['han'] for r in ok):>9}"
              f"{sum(len(r['missing']) for r in ok):>9}"
              f"{sum(r['ratio'] for r in ok)/len(ok):>11.3f}{bad:>9}")

    out = Path(args.out or f"data/logs/ab_translate_{args.field}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"field": args.field, "configs": list(cfgs),
                               "results": results}, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    print(f"\n明细（含全部译文）已写入 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
