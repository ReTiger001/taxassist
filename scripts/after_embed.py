"""等向量化跑完 → 建 HNSW 索引 → 启动翻译工作流与守护（无人值守衔接）。

## 为什么需要这个脚本

2026-10-20 凌晨用户要求"向量化完成后打开翻译工作，我去睡觉"。向量化还要约
30 分钟，而**不能靠人等**、也不该依赖某个会话进程一直活着（本项目已经因为
"没人盯着"空转过 11 小时）。

本脚本做成独立后台进程，自己轮询到向量化结束，然后依次：
  1. 建 HNSW 索引（幂等，已建则跳过）
  2. 启动 `auto_workflow`（翻译 → 自检 → 修复）
  3. 启动 `guardian`（之后由它接管：Ollama 或工作流挂了自动拉起）

## 判定"向量化跑完"的两条路

- 待处理数为 0 → 正常跑完
- 或者 embed 进程已退出（哪怕还有剩余）→ 也继续，**不能死等**
  剩余由下次 `scripts/embed_policy.py` / 采集钩子 / 日更补齐（都是增量的）
"""
from __future__ import annotations

import logging
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = ROOT / ".venv" / "Scripts" / "python.exe"
LOG_DIR = ROOT / "data" / "logs"
MAX_WAIT_SEC = 3 * 3600

sys.path.insert(0, str(ROOT / "src"))
from taxassist import indexing  # noqa: E402
from taxassist import proc as procutil  # noqa: E402
from taxassist.repo.conn import connect_compat  # noqa: E402

LOG_DIR.mkdir(parents=True, exist_ok=True)
log = logging.getLogger("after_embed")
log.setLevel(logging.INFO)
_fh = logging.FileHandler(LOG_DIR / "after_embed.log", encoding="utf-8")
_fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
log.addHandler(_fh)
_sh = logging.StreamHandler()
_sh.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
log.addHandler(_sh)


def _embed_running() -> bool:
    """embed_policy.py 还在跑吗（按命令行匹配，跨会话可见）。"""
    try:
        out = subprocess.run(
            ["wmic", "process", "where", "name='python.exe'", "get", "CommandLine"],
            capture_output=True, text=True, errors="replace",
            creationflags=procutil.hidden_flags(), timeout=60).stdout
        return "embed_policy.py" in out
    except Exception:  # noqa: BLE001 - 判不出来就当它还在跑（宁可多等一轮）
        return True


def _pending(conn) -> int:
    return int(conn.execute(
        "SELECT COUNT(*) AS n FROM policy p"
        " WHERE p.content IS NOT NULL AND p.content <> ''"
        "   AND (NOT EXISTS (SELECT 1 FROM policy_embedding e WHERE e.doc_uid = p.doc_uid)"
        "        OR EXISTS (SELECT 1 FROM policy_embedding e WHERE e.doc_uid = p.doc_uid"
        "                   AND e.src_hash IS DISTINCT FROM p.content_hash))"
    ).fetchone()["n"])


def _start(argv: list[str], logname: str) -> None:
    """以隐藏窗口启动子进程，输出追加到日志。"""
    fh = open(LOG_DIR / logname, "a", encoding="utf-8", errors="replace")  # noqa: SIM115
    subprocess.Popen(argv, cwd=str(ROOT), stdout=fh, stderr=subprocess.STDOUT,
                     creationflags=procutil.hidden_flags())
    log.info("已启动：%s", " ".join(argv))


def main() -> int:
    log.info("衔接脚本启动：等待向量化完成（最多等 %d 分钟）", MAX_WAIT_SEC // 60)
    t0 = time.time()
    while time.time() - t0 < MAX_WAIT_SEC:
        conn = connect_compat()
        try:
            n = _pending(conn)
        finally:
            conn.close()
        running = _embed_running()
        log.info("待处理 %d 条，embed 进程在跑=%s", n, running)
        if n == 0:
            log.info("向量化已完成（待处理 0）")
            break
        if not running:
            log.warning("embed 进程已退出，仍有 %d 条未处理 —— 继续收尾，剩余交给增量补齐", n)
            break
        time.sleep(60)

    conn = connect_compat()
    try:
        log.info("建 HNSW 索引…")
        indexing.build_index(conn)
        cnt = conn.execute("SELECT COUNT(*) AS n FROM policy_embedding").fetchone()["n"]
        doc = conn.execute("SELECT COUNT(DISTINCT doc_uid) AS n FROM policy_embedding").fetchone()["n"]
        log.info("向量库：%s 块 / %s 条文档", cnt, doc)
    except Exception as e:  # noqa: BLE001
        log.exception("建索引失败：%s", e)
    finally:
        conn.close()

    # 先起守护：它检测到工作流不在会自动拉起；之后也由它兜住中断
    _start([str(PY), "scripts/guardian.py"], "guardian_stdout.log")
    # 再直接起一次工作流（不必等守护的 5 分钟巡检周期）
    _start([str(PY), "scripts/auto_workflow.py", "--hours", "0-24"], "auto_workflow.log")
    log.info("收尾完成：工作流与守护已启动")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
