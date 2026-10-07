"""客户计费：用量计量、余额、API Key —— 商业化能力的地基。

**为什么单独一个模块**：计费逻辑会被三处调用 —— Web 的 API 端点（计量 + 扣减）、
后台（加额 / 查看用量）、CLI（建客户 / 出 Key）。集中一处才不会出现"三个地方各
算一遍余额"这种必然对不上的局面。

**口径（2026-10 用户定）**：
  · 按**次数**计费，且**检索与助手分开计价** —— 检索是本机 SQL（毫秒级），
    助手要跑 20 秒 GPU，成本相差百倍；统一单价会让高频用助手的人亏本。
    因此每个客户有**两个余额字段**，互不通用。
  · **收款走人工充值**：后台手动加额，不对接支付渠道。零合规风险，适合起步。
  · 对外访问走 Tailscale 私有网络，不做公网域名。

**三条纪律**：
  1. **只存 Key 的哈希**（与用户口令同规）—— 库被人看到也反推不出 Key。
    明文只在生成时返回一次，之后无处可取。
  2. **请求成功才扣费** —— 失败不扣，避免"失败也计费"的争议。
  3. **扣减与计量在同一个事务里** —— 否则会出现"扣了钱没记录"或反过来。
"""
from __future__ import annotations

import hashlib
import logging
import secrets
import sqlite3

from .db import now_iso

log = logging.getLogger(__name__)

#: 计费种类。**值与余额字段一一对应** —— 加两个新种类就要加两个余额列，
#: 这是有意的：种类混用会让"这个客户还剩多少"没法一眼说清。
KIND_SEARCH = "search"        # 检索类：search_policies / get_policy / lookup / overview
KIND_ASSISTANT = "assistant"  # 助手问答：成本含大模型推理

BALANCE_COLUMN = {KIND_SEARCH: "balance", KIND_ASSISTANT: "assistant_balance"}

KEY_PREFIX = "tk_"


def ensure_tables(conn: sqlite3.Connection) -> None:
    """建表（幂等），并把老库缺的列补上。"""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS customer (
            name              TEXT PRIMARY KEY,
            note              TEXT,
            balance           INTEGER NOT NULL DEFAULT 0,  -- 检索次数余额
            assistant_balance INTEGER NOT NULL DEFAULT 0,  -- 助手次数余额
            active            INTEGER NOT NULL DEFAULT 1,
            -- 无限额度：调用照常记录进 usage_log（看得见用量），但不扣减、
            -- 也不受余额检查拦截。给超管自己的账号用。
            unlimited         INTEGER NOT NULL DEFAULT 0,
            created_at        TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS api_key (
            key_hash     TEXT PRIMARY KEY,          -- sha256(明文)，不存明文
            customer     TEXT NOT NULL,
            label        TEXT,
            created_at   TEXT NOT NULL,
            last_used_at TEXT,
            revoked      INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS usage_log (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            ts       TEXT NOT NULL,
            customer TEXT NOT NULL,
            kind     TEXT NOT NULL,
            ok       INTEGER NOT NULL,
            cost     INTEGER NOT NULL DEFAULT 0,
            detail   TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_usage_customer_ts
            ON usage_log(customer, ts);
        """
    )
    # 老库补 unlimited 列。SQLite 没有 ADD COLUMN IF NOT EXISTS，只能先查
    # PRAGMA —— 与 auth.ensure_user_table 补 role 列是同一个写法。
    cols = {row[1] for row in conn.execute("PRAGMA table_info(customer)")}
    if "unlimited" not in cols:
        conn.execute("ALTER TABLE customer ADD COLUMN"
                     " unlimited INTEGER NOT NULL DEFAULT 0")
    conn.commit()


# ------------------------------------------------------------------ Key

def _hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def create_key(conn: sqlite3.Connection, customer: str, label: str = "") -> str:
    """给客户签一枚 API Key，**返回明文（仅此一次）**。"""
    ensure_tables(conn)
    raw = KEY_PREFIX + secrets.token_hex(16)      # tk_ + 32 hex
    conn.execute(
        "INSERT INTO api_key(key_hash, customer, label, created_at) VALUES(?,?,?,?)",
        (_hash_key(raw), customer, (label or "").strip() or None, now_iso()))
    conn.commit()
    log.info("已为客户 %s 签发 API Key（label=%s）", customer, label or "-")
    return raw


def resolve_key(conn: sqlite3.Connection, raw: str) -> str | None:
    """把明文 Key 换成客户名；无效 / 已吊销 / 客户停用一律返回 None。

    顺带刷新 last_used_at —— 后台要靠它看出"哪个客户的 Key 很久没动静"。
    """
    if not raw or not raw.startswith(KEY_PREFIX):
        return None
    ensure_tables(conn)
    row = conn.execute(
        "SELECT k.customer, c.active FROM api_key k"
        " LEFT JOIN customer c ON c.name = k.customer"
        " WHERE k.key_hash = ? AND k.revoked = 0", (_hash_key(raw),)).fetchone()
    if row is None or not row["active"]:
        return None
    conn.execute("UPDATE api_key SET last_used_at = ? WHERE key_hash = ?",
                 (now_iso(), _hash_key(raw)))
    conn.commit()
    return row["customer"]


def revoke_key(conn: sqlite3.Connection, raw: str) -> bool:
    ensure_tables(conn)
    cur = conn.execute("UPDATE api_key SET revoked = 1 WHERE key_hash = ? AND revoked = 0",
                       (_hash_key(raw),))
    conn.commit()
    return cur.rowcount > 0


def list_keys(conn: sqlite3.Connection, customer: str) -> list[dict]:
    """某客户名下的 Key 列表（供自助页与后台显示）。

    **只给出不可逆的短标识（key_hash 前 12 位）与元数据**，不给任何可用于
    调用的东西 —— 库里本来就只有 sha256，明文在签发那一次之后就无处可寻。
    短标识的作用是让客户能指着某一枚说"吊销它"（那枚丢了/换人了）。
    """
    ensure_tables(conn)
    rows = conn.execute(
        "SELECT key_hash, label, created_at, last_used_at, revoked"
        " FROM api_key WHERE customer = ? ORDER BY created_at DESC",
        (customer,)).fetchall()
    return [{"id": r["key_hash"][:12], "label": r["label"],
             "created_at": r["created_at"], "last_used_at": r["last_used_at"],
             "revoked": bool(r["revoked"])} for r in rows]


def revoke_key_by_id(conn: sqlite3.Connection, customer: str, key_id: str) -> bool:
    """按**短标识**吊销 Key —— 客户自助页用这条路，因为谁也拿不到明文。

    **`customer` 条件不能省**：短标识在页面上是公开可见的，而可见不等于安全。
    少了这个条件，任何登录用户只要猜到（或从自己页面上看到过后试别人的）
    12 位片段，就能吊销别人的 Key。sha256 不可逆，但截断片段更不是密文。
    """
    ensure_tables(conn)
    kid = (key_id or "").strip().lower()
    if len(kid) != 12:
        return False
    cur = conn.execute(
        "UPDATE api_key SET revoked = 1"
        " WHERE customer = ? AND revoked = 0 AND substr(key_hash, 1, 12) = ?",
        (customer, kid))
    conn.commit()
    return cur.rowcount > 0


# ------------------------------------------------------------------ 客户与余额

def create_customer(conn: sqlite3.Connection, name: str,
                    note: str = "", balance: int = 0,
                    assistant_balance: int = 0) -> None:
    ensure_tables(conn)
    conn.execute(
        "INSERT INTO customer(name, note, balance, assistant_balance, created_at)"
        " VALUES(?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET note = excluded.note",
        (name, (note or "").strip() or None, max(0, int(balance)),
         max(0, int(assistant_balance)), now_iso()))
    conn.commit()


def ensure_customer(conn: sqlite3.Connection, name: str) -> bool:
    """确保有一个同名的计费客户，返回是否**新建**。

    注册时用它"注册即开户"：客户自己就能在「我的账户」看余额、申请 Key，
    不必等超管先在后台建一遍。若超管早已建过同名客户（先谈好再开通的场景），
    这里**什么都不改** —— 余额、备注、已发的 Key、用量记录全部原样保留，
    只是把这个登录账号和已有的计费账户接上。

    **为什么不能拿 create_customer 顶替**：那条 SQL 是 upsert，DO UPDATE 会写
    note —— 注册流程里若用它"确保存在"，会把后台手工写的备注抹成 NULL。
    INSERT OR IGNORE 才是"只在不存在时建"的正确表达。
    """
    ensure_tables(conn)
    name = (name or "").strip()
    # 空名直接拒绝。这不是理论问题：免认证模式下 request.state.user 是 None，
    # /account 一被访问就会插进一条 name='' 的客户 —— 它在后台列表里是个
    # 没有名字的幽灵行，而且要有人发现才清得掉（本机验证时就真出现过一条）。
    if not name:
        return False
    # 超管自动无限额度。角色在 app_user 表里 —— 同库一条 SQL 就能问，不必
    # import auth（那会让 billing 依赖 auth，而 web 层两边都用，容易绕成环）。
    # app_user 可能还不存在（全新库、还没建号），所以要容错。
    try:
        is_owner = conn.execute(
            "SELECT 1 FROM app_user WHERE username = ? AND role = 'owner'",
            (name,)).fetchone() is not None
    except sqlite3.OperationalError:
        is_owner = False
    cur = conn.execute(
        "INSERT OR IGNORE INTO customer"
        " (name, balance, assistant_balance, unlimited, created_at)"
        " VALUES(?, 0, 0, ?, ?)",
        (name, 1 if is_owner else 0, now_iso()))
    # 客户早就存在、但后来被封为超管的情形：把标志补上（老库首次跑到这里时有用）。
    if is_owner:
        conn.execute("UPDATE customer SET unlimited = 1 WHERE name = ?", (name,))
    conn.commit()
    return cur.rowcount == 1


def add_balance(conn: sqlite3.Connection, name: str, kind: str, amount: int) -> int:
    """给客户加（或减，传负数）余额，返回加完后的余额。

    **这是后台"加余额"功能的核心**（收款走人工充值，所以它必须能加）。
    允许负数是为了纠正误操作，但结果不会低于 0 —— 余额为负会让"还能用几次"
    没法回答，那种状态下客户无论调什么都失败，却看不出原因。
    """
    col = BALANCE_COLUMN.get(kind)
    if col is None:
        raise ValueError(f"未知的计费种类：{kind!r}")
    ensure_tables(conn)
    conn.execute(
        f"UPDATE customer SET {col} = MAX(0, {col} + ?) WHERE name = ?",
        (int(amount), name))
    if conn.execute("SELECT changes()").fetchone()[0] == 0:
        conn.rollback()
        raise ValueError(f"没有这个客户：{name!r}")
    conn.commit()
    row = conn.execute(f"SELECT {col} FROM customer WHERE name = ?", (name,)).fetchone()
    log.info("客户 %s 的 %s 余额调整为 %d（本次 %+d）", name, kind, row[0], amount)
    return int(row[0])


def get_balance(conn: sqlite3.Connection, name: str) -> dict:
    """取客户的两个余额与状态。"""
    ensure_tables(conn)
    row = conn.execute(
        "SELECT balance, assistant_balance, active, unlimited"
        " FROM customer WHERE name = ?", (name,)).fetchone()
    if row is None:
        return {"exists": False, "search": 0, "assistant": 0,
                "active": False, "unlimited": False}
    return {"exists": True, "search": int(row["balance"]),
            "assistant": int(row["assistant_balance"]),
            "active": bool(row["active"]),
            "unlimited": bool(row["unlimited"])}


def charge(conn: sqlite3.Connection, customer: str, kind: str, *,
           ok: bool, detail: str = "", cost: int = 1) -> bool:
    """记一次用量并在成功时扣费。返回"是否放行"。

    **只在 ok=True 时扣** —— 失败请求不收费，这是对客户最基本的公平，
    也省掉"为什么这次也扣了"的争论。
    **扣减与记录在同一个事务里**：先写 usage_log 再 UPDATE 余额，一起提交 ——
    否则中途出错会出现"扣了钱没记录"或反之，那种账对不上。

    返回 False 表示余额不足（调用方应当拒绝这次请求，且**不记账**）。
    """
    col = BALANCE_COLUMN.get(kind)
    if col is None:
        raise ValueError(f"未知的计费种类：{kind!r}")
    ensure_tables(conn)
    try:
        # 无限额度的客户（超管自己）跳过余额检查与扣减，**但用量照记** ——
        # 否则连自己都不知道跑了多少。所以这里不能直接 return，要继续走
        # 下面的 usage_log 插入。
        row = conn.execute("SELECT unlimited FROM customer WHERE name = ?",
                           (customer,)).fetchone()
        free = bool(row and row[0])
        if ok and not free:
            cur = conn.execute(
                f"UPDATE customer SET {col} = {col} - ?"
                f" WHERE name = ? AND {col} >= ?", (cost, customer, cost))
            if cur.rowcount == 0:
                conn.rollback()
                return False
        # 无限额度的那笔记账面 0 —— 因为**确实没扣**。把"本可扣多少"写在
        # detail 里，账面上就不会出现"记了扣减但余额没动"的对不上。
        mark = "　[无限额度，未扣减]" if (free and ok) else ""
        conn.execute(
            "INSERT INTO usage_log(ts, customer, kind, ok, cost, detail)"
            " VALUES(?,?,?,?,?,?)",
            (now_iso(), customer, kind, 1 if ok else 0,
             cost if (ok and not free) else 0,
             ((detail or "") + mark)[:300]))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return True


def set_unlimited(conn: sqlite3.Connection, name: str, on: bool = True) -> bool:
    """开关某个客户的无限额度。返回是否生效（客户不存在则 False）。

    用于两处：超管建号时自动打开，以及后台手工勾选某个客户为"不计量"。
    """
    ensure_tables(conn)
    cur = conn.execute("UPDATE customer SET unlimited = ? WHERE name = ?",
                       (1 if on else 0, (name or "").strip()))
    conn.commit()
    if cur.rowcount:
        log.info("客户 %s 的无限额度：%s", name, "开" if on else "关")
    return bool(cur.rowcount)


def usage_summary(conn: sqlite3.Connection, customer: str | None = None) -> list[dict]:
    """按客户汇总用量（供后台显示）。**含从未调用过的客户**（用量 0）。"""
    ensure_tables(conn)
    where, params = ("", ())
    if customer:
        where, params = (" WHERE c.name = ?", (customer,))
    rows = conn.execute(
        "SELECT c.name, c.note, c.balance, c.assistant_balance, c.active,"
        " c.unlimited, c.created_at,"
        " (SELECT COUNT(*) FROM usage_log u WHERE u.customer = c.name AND u.ok = 1)"
        "   AS calls_ok,"
        " (SELECT COUNT(*) FROM usage_log u WHERE u.customer = c.name AND u.ok = 0)"
        "   AS calls_failed,"
        " (SELECT MAX(ts) FROM usage_log u WHERE u.customer = c.name) AS last_call,"
        " (SELECT COUNT(*) FROM api_key k WHERE k.customer = c.name AND k.revoked = 0)"
        "   AS active_keys"
        f" FROM customer c{where} ORDER BY c.created_at DESC", params).fetchall()
    return [dict(r) for r in rows]


def recent_calls(conn: sqlite3.Connection, customer: str, limit: int = 30) -> list[dict]:
    """某客户最近若干次调用明细（后台展开看）。"""
    ensure_tables(conn)
    rows = conn.execute(
        "SELECT ts, kind, ok, cost, detail FROM usage_log"
        " WHERE customer = ? ORDER BY id DESC LIMIT ?", (customer, limit)).fetchall()
    return [dict(r) for r in rows]
