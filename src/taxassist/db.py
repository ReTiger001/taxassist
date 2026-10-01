"""SQLite 存储层：schema、连接、写入助手。

设计要点：
1. **官方字段与本地判定字段分开存**，绝不混用——官方给的效力标注和本系统推断的
   效力结论必须能分别追溯，否则出错时无法判断是官方数据问题还是我们的推断问题。
2. **原文归档可追溯**：每条政策的原始 API 响应与详情页都落盘并记哈希，
   保证"当时看到的就是这个"。
3. **抓取完整性可验证**：fetch_log 同时记录接口声称的 total 与实际抓到的条数，
   两者不等即视为抓取不完整，必须告警而不是静默通过。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .config import DB_PATH, ensure_dirs

SCHEMA_VERSION = 1

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- 元信息：schema 版本、各源上次成功抓取的窗口
CREATE TABLE IF NOT EXISTS meta (
    key         TEXT PRIMARY KEY,
    value       TEXT,
    updated_at  TEXT NOT NULL
);

-- 政策文件主表
-- 命名约定：o_ 前缀 = 官方原始字段；p_ 前缀 = 本系统推断/判定字段
CREATE TABLE IF NOT EXISTS policy (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_uid            TEXT NOT NULL UNIQUE,   -- 官方 id（跨系统稳定标识）
    url                TEXT,                   -- 详情页
    snapshot_url       TEXT,                   -- 官方快照页
    url_md5            TEXT,                   -- 官方 urlMD5

    title              TEXT NOT NULL,
    second_title       TEXT,
    o_column           TEXT,                   -- 栏目：政策法规/政策解读/政策指引
    o_label            TEXT,                   -- 标签：法律/行政法规/税务规范性文件/财税文件…
    o_typename         TEXT,
    o_site_name        TEXT,

    cwrq               TEXT,                   -- 成文日期 YYYY-MM-DD
    pub_date           TEXT,                   -- 发布日期 YYYY-MM-DD
    o_abolish_date     TEXT,                   -- 官方废止日期（若有）

    o_doc_no_raw       TEXT,                   -- 官方 docNo —— 注意：实测只是序号
    o_doc_num          TEXT,                   -- 官方 docNum
    o_doc_type         TEXT,
    o_doc_year         TEXT,
    p_doc_no_full      TEXT,                   -- 本系统拼接的完整文号
    p_doc_no_confidence TEXT,                  -- high/medium/low

    pub_name           TEXT,                   -- 发文机关
    o_file_type        TEXT,                   -- 官方 xxgk_effectLevel（实测是文件类型）
    o_aging            TEXT,                   -- 官方 xxgk_aging（真实时效标注，常为空）
    o_tax_policy       TEXT,
    o_tax_discount     TEXT,
    o_industry_type    TEXT,
    o_taxpayer_type    TEXT,
    o_policy_file_type TEXT,
    o_revise_type      TEXT,
    o_resolve_type     TEXT,
    o_related_policy   TEXT,                   -- 官方"相关文件"名称
    o_keywords         TEXT,
    o_industries       TEXT,

    content            TEXT,                   -- 正文（API 返回）
    zw_content         TEXT,                   -- 正文（政务版）
    short_content      TEXT,

    -- 本地效力判定
    p_effect_status    TEXT DEFAULT 'unknown', -- 现行有效/已废止/部分失效/尚未生效/未知
    p_effect_source    TEXT DEFAULT 'none',    -- official/inferred/manual
    p_effect_reason    TEXT,                   -- 判定理由（人可读）
    p_effect_evidence  TEXT,                   -- 证据原文片段
    p_review_state     TEXT DEFAULT 'auto',    -- auto/needs_review/confirmed

    content_hash       TEXT,
    first_seen_at      TEXT NOT NULL,
    last_seen_at       TEXT NOT NULL,
    last_changed_at    TEXT,
    fetch_count        INTEGER NOT NULL DEFAULT 1,

    -- 详情页补充字段
    p_effective_date    TEXT,                  -- 施行日期（从详情页正文抽取）
    p_detail_fetched_at TEXT,                  -- 详情页最近抓取时间

    -- 地区维度：全国 / 省名。用于按地区筛选与统计
    p_region            TEXT
);

CREATE INDEX IF NOT EXISTS idx_policy_cwrq        ON policy(cwrq DESC);
CREATE INDEX IF NOT EXISTS idx_policy_pubdate     ON policy(pub_date DESC);
CREATE INDEX IF NOT EXISTS idx_policy_column      ON policy(o_column);
CREATE INDEX IF NOT EXISTS idx_policy_docno       ON policy(p_doc_no_full);
CREATE INDEX IF NOT EXISTS idx_policy_effect      ON policy(p_effect_status);
CREATE INDEX IF NOT EXISTS idx_policy_review      ON policy(p_review_state);
CREATE INDEX IF NOT EXISTS idx_policy_urumd5      ON policy(url_md5);

-- 原文归档索引：每条政策的每次抓取快照（响应体存文件，此处只记索引与哈希）
CREATE TABLE IF NOT EXISTS raw_snapshot (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_uid       TEXT,
    source_id     TEXT NOT NULL,
    fetched_at    TEXT NOT NULL,
    http_url      TEXT NOT NULL,
    kind          TEXT NOT NULL,      -- api_json / detail_html / attachment_pdf
    rel_path      TEXT,               -- 相对 RAW_DIR 的归档路径
    content_hash  TEXT,
    size_bytes    INTEGER,
    UNIQUE(doc_uid, kind, content_hash)
);

CREATE INDEX IF NOT EXISTS idx_raw_docuid ON raw_snapshot(doc_uid);

-- 抓取日志：完整性可验证（reported_total vs fetched_count）
CREATE TABLE IF NOT EXISTS fetch_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id       TEXT NOT NULL,
    mode            TEXT NOT NULL,     -- full / incremental / probe
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    window_start    TEXT,
    window_end      TEXT,
    reported_total  INTEGER,           -- 接口声称的总数
    fetched_count   INTEGER,           -- 实际抓到条数
    new_count       INTEGER DEFAULT 0,
    updated_count   INTEGER DEFAULT 0,
    status          TEXT,              -- ok / incomplete / failed
    error           TEXT
);

CREATE INDEX IF NOT EXISTS idx_fetchlog_source ON fetch_log(source_id, started_at DESC);

-- 附件（PDF/Excel 等），供后续下载与解析
CREATE TABLE IF NOT EXISTS attachment (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_uid       TEXT NOT NULL,
    url           TEXT,
    filename      TEXT,
    ext           TEXT,
    raw_path      TEXT,                -- 下载后的本地路径
    parsed_text   TEXT,                -- PDF/Excel 解析出的文本
    parse_status  TEXT DEFAULT 'pending',  -- pending/ok/failed
    parse_error   TEXT,
    created_at    TEXT NOT NULL,
    UNIQUE(doc_uid, url)
);

CREATE INDEX IF NOT EXISTS idx_attach_docuid ON attachment(doc_uid);

-- 政策间关系（引用 / 废止 / 修订 / 替代）
-- 为什么单独建表而不是塞一个字符串字段：废止是"一引多"关系且必须能反向查询
-- （"这条政策被谁废止了"是实务里最常问的问题），同时存在引用方或被引用方
-- 尚未入库的悬空引用，必须允许 dst 为空。
CREATE TABLE IF NOT EXISTS policy_relation (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    src_doc_uid    TEXT NOT NULL,          -- 引用方（"谁说的"）
    dst_doc_uid    TEXT,                   -- 被引用方（已入库时）
    dst_doc_no     TEXT,                   -- 被引用文号（文本，可能尚未入库）
    relation       TEXT NOT NULL,          -- cites/repeals/amends/supersedes/related
    evidence       TEXT,                   -- 证据原文片段
    evidence_source TEXT,                  -- official_field/title/content/manual
    confidence     TEXT DEFAULT 'low',     -- high/medium/low
    created_at     TEXT NOT NULL,
    UNIQUE(src_doc_uid, dst_doc_uid, dst_doc_no, relation)
);

CREATE INDEX IF NOT EXISTS idx_rel_src ON policy_relation(src_doc_uid);
CREATE INDEX IF NOT EXISTS idx_rel_dst ON policy_relation(dst_doc_uid);
CREATE INDEX IF NOT EXISTS idx_rel_dstno ON policy_relation(dst_doc_no);
"""

# FTS5 分词器：中文用 trigram（SQLite >= 3.34），不可用时退回 unicode61
FTS_SCHEMA_TRIGRAM = """
CREATE VIRTUAL TABLE IF NOT EXISTS policy_fts USING fts5(
    title, p_doc_no_full, pub_name, content, o_keywords,
    content='policy', content_rowid='id',
    tokenize='trigram'
);
"""

FTS_SCHEMA_FALLBACK = """
CREATE VIRTUAL TABLE IF NOT EXISTS policy_fts USING fts5(
    title, p_doc_no_full, pub_name, content, o_keywords,
    content='policy', content_rowid='id',
    tokenize='unicode61'
);
"""

FTS_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS policy_ai AFTER INSERT ON policy BEGIN
    INSERT INTO policy_fts(rowid, title, p_doc_no_full, pub_name, content, o_keywords)
    VALUES (new.id, new.title, new.p_doc_no_full, new.pub_name, new.content, new.o_keywords);
END;

CREATE TRIGGER IF NOT EXISTS policy_ad AFTER DELETE ON policy BEGIN
    INSERT INTO policy_fts(policy_fts, rowid, title, p_doc_no_full, pub_name, content, o_keywords)
    VALUES ('delete', old.id, old.title, old.p_doc_no_full, old.pub_name, old.content, old.o_keywords);
END;

CREATE TRIGGER IF NOT EXISTS policy_au AFTER UPDATE ON policy
WHEN old.title IS NOT new.title
  OR old.p_doc_no_full IS NOT new.p_doc_no_full
  OR old.pub_name IS NOT new.pub_name
  OR old.content IS NOT new.content
  OR old.o_keywords IS NOT new.o_keywords
BEGIN
    INSERT INTO policy_fts(policy_fts, rowid, title, p_doc_no_full, pub_name, content, o_keywords)
    VALUES ('delete', old.id, old.title, old.p_doc_no_full, old.pub_name, old.content, old.o_keywords);
    INSERT INTO policy_fts(rowid, title, p_doc_no_full, pub_name, content, o_keywords)
    VALUES (new.id, new.title, new.p_doc_no_full, new.pub_name, new.content, new.o_keywords);
END;
"""


def now_iso() -> str:
    """本地时区的 ISO 时间戳（本地库，用本地时间更便于人工核对）。"""
    return datetime.now().astimezone().replace(microsecond=0).isoformat()


def utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    """建立连接（自动建目录）。"""
    ensure_dirs()
    target = Path(path) if path else DB_PATH
    # busy_timeout：默认只等 5 秒，多个任务同时写库时经常不够 ——
    # 实测正文翻译跑到第 440 条时崩于 "database is locked"（当时另一个
    # 批量任务正持着写锁），白跑一场。
    # 后来又把窗口提到 120 秒：浏览器模式的抓取每条要 10 秒、若攒批提交，
    # 写锁能被持有 200 秒，把并行的采集挤死（内蒙古与贵州都中过招）。
    # **两边要一起做**：这里放长等待，长任务那边把提交粒度改小。
    conn = sqlite3.connect(str(target), timeout=120)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 120000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _supports_trigram(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS _fts_probe USING fts5(x, tokenize='trigram')")
        conn.execute("DROP TABLE IF EXISTS _fts_probe")
        return True
    except sqlite3.OperationalError:
        return False


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> bool:
    """SQLite 不支持 ADD COLUMN IF NOT EXISTS，只能先查 PRAGMA。返回是否新增。"""
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column in existing:
        return False
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")
    return True


def migrate(conn: sqlite3.Connection) -> list[str]:
    """轻量迁移：只做加列 + 必要的数据回填，不做破坏性变更。"""
    applied: list[str] = []
    if _ensure_column(conn, "policy", "p_effective_date", "p_effective_date TEXT"):
        applied.append("policy.p_effective_date")
    if _ensure_column(conn, "policy", "p_detail_fetched_at", "p_detail_fetched_at TEXT"):
        applied.append("policy.p_detail_fetched_at")
    if _ensure_column(conn, "policy", "p_region", "p_region TEXT"):
        applied.append("policy.p_region")
        # 回填存量数据：按站点名识别地区。
        # 只回填 NULL 值，不覆盖已有结果 —— 人工修过的地区不该被自动逻辑改回去。
        from .config import region_from_site_name

        sites = conn.execute(
            "SELECT DISTINCT IFNULL(o_site_name,'') s FROM policy WHERE p_region IS NULL"
        ).fetchall()
        for row in sites:
            conn.execute(
                "UPDATE policy SET p_region=? WHERE p_region IS NULL AND IFNULL(o_site_name,'')=?",
                (region_from_site_name(row["s"]), row["s"]),
            )
    # 本地模型翻译结果。与中文原文**分列存放**：原文永远是权威版本，
    # 译文只作阅读辅助 —— 两者必须能分别取用、分别清空，绝不混在一列里。
    if _ensure_column(conn, "policy", "p_title_en", "p_title_en TEXT"):
        applied.append("policy.p_title_en")
    if _ensure_column(conn, "policy", "p_content_en", "p_content_en TEXT"):
        applied.append("policy.p_content_en")
    if _ensure_column(conn, "policy", "p_translated_at", "p_translated_at TEXT"):
        applied.append("policy.p_translated_at")
    conn.commit()
    return applied


def init_db(conn: sqlite3.Connection) -> str:
    """建表 + 迁移 + 建 FTS。返回使用的分词器名，便于日志记录。"""
    conn.executescript(SCHEMA)
    migrate(conn)
    fts = "trigram" if _supports_trigram(conn) else "unicode61"
    conn.executescript(FTS_SCHEMA_TRIGRAM if fts == "trigram" else FTS_SCHEMA_FALLBACK)
    # 触发器定义变更时必须先删后建：CREATE TRIGGER IF NOT EXISTS 不会替换已有的。
    # （踩过：给 policy_au 加了 WHEN 条件后旧触发器仍在，优化不生效。）
    for trg in ("policy_ai", "policy_ad", "policy_au"):
        conn.execute(f"DROP TRIGGER IF EXISTS {trg}")
    conn.executescript(FTS_TRIGGERS)
    set_meta(conn, "schema_version", str(SCHEMA_VERSION))
    set_meta(conn, "fts_tokenizer", fts)
    conn.commit()
    return fts


def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value, updated_at) VALUES(?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, value, now_iso()),
    )


def sqlite_version(conn: sqlite3.Connection) -> str:
    return conn.execute("SELECT sqlite_version()").fetchone()[0]
