"""术语用词修复：把非定稿写法改成定稿写法。**默认只报告，不写库。**

与 ``scripts/fix_org_names.py`` 同源（那个管结构，这个管用词）。

======================================================================
两类改动，分开处理
======================================================================

**A 类 · 官方已改名，没有讨论余地** —— 可直接修：
    State Administration of Taxation  →  State Taxation Administration
    State Tax Administration          →  State Taxation Administration
国家税务总局 2018 年由「国家税务局」改为「国家税务总局」，官方英文名随之变更。
旧译名不是「另一种合理译法」，是错的名字。（实测：4978 条含该机构的译文里
99.4% 已用新名，残留 12 条。）

**B 类 · 两种写法都说得通，得人来定** —— 只报告，不动：
    Corporate Income Tax  ↔  Enterprise Income Tax（中文官方用后者）
    Stamp Duty            ↔  Stamp Tax
    这类**必须使用者拍板**：他才是这一行的专业人士，机器只能把分布摆出来。

======================================================================
为什么还要对照中文原文
======================================================================

与机构名那次同一个理由：英文串单独看分不出对错。「State Administration of
Taxation」在引用 2018 年前的历史文件时可能**本就是正确的原文表述** —— 但这里
的场合是翻译中文政策，原文写的都是「国家税务总局」，所以对照中文是必要门槛，
不是多余的保险。

用法：
    python scripts/fix_terms.py                  # 干跑：只报告
    python scripts/fix_terms.py --apply          # 落库（会先写备份）
    python scripts/fix_terms.py --field title    # 只处理标题
    python scripts/fix_terms.py --include-b      # 连 B 类一起报告（仍不落库）
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from taxassist import db as dbmod  # noqa: E402

#: A 类：官方名已变，旧写法就是错的。值是 (错误写法, 正确写法)。
FIXABLE: list[tuple[re.Pattern, str, str]] = [
    (re.compile(r"State Administration of Taxation", re.I),
     "State Administration of Taxation", "State Taxation Administration"),
    # 只在不是 "State Administration of Taxation" 的场合才动（顺序有讲究：
    # 上面那条先跑，把长的吃掉，这条才不会误伤）。
    (re.compile(r"State Tax Administration", re.I),
     "State Tax Administration", "State Taxation Administration"),
]

#: B 类：两边都说得通，只摆出来给人定，脚本不动。
DEBATABLE: list[tuple[str, str]] = [
    ("Corporate Income Tax", "Enterprise Income Tax（中文官方写法）"),
    ("Stamp Duty", "Stamp Tax"),
    ("Personal Income Tax", "Individual Income Tax"),
    ("Abolish", "Repeal"),
]

#: 中文门槛：只有原文里真的出现该机构，才认定这处英文该改。
ZH_GATE = {
    "State Administration of Taxation": re.compile("国家税务总局"),
    "State Tax Administration": re.compile("国家税务总局"),
}


def build(field: str = "title", connect=dbmod.connect):
    """算出所有该改的条目，返回 [(translation.id, 改前, 改后, 命中写法)]。"""
    conn = connect()
    try:
        zh_col = "p.title" if field == "title" else "p.content"
        rows = conn.execute(
            f"SELECT t.id, t.text AS en, {zh_col} AS zh"
            f" FROM translation t LEFT JOIN policy p ON p.doc_uid = t.doc_uid"
            f" WHERE t.field=? AND t.text IS NOT NULL", (field,)).fetchall()
    finally:
        conn.close()

    planned, seen = [], set()
    for r in rows:
        en, zh = r["en"] or "", r["zh"] or ""
        new = en
        hits = []
        for pat, wrong, right in FIXABLE:
            gate = ZH_GATE.get(wrong)
            if gate is not None and not gate.search(zh):
                continue                      # 中文里没有 → 不动
            if not pat.search(new):
                continue
            # **收敛保护**（同 fix_org_names）：替换后若还匹配得上，说明这处形态
            # 在本规则之外，改了只会原地打转 —— 跳过，别让脚本不收敛。
            before = len(pat.findall(new))
            cand = pat.sub(right, new)
            if len(pat.findall(cand)) < before and cand != new:
                new = cand
                hits.append(wrong)
        if new != en and r["id"] not in seen:
            seen.add(r["id"])
            planned.append((r["id"], en, new, ", ".join(sorted(set(hits)))))
    return planned


def scan_debatable(field: str, connect=dbmod.connect) -> dict[str, int]:
    """B 类只统计，不改。"""
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT t.text AS en FROM translation t WHERE t.field=?",
            (field,)).fetchall()
    finally:
        conn.close()
    out = {}
    for wrong, _right in DEBATABLE:
        pat = re.compile(re.escape(wrong), re.I)
        out[wrong] = sum(1 for r in rows if pat.search(r["en"] or ""))
    return out


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="真的写库（默认只干跑）")
    ap.add_argument("--field", choices=("title", "content"), default="content")
    ap.add_argument("--include-b", action="store_true", help="连 B 类一起报告")
    args = ap.parse_args()

    planned = build(args.field)
    print(f"[{args.field}] A 类（官方名，可直接修）：{len(planned)} 条待改")
    for tid, before, after, hit in planned[:8]:
        # 显示**改动处**的上下文，不显示开头 —— 长正文的开头永远一样。第一版只印
        # 开头 86 字符，8 条里每条的改前/改后看起来完全一样，等于没报告。
        i = next((k for k in range(min(len(before), len(after)))
                  if before[k] != after[k]), 0)
        s = max(0, i - 55)
        print(f"  id={tid} 命中「{hit}」")
        print(f"    改前: …{before[s:i + 70]}…")
        print(f"    改后: …{after[s:i + 70]}…")

    if args.include_b:
        print(f"\n[{args.field}] B 类（两种都说得通，需人定，脚本不动）：")
        for w, r in scan_debatable(args.field).items():
            print(f"  {w:<32} {r:>6} 条   ↔  {dict(DEBATABLE)[w]}")

    if not planned:
        print("\n没有需要改的。")
        return 0
    if not args.apply:
        print("\n（这是干跑。确认无误后加 --apply 落库。）")
        return 0

    backup = Path(f"data/logs/term_fix_{args.field}_backup.json")
    backup.parent.mkdir(parents=True, exist_ok=True)
    backup.write_text(json.dumps(
        [{"id": t, "before": b} for t, b, _a, _w in planned],
        ensure_ascii=False, indent=1), encoding="utf-8")

    conn = dbmod.connect()
    try:
        for tid, _before, after, _w in planned:
            conn.execute("UPDATE translation SET text=? WHERE id=?", (after, tid))
        conn.commit()
    finally:
        conn.close()
    print(f"\n已修复 {len(planned)} 条；改前内容备份在 {backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
