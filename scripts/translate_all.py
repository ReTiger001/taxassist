"""全量翻译总控：标题 → 正文 → 附件，串行执行，可中断续跑。

======================================================================
为什么串行而不是并发
======================================================================

只有一块 GPU。两个翻译进程同时跑会互相抢算力、还把显存挤爆
（Q8_0 单路 KV 约 1.0GB）。串行反而更快也更稳。

======================================================================
为什么可以随时中断
======================================================================

translate_llm 按**原文指纹**缓存：已译且原文未变的会跳过。所以这个脚本
被杀掉、机器重启、或中途手动停，重跑时只补未译部分，不会白做。
全量正文按实测约 30 小时，这一点是必需的而不是锦上添花。

用法：
    python scripts/translate_all.py            # 全部（标题→正文→附件）
    python scripts/translate_all.py --skip-titles
    python scripts/translate_all.py --only content
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
BATCH = ROOT / "scripts" / "translate_batch.py"

# 两个阶段都用 Q4（hunyuan-mt）。依据：Q8_0 实测慢 8.7 倍
# （12.15s/条 vs 1.40s/条）、翻译质量无可辨差异，为省 15G 空间已删除 ——
# 所以这里不能再引用 hunyuan-mt-q8（会直接报模型不存在）。
STAGES = [
    ("titles", "hunyuan-mt", "标题（约 125 分钟）"),
    ("content", "hunyuan-mt", "正文（约 806 万字）"),
]


def run(what: str, model: str, label: str) -> int:
    print(f"\n{'=' * 76}\n【{label}】 模型 {model}\n{'=' * 76}", flush=True)
    t0 = time.time()
    # creationflags 不能省：本脚本由守护以无控制台方式启动，不给这个标志的话
    # Windows 会给下面这个 python 子进程**弹一个新窗口**（见 taxassist.proc）。
    # 变量不叫 proc，避免遮蔽别名 procutil。
    from taxassist import proc as procutil

    done = subprocess.run(
        [PY, str(BATCH), "--what", what, "--model", model],
        cwd=str(ROOT), creationflags=procutil.hidden_flags(),
        # stdout/stderr **必须显式传**，不能靠继承：本脚本被守护以
        # CREATE_NO_WINDOW 拉起、输出重定向到日志文件时，子进程只靠"继承"
        # 会把自己的输出丢进一个不可见的控制台 —— 实测 translate_run.log
        # 里从头到尾没出现过子进程的「待处理 / 已译」进度行，于是
        # "正文其实一条都没译成"这件事被藏了很久，看日志只知道"瞬间完成"。
        stdout=sys.stdout, stderr=sys.stderr)
    done_stamp = time.time()
    print(f"  {label} 结束，退出码 {done.returncode}，用时 {(done_stamp - t0) / 60:.1f} 分钟",
          flush=True)
    return done.returncode


def _in_window(spec: str) -> bool:
    """当前时刻是否落在 ``起-止`` 小时窗内（含起点、不含终点）。

    支持跨天写法（如 ``23-7``）。窗口由作息决定 —— 翻译会占满 GPU 并持有
    写库锁，所以只在不用电脑的时段跑，其余时间让位给采集/publish。
    """
    try:
        start_s, end_s = spec.split("-", 1)
        start_h, end_h = int(start_s), int(end_s)
    except Exception:  # noqa: BLE001 - 规格写错就不限制，别把翻译卡死
        return True
    hour = datetime.now().hour
    if start_h <= end_h:
        return start_h <= hour < end_h
    return hour >= start_h or hour < end_h


def _wait_for_window(spec: str) -> None:
    """窗口外就等到窗口开始（每 2 分钟醒一次，便于 Ctrl-C 打断）。"""
    if _in_window(spec):
        return
    print(f"当前不在翻译时段（{spec} 点之间），等待中……", flush=True)
    while not _in_window(spec):
        time.sleep(120)
    print(f"进入翻译时段（{spec} 点之间），开工。", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=[s[0] for s in STAGES], default=None)
    ap.add_argument("--skip-titles", action="store_true")
    ap.add_argument("--window", default="11-19",
                    help="允许翻译的小时窗（默认 11-19，即只在此时段跑）")
    ap.add_argument("--anytime", action="store_true",
                    help="忽略时间窗，立刻开始（手动补译时用）")
    args = ap.parse_args()

    stages = STAGES
    if args.only:
        stages = [s for s in STAGES if s[0] == args.only]
    elif args.skip_titles:
        stages = [s for s in STAGES if s[0] != "titles"]

    if not args.anytime:
        _wait_for_window(args.window)

    # **先取写库锁再开工**：正文翻译一条 20 秒、攒批窗口百秒级；若此时
    # worker 在跑采集/publish，两边会互相撞锁（实测撞过 init_db 的
    # DROP TRIGGER 与 record_snapshot）。取不到就等 —— 翻译是长任务，
    # 等几分钟远好过撞死重跑。（见 taxassist.writelock）
    # **写库锁交给 translate_batch 按批管理**（见那里的注释）。
    # 原先这里是整场持有（acquire 到结束才 release）—— 那正是撞锁的根因：
    # 其它写任务（附件重试、judge、采集）永远等不到锁，只能去撞 SQLite
    # 引擎锁，busy_timeout 用尽就失败。实测附件重试正是这么崩的。
    # 现在只探一次能否开工（拿不到就早退，免得白起一轮进程），随即还回去；
    # 真正的持有发生在 batch 的每一批里。
    from taxassist import writelock

    if not writelock.acquire("translate", timeout=300):
        print(f"写库锁被 {writelock.holder()} 占用，等待超时，未启动翻译。")
        return 1
    writelock.release()          # 立刻还回去，不当长跑任务的长锁持有者
    for what, model, label in stages:
        run(what, model, label)
    print("\n全部阶段完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
