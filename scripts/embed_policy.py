"""为政策正文建立向量索引（pgvector + qwen3-embedding:0.6b）—— 运维手动入口。

核心逻辑在 ``src/taxassist/indexing.py``（采集流程与定时任务也调用它，
三处共用一份，不重复实现）。

## 为什么需要向量

站点检索原来只有 PGroonga 全文匹配：搜「税率」能命中 2015 条，但**同义不同词**
就搜不到（搜「税负」找不到只写「税收负担」的政策）。向量补的是这块语义召回，
与全文**混合使用**（RRF 融合）而不是替换 —— 全文对文号、专有术语仍然更准。

## 断点续跑

只处理「没有向量」或「正文变过、向量过期」的文档（判据见 indexing.pending_docs），
中断后再跑不会重复烧模型时间（全量要跑数小时，中断是常态）。

## 用法

    .venv\\Scripts\\python.exe scripts\\embed_policy.py            # 增量（可反复跑）
    .venv\\Scripts\\python.exe scripts\\embed_policy.py --limit 100 # 先小批量验证
    .venv\\Scripts\\python.exe scripts\\embed_policy.py --rebuild   # 清空重建
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from taxassist import indexing  # noqa: E402
from taxassist.repo.conn import connect_compat  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=None, help="只处理前 N 条（验证用）")
    ap.add_argument("--rebuild", action="store_true", help="清空已有向量后重建")
    ap.add_argument("--no-index", action="store_true", help="跑完不建 HNSW 索引")
    args = ap.parse_args()

    conn = connect_compat()
    try:
        if args.rebuild:
            conn.execute("TRUNCATE policy_embedding")
            conn.commit()
            print("  已清空 policy_embedding")

        pending = indexing.pending_docs(conn, args.limit)
        total = len(pending)
        print(f"待向量化 {total} 条文档（块大小 {indexing.CHUNK}、"
              f"重叠 {indexing.OVERLAP}）\n", flush=True)
        if not total:
            print("没有待处理的文档。")
            return 0

        def progress(done: int, all_: int) -> None:
            print(f"  [{done}/{all_}] …", flush=True)

        r = indexing.embed_pending(conn, limit=args.limit, progress=progress)
        print(f"\n完成：{r['docs']} 条文档 / {r['chunks']} 块，"
              f"用时 {r['seconds']/60:.1f} 分钟")

        if not args.no_index:
            if args.limit:
                print("  （--limit 模式不建索引，全量跑完再建）")
            else:
                print("  建 HNSW 索引（余弦距离）…", flush=True)
                indexing.build_index(conn)
                print("  ✓ 索引已建")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
