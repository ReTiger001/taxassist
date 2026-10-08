"""无人值守工作流：翻译 → 自检 → 自纠错，在时间窗内自己循环。

======================================================================
它解决的是什么
======================================================================

「每天 9-20 点做一个自翻译自检查自纠错的工作流，而不是一直无谓地浪费
token。」—— 这句话点出的是：**人（或 AI）不该做机器能循环做的事**。
查进度、跑体检、跑修复，每一步都已有现成脚本；缺的只是把它们串成一个
不需要人看着的循环。

三个环节各自的工具：
  翻译    translate_batch.py      按原文指纹缓存，重复跑不白做
  自检    audit_translation.py    10 类机器可判定的硬错误
  自纠错  fix_org_names.py        机构名结构（确定规则）
          fix_terms.py            术语用词（已定稿的那批）
          retranslate.py          定点重译（机检命中且重译能修的）

======================================================================
它刻意不做的事
======================================================================

1. **不做需要判断的修改**。语义嫌疑、A4 的假阳性、A3 的真伪 —— 这些必须
   有人看原文，工作流只处理「规则明确」与「重译能修」的两类，其余写进
   嫌疑清单等人审。**自动修错比不修更糟**：fix_org_names 曾因正则缺个 `\b`
   造出 27 条错词，而那还是人工干跑看出来的。

2. **不静默失败**。每一步都记进 JSON 与日志；失败不中断整轮，但会留着 ——
   无人值守最怕的不是出错，是出错没人知道。

3. **不在窗外空转**。窗外直接退出，不占资源。

4. **不抢写库锁**。所有写操作交给子脚本，它们各自按项目约定处理锁。

用法：
    python scripts/auto_workflow.py --once                  # 只跑一轮（调试）
    python scripts/auto_workflow.py                         # 9-20 点自己循环
    python scripts/auto_workflow.py --translate-batch 200   # 每轮多译一些
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
PROGRESS = ROOT / "data" / "logs" / "auto_workflow.json"

sys.path.insert(0, str(ROOT / "src"))
from taxassist import proc as procutil  # noqa: E402


def log(msg: str) -> None:
    print(f"{dt.datetime.now():%H:%M:%S} {msg}", flush=True)


def run(argv: list[str], timeout: int = 7200) -> dict:
    """跑一个子步骤并记录结果。**失败不抛异常** —— 无人值守时一次失败不该
    终止整轮，更不该让循环死掉。"""
    t0 = time.time()
    try:
        p = subprocess.run([PY, *argv], cwd=str(ROOT), capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           timeout=timeout,
                           # creationflags 不能省：Windows 下不给它，每起一个
                           # 子进程就弹一个黑框。项目有一条硬约定和一条测试
                           # （tests/test_no_window_guard.py）盯着这件事 ——
                           # 正是它拦下了这个文件的第一版。
                           creationflags=procutil.hidden_flags())
        lines = [x for x in (p.stdout or "").strip().splitlines() if x.strip()]
        return {"rc": p.returncode, "sec": round(time.time() - t0, 1),
                "tail": lines[-2:]}
    except subprocess.TimeoutExpired:
        return {"rc": -1, "sec": round(time.time() - t0, 1),
                "tail": [f"超时（>{timeout}s）"]}
    except Exception as e:  # noqa: BLE001
        return {"rc": -2, "sec": round(time.time() - t0, 1),
                "tail": [f"{type(e).__name__}: {e}"]}


def show(tag: str, r: dict) -> None:
    tail = r["tail"][-1] if r["tail"] else ""
    log(f"   {tag}: rc={r['rc']} {r['sec']}s  {tail[:110]}")


def one_round(args) -> dict:
    rep: dict = {}

    # ① 翻译一批。**用 translate_batch 而不是 translate_all**：前者不设时间窗，
    #    由本工作流的 --hours 统一管；后者自带 11-19 的窗，会和这里打架。
    log(f"① 翻译（上限 {args.translate_batch} 条）")
    rep["translate"] = run(["scripts/translate_batch.py", "--what", "content",
                            "--limit", str(args.translate_batch)])
    show("翻译", rep["translate"])

    # ② 自检：全量机检，只读。它给后面两步提供目标清单。
    log("② 自检（全量机检）")
    rep["audit"] = run(["scripts/audit_translation.py", "--field", "both",
                        "--samples", "1"], timeout=3600)
    show("自检", rep["audit"])

    # ③ 自纠错之一：规则明确的（机构名结构 + 已定稿术语）。这两类不需要判断，
    #    改错了会被后面的自检验证出来。
    log("③ 自纠错 · 规则修复")
    rep["fix_org"] = run(["scripts/fix_org_names.py", "--field", "content",
                          "--apply"])
    show("机构名", rep["fix_org"])
    rep["fix_terms"] = run(["scripts/fix_terms.py", "--field", "content",
                            "--apply", "--show", "0"])
    show("术语", rep["fix_terms"])

    # ④ 自纠错之二：定点重译（限量）。**限量是刻意的** —— 长文档一条要几分钟，
    #    不限量会让一轮永远跑不完，自检和修复就再也轮不到。
    log(f"④ 自纠错 · 定点重译（上限 {args.retranslate_limit} 条）")
    rep["retranslate"] = run(["scripts/retranslate.py", "--limit",
                              str(args.retranslate_limit)], timeout=5400)
    show("重译", rep["retranslate"])

    return rep


def in_window(spec: str) -> bool:
    """``9-20`` 表示 9:00 ≤ 现在 < 20:00。规格写错就不限制 —— 别把工作流卡死。"""
    try:
        a, b = (int(x) for x in spec.split("-", 1))
    except Exception:  # noqa: BLE001
        return True
    h = dt.datetime.now().hour
    return a <= h < b if a <= b else (h >= a or h < b)


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--hours", default="9-20", help="工作时段（默认 9-20）")
    ap.add_argument("--translate-batch", type=int, default=150,
                    help="每轮翻译条数上限")
    ap.add_argument("--retranslate-limit", type=int, default=40,
                    help="每轮定点重译条数上限")
    ap.add_argument("--sleep", type=int, default=30, help="轮间隔秒")
    ap.add_argument("--once", action="store_true", help="只跑一轮")
    ap.add_argument("--rounds", type=int, default=0, help="跑几轮，0=不限")
    args = ap.parse_args()

    history: list[dict] = []
    n = 0
    while True:
        if not args.once and not in_window(args.hours):
            log(f"当前不在 {args.hours} 时段，退出")
            break
        n += 1
        log(f"===== 第 {n} 轮 =====")
        rep = one_round(args)
        history.append({"round": n,
                        "at": dt.datetime.now().isoformat(timespec="seconds"),
                        **rep})
        PROGRESS.write_text(json.dumps(history[-60:], ensure_ascii=False, indent=1),
                            encoding="utf-8")
        if args.once or (args.rounds and n >= args.rounds):
            break
        log(f"本轮结束，{args.sleep} 秒后进入下一轮")
        time.sleep(args.sleep)
    log(f"工作流结束，共 {n} 轮。进度：{PROGRESS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
