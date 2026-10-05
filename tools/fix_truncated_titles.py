"""回填被截断的标题：走详情页 <title> 补齐存量数据。

已有 1364 条标题以省略号结尾（河北 817 / 新疆 257 / 陕西 234 / 辽宁 16）。
新的 apply_enrichment 逻辑会自动修，但那只对**之后**抓的生效 —— 存量得单独
跑一遍。这里复用同一条路径（parse_detail + apply_enrichment），
不另写一套合并逻辑，免得两处行为不一致。
"""
import sys
from pathlib import Path

sys.path.insert(0, r"D:\EY-project\src")
from taxassist import db as dbmod  # noqa: E402
from taxassist import store, writelock  # noqa: E402
from taxassist.collect.browser import fetch_many  # noqa: E402
from taxassist.collect.detail import parse_detail  # noqa: E402
from taxassist.config import RAW_DIR  # noqa: E402

if not writelock.acquire("fix_titles", timeout=1800):
    print(f"写库锁被 {writelock.holder()} 占用，等待超时，未启动。")
    raise SystemExit(1)

conn = dbmod.connect()
rows = conn.execute(
    "SELECT doc_uid, url, cwrq, title FROM policy"
    " WHERE title LIKE '%...' OR title LIKE '%..' OR title LIKE '%…'").fetchall()
print(f"待补标题 {len(rows)} 条", flush=True)
if not rows:
    conn.close()
    writelock.release()
    raise SystemExit(0)

# 先用已归档的详情页快照，没有再联网抓 —— 省时且不打扰源站。
snap = {}
for (d, rel) in conn.execute(
        "SELECT doc_uid, rel_path FROM raw_snapshot WHERE kind='detail_html'"):
    snap[d] = rel
hit = sum(1 for r in rows if r["doc_uid"] in snap)
print(f"其中 {hit} 条有归档快照（优先用快照，不联网）", flush=True)

need_fetch = [r["url"] for r in rows
              if r["doc_uid"] not in snap and r["url"]]
pages = fetch_many(need_fetch) if need_fetch else {}
print(f"另需联网抓 {len(need_fetch)} 页，完成 {len(pages)}", flush=True)

fixed = failed = 0
for i, r in enumerate(rows, 1):
    text = None
    rel = snap.get(r["doc_uid"])
    if rel:
        p = RAW_DIR / rel
        if p.exists():
            text = p.read_text(encoding="utf-8", errors="replace")
    if text is None:
        raw = pages.get(r["url"])
        if isinstance(raw, BaseException) or not raw:
            failed += 1
            continue
        text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw

    d = parse_detail(text, known_cwrq=r["cwrq"])
    if store.apply_enrichment(conn, r["doc_uid"], d) == "updated":
        fixed += 1
    conn.commit()
    if i % 100 == 0:
        print(f"  进度 {i}/{len(rows)}  已修 {fixed}  跳过 {failed}", flush=True)

print(f"\n完成：修复 {fixed}，无法处理 {failed}")
conn.close()
writelock.release()
