"""修复标题翻译里「国家税务总局 + 地方税务局」被拆成两个机构的问题。

**问题长什么样**：原文「国家税务总局江苏省税务局」指**一个**机关，模型却译成
    State Taxation Administration and the Jiangsu Provincial Taxation Bureau
—— 总局与地方局并列成两个机构。同一批数据里模型两种译法都有，有时对有时错：
    正确的：the Jiangsu Provincial Tax Service, State Taxation Administration
    错误的：State Taxation Administration and the Jiangsu Provincial Taxation Bureau

**修法**：把总局在前的并列/逗号结构倒过来，改成官方的逗号从属形式：
    State Taxation Administration and the <地方> Provincial Taxation Bureau
      →  the <地方> Provincial Taxation Bureau, State Taxation Administration
地方在前、总局在后、用逗号表示从属 —— 与官方英文名一致。

**为什么做成脚本而不是一次性修数据**：新翻译的内容还会再产生同样的错，重译之后
也可能回退。这是可重复运行的工具，不是补丁。

**用法**：
    python scripts/fix_org_names.py            # 干跑：只报告，不动库
    python scripts/fix_org_names.py --apply    # 落库，并写出备份 json

**安全**：默认干跑；`--apply` 时把改动前的原文写进
``data/logs/title_org_backup.json``，改错了可以据此回滚。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from taxassist import db as dbmod  # noqa: E402

STA = r"State Taxation Administration"
# 地方局：可选冠词 + 若干首字母大写词 + 可选 Provincial/Municipal/... + 机构词
LOCAL = (r"((?:[A-Z][\w'-]+\s+){0,3}?"
         r"(?:Provincial|Municipal|Autonomous|Regional)?\s*"
         r"(?:Taxation Bureau|Tax Service|Tax Bureau|Tax Authority|Tax Office))")

# **前导的 `the` 要一起吃进匹配**（它指的是"总局"）。第一版漏了这一步，替换后
# 变成 "by the the Jiangsu…"；第二版吃进来了却忘了补回，又变成 "by Jiangsu…"。
# 现在：匹配吃掉它，替换时给地方局补一个新的。
PAT = re.compile(rf"(?:the\s+)?{STA}\s*(?:and|,)\s*(?:the\s+)?{LOCAL}", re.I)


def build(connect=dbmod.connect):
    """算出所有该改的条目，返回 [(translation.id, 改前, 改后)]。"""
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, text FROM translation WHERE field='title' AND text IS NOT NULL"
        ).fetchall()
    finally:
        conn.close()
    planned = []
    for r in rows:
        en = r["text"]
        # **用 finditer 处理所有匹配**：一个标题里同一机构可能出现两次
        # （如「关于《…办法》…的公告」—— 书名号内外各一次）。最初用 search()
        # 只改第一处，剩下的就漏了 —— 干跑复核时才暴露。
        matches = list(PAT.finditer(en))
        if not matches:
            continue
        # 从后往前替换，免得前面的替换改动后面匹配的下标
        new = en
        for m in reversed(matches):
            local = m.group(1).strip()
            new = new[:m.start()] + f"the {local}, {STA}" + new[m.end():]
        if new != en:
            planned.append((r["id"], en, new))
    return planned


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="真的写库（默认只干跑）")
    args = ap.parse_args()

    planned = build()
    print(f"找到 {len(planned)} 条需要修复的标题")
    for _tid, before, after in planned[:5]:
        print(f"  改前: {before[:88]}")
        print(f"  改后: {after[:88]}")
    if not planned:
        return 0
    if not args.apply:
        print("\n（这是干跑。确认无误后加 --apply 落库。）")
        return 0

    backup = Path("data/logs/title_org_backup.json")
    backup.parent.mkdir(parents=True, exist_ok=True)
    backup.write_text(
        json.dumps([{"id": t, "before": b} for t, b, _a in planned],
                   ensure_ascii=False, indent=1),
        encoding="utf-8")

    conn = dbmod.connect()
    try:
        for tid, _before, after in planned:
            conn.execute("UPDATE translation SET text=? WHERE id=?", (after, tid))
        conn.commit()
    finally:
        conn.close()
    print(f"\n已修复 {len(planned)} 条；改前内容备份在 {backup}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
