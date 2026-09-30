"""批量翻译标题 / 正文，用本地 Hunyuan-MT（通过 ollama）。

用法：
    python scripts/translate_batch.py --what titles --limit 20     # 先试 20 条看质量
    python scripts/translate_batch.py --what titles                # 全量标题
    python scripts/translate_batch.py --what content --years 3     # 近三年正文
    python scripts/translate_batch.py --what content               # 全量正文（很慢）

设计要点：
- 已译且原文未变的会跳过（缓存 + 原文指纹），中断后重跑不会白做
- 每 20 条报一次进度，含速度与预计剩余时间 —— 全量要跑很久，没有进度会以为它挂了
- 失败不中断整批，逐条记录继续
"""
from __future__ import annotations

import argparse
import datetime
import time

from taxassist import db as dbmod
from taxassist import translate_llm as tl


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--what", choices=["titles", "content"], default="titles")
    ap.add_argument("--limit", type=int, default=0, help="0 = 全部")
    ap.add_argument("--years", type=int, default=0, help="只译最近 N 年（0 = 全部）")
    ap.add_argument("--model", default=tl.DEFAULT_MODEL)
    ap.add_argument("--max-chars", type=int, default=1200, help="正文切块大小")
    args = ap.parse_args()

    ready, why = tl.is_available(args.model)
    print(f"模型状态：{why}")
    if not ready:
        return 1

    conn = dbmod.connect()
    tl.ensure_table(conn)

    field = "title" if args.what == "titles" else "content"
    where: list[str] = []
    params: list = []
    if field == "content":
        where.append("IFNULL(p.content,'') <> ''")
    if args.years:
        since = (datetime.date.today()
                 - datetime.timedelta(days=365 * args.years)).isoformat()
        where.append("p.cwrq >= ?")
        params.append(since)

    sql = "SELECT p.doc_uid, p.title, p.content FROM policy p"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY p.cwrq DESC"
    if args.limit:
        sql += f" LIMIT {args.limit}"

    rows = conn.execute(sql, params).fetchall()
    print(f"待处理 {len(rows)} 条（{field}）")
    if not rows:
        return 0

    done = skipped = failed = 0
    t0 = time.time()
    for i, r in enumerate(rows, 1):
        src = r["title"] if field == "title" else (r["content"] or "")
        if not (src or "").strip():
            skipped += 1
            continue
        if tl.cached(conn, r["doc_uid"], field, src) is not None:
            skipped += 1
            continue
        try:
            if field == "title":
                out = tl.translate(src, model=args.model)
            else:
                out = tl.translate_long(src, model=args.model, max_chars=args.max_chars)
        except Exception as e:  # noqa: BLE001 - 单条失败不该中断整批
            failed += 1
            print(f"  [{i}/{len(rows)}] 失败 {r['doc_uid']}: {type(e).__name__}: {e}")
            continue
        tl.save(conn, r["doc_uid"], field, src, out, model=args.model)
        done += 1

        if done % 20 == 0:
            el = time.time() - t0
            rate = done / el if el > 0 else 0
            left = (len(rows) - i) / rate / 60 if rate > 0 else 0
            print(f"  已译 {done}  跳过 {skipped}  失败 {failed}  "
                  f"{rate:.2f} 条/秒  预计剩余 {left:.0f} 分钟")

    el = time.time() - t0
    print(f"\n完成：译 {done}，跳过 {skipped}（已缓存或为空），失败 {failed}，"
          f"用时 {el/60:.1f} 分钟")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
