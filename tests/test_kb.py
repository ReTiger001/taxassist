"""AI 调用层测试：检索内核（kb）、MCP 协议、HTTP 接口。

============================================================
这组测试要锁住的不变量
============================================================

1. **只读。** AI 层绝不能写库 —— 一次对话几十次调用，任何一次写都可能与
   采集/翻译的长任务抢写锁。测试直接尝试写入，要求被拒。
2. **可溯源。** 每条检索结果必须带 url 与文号；效力结论必须带来源。
   这是 README 第一条边界，不允许因为「AI 用不到」就省掉。
3. **不因输入而崩。** AI 会传错参数（数字写成字符串、字段名写错、空字符串）。
   任何输入都只能得到「结果」或「错误结果」，不能是异常或进程退出。
4. **协议正确。** 通知不回复、未知方法回 -32601、工具失败走 isError 而不是
   JSON-RPC error。这些错了的表现是「客户端挂上去没反应」，极难排查。

所有用例都用临时库（tmp_path），不碰 data/taxassist.db 真库。
"""
from __future__ import annotations

import io
import json

import pytest

from taxassist import db as dbmod
from taxassist import kb, mcp_server

# ---------------------------------------------------------------- 夹具


def _add_policy(conn, uid: str, title: str, **fields) -> None:
    data = {
        "doc_uid": uid,
        "title": title,
        "first_seen_at": "2026-01-01T00:00:00+08:00",
        "last_seen_at": "2026-01-01T00:00:00+08:00",
        "p_effect_status": "现行有效",
        "p_effect_source": "official",
        "p_review_state": "auto",
    }
    data.update(fields)
    cols = ", ".join(data)
    marks = ", ".join("?" * len(data))
    conn.execute(f"INSERT INTO policy ({cols}) VALUES ({marks})", tuple(data.values()))


@pytest.fixture()
def kb_db(tmp_path):
    """带样例数据的临时库，返回库文件路径。"""
    path = tmp_path / "kb.db"
    conn = dbmod.connect(path)
    dbmod.init_db(conn)

    _add_policy(
        conn, "uid-1", "财政部 税务总局关于研发费用加计扣除政策的公告",
        p_doc_no_full="财政部 税务总局公告2026年第1号", cwrq="2026-03-01",
        pub_date="2026-03-02", pub_name="财政部 税务总局", o_column="政策法规",
        p_region="全国", url="https://example.test/1.html",
        content="企业开展研发活动中实际发生的研发费用，未形成无形资产计入当期损益的，"
                "在按规定据实扣除的基础上，再按照实际发生额的100%在税前加计扣除。",
        o_keywords="研发费用 加计扣除")
    _add_policy(
        conn, "uid-2", "国家税务总局关于契税征收管理有关事项的公告",
        p_doc_no_full="国家税务总局公告2025年第9号", cwrq="2025-06-15",
        pub_name="国家税务总局", o_column="政策法规", p_region="全国",
        url="https://example.test/2.html",
        content="契税的纳税义务发生时间，为纳税人签订土地、房屋权属转移合同的当日。",
        p_effect_status="已废止", p_effect_source="inferred",
        p_effect_evidence="自本文施行之日起，原《契税征收管理公告》同时废止。",
        p_review_state="needs_review")
    _add_policy(
        conn, "uid-3", "广东省税务局关于增值税小规模纳税人减免政策的指引",
        p_doc_no_full="粤税发〔2026〕12号", cwrq="2026-01-20",
        pub_name="国家税务总局广东省税务局", o_column="政策指引", p_region="广东",
        url="https://example.test/3.html",
        content="增值税小规模纳税人月销售额10万元以下的，免征增值税。")
    conn.commit()
    conn.close()
    return path


def _run_mcp(messages: list[dict]) -> list[dict]:
    """把消息逐行喂给 MCP 服务，收集它的每一行回复。"""
    payload = "".join(json.dumps(m, ensure_ascii=False) + "\n" for m in messages)
    out = io.BytesIO()
    rc = mcp_server.serve(stdin=io.BytesIO(payload.encode("utf-8")), stdout=out)
    assert rc == 0, "serve() 必须正常返回，不能在客户端关流时抛异常"
    return [json.loads(line) for line in out.getvalue().decode("utf-8").splitlines() if line.strip()]


# ---------------------------------------------------------------- 检索内核

def test_search_finds_by_title(kb_db):
    res = kb.search("研发费用加计扣除", path=kb_db)
    assert res["error"] is None
    assert res["total_matched"] == 1


def test_search_ranks_title_match_above_content_match(tmp_path):
    """标题命中的必须排在「正文里顺带提一句」的前面。

    这条有来历（2026-10-06 实测）：搜「关税」命中 2348 条 —— 正文提到一句
    也算命中 —— 而当时的默认排序是「实质政策优先 + 日期倒序」，于是前三条
    的标题里**一个「关税」都没有**，是《广东省增值税申报试点公告》《疾病
    控制机构税收优惠政策》这类文件。命中两千多条时，不给相关性等于没排序。

    这里特意让**正文命中的那条日期更新**：旧排序会把它排前面，新排序不该。
    """
    path = tmp_path / "rank.db"
    conn = dbmod.connect(path)
    dbmod.init_db(conn)
    _add_policy(conn, "content-hit", "关于增值税申报试点的公告",
                content="本公告自发布之日起施行，涉及进口关税的适用问题。",
                cwrq="2026-09-30")
    _add_policy(conn, "title-hit", "国务院关税税则委员会关于调整关税税率的通知",
                content="现就有关事项通知如下。", cwrq="2001-01-01")
    conn.commit()
    conn.close()

    res = kb.search("关税", path=path, limit=5)
    assert res["error"] is None, res["error"]
    assert res["total_matched"] == 2
    assert res["mode"] == "like", "2 字词应走 LIKE 兜底（trigram 对 <3 字符返回空）"
    titles = [h["title"] for h in res["hits"]]
    assert titles[0].startswith("国务院关税税则委员会"), (
        f"标题命中的没排到前面，实际顺序：{titles}")


def test_search_ranks_by_bm25_in_fts_mode(tmp_path):
    """FTS 模式（>=3 字）下按 bm25 排序，标题权重压过正文。

    同样让日期顺序与相关性相反，确保测的是排序而不是日期。
    """
    path = tmp_path / "rank_fts.db"
    conn = dbmod.connect(path)
    dbmod.init_db(conn)
    _add_policy(conn, "content-only", "关于企业所得税汇算清缴有关事项的公告",
                content="现将企业所得税汇算清缴有关事项公告如下，本文不涉及增值税。",
                cwrq="2026-09-30")
    _add_policy(conn, "title-hit", "财政部 税务总局关于增值税小规模纳税人的公告",
                content="现就增值税政策公告如下。", cwrq="2019-01-01")
    conn.commit()
    conn.close()

    res = kb.search("增值税", path=path, limit=5)
    assert res["error"] is None, res["error"]
    assert res["mode"] == "fts"
    titles = [h["title"] for h in res["hits"]]
    assert titles[0].startswith("财政部 税务总局关于增值税"), (
        f"FTS 模式下标题命中的没排前面，实际顺序：{titles}")


def test_search_results_always_carry_source(kb_db):
    """可溯源：每条结果都要有 url；效力判定要有来源。"""
    for hit in kb.search("", path=kb_db, limit=50)["hits"]:
        assert hit["url"], "检索结果缺少 url —— 违反「每条结论可溯源」"
        assert hit["effect_status"]
        assert hit["effect_source"]


def test_search_short_term_falls_back_to_like(kb_db):
    """2 字词走 FTS 会静默返回 0 条，必须退回 LIKE。"""
    res = kb.search("契税", path=kb_db)
    assert res["mode"] == "like"
    assert res["total_matched"] == 1
    assert res["hits"][0]["doc_uid"] == "uid-2"


def test_search_multi_terms_are_and_not_phrase(kb_db):
    """多词是 AND 而不是短语：「研发费用 加计扣除」应能命中，而不是要求连读。"""
    res = kb.search("研发费用 加计扣除", path=kb_db)
    assert res["error"] is None
    assert res["mode"] == "fts"
    assert res["total_matched"] == 1
    assert res["hits"][0]["doc_uid"] == "uid-1"


def test_search_empty_query_with_filters_only(kb_db):
    """空关键词 + 筛选是真实用法（「只看广东的」），不能返回 0 条。"""
    res = kb.search("", region="广东", path=kb_db)
    assert res["error"] is None
    assert res["total_matched"] == 1
    assert res["hits"][0]["region"] == "广东"


def test_search_filter_by_effect_and_tax(kb_db):
    res = kb.search("", effect="已废止", path=kb_db)
    assert [h["doc_uid"] for h in res["hits"]] == ["uid-2"]

    res = kb.search("", tax="增值税", path=kb_db)
    assert [h["doc_uid"] for h in res["hits"]] == ["uid-3"]


def test_search_limit_is_capped(kb_db):
    """AI 会传 limit=1000。夹到上限而不是照做（否则一次调用挤爆它的上下文）。"""
    res = kb.search("", limit=1000, path=kb_db)
    assert res["limit"] == kb.MAX_LIMIT


def test_search_no_hit_gives_guidance(kb_db):
    res = kb.search("完全不存在的词条", path=kb_db)
    assert res["total_matched"] == 0
    assert res["hint"], "没有命中时必须给出下一步建议，否则 AI 容易直接答「没有规定」"


def test_search_hostile_input_never_raises(kb_db):
    """恶意/畸形输入只能得到「检索失败」，不能是异常。"""
    for bad in ['" OR 1=1 --', "a" * 500, "%", "*", "（）", "a b c", "'"]:
        res = kb.search(bad, path=kb_db)
        assert isinstance(res, dict)
        assert res["error"] is None or isinstance(res["error"], str)


def test_search_result_snippet_contains_hit(kb_db):
    res = kb.search("加计扣除", path=kb_db)
    snippet = res["hits"][0]["snippet"]
    assert "加计扣除" in snippet


def test_search_pagination_reports_more(kb_db):
    res = kb.search("", limit=1, path=kb_db)
    assert res["returned"] == 1
    assert res["total_matched"] == 3
    assert res["has_more"] is True


def test_kb_layer_is_readonly(kb_db):
    """AI 层必须只读：任何写入都要被 SQLite 在文件层拒绝。"""
    conn = kb.connect_readonly(kb_db)
    try:
        with pytest.raises(Exception):
            conn.execute("CREATE TABLE _probe (x)")
        with pytest.raises(Exception):
            conn.execute("INSERT INTO policy (doc_uid, title) VALUES ('x', 'y')")
    finally:
        conn.close()


def test_missing_db_raises_friendly_error(tmp_path):
    with pytest.raises(kb.KBError) as exc:
        kb.search("x", path=tmp_path / "不存在.db")
    assert "initdb" in str(exc.value), "报错要告诉使用者怎么修，而不是只抛文件不存在"


# ---------------------------------------------------------------- 文号查找

def test_lookup_by_doc_no_exact(kb_db):
    res = kb.lookup_by_doc_no("国家税务总局公告2025年第9号", path=kb_db)
    assert res["found_in_library"] is True
    assert res["matches"][0]["doc_uid"] == "uid-2"
    assert "已废止" in res["effect_summary"]


def test_lookup_by_doc_no_normalizes_brackets_and_spaces(kb_db):
    """「粤税发〔2026〕12号」要能用半角括号、带空格的写法查到。"""
    for variant in ("粤税发[2026]12号", "粤税发〔2026〕12 号", "粤税发（2026）12号"):
        res = kb.lookup_by_doc_no(variant, path=kb_db)
        assert res["found_in_library"] is True, f"变体 {variant} 没查到"
        assert res["matches"][0]["doc_uid"] == "uid-3"


def test_lookup_by_doc_no_reports_dangling_reference(kb_db, tmp_path):
    """被引用但没入库的文号也要能查到引用来源 —— 直接说「没有」是错的。"""
    path = tmp_path / "rel.db"
    conn = dbmod.connect(path)
    dbmod.init_db(conn)
    _add_policy(conn, "uid-a", "关于废止部分文件的公告", cwrq="2026-02-01",
                url="https://example.test/a.html")
    conn.execute(
        "INSERT INTO policy_relation (src_doc_uid, dst_doc_no, relation, evidence, created_at)"
        " VALUES ('uid-a', '财税〔2019〕99号', 'repeals', '附件所列文件同时废止', '2026-02-01')")
    conn.commit()
    conn.close()

    res = kb.lookup_by_doc_no("财税[2019]99号", path=path)
    assert res["matches"] == []
    assert len(res["dangling_references"]) == 1
    assert res["dangling_references"][0]["src_title"] == "关于废止部分文件的公告"


def test_lookup_empty_doc_no_is_reported(kb_db):
    assert kb.lookup_by_doc_no("", path=kb_db)["error"]


# ---------------------------------------------------------------- 详情

def test_get_policy_returns_evidence_and_relations(kb_db):
    p = kb.get_policy("uid-2", path=kb_db)
    assert p["title"].startswith("国家税务总局关于契税")
    assert p["effect"]["status"] == "已废止"
    assert p["effect"]["source"] == "inferred"
    assert p["effect"]["evidence"], "效力结论必须附证据片段"
    assert p["effect"]["needs_review"] is True
    assert p["url"]


def test_get_policy_truncates_and_paginates(kb_db, tmp_path):
    """长正文分段读取：既不能一次全给，也不能给不全还说自己完整。"""
    path = tmp_path / "long.db"
    conn = dbmod.connect(path)
    dbmod.init_db(conn)
    _add_policy(conn, "uid-long", "超长文件", url="https://example.test/long.html",
                content="甲" * 5000)
    conn.commit()
    conn.close()

    first = kb.get_policy("uid-long", max_chars=1000, path=path)
    assert len(first["content"]) == 1000
    assert first["content_length"] == 5000
    assert first["content_truncated"] is True
    assert first["next_offset"] == 1000

    second = kb.get_policy("uid-long", content_offset=1000, max_chars=1000, path=path)
    assert second["content"] == "甲" * 1000
    assert second["content_offset"] == 1000

    last = kb.get_policy("uid-long", content_offset=5000, max_chars=1000, path=path)
    assert last["content"] == ""
    assert last["content_truncated"] is False
    assert last["next_offset"] is None


def test_get_policy_unknown_uid_returns_none(kb_db):
    assert kb.get_policy("no-such-uid", path=kb_db) is None


# ---------------------------------------------------------------- 概况

def test_overview_reports_freshness_and_dimensions(kb_db):
    ov = kb.overview(path=kb_db)
    assert ov["total_policies"] == 3
    assert "现行有效" in ov["by_effect_status"]
    assert ov["tax_types"], "要给出可用税种，否则 AI 无法自己收窄检索"
    assert ov["regions"] == {"全国": 2, "广东": 1}
    assert "last_successful_fetch" in ov["data_freshness"]
    assert ov["needs_manual_review"] >= 1
    assert ov["effect_source_legend"]["inferred"]


# ---------------------------------------------------------------- MCP 协议

def test_mcp_initialize_echoes_client_protocol():
    """客户端只认旧版协议时，回它发的那个版本 —— 回新版会被直接断开。"""
    [resp] = _run_mcp([{"jsonrpc": "2.0", "id": 1, "method": "initialize",
                        "params": {"protocolVersion": "2024-11-05",
                                   "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}])
    assert resp["result"]["protocolVersion"] == "2024-11-05"
    assert resp["result"]["serverInfo"]["name"] == mcp_server.SERVER_NAME
    assert "tools" in resp["result"]["capabilities"]
    assert resp["result"]["instructions"], "引用规范必须随 initialize 下发"


def test_mcp_initialize_unknown_protocol_falls_back_to_latest():
    [resp] = _run_mcp([{"jsonrpc": "2.0", "id": 1, "method": "initialize",
                        "params": {"protocolVersion": "1999-01-01"}}])
    assert resp["result"]["protocolVersion"] == mcp_server.LATEST_PROTOCOL


def test_mcp_notification_gets_no_reply():
    """通知（无 id）绝不能有回复：孤儿响应会让客户端错位解析。"""
    responses = _run_mcp([
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1}},
    ])
    assert len(responses) == 1
    assert responses[0]["id"] == 1


def test_mcp_tools_list_shape():
    [resp] = _run_mcp([{"jsonrpc": "2.0", "id": 2, "method": "tools/list"}])
    tools = resp["result"]["tools"]
    names = {t["name"] for t in tools}
    assert names == {"search_policies", "get_policy", "lookup_by_doc_no", "kb_overview"}
    for t in tools:
        assert t["description"], f"{t['name']} 缺少 description —— AI 靠它决定何时调用"
        assert t["inputSchema"]["type"] == "object"


def test_mcp_unknown_method_returns_method_not_found():
    [resp] = _run_mcp([{"jsonrpc": "2.0", "id": 3, "method": "resources/subscribe"}])
    assert resp["error"]["code"] == -32601


def test_mcp_unknown_tool_is_tool_error_not_protocol_error():
    [resp] = _run_mcp([{"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                        "params": {"name": "drop_database", "arguments": {}}}])
    assert "error" not in resp, "工具级问题不能升级成 JSON-RPC 错误"
    assert resp["result"]["isError"] is True


def test_mcp_malformed_json_does_not_kill_service():
    """一行坏 JSON 不能让服务停摆：后面的正常请求仍要被处理。"""
    payload = (b'{"jsonrpc":"2.0","id":1,"method":"ping"}\n'
               b'this is not json\n'
               b'\n'
               b'{"jsonrpc":"2.0","id":2,"method":"ping"}\n')
    out = io.BytesIO()
    rc = mcp_server.serve(stdin=io.BytesIO(payload), stdout=out)
    assert rc == 0
    lines = [json.loads(l) for l in out.getvalue().decode("utf-8").splitlines() if l.strip()]
    assert [l["id"] for l in lines] == [1, 2]


def test_mcp_ping():
    [resp] = _run_mcp([{"jsonrpc": "2.0", "id": 5, "method": "ping"}])
    assert resp["result"] == {}


def test_mcp_tool_arguments_are_coerced():
    """AI 把数字写成字符串、把参数写成 null 是常态，不能被这些卡死。"""
    assert mcp_server._as_int("10", 3) == 10
    assert mcp_server._as_int("abc", 3) == 3
    assert mcp_server._as_int(None, 3) == 3
    assert mcp_server._as_int(2.9, 3) == 2
    assert mcp_server._as_str(None) == ""
    assert mcp_server._as_str(5) == "5"


def test_mcp_search_tool_reports_missing_db(monkeypatch, tmp_path):
    """库不存在时，AI 要拿到可读的说明，而不是进程崩掉。"""
    monkeypatch.setattr(kb, "DB_PATH", tmp_path / "缺失.db")
    result = mcp_server._tools_call({"name": "search_policies", "arguments": {"query": "x"}})
    assert result["isError"] is True
    assert "知识库不可用" in result["content"][0]["text"]


def test_mcp_get_policy_without_uid_is_tool_error():
    result = mcp_server._tools_call({"name": "get_policy", "arguments": {}})
    assert result["isError"] is True


def test_mcp_responses_are_utf8_on_wire():
    """中文必须按 UTF-8 上线：Windows 默认 GBK，一个编不出的字符就能让服务死掉。"""
    out = io.BytesIO()
    msg = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, ensure_ascii=False)
    mcp_server.serve(stdin=io.BytesIO((msg + "\n").encode("utf-8")), stdout=out)
    raw = out.getvalue()
    text = raw.decode("utf-8")     # 解不出就会抛
    assert "政策" in text          # 中文原样写出，没有被转成 \uXXXX


# ---------------------------------------------------------------- HTTP 接口

@pytest.fixture()
def api_client(kb_db, monkeypatch):
    """指向临时库的 HTTP 客户端。端点在内部走默认库路径，所以要把
    kb.DB_PATH 改到临时库 —— 否则测的就是真库。"""
    from fastapi.testclient import TestClient

    from taxassist import kb_api

    monkeypatch.setattr(kb, "DB_PATH", kb_db)
    with TestClient(kb_api.create_app()) as client:
        yield client


def test_api_index_lists_endpoints(api_client):
    body = api_client.get("/").json()
    assert any("/api/search" in key for key in body["endpoints"])
    assert "只读" in body["note"]


def test_api_health(api_client):
    body = api_client.get("/health").json()
    assert body["status"] == "ok"
    assert body["total_policies"] == 3


def test_api_search_returns_same_shape_as_kb(api_client):
    body = api_client.get("/api/search", params={"q": "研发费用加计扣除"}).json()
    assert body["total_matched"] == 1
    assert body["hits"][0]["url"], "HTTP 层同样不能丢溯源信息"
    assert body["error"] is None


def test_api_search_filters(api_client):
    body = api_client.get("/api/search", params={"region": "广东"}).json()
    assert [h["doc_uid"] for h in body["hits"]] == ["uid-3"]


def test_api_policy_detail_and_404(api_client):
    body = api_client.get("/api/policy/uid-2").json()
    assert body["effect"]["evidence"]
    assert body["effect"]["source"] == "inferred"

    missing = api_client.get("/api/policy/不存在")
    assert missing.status_code == 404
    assert "error" in missing.json()


def test_api_docno_lookup(api_client):
    body = api_client.get("/api/docno/粤税发[2026]12号").json()
    assert body["found_in_library"] is True
    assert body["matches"][0]["doc_uid"] == "uid-3"


def test_api_overview(api_client):
    body = api_client.get("/api/overview").json()
    assert body["total_policies"] == 3
    assert body["data_freshness"]


def test_api_rejects_wrong_param_type(api_client):
    """limit 传非数字：HTTP 层给 422（标准），而不是静默当成默认值。"""
    assert api_client.get("/api/search", params={"limit": "abc"}).status_code == 422


def test_api_returns_503_when_db_missing(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from taxassist import kb_api

    monkeypatch.setattr(kb, "DB_PATH", tmp_path / "缺失.db")
    with TestClient(kb_api.create_app()) as client:
        resp = client.get("/api/search", params={"q": "x"})
        assert resp.status_code == 503
        assert "initdb" in resp.json()["error"]


def test_pick_port_skips_busy_port():
    """默认端口被占时要自动往后找。

    这条是有来历的：本机 8765 / 8766 / 8771 上跑着别的 taxassist 服务，
    而本接口最初把默认端口定成了 8766 —— 一启动就 address already in use，
    使用者看到的是「双击 bat 就报错」。
    """
    import socket

    from taxassist import kb_api

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen(1)
        taken = busy.getsockname()[1]

        picked = kb_api._pick_port("127.0.0.1", taken)
        assert picked is not None
        assert picked != taken, "占用中的端口被返回了 —— 启动仍会撞车"

        # 返回的端口必须真能绑上（可能被别的进程抢走，所以再验一次）
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", picked))
