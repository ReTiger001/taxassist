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
    last_commit = time.time()   # 提交条件的时间兜底，理由见下面改动处的注释
    # **写库锁改为「每批拿一次」** —— 这是撞锁的根因所在。
    # 原来由 translate_all 整场持有（开工锁到结束），其它写任务
    # （附件重试、judge、采集）永远等不到锁，只能去撞 SQLite 引擎锁，
    # busy_timeout 用尽就失败 —— 实测附件重试正是这么崩于
    # database is locked 的。
    # 现在：每批开始前拿锁，提交后立刻让出，让别的任务能插进来。
    from taxassist import writelock

    locked = False
    for i, r in enumerate(rows, 1):
        if not locked:
            if not writelock.acquire("translate", timeout=600):
                print(f"  写库锁被 {writelock.holder()} 占用，本轮退出")
                break
            locked = True
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
        # commit=False 攒批提交。translate_llm.save 的注释里写明了原因：
        # "每写一条就 commit 会让翻译与其它任务（效力判定、采集）频繁争抢
        # SQLite 写锁 —— 实测两边都慢十倍以上"。这里原先用了默认的
        # commit=True，实测直接撞成 "database is locked" 让整批退出。
        tl.save(conn, r["doc_uid"], field, src, out, model=args.model,
                commit=False)
        done += 1

        # **提交条件：每 5 条，或距上次提交已过 20 秒。**
        #
        # 为什么要加时间兜底：正文一条要 20 秒（逐段译），"每 5 条"看着
        # 不多，实际是 **100 秒的持锁窗口** —— 期间其它写任务（附件重试、
        # judge、采集）全部干等。实测：附件重试每条要等 ~180 秒才完成一次，
        # 而它自己下载只花 0.7 秒，纯粹是被这个窗口饿着。
        #
        # 按时间兜底后的效果：标题阶段（0.75 秒/条）仍是每 5 条提交
        # （3.75 秒一次，与原先一致）；正文阶段则每条都触发时间条件，
        # 等于每条提交，持锁从 100 秒缩到毫秒级。翻译本身不受影响 ——
        # 推理那 20 秒根本不碰数据库。
        if done % 5 == 0 or (time.time() - last_commit) > 20:
            conn.commit()
            last_commit = time.time()
            # **提交后让出写库锁**：给等待中的其它写任务一个窗口。
            # 让出 0.2 秒对翻译几乎无感（每批一次），但对附件重试那种
            # 几百条的小任务来说，就是能不能插进来的区别。
            if locked:
                writelock.release()
                locked = False
                time.sleep(0.2)
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
