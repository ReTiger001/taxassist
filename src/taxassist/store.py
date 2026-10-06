"""入库层：政策行 upsert、原始响应归档、抓取日志。

三条不可妥协的规则：

1. **归档原始响应**：抓取当时官方返回了什么，就落盘保存什么（gzip JSONL）。
   政策网站改版、文件被撤下时，这是唯一能证明"当时依据是什么"的证据。
2. **完整性可验证**：fetch_log 同时记 reported_total 与 fetched_count，
   两者不等标为 ``incomplete``——宁可报错，也不让"漏抓"看起来像"没政策"。
3. **变更留痕**：政策正文发生变化时更新 last_changed_at 并保留哈希，
   而不是静默覆盖（政策"悄悄改一句话"在实务中确实会发生）。
"""
from __future__ import annotations

import gzip
import hashlib
import json
import logging
import re
from pathlib import Path

from .config import RAW_DIR, ensure_dirs
from .db import now_iso

log = logging.getLogger(__name__)

# 列表页标题**完全不是标题**的情形 —— 只给了个日期，如广西的 "2026-09-28"。
# 单独提出来是因为它触发的修复路径与「标题被截断」不同：截断可以用列表标题
# 做前缀定位，纯日期则根本无从定位（见 apply_enrichment 里的两分支）。
_DATE_ONLY_TITLE_RE = re.compile(
    r"^\s*(?:19|20)?\d{2}\s*[-./年]\s*\d{1,2}\s*[-./月]\s*\d{1,2}\s*日?\s*$")

# 政策标题的结尾体裁词。用来判断一段文本"像不像标题" —— 专门用来区分
# 「干净的真标题」与「站点名+栏目名」（后者实测如
# "国家税务总局河北省税务局 最新文件"，结尾不是体裁词）。
_TITLE_TAIL_WORDS = (
    "公告", "通知", "办法", "规定", "决定", "批复", "意见", "细则",
    "条例", "制度", "指引", "清单", "目录", "解读", "答复", "函",
    "规则", "标准", "基准", "方案", "规程", "规范", "计划", "报告",
    "通告", "公告）", "公告)", "通知）", "通知）",
)

# 参与内容哈希的字段：只含"政策本身变了才应该变"的字段，
# 不含 last_seen_at / fetch_count 这类每次抓取都会变的计数列。
HASH_FIELDS = (
    "title", "second_title", "cwrq", "pub_date", "pub_name",
    "o_doc_no_raw", "o_aging", "o_abolish_date", "o_file_type",
    "content", "zw_content", "o_tax_policy", "o_related_policy",
)


def content_hash(row: dict) -> str:
    payload = json.dumps({k: row.get(k) for k in HASH_FIELDS}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- 原始归档

def archive_payload(source_id: str, label: str, payload: dict | list | bytes) -> Path:
    """把一次抓取的原始响应归档为 gzip 文件，返回相对 RAW_DIR 的路径。"""
    ensure_dirs()
    day = now_iso()[:10]
    out_dir = RAW_DIR / source_id / day
    out_dir.mkdir(parents=True, exist_ok=True)

    if isinstance(payload, bytes):
        blob = payload
    else:
        blob = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    safe_label = "".join(c if c.isalnum() or c in "-_." else "_" for c in label)[:120]
    path = out_dir / f"{safe_label}.json.gz"
    with gzip.open(path, "wb") as fh:
        fh.write(blob)
    return path


def record_snapshot(
    conn,
    *,
    doc_uid: str | None,
    source_id: str,
    http_url: str,
    kind: str,
    rel_path: str | None,
    payload_hash: str | None,
    size_bytes: int | None,
    fetched_at: str | None = None,
) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO raw_snapshot"
        "(doc_uid, source_id, fetched_at, http_url, kind, rel_path, content_hash, size_bytes)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (doc_uid, source_id, fetched_at or now_iso(), http_url, kind,
         rel_path, payload_hash, size_bytes),
    )


# ---------------------------------------------------------------- 政策 upsert

def upsert_policy(conn, row: dict) -> str:
    """写入或更新一条政策，返回 ``new`` / ``updated`` / ``unchanged``。

    设计：先按 doc_uid 查已有记录的 content_hash；一致则只刷新
    last_seen_at / fetch_count（说明只是又被抓到一次），不一致才视为更新。
    """
    h = content_hash(row)
    row = {**row, "content_hash": h}
    # 兜底：调用方可能只关心业务字段，时间戳由存储层补齐，
    # 而不是把 NOT NULL 约束的负担推给每个调用点。
    if not row.get("first_seen_at"):
        row["first_seen_at"] = now_iso()
    if not row.get("last_seen_at"):
        row["last_seen_at"] = now_iso()
    existing = conn.execute(
        "SELECT id, content_hash FROM policy WHERE doc_uid = ?", (row["doc_uid"],)
    ).fetchone()

    if existing is None:
        cols = list(row.keys())
        placeholders = ",".join("?" for _ in cols)
        conn.execute(
            f"INSERT INTO policy({','.join(cols)}) VALUES({placeholders})",
            [row[c] for c in cols],
        )
        status = "new"
    elif existing["content_hash"] != h:
        updatable = [c for c in row if c not in ("doc_uid", "first_seen_at")]
        # **不用空值覆盖已有的值**。
        # 列表接口对很多文件不返回 content / o_aging / 完整文号，下次增量抓取时
        # 这些字段是 None；若照常 UPDATE，详情页辛苦补出的内容会被抹回空值，
        # 而且因为 only_missing 条件（content IS NULL OR p_detail_fetched_at IS NULL）
        # 已不再成立，该条**永远不会被重抓** —— 静默退回较差状态且看起来一切正常。
        # 这一问题由独立验证代理发现（实测：enrich 的成果会被下一次列表抓取覆盖）。
        drop_null = [c for c in updatable if row.get(c) is None]
        if drop_null:
            updatable = [c for c in updatable if c not in drop_null]
        if not updatable:
            conn.execute(
                "UPDATE policy SET last_seen_at=?, fetch_count=fetch_count+1, content_hash=?"
                " WHERE doc_uid=?", (now_iso(), h, row["doc_uid"]))
            return "unchanged"
        assignments = ",".join(f"{c}=?" for c in updatable)
        conn.execute(
            f"UPDATE policy SET {assignments}, last_changed_at=? WHERE doc_uid=?",
            [row[c] for c in updatable] + [now_iso(), row["doc_uid"]],
        )
        status = "updated"
    else:
        conn.execute(
            "UPDATE policy SET last_seen_at=?, fetch_count=fetch_count+1 WHERE doc_uid=?",
            (now_iso(), row["doc_uid"]),
        )
        status = "unchanged"

    return status


def archive_page(
    conn,
    *,
    source_id: str,
    http_url: str,
    column: str,
    page_num: int,
    window_start: str,
    window_end: str,
    payload: dict,
) -> str:
    """归档一页原始响应，返回相对 RAW_DIR 的路径。

    **按页归档而非按条**：全量 5 万条会变成 5 万个碎文件，按页则是几百个文件，
    既避免文件系统压力，又完整保留"当时这一页返回了什么"的证据。
    """
    label = f"page_{column}_{window_start[:10]}_{window_end[:10]}_{page_num:05d}"
    blob = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    path = archive_payload(source_id, label, payload)
    digest = hashlib.sha256(blob).hexdigest()[:16]
    rel = str(path.relative_to(RAW_DIR))
    record_snapshot(
        conn, doc_uid=None, source_id=source_id, http_url=http_url,
        kind="api_json_page", rel_path=rel, payload_hash=digest, size_bytes=len(blob),
    )
    return rel


# ---------------------------------------------------------------- 抓取日志

def log_fetch_start(conn, source_id: str, mode: str,
                    window_start: str | None = None, window_end: str | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO fetch_log(source_id, mode, started_at, window_start, window_end, status)"
        " VALUES(?,?,?,?,?,'running')",
        (source_id, mode, now_iso(), window_start, window_end),
    )
    conn.commit()
    return int(cur.lastrowid)


def log_fetch_finish(
    conn,
    log_id: int,
    *,
    reported_total: int | None,
    fetched_count: int,
    new_count: int = 0,
    updated_count: int = 0,
    status: str | None = None,
    error: str | None = None,
) -> str:
    """收尾抓取日志。状态自动判定：抓到条数 < 声称条数 → incomplete。"""
    if status is None:
        if error:
            status = "failed"
        elif reported_total is not None and fetched_count < reported_total:
            status = "incomplete"
        else:
            status = "ok"
    conn.execute(
        "UPDATE fetch_log SET finished_at=?, reported_total=?, fetched_count=?,"
        " new_count=?, updated_count=?, status=?, error=? WHERE id=?",
        (now_iso(), reported_total, fetched_count, new_count, updated_count, status, error, log_id),
    )
    conn.commit()
    if status != "ok":
        log.warning("抓取未正常完成 source=%s status=%s fetched=%s/%s err=%s",
                    log_id, status, fetched_count, reported_total, error)
    return status


def recent_fetch_summary(conn, limit: int = 20) -> list[dict]:
    rows = conn.execute(
        "SELECT source_id, mode, started_at, window_start, window_end,"
        " reported_total, fetched_count, new_count, updated_count, status, error"
        " FROM fetch_log ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- 详情页结果合并

def _rehash_policy(conn, doc_uid: str) -> None:
    """重算单条政策的 content_hash（详情页补充内容后必须重算）。"""
    cols = ",".join(HASH_FIELDS)
    r = conn.execute(f"SELECT id,{cols} FROM policy WHERE doc_uid=?", (doc_uid,)).fetchone()
    if r is None:
        return
    conn.execute("UPDATE policy SET content_hash=? WHERE id=?",
                 (content_hash(dict(r)), r["id"]))


def apply_enrichment(conn, doc_uid: str, detail) -> str:
    """把详情页提取结果合并进 policy 行，返回 ``updated`` / ``unchanged`` / ``missing``。

    **合并原则：不让质量更差的数据覆盖更好的数据。**

    - ``content``：只在原本为空、或新正文明显更长时替换（避免用截断版覆盖全文）
    - ``p_doc_no_full``：详情页的文号是完整文号（``国家税务总局公告2026年第19号``），
      优先于从标题拼装的结果，置信度直接标 high
    - ``o_aging``：详情页的官方时效优先（列表接口该字段填充率仅 2%）
    - ``cwrq``：已有时不覆盖（列表接口填充率 100%，更可靠）
    """
    existing = conn.execute(
        "SELECT id, content, p_doc_no_full, o_aging, cwrq, title"
        " FROM policy WHERE doc_uid=?",
        (doc_uid,),
    ).fetchone()
    if existing is None:
        return "missing"

    updates: dict[str, object] = {}
    if detail.body:
        old = existing["content"] or ""
        if len(detail.body) > len(old):
            updates["content"] = detail.body
    if detail.doc_no:
        updates["p_doc_no_full"] = detail.doc_no
        updates["p_doc_no_confidence"] = "high"
    if detail.aging_official:
        updates["o_aging"] = detail.aging_official
    if detail.cwrq and not existing["cwrq"]:
        updates["cwrq"] = detail.cwrq
    if detail.effective_date:
        updates["p_effective_date"] = detail.effective_date

    # 标题修复：列表页标题被站点截断成 "…的发..."，而详情页 <title> 是完整的
    # （实测河北 817 / 新疆 257 / 陕西 234 / 辽宁 16 条都栽在这，用户得点原
    # 链接才看得到全称）。**只修以省略号结尾的**，并用列表标题做前缀定位 ——
    # 详情标题前面挂着站点名与栏目名（"国家税务总局浙江省税务局 政策解读
    # 关于《…》的公告的解读"），整条存进去会把站点名带进标题。
    if detail.page_title:
        old_title = existing["title"] or ""
        if old_title.endswith(("...", "..", "…")):
            # 去掉尾省略号，并**剥掉列表页的前导项目符号** —— 陕西的列表标题
            # 形如 "• 关于《…》的解读"，而详情页标题没有那个 "•"，
            # 不剥就一条都定位不上（实测 233 条全卡在这一个字符上）。
            core = (old_title.rstrip(".．。… ")
                    .lstrip("•·-—–*　 ").strip())
            idx = detail.page_title.find(core) if core else -1
            if idx >= 0:
                fixed = detail.page_title[idx:].strip()
                if len(fixed) > len(old_title):
                    updates["title"] = fixed
        elif _DATE_ONLY_TITLE_RE.match(old_title.strip()):
            # 另一种病灶（广西 gx_zcwj 实测 4 条）：列表页压根没给标题，
            # 只给了日期 —— "2026-09-28"。上面那套「用列表标题做前缀定位」
            # 在此**必然失效**（日期不可能出现在详情页标题里），于是详情页
            # 明明有真标题（<title> 里就是）也用不上。
            #
            # 这里改成分隔符切法：详情页 <title> 形如
            #   "国家税务总局关于发布《…》的公告_国家税务总局广西壮族自治区税务局"
            # 即「真标题 + 站点名」。站点后缀分隔符各省不一，取常见的几个；
            # 中文标题本身极少含这些符号，所以切错的风险很低，且下面还有
            # 「必须比原标题长」这道闸。
            cand = detail.page_title
            for sep in ("_", "|", " - ", "－", "—"):
                if sep in cand:
                    cand = cand.split(sep)[0].strip()
                    break
            else:
                # 没命中分隔符。两种可能，**必须分开**：
                #   · page_title 本身**就是干净标题** —— detail.py 是三级取法，
                #     meta ArticleTitle 与 <h1> 都不含站点名，广西这 4 条实测
                #     取到的正是纯标题（第一次写这个分支时我漏了这种情况，
                #     结果把真标题当"可疑"扔掉了）；
                #   · 或它是"站点名 + 栏目名"（河北实测：
                #     "国家税务总局河北省税务局 最新文件"），毫无标题信息。
                # 判据用**结尾体裁词**：政策标题几乎都以这些词收尾，而站点名
                # 与栏目名不会 —— 两种情况都能正确区分。
                cand = cand if cand.endswith(_TITLE_TAIL_WORDS) else ""
            if len(cand) > len(old_title) and len(cand) > 4:
                updates["title"] = cand

    updates["p_detail_fetched_at"] = now_iso()
    assignments = ",".join(f"{k}=?" for k in updates)
    conn.execute(f"UPDATE policy SET {assignments} WHERE doc_uid=?",
                 list(updates.values()) + [doc_uid])
    _rehash_policy(conn, doc_uid)
    changed = [k for k in updates if k != "p_detail_fetched_at"]
    return "updated" if changed else "unchanged"


def upsert_attachment(conn, doc_uid: str, att: dict) -> None:
    """登记附件（暂不下载文件本体）。"""
    conn.execute(
        "INSERT OR IGNORE INTO attachment(doc_uid, url, filename, ext, created_at)"
        " VALUES(?,?,?,?,?)",
        (doc_uid, att.get("url"), att.get("filename"), att.get("ext"), now_iso()),
    )
