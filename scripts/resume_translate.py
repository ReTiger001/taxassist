"""反复跑 translate_batch，直到正文与标题的缺口都归零。

======================================================================
为什么需要这个循环
======================================================================

``translate_batch.py`` 一旦发现写库锁被别人持有（采集 / enrich / 审计 /
fix_terms 都在抢），就**整轮退出**，不做等待。2026-10-14 实测：一轮译了
120 条就退出，用时 47.7 分钟，0.05 条/秒 —— 按那个速率，剩下的 2900
多条标题要跑十几个小时，而且每被挤掉一次就得有人重新敲一遍命令。

锁的空闲窗口是零碎的。这个循环只做一件事：**锁一空就立刻继续译**，
把那些窗口全部利用起来，不需要有人盯着。

它**不改变** translate_batch 的任何行为 —— 不改它的取数、切块、术语或
写库方式，只是反复调用它的正常入口。它自己也不写库（只读统计缺口）。

用法::

    python scripts/resume_translate.py            # 最多 400 轮
    python scripts/resume_translate.py 20         # 最多 20 轮
"""

import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from taxassist import proc as procutil  # 子进程必须带 CREATE_NO_WINDOW

DB_RO = "file:D:/EY-project/data/taxassist.db?mode=ro"
GAP_SQL = {
    "titles": """SELECT COUNT(*) FROM policy p
        WHERE p.title IS NOT NULL AND LENGTH(p.title)>1
          AND NOT EXISTS (SELECT 1 FROM translation t
                          WHERE t.doc_uid=p.doc_uid AND t.field='title')""",
    "content": """SELECT COUNT(*) FROM policy p
        WHERE p.content IS NOT NULL AND LENGTH(p.content)>10
          AND NOT EXISTS (SELECT 1 FROM translation t
                          WHERE t.doc_uid=p.doc_uid AND t.field='content')""",
}


def gap(what):
    c = sqlite3.connect(DB_RO, uri=True, timeout=30)
    try:
        return c.execute(GAP_SQL[what]).fetchone()[0]
    finally:
        c.close()


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    max_rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 400
    batch = str(Path(__file__).resolve().parent / "translate_batch.py")
    for rnd in range(1, max_rounds + 1):
        gt, gc = gap("titles"), gap("content")
        print(f"[{time.strftime('%H:%M:%S')}] 第 {rnd} 轮："
              f"标题缺 {gt}，正文缺 {gc}", flush=True)
        if gt == 0 and gc == 0:
            print("两个缺口都已归零，收工。", flush=True)
            return 0
        what = "titles" if gt else "content"
        # 不捕获输出：让它直接进日志，人看得见进度
        # creationflags 不能省：本脚本可能被守护以无控制台方式拉起，不给它
        # 每起一个子进程就弹一个命令窗（tests/test_no_window_guard.py 焊死）。
        subprocess.run([sys.executable, batch, "--what", what, "--limit", "0"],
                       cwd=str(Path(__file__).resolve().parent.parent),
                       creationflags=procutil.hidden_flags())
        time.sleep(10)
    print(f"达到轮数上限 {max_rounds}，仍有缺口。", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
