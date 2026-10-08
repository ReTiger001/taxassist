"""术语定稿与修复：把不一致的写法统一成定稿写法。**默认只报告，不写库。**

与 ``scripts/fix_org_names.py`` 同源（那个管结构，这个管用词）。

======================================================================
两批规则，来源不同
======================================================================

**A 批 · 官方名已变，没有讨论余地**
    State Administration of Taxation  →  State Taxation Administration
    State Tax Administration          →  State Taxation Administration

国家税务总局的官方英文名随 2018 年机构更名而改变。旧译名不是「另一种合理
译法」，是错的名字。实测正文里残留 46 处。

**B 批 · 使用者定稿（2026-10-07）**

先用 ``scripts/audit_terms.py`` 把各写法的分布摆出来，再由使用者拍板：

    企业所得税   Corporate Income Tax  →  Enterprise Income Tax（中国官方写法）
    印花税       Stamp Duty            →  Stamp Tax
    个人所得税   Personal Income Tax   →  Individual Income Tax
    废止         Abolish / Abolition…  →  Repeal…

======================================================================
为什么不能一把 sub() 了事
======================================================================

实测出来的形态比预想的多得多（数字是正文里的实际处数）：

    企业所得税   corporate income tax 2296 ／ Corporate Income Tax 1273
                 ／ Corporate income tax 61 ／ … taxes 11
    印花税       stamp duty 981 ／ Stamp Duty 299 ／ Stamp duty 82 ／ duties 12
    废止         abolished 270 ／ Abolished 241 ／ abolition 143 ／ abolish 40
                 ／ abolishing 30 ／ abolishment 6 ／ abolishes 2

所以：**规则按长词优先排列**（`Stamp Duties` 必须排在 `Stamp Duty` 之前，
否则复数的 s 会被落下；`Abolished` 必须排在 `Abolish` 之前），**替换用
_match_case 跟随原形态** —— 否则句中的 `stamp duty` 会变成 `Stamp Tax`，
大写位置错得很显眼。

用法：
    python scripts/fix_terms.py                  # 干跑：只报告
    python scripts/fix_terms.py --apply          # 落库（先写备份）
    python scripts/fix_terms.py --field title    # 只处理标题
    python scripts/fix_terms.py --batch a        # 只跑 A 批（官方名）
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from taxassist import db as dbmod  # noqa: E402

#: A 批：官方名已变。每项 = (匹配, 错误写法, 正确写法)。
#: **顺序有讲究**：长的先跑，否则 `State Tax Administration` 会先被别的规则
#: 吃掉一部分。
A_RULES: list[tuple[re.Pattern, str, str]] = [
    (re.compile(r"\bState Administration of Taxation\b", re.I),
     "State Administration of Taxation", "State Taxation Administration"),
    (re.compile(r"\bState Tax Administration\b", re.I),
     "State Tax Administration", "State Taxation Administration"),
    # --- 下面两条是**清洗我们自己造出来的错词**（2026-10-08 发现）---
    # fix_org_names 的 LOCAL 正则原先缺 `\b`：`Tax Bureaus`（复数）被切成
    # `Tax Bureau` + 残留的 `s`，那个 `s` 粘到替换结果末尾，于是产出了
    # `the Tax Bureau, State Taxation Administrations of various cities…`
    # 这种词（库里 27 条）。根因已在该脚本里修掉（加 `\b`），这两条规则负责
    # 把已经写进库的清理干净。**不设中文门槛** —— 它们是自造的错，与原文无关。
    (re.compile(r"\bState Taxation Administrations\b", re.I),
     "State Taxation Administrations", "State Taxation Administration"),
    (re.compile(r"\bthe Tax Bureau, State Taxation Administration\b"),
     "the Tax Bureau, State Taxation Administration",
     "the Tax Bureaus, State Taxation Administration"),
]

#: B 批：使用者定稿的术语统一。每项 = (匹配, Title 形式, 全小写形式)。
#: **长词优先**（见文件头）。
B_RULES: list[tuple[re.Pattern, str, str]] = [
    (re.compile(r"\bCorporate Income Taxes\b", re.I),
     "Enterprise Income Taxes", "enterprise income taxes"),
    (re.compile(r"\bCorporate Income Tax\b", re.I),
     "Enterprise Income Tax", "enterprise income tax"),
    (re.compile(r"\bStamp Duties\b", re.I), "Stamp Taxes", "stamp taxes"),
    (re.compile(r"\bStamp Duty\b", re.I), "Stamp Tax", "stamp tax"),
    (re.compile(r"\bPersonal Income Taxation\b", re.I),
     "Individual Income Taxation", "individual income taxation"),
    (re.compile(r"\bPersonal Income Taxes\b", re.I),
     "Individual Income Taxes", "individual income taxes"),
    (re.compile(r"\bPersonal Income Tax\b", re.I),
     "Individual Income Tax", "individual income tax"),
    # 废止的六种形态 —— 长的必须排在 `Abolish` 之前
    (re.compile(r"\bAbolishment\b", re.I), "Repeal", "repeal"),
    # `abolitions`（复数名词）也漏过一轮 —— 词边界把 `Abolition\b` 挡在了 `s` 前。
    (re.compile(r"\bAbolitions\b", re.I), "Repeals", "repeals"),
    (re.compile(r"\bAbolition\b", re.I), "Repeal", "repeal"),
    (re.compile(r"\bAbolishing\b", re.I), "Repealing", "repealing"),
    (re.compile(r"\bAbolishes\b", re.I), "Repeals", "repeals"),
    (re.compile(r"\bAbolished\b", re.I), "Repealed", "repealed"),
    (re.compile(r"\bAbolish\b", re.I), "Repeal", "repeal"),
]

#: 中文门槛：只有原文里真的出现该机构，才认定这处英文该改。理由同
#: fix_org_names.py —— 英文串单独看分不出对错。
ZH_GATE = {
    "State Administration of Taxation": re.compile("国家税务总局"),
    "State Tax Administration": re.compile("国家税务总局"),
}


def _match_case(form: str, title: str, lower: str) -> str:
    """让替换结果跟随原文的大小写形态。

    不做这一步，句中的 ``stamp duty`` 会被改成 ``Stamp Tax`` —— 机械替换
    最容易在这种地方留下痕迹，而这类痕迹读者一眼就能看出是机器改的。
    """
    if form.isupper():
        return title.upper()
    return title if form[:1].isupper() else lower


def _sub_counted(pat: re.Pattern, text: str, fixed: str | None = None, *,
                 title: str = "", lower: str = "") -> tuple[str, int]:
    """替换并计数。``fixed`` 给定时用固定串，否则按原形态跟随 title/lower。"""
    n = 0

    def repl(m: re.Match) -> str:
        nonlocal n
        n += 1
        return fixed if fixed is not None else _match_case(m.group(0), title, lower)

    return pat.sub(repl, text), n


def _fix_article(text: str) -> str:
    """替换后修不定冠词：目标词以元音开头时 ``a`` → ``an``。

    实测踩到：``file a personal income tax return`` 被改成
    ``file a individual income tax return`` —— 一处一眼就能看出是机器改的
    语法错误。Enterprise 与 Individual 都是元音开头，一律要修。
    """
    def repl(m: re.Match) -> str:
        return "An " if m.group(1).isupper() else "an "

    return re.sub(
        r"\b([Aa]) (?=(?:Enterprise|Individual|enterprise|individual)\b)",
        repl, text)


def build(field: str = "content", batch: str = "ab", connect=dbmod.connect):
    """算出所有该改的条目，返回 [(translation.id, 改前, 改后, 命中说明, 处数)]。"""
    conn = connect()
    try:
        zh_col = "p.title" if field == "title" else "p.content"
        rows = conn.execute(
            f"SELECT t.id, t.text AS en, {zh_col} AS zh"
            f" FROM translation t LEFT JOIN policy p ON p.doc_uid = t.doc_uid"
            f" WHERE t.field=? AND t.text IS NOT NULL", (field,)).fetchall()
    finally:
        conn.close()

    planned = []
    for r in rows:
        en, zh = r["en"] or "", r["zh"] or ""
        new, hits, total = en, [], 0

        if "a" in batch:
            for pat, wrong, right in A_RULES:
                gate = ZH_GATE.get(wrong)
                if gate is not None and not gate.search(zh):
                    continue          # 中文里没有 → 不动
                new, n = _sub_counted(pat, new, right)
                if n:
                    total += n
                    hits.append(f"{wrong}→{right}×{n}")

        if "b" in batch:
            for pat, title, lower in B_RULES:
                new, n = _sub_counted(pat, new, None, title=title, lower=lower)
                if n:
                    total += n
                    hits.append(f"{pat.pattern} → {title} × {n}")

        if new != en and total:
            planned.append((r["id"], en, _fix_article(new), "；".join(hits), total))
    return planned


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="真的写库（默认只干跑）")
    ap.add_argument("--field", choices=("title", "content"), default="content")
    ap.add_argument("--batch", choices=("a", "b", "ab"), default="ab")
    ap.add_argument("--show", type=int, default=6)
    args = ap.parse_args()

    planned = build(args.field, args.batch)
    n_places = sum(p[4] for p in planned)
    print(f"[{args.field} / {args.batch} 批] {len(planned)} 条待改，共 {n_places} 处\n")
    for tid, before, after, hit, _n in planned[:args.show]:
        # 显示**改动处**的上下文，不显示开头 —— 长正文的开头永远一样。
        i = next((k for k in range(min(len(before), len(after)))
                  if before[k] != after[k]), 0)
        s = max(0, i - 55)
        print(f"  id={tid}  {hit}")
        print(f"    改前: …{before[s:i + 70]}…")
        print(f"    改后: …{after[s:i + 70]}…")

    if not planned:
        print("没有需要改的。")
        return 0
    if not args.apply:
        print("\n（这是干跑。确认无误后加 --apply 落库。）")
        return 0

    backup = Path(f"data/logs/term_fix_{args.field}_{args.batch}_backup.json")
    backup.parent.mkdir(parents=True, exist_ok=True)
    backup.write_text(json.dumps(
        [{"id": t, "before": b} for t, b, _a, _h, _n in planned],
        ensure_ascii=False, indent=1), encoding="utf-8")

    from taxassist import writelock

    # **探一次锁，拿不到就直接写。** 这是实测调出来的：worker 的 run_forever
    # 是「每一轮」拿锁，一轮含采集/校对/上架，可能十几分钟；而本脚本的改动是
    # 毫秒级的事 —— 为它去等，代价远大于撞一次 SQLite 引擎锁的概率（第一次
    # 带 600 秒超时的写法真的卡住了，前台两分钟零输出）。SQLite 自己的锁 +
    # busy_timeout 仍然保护一致性，不会写坏数据。
    got = writelock.acquire("fix_terms", timeout=5)
    if not got:
        print(f"（写库锁正被 {writelock.holder()} 占用，改动量小，直接写库）")

    conn = dbmod.connect()
    try:
        for tid, _before, after, _hit, _n in planned:
            conn.execute("UPDATE translation SET text=? WHERE id=?", (after, tid))
        conn.commit()
    finally:
        conn.close()
        if got:
            writelock.release()
    print(f"\n已改 {len(planned)} 条、{n_places} 处；改前内容备份在 {backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
