"""语义校验：用本地大模型逐条判断「英译是否准确表达了原文」。

======================================================================
它和机检的分工
======================================================================

``scripts/audit_translation.py`` 的 9 类检查全是**机器可判定**的（数字、条目数、
残留汉字、结构）—— 它们能覆盖「信息丢失」，但覆盖不了**整句意思读偏**：
把「应当」译成 *may*、把「不予退还」译成 *shall be refunded*，数字一个不少、
条目数一模一样，可意思正好相反。这类错只有懂语义的读者看得出来。

这里让 ``qwen2.5:14b-instruct`` 来当那个读者。它**不是判官** —— 它也会错
（幻觉、被长文本带偏），所以它产出的是**嫌疑清单**，仍需人工复核。但
「6000 条没人看过」和「模型标出的几十条嫌疑」是两种完全不同量级的工作。

======================================================================
三条刻意的限制
======================================================================

1. **只查短文本**（默认 ≤2500 字）。长文档要对照全文才判得准，塞不进模型
   上下文，硬塞只会让它瞎猜。而长文档的问题机检已经能抓（结构丢失、数字
   成批消失），语义校验不必重复劳动。

2. **优先查机检全绿的条目**。机检已报警的那些问题已知；真正没人看过的是
   「机检说没问题」的那五千多条 —— 校验的价值就在那里。

3. **不写库**。只产出嫌疑清单，不做任何修改。

用法：
    python scripts/semantic_check.py --limit 3       # 先试 3 条，看它判得准不准
    python scripts/semantic_check.py --limit 150     # 抽样 150 条
    python scripts/semantic_check.py --only-flagged  # 反过来：查机检已命中的
    python scripts/semantic_check.py --max-chars 1200
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import httpx  # noqa: E402

from taxassist import db as dbmod  # noqa: E402

MODEL = "qwen2.5:14b-instruct"
OLLAMA = "http://127.0.0.1:11434/api/generate"

#: 提示词刻意写得**短而具体**。长篇大论的要求会让 14B 模型抓不住重点；
#: 点出三类最容易读反的地方（义务方向、数字、适用条件）比泛泛说「是否准确」
#: 有效得多。要求它只回 OK 或「问题：…」，是为了让判定可以被字符串匹配 ——
#: 让模型输出 JSON 再解析，反而更容易被它多写的几句话搞崩。
PROMPT = """你是税务政策翻译的校对员。下面是一份中文政策原文和它的机器英译。

判断英译是否准确表达了原文的意思。特别注意这三类最容易读反的地方：
1. 义务方向：「应当」不能译成 may，「可以」不能译成 shall；「不予」与「予以」不能相反
2. 数字、比例、金额、期限
3. 适用条件：谁适用、什么情况下适用

如果准确，只回答：OK
如果有偏差，只回答：问题：<一句话说明>

原文：
{zh}

英译：
{en}
"""


def ask(zh: str, en: str, timeout: int = 400) -> str:
    r = httpx.post(OLLAMA, json={
        "model": MODEL,
        "prompt": PROMPT.format(zh=zh, en=en),
        "stream": False,
        # num_ctx 必须显式给：ollama 的默认值随版本变（2048/4096），而这里
        # 一条最多要装 2500 字中文 + 对应英文，给小了会被静默截断 —— 模型
        # 看不到结尾，就会把「没看到」当成「原文没写」。
        "options": {"num_ctx": 8192, "temperature": 0.1, "num_predict": 200},
    }, timeout=timeout)
    r.raise_for_status()
    return (r.json().get("response") or "").strip()


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--field", choices=("title", "content"), default="content")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--max-chars", type=int, default=2500,
                    help="超过这个字数的原文跳过（模型上下文装不下）")
    ap.add_argument("--only-flagged", action="store_true",
                    help="只查机检已命中的（默认相反：只查机检全绿的）")
    ap.add_argument("--seed", type=int, default=20261008)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    rep_path = Path("data/logs/audit_translation.json")
    rep = json.loads(rep_path.read_text(encoding="utf-8")).get(args.field, {})
    flagged = {h["uid"] for lst in rep.get("hits", {}).values() for h in lst}

    conn = dbmod.connect()
    zh_col = "p.title" if args.field == "title" else "p.content"
    rows = conn.execute(
        f"SELECT t.doc_uid, t.text AS en, {zh_col} AS zh FROM translation t"
        f" LEFT JOIN policy p ON p.doc_uid=t.doc_uid"
        f" WHERE t.field=? AND t.text IS NOT NULL", (args.field,)).fetchall()
    conn.close()

    pool = [r for r in rows
            if (r["zh"] or "").strip() and len(r["zh"]) <= args.max_chars
            and ((r["doc_uid"] in flagged) == args.only_flagged)]
    random.Random(args.seed).shuffle(pool)
    picked = pool[:args.limit]
    print(f"[{args.field}] 候选池 {len(pool)} 条（≤{args.max_chars} 字，"
          f"{'机检已命中' if args.only_flagged else '机检全绿'}），抽 {len(picked)} 条\n")

    results, issues = [], []
    t0 = time.time()
    for i, r in enumerate(picked, 1):
        try:
            ans = ask(r["zh"], r["en"])
        except Exception as e:  # noqa: BLE001 - 单条失败不中断整批
            print(f"  [{i}/{len(picked)}] 失败 {r['doc_uid'][:36]}: "
                  f"{type(e).__name__}: {e}", flush=True)
            continue
        ok = ans.strip().upper().startswith("OK")
        rec = {"uid": r["doc_uid"], "zh_len": len(r["zh"]),
               "en_len": len(r["en"]), "ok": ok, "answer": ans[:400]}
        results.append(rec)
        if not ok:
            issues.append(rec)
        mark = "OK " if ok else "嫌疑"
        print(f"  [{i}/{len(picked)}] {mark} {r['doc_uid'][:38]:<38} "
              f"{'' if ok else ans[:90]}", flush=True)

    el = (time.time() - t0) / 60
    print(f"\n完成：{len(results)} 条，用时 {el:.1f} 分钟")
    print(f"  OK {len(results) - len(issues)} 条 ／ 嫌疑 {len(issues)} 条")
    out = Path(args.out or f"data/logs/semantic_check_{args.field}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"field": args.field, "model": MODEL,
                               "results": results}, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    print(f"明细已写入 {out}")
    if issues:
        print("\n嫌疑清单（需人工对照原文复核）：")
        for r in issues:
            print(f"  · {r['uid'][:52]}  {r['answer'][:110]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
