"""本地模型自动审核：给每条译文一个「准不准」的判断，拿不准的升级给人。

使用者的要求是「先翻译、然后本地模型审核、有模棱两可的你亲自审核，站在无人
托管的角度设计」。这个脚本就是中间那一环，也是让「无人托管」成立的关键 ——
没有它，机器只能自检那些**机器可判定**的错（数字、条目、结构），
「意思读偏」这类只有懂语义的读者看得出来的问题就永远漏着。

======================================================================
三档，关键是第三档
======================================================================

    OK                  译文准确
    问题：<一句说明>     译文有明确偏差
    不确定：<一句说明>   模型拿不准，需要人看

**第三档是刻意留的出口**。只给「OK / 问题」两个选项时，模型对拿不准的样本
会硬猜 —— 要么放过真错，要么报出一堆假问题。它的判断力本身是验证过的
（对照测试：把「应当」故意改成 may，它准确报出「应当不能译成may，逾期不予
受理与 Late filings shall be accepted 相反」），但那正是它**确定**的样本。
给它一个「不确定」的出口，「确定的」和「拿不准的」才能分开处理：

    确定的 OK    → 记账，不再打扰人
    确定的问题   → 进重译队列（机器能修）
    拿不准的     → 升级清单（人 / 上层 AI 处理）

======================================================================
一次审核，永久记账
======================================================================

结果落 ``translation_ai_review`` 表，**同一条不重复审**（表里按
(doc_uid, field) 唯一）。理由：审核要占 GPU，而重复审同一条不会得到新信息
—— 除非译文变了。译文变了怎么知道？``translation`` 表存了原文指纹，指纹变
则译文变，那种条目在下一次审核时会因为 reviewed_at 落后于 created_at 而被
重新取到。

用法：
    python scripts/ai_review.py --limit 5        # 先试 5 条看判得准不准
    python scripts/ai_review.py --limit 300      # 常规批量
    python scripts/ai_review.py --stats          # 看三档分布
    python scripts/ai_review.py --field title
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import httpx  # noqa: E402

from taxassist import db as dbmod  # noqa: E402

#: **2026-10-21：拆成"审核判定"与"修复生成"两级模型**（用户选的方案 B）。
#:
#: 原来统一用 qwen3.8:27b。质量确实好 —— 它揪出过 qwen2.5 漏掉的约 25% 真错
#: （「2亿元」→"200 million" 差 10 倍、「2022年10月31日」→"October 30"、
#: 合同款 256,349,924 → 25,634,992 差一个数量级）。
#:
#: 但 27B 在 16GB 卡上有 18/66 层跑在 CPU（**6.8 tok/s**），而长条目的修正译文
#: 可达 2 万字符（约 5000 token）→ 单条 700+ 秒。2026-10-20 用 40 条/轮时
#: **连续两轮撞满 7200s 超时整步丢弃**（第 8、9 轮），剩余 1660 条按当时速度
#: 要跑约 4 天。
#:
#: 拆开之后各用所长：
#:   - **审核判定**（判断有没有错、错在哪）：qwen3:14b —— 全部驻留显存，热态快，
#:     而且"发现问题"这个能力 14B 已经够（它比 qwen2.5 强得多，实测能揪出
#:     2 亿元/日期/术语错误）
#:   - **修复生成**（写出完整修正译文，几千 token）：仍用 qwen3.8:27b ——
#:     这是最容易"改出新错"的环节，值得用更强的模型、多等一会儿
#:
#: 预计整体从约 4 天压到约 1.5 天。
MODEL_AUDIT = "qwen3.8:27b-ud-q3"
MODEL_FIX = "qwen3.8:27b-ud-q3"
#: 兼容旧引用：审核记录里"纯判定"的条目标这个模型名。
MODEL = MODEL_AUDIT
OLLAMA = "http://127.0.0.1:11434/api/generate"
UNSURE_OUT = Path("data/logs/ai_unsure.json")

#: **必须显式关掉思考模式**。Qwen3 系列默认开启 thinking，实测它会把整个
#: num_predict 预算吃在推理上、`response` 直接返回空字符串 ——
#: 那样每条都会解析成"拿不准"，而且是静默的（不报错）。
#: 实测同一问题：默认 27.0s / 输出 200 tok / 回答为空；think=False 后
#: 16.9s / 输出 7 tok / 回答正常。审核是判断题，不需要思维链。
THINK = False

#: 提示词刻意写得短而具体。长篇大论的要求会让 14B 模型抓不住重点；点出三类最
#: 容易读反的地方，比泛泛说「是否准确」有效得多。**必须给出「不确定」这个
#: 出口**，否则它会对拿不准的样本硬猜。
PROMPT = """你是税务政策翻译的校对员。下面是一份中文政策原文和它的机器英译。

判断英译是否准确表达了原文的意思。特别注意这三类最容易读反的地方：
1. 义务方向：「应当」不能译成 may，「可以」不能译成 shall；「不予」与「予以」不能相反
2. 数字、比例、金额、期限
3. 适用条件：谁适用、什么情况下适用

只回答以下三种之一，不要写别的：
OK
问题：<一句话说明哪里不对>
不确定：<一句话说明你拿不准什么>

原文：
{zh}

英译：
{en}
"""


def ensure_table(conn: sqlite3.Connection) -> None:
    """建表（幂等）。PostgreSQL 侧的表由 pg/schema.sql 建，这里只处理 SQLite。"""
    from taxassist.repo import conn as _rc

    if _rc.BACKEND == "pg":
        return
    conn.execute(
        "CREATE TABLE IF NOT EXISTS translation_ai_review ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " doc_uid TEXT NOT NULL,"
        " field TEXT NOT NULL,"
        " verdict TEXT NOT NULL,"          # ok / issue / unsure
        " reason TEXT,"
        " model TEXT,"
        " zh_len INTEGER,"                 # 记下原文长度：指纹比对之外的第二个线索
        " reviewed_at TEXT NOT NULL,"
        " UNIQUE(doc_uid, field))")
    conn.commit()


#: 送进模型的上下文窗口。**32768 是这台机器的上限，且是实测出来的**：
#: - 模型本体 14B-Q4_K_M 常驻显存约 10.4GB，16GB 卡只剩约 5.5GB 给 KV cache
#: - 实测 num_ctx=32768 处理原文 19462 字（prompt 27077 tokens）耗时 51.7s，
#:   模型稳定驻留显存、没有明显 CPU 卸载；处理 11083 字（15751 tokens）19.1s
#: - 超过 32768 就要往 CPU 卸层，吞吐掉得厉害，不划算
#: **别调小**：给小了 ollama 会**静默截断输入**（实测同一请求 ctx=8192 只报
#: 4098 tokens，真实值是 17003），而模型看不到结尾时会把自己"没看到"当成
#: "原文没写"，于是给出假 OK —— 这比不审更危险。
NUM_CTX = 32768

#: 分块审核时每块的原文字数。
#: 实测 1.4 token/字（原文 19462 字 → 27077 tokens），12000 字 ≈ 16800 tokens，
#: 对 32768 留了约一半给输出与模板 —— 余量给足是因为这一路专门处理超长文档，
#: 它们的表格/数字最密，token 比可能更高。
CHUNK_CHARS = 12000


def ask(zh: str, en: str, timeout: int = 600) -> str:
    """**纯判定**用快模型（``MODEL_AUDIT``）：只回答 OK / 问题 / 不确定，输出很短。"""
    r = httpx.post(OLLAMA, json={
        "model": MODEL_AUDIT,
        "prompt": PROMPT.format(zh=zh, en=en),
        "stream": False,
        "think": THINK,
        "options": {"num_ctx": NUM_CTX, "temperature": 0.1, "num_predict": 160},
    }, timeout=timeout)
    r.raise_for_status()
    return (r.json().get("response") or "").strip()


def parse(ans: str) -> tuple[str, str]:
    """把模型的回答归到三档。**认不出来的一律算 unsure** —— 宁可升级给人，
    也不要让一句没读懂的回答冒充「通过」。"""
    t = (ans or "").strip()
    if t.upper().startswith("OK"):
        return "ok", ""
    if t.startswith("问题") or "问题：" in t[:8]:
        return "issue", t.split("：", 1)[-1][:300]
    if t.startswith("不确定") or "不确定：" in t[:12]:
        return "unsure", t.split("：", 1)[-1][:300]
    return "unsure", f"回答无法归类：{t[:200]}"


#: 审核 + **直接给出修正译文**的提示词。
#: 用户要的流程是「千问发现问题 → 千问自己修好」，而不是「发现问题 → 退回混元重译」。
#: 一次调用同时拿到判定与修正，省一轮往返。
FIX_PROMPT = """你是税务政策翻译的校对员兼修理工。下面是一份中文政策原文和它的机器英译。

第一步：判断英译有没有**实质性错误**，只看这几类：
1. 义务方向（"可/应当/不得/准予"）译反或强度译错
2. 数字、比例、金额、日期、期限译错或漏掉
3. 适用条件、主体、范围译错
4. 关键术语译错（如"免征/减征""留抵退税""起征点"）

第二步：
- 若**没有**错误，只回答：OK
- 若**有**错误，先一句话指出问题，再给出**完整的修正后英译**（不要只给片段、不要省略）

严格按此格式，不要额外解释：
OK
或
问题：<一句话>
修正译文：
<完整英文译文>

原文：
{zh}

英译：
{en}"""


#: 单条**直接生成修正译文**的原文上限（字）。
#:
#: 为什么必须设：`_ai_fix` 要模型输出**完整**修正译文，输出量约等于译文长度。
#: 原文 1.5 万字 → 修正译文约 6 万字符 ≈ 7.8 万 token，按 qwen3.8 在本机
#: 6.8 tok/s 要 **3 小时** —— 必然撞 900s 超时（2026-10-21 实测第一条
#: 15811 字直接 ReadTimeout，整轮报废）。
#:
#: 超过这个长度的条目：14b 判为 issue 后**不直接修**，交给第 ⑤ 步
#: `retranslate.py` 用混元重译（它有分块能力，且混元是翻译专长）。
FIX_MAX_CHARS = 5000


def _ai_fix(prompt: str, zh: str, en: str, timeout: int = 900) -> tuple[str, str, str | None]:
    """审核并（如发现问题）给出修正译文。返回 ``(判定, 说明, 修正译文或None)``。

    **这一步用强模型 ``MODEL_FIX``**：要写出几千 token 的完整修正译文，是整个
    流程里最容易"改出新错"的环节；而判定那一步（``ask``）用快模型就够。
    这是 2026-10-21 的分级改造：审核 14b、修复 3.8。

    任意格式异常一律降级为 unsure 且不带修正 —— 与 ``parse`` 同一原则：
    宁可升级给人，也不要让没读懂的回答冒充通过。
    """
    r = httpx.post(OLLAMA, json={
        "model": MODEL_FIX, "prompt": prompt.format(zh=zh, en=en), "stream": False,
        "think": THINK,
        # num_predict **按译文长度动态给**：修正译文与原文等长（英文约 4 倍字符，
        # 约 1.3 token/字符）。给死 8192 会白等（本机约 6.8-29 tok/s，看模型），
        # 万一模型写满 8192 就是几分钟。留 1.5 倍余量给"问题：…"那段说明。
        "options": {"num_ctx": NUM_CTX, "temperature": 0.1,
                    "num_predict": max(512, int(len(en) * 1.3 * 1.5))},
    }, timeout=timeout)
    r.raise_for_status()
    t = (r.json().get("response") or "").strip()
    if t.upper().startswith("OK"):
        return "ok", "", None
    if "修正译文" not in t:
        # 有判定但没给修正 → 拿不准（无法自动修）
        if t.startswith("问题") or "问题：" in t[:8]:
            return "unsure", t.split("：", 1)[-1][:300], None
        return "unsure", f"回答无法归类：{t[:160]}", None
    head, body = t.split("修正译文", 1)
    fixed = body.lstrip("：: \n").strip()
    reason = head.replace("问题：", "").replace("问题:", "").strip()[:300]
    if not fixed:
        return "unsure", reason or "模型未给出修正译文", None
    return "issue", reason, fixed


def _fix_ok(old: str, fixed: str) -> bool:
    """修正译文的廉价校验。**宁可放弃自动修，也不让"修得更差"静默入库。**

    只查两件机械可判的事：
      ① 长度是否与原文同量级（过短多半是模型只回了片段）
      ② 原文里的数字**保留率** —— 这是数字被打错、整段被截断最直接的信号
    """
    if not fixed or len(fixed) < 0.3 * len(old):
        return False
    old_nums = set(re.findall(r"\d+(?:\.\d+)?", old))
    if not old_nums:
        return True
    fixed_nums = set(re.findall(r"\d+(?:\.\d+)?", fixed))
    return len(old_nums & fixed_nums) / len(old_nums) >= 0.7


def _apply_fix(conn, uid: str, field: str, fixed: str, reason: str,
               zh_len: int = 0) -> None:
    """把修正译文落库，并**同步写成审核记录**（verdict 用模型给出的判定）。

    ``created_at`` 不动：它若变新，条目会因为「译文比审核记录新」被无限重审。
    """
    now = dt.datetime.now().isoformat(timespec="seconds")
    conn.execute("UPDATE translation SET text=?, model=? WHERE doc_uid=? AND field=? AND lang=?",
                 (fixed, f"{MODEL_FIX}+fix", uid, field, "en"))
    conn.execute(
        "INSERT INTO translation_ai_review"
        " (doc_uid, field, verdict, reason, model, zh_len, reviewed_at)"
        " VALUES (?,?,?,?,?,?,?)"
        " ON CONFLICT(doc_uid, field) DO UPDATE SET"
        "   verdict=excluded.verdict, reason=excluded.reason, model=excluded.model,"
        "   reviewed_at=excluded.reviewed_at",
        (uid, field, "issue", f"[已修复] {reason}", f"{MODEL_FIX}+fix", zh_len, now))


def _chunk_pair(zh: str, en: str, max_chars: int) -> list[tuple[str, str]]:
    """把超长条目切成若干块，**两个文本按长度比例同切**。

    为什么按比例而不是各自均分：译文与原文段落一一对应，各自均分会让第 k 段
    的中文配上第 k 段的英文的**错位**版本 —— 那等于拿两份无关文本让模型对照，
    比不审更糟。

    切点找不到句末标点时退回均分：**宁可切在句中也不能丢字**，
    丢字会让模型把自己"没看到"当成"原文没写"（同 NUM_CTX 那条假 OK 隐患）。
    """
    n = max(1, -(-len(zh) // max_chars))
    parts: list[tuple[str, str]] = []
    pz = pe = 0
    for i in range(n):
        z_end = len(zh) if i == n - 1 else min(len(zh), (i + 1) * max_chars)
        e_end = len(en) if i == n - 1 else min(len(en), round((i + 1) * len(en) / n))
        # 只有还有富余时才往前找句末 —— 避免把块缩得太短
        win = min(240, max(0, z_end - pz - max_chars // 2))
        if i < n - 1 and win > 0:
            seg = zh[z_end - win:z_end]
            cut = max(seg.rfind(c) for c in "。；！？\n")
            if cut > 0:
                z_end = z_end - win + cut + 1
        parts.append((zh[pz:z_end], en[pe:e_end]))
        pz, pe = z_end, e_end
    return parts


def review_long(zh: str, en: str, max_chars: int | None = None) -> tuple[str, str]:
    """超长条目：分块审核后合并结论。

    合并规则偏保守：**任一块有问题即为有问题**（并标出在哪一块），
    任一块拿不准即为拿不准；全 OK 才算 OK。理由与 ``parse`` 一致 ——
    宁可升级给人，也不要让漏看的块冒充「通过」。

    任何一块失败都返回 unsure 并写明失败位置，**绝不静默丢弃**：
    那等于把"没审到"粉饰成"通过"。
    """
    mc = max_chars or CHUNK_CHARS
    parts = _chunk_pair(zh, en, mc)
    verdicts: list[str] = []
    for idx, (cz, ce) in enumerate(parts, 1):
        try:
            v, _ = parse(ask(cz, ce))
        except Exception as e:  # noqa: BLE001 - 单块失败不中断整条
            return "unsure", f"第 {idx}/{len(parts)} 块审核失败：{type(e).__name__}: {e}"
        verdicts.append(v)
    n_issue = sum(1 for v in verdicts if v == "issue")
    if n_issue:
        loc = "、".join(str(i + 1) for i, v in enumerate(verdicts) if v == "issue")
        return "issue", f"分 {len(parts)} 块审核：第 {loc} 块发现问题"
    if "unsure" in verdicts:
        loc = "、".join(str(i + 1) for i, v in enumerate(verdicts) if v == "unsure")
        return "unsure", f"分 {len(parts)} 块审核：第 {loc} 块拿不准"
    return "ok", ""


def pick_pending(conn, field: str, limit: int, max_chars: int | None,
                 model: str | None = None) -> list[sqlite3.Row]:
    """取待审条目，**按"最可能有问题"优先**。

    ``max_chars=None`` 表示不限长度 —— 分块审核那条路用它把超长条目也捞出来。

    排序（2026-10-17 加）：未审 → 现判 issue → 现判 unsure → 其余。
    为什么优先重审自己的旧判定：库里大批 ``ok`` 是**较弱模型**（qwen2.5:14b）
    下的结论，实测它标 ok 的条目里约 25% 有实质错误；而 ``issue`` 的条目
    直接进第 ⑤ 步修，优先级最高。

    ``model`` 传当前审核模型时，**已由该模型审过的条目不再重审** ——
    否则本轮判为 issue 的条目下一轮又被捞回来，永远在原地打转。
    未审条目不受此限（r.model 为 NULL → 比较得 NULL → 视为不成立，
    由同一条件里的 ``r.doc_uid IS NULL`` 分支放行）。
    """
    zh_col = "p.title" if field == "title" else "p.content"
    hi = "" if max_chars is None else f" AND LENGTH(IFNULL({zh_col}, '')) BETWEEN 1 AND ?"
    params: list = [field]
    if max_chars is not None:
        params.append(max_chars)
    # 待审条件：未审过 / 译文在上次审核后又被重译过 /
    # 现判 issue·unsure 且**不是当前模型**审的（换模型后要复审，但不能每轮重复审）
    cond = "(r.doc_uid IS NULL OR t.created_at > r.reviewed_at"
    if model is not None:
        # `!=` 而非 `IS NOT`：后者是 `IS DISTINCT FROM` 的语法位，
        # PG 不接受参数占位符（实测报 `语法错误 在 "$3" 或附近`）。
        cond += " OR (r.verdict IN ('issue','unsure') AND r.model != ?)"
        params.append(model)
    cond += ")"
    params.append(limit)
    return conn.execute(
        f"SELECT t.doc_uid, {zh_col} AS zh, t.text AS en, t.created_at AS made,"
        f" r.verdict AS prev_verdict, r.model AS prev_model"
        f" FROM translation t"
        f" JOIN policy p ON p.doc_uid = t.doc_uid"
        f" LEFT JOIN translation_ai_review r ON r.doc_uid = t.doc_uid"
        f"   AND r.field = t.field"
        f" WHERE t.field = ? AND t.text IS NOT NULL"
        f"{hi}"
        f"   AND {cond}"
        f" ORDER BY CASE WHEN r.verdict IN ('issue','unsure') THEN 0"
        f"               WHEN r.doc_uid IS NULL THEN 1"
        f"               ELSE 2 END,"
        f"          t.created_at DESC LIMIT ?",
        tuple(params)).fetchall()


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--field", choices=("title", "content"), default="content")
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--max-chars", type=int, default=20000,
                    help="**单次送模型的原文上限**，超过的会分块审核（不是跳过）。默认"
                         " 20000 是实测倒推的：NUM_CTX=32768 下原文 19462 字"
                         "（译文 73315 字符）的 prompt 实测 27077 tokens，留了约 5000"
                         " token 给输出；约 24000 字即顶到 32768。")
    ap.add_argument("--chunk-chars", type=int, default=CHUNK_CHARS,
                    help=f"分块审核时每块的原文字数（默认 {CHUNK_CHARS}，"
                         "按 1.4 token/字 与 32768 上下文倒推，留足输出余量）")
    ap.add_argument("--max-block-docs", type=int, default=5,
                    help="每轮最多分块审核几篇超长文档（默认 5）。**限量是刻意的**："
                         "一篇 11 万字要切 10 块、耗时几分钟，不限量会挤掉正常队列"
                         "并撞上工作流的 7200s 超时")
    ap.add_argument("--stats", action="store_true")
    args = ap.parse_args()

    conn = dbmod.connect()
    try:
        ensure_table(conn)
        if args.stats:
            rows = conn.execute(
                "SELECT field, verdict, COUNT(*) n FROM translation_ai_review"
                " GROUP BY field, verdict ORDER BY field, verdict").fetchall()
            if not rows:
                print("还没有任何 AI 审核记录。")
                return 0
            for r in rows:
                print(f"  {r['field']:8} {r['verdict']:7} {r['n']} 条")
            return 0

        # 两条队列：正常长度的直接审；超长的分块审（否则它们永远轮不到）。
        # 分块的那条**每轮限量**（--max-block-docs），免得多篇 11 万字文档
        # 挤掉正常队列并撞上工作流的 7200s 超时。
        normal = pick_pending(conn, args.field, args.limit, args.max_chars, model=MODEL_AUDIT)
        blocks = pick_pending(conn, args.field, args.max_block_docs, None, model=MODEL_AUDIT)
        blocks = [r for r in blocks if len(r["zh"] or "") > args.max_chars]
        pending = [(r, False) for r in normal] + [(r, True) for r in blocks]
        print(f"[{args.field}] 待审 {len(normal)} 条（≤{args.max_chars} 字）"
              f" + 超长 {len(blocks)} 条（分块，每轮上限 {args.max_block_docs}）"
              f"　新的优先\n")
        if not pending:
            return 0

        counts = {"ok": 0, "issue": 0, "unsure": 0, "fixed": 0}
        unsure: list[dict] = []
        t0 = time.time()
        for i, (r, is_block) in enumerate(pending, 1):
            fixed: str | None = None
            # 上一轮修过 → 本轮是**复查**。复查只审核、不重修：
            # 若仍判 issue 就降级 unsure 交给人工。否则会「修→审→还 issue→再修」
            # 无限循环 —— 实测 163 条复查里 79 条又判 issue，每轮 50 条有大量
            # 配额耗在同一批条目上（2026-10-19 观测：9 轮只净增 73 条修复）。
            is_recheck = "+fix" in (r["prev_model"] or "")
            try:
                if is_block:
                    # 超长条目：分块审核（超长文档的修复交给第 ⑤ 步重译，
                    # 因为修正译文比原文更长，分块改写风险高于收益）
                    verdict, reason = review_long(r["zh"], r["en"], args.chunk_chars)
                elif is_recheck:
                    # **复查用 ask(不生成修正)**：`_ai_fix` 会让模型再写一遍完整
                    # 译文（约 124s），而复查只需要一个判定（约 60s）。
                    verdict, reason = parse(ask(r["zh"], r["en"]))
                    if verdict == "issue":
                        verdict = "unsure"
                        reason = f"修复后复查仍发现问题：{reason[:180]}"
                else:
                    # **分级改造（2026-10-21）**：先用快模型（14b）做判定，
                    # **只有判为 issue 才调强模型（3.8）生成修正译文**。
                    # 原来是一步 `_ai_fix`（3.8 同时判定+修），于是每条 OK 也
                    # 要付 3.8 的速度代价（6.8 tok/s）—— 而 OK 占未审条目的多数。
                    # 现在 OK 只花一次 14b 调用，issue 才多一次 3.8。
                    verdict, reason = parse(ask(r["zh"], r["en"]))
                    if verdict == "issue":
                        if len(r["zh"] or "") > FIX_MAX_CHARS:
                            # **长条目不在这一步直接修**：生成完整修正译文会超时
                            # （见 FIX_MAX_CHARS 的说明）。保持 issue 判定，
                            # 由第 ⑤ 步 retranslate 用混元分块重译修复。
                            reason = (f"{reason}（原文 {len(r['zh'] or '')} 字，"
                                      f"超过直接修复上限 {FIX_MAX_CHARS}，交第⑤步重译）")
                        else:
                            _, fix_reason, fixed = _ai_fix(FIX_PROMPT, r["zh"], r["en"])
                            if fixed is None:
                                # 3.8 也没能给出修正（或格式坏）→ 不硬修，交给人
                                verdict = "unsure"
                            reason = fix_reason or reason
            except Exception as e:  # noqa: BLE001 - 单条失败不中断整批
                print(f"  [{i}/{len(pending)}] 失败 {r['doc_uid'][:36]}: "
                      f"{type(e).__name__}: {e}", flush=True)
                continue

            fix_note = ""
            if fixed is not None:
                if _fix_ok(r["en"], fixed):
                    _apply_fix(conn, r["doc_uid"], args.field, fixed, reason,
                               len(r["zh"] or ""))
                    # **每条修正立即提交**，不等每 20 条的批量提交：
                    # 单条修复实测约 45-230s，攒 20 条意味着最多半小时的成果
                    # 悬在未提交事务里，进程一被 kill 就全丢 —— 而这些是已花掉
                    # 的模型时间，重跑代价极高。
                    conn.commit()
                    counts["fixed"] += 1
                    fix_note = f" → 已修正 {len(fixed)} 字符"
                else:
                    # 修正没过校验：降级为拿不准，**绝不把可疑修正写进库**
                    verdict = "unsure"
                    reason = f"修正未通过校验（长度/数字对不上）：{reason[:120]}"
                    fixed = None

            counts[verdict] += 1
            if fixed is None:
                conn.execute(
                    "INSERT INTO translation_ai_review"
                    " (doc_uid, field, verdict, reason, model, zh_len, reviewed_at)"
                    " VALUES (?,?,?,?,?,?,?)"
                    " ON CONFLICT(doc_uid, field) DO UPDATE SET"
                    "   verdict=excluded.verdict, reason=excluded.reason,"
                    "   model=excluded.model, reviewed_at=excluded.reviewed_at",
                    (r["doc_uid"], args.field, verdict, reason, MODEL,
                     len(r["zh"] or ""), dt.datetime.now().isoformat(timespec="seconds")))
            if verdict == "unsure":
                unsure.append({"uid": r["doc_uid"], "reason": reason,
                               "zh_len": len(r["zh"] or "")})
            if i % 20 == 0:
                conn.commit()
            mark = {"ok": "OK ", "issue": "问题", "unsure": "拿不准"}[verdict]
            tag = "[长]" if is_block else ""
            print(f"  [{i}/{len(pending)}] {tag}{mark} {r['doc_uid'][:36]:<36} "
                  f"{reason[:60]}{fix_note}", flush=True)
        conn.commit()
    finally:
        conn.close()

    el = (time.time() - t0) / 60
    tot = sum(counts.values())
    print(f"\n完成：{tot} 条，用时 {el:.1f} 分钟")
    print(f"  OK {counts['ok']} ／ 有问题 {counts['issue']}（其中已修正并落库 "
          f"{counts['fixed']}）／ 拿不准 {counts['unsure']}")
    if unsure:
        # **从库里取全部 unsure，而不是只写这一批** —— 第一版把局部变量
        # `unsure` 直接覆盖写进文件，于是每次运行都冲掉上一批：库里累计 54 条，
        # 文件里只剩最后一批的 3 条。而「等人看」要的正是完整清单，
        # 少的那 51 条等于没交给人。
        try:
            c2 = dbmod.connect()
            try:
                all_unsure = c2.execute(
                    "SELECT doc_uid, reason FROM translation_ai_review"
                    " WHERE field=? AND verdict='unsure'"
                    " ORDER BY reviewed_at DESC", (args.field,)).fetchall()
            finally:
                c2.close()
            UNSURE_OUT.write_text(json.dumps(
                [{"uid": r["doc_uid"], "reason": r["reason"]} for r in all_unsure],
                ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"\n拿不准的**累计** {len(all_unsure)} 条已写入 {UNSURE_OUT}")
        except Exception as e:  # noqa: BLE001 - 清单写不出来不该影响主流程
            print(f"（写 unsure 清单失败：{e}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
