"""把审校台上导出的判定结果入库 —— 让「谁核过」这件事可追溯。

======================================================================
为什么非要入库
======================================================================

``build_review_html.py`` 生成的是**只读的对照页**，判定存在浏览器本地。
这个脚本负责最后一跳：把导出的 JSON 收进库，落到 ``translation_review`` 表。

使用者说过「进正式文件需要这个追溯」。浏览器本地存储做不到追溯 —— 换台
机器就没了，也没法回答「这条是谁在什么时候核的、核的时候机器报的是什么」。

**幂等**：同一个 (doc_uid, field) 重复导入是**覆盖**，不是插两条 —— 否则
「这条到底核过几次、以哪次为准」会变成新的问题。

**留下机器当时的判据**（kind 列）：人判定之后，回头看「当时机器为什么挑它」
是有用的 —— 尤其当人判了「通过」而机器报了错时，那说明机器在这一类上误报，
是检查器的改进线索。

用法：
    python scripts/import_review.py --file review_content.json --reviewer 安永
    python scripts/import_review.py --file review_content.json --dry-run
    python scripts/import_review.py --stats
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from taxassist import db as dbmod  # noqa: E402


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS translation_review ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " doc_uid TEXT NOT NULL,"
        " field TEXT NOT NULL,"
        " verdict TEXT NOT NULL,"          # ok（通过）/ bad（有问题）
        " note TEXT,"
        " reviewer TEXT,"
        " kind TEXT,"                      # 机器当时挑它的类别
        " reviewed_at TEXT NOT NULL,"
        " UNIQUE(doc_uid, field))")
    conn.commit()


def load_kinds(field: str) -> dict[str, str]:
    """从机检报告取「机器当时为什么挑这条」，作为上下文一并存下。"""
    out: dict[str, str] = {}
    p = Path("data/logs/audit_translation.json")
    if not p.exists():
        return out
    try:
        hits = json.loads(p.read_text(encoding="utf-8")).get(field, {}).get("hits", {})
    except Exception:  # noqa: BLE001
        return out
    for kind, lst in hits.items():
        for h in lst:
            out.setdefault(h["uid"], kind)
    return out


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--file", help="审校台导出的 JSON")
    ap.add_argument("--field", choices=("title", "content"), default="content")
    ap.add_argument("--reviewer", default="", help="谁核的（写进库，供追溯）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--stats", action="store_true")
    args = ap.parse_args()

    conn = dbmod.connect()
    try:
        ensure_table(conn)
        if args.stats or not args.file:
            rows = conn.execute(
                "SELECT field, verdict, COUNT(*) n FROM translation_review"
                " GROUP BY field, verdict ORDER BY field, verdict").fetchall()
            if not rows:
                print("还没有任何人工判定记录。")
                return 0
            for r in rows:
                print(f"  {r['field']:8} {r['verdict']:4} {r['n']} 条")
            tot = conn.execute(
                "SELECT COUNT(*) FROM translation_review WHERE verdict='bad'"
            ).fetchone()[0]
            print(f"\n判定为「有问题」的共 {tot} 条 —— 这些是需要修的。")
            return 0

        src = Path(args.file)
        if not src.exists():
            print(f"找不到 {src}")
            return 1
        data = json.loads(src.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            print("文件格式不对：应当是审校台导出的数组")
            return 1

        kinds = load_kinds(args.field)
        now = dt.datetime.now().isoformat(timespec="seconds")
        ok = bad = 0
        for r in data:
            uid, verdict = r.get("uid"), r.get("verdict")
            if not uid or verdict not in ("ok", "bad"):
                continue
            if verdict == "ok":
                ok += 1
            else:
                bad += 1
            if args.dry_run:
                continue
            conn.execute(
                "INSERT INTO translation_review"
                " (doc_uid, field, verdict, note, reviewer, kind, reviewed_at)"
                " VALUES (?,?,?,?,?,?,?)"
                " ON CONFLICT(doc_uid, field) DO UPDATE SET"
                "   verdict=excluded.verdict, note=excluded.note,"
                "   reviewer=excluded.reviewer, reviewed_at=excluded.reviewed_at",
                (uid, args.field, verdict, r.get("note", ""), args.reviewer,
                 kinds.get(uid, ""), r.get("at") or now))
        if not args.dry_run:
            conn.commit()
        verb = "将导入" if args.dry_run else "已导入"
        print(f"{verb} {ok + bad} 条判定（通过 {ok} ／ 有问题 {bad}）"
              f"  reviewer={args.reviewer or '（未填）'}")
        if bad and not args.dry_run:
            print("\n判定为「有问题」的条目：")
            for r in data:
                if r.get("verdict") == "bad":
                    print(f"  · {r['uid'][:52]}  {str(r.get('note', ''))[:80]}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
