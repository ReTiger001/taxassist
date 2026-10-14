r"""语义校验：用本地模型找「形式没问题、意思不对」的偏差。

======================================================================
为什么机检不够
======================================================================

``audit_translation.py`` 查的是**形式** —— 残留汉字、数字缺失、条目数不
对、结构丢失。它查不出「数字一个不少、句子也通顺，但意思译反了」这类
偏差。2026-10-08 那轮用 250 条抽样验伪检查器时就见过实例。

这一步把**中文原文与英文译文一起**交给本地模型，让它给语义判定。

======================================================================
为什么走本地模型
======================================================================

正文 14000+ 条，逐条送云端既慢又要按量计费，而判定质量要求并不高
（它只负责**把可疑的挑出来交给人**，不负责拍板）。qwen2.5:14b-instruct
在本机跑就够了。

模型走 Ollama。**启动必须用 启动Ollama.bat 或显式带上** ``OLLAMA_MODELS``
—— 模型在 D:\models 而非 ollama 默认目录，不带这个变量拿到的模型列表是
空的，会误报「模型不存在」。

======================================================================
纪律
======================================================================

- **只读生产库**（``mode=ro``）：库正被翻译工作流写入，绝不去抢锁。
- **增量**：已判过的 uid 跳过，随时可中断续跑。
- **输出兼容**：``data/logs/semantic_check_<field>.json`` 沿用既有格式，
  ``build_review_html.py`` 直接读它，不用改下游。

用法::

    python scripts/semantic_check.py --field content --limit 20          # 随机/最早优先
    python scripts/semantic_check.py --field content --only-flagged      # 只查机器标红与拿不准
    python scripts/semantic_check.py --field content --only-flagged --limit 0   # 全量
"""

import argparse
import json
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
DB_RO = "file:D:/EY-project/data/taxassist.db?mode=ro"
LOG_DIR = Path("data/logs")
DEFAULT_MODEL = "qwen2.5:14b-instruct"

PROMPT = """你是税务政策翻译的审校员。下面是一条中文政策文本和它的英文译文。
判断英文译文是否忠实、完整地传达了中文原文的语义。

只输出一行，不要复述原文、不要解释过程：
- 忠实且完整 -> 输出 OK
- 有语义偏差（漏掉要点、译反、添油加醋、把说明文字当正文译进去）-> 输出 问题：<一句话说明>

【中文原文】
{zh}

【英文译文】
{en}
"""


def ask_model(model, zh, en, timeout=180):
    """向 Ollama 要一次判定，返回 (ok, answer)。"""
    payload = json.dumps({
        "model": model,
        "prompt": PROMPT.format(zh=zh, en=en),
        "stream": False,
        # num_ctx 显式给 —— 与 ai_review.py 同一条理由：ollama 的默认值随版本
        # 变，给小了会**静默截断**。而这里的后果尤其隐蔽：输入是「中文原文 +
        # 英文译文」，截断后模型只看到前半段，会**因为看不到问题而判 OK** ——
        # 假阴性、不报错，还会让"语义校验通过"这个结论失去意义。
        # `--max-chars` 默认 6000 是必要的但不够：中文约 1 字 1 token，
        # 再加英文译文合计可到 7000+，仍超过默认窗口。
        "options": {"num_ctx": 8192, "temperature": 0, "num_predict": 128},
    }).encode("utf-8")
    req = urllib.request.Request(OLLAMA_URL, data=payload,
                                headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = json.loads(r.read())
    except urllib.error.URLError as e:
        raise SystemExit(f"连不上 Ollama（{e}）。先跑 启动Ollama.bat，"
                         "或确认 OLLAMA_MODELS 已设。") from None
    answer = (body.get("response") or "").strip()
    ok = answer.upper().startswith("OK")
    return ok, answer[:400]


def load_done(path):
    if not path.exists():
        return set()
    try:
        return {r["uid"] for r in json.loads(path.read_text(encoding="utf-8")).get("results", [])}
    except (json.JSONDecodeError, KeyError, TypeError):
        return set()


def pick_items(conn, field, only_flagged, limit):
    """挑待判条目。only_flagged 时优先取 AI 审核标了 issue / unsure 的。"""
    if only_flagged:
        rows = conn.execute(
            "SELECT r.doc_uid FROM translation_ai_review r"
            " WHERE r.field=? AND r.verdict IN ('issue','unsure')"
            " ORDER BY CASE r.verdict WHEN 'issue' THEN 0 ELSE 1 END, r.doc_uid",
            (field,)).fetchall()
        uids = [r[0] for r in rows]
    else:
        uids = [r[0] for r in conn.execute(
            "SELECT doc_uid FROM translation WHERE field=? ORDER BY doc_uid", (field,))]
    return uids if limit <= 0 else uids[:limit]


def fetch_pair(conn, uid, field):
    col = "content" if field == "content" else "title"
    p = conn.execute(f"SELECT {col} FROM policy WHERE doc_uid=?", (uid,)).fetchone()
    t = conn.execute("SELECT text FROM translation WHERE doc_uid=? AND field=?",
                     (uid, field)).fetchone()
    if not p or not t:
        return None
    zh, en = (p[0] or ""), (t[0] or "")
    # 空内容不要送进模型：它只会回「缺少原文和译文内容」，
    # 那条既不是真判定，又会污染 flagged 计数（本轮踩过，7 条）。
    if not zh.strip() or not en.strip():
        return None
    return zh, en


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--field", choices=("title", "content"), default="content")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--limit", type=int, default=20, help="0 = 全部")
    ap.add_argument("--only-flagged", action="store_true",
                    help="只查 AI 审核标了 issue / unsure 的条目（机器标红优先）")
    ap.add_argument("--max-chars", type=int, default=6000, help="超长正文截断，避免撑爆上下文")
    args = ap.parse_args()

    out = LOG_DIR / f"semantic_check_{args.field}.json"
    conn = sqlite3.connect(DB_RO, uri=True, timeout=30)
    done = load_done(out)
    uids = [u for u in pick_items(conn, args.field, args.only_flagged, 0) if u not in done]
    todo = uids if args.limit <= 0 else uids[:args.limit]
    print(f"待判 {len(uids)} 条（已完成 {len(done)} 条，本次处理 {len(todo)} 条）")

    results = []
    if out.exists():
        try:
            results = json.loads(out.read_text(encoding="utf-8")).get("results", [])
        except (json.JSONDecodeError, KeyError, TypeError):
            results = []
    flagged = 0
    t0 = time.time()
    for i, uid in enumerate(todo, 1):
        pair = fetch_pair(conn, uid, args.field)
        if pair is None:
            continue
        zh, en = pair
        ok, answer = ask_model(args.model, zh[:args.max_chars], en[:args.max_chars])
        if not ok:
            flagged += 1
        results.append({"uid": uid, "zh_len": len(zh), "en_len": len(en),
                        "ok": ok, "answer": answer})
        rate = i / max(time.time() - t0, 0.01)
        print(f"  [{i}/{len(todo)}] {uid[:34]:<34} "
              f"{'OK' if ok else '问题':<4} {rate:.2f} 条/秒 "
              f"{'' if ok else answer[:60]}")
        if i % 5 == 0 or i == len(todo):
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(
                {"field": args.field, "model": args.model, "results": results},
                ensure_ascii=False, indent=1), encoding="utf-8")
    conn.close()
    print(f"完成：本轮判 {len(todo)} 条，其中 {flagged} 条判为有问题。"
          f"累计 {len(results)} 条 -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
