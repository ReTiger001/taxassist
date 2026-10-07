"""把机检命中的条目渲染成可读的对照清单，供人工验伪。

======================================================================
为什么需要它
======================================================================

机检只负责「指出可疑」，判定对错**必须有人看中文原文** —— 这是本次质量工作
里反复验证的一条：检查器自己就被推翻过五次，最高一次误报 100%（A5 那 455 条）。
但人工看也需要载体：把 JSON 报告直接丢给人，等于什么都没做。

所以这里把命中条目渲染成**带原文对照的 Markdown**：每条嫌疑点旁边就是中文
原文的对应句子，人只需要判断「译文这一处对不对」。

一条刻意的设计：**按类别分别渲染**，因为不同类别的判据不一样 ——
- A4（数字缺失）：要的是「原文那句写了什么数、译文里有没有」
- A9（模型自述）：要的是「这句话是不是模型在说话，而不是政策内容」
- A3（疑似截断）：要的是「看结尾，是不是话说一半」

用法：
    python scripts/show_suspects.py --kinds A4 --limit 40
    python scripts/show_suspects.py --kinds A3,A4,A9
    python scripts/show_suspects.py --field title --kinds A1,A4
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from audit_translation import ENTITY  # noqa: E402

from taxassist import db as dbmod  # noqa: E402


def zh_sentence_with(zh: str, token: str, width: int = 170) -> str:
    """原文里含该数字的句子（截断）。**必须先剥实体** —— `&#8203;` 会被当成
    数字 8203，那是检查器踩过的坑，渲染时不能重蹈。"""
    if not token:
        return "（空）"
    for sent in re.split(r"[。；\n]", ENTITY.sub(" ", zh or "")):
        if token in sent:
            s = sent.strip()
            return s if len(s) <= width else s[:width] + "…"
    return "（原文里找不到该数字？）"


def en_contexts(en: str, token: str, width: int = 110, limit: int = 2) -> list[str]:
    """译文里该数字出现的上下文；没有就是没有。"""
    out = []
    for m in list(re.finditer(re.escape(token), en or ""))[:limit]:
        i = m.start()
        out.append("…" + en[max(0, i - width):i + width].replace("\n", " ") + "…")
    return out


def render(kind: str, h: dict, zh: str, en: str) -> list[str]:
    """按类别渲染一条。"""
    out = []
    if kind == "A4":
        for tok in (h.get("missing") or [])[:3]:
            out.append(f"\n**缺 `{tok}`**\n\n- 原文：{zh_sentence_with(zh, tok)}")
            ctx = en_contexts(en, tok)
            if ctx:
                out.append(f"- 译文里其实有（可能是写法不同）：{ctx[0]}")
            else:
                out.append("- 译文里**完全找不到**这个数")
    elif kind == "A9":
        out.append(f"\n译文里的自述：`{h.get('detail', '')}`")
        m = re.search(re.escape(str(h.get("detail", ""))[:12]), en or "")
        if m:
            i = m.start()
            out.append(f"\n上下文：…{en[max(0, i - 120):i + 160]}…".replace("\n", " "))
    elif kind == "A3":
        out.append(f"\n译文结尾：\n\n```\n…{h.get('tail', '')}\n```")
    elif kind == "A1":
        out.append(f"\n残留汉字：`{h.get('detail', '')}`（{h.get('n_han')} 个）")
    elif kind in ("A7", "A8"):
        out.append(f"\n{h}")
    elif kind == "A5":
        out.append(f"\n重复片段：`{h.get('detail', '')}`")
    else:
        out.append(f"\n{h}")
    return out


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--field", choices=("title", "content"), default="content")
    ap.add_argument("--kinds", default="A4", help="逗号分隔，如 A3,A4,A9")
    ap.add_argument("--limit", type=int, default=30, help="每类最多列几条")
    ap.add_argument("--out", default="data/logs/translation_suspects.md")
    args = ap.parse_args()

    rep_path = Path("data/logs/audit_translation.json")
    rep = json.loads(rep_path.read_text(encoding="utf-8")).get(args.field, {})
    hits = rep.get("hits", {})

    conn = dbmod.connect()
    zh_col = "p.title" if args.field == "title" else "p.content"
    lines = [
        f"# 机检嫌疑清单 · {args.field}",
        "",
        "> **机检会误报，判定必须回到中文原文。** 本轮实测里检查器被推翻过五次，",
        "> 最高一次误报 100%（A5 那 455 条）。这份清单是**待判定的嫌疑**，不是结论。",
        "",
        f"生成自 `{rep_path}`（类别：{args.kinds}）",
    ]
    total = 0
    for kind in args.kinds.split(","):
        lst = hits.get(kind, [])
        if not lst:
            continue
        show = lst[:args.limit]
        lines.append(f"\n---\n\n## {kind}（共 {len(lst)} 条，列出 {len(show)} 条）")
        total += len(show)
        for h in show:
            r = conn.execute(
                f"SELECT t.text AS en, {zh_col} AS zh FROM translation t"
                f" LEFT JOIN policy p ON p.doc_uid=t.doc_uid"
                f" WHERE t.doc_uid=? AND t.field=?", (h["uid"], args.field)).fetchone()
            if not r:
                continue
            lines.append(f"\n### `{h['uid'][:52]}`")
            lines.extend(render(kind, h, r["zh"] or "", r["en"] or ""))
    conn.close()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"已写出 {out}：{total} 条嫌疑（{len(lines)} 行）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
