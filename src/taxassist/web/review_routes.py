"""审校台（/review）—— 把机器判定与人工结论摆在一起看。

## 为什么要专门一页

译文质量此前只有两个去处：脚本产出的 JSON 散在 `data/logs` 下，或者数据库里
一万两千行判定没人看得过来（`translation_ai_review` 12290 行：ok 11658 /
issue 481 / unsure 151）。这一页把「机器说有问题」与「人已经核过的」并排放，
让判断有落点 —— 而不是让那 481 条一直躺在一个谁也打不开的表里。

**两张表语义不同，页面上也分开**：
* `translation_ai_review` —— 机器判定，可重跑可覆盖，只用于筛选与排序。
* `translation_review` —— 人工结论，是结论，机器结果永不覆盖它。

## 数据来自仓储层

`repo.review` 与 `repo.translation` 天然分后端（SQLite / PostgreSQL 都读），
所以这一页在切库前后是同一份代码。
"""
from __future__ import annotations

import logging

from fastapi import Query, Request
from fastapi.responses import HTMLResponse

from .. import db as dbmod
from ..repo import review as R_review
from ..repo import translation as R_tr

log = logging.getLogger(__name__)


def register(app, *, templates, require_auth: bool) -> None:
    """把 /review 挂到 app 上。"""

    @app.get("/review", response_class=HTMLResponse)
    def review_page(request: Request,
                    field: str = Query("content"),
                    verdict: str = Query("issue"),
                    limit: int = Query(50, ge=1, le=500)):
        """审校台：先看判定分布，再按判定值取明细。

        ``field`` 取 title 或 content —— 两者的判定质量差别很大（标题短、
        缺上下文，AI 更容易拿不准），混在一起看会误导。
        """
        conn = dbmod.connect()
        try:
            ai_stats = R_review.ai_stats(conn, field)
            human_stats = R_review.human_stats(conn)
            human_total = R_review.human_count(conn)
            pending = len(R_review.unreviewed(conn, field=field, limit=1000))
            rows = R_review.ai_by_verdict(conn, verdict, field, limit) if verdict else []

            # 把每条的译文带上：审校时要同时看到原文判定理由与译文本身。
            items = []
            for r in rows:
                t = R_tr.get(conn, r["doc_uid"], r["field"])
                items.append({
                    "doc_uid": r["doc_uid"],
                    "field": r["field"],
                    "verdict": r["verdict"],
                    "reason": r["reason"],
                    "model": r["model"],
                    "reviewed_at": r["reviewed_at"],
                    "text": (t["text"] if t else ""),
                })
        finally:
            conn.close()

        me = getattr(request.state, "user", None)
        return templates.TemplateResponse(
            request=request, name="review.html",
            context={
                "request": request, "user": me,
                "is_owner": getattr(request.state, "is_owner", False),
                "exposed": require_auth, "lang": "zh",
                "ai_stats": ai_stats,
                "human_stats": human_stats,
                "human_total": human_total,
                "pending": pending,
                "items": items,
                "field": field, "verdict": verdict, "limit": limit,
            })
