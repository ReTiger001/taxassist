"""重试 unsupported 附件 —— WPS 转换已确认可用（实测 6/6 成功）。

`fetch_attachments(only_pending=True)` 会把 parse_status='unsupported' 的
也捞进来重试（代码注释："解析器升级后应该再试一次 —— 否则修复永远不会
生效"）。WPS 转换现在确实能用了，所以这 1343 条值得整体重跑。
"""
import sys
import time

sys.path.insert(0, r"D:\EY-project\src")
from taxassist import db as dbmod  # noqa: E402
from taxassist import pipeline, writelock  # noqa: E402

# 与 worker 同样的让路规则：翻译在跑时不抢写库（见 taxassist.writelock）
if not writelock.acquire("retry_attachments", timeout=1800):
    print(f"写库锁被 {writelock.holder()} 占用，等待超时，未启动。")
    raise SystemExit(1)

conn = dbmod.connect()
tot = {"ok": 0, "unsupported": 0, "failed": 0, "no_text_layer": 0}
for rnd in range(1, 40):
    r = pipeline.fetch_attachments(conn, limit=50, only_pending=True)
    if r["requested"] == 0:
        print("没有待处理的了。")
        break
    for k in tot:
        tot[k] += r.get(k, 0)
    print(f"  第 {rnd} 轮：请求 {r['requested']}  成功 {r['ok']}  "
          f"不支持 {r['unsupported']}  失败 {r['failed']}  "
          f"扫描件 {r['no_text_layer']}  累计成功 {tot['ok']}", flush=True)
    # 整轮都没成功、且全是不支持 → 再跑也没意义
    if r["ok"] == 0 and r["unsupported"] >= r["requested"]:
        print("  这一轮全是 unsupported，再跑也白跑，停。")
        break
    time.sleep(1)

print(f"\n合计：{tot}")
conn.close()
writelock.release()
