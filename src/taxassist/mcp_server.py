"""MCP 服务：把本地政策库挂给 AI（stdio，零依赖）。

============================================================
为什么手写协议而不是引入官方 SDK
============================================================

本项目要能**离线运行**（README 的边界之一），而 mcp SDK 的版本迭代快、
依赖树不小（pydantic / anyio / sse-starlette…）。本服务只用到协议的
「tools」这一小块，手写的成本远低于长期跟版本的代价，也不会因为 SDK
升级而突然不可用。

============================================================
三条实现纪律
============================================================

1. **stdout 只走 JSON-RPC。** 任何一行日志混进 stdout 都会让客户端解析失败，
   表现为「服务没反应」而不是报错。所有日志一律去 stderr。
2. **绝不因为一次调用失败而退出。** AI 传错参数是常态；进程崩一次，客户端
   就会把服务标记为不可用。工具内的异常一律转成 ``isError`` 结果返回。
3. **UTF-8 硬编码读写字节流。** Windows 控制台默认 GBK，中文政策正文里
   一个 GBK 编不出的字符（如 "↔"）就能让整个服务抛 UnicodeEncodeError
   死掉 —— 实测踩过一次。这里直接操作 buffer，绕开文本层的编码推断。
"""
from __future__ import annotations

import json
import logging
import sys

from . import kb

log = logging.getLogger(__name__)

SERVER_NAME = "taxassist-kb"
SERVER_VERSION = "0.1.0"

#: 声明支持的协议版本。回版本号时**优先回显客户端发来的那个** ——
#: 客户端若只认旧版（如 Claude Desktop 的 2024-11-05），回一个它不认识的
#: 新版会被直接断开。本服务只用 tools 能力，各版本在这部分是兼容的。
SUPPORTED_PROTOCOLS = ("2024-11-05", "2025-03-26", "2025-06-18")
LATEST_PROTOCOL = SUPPORTED_PROTOCOLS[-1]

#: 交给 AI 的服务级说明。这里是**唯一**能主动告诉 AI「怎么用这个库」的地方，
#: 所以引用规范与数据边界必须写在这里，而不是藏在某个工具的 description 里。
INSTRUCTIONS = """本地税务政策知识库（数据与检索全部在本机执行，不出网）。

使用要点：
1. 引用政策必须给出文号与原文 URL。本库不允许「无出处的结论」。
2. 效力状态为「未知」或结果里 needs_review=true 的条目，必须提示需人工核对，
   不得直接当作有效依据下结论。
3. 效力判定有四种来源，引用时要说清是哪一种：
   official=官方标注；inferred=据废止公告/正文推断（**推断，非官方**）；
   default=推定有效（仅因未发现废止，最弱）；manual=人工确认。
4. 政策有时效。回答任何涉及「现在是否有效」「最新规定」的问题前，
   先调 kb_overview 看 data_freshness（最后成功抓取时间）。
5. 检索为空时不要直接回答「没有相关规定」——先换词、去掉部分关键词，
   或用 kb_overview 查看库里的税种/地区/年份可选值再试。
"""

# ---------------------------------------------------------------- 工具定义

TOOLS = [
    {
        "name": "search_policies",
        "description": (
            "在已入库的税务政策库（国家税务总局 + 各省税务局）做全文检索，"
            "可按税种、地区、年份、效力状态、栏目筛选。"
            "返回标题、文号、成文日期、效力状态、原文 URL 与正文摘要片段。"
            "多词之间是 AND 关系（词越多命中越窄）；2 字词（如「契税」）与"
            "英文术语（如 value-added tax）都已做兼容处理，可直接用。"
            "引用结果时务必带上 doc_no 与 url。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "关键词，可为空（为空则纯按筛选条件浏览）"},
                "tax": {"type": "string", "description": "税种，如：增值税、企业所得税、个人所得税、契税、印花税。"},
                "region": {"type": "string", "description": "地区，如：全国、广东、上海、新疆。"},
                "effect": {"type": "string", "description": "效力状态，如：现行有效、已废止、尚未生效。"},
                "year": {"type": "string", "description": "成文年份，如 2026。"},
                "column": {"type": "string", "description": "栏目，如：政策法规、政策解读、政策指引。"},
                "sort": {"type": "string", "enum": ["relevance", "date_desc", "date_asc"],
                         "description": "默认 relevance（实质政策优先 + 日期倒序）"},
                "limit": {"type": "integer", "minimum": 1, "maximum": kb.MAX_LIMIT,
                          "description": f"返回条数，默认 {kb.DEFAULT_LIMIT}，最多 {kb.MAX_LIMIT}"},
                "offset": {"type": "integer", "minimum": 0, "description": "翻页偏移"},
            },
            "required": [],
        },
    },
    {
        "name": "get_policy",
        "description": (
            "取一条政策的完整档案：正文（默认前 6000 字，可用 content_offset 续读）、"
            "效力状态及其证据片段、引用关系、废止/被废止关系、附件摘要与原文链接。"
            "doc_uid 来自 search_policies 或 lookup_by_doc_no 的结果。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "doc_uid": {"type": "string", "description": "政策唯一标识，取自检索结果"},
                "content_offset": {"type": "integer", "minimum": 0,
                                   "description": "从正文第几个字符开始读，用于续读长文"},
                "max_chars": {"type": "integer", "minimum": 500, "maximum": kb.MAX_CONTENT_CHARS,
                              "description": f"本次返回的正文字符数，默认 {kb.DEFAULT_CONTENT_CHARS}"},
            },
            "required": ["doc_uid"],
        },
    },
    {
        "name": "lookup_by_doc_no",
        "description": (
            "按文号精确查找政策，如「财税〔2023〕25号」。自动兼容全角/半角括号与空格差异。"
            "返回该文号对应政策的当前效力状态与原文 URL；"
            "若该文号在库中没有原文但被其他文件引用过，也会返回引用来源。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "doc_no": {"type": "string", "description": "完整或部分文号"},
                "limit": {"type": "integer", "minimum": 1, "maximum": kb.MAX_LIMIT},
            },
            "required": ["doc_no"],
        },
    },
    {
        "name": "kb_overview",
        "description": (
            "知识库概览：总条数、效力分布、可用的税种/地区/年份/栏目取值与各自条数、"
            "数据新鲜度（最后成功抓取时间）、待人工确认条数。"
            "开始检索前调它可确定筛选值；回答时效性问题前调它看数据截止到哪天。"
        ),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
]


# ---------------------------------------------------------------- 参数清洗

def _as_int(value, default: int) -> int:
    """AI 有时会把数字写成字符串（"10"），或干脆传错。一律退回默认值，
    不因为一个参数格式就让整次调用失败。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_str(value) -> str:
    return value if isinstance(value, str) else ("" if value is None else str(value))


# ---------------------------------------------------------------- 工具实现

def _tool_search(args: dict) -> dict:
    return kb.search(
        _as_str(args.get("query")),
        tax=_as_str(args.get("tax")),
        region=_as_str(args.get("region")),
        effect=_as_str(args.get("effect")),
        year=_as_str(args.get("year")),
        column=_as_str(args.get("column")),
        sort=_as_str(args.get("sort")) or "relevance",
        limit=_as_int(args.get("limit"), kb.DEFAULT_LIMIT),
        offset=_as_int(args.get("offset"), 0),
    )


def _tool_get_policy(args: dict) -> dict:
    doc_uid = _as_str(args.get("doc_uid")).strip()
    if not doc_uid:
        return {"error": "缺少 doc_uid 参数。可先用 search_policies 或 lookup_by_doc_no 拿到。"}
    result = kb.get_policy(
        doc_uid,
        content_offset=_as_int(args.get("content_offset"), 0),
        max_chars=_as_int(args.get("max_chars"), kb.DEFAULT_CONTENT_CHARS),
    )
    if result is None:
        return {"error": f"库里没有 doc_uid={doc_uid} 的政策。请确认它来自检索结果。"}
    return result


def _tool_lookup(args: dict) -> dict:
    doc_no = _as_str(args.get("doc_no")).strip()
    if not doc_no:
        return {"error": "缺少 doc_no 参数。"}
    return kb.lookup_by_doc_no(doc_no, limit=_as_int(args.get("limit"), 10))


def _tool_overview(args: dict) -> dict:  # noqa: ARG001 - 无参数
    return kb.overview()


_TOOL_FUNCS = {
    "search_policies": _tool_search,
    "get_policy": _tool_get_policy,
    "lookup_by_doc_no": _tool_lookup,
    "kb_overview": _tool_overview,
}


# ---------------------------------------------------------------- 协议处理

def _dump(payload) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=1)


def _tool_text(text: str, *, is_error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _tools_call(params: dict) -> dict:
    name = _as_str(params.get("name"))
    args = params.get("arguments")
    if not isinstance(args, dict):
        args = {}
    func = _TOOL_FUNCS.get(name)
    if func is None:
        return _tool_text(f"未知工具：{name!r}。可用工具：{', '.join(_TOOL_FUNCS)}", is_error=True)
    try:
        payload = func(args)
    except kb.KBError as e:
        # 知识库自身的问题（库不存在/未初始化）：这是使用者需要看到的信息
        return _tool_text(f"知识库不可用：{e}", is_error=True)
    except Exception as e:  # noqa: BLE001 - 任何异常都不能让服务退出
        log.exception("工具 %s 执行失败", name)
        return _tool_text(f"工具执行失败（{type(e).__name__}）。"
                          "可换一种查询方式或稍后重试。", is_error=True)
    # 工具返回体里的 error 字段（参数缺失、检索失败、文号为空…）一律视为工具错误。
    # 不这么做的话，AI 会收到 isError=false 且内容形如 {"error": "缺少 doc_uid"}，
    # 它会把这当成一次"成功的空结果"继续往下编。成功结果里 error 恒为 None。
    is_error = isinstance(payload, dict) and bool(payload.get("error"))
    return _tool_text(_dump(payload), is_error=is_error)


def _initialize(params: dict) -> dict:
    client_version = _as_str(params.get("protocolVersion"))
    version = client_version if client_version in SUPPORTED_PROTOCOLS else LATEST_PROTOCOL
    return {
        "protocolVersion": version,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        "instructions": INSTRUCTIONS,
    }


def _error(msg_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def handle_message(msg: dict) -> dict | None:
    """处理一条 JSON-RPC 消息。返回 None 表示这是通知，不需要回复。"""
    if not isinstance(msg, dict):
        return None
    method = _as_str(msg.get("method"))
    params = msg.get("params")
    if not isinstance(params, dict):
        params = {}
    if "id" not in msg:
        # 通知（notifications/initialized、cancelled 等）一律不回复 ——
        # 给通知回消息会让客户端把回复当成无对应请求的孤儿响应。
        return None
    msg_id = msg.get("id")

    try:
        if method == "initialize":
            result = _initialize(params)
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            result = _tools_call(params)
        elif method == "ping":
            result = {}
        elif method == "resources/list":
            result = {"resources": []}      # 未声明该能力，但别让客户端报错
        elif method == "prompts/list":
            result = {"prompts": []}
        else:
            return _error(msg_id, -32601, f"未实现的方法：{method}")
    except Exception as e:  # noqa: BLE001
        log.exception("处理 %s 时出错", method)
        return _error(msg_id, -32603, f"服务内部错误：{type(e).__name__}")
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def serve(stdin=None, stdout=None) -> int:
    """stdio 主循环：逐行读 JSON-RPC，处理，写回一行 JSON。

    直接操作字节流（不经文本层）：文本层会按系统 locale 猜编码，Windows 上
    猜成 GBK 就会在输出中文正文时崩掉。响应按行 flush —— 攒着不发的表现是
    「客户端一直转圈」。
    """
    inp = stdin if stdin is not None else sys.stdin.buffer
    out = stdout if stdout is not None else sys.stdout.buffer
    while True:
        raw = inp.readline()
        if not raw:
            break                      # 客户端关掉了管道，正常退出
        raw = raw.strip()
        if not raw:
            continue                   # 空行不是合法的 JSON-RPC 消息，跳过
        try:
            msg = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            # 没有 id 就无法构造错误响应，只能记日志（去 stderr）
            log.warning("收到无法解析的消息：%s", e)
            continue
        response = handle_message(msg)
        if response is None:
            continue
        out.write((json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8"))
        out.flush()
    return 0


def main() -> int:
    logging.basicConfig(
        level=logging.WARNING,
        stream=sys.stderr,             # 绝不能是 stdout
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log.info("%s v%s 启动（stdio）", SERVER_NAME, SERVER_VERSION)
    try:
        return serve()
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
