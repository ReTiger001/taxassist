"""命令行入口。

    python -m taxassist initdb                      初始化数据库
    python -m taxassist collect --days 7            每日增量抓取
    python -m taxassist collect --full --year-from 1984   首次全量导入
    python -m taxassist status                      抓取日志与库统计
    python -m taxassist search 研发费用加计扣除      全文检索
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

from . import db as dbmod
from . import auth, backfill, effect, pipeline, store
from .collect.fgk import COLUMNS

log = logging.getLogger("taxassist")


def _setup_console() -> None:
    """让控制台吃得下警告符号。

    Windows 控制台默认是 GBK 代码页，打印 "⚠" 会直接抛 UnicodeEncodeError ——
    而 ``serve`` 对外暴露时正要打印这些警告，等于**在最需要提醒的时候程序崩掉**
    （实测：--host 0.0.0.0 启动即退出，人只会看到一堆乱码的异常）。
    这里把控制台代码页与输出编码一并切到 UTF-8，重定向到文件时也不会报错。
    """
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        except Exception:  # noqa: BLE001 - 没有控制台（管道/重定向）时忽略
            pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="taxassist", description="税务智能知识助手（本地）")
    p.add_argument("-v", "--verbose", action="store_true", help="显示调试日志")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("initdb", help="初始化/升级数据库")

    c = sub.add_parser("collect", help="抓取政策")
    c.add_argument("--days", type=int, default=7, help="增量回溯天数（默认 7，重叠防漏）")
    c.add_argument("--full", action="store_true", help="首次全量导入（按年切窗口）")
    c.add_argument("--year-from", type=int, default=1984, help="全量起始年份")
    c.add_argument("--column", action="append", help="只抓指定栏目，可重复；默认主要三类")
    c.add_argument("--max-pages", type=int, default=None, help="每个窗口最多抓几页（调试用）")
    c.add_argument("--no-archive", action="store_true", help="不归档原始响应（不建议）")

    s = sub.add_parser("status", help="抓取日志与库统计")
    s.add_argument("--limit", type=int, default=15, help="显示最近多少条抓取记录")

    q = sub.add_parser("search", help="全文检索")
    q.add_argument("keyword", help="检索词（标题/文号/正文/关键词）")
    q.add_argument("--limit", type=int, default=15)

    e = sub.add_parser("enrich", help="抓取详情页：补正文/完整文号/官方时效/施行日/附件")
    e.add_argument("--limit", type=int, default=50, help="本次最多抓多少条")
    e.add_argument("--all", action="store_true", help="重抓全部（默认只抓缺失的）")

    at = sub.add_parser("attach", help="下载并解析附件（PDF/Excel/Word）")
    at.add_argument("--limit", type=int, default=20)
    at.add_argument("--all", action="store_true", help="重抓已处理过的附件")

    sub.add_parser("judge", help="效力判定与引用关系抽取")

    rv = sub.add_parser("review", help="查看待人工确认队列")
    rv.add_argument("--limit", type=int, default=30)

    sv = sub.add_parser("serve", help="启动网页界面（默认仅本机；--host 0.0.0.0 对外并启用认证）")
    sv.add_argument("--port", type=int, default=8765)
    sv.add_argument("--no-scheduler", action="store_true",
                    help="不启动后台定时抓取（只开界面）")
    sv.add_argument("--at", default="07:30", help="每日执行时间，如 07:30")
    sv.add_argument("--host", default="127.0.0.1",
                    help="绑定地址。默认 127.0.0.1（仅本机）；对外提供访问用 0.0.0.0")
    sv.add_argument("--expose", action="store_true",
                    help="本机监听但按对外提供服务对待（开启认证与风险提示）。"
                         "用隧道/反向代理转发时**必须**加上，否则等于没有门锁")
    sv.add_argument("--auth", choices=("page", "basic"), default="page",
                    help="对外的认证方式：page=登录页 + 邀请码注册（默认，可登出）；"
                         "basic=HTTP Basic（脚本友好，但没有注册入口也不能登出）")

    ua = sub.add_parser("useradd", help="本机直接建号（对外时更推荐让使用者用邀请码自己注册）")
    ua.add_argument("username")
    ua.add_argument("--password", help="不传则交互式输入（推荐，避免明文留在命令历史）")
    ua.add_argument("--owner", action="store_true",
                    help="授予超级管理员：可进 /admin 管理账号与邀请码")
    ua.add_argument("--demote", action="store_true",
                    help="配合已有账号：取消其超级管理员身份")

    ud = sub.add_parser("userdel", help="撤销访问权：删除账号（其登录会话立即失效）")
    ud.add_argument("username")

    iv = sub.add_parser("invite", help="生成/查看/吊销注册邀请码（对外时唯一的准入通道）")
    iv.add_argument("--note", default="", help="备注：这份邀请码发给谁")
    iv.add_argument("--days", type=int, default=30, help="有效期天数；0 表示不过期")
    iv.add_argument("--owner", action="store_true",
                    help="管理员邀请码：用它注册出来的账号可以进 /admin 管账号")
    iv.add_argument("--list", action="store_true", help="列出已有邀请码")
    iv.add_argument("--available", action="store_true", help="配合 --list：只看可用的")
    iv.add_argument("--revoke", metavar="CODE", help="吊销指定邀请码")

    ctt = sub.add_parser("cit-template", help="生成企业所得税汇算清缴输入模板")
    ctt.add_argument("--out", required=True, help="模板输出路径 (.xlsx)")

    ct = sub.add_parser("cit", help="从输入模板生成纳税调整底稿（客户数据只在本机处理）")
    ct.add_argument("--input", required=True, help="填好的输入模板 (.xlsx)")
    ct.add_argument("--out", required=True, help="底稿输出路径 (.xlsx)")
    ct.add_argument("--rate", type=float, default=0.25, help="适用税率，默认 0.25")

    sub.add_parser("dedupe", help="清理跨源重复（同一文件被总局与省级站各抓一次）")

    b = sub.add_parser("backfill", help="从已有正文补全施行日期与文号（不联网）")
    rp = sub.add_parser(
        "reparse",
        help="从归档的详情页快照重新解析正文（不联网；改进解析器后跑它）")
    rp.add_argument("--limit", type=int, default=0, help="最多处理多少条（0=全部）")
    b.add_argument("--recheck-docno", action="store_true",
                   help="复核并修正被正文污染的文号（历史上贪婪匹配留下的，只动受污染的）")
    return p


def cmd_initdb(args) -> int:
    conn = dbmod.connect()
    fts = dbmod.init_db(conn)
    total = conn.execute("SELECT COUNT(*) FROM policy").fetchone()[0]
    print(f"数据库已就绪：{dbmod.DB_PATH}")
    print(f"SQLite {dbmod.sqlite_version(conn)}，全文检索分词器 {fts}，现有政策 {total} 条")
    return 0


def cmd_collect(args) -> int:
    conn = dbmod.connect()
    dbmod.init_db(conn)
    columns = tuple(args.column) if args.column else pipeline.DEFAULT_COLUMNS
    print(f"开始抓取：栏目={list(columns)}，模式={'全量' if args.full else '增量'}")

    if args.full:
        results = pipeline.collect_full(
            conn, year_from=args.year_from, columns=columns,
            max_pages=args.max_pages, archive=not args.no_archive,
        )
    else:
        results = pipeline.collect_incremental(
            conn, days=args.days, columns=columns,
            max_pages=args.max_pages, archive=not args.no_archive,
        )

    print(pipeline.summarize(results))

    # 抓完顺手判定一次：否则新条目的效力状态停在 'unknown'，
    # 界面上显示"未判定"，看起来像系统坏了
    stats = effect.judge_effects(conn)
    print(f"效力判定：{stats['judged']} 条（来源分布 {stats['by_source']}）")

    bad = [r for r in results if r["status"] != "ok"]
    return 1 if bad else 0


def cmd_status(args) -> int:
    conn = dbmod.connect()
    dbmod.init_db(conn)

    total = conn.execute("SELECT COUNT(*) FROM policy").fetchone()[0]
    with_docno = conn.execute(
        "SELECT COUNT(*) FROM policy WHERE p_doc_no_full IS NOT NULL").fetchone()[0]
    pending = conn.execute(
        "SELECT COUNT(*) FROM policy WHERE p_review_state = 'needs_review'").fetchone()[0]
    print(f"政策总数 {total}；含完整文号 {with_docno}；待人工确认 {pending}")
    print("效力判定来源（official=官方标注，default=本系统推定，inferred=据其他文件推定）：")
    for r in conn.execute(
        "SELECT COALESCE(p_effect_source,'(未判定)') s, COUNT(*) c FROM policy"
        " GROUP BY s ORDER BY c DESC"
    ):
        print(f"    {r['s']:12s} {r['c']}")
    print("效力状态：")
    for r in conn.execute(
        "SELECT COALESCE(p_effect_status,'(未判定)') s, COUNT(*) c FROM policy"
        " GROUP BY s ORDER BY c DESC"
    ):
        print(f"    {r['s']:12s} {r['c']}")
    print("按栏目：")
    for r in conn.execute(
        "SELECT o_column, COUNT(*) c FROM policy GROUP BY o_column ORDER BY c DESC"
    ):
        print(f"    {r['o_column'] or '(未知)':16s} {r['c']}")

    print(f"\n最近 {args.limit} 次抓取：")
    for r in store.recent_fetch_summary(conn, args.limit):
        flag = "" if r["status"] == "ok" else f"  <== {r['status']}"
        print(f"    {r['started_at'][:19]}  {r['source_id']:12s} {r['mode']:11s} "
              f"{(r['window_start'] or '')[:10]}~{(r['window_end'] or '')[:10]}  "
              f"抓 {r['fetched_count']}/{r['reported_total']}  新增 {r['new_count']}{flag}")
        if r["error"]:
            print(f"        错误：{r['error'][:160]}")
    return 0


def cmd_search(args) -> int:
    conn = dbmod.connect()
    dbmod.init_db(conn)
    # trigram 分词器要求按短语检索，用双引号包裹
    sql = (
        "SELECT p.cwrq, p.title, p.p_doc_no_full, p.p_effect_status, p.o_column, p.url "
        "FROM policy_fts f JOIN policy p ON p.id = f.rowid "
        "WHERE policy_fts MATCH ? ORDER BY p.cwrq DESC LIMIT ?"
    )
    try:
        rows = conn.execute(sql, (f'"{args.keyword}"', args.limit)).fetchall()
    except Exception as e:  # noqa: BLE001
        print(f"检索失败：{e}")
        return 2
    if not rows:
        print("没有命中。")
        return 0
    for r in rows:
        print(f"{(r['cwrq'] or '????-??-??')}  [{(r['p_effect_status'] or '?')}] "
              f"{r['p_doc_no_full'] or ''} {r['title'][:70]}")
        print(f"            {r['url'] or ''}")
    return 0


def cmd_enrich(args) -> int:
    conn = dbmod.connect()
    dbmod.init_db(conn)
    stats = pipeline.enrich_details(conn, limit=args.limit, only_missing=not args.all)
    print(
        f"详情页：尝试 {stats['requested']} 条，成功 {stats['ok']}，失败 {stats['failed']}，"
        f"更新 {stats['updated']}，附件 {stats['attachments']}"
    )
    for err in stats["errors"][:8]:
        print(f"    失败：{err}")
    return 1 if stats["failed"] else 0


def cmd_attach(args) -> int:
    conn = dbmod.connect()
    dbmod.init_db(conn)
    stats = pipeline.fetch_attachments(conn, limit=args.limit, only_pending=not args.all)
    print(
        f"附件：尝试 {stats['requested']}，解析成功 {stats['ok']}，"
        f"无文本层(扫描件) {stats['no_text_layer']}，格式不支持 {stats['unsupported']}，"
        f"失败 {stats['failed']}，共下载 {stats['bytes'] / 1024:.0f} KB"
    )
    for msg in stats["errors"][:8]:
        print(f"    {msg}")
    return 1 if stats["failed"] else 0


def cmd_judge(args) -> int:
    conn = dbmod.connect()
    dbmod.init_db(conn)
    stats = effect.judge_effects(conn)
    print(f"判定 {stats['judged']} 条")
    print(f"  判定来源：{stats['by_source']}   （official=官方标注，default=本系统推定）")
    print(f"  效力状态：{stats['by_status']}")
    print(f"  关系：引用 {stats['citations']}，废止 {stats['repeal_relations']}"
          f"（其中 {stats['repeal_matched']} 条匹配到了库内具体文件）")
    print(f"  悬空引用 {stats['dangling_citations']} 条（引用的文件不在库中，可能漏抓）")
    print(f"  待人工确认：{stats['needs_review']} 条（用 taxassist review 查看）")
    return 0


def cmd_review(args) -> int:
    """待人工确认队列：效力判定依据较弱的条目。"""
    conn = dbmod.connect()
    dbmod.init_db(conn)
    rows = effect.pending_review(conn, args.limit)
    if not rows:
        print("没有待人工确认的条目。")
        return 0
    print(f"待确认 {len(rows)} 条：\n")
    for r in rows:
        print(f"[{r['p_effect_status']}] {r['cwrq'] or '????-??-??'}  "
              f"{r['p_doc_no_full'] or '(文号待补)'}")
        print(f"    {r['title'][:64]}")
        print(f"    依据：{r['p_effect_reason']}  （来源：{r['p_effect_source']}）\n")
    return 0


def cmd_useradd(args) -> int:
    import getpass

    conn = dbmod.connect()
    dbmod.init_db(conn)
    auth.ensure_user_table(conn)
    pwd = args.password or getpass.getpass("请输入口令（至少 10 位）: ")
    role = auth.ROLE_OWNER if args.owner else auth.ROLE_MEMBER
    try:
        auth.create_user(conn, args.username, pwd, role=role)
    except ValueError as e:
        print(f"未创建：{e}")
        conn.close()
        return 1
    print(f"账号 {args.username} 已就绪。")
    for u in auth.list_users(conn):
        mark = "超级管理员" if u["role"] == auth.ROLE_OWNER else "普通"
        print(f"   {u['username']:<20} {mark}  创建 {u['created_at'][:19]}"
              f"  登录 {u['login_count']} 次")
    if auth.owner_count(conn) == 0:
        print()
        print("  ⚠ 还没有超级管理员：/admin 后台没人能进。")
        print("    加一个：python -m taxassist useradd <用户名> --owner")
    conn.close()
    return 0


def cmd_userdel(args) -> int:
    """删除账号，即撤销访问权。

    对外站点必须能撤销：客户合作结束、笔记本丢失、口令外泄，都要有一条
    立即生效的退出通道。已发出的邀请码不受影响 —— 那是独立的准入凭据。
    """
    conn = dbmod.connect()
    dbmod.init_db(conn)
    removed = auth.delete_user(conn, args.username)
    users = auth.list_users(conn)
    conn.close()
    if removed == 0:
        print(f"没有找到账号 {args.username}。")
        return 1
    print(f"账号 {args.username} 已删除，其登录会话立即失效。")
    print(f"  现存账号 {len(users)} 个："
          + ("、".join(u["username"] for u in users) if users else "（无）"))
    if not users:
        print("  ⚠ 现在谁也进不来 —— 用 python -m taxassist invite 生成邀请码再放人进来。")
    return 0


def cmd_invite(args) -> int:
    """注册邀请码：对外暴露时唯一的准入通道。

    取舍：邀请码是**一次性**的，一码一人。这样"谁在什么时候被放进来"在库里
    一查便知，也避免了"一个码传遍所有人"这种最常见的失控方式。
    """
    conn = dbmod.connect()
    dbmod.init_db(conn)
    auth.ensure_invite_table(conn)

    if args.revoke:
        ok = auth.revoke_invite(conn, args.revoke)
        conn.close()
        print(f"已吊销邀请码 {args.revoke}。" if ok
              else f"未吊销：{args.revoke} 不存在、已经被使用过、或已经吊销过。")
        return 0 if ok else 1

    if args.list:
        rows = auth.list_invites(conn, only_available=args.available)
        if not rows:
            print("（还没有邀请码。执行 python -m taxassist invite 生成一份）")
        for r in rows:
            exp = (f"有效期至 {r['expires_at'][:10]}" if r["expires_at"] else "不过期")
            note = f"备注：{r['note']}　" if r["note"] else ""
            role = "管理员码　" if r["grants_role"] == auth.ROLE_OWNER else ""
            used = (f"已由 {r['used_by']} 于 {r['used_at'][:10]} 注册　"
                    if r["used_by"] else "")
            print(f"  {r['display']}  [{r['state']}]  {role}{note}{exp}　{used}".rstrip())
        conn.close()
        return 0

    inv = auth.create_invite(
        conn, note=args.note, ttl_days=args.days or None,
        grants_role=auth.ROLE_OWNER if args.owner else auth.ROLE_MEMBER)
    conn.close()
    print()
    print(f"  邀请码：{inv['code']}")
    print()
    if inv["grants_role"] == auth.ROLE_OWNER:
        print("  ⚠ 这是**管理员邀请码**：用它注册出来的账号可以进 /admin 管账号。")
    print(f"  {'有效期至 ' + inv['expires_at'][:10] if inv['expires_at'] else '不过期'}；"
          "一个邀请码只能注册一个账号。")
    print("  把码发给使用者，让其打开本站的 /register 页面自行注册（口令由对方设定，"
          "本站不保存口令原文）。")
    return 0


def _is_exposed(host: str, expose_flag: bool = False) -> bool:
    """是否按"对外提供访问"对待（决定要不要启用认证）。

    **必须与绑定地址解耦。** 隧道与反向代理（Cloudflare Tunnel、tailscale serve、
    frp、nginx）的标准做法都是让后端只监听 127.0.0.1，再由隧道转发公网流量进来；
    若认证仅凭"绑定地址不是本机"来启用，那按标准做法一配隧道，认证就被关掉了 ——
    等于自己把门锁拆了。所以这里额外认一个显式的 --expose。
    """
    return bool(expose_flag) or host not in ("127.0.0.1", "localhost")


def cmd_serve(args) -> int:
    """启动网页界面。

    默认绑定 127.0.0.1（仅本机）。**对外暴露时**（--host 0.0.0.0，或本机绑定
    加 --expose 走隧道/反向代理）会启用认证，并在启动时打印风险清单 ——
    因为这一步意味着这台工作机变成对外服务。
    """
    import uvicorn

    from .web.app import create_app

    conn = dbmod.connect()
    dbmod.init_db(conn)
    total = conn.execute("SELECT COUNT(*) FROM policy").fetchone()[0]
    auth.ensure_user_table(conn)
    n_users = auth.user_count(conn)
    n_owners = auth.owner_count(conn)
    conn.close()

    exposed = _is_exposed(args.host, args.expose)
    print(f"税务知识助手：http://{args.host}:{args.port}　库内政策 {total} 条")
    if exposed and args.host in ("127.0.0.1", "localhost"):
        print("（只监听本机；外部使用者经由你配置的隧道或反向代理进来）")

    if exposed:
        print()
        print("  ⚠ 正在对外提供访问，请确认三件事：")
        print("     1. 已启用 HTTPS —— 没有 TLS，登录口令在公网路径上是明文传输，")
        print("        等于把口令交给网络路径上的每一个节点")
        print("     2. 已限制来源 —— 用防火墙/安全组/隧道服务限定可访问的来源，")
        print("        不要把端口直接敞开给全网")
        print("     3. 已向公司 IT / 风险部门确认过对外暴露的合规性")
        print("  ⚠ 被攻破的不只是这个网站，而是这台工作机上的全部数据")
        if args.auth == "basic":
            print("  认证方式：HTTP Basic（脚本友好；没有注册入口，也无法登出）")
        else:
            print("  认证方式：登录页 + 邀请码注册")
        if n_users == 0:
            # 没有任何账号时站点是**锁死**的（谁也进不来），而不是敞开的 ——
            # 说成"任何人都能访问"会把风险讲反。
            print("  ⚠ 还没有任何账号 —— 现在谁也进不来（包括你自己）：")
            if args.auth == "page":
                print("    先执行  python -m taxassist invite  生成邀请码，")
                print("    再用它打开本站 /register 注册第一个账号。")
            else:
                print("    先执行：python -m taxassist useradd <用户名>")
        else:
            print(f"  已有 {n_users} 个账号。新使用者自行注册："
                  "python -m taxassist invite --note \"发给谁\"")
        if n_owners == 0:
            # 容易漏掉的一步：有账号不等于有人能管账号
            print("  ⚠ 还没有超级管理员，/admin 后台无人可进：")
            print("    python -m taxassist useradd <用户名> --owner")
    else:
        print("仅监听本机（127.0.0.1），局域网与公网无法访问。")
        print("要让外部使用者访问：加 --host 0.0.0.0，并在前面套一层 HTTPS 隧道。")

    if not args.no_scheduler:
        from .scheduler import start_background_scheduler

        try:
            hour, minute = (int(x) for x in args.at.split(":"))
        except ValueError:
            print(f"时间格式无法识别（{args.at}），改用默认 07:30")
            hour, minute = 7, 30
        start_background_scheduler(hour=hour, minute=minute)
        print(f"后台调度已启动：每日 {hour:02d}:{minute:02d} 自动抓取；"
              f"启动时已检查并补抓漏掉的日期。")
    else:
        print("已跳过后台调度（--no-scheduler）。")

    print("按 Ctrl+C 停止。")
    uvicorn.run(create_app(require_auth=exposed, auth_mode=args.auth),
                host=args.host, port=args.port, log_level="warning",
                # proxy_headers=False：默认 True 时 uvicorn 信任来自 127.0.0.1 的
                # X-Forwarded-For / X-Forwarded-Proto 并据此改写 client.host ——
                # 而隧道（cloudflared / tailscaled）正是从 127.0.0.1 连过来的，
                # 于是请求头变成攻击者可控，会污染「是否本机」判定与限速键。
                # 不构成认证绕过（门锁是启动常量），但没必要留这个口子。
                proxy_headers=False,
                # 访问日志必须留：这是公网服务，被爆破或被登录过，
                # 控制台一关就什么痕迹都没有（审计指出的取证盲区）。
                access_log=True)
    return 0


def cmd_cit_template(args) -> int:
    from .cit.workpaper import write_input_template

    path = write_input_template(args.out)
    print(f"输入模板已生成：{path}")
    print("填好数据后执行：python -m taxassist cit --input <模板> --out <底稿>")
    return 0


def cmd_cit(args) -> int:
    from .cit.workpaper import run_from_template

    conn = dbmod.connect()
    dbmod.init_db(conn)
    try:
        result = run_from_template(args.input, args.out, conn=conn)
    finally:
        conn.close()

    print(f"底稿已生成：{args.out}")
    print(f"  利润总额            {result.inputs.total_profit:>16,.2f}")
    print(f"  纳税调整增加额      {result.total_increase:>16,.2f}")
    print(f"  纳税调整减少额      {result.total_decrease:>16,.2f}")
    print(f"  纳税调整后所得      {result.taxable_income:>16,.2f}")
    print(f"  应纳税额（{result.rate:.0%}）   {result.tax_payable:>16,.2f}")

    stale = [i for i in result.items if i.basis_status.startswith("库中已收录·已废止")]
    unchecked = [i for i in result.items if i.basis_status == "库中未收录"]
    if stale:
        print("\n  ⚠ 以下调整项的依据在本地政策库中已废止/失效，务必重新确认：")
        for i in stale:
            print(f"     · {i.name}：{i.basis_evidence}")
    if unchecked:
        print(f"\n  提示：{len(unchecked)} 个调整项的依据未在本地政策库中检索到"
              "（不等于依据不存在，仅表示本地库未收录）")

    print("\n  ⚠ 本底稿未考虑的事项（必须人工处理，详见底稿「未考虑事项」页）：")
    for w in result.warnings:
        print(f"     · {w}")
    return 0


def cmd_dedupe(args) -> int:
    """清理历史跨源重复：标题完全相同者只保留一条（优先级：非地方转载 > 地方政策）。

    只做"标题完全相同"的合并 —— 这是同一份文件被两个源转载的可靠特征。
    标题相近但不相同的一律不动，宁可留着让人判断，也不擅自删数据。
    """
    conn = dbmod.connect()
    dbmod.init_db(conn)

    titles = conn.execute(
        "SELECT title FROM policy GROUP BY title HAVING COUNT(*) > 1").fetchall()
    removed = 0
    for t in titles:
        rows = conn.execute(
            "SELECT doc_uid, o_column, p_doc_no_full FROM policy WHERE title = ?"
            " ORDER BY CASE WHEN o_column = '地方政策' THEN 1 ELSE 0 END, id",
            (t["title"],),
        ).fetchall()
        for r in rows[1:]:                      # 保留第一条，其余视为转载
            uid = r["doc_uid"]
            conn.execute("DELETE FROM policy_relation WHERE src_doc_uid=? OR dst_doc_uid=?", (uid, uid))
            conn.execute("DELETE FROM attachment WHERE doc_uid=?", (uid,))
            conn.execute("DELETE FROM raw_snapshot WHERE doc_uid=?", (uid,))
            conn.execute("DELETE FROM policy WHERE doc_uid=?", (uid,))
            removed += 1

    # 处理标题被拼接的历史记录：省级列表页的图标提示曾混进标题，
    # 形成「甲公告关于《甲公告》的解读」这类畸形标题。
    # 特征是"本条标题以另一条记录的完整标题开头"——这是拼接产物，
    # 不是真实的文件标题，留着会让人以为真有一份这么长的文件。
    malformed = 0
    for r in conn.execute(
        "SELECT doc_uid, title FROM policy WHERE LENGTH(title) > 60").fetchall():
        head = conn.execute(
            "SELECT doc_uid FROM policy WHERE doc_uid <> ? AND LENGTH(title) >= 8"
            " AND SUBSTR(?, 1, LENGTH(title)) = title LIMIT 1",
            (r["doc_uid"], r["title"]),
        ).fetchone()
        if head is None:
            continue
        uid = r["doc_uid"]
        conn.execute("DELETE FROM policy_relation WHERE src_doc_uid=? OR dst_doc_uid=?", (uid, uid))
        conn.execute("DELETE FROM attachment WHERE doc_uid=?", (uid,))
        conn.execute("DELETE FROM raw_snapshot WHERE doc_uid=?", (uid,))
        conn.execute("DELETE FROM policy WHERE doc_uid=?", (uid,))
        malformed += 1

    conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM policy").fetchone()[0]
    print(f"清理完成：转载副本 {removed} 条、标题拼接记录 {malformed} 条，"
          f"现有政策 {total} 条")
    if removed == 0 and malformed == 0:
        print("（没有发现标题完全相同的重复，或标题被拼接的记录）")
    return 0


def cmd_backfill(args) -> int:
    conn = dbmod.connect()
    dbmod.init_db(conn)
    if args.recheck_docno:
        result = backfill.recheck_doc_no(conn)
        conn.close()
        print(f"文号复核：扫描 {result['scanned']} 条，"
              f"发现受污染 {result['contaminated']} 条，"
              f"修正 {result['doc_no_repaired']} 条")
        for old, new in result["samples"]:
            print(f"    {old}  →  {new}")
        return 0
    stats = backfill.backfill_from_content(conn)
    cover = backfill.coverage(conn)
    conn.close()

    print(f"离线补全：扫描 {stats['scanned']} 条，"
          f"补出施行日期 {stats['effective_date_filled']} 条、"
          f"完整文号 {stats['doc_no_filled']} 条")
    print("字段覆盖：")
    for key, value in cover.items():
        print(f"    {key:16s} {value}")
    return 0


def cmd_reparse(args) -> int:
    """从归档快照重新解析详情页 —— 不联网，解析器改进后跑它即可生效。"""
    conn = dbmod.connect()
    dbmod.init_db(conn)
    result = pipeline.reparse_details_from_snapshots(conn, limit=args.limit)
    conn.close()
    print(f"从快照重新解析：扫描 {result['scanned']} 条，更新 {result['updated']} 条，"
          f"失败 {result['failed']} 条，快照缺失 {result['missing']} 条")
    for err in result["errors"]:
        print(f"    {err}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_console()
    # 日志同时落盘：这是暴露在公网的服务，被爆破或被登录过，
    # 只输出到控制台的话窗口一关就什么痕迹都不剩（审计指出的取证盲区）。
    from logging.handlers import RotatingFileHandler
    from pathlib import Path
    log_dir = Path(__file__).resolve().parents[2] / "data" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        log_dir / "taxassist.log", maxBytes=5_000_000, backupCount=5,
        encoding="utf-8")
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s | %(message)s"))
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
        handlers=[logging.StreamHandler(), file_handler],
    )
    handlers = {
        "initdb": cmd_initdb,
        "collect": cmd_collect,
        "enrich": cmd_enrich,
        "attach": cmd_attach,
        "judge": cmd_judge,
        "review": cmd_review,
        "serve": cmd_serve,
        "useradd": cmd_useradd,
        "userdel": cmd_userdel,
        "invite": cmd_invite,
        "cit": cmd_cit,
        "cit-template": cmd_cit_template,
        "dedupe": cmd_dedupe,
        "backfill": cmd_backfill,
        "reparse": cmd_reparse,
        "status": cmd_status,
        "search": cmd_search,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
