"""访问认证：独立账号 + 口令验证。

======================================================================
为什么这个模块是必需的
======================================================================

本应用默认只监听 127.0.0.1（仅本机可连），因此此前没有任何认证。
一旦对外暴露，**谁访问到它就能看到全部内容** —— 包括内部推定结论、
待确认队列与附件全文。

**认证是唯一的那道门锁。** 没有它，其他安全措施都无意义。

======================================================================
安全取舍（如实记录）
======================================================================

- 口令用标准库 ``hashlib.scrypt`` 哈希，**不存明文**；参数取到约 16ms/次 ——
  慢到足以阻止暴力破解，又不影响正常登录。
- 用 HTTP Basic Auth：无状态、浏览器原生支持、无需管理 session。
  代价是**无法主动登出**，且每次请求都携带凭据 ——
  因此**公网部署必须启用 HTTPS**，否则口令在城市级的网络路径上是明文。
- 失败尝试限速：同一用户名连续失败到阈值会短暂拒绝，挡住在线暴力破解。

======================================================================
这不是"做完就安全"
======================================================================

本模块降低的是"无认证"这一类风险。它挡不住：应用自身漏洞、依赖漏洞、
机器上其他服务的漏洞、以及拿到机器的人。对外暴露的决定同时意味着
**持续维护责任**（看日志、更新依赖、及时改口令）。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import re
import secrets
import sqlite3
import time
from datetime import datetime, timedelta

log = logging.getLogger(__name__)

_SCRYPT_N = 2 ** 14
MIN_PASSWORD_LEN = 10
ROLE_OWNER = "owner"
ROLE_MEMBER = "member"
# 账号名限定在 ASCII：口令与账号要能经微信/电话转述、能在日志里无误呈现，
# 且 Basic 兼容模式下冒号会被当作分隔符，中文名容易引发无法解释的登录失败。
_USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,31}$")
_USER_TABLE = """
CREATE TABLE IF NOT EXISTS app_user (
    username      TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    last_login_at TEXT,
    login_count   INTEGER NOT NULL DEFAULT 0,
    role          TEXT NOT NULL DEFAULT 'member'
);
"""


def ensure_user_table(conn) -> None:
    conn.executescript(_USER_TABLE)
    # 老库补 role 列（SQLite 没有 ADD COLUMN IF NOT EXISTS，只能先查 PRAGMA）
    columns = {row[1] for row in conn.execute("PRAGMA table_info(app_user)")}
    if "role" not in columns:
        conn.execute("ALTER TABLE app_user ADD COLUMN role TEXT NOT NULL DEFAULT 'member'")
    conn.commit()


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt,
                        n=_SCRYPT_N, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, salt_hex, dk_hex = stored.split("$")
        if algo != "scrypt":
            return False
        dk = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt_hex),
                            n=_SCRYPT_N, r=8, p=1, dklen=32)
        # 常量时间比较：避免通过响应时间猜出口令
        return hmac.compare_digest(dk.hex(), dk_hex)
    except (ValueError, AttributeError, TypeError):
        return False


def validate_username(username: str) -> str:
    """规范化并校验账号名；非法时抛 ValueError（消息直接给使用者看）。"""
    name = (username or "").strip()
    if not _USERNAME_RE.match(name):
        raise ValueError("账号需为 3-32 位，只能含字母、数字、点、下划线、连字符，"
                         "且以字母或数字开头")
    return name


def validate_password(password: str) -> None:
    if not password or len(password) < MIN_PASSWORD_LEN:
        raise ValueError(f"口令至少 {MIN_PASSWORD_LEN} 位"
                         "（对外暴露的站点，短口令等于没有门锁）")


def _insert_user(conn, username: str, password: str, overwrite: bool,
                 role: str = ROLE_MEMBER) -> str:
    """写库但**不提交** —— 注册时它要与邀请码核销处在同一个事务里。

    返回规范化的账号名。
    """
    from .db import now_iso

    name = validate_username(username)
    validate_password(password)
    ensure_user_table(conn)
    if overwrite:
        conn.execute(
            "INSERT INTO app_user(username, password_hash, created_at, role)"
            " VALUES(?,?,?,?)"
            " ON CONFLICT(username) DO UPDATE SET password_hash=excluded.password_hash,"
            " role=excluded.role",
            (name, hash_password(password), now_iso(), role),
        )
    else:
        # 注册路径**绝不能覆盖已有账号的口令** —— 否则任何一个拿到邀请码的人
        # 只要填别人的账号名，就能把对方的口令改掉，等于越权接管。
        try:
            conn.execute(
                "INSERT INTO app_user(username, password_hash, created_at, role)"
                " VALUES(?,?,?,?)",
                (name, hash_password(password), now_iso(), role),
            )
        except sqlite3.IntegrityError as e:
            raise ValueError(f"账号「{name}」已被占用，请换一个") from e
    return name


def create_user(conn, username: str, password: str, *, overwrite: bool = True,
                role: str = ROLE_MEMBER) -> None:
    """管理员建号（CLI 用）。``overwrite=True`` 时同名账号视为重置口令。"""
    try:
        _insert_user(conn, username, password, overwrite, role)
        conn.commit()
    except Exception:
        conn.rollback()      # 失败别把连接留在半开事务里，后续语句会莫名报错
        raise


def set_role(conn, username: str, role: str) -> int:
    """调整账号角色（owner=超级管理员，可进后台管账号）。返回受影响行数。"""
    ensure_user_table(conn)
    cur = conn.execute("UPDATE app_user SET role=? WHERE username=?",
                       (role, (username or "").strip()))
    conn.commit()
    return cur.rowcount


def is_owner(conn, username: str | None) -> bool:
    if not username:
        return False
    ensure_user_table(conn)
    row = conn.execute("SELECT role FROM app_user WHERE username=?",
                       (username,)).fetchone()
    return bool(row) and row["role"] == ROLE_OWNER


def owner_count(conn) -> int:
    ensure_user_table(conn)
    return conn.execute("SELECT COUNT(*) FROM app_user WHERE role=?",
                        (ROLE_OWNER,)).fetchone()[0]


def delete_user(conn, username: str) -> int:
    """删除账号 = 撤销访问权。返回删除的行数。

    无状态会话令牌靠"账号是否仍存在 + 口令指纹"校验，所以删号之后
    对方浏览器里那个 Cookie 会立刻失效，不需要额外的黑名单。
    """
    ensure_user_table(conn)
    cur = conn.execute("DELETE FROM app_user WHERE username=?", ((username or "").strip(),))
    conn.commit()
    if cur.rowcount:
        log.info("已删除账号：%s", username)
    return cur.rowcount


def list_users(conn) -> list[dict]:
    ensure_user_table(conn)
    return [dict(r) for r in conn.execute(
        "SELECT username, role, created_at, last_login_at, login_count FROM app_user"
        " ORDER BY CASE role WHEN 'owner' THEN 0 ELSE 1 END, username")]


def user_count(conn) -> int:
    ensure_user_table(conn)
    return conn.execute("SELECT COUNT(*) FROM app_user").fetchone()[0]


# ---------------------------------------------------------------- 失败限速

_ATTEMPTS: dict[str, list[float]] = {}
MAX_FAILURES = 8
WINDOW_SEC = 300


def _recent(key: str) -> list[float]:
    now = time.monotonic()
    kept = [t for t in _ATTEMPTS.get(key, []) if now - t < WINDOW_SEC]
    if not kept:
        _ATTEMPTS.pop(key, None)          # 空键及时清掉，避免字典无限增长
    else:
        _ATTEMPTS[key] = kept
    if len(_ATTEMPTS) > 4096:             # 兜底：被大量不同 key 冲刷时整体老化
        _ATTEMPTS.clear()
    return kept


def is_rate_limited(username: str) -> bool:
    return len(_recent(username)) >= MAX_FAILURES


def record_failure(username: str) -> None:
    _recent(username).append(time.monotonic())


def record_success(conn, username: str) -> None:
    _ATTEMPTS.pop(username, None)
    from .db import now_iso

    conn.execute(
        "UPDATE app_user SET last_login_at=?, login_count=login_count+1 WHERE username=?",
        (now_iso(), username),
    )
    conn.commit()


def check_credentials(conn, username: str, password: str) -> bool:
    """验证账号口令。**任何异常都返回 False**，不向外部泄露失败原因。"""
    ensure_user_table(conn)
    if not username or not password or is_rate_limited(username):
        return False
    row = conn.execute(
        "SELECT password_hash FROM app_user WHERE username=?", (username,)
    ).fetchone()
    ok = bool(row) and verify_password(password, row["password_hash"])
    if ok:
        record_success(conn, username)
    else:
        record_failure(username)
        log.warning("登录失败：%s（近 %d 秒内第 %d 次）",
                    username, WINDOW_SEC, len(_recent(username)))
    return ok


# ================================================================ 邀请码注册
#
# 为什么不做"开放注册"：本库含全部政策正文、内部推定结论与待确认队列，
# 开放注册等于把门锁拆掉 —— 任何知道网址的人都能看到全部内容。
# 邀请码是"客户自己建账号"与"只有被邀请的人能进来"之间唯一可行的折中：
# **账号与口令由客户自己设定（我们从不接触其口令），准入由你发的邀请码控制。**

_INVITE_TABLE = """
CREATE TABLE IF NOT EXISTS invite_code (
    code        TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL,
    expires_at  TEXT,
    used_by     TEXT,
    used_at     TEXT,
    note        TEXT,
    revoked     INTEGER NOT NULL DEFAULT 0,
    grants_role TEXT NOT NULL DEFAULT 'member'
);
"""

# 去掉易混字符（0/O、1/I/L、5/S、2/Z、8/B）：邀请码要靠微信或电话转述、要能手抄。
_INVITE_ALPHABET = "ACDEFGHJKMNPQRTUVWXY34679"
INVITE_LEN = 12


def ensure_invite_table(conn) -> None:
    conn.executescript(_INVITE_TABLE)
    # 老库补列
    columns = {row[1] for row in conn.execute("PRAGMA table_info(invite_code)")}
    if "grants_role" not in columns:
        conn.execute("ALTER TABLE invite_code"
                     " ADD COLUMN grants_role TEXT NOT NULL DEFAULT 'member'")
    conn.commit()


def normalize_invite(text: str) -> str:
    """统一邀请码书写形式：忽略大小写、空格与连字符（客户抄写时的常见差异）。"""
    return re.sub(r"[^A-Z0-9]", "", (text or "").upper())


def format_invite(code: str) -> str:
    """XXXX-XXXX-XXXX 形式，便于口头转述。"""
    return "-".join(code[i:i + 4] for i in range(0, len(code), 4))


def create_invite(conn, note: str = "", ttl_days: int | None = 30,
                  grants_role: str = ROLE_MEMBER) -> dict:
    """生成一个一次性邀请码。``ttl_days=None`` 表示不过期。

    ``grants_role=ROLE_OWNER`` 时，用这个码注册出来的账号是超级管理员 ——
    这样**可以由使用者自己设定口令**，发码的人自始至终不知道对方的口令。
    """
    from .db import now_iso

    if grants_role not in (ROLE_OWNER, ROLE_MEMBER):
        grants_role = ROLE_MEMBER
    ensure_invite_table(conn)
    expiry = None
    if ttl_days:
        expiry = (datetime.now().astimezone() + timedelta(days=ttl_days)
                  ).replace(microsecond=0).isoformat()
    for _ in range(20):
        code = "".join(secrets.choice(_INVITE_ALPHABET) for _ in range(INVITE_LEN))
        try:
            conn.execute(
                "INSERT INTO invite_code(code, created_at, expires_at, note, grants_role)"
                " VALUES(?,?,?,?,?)",
                (code, now_iso(), expiry, (note or "").strip() or None, grants_role),
            )
            conn.commit()
        except sqlite3.IntegrityError:      # 撞码（27^12 分之一），重摇
            continue
        return {"code": format_invite(code), "raw": code, "expires_at": expiry,
                "note": note, "grants_role": grants_role}
    raise RuntimeError("邀请码生成失败，请重试")


def invite_state(row) -> str:
    """给使用者看的状态：可用 / 已使用 / 已过期 / 已吊销。"""
    from .db import now_iso

    if row["revoked"]:
        return "已吊销"
    if row["used_by"]:
        return "已使用"
    if row["expires_at"] and row["expires_at"] < now_iso():
        return "已过期"
    return "可用"


def list_invites(conn, only_available: bool = False) -> list[dict]:
    ensure_invite_table(conn)
    sql = ("SELECT code, created_at, expires_at, used_by, used_at, note, revoked,"
           " grants_role FROM invite_code")
    if only_available:
        sql += " WHERE used_by IS NULL AND revoked = 0"
    rows = [dict(r) for r in conn.execute(sql + " ORDER BY created_at DESC")]
    for r in rows:
        r["display"] = format_invite(r["code"])
        r["state"] = invite_state(r)
    return rows


def revoke_invite(conn, code: str) -> bool:
    """吊销未使用的邀请码。返回是否**真的**改变了状态。

    已使用的不动（吊销准入 ≠ 撤销已开通的账号，那是 delete_user 的事）；
    已经吊销过的也不重复计数，好让调用方据此给出准确提示。
    """
    ensure_invite_table(conn)
    cur = conn.execute(
        "UPDATE invite_code SET revoked=1 WHERE code=? AND used_by IS NULL AND revoked=0",
        (normalize_invite(code),))
    conn.commit()
    return cur.rowcount > 0


def redeem_invite(conn, code: str, username: str, password: str) -> str:
    """凭邀请码注册。成功返回账号名；任何不合格都抛 ValueError（原因给人看）。"""
    from .db import now_iso

    ensure_invite_table(conn)
    key = normalize_invite(code)
    if not key:
        raise ValueError("请填写邀请码")
    row = conn.execute("SELECT * FROM invite_code WHERE code=?", (key,)).fetchone()
    if row is None:
        raise ValueError("邀请码无效，请核对是否抄错，或向发放人确认")
    if row["revoked"]:
        raise ValueError("该邀请码已被吊销，请联系发放人重新获取")
    if row["used_by"]:
        raise ValueError("该邀请码已被使用（一个邀请码只能注册一个账号）")
    if row["expires_at"] and row["expires_at"] < now_iso():
        raise ValueError(f"该邀请码已于 {row['expires_at'][:10]} 过期，请联系发放人重新获取")
    try:
        name = _insert_user(conn, username, password, overwrite=False,
                            role=row["grants_role"] or ROLE_MEMBER)
        conn.execute("UPDATE invite_code SET used_by=?, used_at=? WHERE code=?",
                     (name, now_iso(), key))
        conn.commit()
    except Exception:
        conn.rollback()      # 账号没建成，邀请码也必须保持可用
        raise
    log.info("邀请码注册成功：%s（码 %s****，角色 %s）",
             name, key[:4], row["grants_role"])
    return name


# ================================================================ 登录态（签名 Cookie）
#
# 为什么对外默认不用 HTTP Basic：它有四个硬伤 —— ①浏览器原生弹框放不了
# "注册"入口，客户无从自助建号；②无法登出（换了使用者仍是前一个人的身份）；
# ③凭据每次请求都随网络发送；④输错只有反复弹框，给不出中文原因。
# "客户自己建立账号"这条需求本身就要求一个真正的登录页，故用签名 Cookie 会话。
# Basic 作为兼容模式保留（serve --auth basic）。

SESSION_COOKIE = "taxassist_session"
SESSION_TTL_SEC = 12 * 3600
_SECRET_KEY = "session_secret"


def session_secret(conn) -> str:
    """签名密钥。存库而非内存：重启服务不该把已登录的浏览器全部踢下线。"""
    from .db import get_meta, set_meta

    secret = get_meta(conn, _SECRET_KEY)
    if not secret:
        secret = secrets.token_urlsafe(32)
        set_meta(conn, _SECRET_KEY, secret)
        conn.commit()
    return secret


def _fingerprint(password_hash: str) -> str:
    """口令哈希尾部指纹：**改口令后旧会话立即失效**（无状态令牌做不到登出，
    但至少要做到"改了口令，别处还挂着"这种情况不成立）。"""
    return password_hash[-12:]


def issue_token(conn, username: str, ttl_sec: int = SESSION_TTL_SEC) -> str:
    row = conn.execute("SELECT password_hash FROM app_user WHERE username=?",
                       (username,)).fetchone()
    if row is None:
        raise ValueError(f"账号不存在：{username}")
    exp = int(time.time()) + ttl_sec
    payload = f"{username}|{exp}|{_fingerprint(row['password_hash'])}"
    sig = hmac.new(session_secret(conn).encode(), payload.encode(),
                   hashlib.sha256).hexdigest()[:32]
    return base64.urlsafe_b64encode(f"{payload}|{sig}".encode()).decode().rstrip("=")


def read_token(conn, token: str) -> str | None:
    """校验会话令牌，返回账号名。任何异常一律返回 None，不区分失败原因。"""
    if not token:
        return None
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode("utf-8")
        username, exp_s, fp, sig = raw.rsplit("|", 3)
        if int(exp_s) < time.time():
            return None
    except (ValueError, UnicodeDecodeError, TypeError, AttributeError):
        return None
    payload = f"{username}|{exp_s}|{fp}"
    expected = hmac.new(session_secret(conn).encode(), payload.encode(),
                        hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        row = conn.execute("SELECT password_hash FROM app_user WHERE username=?",
                           (username,)).fetchone()
    except sqlite3.OperationalError:      # 全新库尚未建表：当作未登录
        return None
    if row is None or _fingerprint(row["password_hash"]) != fp:
        return None
    return username
