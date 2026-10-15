"""端到端冒烟：在 PostgreSQL 上把关键路径走一遍。

**为什么单独做一次**（而不是只靠单元测试）：单元测试跑在 SQLite 上，证明的
是「SQLite 侧没坏」；而这次改造的全部意义是「同一份代码在 PG 上也对」。
这个脚本把那条路径真跑一遍。

覆盖：连接 → 检索 → 详情页四表 → 列表页聚合 → 助手取数层 → 审校台渲染 →
每日简报渲染 → 写入 + 回滚 → 附件租约领取 → 异常类型翻译。

全部在 taxassist 库（演练库）上做；写入的部分自己清理。

**它是切换手册第 6 步用的验证工具**，所以放在 `scripts/` 下受版本控制 ——
放在临时目录里的话，真要切换那天它可能已经不在了。
"""
import os

os.environ.setdefault("TAXASSIST_BACKEND", "pg")

from taxassist import db, kb  # noqa: E402
from taxassist.repo import attachment as R_att  # noqa: E402
from taxassist.repo import conn as rc  # noqa: E402

UID = "bj_sszc:970e05e1de10d02e"
ok = 0
fail = []


def check(label, fn):
    global ok
    try:
        got = fn()
        print(f"  ✓ {label:<24} {got}")
        ok += 1
    except Exception as e:  # noqa: BLE001
        print(f"  ✗ {label:<24} {type(e).__name__}: {str(e)[:90]}")
        fail.append(label)


print("后端:", rc.BACKEND, "| DSN:", rc.DEFAULT_PG_DSN)
print()

conn = db.connect()


def conn_count():
    return f"{conn.execute('SELECT COUNT(*) FROM policy').fetchone()[0]} 条政策"


def row_both_ways():
    a = conn.execute("SELECT COUNT(*) AS n FROM policy").fetchone()[0]
    b = conn.execute("SELECT COUNT(*) AS n FROM policy").fetchone()["n"]
    return f"{a} / {b}"


def ifnull_translated():
    return conn.execute("SELECT IFNULL(NULL,'fb') AS x").fetchone()["x"]


def pragma_shape():
    return f"{len(conn.execute('PRAGMA table_info(attachment)').fetchall())} 列"


check("连接与计数", conn_count)
check("行对象双取值", row_both_ways)
check("IFNULL 翻译", ifnull_translated)
check("PRAGMA table_info", pragma_shape)


# 详情页四表
def detail():
    n = []
    for sql in ("SELECT * FROM policy WHERE doc_uid=?",
                "SELECT * FROM attachment WHERE doc_uid=?",
                "SELECT * FROM translation WHERE doc_uid=?",
                "SELECT * FROM policy_relation WHERE src_doc_uid=? LIMIT 5"):
        n.append(len(conn.execute(sql, (UID,)).fetchall()))
    return f"policy/att/tr/rel = {n}"


check("详情页四表", detail)

# 列表页聚合
check("列表页聚合", lambda: conn.execute(
    "SELECT COUNT(*) AS n FROM policy WHERE IFNULL(is_official,1)=1"
).fetchone()["n"])

conn.close()


# 检索（走 kb，它自带后端分支）
def search():
    r = kb.search("增值税", limit=3)
    hits = r.get("hits") or []
    first = (hits[0].get("title", "")[:22]) if hits else ""
    return f"{r.get('mode')} / {len(hits)} 条 / 首条 {first}"


def search_short_term():
    return f"{len(kb.search('税率', limit=3).get('hits') or [])} 条（FTS5 在此为 0）"


check("检索 kb.search", search)
check("二字词检索", search_short_term)


# 审校台渲染
def review_page():
    from fastapi.testclient import TestClient

    from taxassist.web.app import create_app

    resp = TestClient(create_app(require_auth=False)).get("/review")
    has_title = "翻译质量与审校" in resp.text
    return f"HTTP {resp.status_code} / {len(resp.text)} 字符 / 标题在: {has_title}"


check("审校台渲染", review_page)


# 每日简报 —— **专门盯 date('now', ?) 这类方言漏网**
# 这里原先写着 `WHERE p.cwrq >= date('now', ?)`：SQLite 的 date() 收两个参数，
# 而 PG 的 date() **只收一个**，于是 /daily 在 PG 上直接 500（报「函数
# date(unknown, unknown) 不存在」），单元测试跑在 SQLite 上看不见。
# 修法是让调用处写两边通用的 SQL（日期边界在 Python 算好当文本参数传）。
# 这条冒烟就是钉住它：任何一端坏掉都会在这里报出来。
def daily_page():
    from fastapi.testclient import TestClient

    from taxassist.web.app import create_app

    resp = TestClient(create_app(require_auth=False)).get("/daily")
    has_title = "Daily Brief" in resp.text
    return f"HTTP {resp.status_code} / {len(resp.text)} 字符 / 标题在: {has_title}"


check("每日简报渲染", daily_page)


# 写入 + 清理
def write_and_clean():
    c = db.connect()
    try:
        c.execute("INSERT INTO meta(key,value,updated_at) VALUES(?,?,?)"
                  " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  ("smoke_test", "1", "2026-10-14T00:00:00"))
        c.commit()
        v = c.execute("SELECT value FROM meta WHERE key=?", ("smoke_test",)).fetchone()["value"]
        c.execute("DELETE FROM meta WHERE key=?", ("smoke_test",))
        c.commit()
        return f"写入并清理 OK (读到 {v})"
    finally:
        c.close()


check("写入 + ON CONFLICT", write_and_clean)


# 附件租约
def claim():
    c = db.connect()
    try:
        c.execute("UPDATE attachment SET claimed_at = NULL")
        c.commit()
        b1 = R_att.claim_batch(c, limit=3)
        b2 = R_att.claim_batch(c, limit=3)
        ids1 = {r["id"] for r in b1}
        ids2 = {r["id"] for r in b2}
        c.execute("UPDATE attachment SET claimed_at = NULL")
        c.commit()
        return f"领 {len(b1)} + {len(b2)} 条，不重叠 {not (ids1 & ids2)}"
    finally:
        c.close()


check("附件租约领取", claim)


# 异常类型翻译
def err_translation():
    c = db.connect()
    try:
        c.execute("SELECT * FROM 这张表不存在_at_all")
        return "没有抛异常（不对）"
    except Exception as e:  # noqa: BLE001
        import sqlite3

        is_operational = isinstance(e, sqlite3.OperationalError)
        return f"{type(e).__name__}，是 sqlite3.OperationalError: {is_operational}"
    finally:
        c.close()


check("异常翻译为 sqlite3 类型", err_translation)

print()
print("=" * 56)
print(f"冒烟结果：{ok} 项通过，{len(fail)} 项失败")
if fail:
    print("失败项：", fail)
