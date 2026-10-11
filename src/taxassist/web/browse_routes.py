"""浏览类路由：政策库总览 / 检索 / 政策详情 / 每日简报 —— 从 `web/app.py` 的
`create_app` 里拆出来。

这四条是**只读的浏览路径**，也是使用者停留最久的地方；放在一个模块里便于
对照它们共用的筛选与分页约定（`ctx()` 拼上下文、`filters` 出 SQL 片段）。

依赖用参数注入（与 assistant_routes / about_routes / auth_routes /
admin_routes 同范式）。`ctx` 是 create_app 里那个依赖 require_auth 的闭包 ——
它是**共享**的，所以不搬进来，由 create_app 注入；其余对 app.py 模块级辅助
的依赖在 register() 里取一次，供各路由闭包捕获（app.py 在模块级就执行
create_app()，模块级导入会成环，详见 assistant_routes）。
"""
from __future__ import annotations

import logging
import sqlite3

from fastapi import Query, Request
from fastapi.responses import HTMLResponse

from .. import db as dbmod
from .. import filters, kb
from ..translate import to_chinese_query
from .helpers import _SUBSTANTIVE_FIRST, _one, _rows

log = logging.getLogger(__name__)

def register(app, *, ctx, templates) -> None:
    """把 /library、/search、/policy/{doc_uid}、/daily 挂到 app 上。"""
    # 实质政策优先的 ORDER BY 片段与两个查询辅助来自 web/helpers.py（模块级
    # 导入，见文件头）。**漏一个 ruff 就报 F821** —— 当初把这些从 app.py 搬出
    # 来时，正是靠它逐个补全的（初版漏了 _SUBSTANTIVE_FIRST / kb / sqlite3 /
    # to_chinese_query 四处，也误留了一个用不到的 _highlight）。
    # ------------------------------------------------------------ 首页

    @app.get("/library", response_class=HTMLResponse)
    def index(request: Request):
        """政策库总览。

        **路径是 /library 而不是 /** —— 首页留给税务助手。用户实测后的判断：
        这个站的核心是"把一件事丢进来、拿到合规判断"，而不是"浏览政策列表"；
        把人先领到一堆文件面前，等于让他自己去找答案。政策库降为第二入口。
        """
        from .. import scheduler

        conn = dbmod.connect()
        try:
            health = scheduler.fetch_health(conn)
            gap = scheduler.days_since_last_success(conn)
            regions = filters.region_counts(conn)
            region_groups = filters.region_groups(conn)
        finally:
            conn.close()

        stats = {
            "total": _one("SELECT COUNT(*) c FROM policy_official")["c"],
            "valid": _one("SELECT COUNT(*) c FROM policy_official WHERE p_effect_status='现行有效'")["c"],
            # 现行有效的**依据分布** —— 图例里要写明"多少条有官方标注、多少条
            # 只是未发现废止证据"。客户不该只看到一个笼统的"现行有效 4423"：
            # 这两个数字的可靠性差一个量级，混在一起看就是误导。
            "valid_official": _one(
                "SELECT COUNT(*) c FROM policy_official WHERE p_effect_status='现行有效'"
                " AND p_effect_source='official'")["c"],
            "valid_default": _one(
                "SELECT COUNT(*) c FROM policy_official WHERE p_effect_status='现行有效'"
                " AND p_effect_source='default'")["c"],
            "pending": _one("SELECT COUNT(*) c FROM policy_official WHERE p_effect_status='尚未生效'")["c"],
            "repealed": _one("SELECT COUNT(*) c FROM policy_official WHERE p_effect_status='已废止'")["c"],
            "review": _one("SELECT COUNT(*) c FROM policy_official WHERE p_review_state='needs_review'")["c"],
            # **只算真正解析成功的**。原来这里是 COUNT(*) 全表 —— 而页面上
            # 那个数字的标签写的是「已解析附件」，于是 4275 里混进了
            # BadZipFile 388、download_failed 372、no_text_layer 243 等等，
            # 与实际能检索到的正文数（3244）差了 1031 条，名实不符。
            # 全量测试比对「页面显示 vs 库内真值」时揪出来的。
            "attachments": _one(
                "SELECT COUNT(*) c FROM attachment"
                " WHERE parse_status='ok'")["c"],
            "relations": _one("SELECT COUNT(*) c FROM policy_relation")["c"],
            "last_fetch": _one(
                "SELECT started_at FROM fetch_log ORDER BY id DESC LIMIT 1"),
        }
        recent = _rows(
            "SELECT p.doc_uid, p.cwrq, p.title, p.p_doc_no_full, p.p_doc_no_confidence,"
            " p.o_column, p.p_region, p.p_effect_status, p.p_effect_source, p.url"
            f" FROM policy_official p ORDER BY {_SUBSTANTIVE_FIRST}, p.cwrq DESC LIMIT 25")
        by_column = _rows(
            "SELECT COALESCE(o_column,'(未知)') v, COUNT(*) c FROM policy_official"
            " GROUP BY v ORDER BY c DESC")
        by_effect = _rows(
            "SELECT COALESCE(p_effect_status,'未判定') v, COUNT(*) c FROM policy_official"
            " GROUP BY v ORDER BY c DESC")
        return templates.TemplateResponse(
            request=request, name="index.html",
            context=ctx(request, stats=stats, recent=recent, by_column=by_column,
                        by_effect=by_effect,
                        health=health, gap=gap, regions=regions,
                        region_groups=region_groups))

    # ------------------------------------------------------------ 检索

    @app.get("/search", response_class=HTMLResponse)
    def search(request: Request, q: str = Query("", max_length=120),
               column: str = "", tax: str = "", region: str = "",
               effect: str = "", year: str = "", sort: str = "relevance",
               limit: int = Query(50, ge=1, le=200)):
        rows, error = [], None
        # 命中总数：模板要显示「共 N 条」，N 必须是**命中总数**而非返回条数。
        # 无条件浏览时保持 None，模板据此不显示这一行。
        matched_total = None
        # 英文查询先过术语反向映射：搜 "value-added tax" 等同于搜 "增值税"。
        # 这样不必先把 5090 条标题全译一遍，英文词也能命中中文政策。
        q_cn, term_hits = to_chinese_query(q)
        terms = [t for t in q_cn.split() if t]
        # 允许"不输关键词、只按条件浏览" —— 筛选本身就是真实用法。
        # effect / year 也必须算作筛选条件：否则"只看已废止"这种纯筛选会走进
        # 空分支返回 0 条（实测：选「已废止」得 0 条，而库里有 1042 条）。
        if terms or column or tax or region or effect or year:
            if terms and all(len(t) >= 3 for t in terms):
                sql = (
                    "SELECT p.doc_uid, p.cwrq, p.title, p.p_doc_no_full, p.p_doc_no_confidence,"
                    " p.pub_name,"
                    " p.o_column, p.p_region, p.p_effect_status, p.p_effect_source, p.url"
                    " FROM policy_fts f JOIN policy p ON p.id = f.rowid"
                    " WHERE policy_fts MATCH ?"
                    # 分层：FTS 是主检索路径，这条**不能漏**。
                    # `FROM policy_fts` 里 policy 后面紧跟下划线，所以上面那轮
                    # 按词边界做的批量替换正好放过了它 —— 在这里单独补上。
                    " AND IFNULL(p.is_official, 1) = 1"
                )
                # 多词用 AND 连接。整体加引号会变成**短语查询**，
                # "增值税 优惠" 必然 0 条 —— 独立验证代理发现的问题。
                params: list = [" AND ".join(f'"{t}"' for t in terms)]
            elif terms:
                # 含 2 字词（"契税""关税""个税"）：trigram 分词器对 <3 字符的查询
                # 返回空结果，走 FTS 会**静默返回 0 条**，因此退回 LIKE。
                like_clause = " AND ".join(
                    "(p.title LIKE ? OR IFNULL(p.content,'') LIKE ?)" for _ in terms)
                sql = (
                    "SELECT p.doc_uid, p.cwrq, p.title, p.p_doc_no_full, p.p_doc_no_confidence,"
                    " p.pub_name,"
                    " p.o_column, p.p_region, p.p_effect_status, p.p_effect_source, p.url"
                    f" FROM policy_official p WHERE ({like_clause})"
                )
                params = []
                for t in terms:
                    params += [f"%{t}%", f"%{t}%"]
            else:
                sql = (
                    "SELECT p.doc_uid, p.cwrq, p.title, p.p_doc_no_full, p.pub_name,"
                    " p.o_column, p.p_region, p.p_effect_status, p.p_effect_source, p.url"
                    " FROM policy_official p WHERE 1=1"
                )
                params = []
            if column:
                sql += " AND p.o_column = ?"
                params.append(column)
            if tax:
                clause, tax_params = filters.tax_filter_sql(tax, "p")
                if clause:
                    sql += f" AND {clause}"
                    params += tax_params
            if region:
                clause, region_params = filters.region_filter_sql(region, "p")
                if clause:
                    sql += f" AND {clause}"
                    params += region_params
            # 效力状态与年份 —— 实际工作中最常用的两个限定
            # （"只看现行有效的""只看今年的"）。
            if effect:
                sql += " AND p.p_effect_status = ?"
                params.append(effect)
            if year.isdigit():
                sql += " AND p.cwrq LIKE ?"
                params.append(f"{year}-%")
            # 相关性排序：必须与 kb.py 的 _build_search 用同一套口径。
            # 那边给 AI/MCP 用、这里给网页用，两处一旦分叉，同一个查询在
            # 网页和 API 里会给出不同顺序，使用者无从判断哪个可信。
            rank_expr = ""
            rank_params: list = []
            if terms and all(len(t) >= 3 for t in terms):
                # FTS5 的 bm25() 越小越相关；权重按 FTS 表列序
                # （title, p_doc_no_full, pub_name, content, o_keywords）。
                rank_expr = f"bm25(policy_fts, {kb.BM25_WEIGHTS}), "
            elif terms:
                # LIKE 没有打分函数：退一步让**标题命中排在正文命中之前**。
                rank_expr = "(CASE WHEN p.title LIKE ? THEN 0 ELSE 1 END), "
                rank_params.append(f"%{terms[0]}%")

            # 命中总数：在追加 LIMIT 之前先算。
            # 原来模板用的是 results|length，于是搜「关税」页面写「共 50 条」，
            # 而实际命中 2348 —— 措辞把人误导成"库里就这么多"。
            # 注意 count 不带 ORDER BY，所以不能把 rank_params 算进去。
            try:
                count_sql = "SELECT COUNT(*) c FROM " + sql.split(" FROM ", 1)[1]
                matched_total = _one(count_sql, tuple(params))["c"]
            except Exception as e:  # noqa: BLE001 - 计数失败不该让整页挂掉
                log.warning("命中计数失败 q=%r: %s", q, e)
                matched_total = None

            if sort == "date_asc":
                sql += " ORDER BY p.cwrq ASC, p.id ASC LIMIT ?"
            elif sort == "date_desc":
                sql += " ORDER BY p.cwrq DESC, p.id DESC LIMIT ?"
            elif sort == "docno_asc":
                # 文号排序：先年份、同年的再排序号。**没有文号的排最后** ——
                # 不能靠 SQLite 的 NULL 默认行为：ASC 时 NULL 本来就在前，
                # 库里现在有 3299 条无文号（含刚清掉错文号的 575 条），
                # 那样第一屏会被空文号占满。(IS NULL) 单独作首要键解决这件事。
                sql += (" ORDER BY (p.p_doc_no_year IS NULL) ASC,"
                        " p.p_doc_no_year ASC, p.p_doc_no_seq ASC, p.id ASC LIMIT ?")
            elif sort == "docno_desc":
                sql += (" ORDER BY (p.p_doc_no_year IS NULL) ASC,"
                        " p.p_doc_no_year DESC, p.p_doc_no_seq DESC, p.id DESC LIMIT ?")
            else:
                sql += (f" ORDER BY {rank_expr}{_SUBSTANTIVE_FIRST},"
                        " p.cwrq DESC LIMIT ?")
            # 参数按 ? 在 SQL 里的顺序：WHERE 的 → ORDER BY 的 → LIMIT。
            params += rank_params
            params.append(limit)
            try:
                rows = _rows(sql, tuple(params))
            except Exception as e:  # noqa: BLE001 - 检索出错要让人看见
                # 不回显原始异常：FTS5 的语法错误消息会把检索实现细节
                # （表名、列名、查询语法）泄露到页面上。细节写日志即可。
                log.warning("检索失败 q=%r: %s", q, e)
                error = "检索失败，请调整关键词后重试。"

        columns = [r["v"] for r in _rows(
            "SELECT DISTINCT o_column v FROM policy_official WHERE o_column IS NOT NULL")]
        conn = dbmod.connect()
        try:
            tax_counts = filters.tax_type_counts(conn)
            regions = filters.region_counts(conn)
            effect_counts = [
                ((r["p_effect_status"] or "未判定"), r["c"])
                for r in _rows("SELECT p_effect_status, COUNT(*) c FROM policy_official"
                               " GROUP BY 1 ORDER BY c DESC")
            ]
            years = [
                r["y"] for r in _rows(
                    "SELECT DISTINCT SUBSTR(cwrq,1,4) y FROM policy_official"
                    " WHERE cwrq IS NOT NULL AND LENGTH(cwrq) >= 4"
                    " ORDER BY y DESC")
                if r["y"] and str(r["y"]).isdigit()
            ]
        finally:
            conn.close()
        return templates.TemplateResponse(
            request=request, name="search.html",
            context=ctx(request, results=rows, error=error, columns=columns,
                        column=column, limit=limit, tax=tax, tax_counts=tax_counts,
                        region=region, regions=regions,
                        effect=effect, effect_counts=effect_counts,
                        year=year, years=years, sort=sort,
                        matched_total=matched_total,
                        hl_terms=terms, term_hits=term_hits, q_original=q))

    # ------------------------------------------------------------ 详情

    @app.get("/policy/{doc_uid:path}", response_class=HTMLResponse)
    def policy_detail(request: Request, doc_uid: str):
        # 详情页查**原表**：参考层的条目也要能通过链接打开看（只是不出现在
        # 列表与检索结果里）。页面上会标注它是「非政策依据」。
        policy = _one("SELECT * FROM policy WHERE doc_uid = ?", (doc_uid,))
        if policy is None:
            return HTMLResponse("<h1>404</h1><p>没有这条政策。</p>", status_code=404)
        # 关系查询改为引用 kb.py 的**唯一一份定义**。
        # 原先这里抄了一份，少了 r.evidence_source、p.p_doc_no_full AS target_doc_no
        # 与 s.url 三个字段 —— 结果是同一份政策，走 MCP 接口能看到引用文号、
        # 走网页看不到（2026-10 全量审计发现的分叉）。
        citations = _rows(kb.SQL_CITATIONS, (doc_uid,))
        repealed = _rows(kb.SQL_REPEALED_BY, (doc_uid,))
        # 附件正文：只取前 2 万字渲染 —— 有的申报表附件单篇就好几万字，
        # 全塞进页面会让详情页变得极慢。完整文本仍在库里（attachment.parsed_text），
        # 需要全文时另行导出，页面上会注明已截断。
        attachments = _rows(
            "SELECT filename, ext, url, parse_status,"
            " LENGTH(COALESCE(parsed_text,'')) AS text_len,"
            " SUBSTR(COALESCE(parsed_text,''), 1, 20000) AS text_excerpt"
            " FROM attachment WHERE doc_uid=? ORDER BY id", (doc_uid,))
        snapshots = _rows(
            "SELECT kind, fetched_at, rel_path, size_bytes FROM raw_snapshot"
            " WHERE doc_uid=? ORDER BY id DESC LIMIT 5", (doc_uid,))
        # 英文译文（机器翻译）。模板里必须标注来源 —— 法律文本的译文被当成
        # 官方英文版本引用，是会出事的。
        #
        # **表可能不存在**：translation 由翻译脚本自己 ensure，不在 init_db 的
        # 建表清单里。所以没跑过翻译的库（含全新初始化的库、没升级的老库）
        # 查它会抛 "no such table: translation"。
        # 缺译文只是少一行标注，**不该让整个详情页 500** —— 这里降级为"没有译文"。
        # 只捕获 OperationalError：别的 SQL 问题（写错列名之类）仍要暴露出来。
        try:
            tr_title = _one(
                "SELECT text, model, created_at FROM translation"
                " WHERE doc_uid=? AND field='title' AND lang='en'", (doc_uid,))
            tr_content = _one(
                "SELECT text, model, created_at FROM translation"
                " WHERE doc_uid=? AND field='content' AND lang='en'", (doc_uid,))
        except sqlite3.OperationalError:
            log.warning("translation 表不可用，详情页按“无译文”渲染：%s", doc_uid)
            tr_title = tr_content = None
        return templates.TemplateResponse(
            request=request, name="detail.html",
            context=ctx(request, p=policy, citations=citations, repealed=repealed,
                        attachments=attachments, snapshots=snapshots,
                        tr_title=tr_title, tr_content=tr_content))

    # ------------------------------------------------------------ 每日简报

    @app.get("/daily", response_class=HTMLResponse)
    def daily(request: Request, days: int = Query(14, ge=1, le=365)):
        rows = _rows(
            "SELECT p.doc_uid, p.cwrq, p.pub_date, p.title, p.p_doc_no_full,"
            " p.p_doc_no_confidence, p.pub_name,"
            " p.o_column, p.p_effect_status, p.p_effect_source, p.url,"
            " (SELECT COUNT(*) FROM attachment a WHERE a.doc_uid=p.doc_uid) AS n_attach"
            " FROM policy_official p"
            " WHERE p.cwrq >= date('now', ?)"
            f" ORDER BY {_SUBSTANTIVE_FIRST}, p.cwrq DESC",
            (f"-{days} days",))
        # 按日期归并分组。**不能依赖"相邻即同组"**：排序是先按实质政策优先、
        # 再按日期，同一天的政策法规与解读会被隔开，顺序分组会产出重复的日期组头
        # （实测：同一天可能出现"09-22 · 6 条"与"09-22 · 2 条"两个组）。
        buckets: dict[str, list] = {}
        for r in rows:
            buckets.setdefault(r["cwrq"] or "(无日期)", []).append(r)
        grouped = sorted(buckets.items(), key=lambda kv: kv[0], reverse=True)
        return templates.TemplateResponse(
            request=request, name="daily.html",
            context=ctx(request, grouped=grouped, days=days, total=len(rows)))

