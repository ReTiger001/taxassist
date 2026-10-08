"""定点重译：把机检标出的条目用当前参数重新译一遍，并当场复检。

======================================================================
为什么必须是单独一个工具
======================================================================

``translation`` 表按**原文指纹**缓存（见 translate_llm 头部）：原文没变就永不
重译。这是对的 —— 否则中断一次就白干几十小时。但它有个直接后果：
**发现译文有错之后，没有办法重译它**，只能手工去删记录。

这就是「闭环」缺的那一环。2026-10-07 修机构名时是人工删的；这次 554 条存量
不可能再这么干。

======================================================================
三个刻意的设计
======================================================================

1. **从机检报告取目标，不手写清单。** 目标是 ``audit_translation.json`` 里
   的命中条目，按类别可筛。这样「查出来的」和「要修的」永远是同一批 ——
   手抄一份清单出来，两边迟早对不上。

2. **改前先备份**（``data/logs/retranslate_<field>_backup.json``）。重译是
   覆盖写，译文本身可能比原来更差（`A/B 实测`里就有这种例子：同一配置重跑
   结果不一致）。没有备份就等于把旧译文直接扔掉。

3. **译完当场复检**，输出「修好 / 没修好」的对照。只重译不验证，等于把
   「有多少条修好了」又变回未知 —— 那正是这轮工作要消灭的东西。

用法：
    python scripts/retranslate.py --dry-run              # 只报告要重译多少条
    python scripts/retranslate.py                        # 默认：切块相关的三类
    python scripts/retranslate.py --kinds A1,A9,A5       # 指定类别
    python scripts/retranslate.py --kinds all --field title
    python scripts/retranslate.py --limit 20             # 先试 20 条看效果
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from audit_translation import (  # noqa: E402
    HAN,
    has_meta_talk,
    lost_items,
    missing_numbers,
    number_drop,
    repeated_sentences,
)

from taxassist import db as dbmod  # noqa: E402
from taxassist import translate_llm as tl  # noqa: E402

#: 默认重译哪几类。切块根因已修（split_chunks），所以 A2/A7/A8 是**最可能被
#: 重译修好的**；A1/A9/A5 是模型自身的退化，也值得一试。A4/A3 不在此列 ——
#: A4 已知含假阳性、A3 还没验伪，先把它们留在报告里做人审，别拿重译去赌。
DEFAULT_KINDS = ("A2", "A7", "A8", "A1", "A9", "A5")


def check(zh: str, en: str, field: str) -> dict:
    """跑一遍机检，返回这一条的命中类别（与 audit_translation 同一套判据）。"""
    out: dict[str, object] = {}
    if HAN.search(en or ""):
        out["A1"] = len(HAN.findall(en))
    strong, _weak = missing_numbers(zh, en)
    if strong:
        out["A4"] = strong[:4]
    if field == "content":
        li = lost_items(zh, en)
        if li:
            out["A7"] = li
        nd = number_drop(zh, en)
        if nd:
            out["A8"] = nd
    if repeated_sentences(en):
        out["A5"] = len(repeated_sentences(en))
    mt = has_meta_talk(en)
    if mt:
        out["A9"] = mt
    return out


def pick(field: str, kinds: tuple[str, ...], from_ai: bool = False,
         connect=dbmod.connect) -> dict[str, set[str]]:
    """取目标：doc_uid → 命中的类别集合。

    ``from_ai=True`` 时改为从**本地模型的审核结果**取（verdict='issue' 那些）——
    这是「先翻译、然后本地模型审核」那一环的出口：模型明确说有问题的，直接进
    重译队列。语义类的问题机检看不见，只有这条路能自动修。
    """
    if from_ai:
        conn = connect()
        try:
            rows = conn.execute(
                "SELECT doc_uid FROM translation_ai_review"
                " WHERE field=? AND verdict='issue'", (field,)).fetchall()
        finally:
            conn.close()
        return {r["doc_uid"]: {"AI:issue"} for r in rows}

    rep_path = Path("data/logs/audit_translation.json")
    rep = json.loads(rep_path.read_text(encoding="utf-8")).get(field, {})
    hits = rep.get("hits", {})
    want = set(hits) if kinds == ("all",) else set(kinds)
    out: dict[str, set[str]] = {}
    for k, lst in hits.items():
        if k not in want:
            continue
        for h in lst:
            out.setdefault(h["uid"], set()).add(k)
    return out


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--field", choices=("title", "content"), default="content")
    ap.add_argument("--kinds", default=",".join(DEFAULT_KINDS),
                    help="要重译的类别，逗号分隔；all = 全部")
    ap.add_argument("--limit", type=int, default=0, help="0 = 全部")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--from-ai", action="store_true",
                    help="从 AI 审核结果取目标（verdict=issue），而不是机检报告")
    args = ap.parse_args()

    kinds = tuple(args.kinds.split(",")) if args.kinds != "all" else ("all",)
    targets = pick(args.field, kinds, from_ai=args.from_ai)
    if args.limit:
        targets = dict(list(targets.items())[:args.limit])
    print(f"[{args.field}] 类别 {','.join(kinds)} → {len(targets)} 条待重译")
    if not targets:
        return 0
    if args.dry_run:
        for uid, ks in list(targets.items())[:10]:
            print(f"  {uid[:48]:<48} {sorted(ks)}")
        return 0

    ready, why = tl.is_available(tl.DEFAULT_MODEL)
    print(f"模型：{why}")
    if not ready:
        return 1

    # **必须先拿到写库锁再开工。** 第一版没拿锁、直接写库 —— 结果第一条就崩于
    # `sqlite3.OperationalError: database is locked`：worker 的采集一轮能持有写锁
    # 几十分钟，而 SQLite 的 busy_timeout 只等 120 秒。项目里 translate_batch.py
    # 早就这么处理了（拿不到锁就退出本轮，不硬撞引擎锁）。
    from taxassist import writelock

    if not writelock.acquire("retranslate", timeout=30):
        print(f"写库锁被 {writelock.holder()} 占用 —— 重译要持续写库，"
              f"先停 worker 再跑：python -m taxassist worker --stop")
        return 1
    print("已取得写库锁", flush=True)

    zh_col = "p.title" if args.field == "title" else "p.content"
    conn = dbmod.connect()
    rows = {r["doc_uid"]: r["zh"] for r in conn.execute(
        f"SELECT p.doc_uid, {zh_col} AS zh FROM policy p WHERE p.doc_uid IN"
        f" ({','.join('?' * len(targets))})", list(targets))}
    old = {r["doc_uid"]: r["text"] for r in conn.execute(
        "SELECT doc_uid, text FROM translation WHERE field=? AND doc_uid IN"
        f" ({','.join('?' * len(targets))})", (args.field, *targets))}
    conn.close()

    backup = Path(f"data/logs/retranslate_{args.field}_backup.json")
    backup.parent.mkdir(parents=True, exist_ok=True)
    # **开工前先把全部旧译文落盘**。只在中途结束时写是不够的 —— 重译是几小时
    # 的长任务，中途被杀意味着旧译文已被覆盖、而备份还不存在。备份的意义就是
    # 「随时可以退回去」，那它必须在第一处覆盖发生之前就存在。
    backup.write_text(json.dumps(
        [{"uid": u, "before": old.get(u)} for u in sorted(targets)],
        ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"旧译文已备份（{len(targets)} 条）→ {backup}", flush=True)

    # **断点续跑**：354 条要跑几小时，中途停一次就从零重来是不可接受的。
    # 每处理完一条就把结果增量写进 result.json，重跑时按 uid 跳过。
    result_path = Path(f"data/logs/retranslate_{args.field}_result.json")
    results: list[dict] = []
    if result_path.exists():
        try:
            results = json.loads(result_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - 结果文件坏了就当没有，别把整轮卡死
            results = []
    done_uids = {r["uid"] for r in results}
    if done_uids:
        print(f"续跑：前面已完成 {len(done_uids)} 条，跳过", flush=True)
    fixed = sum(1 for r in results if len(r["after"]) < len(r["before"]))
    same = sum(1 for r in results if len(r["after"]) == len(r["before"]))
    worse = sum(1 for r in results if len(r["after"]) > len(r["before"]))
    failed = 0
    t0 = time.time()
    for i, (uid, ks) in enumerate(sorted(targets.items()), 1):
        zh = rows.get(uid) or ""
        if not zh.strip():
            continue
        try:
            if args.field == "title":
                new = tl.translate(zh, model=tl.DEFAULT_MODEL)
            else:
                new = tl.translate_long(zh, model=tl.DEFAULT_MODEL)
        except Exception as e:  # noqa: BLE001 - 单条失败不该中断整批
            failed += 1
            print(f"  [{i}/{len(targets)}] 失败 {uid[:40]}: {type(e).__name__}: {e}",
                  flush=True)
            continue

        before = check(zh, old.get(uid) or "", args.field)
        after = check(zh, new, args.field)
        nb, na = len(before), len(after)
        if na < nb:
            fixed += 1
        elif na > nb:
            worse += 1
        else:
            same += 1
        # 落库（覆盖）。**只在这一条真的更好或持平的时候才写** —— 重译结果可能
        # 更差（温度 0.7 的随机性，A/B 实测见过），那种情况宁可不写。
        if na <= nb:
            try:
                conn = dbmod.connect()
                try:
                    tl.save(conn, uid, args.field, zh, new, model=tl.DEFAULT_MODEL)
                finally:
                    conn.close()
            except Exception as e:  # noqa: BLE001 - 单条落库失败不该中断整批
                failed += 1
                print(f"  [{i}/{len(targets)}] 落库失败 {uid[:36]}: "
                      f"{type(e).__name__}: {e}", flush=True)
                continue
        # 落库成功之后才记结果 —— 顺序反了的话，落库失败仍会留下「已处理」的
        # 记录，续跑时就被跳过了，那条错误会被永久留在库里。
        results.append({"uid": uid, "kinds": sorted(ks), "before": before,
                        "after": after, "zh_len": len(zh),
                        "en_old_len": len(old.get(uid) or ""), "en_new_len": len(new)})
        result_path.write_text(json.dumps(results, ensure_ascii=False, indent=1),
                               encoding="utf-8")
        print(f"  [{i}/{len(targets)}] {uid[:40]:<40} {nb} → {na} "
              f"{'✓' if na < nb else ('=' if na == nb else '✗未落库')} "
              f"{len(zh)}字 → {len(new)}字符", flush=True)

    # 结果单独一个文件 —— 别覆盖掉上面那份**改前**备份（那个是回滚用的）
    Path(f"data/logs/retranslate_{args.field}_result.json").write_text(json.dumps(
        results, ensure_ascii=False, indent=1), encoding="utf-8")
    el = (time.time() - t0) / 60
    print(f"\n完成：{len(results)} 条，用时 {el:.1f} 分钟")
    print(f"  修好 {fixed} 条 ／ 持平 {same} 条 ／ 更差 {worse} 条（未落库）／ 失败 {failed} 条")
    print(f"  改前译文备份在 {backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
