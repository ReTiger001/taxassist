"""招标信息浏览：列表 / 筛选 / 详情。

与政策检索（`browse_routes`）分开：招标是**商机**，筛选维度不同（截止日、
预算、公告类型），且默认只看税务服务类。混在一起会让两个页面的筛选语义都变糊。

数据源与抓取见 `collect/ccgp.py`；数据模型见 `tender.py`。
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

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


def register(app, *, ctx, templates) -> None:
    """把 /tenders、/tender/{doc_uid} 挂到 app 上。"""

    @app.get("/tenders", response_class=HTMLResponse)
    def tenders_page(request: Request,
                     q: str = Query("", max_length=80),
                     region: str = "",
                     notice_type: str = "",
                     tax_only: str = "1",
                     days: str = "30",
                     page: int = Query(1, ge=1),
                     limit: int = Query(30, ge=1, le=100)):
        """招标列表。

        ``tax_only=1``（默认）只看判为税务服务的条目 —— 用户明确要"默认只显示
        税务类，可切换看全部"。``tax_only=0`` 时显示全部（含设备/工程采购）。
        """
        conn = dbmod.connect()
        try:
            _ensure(conn)
        finally:
            conn.close()

        where: list[str] = []
        params: list = []
        if tax_only == "1":
            where.append("t.is_tax = 1")
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
            since = (date.today() - timedelta(days=int(days))).isoformat()
            where.append("(t.publish_at IS NULL OR t.publish_at >= ?)")
            params.append(since)

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
                   "tax_only": tax_only, "days": days, "page": page, "limit": limit}
            cur.update(kw)
            qs = "&".join(f"{k}={v}" for k, v in cur.items() if v not in (None, ""))
            return f"/tenders?{qs}"

        return templates.TemplateResponse(
            request=request, name="tenders.html",
            context=ctx(request,
                        q=q, region=region, notice_type=notice_type,
                        tax_only=tax_only, days=days, page=page, limit=limit,
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
        return templates.TemplateResponse(
            request=request, name="tender_detail.html",
            context=ctx(request, row=row,
                        notice_types=tmod.NOTICE_TYPES), status_code=200)
