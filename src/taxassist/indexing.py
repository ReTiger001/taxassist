"""政策正文向量索引：分块、嵌入、写入、增量维护。

## 为什么单独成模块而不是留在 scripts/

三个地方要建/补向量，逻辑必须是**同一份**（本项目反复踩的坑就是"两处各写一遍"）：

- ``scripts/embed_policy.py``     —— 运维手动全量建/重建
- ``cli.cmd_collect``             —— 采集结束后自动补增量
- ``scheduler._job_run_daily``    —— 每日 07:30 定时采集后补增量

## 增量判据

``policy.content_hash``（入库层每次内容变化都会更新它）≠
``policy_embedding.src_hash``（建索引时记下的原文哈希）→ 重建该文档的块。

**只判断"没有向量"是错的**：政策被重新抓取/修订后向量不会更新，检索会一直
用旧的语义表示。2026-10-20 之前就是这么写的，已修正。
"""
from __future__ import annotations

import logging
import time

import httpx

log = logging.getLogger(__name__)

MODEL = "qwen3-embedding:0.6b"
OLLAMA = "http://127.0.0.1:11434/api/embed"
DIM = 1024
CHUNK = 600
OVERLAP = 100
#: 一次送多少块给 embedding。批太大单次超时（600s）的成本会被放大。
BATCH = 16
#: 单条正文最多切多少块（11 万字超长文档约 220 块，全做太贵且收益递减）。
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
    """拼成 pgvector 字面量。不依赖 psycopg 适配器注册，传字符串最省事。"""
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


def pending_docs(conn, limit: int | None = None) -> list[tuple[str, str, str]]:
    """待处理文档：从未向量化的，或正文变过、向量已过期的。"""
    sql = (
        "SELECT p.doc_uid, p.content, p.content_hash FROM policy p"
        " WHERE p.content IS NOT NULL AND p.content <> ''"
        "   AND (NOT EXISTS (SELECT 1 FROM policy_embedding e WHERE e.doc_uid = p.doc_uid)"
        "        OR EXISTS (SELECT 1 FROM policy_embedding e WHERE e.doc_uid = p.doc_uid"
        "                   AND e.src_hash IS DISTINCT FROM p.content_hash))"
        " ORDER BY p.doc_uid"
    )
    if limit:
        sql += f" LIMIT {int(limit)}"
    return [(r["doc_uid"], r["content"], r["content_hash"] or "")
            for r in conn.execute(sql).fetchall()]


def embed_pending(conn, *, limit: int | None = None,
                  progress=None) -> dict:
    """把待处理文档向量化。返回 ``{"docs": n, "chunks": n, "seconds": s}``。

    ``progress(done, total)`` 可选回调，供 CLI 打印进度。
    单条失败只记录、不中断 —— 全量是长任务，一条坏数据不该让它整体停摆。
    """
    docs = pending_docs(conn, limit)
    total = len(docs)
    if not total:
        return {"docs": 0, "chunks": 0, "seconds": 0.0}

    t0 = time.time()
    done_docs = done_chunks = 0
    buf: list[tuple[str, int, str, str, str]] = []

    def flush() -> None:
        nonlocal done_chunks
        if not buf:
            return
        # 重建前先删该文档的旧块：正文变短时旧块会多出来，只靠 ON CONFLICT
        # 更新会留下越界的残块（检索时仍可能被召回）。
        for u in {b[0] for b in buf}:
            conn.execute("DELETE FROM policy_embedding WHERE doc_uid = ?", (u,))
        conn.executemany(
            "INSERT INTO policy_embedding(doc_uid, chunk_no, chunk_text, embedding, src_hash)"
            " VALUES(?,?,?,?,?)"
            " ON CONFLICT(doc_uid, chunk_no) DO UPDATE SET"
            "   chunk_text=excluded.chunk_text, embedding=excluded.embedding,"
            "   src_hash=excluded.src_hash",
            [(u, n, t, v, h) for (u, n, t, v, h) in buf])
        conn.commit()
        done_chunks += len(buf)
        buf.clear()

    for i, (uid, content, chash) in enumerate(docs, 1):
        pieces = chunk_text(content)
        if not pieces:
            continue
        try:
            vecs = embed(pieces)
        except Exception as e:  # noqa: BLE001
            log.warning("向量化失败 %s: %s: %s", uid, type(e).__name__, e)
            continue
        for n, (piece, vec) in enumerate(zip(pieces, vecs, strict=False)):
            buf.append((uid, n, piece, to_pg_vector(vec), chash))
        done_docs += 1
        if len(buf) >= BATCH * 4:
            flush()
        if progress and (i % 100 == 0 or i == total):
            progress(i, total)
    flush()
    return {"docs": done_docs, "chunks": done_chunks, "seconds": time.time() - t0}


def build_index(conn) -> None:
    """建 HNSW 索引（余弦距离）。**批量插入后再建**，比边插边更新快得多。"""
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_policy_embedding_hnsw"
        " ON policy_embedding USING hnsw (embedding vector_cosine_ops)")
    conn.commit()


def ensure_ready(conn) -> bool:
    """向量基础设施是否可用（表在、有数据）。"""
    try:
        n = conn.execute("SELECT COUNT(*) AS n FROM policy_embedding").fetchone()["n"]
        return int(n) > 0
    except Exception:  # noqa: BLE001
        return False
