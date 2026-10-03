"""自生产体检：四个阶段各自的健康度 + 真实缺口。

判据不是"有没有数据"，而是"**该做的做了没有**"：
  fetch     31 省是否都有源、最近有没有跑、有没有失败的源
  publish   有多少条还没正文 / 文号 / 成文日期，效力判了多少
  verify    归档快照与政策关系（重解析的原料）
  translate 标题与正文的翻译覆盖率
  attachment 解析成功与不支持的构成
"""
import sqlite3
from pathlib import Path

ROOT = Path(r"D:\EY-project")
conn = sqlite3.connect(f"file:{ROOT / 'data' / 'taxassist.db'}?mode=ro",
                       uri=True, timeout=10)
c = conn.cursor()


def one(sql):
    return c.execute(sql).fetchone()[0]


# 先取字段名，避免猜错列名（上几轮就吃过这个亏）
cols = {r[1] for r in c.execute("PRAGMA table_info(policy)")}
n = one("SELECT COUNT(*) FROM policy")
print(f"=== 库总量 {n} ===\n")


def coverage(col, label):
    if col not in cols:
        print(f"  {label:<12} （无此字段）")
        return
    got = one(f"SELECT COUNT(*) FROM policy WHERE IFNULL({col},'')<>''")
    print(f"  {label:<12} {got:>6}/{n}  {got * 100 // max(n, 1)}%")


print("【fetch】抓取")
print(f"  出现过的源    {one('SELECT COUNT(DISTINCT source_id) FROM fetch_log')}")
print(f"  最近一次      {one('SELECT MAX(started_at) FROM fetch_log')}")
fl = c.execute("SELECT source_id, COUNT(*) FROM fetch_log WHERE status='failed'"
               " GROUP BY source_id ORDER BY 2 DESC LIMIT 5").fetchall()
print(f"  失败过的源    {len(fl)} 个" + (f"：{', '.join(x[0] for x in fl)}" if fl else ""))

print("\n【publish】详情与判定")
for col, label in (("content", "有正文"), ("cwrq", "成文日期"),
                   ("p_doc_no_full", "有文号"), ("o_column", "有栏目"),
                   ("p_effect_status", "效力结论"),
                   ("p_effect_source", "判定依据"),
                   ("p_effective_date", "施行日期")):
    coverage(col, label)

print("\n【verify】归档与关系")
print(f"  归档快照      {one('SELECT COUNT(*) FROM raw_snapshot')}")
print(f"  政策关系      {one('SELECT COUNT(*) FROM policy_relation')}")

print("\n【translate】翻译")
for f in ("title", "content"):
    got = one(f"SELECT COUNT(*) FROM translation WHERE field='{f}'")
    print(f"  {f:<12} {got:>6}/{n}  {got * 100 // max(n, 1)}%")

print("\n【attachment】附件")
tot = one("SELECT COUNT(*) FROM attachment")
ok = one("SELECT COUNT(*) FROM attachment WHERE parse_status='ok'")
print(f"  总数 {tot}，解析成功 {ok}（{ok * 100 // max(tot, 1)}%）")
for status, cnt in c.execute("SELECT IFNULL(parse_status,'(空)'), COUNT(*) "
                             "FROM attachment GROUP BY parse_status "
                             "ORDER BY 2 DESC LIMIT 6"):
    print(f"    {str(status):<20} {cnt}")
conn.close()
