"""用本地 Hunyuan-MT 翻译政策文本（标题 / 正文）。

======================================================================
设计与取舍
======================================================================

**只走本机**：通过 ollama 的本地接口（127.0.0.1:11434）调用，
政策文本不出本机。

**逐条缓存、记原文指纹**：译文存进 translation 表，并记下原文的哈希。
- 重复跑不会重译（全量 1960 万字按 50 tok/s 要几天，任何中断都不该让
  已完成的成果作废）
- 原文若被重新抓取而变了，指纹对不上，译文自动作废重译 —— 否则界面会
  拿旧译文配新原文，这种错最难发现

**分段**：模型上下文有限，长正文按段切块、逐块翻译再拼回。

**必须标注是机器翻译**：译文本旁边记 model 与时间。法律文本译错比不译危险 ——
它读起来通顺，但意思可能偏了，绝不能让读者以为是官方英文版。
"""
from __future__ import annotations

import hashlib
import logging

import httpx

log = logging.getLogger(__name__)

OLLAMA_API = "http://127.0.0.1:11434/api/generate"
DEFAULT_MODEL = "hunyuan-mt"

# 提示词用**官方模板**，不是自己编的。
# 出处：tencent/Hunyuan-MT-7B 模型卡 —— ZH⇔XX 的写法是
#   「把下面的文本翻译成<target_language>，不要额外解释。\n\n<source_text>」
# 另一条实测教训：这个模型**没有默认 system prompt**，且模板不对时
# system 会被静默丢弃 —— 所以术语约束必须写进送去的文本里，不能只放在
# Modelfile 的 SYSTEM 段指望它生效。
PROMPT = "把下面的文本翻译成英文，不要额外解释。\n\n{text}"

# 模型卡给的官方采样参数（此前我用的 0.1/0.9 是自拟，与官方不符）
SAMPLING = {
    "temperature": 0.7,
    "top_k": 20,
    "top_p": 0.6,
    "repeat_penalty": 1.05,
}


def _hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:32]


# 与 assistant.is_available 同样的理由：这个探测要 ~0.8 秒，而它每次都
# httpx 打一次 ollama —— 慢的是 **httpx 库自身**，不是 ollama（实测同一
# 时刻同一 URL：socket 裸连 6ms、urllib 38ms、httpx 793-933ms；已排除
# 代理与冷启动，详见 assistant.py 里 _AVAIL_CACHE 的注释）。
# /health 每次请求都会调它，翻译任务也会。30 秒内复用结果。
_AVAIL_CACHE = None
_AVAIL_TTL = 30.0


def is_available(model: str = DEFAULT_MODEL) -> tuple[bool, str]:
    """本地模型是否就绪。返回 (可用, 说明)。**带 30 秒缓存**。"""
    global _AVAIL_CACHE
    import time

    now = time.time()
    if _AVAIL_CACHE is not None and now - _AVAIL_CACHE[0] < _AVAIL_TTL:
        return _AVAIL_CACHE[1]
    result = _probe_available(model)
    _AVAIL_CACHE = (now, result)
    return result


def _probe_available(model: str) -> tuple[bool, str]:
    """实际探测一次。"""
    try:
        r = httpx.get("http://127.0.0.1:11434/api/tags", timeout=10)
        r.raise_for_status()
        names = [m.get("name", "") for m in r.json().get("models", [])]
    except Exception as e:  # noqa: BLE001
        return False, f"连不上 ollama：{type(e).__name__}: {e}"
    if not any(n.startswith(model) for n in names):
        return False, f"ollama 里没有模型 {model}（现有：{names or '无'}）"
    return True, f"就绪（{model}）"


def translate(text: str, *, model: str = DEFAULT_MODEL, timeout: int = 600,
              prompt: str | None = None, sampling: dict | None = None) -> str:
    """翻译一段文本。失败抛异常，由调用方决定是记 failed 还是跳过。

    ``prompt`` / ``sampling`` 是留给 A/B 实测的**覆盖口子**，默认仍是官方模板
    与官方采样参数 —— 生产路径的行为一字不变。留这个口子的理由：法律文本该用
    多少温度、提示词里要不要塞术语约束，两件事都能讲出道理支持相反的结论，而
    13946 条的量决定了一旦选错就要整体返工，只能实测，不能凭感觉。
    """
    body = (prompt or PROMPT).format(text=text)
    r = httpx.post(
        OLLAMA_API,
        json={
            "model": model,
            "prompt": body,
            "stream": False,
            # num_predict 放前面，让 sampling 能连它也一起覆盖
            "options": {**SAMPLING, "num_predict": 4096, **(sampling or {})},
        },
        timeout=timeout,
    )
    r.raise_for_status()
    out = (r.json().get("response") or "").strip()
    if not out:
        raise RuntimeError("模型返回空译文")
    return out


# ⚠ 下面两个函数**不是死代码，不许删**。
#
# 它们在提交 f063f56「删净死代码」里被当成"零引用"删过一次，后果是
# **正文翻译当场停摆**：唯一的调用点是 scripts/translate_batch.py 里的
# ``tl.translate_long(...)`` —— 别名导入（``import translate_llm as tl``）
# 加属性访问，按函数名做静态扫描**看不到它**。
#
# 删任何"零引用"函数之前，务必再搜一遍 ``\.函数名`` 这种属性访问形式，
# 以及 scripts/ 与 tools/ 这两个不含在包内、最容易被扫描漏掉的目录。
# tests/test_llm_contract.py 把这条契约钉住了。


def split_chunks(text: str, max_chars: int = 1200) -> list[str]:
    """按段落切块，尽量避免把一句话劈开。

    中文政策正文段落很长，所以按「段落」优先，段落超长再按句号切。
    """
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    buf = ""
    for para in text.split("\n"):
        para = para.strip()
        if not para:
            continue
        if len(para) > max_chars:
            # 长段落按句号切
            for sent in para.replace("。", "。\n").split("\n"):
                if not sent.strip():
                    continue
                # **切不动就硬切。** 这是实测挖出来的一个真 bug：中文政策里的
                # 清单惯用「、」「；」分隔，整段可能一个句号都没有 —— 旧逻辑
                # 于是把 25571 字原样当成「一个句子」，再原样变成一块。
                # 后果很具体：26108 字的《西部地区鼓励类产业目录》只被切成
                # 2 块（25571 + 536），模型收到 25571 字的输入只能「概括」，
                # 译文里还混进了「由于文本内容较长……请提供全部文本」这类
                # 自述，而读者完全看不出来少了什么。
                # 按 max_chars 硬切会切断词句，但**切歪一句**远好过**吞掉全文**。
                while len(sent) > max_chars:
                    if buf:
                        chunks.append(buf.strip())
                        buf = ""
                    chunks.append(sent[:max_chars].strip())
                    sent = sent[max_chars:]
                if len(buf) + len(sent) > max_chars and buf:
                    chunks.append(buf.strip())
                    buf = ""
                buf += sent
        else:
            if len(buf) + len(para) > max_chars and buf:
                chunks.append(buf.strip())
                buf = ""
            buf += para + "\n"
    if buf.strip():
        chunks.append(buf.strip())
    return chunks


def translate_long(text: str, *, model: str = DEFAULT_MODEL,
                   max_chars: int = 1200, prompt: str | None = None,
                   sampling: dict | None = None) -> str:
    """长文本分块翻译再拼回。块间用空行分隔，保持可读。

    ``prompt`` / ``sampling`` 透传给 :func:`translate`（A/B 实测用），默认不变。
    """
    parts = split_chunks(text, max_chars)
    if not parts:
        return ""
    outs = [translate(p, model=model, prompt=prompt, sampling=sampling)
            for p in parts]
    return "\n\n".join(outs)


# ---------------------------------------------------------------- 落库

def ensure_table(conn) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS translation ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " doc_uid TEXT NOT NULL,"
        " field TEXT NOT NULL,"          # title / content / attachment:<id>
        " lang TEXT NOT NULL DEFAULT 'en',"
        " src_hash TEXT NOT NULL,"       # 原文指纹：对不上就重译
        " text TEXT NOT NULL,"
        " model TEXT,"
        " created_at TEXT NOT NULL,"
        " UNIQUE(doc_uid, field, lang))")
    conn.commit()


def cached(conn, doc_uid: str, field: str, src_text: str,
           lang: str = "en") -> str | None:
    """取缓存译文；原文变过（指纹不符）则返回 None 表示需重译。"""
    row = conn.execute(
        "SELECT text, src_hash FROM translation"
        " WHERE doc_uid=? AND field=? AND lang=?", (doc_uid, field, lang)).fetchone()
    if row is None:
        return None
    return row["text"] if row["src_hash"] == _hash(src_text) else None


def save(conn, doc_uid: str, field: str, src_text: str, translated: str,
         model: str = DEFAULT_MODEL, lang: str = "en",
         commit: bool = True) -> None:
    """写入一条译文。

    ``commit=False`` 供批量场景使用：每写一条就 commit 会让翻译与其它任务
    （效力判定、采集）频繁争抢 SQLite 写锁 —— 实测两边都慢十倍以上。
    攒一批再提交可显著缓解，调用方自己控制提交时机。
    """
    import datetime
    conn.execute(
        "INSERT INTO translation (doc_uid, field, lang, src_hash, text, model, created_at)"
        " VALUES (?,?,?,?,?,?,?)"
        " ON CONFLICT(doc_uid, field, lang) DO UPDATE SET"
        "   src_hash=excluded.src_hash, text=excluded.text,"
        "   model=excluded.model, created_at=excluded.created_at",
        (doc_uid, field, lang, _hash(src_text), translated, model,
         datetime.datetime.now().isoformat(timespec="seconds")))
    if commit:
        conn.commit()


def progress(conn) -> dict:
    """翻译进度统计。"""
    rows = conn.execute(
        "SELECT field, COUNT(*) AS n FROM translation GROUP BY field").fetchall()
    done = {r["field"]: r["n"] for r in rows}
    total = conn.execute("SELECT COUNT(*) FROM policy").fetchone()[0]
    n_content = conn.execute(
        "SELECT COUNT(*) FROM policy WHERE IFNULL(content,'')<>''").fetchone()[0]
    return {"translated_titles": done.get("title", 0), "total_policies": total,
            "translated_contents": done.get("content", 0),
            "policies_with_content": n_content}
