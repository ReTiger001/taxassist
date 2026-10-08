"""实时监控台：翻译在做什么、进度到哪了，一眼看到。

======================================================================
为什么需要它
======================================================================

工作流的子步骤输出写进了 ``data/logs/wf_<tag>.log``，但**没人会去 tail 它** ——
而且一轮要一百分钟，其间「正在译第几条、还要多久」是完全合理的疑问。光看
``auto_workflow.json`` 也不行：它**每轮结束才写一次**，照它看只能得到
「第 N 轮进行中」。

这个脚本每几秒重绘一屏，把三处信息拼起来：

    data/logs/auto_workflow_state.json   当前在哪一步（工作流每步开始时写）
    data/logs/wf_*.log                   本步的真实输出（含进度行）
    taxassist.db                         总进度、AI 审核统计、最近译文

**只读**：不写库、不改工作流的任何文件，关掉它不影响干活。

用法：
    python scripts/watch.py               # 常驻刷新（.bat 调的就是它）
    python scripts/watch.py --once        # 只画一屏（调试/截图）
    python scripts/watch.py --interval 5  # 改刷新间隔
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from taxassist import db as dbmod  # noqa: E402

LOGS = ROOT / "data" / "logs"
STATE = LOGS / "auto_workflow_state.json"
RUN_LOG = LOGS / "auto_workflow.log"

#: translate_batch 的进度行长这样（见该脚本的 print）：
#:   已译 47  跳过 0  失败 1  3.20 条/秒  预计剩余 32 分钟
PROG = re.compile(r"已译\s*(\d+)\s+跳过\s*(\d+)\s+失败\s*(\d+)\s+"
                  r"([\d.]+)\s*条/秒\s+预计剩余\s*(\d+)\s*分钟")
#: ai_review 的进度行：  [12/150] OK  uid 说明
AI = re.compile(r"^\[(\d+)/(\d+)\]\s+(\S+)\s+(\S+)")
W = 74


def _read_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _tail_lines(p: Path, n: int) -> list[str]:
    try:
        return [x for x in p.read_text(encoding="utf-8", errors="replace")
                .splitlines() if x.strip()][-n:]
    except Exception:  # noqa: BLE001
        return []


def _bar(done: int, total: int, width: int = 34) -> str:
    if total <= 0:
        return "─" * width
    n = min(width, round(done / total * width))
    return "█" * n + "─" * (width - n)


def step_progress() -> tuple[str, str]:
    """本步进度：优先认翻译，其次认 AI 审核。返回 (描述, 明细行)。"""
    text = "\n".join(_tail_lines(LOGS / "wf_translate.log", 40))
    hits = PROG.findall(text)
    if hits:
        done, skip, fail, rate, left = hits[-1]
        line = (f"{done} 条已译 · {skip} 跳过 · {fail} 失败 · "
                f"{rate} 条/秒 · 预计还需 {left} 分钟")
        last = _tail_lines(LOGS / "wf_translate.log", 1)
        return line, (last[0].strip()[:W] if last else "")

    text = "\n".join(_tail_lines(LOGS / "wf_ai_review.log", 60))
    for ln in reversed(text.splitlines()):
        m = AI.match(ln.strip())
        if m:
            return (f"AI 审核 {m.group(1)} / {m.group(2)} 条",
                    ln.strip()[:W])
    return "", ""


def draw() -> None:
    if os.name == "nt":
        sys.stdout.write("\033[2J\033[H")   # 清屏 + 光标归位（不用 cls，闪得厉害）
    else:
        sys.stdout.write("\033[2J\033[H")

    now = dt.datetime.now()
    st = _read_json(STATE)
    conn = dbmod.connect()
    try:
        tot = conn.execute("SELECT COUNT(*) FROM translation WHERE field='content'").fetchone()[0]
        pol = conn.execute(
            "SELECT COUNT(*) FROM policy WHERE IFNULL(content,'')<>''").fetchone()[0]
        today = conn.execute(
            "SELECT COUNT(*) FROM translation WHERE field='content'"
            " AND created_at >= ?", (now.strftime("%Y-%m-%dT00:00"),)).fetchone()[0]
        try:
            ai = {r["verdict"]: r["n"] for r in conn.execute(
                "SELECT verdict, COUNT(*) n FROM translation_ai_review"
                " GROUP BY verdict")}
        except Exception:  # noqa: BLE001 - 表还没建过
            ai = {}
        recent = conn.execute(
            "SELECT substr(created_at,12,5) t, doc_uid,"
            " LENGTH(text) en FROM translation WHERE field='content'"
            " ORDER BY created_at DESC LIMIT 5").fetchall()
    finally:
        conn.close()

    pct = tot / pol * 100 if pol else 0
    print("═" * W)
    print(f"  税务知识助手 · 翻译工作台{'':>{W - 34}}{now:%H:%M:%S}")
    print("═" * W)
    print()

    if st:
        round_no = st.get("round") or "?"
        print(f"  工作流   第 {round_no} 轮 · {st.get('step', '?')}"
              f"{'  ' + st['detail'] if st.get('detail') else ''}")
        print(f"           该步骤开始于 {st.get('at', '?')[11:19]}")
    else:
        print("  工作流   未在运行（state 文件为空）")
    print()

    desc, detail = step_progress()
    if desc:
        print(f"  本步进度 {desc}")
        if detail:
            print(f"           {detail}")
    else:
        print("  本步进度 （这一步没有进度行，看下面日志尾部）")
    print()

    print("─" * W)
    print(f"  总进度   正文 {tot} / {pol}（{pct:.1f}%）   今日新增 {today}")
    print(f"           {_bar(tot, pol)}")
    if ai:
        ok = ai.get("ok", 0)
        bad = ai.get("issue", 0)
        un = ai.get("unsure", 0)
        print(f"  AI 审核  已审 {ok + bad + un} 条 · OK {ok} · "
              f"有问题 {bad} · 拿不准 {un}")
        if un:
            print("           拿不准的写在 data/logs/ai_unsure.json，等人看")
    else:
        print("  AI 审核  还没有记录")
    print()

    print("─" * W)
    print("  最近完成")
    for r in recent:
        print(f"   {r['t']}  {r['doc_uid'][:40]:<40} → {r['en'] or 0} 字符")
    print()
    print("─" * W)
    tail = _tail_lines(RUN_LOG, 4)
    if tail:
        print("  工作流日志")
        for ln in tail:
            print(f"   {ln[:W - 4]}")
    print()
    print("  （每 3 秒刷新 · Ctrl+C 退出 · 这只是一个只读的窗口，关了不影响翻译）")


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--interval", type=float, default=3.0)
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()

    if args.once:
        draw()
        return 0
    try:
        while True:
            try:
                draw()
            except Exception as e:  # noqa: BLE001 - 画不出来不该让窗口挂掉
                print(f"（这一屏没画出来：{type(e).__name__}: {e}）")
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n（已退出监控台；翻译不受影响。）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
