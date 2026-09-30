"""用本地 ollama 模型翻译政策标题与正文。
======================================================================
为什么是本地模型
======================================================================

① **完全离线**：1978 万字不送到任何外部服务。政策虽是公开信息、出网不违边界，
   但近 2000 万字的外发仍是不可接受的风险与成本。
② **免费**：本地推理只花电费。
③ **可复现**：同一模型同一参数，结果稳定，便于建立索引。

为什么选 ollama 而不是 transformers+torch：ollama 已在用户机器上装好并在跑
（端口 11434），不必再装几 GB 的 CUDA 轮子——在 Windows 上那件事很容易失败。

======================================================================
质量边界（必须写进页面，不能省）
======================================================================

**这是机器翻译，不是专业法律翻译。** 7B 模型处理税务法规，能做到"读得懂"，
但**不保证术语精确**（"加计扣除""留抵退税""汇算清缴"这类需要专门对照）。
所以：

- 中文原文永远是权威版本，英文只是阅读辅助
- 每页标注"机器翻译，以中文原文为准，不得作为官方英文文本引用"
- 术语一致性靠提示词里的对照表约束

宁可标注"机器翻译"，也不要让它看起来像官方英文文本。
"""
from __future__ import annotations

import datetime
import json
import logging
import urllib.error
import urllib.request

log = logging.getLogger(__name__)

OLLAMA_API = "http://127.0.0.1:11434/api/generate"
DEFAULT_MODEL = "qwen2.5:7b"

# 术语对照：让模型在译文中使用固定译法，避免同词多译
TERM_GUIDE = (
    "企业所得税 enterprise income tax；个人所得税 individual income tax；"
    "增值税 value-added tax (VAT)；消费税 consumption tax；印花税 stamp duty；"
    "加计扣除 super-deduction；留抵退税 VAT credit refund；汇算清缴 annual final settlement；"
    "小规模纳税人 small-scale taxpayer；一般纳税人 general taxpayer；"
    "扣缴义务人 withholding agent；税收协定 tax treaty；转让定价 transfer pricing；"
    "国家税务总局 State Taxation Administration；财政部 Ministry of Finance；"
    "国务院 State Council；公告 Announcement；通知 Notice；办法 Measures；"
    "规定 Provisions；批复 Official Reply；解读 Interpretation"
)

SYSTEM_PROMPT_TITLE = (
    "你是中国税务法规的专业译者。把用户给的中文**政策标题**译成英文，要求：\n"
    "1. 使用标准税务术语，严格遵循这份对照表：" + TERM_GUIDE + "\n"
    "2. 文号保持原样不翻译（如「财税〔2016〕36号」写作 Cai Shui [2016] No.36）。\n"
    "3. 专有名词（机构名、奖项名、项目名）用通行英文或音译，不要漏译。\n"
    "4. 只输出译文本身，不要解释、不要加引号、不要重复原文。\n"
    "5. 这是标题，采用 Title Case 风格。"
)

# 正文的提示词与标题分开：实测把「标题式大写」的要求混在一起时，
# 模型会对**正文**也输出全大写 —— 那是能读但很别扭的格式。
SYSTEM_PROMPT_BODY = (
    "你是中国税务法规的专业译者。把用户给的中文**政策正文**译成英文，要求：\n"
    "1. 使用标准税务术语，严格遵循这份对照表：" + TERM_GUIDE + "\n"
    "2. 文号保持原样不翻译（如「财税〔2016〕36号」写作 Cai Shui [2016] No.36）。\n"
    "3. 专有名词（机构名、奖项名、项目名）用通行英文或音译，不要漏译。\n"
    "4. 保持原文的段落结构，条款编号（一、二、（一）1.）照原样保留。\n"
    "5. **用正常的英文句子大小写，不要全大写**。\n"
    "6. 只输出译文本身，不要解释、不要加引号、不要重复原文。"
)

DISCLAIMER = "机器翻译（本地模型），仅供参考；以中文原文为准，不作为官方英文文本引用。"


def ollama_available(timeout: float = 4.0) -> bool:
    """ollama 服务是否在跑。"""
    try:
        with urllib.request.urlopen(
                "http://127.0.0.1:11434/api/tags", timeout=timeout) as resp:
            return resp.status == 200
    except Exception:  # noqa: BLE001
        return False


def list_models() -> list[str]:
    """列出 ollama 已下载的模型。"""
    try:
        with urllib.request.urlopen(
                "http://127.0.0.1:11434/api/tags", timeout=6) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return [m.get("name", "") for m in data.get("models", [])]
    except Exception:  # noqa: BLE001
        return []


def translate_text(text: str, *, model: str = DEFAULT_MODEL,
                   timeout: float = 120.0, title: bool = False) -> str:
    """把一段中文译成英文。失败时返回空串（调用方据此保留原文，不写半截译文）。

    ``title=True`` 用标题式提示词（Title Case），否则用正文式（正常大小写）。
    """
    src = (text or "").strip()
    if not src:
        return ""
    payload = json.dumps({
        "model": model,
        "system": SYSTEM_PROMPT_TITLE if title else SYSTEM_PROMPT_BODY,
        "prompt": src,
        "stream": False,
        "options": {"temperature": 0.1, "num_predict": 2048},
    }).encode("utf-8")
    req = urllib.request.Request(
        OLLAMA_API, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return (data.get("response") or "").strip()
    except urllib.error.URLError as e:
        log.warning("翻译请求失败：%s", e)
        return ""
    except Exception as e:  # noqa: BLE001
        log.warning("翻译异常：%s", e)
        return ""


# ---------------------------------------------------------------- 批处理
#
# **可续跑是硬要求**：正文 806 万字按本地 7B 的速度要跑约两天，中途断电、
# 重启、用户关窗口都是常态。所以每 20 条提交一次，且默认只译还没译过的 ——
# 重跑不会从头开始，也不会把已完成的成果丢掉。


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _batch(conn, sql: str, table: str, col: str, model: str,
           timeout: float, label: str, *, title: bool = False) -> dict:
    rows = conn.execute(sql).fetchall()
    stats = {"total": len(rows), "done": 0, "failed": 0}
    for r in rows:
        en = translate_text(r["src"], model=model, timeout=timeout, title=title)
        if not en:
            # 失败就跳过，**不写半截译文**：宁可保持空白（界面回落中文），
            # 也不要让人读到一段被截断的英文而以为那就是全文。
            stats["failed"] += 1
            continue
        conn.execute(
            f"UPDATE {table} SET {col}=?, p_translated_at=? WHERE id=?",
            (en, _now(), r["id"]))
        stats["done"] += 1
        if stats["done"] % 20 == 0:
            conn.commit()
            log.info("%s 进度 %s/%s", label, stats["done"], stats["total"])
    conn.commit()
    log.info("%s 完成：%s", label, stats)
    return stats


def translate_titles(conn, *, model: str = DEFAULT_MODEL,
                     limit: int | None = None, only_missing: bool = True) -> dict:
    """批量翻译政策标题 -> ``policy.p_title_en``。"""
    sql = "SELECT id, title AS src FROM policy WHERE IFNULL(title,'')<>''"
    if only_missing:
        sql += " AND IFNULL(p_title_en,'')=''"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return _batch(conn, sql, "policy", "p_title_en", model, 90.0, "标题翻译",
                  title=True)


def translate_contents(conn, *, model: str = DEFAULT_MODEL,
                       limit: int | None = None) -> dict:
    """批量翻译政策正文 -> ``policy.p_content_en``。

    只处理有正文的（部分政策解读类没有正文）。正文长，单条超时放宽到 5 分钟。
    """
    sql = ("SELECT id, content AS src FROM policy"
           " WHERE IFNULL(content,'')<>'' AND IFNULL(p_content_en,'')=''")
    if limit:
        sql += f" LIMIT {int(limit)}"
    return _batch(conn, sql, "policy", "p_content_en", model, 300.0, "正文翻译")
