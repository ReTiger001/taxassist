"""回填文号排序键（p_doc_no_year / p_doc_no_seq）。

为什么要落库而不是每次 ORDER BY 现算：文号形态五花八门，提取要用正则；
13946 条排序时在 SQL 里跑字符串函数既慢又用不上索引。

依赖 db.migrate 先加列 —— 所以这里先调 init_db（它会跑 migrate），
再在同一个写库锁内回填。两步都写库，必须一起持锁。
"""
import sys
import time

sys.path.insert(0, 'D:/EY-project/src')
from taxassist import db, writelock  # noqa: E402
from taxassist.collect.normalize import doc_no_sort_key  # noqa: E402

conn = db.connect()

got = False
for i in range(24):
    if writelock.acquire("docno_sort_fill", timeout=25):
        got = True
        break
    print(f"  等锁第 {i+1} 次（{writelock.holder()}）", flush=True)
    time.sleep(45)
if not got:
    print("拿不到锁，退出")
    sys.exit(1)

# ① 迁移（加两列；已存在则跳过）
applied = db.migrate(conn)
print(f"迁移应用：{applied or '（无新增列）'}")

# ② 回填
rows = conn.execute("SELECT doc_uid, p_doc_no_full FROM policy"
                    " WHERE IFNULL(p_doc_no_full,'') <> ''").fetchall()
pairs = []
for r in rows:
    k = doc_no_sort_key(r["p_doc_no_full"])
    if k:
        pairs.append((k[0], k[1], r["doc_uid"]))
print(f"有文号 {len(rows)} 条，能算出排序键的 {len(pairs)} 条"
      f"（算不出的 {len(rows) - len(pairs)} 条不猜，留 NULL）")
conn.executemany(
    "UPDATE policy SET p_doc_no_year=?, p_doc_no_seq=? WHERE doc_uid=?", pairs)
conn.commit()
writelock.release()

filled = conn.execute("SELECT COUNT(*) FROM policy WHERE p_doc_no_year IS NOT NULL").fetchone()[0]
print(f"回填完成：{filled} 条有排序键")

# ③ 抽查排序效果（这就是使用者要的「先排年份、同年排序号」）
print("\n升序前 8 条（最早在前）：")
for r in conn.execute("SELECT p_doc_no_year y, p_doc_no_seq s, p_doc_no_full n, title t"
                      " FROM policy WHERE p_doc_no_year IS NOT NULL"
                      " ORDER BY p_doc_no_year ASC, p_doc_no_seq ASC LIMIT 8"):
    print(f"   {r['y']}-{r['s']:<5} [{r['n'][:34]}] {(r['t'] or '')[:34]}")
print("\n降序前 8 条（最新在前）：")
for r in conn.execute("SELECT p_doc_no_year y, p_doc_no_seq s, p_doc_no_full n, title t"
                      " FROM policy WHERE p_doc_no_year IS NOT NULL"
                      " ORDER BY p_doc_no_year DESC, p_doc_no_seq DESC LIMIT 8"):
    print(f"   {r['y']}-{r['s']:<5} [{r['n'][:34]}] {(r['t'] or '')[:34]}")
conn.close()
