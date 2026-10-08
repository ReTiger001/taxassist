"""本地模型自动审核：给每条译文一个「准不准」的判断，拿不准的升级给人。

使用者的要求是「先翻译、然后本地模型审核、有模棱两可的你亲自审核，站在无人
托管的角度设计」。这个脚本就是中间那一环，也是让「无人托管」成立的关键 ——
没有它，机器只能自检那些**机器可判定**的错（数字、条目、结构），
「意思读偏」这类只有懂语义的读者看得出来的问题就永远漏着。

======================================================================
三档，关键是第三档
======================================================================

    OK                  译文准确
    问题：<一句说明>     译文有明确偏差
    不确定：<一句说明>   模型拿不准，需要人看

**第三档是刻意留的出口**。只给「OK / 问题」两个选项时，模型对拿不准的样本
会硬猜 —— 要么放过真错，要么报出一堆假问题。它的判断力本身是验证过的
（对照测试：把「应当」故意改成 may，它准确报出「应当不能译成may，逾期不予
受理与 Late filings shall be accepted 相反」），但那正是它**确定**的样本。
给它一个「不确定」的出口，「确定的」和「拿不准的」才能分开处理：

    确定的 OK    → 记账，不再打扰人
    确定的问题   → 进重译队列（机器能修）
    拿不准的     → 升级清单（人 / 上层 AI 处理）

======================================================================
一次审核，永久记账
======================================================================

结果落 ``translation_ai_review`` 表，**同一条不重复审**（表里按
(doc_uid, field) 唯一）。理由：审核要占 GPU，而重复审同一条不会得到新信息
—— 除非译文变了。译文变了怎么知道？``translation`` 表存了原文指纹，指纹变
则译文变，那种条目在下一次审核时会因为 reviewed_at 落后于 created_at 而被
重新取到。

用法：
    python scripts/ai_review.py --limit 5        # 先试 5 条看判得准不准
    python scripts/ai_review.py --limit 300      # 常规批量
    python scripts/ai_review.py --stats          # 看三档分布
    python scripts/ai_review.py --field title
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import httpx  # noqa: E402

from taxassist import db as dbmod  # noqa: E402

MODEL = "qwen2.5:14b-instruct"
OLLAMA = "http://127.0.0.1:11434/api/generate"
UNSURE_OUT = Path("data/logs/ai_unsure.json")

#: 提示词刻意写得短而具体。长篇大论的要求会让 14B 模型抓不住重点；点出三类最
#: 容易读反的地方，比泛泛说「是否准确」有效得多。**必须给出「不确定」这个
#: 出口**，否则它会对拿不准的样本硬猜。
PROMPT = """你是税务政策翻译的校对员。下面是一份中文政策原文和它的机器英译。

判断英译是否准确表达了原文的意思。特别注意这三类最容易读反的地方：
1. 义务方向：「应当」不能译成 may，「可以」不能译成 shall；「不予」与「予以」不能相反
2. 数字、比例、金额、期限
3. 适用条件：谁适用、什么情况下适用

只回答以下三种之一，不要写别的：
OK
问题：<一句话说明哪里不对>
不确定：<一句话说明你拿不准什么>

原文：
{zh}

英译：
{en}
"""


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS translation_ai_review ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " doc_uid TEXT NOT NULL,"
        " field TEXT NOT NULL,"
        " verdict TEXT NOT NULL,"          # ok / issue / unsure
        " reason TEXT,"
        " model TEXT,"
        " zh_len INTEGER,"                 # 记下原文长度：指纹比对之外的第二个线索
        " reviewed_at TEXT NOT NULL,"
        " UNIQUE(doc_uid, field))")
    conn.commit()


def ask(zh: str, en: str, timeout: int = 400) -> str:
    r = httpx.post(OLLAMA, json={
        "model": MODEL,
        "prompt": PROMPT.format(zh=zh, en=en),
        "stream": False,
        # num_ctx 显式给：ollama 默认值随版本变，给小了会静默截断 —— 模型看不到
        # 结尾，会把「没看到」当成「原文没写」。
        "options": {"num_ctx": 8192, "temperature": 0.1, "num_predict": 160},
    }, timeout=timeout)
    r.raise_for_status()
    return (r.json().get("response") or "").strip()


def parse(ans: str) -> tuple[str, str]:
    """把模型的回答归到三档。**认不出来的一律算 unsure** —— 宁可升级给人，
    也不要让一句没读懂的回答冒充「通过」。"""
    t = (ans or "").strip()
    if t.upper().startswith("OK"):
        return "ok", ""
    if t.startswith("问题") or "问题：" in t[:8]:
        return "issue", t.split("：", 1)[-1][:300]
    if t.startswith("不确定") or "不确定：" in t[:12]:
        return "unsure", t.split("：", 1)[-1][:300]
    return "unsure", f"回答无法归类：{t[:200]}"


def pick_pending(conn, field: str, limit: int, max_chars: int) -> list[sqlite3.Row]:
    """取还没审过的（或译文在上次审核之后又被重译过的）。"""
    zh_col = "p.title" if field == "title" else "p.content"
    return conn.execute(
        f"SELECT t.doc_uid, {zh_col} AS zh, t.text AS en, t.created_at AS made"
        f" FROM translation t"
        f" JOIN policy p ON p.doc_uid = t.doc_uid"
        f" LEFT JOIN translation_ai_review r ON r.doc_uid = t.doc_uid"
        f"   AND r.field = t.field"
        f" WHERE t.field = ? AND t.text IS NOT NULL"
        f"   AND LENGTH(IFNULL({zh_col}, '')) BETWEEN 1 AND ?"
        f"   AND (r.doc_uid IS NULL OR t.created_at > r.reviewed_at)"
        f" ORDER BY t.created_at DESC LIMIT ?",
        (field, max_chars, limit)).fetchall()


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--field", choices=("title", "content"), default="content")
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--max-chars", type=int, default=2500,
                    help="超过这个字数的跳过（模型上下文装不下，硬塞只会让它瞎猜）")
    ap.add_argument("--stats", action="store_true")
    args = ap.parse_args()

    conn = dbmod.connect()
    try:
        ensure_table(conn)
        if args.stats:
            rows = conn.execute(
                "SELECT field, verdict, COUNT(*) n FROM translation_ai_review"
                " GROUP BY field, verdict ORDER BY field, verdict").fetchall()
            if not rows:
                print("还没有任何 AI 审核记录。")
                return 0
            for r in rows:
                print(f"  {r['field']:8} {r['verdict']:7} {r['n']} 条")
            return 0

        pending = pick_pending(conn, args.field, args.limit, args.max_chars)
        print(f"[{args.field}] 待审 {len(pending)} 条（≤{args.max_chars} 字，"
              f"新的优先）\n")
        if not pending:
            return 0

        counts = {"ok": 0, "issue": 0, "unsure": 0}
        unsure: list[dict] = []
        t0 = time.time()
        for i, r in enumerate(pending, 1):
            try:
                ans = ask(r["zh"], r["en"])
            except Exception as e:  # noqa: BLE001 - 单条失败不中断整批
                print(f"  [{i}/{len(pending)}] 失败 {r['doc_uid'][:36]}: "
                      f"{type(e).__name__}: {e}", flush=True)
                continue
            verdict, reason = parse(ans)
            counts[verdict] += 1
            conn.execute(
                "INSERT INTO translation_ai_review"
                " (doc_uid, field, verdict, reason, model, zh_len, reviewed_at)"
                " VALUES (?,?,?,?,?,?,?)"
                " ON CONFLICT(doc_uid, field) DO UPDATE SET"
                "   verdict=excluded.verdict, reason=excluded.reason,"
                "   reviewed_at=excluded.reviewed_at",
                (r["doc_uid"], args.field, verdict, reason, MODEL, len(r["zh"] or ""),
                 dt.datetime.now().isoformat(timespec="seconds")))
            if verdict == "unsure":
                unsure.append({"uid": r["doc_uid"], "reason": reason,
                               "zh_len": len(r["zh"] or "")})
            if i % 20 == 0:
                conn.commit()
            mark = {"ok": "OK ", "issue": "问题", "unsure": "拿不准"}[verdict]
            print(f"  [{i}/{len(pending)}] {mark} {r['doc_uid'][:40]:<40} "
                  f"{reason[:76]}", flush=True)
        conn.commit()
    finally:
        conn.close()

    el = (time.time() - t0) / 60
    tot = sum(counts.values())
    print(f"\n完成：{tot} 条，用时 {el:.1f} 分钟")
    print(f"  OK {counts['ok']} ／ 有问题 {counts['issue']} ／ "
          f"拿不准 {counts['unsure']}")
    if unsure:
        UNSURE_OUT.write_text(json.dumps(unsure, ensure_ascii=False, indent=1),
                              encoding="utf-8")
        print(f"\n拿不准的 {len(unsure)} 条已写入 {UNSURE_OUT} —— 这些需要人/上层 AI 看")
        for r in unsure[:12]:
            print(f"  · {r['uid'][:52]}  {r['reason'][:80]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
