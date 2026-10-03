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
    proc = subprocess.run(
        [PY, str(BATCH), "--what", what, "--model", model],
        cwd=str(ROOT))
    print(f"  {label} 结束，退出码 {proc.returncode}，用时 {(time.time() - t0) / 60:.1f} 分钟",
          flush=True)
    return proc.returncode


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=[s[0] for s in STAGES], default=None)
    ap.add_argument("--skip-titles", action="store_true")
    args = ap.parse_args()

    stages = STAGES
    if args.only:
        stages = [s for s in STAGES if s[0] == args.only]
    elif args.skip_titles:
        stages = [s for s in STAGES if s[0] != "titles"]

    # **先取写库锁再开工**：正文翻译一条 20 秒、攒批窗口百秒级；若此时
    # worker 在跑采集/publish，两边会互相撞锁（实测撞过 init_db 的
    # DROP TRIGGER 与 record_snapshot）。取不到就等 —— 翻译是长任务，
    # 等几分钟远好过撞死重跑。（见 taxassist.writelock）
    from taxassist import writelock

    if not writelock.acquire("translate_all", timeout=1800):
        print(f"写库锁被 {writelock.holder()} 占用，等待超时，未启动翻译。")
        return 1
    try:
        for what, model, label in stages:
            run(what, model, label)
    finally:
        writelock.release()
    print("\n全部阶段完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
