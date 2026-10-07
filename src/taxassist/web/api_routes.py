"""对外开放的 API —— 客户用 API Key 调用本机的政策库。

**为什么开在 web 层**：认证、限流、计量、审计都在这一层，与页面共用同一套中间件
与 request.state。`kb_api.py` 的设计说明写得很清楚 —— 它刻意不做认证、只绑回环，
"要对外请用 web/app.py 那条路"。这一页就是那条路。

**鉴权**：请求头 `Authorization: Bearer tk_...`。库里只存 Key 的 sha256，
所以丢了只能重签、找不回来。

**计费**：每次调用扣 1 次**检索余量**（计费种类 search）；**请求成功才扣**
（失败不扣，避免"失败也计费"的争议）；余额不足返回 **402** 并明确说找谁充值。

**三条边界不变**：
  · 只暴露**公开政策** —— 客户上传的合同等数据仍只在本机处理，不经这里出去
  · 本模块**只读**，没有任何写操作
  · 对外访问必须经隧道 + `--expose`，否则等于没有门锁（项目既有规矩）
"""
from __future__ import annotations

import json
import logging

from fastapi import Request
from fastapi.responses import JSONResponse

from .. import billing
from .. import db as dbmod
from .. import kb

log = logging.getLogger(__name__)

_NO_BALANCE = {
    "error": "检索余量不足，请联系管理员充值",
    "how": "余额按次数计，检索与助手分开计价；充值后立即生效，无需换 Key",
}


def register(app, *, require_auth: bool) -> None:
    """把 /api/* 挂到 app 上。"""

    def _json(data):
        """把库返回值清成纯 JSON。

        kb 返回的是 dict/list，值类型本来干净，但一旦混进 date/datetime 之类
        JSONResponse 就会抛错 —— 在边界上兜一层 `default=str` 比逐个排查划算。
        """
        return json.loads(json.dumps(data, ensure_ascii=False, default=str))

    def _auth(request: Request) -> str | None:
        header = request.headers.get("authorization", "")
        if not header.lower().startswith("bearer "):
            return None
        conn = dbmod.connect()
        try:
            return billing.resolve_key(conn, header[7:].strip())
        finally:
            conn.close()

    def _charge(customer: str, ok: bool, detail: str) -> bool:
        """记一次用量；返回 False = 余额不足（调用方回 402）。"""
        conn = dbmod.connect()
        try:
            return billing.charge(conn, customer, billing.KIND_SEARCH,
                                  ok=ok, detail=detail)
        finally:
            conn.close()

    def _unauthorized():
        return JSONResponse(
            {"error": "缺少或无效的 API Key",
             "how": "请求头加 Authorization: Bearer tk_…（Key 由站长在后台签发）"},
            status_code=401)

    @app.get("/api/overview")
    def api_overview(request: Request):
        customer = _auth(request)
        if not customer:
            return _unauthorized()
        try:
            data = kb.overview()
        except Exception as exc:  # noqa: BLE001 - 对外接口不能漏栈
            _charge(customer, False, "overview failed")
            log.warning("API overview 失败：%s", exc)
            return JSONResponse({"error": "库暂时不可用"}, status_code=503)
        if not _charge(customer, True, "overview"):
            return JSONResponse(_NO_BALANCE, status_code=402)
        return JSONResponse(_json(data))

    @app.get("/api/search")
    def api_search(request: Request, q: str = "", tax: str = "", region: str = "",
                   effect: str = "", year: str = "", column: str = "",
                   sort: str = "relevance", limit: int = 20, offset: int = 0):
        customer = _auth(request)
        if not customer:
            return _unauthorized()
        try:
            res = kb.search(query=q, tax=tax, region=region, effect=effect,
                            year=year, column=column, sort=sort,
                            limit=limit, offset=offset)
        except Exception as exc:  # noqa: BLE001
            _charge(customer, False, f"search q={q[:60]}")
            log.warning("API search 失败：%s", exc)
            return JSONResponse({"error": "检索失败，请调整关键词"}, status_code=503)
        if res.get("error"):        # kb 自己判定的参数问题：不计费
            _charge(customer, False, f"search q={q[:60]}")
            return JSONResponse({"error": res["error"]}, status_code=400)
        if not _charge(customer, True, f"search q={q[:60]}"):
            return JSONResponse(_NO_BALANCE, status_code=402)
        return JSONResponse(_json(res))

    @app.get("/api/policy/{doc_uid:path}")
    def api_policy(request: Request, doc_uid: str):
        customer = _auth(request)
        if not customer:
            return _unauthorized()
        try:
            data = kb.get_policy(doc_uid)
        except Exception as exc:  # noqa: BLE001
            _charge(customer, False, f"policy {doc_uid[:60]}")
            log.warning("API policy 失败：%s", exc)
            return JSONResponse({"error": "取政策失败"}, status_code=503)
        if data is None:
            _charge(customer, False, f"policy 404 {doc_uid[:60]}")
            return JSONResponse({"error": "没有这条政策"}, status_code=404)
        if not _charge(customer, True, f"policy {doc_uid[:60]}"):
            return JSONResponse(_NO_BALANCE, status_code=402)
        return JSONResponse(_json(data))

    @app.post("/api/mcp")
    async def api_mcp(request: Request):
        """**MCP over HTTP** —— 让 Cursor / Claude Desktop 这类 AI 客户端直连。

        MCP 的标准传输是 stdio（本机子进程），远程客户用不了 —— 这正是
        mcp_server.py 那样写着"只供本机"的原因。这里用 **JSON-RPC over HTTP**
        承载同一套方法：请求体一条 JSON-RPC，响应体一条回复，直接复用
        `mcp_server.handle_message` —— 所以方法集与 stdio 版**完全一致**
        （search_policies / get_policy / lookup_by_doc_no / kb_overview），
        不会出现"两个版本能力不一样"。

        鉴权与计量同其它 /api/*：Bearer Key，**成功才扣**（JSON-RPC 的 error
        回复同样不扣 —— 那是调用方的问题，不是我们提供了服务）。
        """
        customer = _auth(request)
        if not customer:
            return _unauthorized()
        try:
            msg = await request.json()
        except Exception:  # noqa: BLE001 - 畸形 JSON 按 JSON-RPC 规范回 -32700
            return JSONResponse(
                {"jsonrpc": "2.0", "id": None,
                 "error": {"code": -32700, "message": "Parse error"}},
                status_code=400)

        from ..mcp_server import handle_message

        reply = handle_message(msg)
        if reply is None:
            # 通知类消息：MCP 约定不回复。仍然记一笔用量（便于看出客户端在活动）。
            _charge(customer, True, f"mcp notify {str(msg.get('method'))[:40]}")
            return JSONResponse({"jsonrpc": "2.0", "id": None, "result": {}})

        ok = "error" not in reply
        if not _charge(customer, ok, f"mcp {str(msg.get('method'))[:40]}"):
            return JSONResponse(_NO_BALANCE, status_code=402)
        return JSONResponse(_json(reply))
