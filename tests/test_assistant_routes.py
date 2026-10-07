"""助手接口（/api/assistant/*）—— 此前零测试。

最要紧的是**错误链路**：2026-10 全量审计发现 `web/assistant_routes.py` 是全项目
唯一漏了 logger 定义的模块，而 except 块里调用 `log.warning` —— 异常发生时
`NameError` 顶掉了紧随其后的 `emit({"error"})`，前端只收到 finally 里的 `done`。
结果是助手失败**静默显示成"完成"**：使用者以为分析过了，其实什么都没拿到。

这种缺陷不会让任何功能测试变红（正常路径完全不受影响），只能靠专门钉住
错误路径的测试发现。本文件就是那一组。
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from taxassist import assistant as am
from taxassist import db as dbmod
from taxassist.web.app import create_app


@pytest.fixture()
def db_path(tmp_path, monkeypatch):
    """把整个应用指向临时库 —— 测试绝不碰真实数据。

    （test_auth_web.py 里也有一份同样的；那份是模块局部 fixture，跨文件用不了，
    所以这里自带一个。）
    """
    monkeypatch.setattr(dbmod, "DB_PATH", tmp_path / "assistant.db")
    conn = dbmod.connect()
    dbmod.init_db(conn)
    conn.close()
    return tmp_path / "assistant.db"


@pytest.fixture()
def client(db_path, monkeypatch):
    """免认证客户端。

    origin 校验单独放行：那不是本文件关注点（已有专门测试），而这里要验的是
    错误能不能传到前端。
    """
    # patch 的是 **app 模块**上的 _origin_ok：assistant_routes 现在是函数内
    # `from .app import _origin_ok`，即调用时才取属性，所以改 app 上那份有效；
    # assistant_routes 模块上已经没有这个名字了（不再是模块级导入）。
    from taxassist.web import app as web_app
    monkeypatch.setattr(web_app, "_origin_ok", lambda request: True)
    return TestClient(create_app(require_auth=False))


def _sse_events(resp) -> list[dict]:
    """把 SSE 响应体拆成事件列表（每条 ``data:`` 一个 JSON）。"""
    out: list[dict] = []
    for line in resp.text.splitlines():
        if line.startswith("data: "):
            try:
                out.append(json.loads(line[6:]))
            except json.JSONDecodeError:
                pass
    return out


def test_assistant_error_reaches_the_client(client, monkeypatch):
    """内部分析失败时，SSE 里**必须有 error** —— 不能只发 done。

    回归（上面文件头描述的那个 P0）。mock 只做一件事：让流程第一步就抛异常，
    这样 except 块被命中。若 logger 再次缺失，error 事件会被 NameError 吞掉，
    本条测试立刻失败。
    """
    monkeypatch.setattr(am, "is_available", lambda *a, **k: (True, ""))

    def boom(_text):
        raise RuntimeError("模拟分析失败")

    monkeypatch.setattr(am, "extract_facts", boom)

    r = client.post("/api/assistant/ask", json={"text": "测试一下"})
    assert r.status_code == 200, "SSE 流本身应当正常建立"

    events = _sse_events(r)
    errors = [e for e in events if "error" in e]
    assert errors, f"没有 error 事件，前端会把失败显示成完成。实际事件：{events}"
    assert "模拟分析失败" in errors[0]["error"]
    assert any(e.get("done") for e in events), "done 仍应照发，否则前端不会收尾"


def test_assistant_streams_normal_stages(client, monkeypatch):
    """正常路径要把阶段与正文发出去 —— 防的是"只发 done"这类退化。"""
    monkeypatch.setattr(am, "is_available", lambda *a, **k: (True, ""))
    monkeypatch.setattr(am, "extract_facts", lambda _t: [{"fact": "某事实"}])
    monkeypatch.setattr(am, "gather_policies", lambda _f: {"0": []})
    monkeypatch.setattr(
        am, "build_prompt",
        lambda *a, **k: ([{"role": "user", "content": "x"}], [], {0: []}))
    monkeypatch.setattr(am, "chat_stream", lambda _m: iter(["第一段", "第二段"]))

    r = client.post("/api/assistant/ask", json={"text": "测试"})
    assert r.status_code == 200
    events = _sse_events(r)
    assert any("stage" in e for e in events), "阶段提示缺失，用户会对着空白等"
    deltas = "".join(e["delta"] for e in events if "delta" in e)
    assert deltas == "第一段第二段", f"正文增量不完整：{deltas!r}"
    assert any(e.get("done") for e in events)


def test_assistant_rejects_empty_input(client):
    """既没文字也没文件：明确报错，而不是空跑一次分析。"""
    r = client.post("/api/assistant/ask", json={"text": "", "file_ids": []})
    assert r.status_code == 400


def test_assistant_reports_model_unavailable_early(client, monkeypatch):
    """模型没起来要**立刻**回 503，而不是让前端等一分半才失败。"""
    monkeypatch.setattr(am, "is_available", lambda *a, **k: (False, "模型当前不可用，先启动 ollama"))
    r = client.post("/api/assistant/ask", json={"text": "测试"})
    assert r.status_code == 503
    assert "模型" in r.text
