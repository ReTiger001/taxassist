"""税务助手：把政策库接进对话，做合规判定与优化建议。

======================================================================
它要回答的三个问题（用户原话）
======================================================================

  ① 这个方案/合同 **是否符合现行政策**（合规判定）
  ② 有没有**更优的政策**可用（政策择优）
  ③ 怎么调整才**既合规又省税**（优化建议）

======================================================================
为什么不做 function calling，而做「先检索、再回答」
======================================================================

本地 14B 模型的 function calling 稳定性一般（ollama 各版本行为也不一），
而这里最不能容忍的是**模型凭空编出一条政策** —— 用户拿它做税务决策，
编造的政策比"答不出来"危险得多。

所以走这条路：
    用户的输入 → 规则抽取关键词/税种 → 调 kb 检索（只读）
    → 把**真实检索到的政策**作为上下文喂给模型
    → 让它只基于这些政策回答，并在每条结论后标 doc_uid

代价是灵活性差一点（模型不能自己决定再查什么），换来的是一条硬保证：
**它提到的每一条政策，都真的在库里、都能点回原文**。

======================================================================
沿用 kb 的三条约束
======================================================================

1. **复用 kb 的检索**，不另写一套 —— 否则"网页搜得到、AI 说没有"会
   直接摧毁对系统的信任（docs/AI接入.md 的原话）。
2. **只读**：只调 kb 的只读接口，不与采集/翻译长任务抢写锁。
3. **可溯源**：输出里每条政策带 doc_uid + 文号 + url。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from . import kb

log = logging.getLogger(__name__)

#: 对话模型。选 Qwen2.5-14B-Instruct：中文强、能跑在 16G 显存上，
#: 而 hunyuan-mt 是翻译专用模型，做政策比对与建议明显不够用。
DEFAULT_MODEL = "qwen2.5:14b-instruct"

#: 一次最多带入多少条政策做依据。太多会把上下文撑爆（14B 的窗口有限），
#: 太少又可能漏掉关键文件 —— 8 条是检索质量与窗口占用的折中。
MAX_CONTEXT_POLICIES = 8

#: 意图识别用的税种词。用户不会说"增值税"三个字时也可能在问增值税的事。
_TAX_WORDS = ("增值税", "企业所得税", "个人所得税", "消费税", "关税",
              "印花税", "房产税", "土地增值税", "契税", "车船税",
              "车辆购置税", "资源税", "环境保护税", "城镇土地使用税",
              "耕地占用税", "出口退税", "税收征管")

#: 关注点词 → 附加检索词。让"我们想给员工发期权"这种业务语言也能
#: 检到政策，而不必用户自己知道该搜什么。
_TOPIC_HINTS = (
    (("股权激励", "期权", "限制性股票", "员工持股"),
     "股权激励 个人所得税"),
    (("研发", "加计扣除", "技术创新"), "研发费用 加计扣除"),
    (("小微", "小型微利"), "小微企业 优惠"),
    (("高新", "高新技术企业"), "高新技术企业 优惠"),
    (("并购", "重组", "合并", "分立"), "企业重组 特殊性税务处理"),
    (("跨境", "境外", "出海", "非居民"), "非居民 跨境 税收"),
    (("社保", "五险一金", "用工"), "社会保险费"),
    (("发票", "开票", "增值税专用发票"), "发票 管理"),
    (("亏损", "弥补"), "亏损 结转 弥补"),
    (("捐赠", "公益"), "公益性捐赠 税前扣除"),
    (("股权转让", "股权变更"), "股权转让 所得税"),
    (("出口", "退税", "免抵退"), "出口退税"),
    (("个税", "个人所得税"), "个人所得税"),
    (("合同", "协议"), ""),
)


#: 本机 ollama。数据不出本机 —— 这是全项目的硬约束。
OLLAMA_URL = "http://127.0.0.1:11434"


def chat(messages: list[dict], *, model: str = DEFAULT_MODEL,
         temperature: float = 0.2, timeout: int = 900) -> str:
    """调本机 ollama 做一次对话（非流式）。

    为什么不复用 translate_llm：那一套是**翻译专用**的（prompt 形态、分块、
    缓存键都围绕翻译），而这里要的是通用对话。两者都走 127.0.0.1:11434，
    "数据不出本机"这条一致。
    """
    import httpx

    payload = {"model": model, "messages": messages, "stream": False,
               "options": {"temperature": temperature}}
    try:
        resp = httpx.post(f"{OLLAMA_URL}/api/chat", json=payload,
                          timeout=timeout)
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001 - 上层要拿到人话错误
        raise RuntimeError(
            f"调用本地模型失败（{model}）：{type(exc).__name__}: {exc}"
        ) from exc
    data = resp.json()
    return ((data.get("message") or {}).get("content") or "").strip()


def plan_queries(text: str) -> list[str]:
    """从用户输入里抽出检索词。

    **为什么用规则而不是让模型自己决定检索什么**：模型一次只能看到它自己
    生成的检索词，如果它想偏了，后面全错；而规则抽取是**可复核**的 ——
    用户能看到"我按这几个词去查了"，发现不对可以直接说。
    """
    queries: list[str] = []
    # ① 业界的税种词直接作为检索词（命中率最高）
    for w in _TAX_WORDS:
        if w in text:
            queries.append(w)
    # ② 关注点词映射成政策语言
    for words, hint in _TOPIC_HINTS:
        if any(w in text for w in words) and hint:
            queries.append(hint)
    # ③ 从原话里挑出像"专有名词"的片段（书名号、引号里的内容）
    for m in re.findall(r"[《〈\"“]([^》〉\"”]{3,24})[》〉\"”]", text):
        queries.append(m.strip())
    # ④ 兜底：取原话里最长的几个中文词块
    if not queries:
        chunks = re.findall(r"[\u4e00-\u9fa5]{4,12}", text)
        queries.extend(sorted(set(chunks), key=len, reverse=True)[:3])
    # 去重保序，并限制条数（每条要发一次检索）
    seen: set[str] = set()
    out: list[str] = []
    for q in queries:
        if q and q not in seen:
            seen.add(q)
            out.append(q)
    return out[:6]


def gather_policies(text: str, *, per_query: int = 4) -> list[dict]:
    """按抽出的检索词查库，汇总成候选政策列表（去重，按效力优先）。"""
    collected: dict[str, dict] = {}
    for q in plan_queries(text):
        try:
            res = kb.search(q, limit=per_query)
        except Exception as exc:  # noqa: BLE001 - 检索失败不该让对话崩掉
            log.warning("检索失败 %r: %s", q, exc)
            continue
        # **两个坑都在这一行**：
        #   ① kb.search 返回的键是 ``hits``，不是 ``results``（踩过，
        #      表现是"检索词明明对，却一条都不命中"）
        #   ② 它失败时**不抛异常**，而是把原因放进 ``error``。不看这个字段，
        #      检索失败就会被当成"库里没有相关政策" —— 模型随即在无依据的
        #      情况下作答，这正是最不能接受的失败模式。
        if res.get("error"):
            log.warning("检索 %r 报错：%s", q, res["error"])
            continue
        for hit in res.get("hits", []):
            uid = hit.get("doc_uid")
            if not uid:
                continue
            # **按文号去重**：同一份总局文件常被多个省局转载，检索会返回同文号
            # 的多条（地区分别是贵州 / 北京 / 全国）。给模型 3 条同一政策既浪费
            # 上下文，又会诱使它在回答里重复引用 [1][2][3] 其实是同一份。
            # 同文号只留一条，**优先「全国」** —— 那才是总局原文。
            key = hit.get("doc_no") or uid
            old = collected.get(key)
            if old is None:
                collected[key] = hit
            elif hit.get("region") == "全国" and old.get("region") != "全国":
                collected[key] = hit
    items = list(collected.values())
    # 效力优先：现行有效的排前面 —— 给"优化建议"时基于失效政策会害人
    order = {"现行有效": 0, "尚未生效": 1, "unknown": 2, "已废止": 3}
    items.sort(key=lambda x: order.get(x.get("effect_status") or "unknown", 2))
    return items[:MAX_CONTEXT_POLICIES]


def _policy_block(items: list[dict]) -> str:
    """把检索到的政策整理成给模型看的依据块。

    **字段名以 kb._hit 的返回为准**（doc_no / effect_status / region /
    column / pub_name），不是 policy 表的列名 —— kb 是对外接口，做过归一。

    **每条都带编号与文号**：模型回答引用 [1][2] 即可，用户能点回原文。
    正文只给前 900 字 —— 完整正文动辄几千字，8 条会把窗口撑爆；900 字够模型
    判断"这条讲什么、适不适用"。
    """
    lines: list[str] = []
    for i, p in enumerate(items, 1):
        body = (p.get("content") or p.get("snippet") or "").strip()
        lines.append(
            f"[{i}] {p.get('title', '')}\n"
            f"    文号：{p.get('doc_no') or '（无）'}\n"
            f"    效力：{p.get('effect_status') or '未知'}"
            f"（依据：{p.get('effect_source') or '未判定'}）\n"
            f"    地区：{p.get('region') or '全国'}"
            f"　成文日期：{p.get('cwrq') or '未知'}"
            f"　栏目：{p.get('column') or '—'}\n"
            f"    正文节选：{body[:900]}\n"
            f"    doc_uid：{p.get('doc_uid')}\n")
    return "\n".join(lines)


SYSTEM_PROMPT = """你是一名资深中国税务顾问，服务于会计师事务所的专业人士。\

你必须严格基于【政策依据】里给出的文件回答问题，绝不允许引用依据之外的\
任何政策、文号或条款。如果依据不足以判断，就直说"现有依据不足以判断"，\
并说明还需要哪方面的政策。

回答必须包含这四部分，用小标题分节：

## 一、合规判断
这段方案/事实，对照依据中的政策，哪些是**明确合规**的、哪些**有风险**、\
哪些**明确不允许**。每条判断后面用 [1][2] 标注依据编号。

## 二、适用政策
列出可用的政策及适用条件。**必须区分现行有效与已废止** —— 依据里若标了\
"已废止"，要点明它不能再作为依据。

## 三、优化空间
在合法合规的前提下，有哪些可选的安排（政策选择、时点安排、主体安排、\
优惠适用等）。每条都要写清"依据哪条政策"和"前提条件是什么"。

## 四、风险与待确认
需要客户补充哪些事实、哪些条件不满足就不能用某个方案、哪些属于需要\
事先沟通税务机关的事项。

风格要求：直接、具体、可执行。不要空话套话，不要"建议咨询专业人士"\
这种废话（用户就是专业人士）。数字与条件要写清楚。"""


def build_prompt(text: str, items: list[dict]) -> list[dict]:
    """组装给模型的 messages。"""
    if items:
        evidence = f"【政策依据】（共 {len(items)} 条，均来自本机政策库）\n\n" \
                   f"{_policy_block(items)}"
    else:
        evidence = ("【政策依据】\n\n（检索没有命中任何政策。请在回答里明确说明"
                    "这一情况，并提示用户换关键词，不要凭记忆作答。）")
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"{evidence}\n\n【待分析的内容】\n{text}"},
    ]


def answer(text: str, *, model: str = DEFAULT_MODEL,
           max_context: int = MAX_CONTEXT_POLICIES) -> dict:
    """一次完整问答：抽词 → 检索 → 让模型基于真实政策作答。

    返回 ``{"answer": str, "policies": [...], "queries": [...]}`` ——
    ``policies`` 与 ``queries`` 一并返回给前端，让用户看见"它查了什么、
    依据是哪些"，这是可溯源的一部分。
    """
    queries = plan_queries(text)
    items = gather_policies(text)[:max_context]
    messages = build_prompt(text, items)
    reply = chat(messages, model=model)
    return {"answer": reply, "policies": items, "queries": queries}
