"""为政策正文建立向量索引（pgvector + qwen3-embedding:0.6b）。

## 为什么

站点的检索原来只有 PGroonga 全文匹配：搜「税率」能命中 2015 条，但**同义不同词**
就搜不到（搜「税负」找不到只写「税收负担」的政策）。向量检索补的正是这一块语义。
两者**混合使用**（RRF 融合），而不是替换 —— 全文匹配对文号、专有术语仍然更准。

## 怎么做

- 分块：正文按 600 字切、相邻块重叠 100 字。重叠是必须的：政策条款常常跨段，
  硬切会把「本条规定适用于……」和它的前一句切成两块，两块单独看都不完整。
- 嵌入：`qwen3-embedding:0.6b`（1024 维，639MB，中文强）。
- 存储：`policy_embedding(doc_uid, chunk_no, chunk_text, embedding vector(1024))`。
- **断点续跑**：只处理 `policy_embedding` 里还没有的 doc_uid。中断后再跑不会重复
  烧模型时间（14548 条要跑数小时，中断是常态）。
- **索引最后建**：批量插入前不建 HNSW，插完再建，比边插边更新快得多。

## 用法

    .venv\\Scripts\\python.exe scripts\\embed_policy.py            # 全量（可续跑）
    .venv\\Scripts\\python.exe scripts\\embed_policy.py --limit 100 # 先小批量验证
    .venv\\Scripts\\python.exe scripts\\embed_policy.py --rebuild   # 清空重建
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from taxassist.repo.conn import connect_compat  # noqa: E402

MODEL = "qwen3-embedding:0.6b"
OLLAMA = "http://127.0.0.1:11434/api/embed"
CHUNK = 600
OVERLAP = 100
#: 一次送多少块给 embedding。ollama 能收列表，但批太大会单次超时放大重试成本。
BATCH = 16
#: 单条正文最多切多少块（超长文档 11 万字 → 约 220 块，全做太贵且收益低）。
MAX_CHUNKS_PER_DOC = 40


def chunk_text(text: str, size: int = CHUNK, overlap: int = OVERLAP) -> list[str]:
    """按固定窗口切块，相邻块重叠 ``overlap`` 字。

    为什么不用按句切：中文政策里句号密度差异极大（表格、名单类几乎没句号），
    按句切会让某些块大到超出嵌入模型窗口、某些块只有几个字。固定窗口 + 重叠
    在两种极端下都稳定。
    """
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]
    step = max(1, size - overlap)
    out: list[str] = []
    for start in range(0, len(text), step):
        piece = text[start:start + size]
        if piece:
            out.append(piece)
        if start + size >= len(text):
            break
    return out[:MAX_CHUNKS_PER_DOC]


def embed(texts: list[str], timeout: int = 600) -> list[list[float]]:
    r = httpx.post(OLLAMA, json={"model": MODEL, "input": texts}, timeout=timeout)
    r.raise_for_status()
    return r.json()["embeddings"]


def to_pg_vector(v: list[float]) -> str:
    """拼成 pgvector 的字面量 ``[0.1,0.2,...]``（避免依赖 psycopg 的适配器注册）。"""
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


def pending_docs(conn, limit: int | None) -> list[tuple[str, str]]:
    sql = (
        "SELECT p.doc_uid, p.content FROM policy p"
        " WHERE p.content IS NOT NULL AND p.content <> ''"
        "   AND NOT EXISTS (SELECT 1 FROM policy_embedding e WHERE e.doc_uid = p.doc_uid)"
        " ORDER BY p.doc_uid"
    )
    if limit:
        sql += f" LIMIT {int(limit)}"
    return [(r["doc_uid"], r["content"]) for r in conn.execute(sql).fetchall()]


def build_index(conn) -> None:
    print("  建 HNSW 索引（余弦距离）…", flush=True)
    t0 = time.time()
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_policy_embedding_hnsw"
        " ON policy_embedding USING hnsw (embedding vector_cosine_ops)")
    conn.commit()
    print(f"  ✓ 索引建成，用时 {time.time()-t0:.0f}s", flush=True)


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

        docs = pending_docs(conn, args.limit)
        total = len(docs)
        print(f"待向量化 {total} 条文档（块大小 {CHUNK}、重叠 {OVERLAP}）\n", flush=True)
        if not total:
            print("没有待处理的文档。")
            return 0

        t0 = time.time()
        done_docs = done_chunks = 0
        buf: list[tuple[str, int, str, str]] = []   # (doc_uid, chunk_no, text, vector_literal)

        def flush():
            nonlocal done_chunks
            if not buf:
                return
            conn.executemany(
                "INSERT INTO policy_embedding(doc_uid, chunk_no, chunk_text, embedding)"
                " VALUES(?,?,?,?) ON CONFLICT(doc_uid, chunk_no) DO NOTHING",
                [(u, n, t, v) for (u, n, t, v) in buf])
            conn.commit()
            done_chunks += len(buf)
            buf.clear()

        for i, (uid, content) in enumerate(docs, 1):
            pieces = chunk_text(content)
            if not pieces:
                continue
            try:
                vecs = embed(pieces)
            except Exception as e:  # noqa: BLE001 - 单条失败不中断全量
                print(f"  [{i}/{total}] 失败 {uid[:36]}: {type(e).__name__}: {str(e)[:80]}",
                      flush=True)
                continue
            for n, (piece, vec) in enumerate(zip(pieces, vecs, strict=False)):
                buf.append((uid, n, piece, to_pg_vector(vec)))
            done_docs += 1
            if len(buf) >= BATCH * 4:
                flush()
            if i % 100 == 0 or i == total:
                el = time.time() - t0
                rate = i / el if el else 0
                eta = (total - i) / rate / 60 if rate else 0
                print(f"  [{i}/{total}] 文档 {done_docs} / 块 {done_chunks + len(buf)}"
                      f"  {rate:.2f} 文档/s  已用 {el/60:.1f}min  预计还需 {eta:.0f}min",
                      flush=True)
        flush()

        el = time.time() - t0
        print(f"\n完成：{done_docs} 条文档 / {done_chunks} 块，用时 {el/60:.1f} 分钟")

        if not args.no_index:
            if args.limit:
                print("  （--limit 模式不建索引，全量跑完再建）")
            else:
                build_index(conn)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
