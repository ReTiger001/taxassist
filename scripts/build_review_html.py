"""把机检 + 语义校验的嫌疑渲染成一份自包含 HTML，供人工逐条过。

======================================================================
为什么是单文件 HTML，而不是一个页面
======================================================================

审校台的全部需求是「把中英对照和机器标记摆在一起，让人能快速读，并把判定
记下来」。为这个起一个 Web 服务、加路由、加登录 —— 成本远大于收益，而且
它要连生产库。

一个自包含的 HTML：双击就打开、离线可用、判定存在浏览器本地（可导出 JSON
交回来）、**不碰生产库一个字节**。等真的需要「签核入库、可追溯」时再升级，
那时需求也已经清楚多了。

======================================================================
一份清单，两个来源
======================================================================

  data/logs/audit_translation.json   机检 10 类（数字、条目、结构、残留汉字…）
  data/logs/semantic_check_*.json    语义校验的判定（「问题：…」那些）

**合成一份、按可信度排序**：语义嫌疑排在前面 —— 那些是机检完全看不见的，
而且已经被人读过的比例最低。

用法：
    python scripts/build_review_html.py --limit 120
    python scripts/build_review_html.py --field title --limit 60
"""
from __future__ import annotations

import argparse
import html
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from taxassist import db as dbmod  # noqa: E402

#: 让读的人知道「机器为什么把这条挑出来」。没有这句，卡片上就只有一个代号，
#: 读的人得先学会 A4/A7/A8 是什么意思 —— 那是把机器的语言转嫁给人。
KIND_DESC = {
    "语义": ("语义存疑", "大模型认为译文没有准确表达原文 —— 这类机检完全看不见"),
    "A2": ("译文过短", "译文比原文短太多，可能整段被概括掉了"),
    "A7": ("条目丢失", "原文的条目数远多于译文，清单可能被概括"),
    "A8": ("数字成批消失", "原文的数字个数远多于译文"),
    "A9": ("混入模型的话", "译文里出现了「由于文本较长…请提供全文」这类自述"),
    "A1": ("残留汉字", "译文里还剩汉字，说明这里漏译了"),
    "B1": ("机构名被拆开", "一个机关名可能被译成了两个"),
    "A4": ("数字缺失", "原文的数字在译文里找不到（也可能只是单位换了写法）"),
    "A3": ("疑似截断", "正文结尾似乎话没说完"),
}
#: 排在前面 = 优先给人看。语义最优先（机检看不见、且最少被人看过）。
ORDER = ("语义", "A2", "A7", "A8", "A9", "A1", "B1", "A4", "A3", "C1")


def collect(field: str) -> list[dict]:
    out: dict[tuple[str, str], dict] = {}

    rep_path = Path("data/logs/audit_translation.json")
    if rep_path.exists():
        hits = json.loads(rep_path.read_text(encoding="utf-8")).get(field, {}).get("hits", {})
        for kind, lst in hits.items():
            for h in lst:
                why = (h.get("missing") or h.get("detail") or h.get("tail")
                       or h.get("zh_items") or h.get("note") or "")
                out[(h["uid"], kind)] = {"uid": h["uid"], "kind": kind,
                                         "why": str(why)[:160]}

    for p in Path("data/logs").glob("semantic_check_*.json"):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - 结果文件坏了不该让整件事跑不起来
            continue
        if d.get("field") != field:
            continue
        for r in d.get("results", []):
            if r.get("ok"):
                continue
            out[(r["uid"], "语义")] = {"uid": r["uid"], "kind": "语义",
                                       "why": str(r.get("answer", ""))[:300]}

    items = list(out.values())
    items.sort(key=lambda x: (ORDER.index(x["kind"])
                              if x["kind"] in ORDER else 99, x["uid"]))
    return items


def render(items: list[dict], field: str) -> str:
    conn = dbmod.connect()
    zh_col = "p.title" if field == "title" else "p.content"
    cards = []
    for it in items:
        r = conn.execute(
            f"SELECT t.text AS en, {zh_col} AS zh FROM translation t"
            f" LEFT JOIN policy p ON p.doc_uid = t.doc_uid"
            f" WHERE t.doc_uid=? AND t.field=?", (it["uid"], field)).fetchone()
        if not r:
            continue
        label, desc = KIND_DESC.get(it["kind"], (it["kind"], ""))
        cards.append(f"""<article class="card" data-uid="{html.escape(it['uid'])}" data-kind="{it['kind']}">
  <header>
    <span class="kind">{html.escape(label)}</span>
    <code>{html.escape(it['uid'])}</code>
  </header>
  <p class="why"><b>机器为什么挑出它：</b>{html.escape(desc)}
     <span class="raw">命中值：{html.escape(it['why'])}</span></p>
  <div class="cols">
    <section><h4>中文原文</h4><pre>{html.escape(r['zh'] or '')}</pre></section>
    <section><h4>英文译文（机器翻译）</h4><pre>{html.escape(r['en'] or '')}</pre></section>
  </div>
  <footer>
    <button class="ok">✓ 通过</button>
    <button class="bad">✗ 有问题</button>
    <input class="note" placeholder="备注（有问题时写一句，可选）">
    <span class="state"></span>
  </footer>
</article>""")
    conn.close()
    return "\n".join(cards)


PAGE = """<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<title>翻译审校台 · {field}</title>
<style>
:root {{ --fg:#1a1a1a; --mut:#6b7280; --line:#e5e7eb; --ok:#0a7d40; --bad:#b42318; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; font:15px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;
        color:var(--fg); background:#f6f7f9; }}
header.top {{ position:sticky; top:0; z-index:9; background:#fff; border-bottom:1px solid var(--line);
        padding:12px 20px; display:flex; gap:16px; align-items:center; flex-wrap:wrap; }}
header.top h1 {{ font-size:16px; margin:0; }}
header.top .stat {{ color:var(--mut); font-size:13px; }}
header.top button {{ padding:6px 14px; border:1px solid var(--line); background:#fff;
        border-radius:6px; cursor:pointer; }}
.wrap {{ max-width:1500px; margin:0 auto; padding:16px 20px 60px; }}
.card {{ background:#fff; border:1px solid var(--line); border-radius:10px;
        margin:0 0 18px; overflow:hidden; }}
.card.done-ok {{ border-left:4px solid var(--ok); }}
.card.done-bad {{ border-left:4px solid var(--bad); }}
.card header {{ padding:10px 16px; border-bottom:1px solid var(--line);
        display:flex; gap:12px; align-items:center; }}
.kind {{ background:#eef2ff; color:#3730a3; padding:2px 10px; border-radius:99px;
        font-size:12px; font-weight:600; }}
code {{ color:var(--mut); font-size:12px; }}
.why {{ margin:0; padding:10px 16px; background:#fafafa; border-bottom:1px solid var(--line);
        font-size:13px; }}
.why .raw {{ color:var(--mut); }}
.cols {{ display:grid; grid-template-columns:1fr 1fr; gap:1px; background:var(--line); }}
.cols section {{ background:#fff; padding:12px 16px; min-width:0; }}
.cols h4 {{ margin:0 0 8px; font-size:12px; color:var(--mut); font-weight:600; }}
pre {{ margin:0; white-space:pre-wrap; word-break:break-word; max-height:420px;
        overflow:auto; font:13px/1.65 ui-monospace,Consolas,monospace; }}
footer {{ padding:10px 16px; border-top:1px solid var(--line); display:flex; gap:10px;
        align-items:center; background:#fcfcfd; }}
footer button {{ padding:6px 16px; border-radius:6px; cursor:pointer; font-size:14px;
        border:1px solid var(--line); background:#fff; }}
footer button.ok.on {{ background:var(--ok); color:#fff; border-color:var(--ok); }}
footer button.bad.on {{ background:var(--bad); color:#fff; border-color:var(--bad); }}
footer .note {{ flex:1; min-width:120px; padding:6px 10px; border:1px solid var(--line);
        border-radius:6px; font-size:13px; }}
.state {{ font-size:12px; color:var(--mut); }}
@media (max-width:900px) {{ .cols {{ grid-template-columns:1fr; }} }}
</style></head><body>
<header class="top">
  <h1>翻译审校台 · {field}</h1>
  <span class="stat" id="stat"></span>
  <span style="flex:1"></span>
  <button id="only-todo">只看未判定</button>
  <button id="export">导出判定结果</button>
</header>
<div class="wrap" id="wrap">
{cards}
</div>
<script>
const KEY = 'taxassist_review_{field}';
const store = JSON.parse(localStorage.getItem(KEY) || '{{}}');
function paint() {{
  let done = 0, todo = 0;
  document.querySelectorAll('.card').forEach(c => {{
    const u = c.dataset.uid, v = store[u];
    c.classList.remove('done-ok','done-bad');
    c.querySelector('.ok').classList.toggle('on', v && v.verdict === 'ok');
    c.querySelector('.bad').classList.toggle('on', v && v.verdict === 'bad');
    c.querySelector('.note').value = (v && v.note) || '';
    c.querySelector('.state').textContent = v ? '已判定' : '';
    v ? done++ : todo++;
  }});
  document.getElementById('stat').textContent = `共 ${{done + todo}} 条，已判定 ${{done}}，待判定 ${{todo}}`;
}}
function set(card, verdict) {{
  const u = card.dataset.uid;
  const note = card.querySelector('.note').value;
  const cur = store[u];
  if (cur && cur.verdict === verdict) {{ delete store[u]; }}
  else {{ store[u] = {{ verdict, note, at: new Date().toISOString() }}; }}
  localStorage.setItem(KEY, JSON.stringify(store));
  paint();
}}
document.querySelectorAll('.card').forEach(c => {{
  c.querySelector('.ok').onclick = () => set(c, 'ok');
  c.querySelector('.bad').onclick = () => set(c, 'bad');
  c.querySelector('.note').onchange = () => {{
    if (store[c.dataset.uid]) set(c, store[c.dataset.uid].verdict);
  }};
}});
document.getElementById('export').onclick = () => {{
  const rows = Object.entries(store).map(([uid, v]) => ({{ uid, ...v }}));
  const blob = new Blob([JSON.stringify(rows, null, 1)], {{ type: 'application/json' }});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'review_{field}.json';
  a.click();
}};
document.getElementById('only-todo').onclick = (e) => {{
  const on = e.target.classList.toggle('on');
  const bg = on ? '#fff' : '';
  document.querySelectorAll('.card').forEach(c => {{
    c.style.display = (on && store[c.dataset.uid]) ? 'none' : '';
  }});
}};
paint();
</script></body></html>"""


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--field", choices=("title", "content"), default="content")
    ap.add_argument("--limit", type=int, default=120)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    items = collect(args.field)[:args.limit]
    cards = render(items, args.field)
    out = Path(args.out or f"data/logs/review_{args.field}.html")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(PAGE.format(field=args.field, cards=cards), encoding="utf-8")
    print(f"已生成 {out}：{len(items)} 条待审（{out.stat().st_size / 1024:.0f} KB）")
    print("双击打开即可；判定存在浏览器本地，「导出判定结果」会下载 JSON。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
