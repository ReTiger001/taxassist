"""税务助手：把政策库接进对话，做合规判定与优化建议。

======================================================================
它要回答的三个问题（用户原话）
======================================================================

  ① 这个方案/合同 **是否符合现行政策**（合规判定）
  ② 有没有**更优的政策**可用（政策择优）
  ③ 怎么调整才**既合规又省税**（优化建议）

用户的四条判据（2026-10-06 确认，改代码前先读这四条）：
  · 判定范围：对照「与该类业务**相关的全部**政策」——不是逐条比 13823 条，
    而是先把内容拆成业务事实，再逐类对照
  · 最优判据：**税负最低 + 风险最低，两者分开列**（不合并成一个分数）
  · 使用者：仅内部（本人与事务所同事），不对外
  · 产出形态：四节报告，**每条结论可点回原文**

======================================================================
流程：拆事实 → 逐类检索 → 结构化作答
======================================================================

    用户的输入（合同/方案/一段想法）
      ↓ ① extract_facts   让模型拆成独立的税务业务事实，每条带检索词
      ↓ ② gather_policies 每条事实**单独**检索，各自拿依据
      ↓ ③ build_prompt    事实与依据配对喂给模型
      ↓ ④ chat            模型只写分析，**效力状态不许它写**
      ↓ ⑤ 前端渲染         效力/文号/链接从 policies 直接取，不经模型

======================================================================
两条硬设计（都是踩出来的，别改回去）
======================================================================

**硬设计一：必须先拆业务事实，不能对整段内容抽词。**

实测（2026-10-06）：问"软件公司年入 5000 万，厂房租给关联公司怎么办"，
关键词撒网抽出了"研发费用 加计扣除"，于是给模型带进两条**集成电路企业**
政策 —— 与厂房租赁毫无关系。原因是整段抽词反映的是"出现频率高的字眼"，
而不是"这件事在税法上属于什么"。拆成事实后逐条检索才不会再跑偏。

**硬设计二：效力状态由程序注入，绝不让模型复述。**

实测（同一次）：依据块里 [3][4] 明明标着"现行有效"，模型在"二、适用政策"
里把它们归进了"**已废止**"一栏。模型会改写它读到的状态 —— 而效力判断错
方向（把失效当有效、把有效当失效）正是最危险的失败模式。

所以：
  · 提示词里明确"效力状态由系统标注，你只引用编号 [1]，不要写效力结论"；
  · 前端渲染依据卡片时，效力标签**取自 policies 数组**，不解析模型输出；
  · 这样即使模型说错，用户看到的效力标签仍然是库里的真值。

======================================================================
沿用 kb 的三条约束
======================================================================

1. **复用 kb 的检索**，不另写一套 —— 否则"网页搜得到、AI 说没有"会直接
   摧毁对系统的信任（docs/AI接入.md 的原话）。
2. **只读**：只调 kb 的只读接口，不与采集/翻译长任务抢写锁。
3. **可溯源**：每条结论带 doc_uid + 文号 + url。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from . import kb

log = logging.getLogger(__name__)

#: 对话模型。选 Qwen2.5-14B-Instruct：中文强、能跑在 16G 显存上。
#: 实测单次问答约 94 秒（15GB 模型 > 16GB 显存，少量层在 CPU），前端要做流式。
DEFAULT_MODEL = "qwen2.5:14b-instruct"

#: 一次最多带入多少条政策。**这个数由 NUM_CTX 反推**：
#: 6 条 × 约 530 字 ≈ 3200 字，加上系统提示与补充说明约 7000 token，
#: 正好装进 8192 的窗口。给 8 条就会溢出，超出部分被静默丢弃 ——
#: 表现是"依据明明在提示词里，模型却像没看见"。
MAX_CONTEXT_POLICIES = 6

#: 最多拆成几条业务事实。超过说明内容太长，先抓主要的 —— 每条事实都要
#: 检索一轮（约 1 秒）并占用上下文（约 1000 字）。
MAX_FACTS = 6

#: 效力优先序，**仅用于相关度并列时的 tie-break**。
#: 踩过的坑（2026-10-06）：曾用它对检索结果整体重排，结果丢掉了 kb 的
#: 相关度顺序 —— 精准命中的文件被泛词命中的无关文件挤到后面，截断时正好
#: 丢掉。相关度是主序，效力只在同分时决定先后。
_EFFECT_ORDER = {"现行有效": 0, "尚未生效": 1, "部分失效": 2, "已废止": 3,
                 "": 4}

#: 本机 ollama。数据不出本机 —— 全项目的硬约束。
OLLAMA_URL = "http://127.0.0.1:11434"

#: 上下文窗口。**必须显式设**：ollama 在这台机器上的默认值是 4096
#: （启动日志里 `default_num_ctx=4096`，按显存估算出来的），而本助手的
#: prompt 带 8 条依据就能到 7000+ token —— 超出部分会被**静默截断**，
#: 表现是"依据明明在提示词里，模型却说没有"。设 8192 并配套把依据条数
#: 压到 6 条以内（见 MAX_CONTEXT_POLICIES），保证 prompt 装得下。
NUM_CTX = 8192

#: 模型驻留时间。默认 5 分钟（OLLAMA_KEEP_ALIVE=5m0s）—— 税务师问完一条
#: 思考几分钟再问下一条是常态，卸载后重加载要 20-60 秒，体验很差。
KEEP_ALIVE = "30m"

#: 税种词。拆事实时给模型提示可用税种，避免它造词。
TAX_TYPES = ("增值税", "企业所得税", "个人所得税", "消费税", "关税", "印花税",
             "房产税", "土地增值税", "契税", "车船税", "车辆购置税", "资源税",
             "环境保护税", "城镇土地使用税", "耕地占用税", "出口退税", "税收征管")


def chat(messages: list[dict], *, model: str = DEFAULT_MODEL,
         temperature: float = 0.2, timeout: int = 900,
         num_predict: int = 2048) -> str:
    """调本机 ollama 做一次对话（非流式）。

    为什么不复用 translate_llm：那一套是**翻译专用**的（prompt 形态、分块、
    缓存键都围绕翻译），而这里要的是通用对话。两者都走 127.0.0.1:11434，
    "数据不出本机"这条一致。

    ``num_predict`` 必须设：14B 模型在四节报告这种结构化长输出上会**重复
    兜圈子**（实测：不设上限时生成 9 分钟仍未结束，而同样的问题在上限内
    只需要 90 秒）。四节报告 2048 token 足够，写不完说明它在绕圈。
    """
    import httpx

    payload = {"model": model, "messages": messages, "stream": False,
               "keep_alive": KEEP_ALIVE,
               "options": {"temperature": temperature,
                           "num_predict": num_predict,
                           "num_ctx": NUM_CTX}}
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


# 模型可用性缓存：**这个探测要 800ms，而页面每次请求都调它**，于是每次点
# 导航都要白等 0.8 秒。慢的不是 ollama（curl 实测 3-5ms），是 httpx 库自身
# 的开销 —— 实测同一时刻同一个 URL：socket 裸连 6ms、urllib 38ms、
# httpx 793-933ms；trust_env=False 无效（排除代理）、第二次调用仍慢
# （排除冷启动）。可用性不需要实时，30 秒内复用结果。
_AVAIL_CACHE = None
_AVAIL_TTL = 30.0


def is_available(model: str = DEFAULT_MODEL) -> tuple[bool, str]:
    """模型是否可用。**带 30 秒缓存**，理由见上面 _AVAIL_CACHE 的注释。"""
    global _AVAIL_CACHE
    import time

    now = time.time()
    if _AVAIL_CACHE is not None and now - _AVAIL_CACHE[0] < _AVAIL_TTL:
        return _AVAIL_CACHE[1]
    result = _probe_available(model)
    _AVAIL_CACHE = (now, result)
    return result


def _probe_available(model: str) -> tuple[bool, str]:
    """实际探测一次。

    页面上要先显示这个状态：模型没起来时让用户点了发送干等 90 秒，
    是最糟的失败方式（他会以为系统坏了）。参考 translate_llm.is_available，
    但这里用 /api/tags 列出模型名做精确比对 —— 只看"能不能连上 ollama"
    不够，还要确认**这个模型真的拉了**。
    """
    import httpx

    try:
        resp = httpx.get(f"{OLLAMA_URL}/api/tags", timeout=5.0)
        resp.raise_for_status()
        names = {m.get("name", "") for m in (resp.json().get("models") or [])}
    except Exception as exc:  # noqa: BLE001
        return False, f"连不上本机 ollama（{OLLAMA_URL}）：{type(exc).__name__}"
    if model in names:
        return True, model
    # 容错：ollama 有时给的名字带或不带 :latest
    stem = model.split(":")[0]
    if any(n.split(":")[0] == stem for n in names):
        return True, model
    return False, (f"ollama 里没有模型 {model}。"
                   f"现有：{'、'.join(sorted(names)) or '无'}。"
                   f"拉取：ollama pull {model}")


def chat_stream(messages: list[dict], *, model: str = DEFAULT_MODEL,
                temperature: float = 0.2, timeout: int = 900,
                num_predict: int = 2048):
    """流式版 ``chat``：逐段 yield 生成的内容。

    **为什么必须流式**：实测一次问答要 90-150 秒（14B 在这台机器上部分层
    跑在 CPU）。让用户对着空白页等一分半，与"豆包式"的对话体验差得太远 ——
    流式让第一个字在几秒内出现，用户能立刻确认"它在干活、方向对不对"。
    """
    import json as _json

    import httpx

    payload = {"model": model, "messages": messages, "stream": True,
               "keep_alive": KEEP_ALIVE,
               "options": {"temperature": temperature,
                           "num_predict": num_predict,
                           "num_ctx": NUM_CTX}}
    with httpx.stream("POST", f"{OLLAMA_URL}/api/chat", json=payload,
                      timeout=timeout) as resp:
        resp.raise_for_status()
        for line in resp.iter_lines():
            if not line:
                continue
            try:
                obj = _json.loads(line)
            except _json.JSONDecodeError:
                continue
            piece = ((obj.get("message") or {}).get("content")) or ""
            if piece:
                yield piece
            if obj.get("done"):
                break


# ------------------------------------------------------------------ ① 拆事实

_FACTS_PROMPT = """你是税务业务分析助手。把下面这段内容拆成**独立的税务业务事实**。

拆解要求：
- 一条事实 = 一个可独立做税务判断的事项
  （例："向核心员工授予股票期权" / "把闲置厂房出租给关联公司"）
- 每条事实配 2-4 个**检索关键词**，要用政策文件的说法，不要用口语
  （"厂房租给关联公司" → ["关联交易", "租赁", "房产税"]）
- 每条事实标注涉及的**税种**，从给定清单里选
- 不遗漏、不重复、不脑补原文没有的事实
- 最多 %d 条；超过就只保留税务影响最大的

可用税种：%s

只输出 JSON 数组，不要解释、不要 markdown 代码块：
[{"fact": "…", "keywords": ["…"], "tax_types": ["…"]}]

待拆解的内容：
---
%s
---"""


def _extract_json_array(text: str) -> list | None:
    """从模型输出里抠出 JSON 数组。

    模型常不听话：包在 ```json 里、前面加句"好的，这是结果"、结尾多逗号。
    所以不能直接 json.loads，要先把最外层的 [ … ] 抠出来再试。
    """
    text = text.strip()
    # 去掉 markdown 代码围栏
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end <= start:
        return None
    raw = text[start:end + 1]
    for attempt in (raw, re.sub(r",\s*([\]}])", r"\1", raw)):
        try:
            data = json.loads(attempt)
        except json.JSONDecodeError:
            continue
        if isinstance(data, list):
            return data
    return None


def extract_facts(text: str, *, model: str = DEFAULT_MODEL,
                  max_facts: int = MAX_FACTS) -> list[dict]:
    """第一步：把内容拆成独立的税务业务事实。

    **失败时降级返回一条「整段」事实**，而不是抛异常：拆解只是为了让检索
    更准，拆不了也还能用关键词兜底，不该因此让整个问答失败。
    """
    prompt = _FACTS_PROMPT % (max_facts, "、".join(TAX_TYPES), text[:12000])
    try:
        raw = chat([{"role": "user", "content": prompt}], model=model,
                   temperature=0.1)
    except Exception as exc:  # noqa: BLE001
        log.warning("拆事实失败，降级为整段检索：%s", exc)
        return [{"fact": text[:200], "keywords": [], "tax_types": []}]

    data = _extract_json_array(raw)
    if not data:
        log.warning("拆事实没解析出 JSON，降级为整段检索。模型原话前 200 字：%s",
                    raw[:200])
        return [{"fact": text[:200], "keywords": [], "tax_types": []}]

    facts: list[dict] = []
    for item in data[:max_facts]:
        if not isinstance(item, dict):
            continue
        fact = str(item.get("fact") or "").strip()
        if not fact:
            continue
        kws = [str(k).strip() for k in (item.get("keywords") or []) if str(k).strip()]
        taxes = [str(t).strip() for t in (item.get("tax_types") or [])
                 if str(t).strip() in TAX_TYPES]
        facts.append({"fact": fact, "keywords": kws, "tax_types": taxes})
    if not facts:
        return [{"fact": text[:200], "keywords": [], "tax_types": []}]
    return facts


# -------------------------------------------------------------- ② 逐类检索


#: 降级抽词用的术语表：**模型没给出关键词时**靠它兜底。
#:
#: 为什么必须有它：降级路径原本是"按 3-10 个汉字机械切片"，切出来的是
#: 「公司把持有的子公司股」这种任意片段 —— 而 kb.search 把每个词包成
#: FTS5 短语（trigram 分词）做匹配，连续十来个字的片段几乎必然 0 命中。
#: 实测：8 个有明确答案的税务问题，这条降级路径 **0/8** 命中。
#:
#: 表里都是**标准说法**（政策标题、正文里就这么写），命中率高得多。
_TOPIC_HINTS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("股权激励", "期权", "限制性股票", "员工持股"), "股权激励"),
    (("股权转让", "股权变更", "转让股权"), "股权转让"),
    (("研发", "加计扣除"), "研发费用加计扣除"),
    (("小微", "小型微利"), "小型微利企业"),
    (("高新", "高新技术"), "高新技术企业"),
    (("并购", "重组", "合并", "分立"), "企业重组"),
    (("跨境", "境外", "非居民", "出海"), "非居民企业"),
    (("社保", "五险一金"), "社会保险费"),
    (("发票", "开票"), "发票"),
    (("亏损", "弥补"), "亏损结转"),
    (("捐赠", "公益"), "公益性捐赠"),
    (("出口", "退税", "免抵退"), "出口退税"),
    (("关联交易", "关联方", "转让定价"), "特别纳税调整"),
    (("房产", "出租", "租金"), "房产税"),
    (("土地增值税",), "土地增值税"),
    (("契税",), "契税"),
    (("印花税",), "印花税"),
    (("增值税",), "增值税"),
    (("个人所得税", "个税"), "个人所得税"),
    (("企业所得税",), "企业所得税"),
)


def _fallback_queries(fact: dict) -> list[str]:
    """事实没给出可用关键词时的兜底检索词。

    **顺序有讲究**：先查术语表（标准说法、命中率高），实在没有才退到
    「取原话里的连续片段」—— 后者是不得已，因为 kb.search 做的是短语匹配，
    长片段基本命不中（实测这条降级路径原来 0/8）。
    """
    text = fact.get("fact") or ""
    out: list[str] = []
    for words, term in _TOPIC_HINTS:
        if any(w in text for w in words) and term not in out:
            out.append(term)
    if out:
        return out[:4]
    # 实在无可依的术语：取较短片段（**不超过 4 个字**）——
    # 越长越难命中，这是实测出来的，不是估的。
    chunks = re.findall(r"[\u4e00-\u9fa5]{2,4}", text)
    seen: set[str] = set()
    picks: list[str] = []
    for c in sorted(chunks, key=len, reverse=True):
        if c not in seen:
            seen.add(c)
            picks.append(c)
    return picks[:3]


def gather_policies(facts: list[dict], *, per_query: int = 4,
                    scan: int = 50) -> dict:
    """第二步：**每条事实单独**检索，返回 ``{事实序号: [政策…]}``。

    ``scan`` 是每个查询词向 kb 要多少条，故意比最终保留数大得多。
    踩过的坑（2026-10-06）：曾经 ``scan=12``，实测问「高新技术企业」时，
    kb 按 **BM25** 排序把「…享受增值税加计抵减政策…」（正文里"技术"出现
    多次的长文档）排进前 12，而标题真正含「高新技术企业」的那份被挤在
    外面 —— 我的标题加权再准也没用，**因为它压根没进入候选**。
    SQLite 检索本身很快，多取几十条的成本可以忽略。

    排序是这个函数的第二个难点，两个坑也踩过：

    **坑一：不能按效力状态重排整个结果。** kb.search 内部是相关度排序，
    这个顺序是有效信息。曾按"现行有效优先"重排，结果是精准命中被泛词命中
    的无关文件挤到后面，截断时正好丢掉。**相关度是主序，效力只做 tie-break。**

    **坑二：多个查询词的结果不能等权混合。** 模型拆事实时会给出
    ``['股权激励', '个人所得税', '企业所得税']`` —— 第一个才是具体词，
    后两个是泛词。所以按**模型给的顺序加权**（它把最具体的放第一个），
    且**标题命中加权 20 倍**（政策文件的标题就是它的适用事项）。

    另外按文号去重并优先"全国"：同一份总局文件常被多省转载（贵州/北京/全国），
    重复喂给模型既浪费上下文又诱导重复引用。
    """
    out: dict[int, list[dict]] = {}
    for idx, fact in enumerate(facts):
        queries = list(fact.get("keywords") or [])
        if fact.get("tax_types"):
            queries = queries + fact["tax_types"][:2]
        if not queries:
            queries = _fallback_queries(fact)

        pool: dict[str, dict] = {}
        score: dict[str, float] = {}
        for qi, q in enumerate(queries[:4]):
            try:
                # **limit 用 scan 而不是 per_query**：见上面 docstring 的坑三。
                res = kb.search(q, limit=scan)
            except Exception as exc:  # noqa: BLE001 - 检索失败不该让对话崩掉
                log.warning("检索失败 %r: %s", q, exc)
                continue
            # **两个坑都在这一处**：
            #   ① kb.search 返回的键是 ``hits``，不是 ``results``（踩过，
            #      表现是"检索词明明对，却一条都不命中"）
            #   ② 它失败时**不抛异常**，而是把原因放进 ``error``。不看这个
            #      字段，检索失败会被当成"库里没有相关政策" —— 模型随即在
            #      无依据的情况下作答，这是最危险的失败模式。
            if res.get("error"):
                log.warning("检索 %r 报错：%s", q, res["error"])
                continue
            # **顺序权重 × 泛词阻尼。**
            # 一半来自"模型给的顺序"（它把最具体的词放第一个）；
            # 另一半来自 kb 返回的 total_matched —— 那就是这个词在全库的
            # 命中数，命中越多说明词越泛。"企业所得税"能命中上千条，而
            # "股权转让"只命中几十条。两者等权时，泛词 rank=0 的结果会压过
            # 具体词 rank=3 的结果，实测「股权转让」因此被挤到第 8 位 ——
            # 而模型只看前 6 条，等于没找到。
            # 1/(1+命中数/200) 做阻尼：200 条以内几乎不降，上千条降到 1/6。
            qw = (1.0 / (qi + 1)) / (1.0 + (res.get("total_matched") or 0) / 200.0)
            for rank, hit in enumerate(res.get("hits", [])):
                uid = hit.get("doc_uid")
                if not uid:
                    continue
                key = hit.get("doc_no") or uid
                old = pool.get(key)
                if old is None or (hit.get("region") == "全国"
                                   and old.get("region") != "全国"):
                    pool[key] = hit
                # **标题命中要加权，倍数必须 > scan（这里 20 > 12）。**
                # kb 用 FTS5 的 BM25 排序，它对长正文里反复出现的词给高分，
                # 于是《…集成电路企业、工业母机企业非货币性资产交换企业
                # 所得税政策…》（正文里举例提到过股权激励）能压过《上市公司
                # 股权激励有关个人所得税政策的公告》。
                # 政策文件的**标题就是它的适用事项**，标题命中是强信号。
                #
                # 为什么倍数要大于 scan：这样"标题命中但排最后"（20/scan）
                # 仍强于"仅正文命中但排第一"（1/1）—— 标题命中严格优先，
                # 倍数内再按相关度排。用 3 倍时实测不够：正确文件在
                # "股权激励"这个词下排第 3，3 倍加权后（0.75）仍输给
                # 仅正文命中的集成电路（1.00）。
                w = qw * (20.0 if q in (hit.get("title") or "") else 1.0)
                score[key] = score.get(key, 0.0) + w / (rank + 1)

        items = sorted(
            pool.values(),
            key=lambda h: (-score.get(h.get("doc_no") or h.get("doc_uid"), 0.0),
                           _EFFECT_ORDER.get(h.get("effect_status") or "", 4)))
        out[idx] = items[:per_query * 2]
    return out


# -------------------------------------------------------------- ③ 组上下文


def _policy_line(i: int, p: dict) -> str:
    """给模型看的一条依据。

    **效力状态如实写进去，但在提示词里禁止模型复述它** —— 它读得到是为了
    理解"这条能不能用"，但不许它转述（实测它会转述错）。

    正文只给前 450 字：**这个长度与 NUM_CTX 绑死**。完整正文动辄几千字，
    6 条就上万 token，一超窗口就被静默截断；而截断发生在末尾，最可能
    丢掉的正是最后一条依据——用户看不到任何提示。
    标题 + 文号 + 效力 + 开头 450 字，足够模型判断"这条讲什么、适不适用"。
    """
    body = (p.get("content") or p.get("snippet") or "").strip()
    return (f"  [{i}] {p.get('title', '')}\n"
            f"      效力：{p.get('effect_status') or '未知'}"
            f"（依据：{p.get('effect_source') or '未判定'}）\n"
            f"      生效范围：{p.get('region') or '全国'}\n"
            f"      正文节选：{body[:450]}\n")


SYSTEM_PROMPT = """你是一名资深中国税务顾问，服务于会计师事务所的专业人士。

【最重要的规则】每条依据的**效力状态由系统标注在方括号内，你不得改写、
不得转述、不得归类**。你只需用编号引用，例如"依据 [1]"。系统会把效力标签
直接显示给用户，你写错会误导他。曾发生过：依据标着"现行有效"，你却把它在
"已废止"栏里列出 —— 绝对不许再犯。如果某条依据的效力状态你不确定，就
完全不提它的效力，只说它的内容。

【第二条规则】只能使用我给你的依据。**绝对不许**引用依据之外的任何政策、
文号、条款，也不许凭记忆补充。依据不足以判断的，明说"现有依据不足以判断"
并说明需要哪方面的政策。

【输出格式】严格按以下四节，用 markdown 二级标题：

## 一、合规判断
逐条对照依据，指出哪些**明确合规**、哪些**有风险**、哪些**明确不允许**。
每条判断后用 [编号] 标注依据。事实若与任何依据都对不上，直接说"无对应依据"。

## 二、适用政策
列出可用政策及其**适用条件**。只写编号、标题、适用条件，**不要写效力状态**
（系统会显示）。

## 三、优化空间
在合法合规前提下，有哪些可选安排（政策选择、时点安排、主体安排、优惠适用）。
分两部分写，**不要合并成一个结论**：
  · **税负最低的方案**：哪个安排税额最小，差多少（能算出数字就写数字）
  · **风险最低的方案**：哪个安排确定性最高、最少争议
两部分可能指向不同做法 —— 这很正常，要说清"要税低选哪个、要稳选哪个"。
每条都要写依据编号与前提条件。

## 四、风险与待确认
需客户补充哪些事实、哪些条件不满足就不能用某方案、哪些需事先与税务机关沟通。

风格：直接、具体、可执行。不要"建议咨询专业人士"这种对专业人士无用的废话。"""


def build_prompt(text: str, facts: list[dict], by_fact: dict[int, list[dict]],
                 max_total: int = MAX_CONTEXT_POLICIES,
                 history: list[dict] | None = None
                 ) -> tuple[list[dict], list[dict], dict[int, list[int]]]:
    """第三步：把「事实 + 它的依据」配对组装。

    **``max_total`` 是全局依据上限，必须设。** 踩过的坑（2026-10-06）：
    只限制了「每条事实多少条」（``per_query * 2``），没限制总数，于是 2 条
    事实就带进 16 条依据、prompt 涨到约 15000 token —— 模型生成 9 分钟
    仍未结束，而同样的问题在 4 条依据时只要 94 秒。prompt 大小直接决定
    等待时间，14B 在这台机器上的 prefill 不快。

    返回 ``(messages, ordered, groups)``：
      · ``ordered`` 按显示顺序排好的政策列表，编号 1..N 与提示词里的 [N] 一致
      · ``groups`` 是 ``{事实序号: [在 ordered 里的 1-based 编号]}``，
        前端据此把依据按事实分组显示。**必须由这里算**：只有这里知道每条
        事实实际分到了哪几条依据（截断逻辑在循环里，外面算不出来）。
    """
    n_facts = max(1, len(facts))
    per_fact = max(2, max_total // n_facts)      # 至少 2 条，否则依据太少
    blocks: list[str] = []
    ordered: list[dict] = []
    groups: dict[int, list[int]] = {}
    n = 0
    for idx, fact in enumerate(facts):
        items = (by_fact.get(idx) or [])[:per_fact]
        lines = [f"### 事实{idx + 1}：{fact['fact']}"]
        if fact.get("tax_types"):
            lines.append(f"（涉及税种：{'、'.join(fact['tax_types'])}）")
        assigned: list[int] = []
        if items:
            lines.append("相关依据：")
            for p in items:
                n += 1
                assigned.append(n)
                ordered.append(p)
                lines.append(_policy_line(n, p))
        else:
            lines.append("相关依据：（检索未命中任何政策，请明确说明这一点）")
        groups[idx] = assigned
        blocks.append("\n".join(lines))

    evidence = "【待分析的业务事实及其相关依据】\n\n" + "\n\n".join(blocks)
    msgs: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
    # 多轮追问：把此前的对话带进来，模型才知道"那如果改成…呢"指的是什么。
    # **只带最近 4 轮，且每轮截断到 1200 字** —— 每轮的 prompt 里已经有
    # 若干条政策依据（几千字），历史若原样带上，上下文会迅速撑爆
    # （NUM_CTX=8192）。所以历史只保留"用户问了什么 + 助手答了什么要点"，
    # **不带当时的依据块** —— 依据由本轮重新检索、重新注入。
    for h in (history or [])[-4:]:
        role = "assistant" if h.get("role") == "assistant" else "user"
        content = str(h.get("content") or "")[:1200]
        if content:
            msgs.append({"role": role, "content": content})
    msgs.append({"role": "user",
                 "content": f"{evidence}\n\n【补充说明】\n{text[:1500]}"})
    return msgs, ordered, groups


# ------------------------------------------------------------------ 主流程


def answer(text: str, *, model: str = DEFAULT_MODEL,
           progress=None) -> dict:
    """一次完整问答：拆事实 → 逐类检索 → 让模型基于真实政策作答。

    ``progress`` 是可选回调 ``f(stage: str)``，用于前端显示进度 ——
    实测一次问答约 94 秒，不让用户对着空白等。

    返回 ``{"answer", "facts", "policies", "by_fact"}``：
      · ``policies`` 是**前端渲染依据卡的唯一数据源**（效力标签取自这里，
        不解析模型输出）
      · ``by_fact`` 说明哪条依据对应哪条事实，便于前端分组展示
    """
    fact_list = extract_facts(text, model=model)
    if progress:
        progress(f"已拆成 {len(fact_list)} 条业务事实")
    by_fact = gather_policies(fact_list)
    if progress:
        total = sum(len(v) for v in by_fact.values())
        progress(f"检索到 {total} 条相关政策")
    messages, ordered, groups = build_prompt(text, fact_list, by_fact)
    if progress:
        progress("正在生成分析…")
    reply = chat(messages, model=model)
    return {"answer": reply, "facts": fact_list, "policies": ordered,
            "groups": groups}
