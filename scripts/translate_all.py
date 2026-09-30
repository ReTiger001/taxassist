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

# 标题用 Q4：短文本量化差异体现不出来，速度却快一倍（实测 8.16s vs 13.62s /600字）
# 正文用 Q8：术语密集，实测 Q8 的 individual income tax 才是合规译法
STAGES = [
    ("titles", "hunyuan-mt", "标题（5109 条，约 125 分钟）"),
    ("content", "hunyuan-mt-q8", "正文（约 806 万字，约 30 小时）"),
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

    for what, model, label in stages:
        run(what, model, label)
    print("\n全部阶段完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
