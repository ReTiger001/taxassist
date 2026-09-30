"""启动标题全量翻译（5090 条，约 1 小时；可中断后可续跑）。

用法：
    python run_translate_titles.py            # 译所有还没译的标题
    python run_translate_titles.py --limit 50 # 只译 50 条（试跑）
"""
import sys

from taxassist import db as dbmod
from taxassist import local_translate as lt

limit = None
if "--limit" in sys.argv:
    limit = int(sys.argv[sys.argv.index("--limit") + 1])

if not lt.ollama_available():
    print("ollama 服务未响应（127.0.0.1:11434）。请先确认它在运行。")
    raise SystemExit(1)

models = lt.list_models()
print("已下载模型:", models)
if not models:
    print("没有可用模型。先执行：ollama pull qwen2.5:7b")
    raise SystemExit(1)

model = "qwen2.5:7b" if "qwen2.5:7b" in models else models[0]
print(f"使用模型: {model}")
print()

conn = dbmod.connect()
dbmod.init_db(conn)
dbmod.migrate(conn)

todo = conn.execute(
    "SELECT COUNT(*) FROM policy WHERE IFNULL(title,'')<>'' AND IFNULL(p_title_en,'')=''"
).fetchone()[0]
print(f"待翻译标题: {todo} 条" + (f"（本次限制 {limit} 条）" if limit else ""))
print()

stats = lt.translate_titles(conn, model=model, limit=limit)
print()
print(f"本次完成 {stats['done']} 条，失败 {stats['failed']} 条，共扫描 {stats['total']} 条")

done = conn.execute(
    "SELECT COUNT(*) FROM policy WHERE IFNULL(p_title_en,'')<>''").fetchone()[0]
total = conn.execute("SELECT COUNT(*) FROM policy").fetchone()[0]
print(f"累计已译 {done} / {total} 条")

print()
print("=== 抽样 3 条（原文 -> 译文）===")
for r in conn.execute(
        "SELECT title, p_title_en FROM policy WHERE IFNULL(p_title_en,'')<>''"
        " ORDER BY p_translated_at DESC LIMIT 3"):
    print(f"  {r['title'][:52]}")
    print(f"    -> {r['p_title_en'][:100]}")
conn.close()
