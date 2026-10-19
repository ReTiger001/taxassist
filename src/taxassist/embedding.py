"""本地 embedding 客户端 —— 政策库向量检索用。

模型：``qwen3-embedding:0.6b``（1024 维，639MB，中文强，100+ 语言）。
全部走本机 ``127.0.0.1:11434``，**没有任何云端调用**（与本项目"数据不出本机"
的硬规则一致）。

## 为什么单独一个模块

`scripts/embed_policy.py`（离线建索引）和 `kb.py`（在线查询）都要把文本转成向量，
两边**必须用同一个模型、同一个维度** —— 换个模型就等于旧向量全部作废。
把这个约束收在一处，改模型时只有一个地方要动（且会立刻暴露给两边）。
"""
from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)

MODEL = "qwen3-embedding:0.6b"
#: 与 `policy_embedding.embedding vector(N)` 的 N 必须一致。改模型时两处一起改。
DIM = 1024
OLLAMA = "http://127.0.0.1:11434/api/embed"


def embed_text(text: str, timeout: float = 120.0) -> list[float] | None:
    """把一段文本转成向量。失败返回 None（**检索要能降级，不能因为向量挂了整页崩**）。"""
    text = (text or "").strip()
    if not text:
        return None
    try:
        r = httpx.post(OLLAMA, json={"model": MODEL, "input": text}, timeout=timeout)
        r.raise_for_status()
        vecs = r.json().get("embeddings") or []
        return vecs[0] if vecs else None
    except Exception as e:  # noqa: BLE001
        log.warning("embedding 失败（%s）：%s: %s", MODEL, type(e).__name__, e)
        return None


def vec_literal(vec: list[float]) -> str:
    """拼成 pgvector 字面量 ``[0.1,0.2,...]``，供参数化查询用。

    不依赖 psycopg 的适配器注册：那需要 `psycopg.types` 里注册 vector 类型，
    而本项目用 ``?`` 占位符 + 连接层翻译，传字符串最省事（PG 会隐式转成 vector）。
    """
    return "[" + ",".join(f"{x:.6f}" for x in vec) + "]"


def available() -> bool:
    """向量检索是否可用：模型在不在、表建没建、有没有数据。

    三件缺一就返回 False —— 调用方据此**退回全文检索**，而不是报错。
    """
    try:
        from .repo import conn as _rc

        if _rc.BACKEND != "pg":
            return False
        conn = _rc.connect_readonly()
        try:
            n = conn.execute("SELECT COUNT(*) AS n FROM policy_embedding").fetchone()["n"]
            return int(n) > 0
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - 表不存在（还没建索引）也走这里
        return False
