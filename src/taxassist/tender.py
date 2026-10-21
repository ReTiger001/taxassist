"""招标信息：数据模型、入库、去重、检索。

## 这是什么

给税务师找"税务服务类"招标公告用。与政策库（``policy``）是两个独立的东西：
政策是**参考资料**，招标是**商机**；生命周期、字段、筛选口径都不同，所以单独
一张表，不挤进 ``policy``。

## 数据源与去重

同一项目会在不同阶段发多条公告（招标公告 → 更正公告 → 中标公告）。它们共用
**项目编号**，去重键就用它；``doc_uid`` 仍按"源 + 公告 id"唯一，因为不同公告
的正文都要保留（更正公告里的截止日期才是最新的）。

真正的"同一个标"由 ``project_no`` 关联，列表页按它聚合成一条展示最新阶段。

## 为什么要 ``is_tax`` 标记

CCGP 每天新增数千条公告，税务服务类只占很小一部分。先本地关键词过滤再抓详情，
能省 95% 的抓取时间（详情页要走浏览器，一条 3-5 秒）。
"""
from __future__ import annotations

import logging
import re
from datetime import UTC, datetime

log = logging.getLogger(__name__)

#: **强关键词**：明确指向"税务服务采购"本身，命中即可认定相关。
#:
#: 2026-10-21 实测教训：只用一个宽关键词表会把"**税务局买设备维保**"标成税务服务
#: （标题「国家税务总局云南省税务局网络及安全等设备维保服务项目」——"税务"命中的
#: 是**采购人名称**，不是采购内容）。那类条目对税务师投标毫无意义，反而制造噪音。
STRONG_KEYWORDS: tuple[str, ...] = (
    "税务咨询", "税务代理", "税务服务", "涉税服务", "涉税鉴证", "涉税专业服务",
    "汇算清缴", "税务师", "税务筹划", "税收筹划", "转让定价", "税务尽调",
    "税务检查", "税务稽查", "税务争议", "税务合规", "税收风险", "税收优惠",
    "税务顾问", "税务外包", "税审",
)

#: **弱关键词**：常见于标题，但可能只是采购人名称或品目。
#: 只有**同时**出现"服务/咨询/代理"这类交付形态词，才认定为税务服务。
WEAK_KEYWORDS: tuple[str, ...] = (
    "税务", "涉税", "纳税", "税收", "发票", "关税", "增值税",
    "企业所得税", "个人所得税", "土地增值税", "契税", "印花税",
    "房产税", "消费税", "tax",
)

#: 弱关键词必须搭配的"交付形态"词（说明买的是服务而不是货物）。
SERVICE_FORM_WORDS: tuple[str, ...] = (
    "服务", "咨询", "代理", "鉴证", "审计", "筹划", "顾问", "外包",
    "委托", "尽调", "检查", "评估", "复核", "培训", "宣讲",
)

#: 公告类型 → 中文（页面展示用）。
NOTICE_TYPES: dict[str, str] = {
    "gkzb": "公开招标",
    "zbgg": "中标公告",
    "gzgg": "更正公告",
    "cjgg": "成交公告",
    "qtgg": "其他公告",
    "dyly": "单一来源",
    "jzxcs": "竞争性磋商",
    "xjgg": "询价公告",
}

TENDER_DDL = """
CREATE TABLE IF NOT EXISTS tender (
    doc_uid       TEXT PRIMARY KEY,
    source        TEXT NOT NULL,
    notice_type   TEXT,
    title         TEXT NOT NULL,
    project_no    TEXT,
    purchaser     TEXT,
    agency        TEXT,
    budget        REAL,
    region        TEXT,
    publish_at    TEXT,
    deadline      TEXT,
    url           TEXT,
    content       TEXT,
    summary       TEXT,
    is_tax        INTEGER NOT NULL DEFAULT 0,
    tax_hits      TEXT,
    created_at    TEXT,
    updated_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_tender_publish ON tender(publish_at DESC);
CREATE INDEX IF NOT EXISTS idx_tender_project ON tender(project_no);
CREATE INDEX IF NOT EXISTS idx_tender_istax ON tender(is_tax, publish_at DESC);
CREATE INDEX IF NOT EXISTS idx_tender_region ON tender(region, publish_at DESC);
"""


def ensure_table(conn) -> None:
    """建表 + 补新列（幂等）。SQLite 与 PostgreSQL 语法在此 DDL 上兼容。"""
    for stmt in [s.strip() for s in TENDER_DDL.split(";") if s.strip()]:
        conn.execute(stmt)
    # 2026-10-22 新增：公告阶段（知了标讯的 bid_process）。
    # 值：招标/预招标/采购意向（可投）、中标结果/中标候选人/合同（已结束）。
    # 用户看不出"候选供应商征集"和"中标候选人公示"的区别，必须显式标出来。
    _add_column(conn, "tender", "bid_process", "TEXT")
    conn.commit()


def _add_column(conn, table: str, column: str, ddl: str) -> None:
    """加列（幂等）。

    **失败后必须 rollback**：PostgreSQL 与 SQLite 的关键差异 —— SQLite 的
    "duplicate column" 只是单条语句失败，事务照常；而 PG 一旦某条语句出错，
    整个事务进入 aborted 状态，**后续所有语句被静默丢弃**（不抛错）。

    2026-10-22 踩到的就是这个：`_add_column` 吞掉异常但没 rollback，结果
    同一连接上后续的 `UPDATE ... bid_process` 全部无效，表现为「采集显示
    更新 100 条、数据库里阶段字段仍是空」——而且不报任何错。
    """
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")
        conn.commit()
        log.info("已新增列 %s.%s", table, column)
    except Exception as e:  # noqa: BLE001 - 已存在是常态
        log.debug("加列 %s.%s 跳过（多半已存在）：%s", table, column, e)
        try:
            conn.rollback()   # ← 关键：清掉 PG 的 aborted 事务状态
        except Exception:  # noqa: BLE001
            pass


#: 还能投的阶段（知了标讯 bid_process 的取值）。
BIDDABLE_PROCESSES: tuple[str, ...] = ("招标", "预招标", "采购意向")
#: 已经结束的阶段（中标结果/候选人/合同）—— 投标再去就是白费力气。
FINISHED_PROCESSES: tuple[str, ...] = ("中标结果", "中标候选人", "合同")


def is_biddable(process: str | None) -> bool:
    """这个阶段还能不能投？**未知阶段一律当真**（宁可多显示，不要误杀）。"""
    if not process:
        return True
    return process not in FINISHED_PROCESSES


def find_duplicate(conn, title: str, purchaser: str | None) -> str | None:
    """查同一项目的已有公告（标题 + 采购人相同）。

    知了标讯聚合了多个来源平台，**同一条公告会被重复收录**（bid_id 不同、
    标题日期完全相同）。实测 100 条里有 6 组完全重复。采集时拦掉，页面才干净。
    """
    if not title:
        return None
    if purchaser:
        row = conn.execute(
            "SELECT doc_uid FROM tender WHERE title = ? AND purchaser = ? LIMIT 1",
            (title, purchaser)).fetchone()
    else:
        row = conn.execute(
            "SELECT doc_uid FROM tender WHERE title = ? LIMIT 1", (title,)).fetchone()
    return row["doc_uid"] if row else None


def tax_hits(text: str) -> list[str]:
    """返回命中的税务关键词（空列表 = 不算税务服务）。

    判定分两级：
    1. **强关键词**命中 → 直接认定（"税务咨询""汇算清缴"这类词本身就指明服务）
    2. 只有**弱关键词**（"税务""纳税"）→ 先剔除采购人名称（"税务局"是买家不是
       服务内容），再看剩余文本有没有"服务/咨询/代理"这类交付形态词

    实测案例（2026-10-21）：「国家税务总局云南省税务局网络及安全等设备维保服务
    项目」—— 剔除"税务局"后剩"网络及安全等设备维保服务项目"，没有税务服务词，
    于是正确判为非税务服务（原来会被"税务"+"服务"双重命中而误报）。
    """
    raw = text or ""
    low = raw.lower()
    strong = [k for k in STRONG_KEYWORDS if k.lower() in low]
    if strong:
        return strong
    # 剔除"税务局/国家税务总局"等采购人名称：它们说明**谁在买**，不说明买什么
    cleaned = re.sub(r"国家税务总局|国家税务局|税务局|税务部门|税务机关|税务系统", " ", raw)
    c_low = cleaned.lower()
    weak = [k for k in WEAK_KEYWORDS if k.lower() in c_low]
    if weak and any(w in cleaned for w in SERVICE_FORM_WORDS):
        return weak
    return []


def make_uid(source: str, notice_id: str) -> str:
    """公告唯一键：源 + 公告 id（CCGP 的 id 是 URL 里那串数字）。"""
    return f"{source}:{notice_id}"


def _now() -> str:
    return datetime.now(UTC).astimezone().isoformat(timespec="seconds")


def _clean(s: str | None) -> str | None:
    if s is None:
        return None
    s = re.sub(r"\s+", " ", str(s)).strip()
    return s or None


def _money(s: str | None) -> float | None:
    """从"预算金额：123.45 万元"这类文本里提取**元**。"""
    if not s:
        return None
    t = str(s).replace(",", "").replace("，", "")
    m = re.search(r"([\d.]+)\s*(亿元|万元|元)", t)
    if not m:
        m2 = re.search(r"([\d.]+)", t)
        return float(m2.group(1)) if m2 else None
    v = float(m.group(1))
    unit = m.group(2)
    if unit == "亿元":
        v *= 100_000_000
    elif unit == "万元":
        v *= 10_000
    return round(v, 2)


def upsert(conn, row: dict) -> tuple[str, bool]:
    """写入一条公告。返回 ``(doc_uid, 是否新增)``。

    按 ``doc_uid`` 幂等更新：同一公告重抓不会重复，正文修订会覆盖。

    **不在这里调 ``ensure_table``**：那会在每条记录前执行一次 DDL，而
    ``_add_column`` 里的 ALTER 失败会 rollback **整个事务** —— 把同一批里
    前面已经 UPDATE 的记录一起撤销。2026-10-22 的 bug 就是这个：采集显示
    "更新 50 条"，实际只有最后一条落库，且全程不报错。
    建表由调用方在开始前调一次 ``ensure_table``。
    """
    uid = row["doc_uid"]
    exists = conn.execute("SELECT 1 FROM tender WHERE doc_uid = ?", (uid,)).fetchone()
    now = _now()
    fields = {
        "source": row.get("source"),
        "notice_type": row.get("notice_type"),
        "bid_process": _clean(row.get("bid_process")),
        "title": _clean(row.get("title")),
        "project_no": _clean(row.get("project_no")),
        "purchaser": _clean(row.get("purchaser")),
        "agency": _clean(row.get("agency")),
        "budget": _money(row.get("budget")) if row.get("budget") else None,
        "region": _clean(row.get("region")),
        "publish_at": _clean(row.get("publish_at")),
        "deadline": _clean(row.get("deadline")),
        "url": row.get("url"),
        "content": row.get("content"),
        "summary": _clean(row.get("summary")),
        "is_tax": 1 if row.get("is_tax") else 0,
        "tax_hits": ",".join(row.get("tax_hits") or []),
        "updated_at": now,
    }
    if exists:
        sets = ", ".join(f"{k} = ?" for k in fields)
        conn.execute(f"UPDATE tender SET {sets} WHERE doc_uid = ?",
                     tuple(fields.values()) + (uid,))
        return uid, False
    fields["doc_uid"] = uid
    fields["created_at"] = now
    cols = ", ".join(fields)
    ph = ", ".join("?" for _ in fields)
    conn.execute(f"INSERT INTO tender({cols}) VALUES({ph})", tuple(fields.values()))
    return uid, True


def summary_sentence(content: str, hits: list[str], width: int = 160) -> str:
    """从正文里截一段**含关键词**的句子作为摘要 —— 比正文开头更有信息量。"""
    text = re.sub(r"\s+", " ", content or "").strip()
    if not text:
        return ""
    for k in hits:
        i = text.find(k)
        if i >= 0:
            start = max(0, i - 40)
            return text[start:start + width]
    return text[:width]
