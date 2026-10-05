"""本地 HTTP 接口：给自建程序与不支持 MCP 的本地模型调用。

============================================================
与 web/app.py 的分工
============================================================

``web/app.py`` 面向**人**：HTML 页面、登录会话、邀请码注册，绑 0.0.0.0 +
Tailscale 对外。本模块面向**程序**：纯 JSON、无会话、无 Cookie、默认只绑
127.0.0.1。两者共用同一个只读内核（kb.py），所以「AI 查到什么」和
「网页上搜到什么」永远是同一套结果。

刻意**不做**的三件事：

1. **不做认证。** 只监听回环地址时，能访问它的只有本机进程；加一套 token
   只会让接入变麻烦，且容易让人误以为「有了 token 就能对外暴露」。要对外
   请用 web/app.py 那条已经配好认证与 HTTPS 的路。
2. **不做写操作。** 与 web 层同一条纪律：写操作集中在 pipeline/store，
   便于审计与复现。
3. **不开 CORS。** 本机脚本调用不需要它；开了等于给浏览器里的任意页面
   访问本政策库的权限。
"""
from __future__ import annotations

import logging

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse

from . import kb

log = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8766


def create_app() -> FastAPI:
    # 关掉文档与 openapi：默认的 /docs 会把接口清单和参数试玩台一并暴露，
    # 对一个「本机数据接口」没有必要（web 层出于同样的理由也关了）。
    app = FastAPI(title="税务政策知识库（本机接口）", docs_url=None,
                  redoc_url=None, openapi_url=None)

    def _kb_error(exc: kb.KBError) -> JSONResponse:
        # 库不可用是环境问题，不是请求问题 —— 503 比 500 更准确，
        # 让调用方能区分「我传错了」和「库还没准备好」。
        return JSONResponse({"error": str(exc)}, status_code=503)

    @app.get("/")
    def index():
        """端点清单。人肉验证时打开根路径就知道有什么可调。"""
        return {
            "service": "税务政策知识库（本机只读接口）",
            "endpoints": {
                "GET /api/search": "全文检索：q, tax, region, effect, year, column, sort, limit, offset",
                "GET /api/policy/{doc_uid}": "单条政策档案：content_offset, max_chars",
                "GET /api/docno/{doc_no}": "按文号查找",
                "GET /api/overview": "库概况与数据新鲜度",
                "GET /health": "健康检查",
            },
            "note": "只读；数据与检索全部在本机执行，不出网。",
        }

    @app.get("/health")
    def health():
        try:
            conn = kb.connect_readonly()
            try:
                total = conn.execute("SELECT COUNT(*) c FROM policy").fetchone()["c"]
            finally:
                conn.close()
        except kb.KBError as e:
            return JSONResponse({"status": "unavailable", "detail": str(e)}, status_code=503)
        return {"status": "ok", "total_policies": total}

    @app.get("/api/search")
    def api_search(
        q: str = Query("", description="关键词，可为空（纯筛选）"),
        tax: str = Query("", description="税种，如 增值税"),
        region: str = Query("", description="地区，如 广东"),
        effect: str = Query("", description="效力状态，如 现行有效"),
        year: str = Query("", description="成文年份，如 2026"),
        column: str = Query("", description="栏目"),
        sort: str = Query("relevance", description="relevance | date_desc | date_asc"),
        limit: int = Query(kb.DEFAULT_LIMIT, description=f"返回条数，上限 {kb.MAX_LIMIT}"),
        offset: int = Query(0, description="翻页偏移"),
    ):
        try:
            return kb.search(q, tax=tax, region=region, effect=effect, year=year,
                             column=column, sort=sort, limit=limit, offset=offset)
        except kb.KBError as e:
            return _kb_error(e)

    @app.get("/api/policy/{doc_uid:path}")
    def api_policy(
        doc_uid: str,
        content_offset: int = Query(0, description="从正文第几个字符开始读"),
        max_chars: int = Query(kb.DEFAULT_CONTENT_CHARS,
                               description=f"本次返回正文字符数，上限 {kb.MAX_CONTENT_CHARS}"),
    ):
        try:
            result = kb.get_policy(doc_uid, content_offset=content_offset, max_chars=max_chars)
        except kb.KBError as e:
            return _kb_error(e)
        if result is None:
            return JSONResponse({"error": f"没有 doc_uid={doc_uid} 的政策"}, status_code=404)
        return result

    @app.get("/api/docno/{doc_no:path}")
    def api_docno(doc_no: str, limit: int = Query(10)):
        try:
            return kb.lookup_by_doc_no(doc_no, limit=limit)
        except kb.KBError as e:
            return _kb_error(e)

    @app.get("/api/overview")
    def api_overview():
        try:
            return kb.overview()
        except kb.KBError as e:
            return _kb_error(e)

    return app


def main(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> int:
    import uvicorn

    if host not in ("127.0.0.1", "localhost", "::1"):
        # 不阻止，但必须让人看见风险：这个接口没有任何认证，
        # 绑到局域网地址等于把政策库交给同一网段的每一台设备。
        print("=" * 68)
        print(f"⚠  正在把【无认证】的只读接口绑定到 {host}:{port}")
        print("   同一网络内的任何设备都能检索本政策库。")
        print("   需要对外提供访问，请改用 `python -m taxassist serve`（带认证与 HTTPS）。")
        print("=" * 68)

    print(f"税务政策知识库接口：http://{host}:{port}/")
    print(f"  检索示例：http://{host}:{port}/api/search?q=研发费用加计扣除&limit=5")
    uvicorn.run(create_app(), host=host, port=port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
