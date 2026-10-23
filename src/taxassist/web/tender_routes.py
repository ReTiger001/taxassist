"""招标信息浏览：列表 / 筛选 / 详情。

与政策检索（`browse_routes`）分开：招标是**商机**，筛选维度不同（截止日、
预算、公告类型），且默认只看税务服务类。混在一起会让两个页面的筛选语义都变糊。

数据源与抓取见 `collect/ccgp.py`；数据模型见 `tender.py`。
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from fastapi import Query, Request
from fastapi.responses import HTMLResponse

from .. import db as dbmod
from .. import tender as tmod
from .helpers import _one, _rows

log = logging.getLogger(__name__)


def _ensure(conn) -> None:
    try:
        tmod.ensure_table(conn)
    except Exception as e:  # noqa: BLE001 - 建表失败不该让页面 500
        log.warning("tender 建表失败：%s", e)


def _days_left(deadline: str | None, today: date) -> int | None:
    """距截止日期还有几天。解析不出返回 None（页面照常显示原日期）。"""
    if not deadline:
        return None
    txt = str(deadline).strip()[:10]
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return (datetime.strptime(txt, fmt).date() - today).days
        except ValueError:
            continue
    return None


def register(app, *, ctx, templates) -> None:
    """把 /tenders、/tender/{doc_uid} 挂到 app 上。"""

    @app.get("/tenders", response_class=HTMLResponse)
    def tenders_page(request: Request,
                     q: str = Query("", max_length=80),
                     region: str = "",
                     notice_type: str = "",
                     all_tax: str = "0",
                     ai_all: str = "0",
                     show_finished: str = "0",
                     recent: str = "90",
                     days: str = "30",
                     page: int = Query(1, ge=1),
                     limit: int = Query(30, ge=1, le=100)):
        """招标列表。

        **默认只看税务类**（用户要求）；``all_tax=1`` 可切到"看全部"。
        ``ai_all=1`` 可把 AI 判为非税务的也显示出来。

        ``recent``（默认 90）：只看**近 N 天发布，且未截止**的标。
        为什么默认开：CMS 平台（邮储/湖北联投）按时间倒序也混着几个月前的历史标，
        不滤的话第一屏全是早已截止的。``recent=all`` 可看全部。
        """
        conn = dbmod.connect()
        try:
            _ensure(conn)
        finally:
            conn.close()

        where: list[str] = []
        params: list = []
        # **反向语义**：checkbox 未勾选时不传参，若用 `tax_only=1` 作默认值就永远
        # 关不掉（审计 B8）。所以用 `all_tax=1` 表示「看全部（含非税务）」。
        if all_tax != "1":
            where.append("t.is_tax = 1")
            # **AI 服务类型过滤**：关键词会把「税务局买食堂/工程/系统开发」也收进来
            # （标题含"税务局""服务"就命中）。AI 评估已判定它们不是税务专业服务。
            # 未评估的**保留**（宁多勿漏），只挡掉 AI 明确判为"非税务服务"的。
            if ai_all != "1":
                where.append("(t.ai_is_tax_service IS NULL OR t.ai_is_tax_service = 1)")
        # **默认只看还能投的阶段**：中标结果/中标候选人/合同意味着项目已经定标，
        # 再去投是白费力气。用户看不出"候选供应商征集"（还能投）和"中标候选人
        # 公示"（已结束）的区别，所以由系统默认挡掉（勾"看全部"仍可看到）。
        if show_finished != "1":
            where.append("(t.bid_process IS NULL OR t.bid_process NOT IN"
                         " ('中标结果', '中标候选人', '合同'))")
        # **投递窗口过滤**：近 N 天发布 + 尚未截止。
        # 截止日未知的条目**保留**（宁多勿漏 —— 错过一个标比多一条噪音代价大）。
        if recent != "all" and recent.isdigit() and int(recent) > 0:
            since = (date.today() - timedelta(days=int(recent))).isoformat()
            where.append("(t.publish_at IS NULL OR t.publish_at >= ?)")
            params.append(since)
            today = date.today().isoformat()
            where.append("(t.deadline IS NULL OR t.deadline = '' OR t.deadline >= ?)")
            params.append(today)
        if q:
            where.append("(t.title LIKE ? OR t.content LIKE ? OR t.project_no LIKE ?)")
            like = f"%{q}%"
            params += [like, like, like]
        if region:
            where.append("t.region = ?")
            params.append(region)
        if notice_type:
            where.append("t.notice_type = ?")
            params.append(notice_type)
        if days.isdigit() and int(days) > 0:
            since2 = (date.today() - timedelta(days=int(days))).isoformat()
            where.append("(t.publish_at IS NULL OR t.publish_at >= ?)")
            params.append(since2)

        where_sql = (" WHERE " + " AND ".join(where)) if where else ""
        total = 0
        rows: list = []
        try:
            total = _one(f"SELECT COUNT(*) AS n FROM tender t{where_sql}",
                         tuple(params))["n"]
            rows = _rows(
                f"SELECT t.* FROM tender t{where_sql}"
                f" ORDER BY t.publish_at DESC, t.doc_uid DESC LIMIT ? OFFSET ?",
                tuple(params + [limit, (page - 1) * limit]))
        except Exception as e:  # noqa: BLE001
            log.warning("招标列表查询失败：%s: %s", type(e).__name__, e)

        # **截止日剩余天数**：投标最怕"看到时已经来不及"。
        # 3 天内到期的在页面上高亮（红色），已截止的置灰。
        today = date.today()
        rows = [dict(r) for r in rows]
        for r in rows:
            r["days_left"] = _days_left(r.get("deadline"), today)
            r["urgent"] = (r["days_left"] is not None and 0 <= r["days_left"] <= 3)

        # 筛选项取值（地区 / 公告类型）——从已入库数据里现取，不维护硬编码列表
        regions: list[str] = []
        types: list[str] = []
        try:
            regions = [r["region"] for r in _rows(
                "SELECT DISTINCT region FROM tender WHERE region IS NOT NULL"
                " ORDER BY region")]
            types = [r["notice_type"] for r in _rows(
                "SELECT DISTINCT notice_type FROM tender"
                " WHERE notice_type IS NOT NULL ORDER BY notice_type")]
        except Exception:  # noqa: BLE001
            pass

        pages = max(1, (int(total) + limit - 1) // limit)

        def url_with(**kw) -> str:
            """保留其它筛选条件的翻页/筛选链接（与 browse_routes 同一手法）。"""
            cur = {"q": q, "region": region, "notice_type": notice_type,
                   "show_finished": show_finished,
                   "recent": recent, "all_tax": all_tax, "ai_all": ai_all, "days": days, "page": page, "limit": limit}
            cur.update(kw)
            qs = "&".join(f"{k}={v}" for k, v in cur.items() if v not in (None, ""))
            return f"/tenders?{qs}"

        return templates.TemplateResponse(
            request=request, name="tenders.html",
            context=ctx(request,
                        q=q, region=region, notice_type=notice_type,
                        show_finished=show_finished,
                        recent=recent, all_tax=all_tax, ai_all=ai_all, days=days, page=page, limit=limit,
                        pages=pages, total=total, rows=rows,
                        regions=regions, types=types,
                        notice_types=tmod.NOTICE_TYPES,
                        not_found=not rows, url_with=url_with))

    @app.get("/tender/{doc_uid}", response_class=HTMLResponse)
    def tender_detail(request: Request, doc_uid: str):
        """单条公告详情。"""
        conn = dbmod.connect()
        try:
            _ensure(conn)
        finally:
            conn.close()
        row = None
        try:
            row = _one("SELECT * FROM tender WHERE doc_uid = ?", (doc_uid,))
        except Exception as e:  # noqa: BLE001
            log.warning("招标详情查询失败 %s：%s", doc_uid, e)
        # 把 bid_guide（JSON 串）解析成 dict，模板里直接按段渲染。
        # 抽不到时为 {}，模板会走「只抓到列表信息」的提示分支。
        guide = {}
        if row:
            try:
                from ..bid_guide import guide_of, platform_url

                guide = guide_of(row)
                # 平台网址：数据里没存时现场按平台名再查一次（映射表可能已更新）
                if not row.get("platform_url"):
                    row["platform_url"] = platform_url(row.get("agency"))
            except Exception as e:  # noqa: BLE001
                log.debug("解析投标指引失败 %s：%s", doc_uid, e)
        return templates.TemplateResponse(
            request=request, name="tender_detail.html",
            context=ctx(request, row=row, guide=guide,
                        notice_types=tmod.NOTICE_TYPES), status_code=200)
