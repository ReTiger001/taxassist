"""健康自检与税务助手的路由（从 web/app.py 拆出）。

拆出的理由见 web/app.py 末尾的说明：这两块是最新、最独立的一组，只依赖
模块级的辅助函数与注入进来的 ``templates``，不碰 auth / admin / 检索那些
与登录态、权限、FTS 分支耦合紧密的老逻辑。

**依赖用参数注入**（而不是把 app.py 的闭包提到模块级）：

    register(app, *, ctx, templates)

``ctx`` 是 create_app 里那个依赖 ``require_auth`` 的闭包 —— 传进来比提到
模块级更显式，也让"这个路由需要哪些上下文"一目了然。
"""
from __future__ import annotations

import logging
import re
import uuid
from pathlib import Path

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from .. import db as dbmod
from .app import _origin_ok, _read_json

#: 这个模块此前**漏了 logger 定义**，而 except 块里调用了 log.warning ——
#: 后果不是"少一条日志"，而是异常发生时 NameError 顶掉后面的 emit({"error"})，
#: 前端拿不到错误、只收到 finally 里的 done，**助手失败会静默显示成"完成"**。
#: （2026-10 全量审计发现；全项目只有这一个模块漏了。）
log = logging.getLogger(__name__)


def register(app, *, ctx, templates) -> None:
    """把 /health 与 /assistant 系列挂到 app 上。"""

    # ------------------------------------------------------------ 健康自检

    @app.get("/health")
    def health_check(request: Request):
        """系统自检：一眼看出"哪里不对"。

        **为什么放 PUBLIC_PATHS**：它的用途就是"还没登录也能查系统活着没"
        （监控探活、隧道排障）。返回的全是运行状态 —— 条数、时间戳、PID、
        模型可用性 —— **不含任何客户数据或政策正文**。

        **为什么需要它**：这个系统出故障的表现是"默默不动"（worker 卡死、
        写锁没释放、ollama 没起、调度没跑），而不是抛错。有了这个端点，
        排障从"翻三个日志文件"变成"看一个 JSON"。每一项独立 try ——
        某个子系统坏了不能导致整个自检返回 500，那样最需要它的时候它反而
        不可用。
        """
        import datetime as _dt

        from .. import writelock

        out: dict = {"now": _dt.datetime.now().isoformat(timespec="seconds")}

        try:                       # ① 库
            conn = dbmod.connect()
            try:
                n = conn.execute("SELECT COUNT(*) FROM policy").fetchone()[0]
                last = conn.execute(
                    "SELECT MAX(started_at) FROM fetch_log").fetchone()[0]
                out["db"] = {"policies": n, "last_fetch_at": last}
            finally:
                conn.close()
        except Exception as exc:   # noqa: BLE001
            out["db"] = {"error": f"{type(exc).__name__}: {exc}"[:120]}

        try:                       # ② 写锁（卡死时这里能看出来）
            held = writelock.holder()
            out["write_lock"] = {"held_by": held}
        except Exception as exc:   # noqa: BLE001
            out["write_lock"] = {"error": type(exc).__name__}

        try:                       # ③ worker 最近一轮
            from .. import worker as worker_mod
            st = worker_mod.read_status()
            out["worker"] = {"rounds": st.get("回合"),
                             "started": st.get("开始"),
                             "finished": st.get("结束"),
                             "stages": st.get("阶段"),
                             "note": st.get("说明")}
        except Exception as exc:   # noqa: BLE001
            out["worker"] = {"error": type(exc).__name__}

        try:                       # ④ 对话用的本机模型
            from .. import assistant as am
            ok, msg = am.is_available()
            out["ollama"] = {"ok": ok, "detail": msg}
        except Exception as exc:   # noqa: BLE001
            out["ollama"] = {"error": type(exc).__name__}

        out["ok"] = not any("error" in (out.get(k) or {})
                            for k in ("db", "write_lock", "worker"))
        return JSONResponse(out)

    # ------------------------------------------------------------ 税务助手

    @app.get("/", response_class=HTMLResponse)
    @app.get("/assistant", response_class=HTMLResponse)
    def assistant_page(request: Request):
        """对话窗口：上传文件或说想法 → 四节报告。

        对外模式下这个页面**必须登录后才能进**（它不在 PUBLIC_PATHS 里）：
        它处理的是客户资料，是这个系统里最敏感的东西。
        """
        from .. import assistant as am

        ok, msg = am.is_available()
        return templates.TemplateResponse(
            request=request, name="assistant.html",
            context=ctx(request, model_ok=ok, model_msg=msg,
                        model=am.DEFAULT_MODEL))

    @app.post("/api/assistant/ask")
    async def assistant_ask(request: Request):
        """税务助手问答，**SSE 流式**返回。

        为什么必须流式：实测一次问答 90-150 秒（14B 部分层跑在 CPU）。
        让用户对着空白等一分半，与"豆包式"体验差得太远 —— 流式让第一句
        在几秒内出现，用户能立刻看出"它理解对没有"。

        事件（每条 ``data:`` 一个 JSON）：
          ``{"stage": "…"}``                   进度文案
          ``{"facts": […], "policies": […]}``  依据（**前端据此渲染效力标签**）
          ``{"delta": "文字"}``                正文增量
          ``{"error": "…"}`` / ``{"done": true}``
        """
        import asyncio
        import json as _json
        import threading

        from .. import assistant as am

        if not _origin_ok(request):
            return JSONResponse({"error": "请求来源异常，请回本站重新提交"},
                                status_code=403)
        try:
            data = await request.json()
        except Exception:  # noqa: BLE001 - 非法 JSON 当空处理
            data = {}
        text = str(data.get("text") or "").strip()[:20000]
        file_ids = data.get("file_ids") or []
        # 多轮追问的历史，由前端维护并回传。这里只做类型保护，
        # **具体裁剪（留几轮、每轮多长）在 assistant.build_prompt 里做** ——
        # 那里最清楚上下文预算（NUM_CTX=8192，每轮的依据块就占几千字）。
        history = data.get("history") or []
        if not isinstance(history, list):
            history = []
        if not text and not file_ids:
            return JSONResponse({"error": "请输入要分析的内容或上传文件"},
                                status_code=400)
        # 模型没起来就早说清楚，别让前端等一分半才报错
        ok, msg = am.is_available()
        if not ok:
            return JSONResponse({"error": msg}, status_code=503)

        # 上传的文件：把解析好的文本拼进待分析内容
        for fid in file_ids[:5]:
            got = _uploaded_text(request, str(fid))
            if got:
                text = f"{text}\n\n【上传文件：{got['name']}】\n{got['text']}" if text \
                    else f"【上传文件：{got['name']}】\n{got['text']}"

        loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue()

        def emit(obj: dict) -> None:
            loop.call_soon_threadsafe(q.put_nowait, obj)

        # 依据字段白名单：正文不传（几百 KB 没必要），前端只用这几项
        keep = ("doc_uid", "title", "doc_no", "effect_status", "effect_source",
                "region", "cwrq", "url")

        def work() -> None:
            """在独立线程里跑同步流程（ollama 客户端是同步的）。"""
            try:
                facts = am.extract_facts(text)
                emit({"stage": f"拆成 {len(facts)} 条业务事实"})
                by_fact = am.gather_policies(facts)
                emit({"stage": f"检索到 {sum(len(v) for v in by_fact.values())} 条相关政策"})
                messages, ordered, groups = am.build_prompt(
                    text, facts, by_fact, history=history)
                emit({"stage": "正在生成分析（首次调用需加载模型，约 1 分钟）"})
                # **先把依据发给前端**：用户能立刻看到"它查到了哪些政策"，
                # 即使后面生成慢，也不是干等。这也是可溯源的一部分。
                # groups 必须一起发：前端据此把依据按事实分组，**漏发会让
                # 依据卡片整块不渲染**（踩过：前端按分组找依据，拿不到分组
                # 就一条都不显示，用户以为没检索到）。
                emit({"facts": facts,
                      "policies": [{k: p.get(k) for k in keep} for p in ordered],
                      "groups": groups})
                for piece in am.chat_stream(messages):
                    emit({"delta": piece})
            except Exception as exc:  # noqa: BLE001 - 任何失败都要传回前端
                log.warning("助手问答失败：%s", exc)
                emit({"error": f"{type(exc).__name__}: {exc}"})
            finally:
                emit({"done": True})
                loop.call_soon_threadsafe(q.put_nowait, None)

        threading.Thread(target=work, daemon=True).start()

        async def stream():
            while True:
                item = await q.get()
                if item is None:
                    break
                yield f"data: {_json.dumps(item, ensure_ascii=False)}\n\n"

        # X-Accel-Buffering: 经反向代理时禁用缓冲，否则流式会被攒成一坨
        return StreamingResponse(
            stream(), media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    @app.post("/api/assistant/upload")
    async def assistant_upload(request: Request):
        """上传文件 → 解析出文本，返回 file_id。

        **走 JSON + base64，不用 multipart。** 项目原则是依赖越少越好
        （``_read_form`` 里写明"不引入 python-multipart"），而手写 multipart
        解析边界情况多（boundary、编码、多段）、容易出错；base64 只让体积涨
        1/3，对几 MB 的税务文档无影响。前端用 FileReader 读成 dataURL 即可。

        复用附件解析那套（``collect.attachments.parse_attachment``）：
        PDF / Excel / Word（含 WPS 转 .doc）/ CSV / TXT 都能读。
        **只解析、不入库**：这是用户给助手看的材料，不是政策，不该混进 policy 表。
        """
        import base64
        import binascii

        from ..collect import attachments as att

        if not _origin_ok(request):
            return JSONResponse({"error": "请求来源异常"}, status_code=403)

        body = await _read_json(request, max_bytes=60 * 1024 * 1024)
        name = att.safe_filename(str(body.get("name") or ""), "上传文件")
        b64 = str(body.get("data") or "")
        if not b64:
            return JSONResponse({"error": "没有收到文件"}, status_code=400)
        # 前端传的是 dataURL（data:application/pdf;base64,xxxx），取逗号后那段
        if b64.startswith("data:") and "," in b64[:100]:
            b64 = b64.split(",", 1)[1]
        try:
            raw = base64.b64decode(b64, validate=True)
        except (binascii.Error, ValueError):
            return JSONResponse({"error": "文件内容不是合法的 base64"},
                                status_code=400)

        tmpdir = _upload_dir()
        dest = tmpdir / f"{uuid.uuid4().hex}_{name}"
        dest.write_bytes(raw)
        try:
            content, status = att.parse_attachment(dest)
        finally:
            try:
                dest.unlink()      # 原文件不留档：解析完就删，只留文本
            except OSError:
                pass
        if not content:
            hint = {"no_text_layer": "这是扫描件（没有文字层），请提供电子版或复制文字",
                    "unsupported": "这个格式读不了，支持 PDF/Word/Excel/CSV/TXT",
                    }.get(status, f"解析失败（{status}）")
            return JSONResponse({"error": hint}, status_code=422)

        fid = uuid.uuid4().hex
        (tmpdir / f"{fid}.txt").write_text(content[:200000], encoding="utf-8")
        # 原名单独存：file_id 是随机串，但**报告里要显示用户认得的文件名**
        # （"股权转让说明.txt" 而不是 "075aa138…"）。
        (tmpdir / f"{fid}.name").write_text(name, encoding="utf-8")
        return JSONResponse({"file_id": fid, "name": name,
                             "chars": len(content),
                             "preview": content[:400]})

    def _upload_dir() -> Path:
        """上传文本的存放目录（data/uploads，随会话清理）。"""
        d = Path(dbmod.DB_PATH).parent / "uploads"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _uploaded_text(request: Request, fid: str) -> dict | None:
        """取回上传文件的解析文本。

        **只认 32 位十六进制 id**：这个 id 直接拼成文件名，若允许 ``..``
        或路径分隔符就是路径穿越漏洞（能读机器上任意 .txt）。
        文件名里的用户原名已由 ``safe_filename`` 清洗，这里再卡一道格式。
        """
        if not re.fullmatch(r"[0-9a-f]{32}", fid or ""):
            return None
        p = _upload_dir() / f"{fid}.txt"
        if not p.is_file():
            return None
        try:
            return {"text": p.read_text(encoding="utf-8"), "name": fid}
        except OSError:
            return None

