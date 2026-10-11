"""省级采集编排（pipeline.collect_provincial）的测试 —— 审计 P1：零端到端覆盖。

**为什么测它**：省级源有 80 个适配器、30 多个独立站点，全靠这一层装配。
而它出错的方式全是**安静的**（项目里真踩过其中两条，注释都还在）：

  · **一个源挂掉拖垮整轮** —— 省级站各自独立改版/超时，任一失败只该影响它自己
  · **同名即判重** —— 总局库里有 14 条「关于调整增值税纳税申报有关事项的公告」，
    是 2011–2026 年间逐年发布的**不同修订版本**。只按标题判重会把它们当一条，
    更糟的是省级站转载较新版本时会被当成重复**丢掉**
  · **硬编码 status="ok"** —— 于是"只抓了一半"与"抓全了"在 fetch_log 里长得
    一模一样，读的人以为今天就这么多

这里用替身把 `_fetch_source` 换掉（不碰网络、不碰浏览器），只测装配逻辑。
"""
from __future__ import annotations

import pytest

from taxassist import db as dbmod
from taxassist import pipeline, province


@pytest.fixture()
def conn(tmp_path):
    c = dbmod.connect(tmp_path / "t.db")
    dbmod.init_db(c)
    yield c
    c.close()


def _adapter(sid: str, region: str = "测试省") -> province.ListPageAdapter:
    return province.ListPageAdapter(
        source_id=sid,
        region=region,
        site_name=f"{region}税务局",
        list_url=f"http://example.test/{sid}/index.html",
        detail_href_re=r"/art/\d+/\d+/\d+/art_\d+_\d+\.html",
        base_url="http://example.test",
    )


def _item(sid: str, n: int, *, title: str, cwrq: str | None = "2020-01-01") -> dict:
    """造一条列表页条目（形状同 province.parse_list_page 的输出）。"""
    return {
        "url": f"http://example.test/{sid}/art/2020/1/{n}/art_1_{n}.html",
        "title": title,
        "cwrq": cwrq,
        "doc_uid": f"{sid}:{n:016d}",
    }


def _patch_sources(monkeypatch, adapters, results) -> None:
    """把适配器表与抓取函数都换成替身。

    ``results``: ``{source_id: (items, err, truncated)}``
    """
    monkeypatch.setattr(province, "ADAPTERS", tuple(adapters))

    def fake_fetch(adapter):
        return results[adapter.source_id]

    monkeypatch.setattr(pipeline, "_fetch_source", fake_fetch)


def test_one_source_failure_does_not_stop_the_others(conn, monkeypatch):
    """单源失败只影响它自己 —— 其它源照常入库，失败写进 fetch_log。

    省级站是 30 多个独立站点，任何一个改版/超时都只该影响它自己；把整轮
    拖垮意味着"今天一条都没抓"，而真相只是其中一个站有问题。
    """
    good, bad = _adapter("good_src"), _adapter("bad_src")
    _patch_sources(monkeypatch, [good, bad], {
        "good_src": ([_item("good_src", 1, title="关于甲事项的公告")], None, False),
        "bad_src": (None, "模拟：站点改版", False),
    })

    out = pipeline.collect_provincial(conn)

    by_id = {r["source_id"]: r for r in out}
    assert by_id["good_src"]["status"] == "ok"
    assert by_id["bad_src"]["status"] == "failed"
    assert by_id["bad_src"]["error"] == "模拟：站点改版"

    # 好源的数据确实入库了 —— 这才叫"没被拖垮"
    n = conn.execute("SELECT COUNT(*) FROM policy WHERE p_region = ?", ("测试省",)).fetchone()[0]
    assert n == 1

    # 失败也进了 fetch_log，不是只在返回值里
    row = conn.execute("SELECT status, error FROM fetch_log WHERE source_id = ?",
                       ("bad_src",)).fetchone()
    assert row["status"] == "failed"
    assert "站点改版" in (row["error"] or "")


def test_same_title_different_date_are_two_documents(conn, monkeypatch):
    """**同名不同成文日期 = 两份不同的文件**，都要留下。

    实测总局库里有 14 条「关于调整增值税纳税申报有关事项的公告」，是 2011–2026
    年间逐年发布的修订版本（日期、内容、效力各不相同）。只按标题判重会把它们
    合成一条；更糟的是省级站转载的若是较新版本，会被误判成重复而**丢弃**。
    """
    a = _adapter("ver_src")
    _patch_sources(monkeypatch, [a], {
        "ver_src": ([
            _item("ver_src", 1, title="关于调整增值税纳税申报有关事项的公告",
                  cwrq="2011-12-01"),
            _item("ver_src", 2, title="关于调整增值税纳税申报有关事项的公告",
                  cwrq="2026-02-01"),
        ], None, False),
    })

    out = pipeline.collect_provincial(conn)

    rows = conn.execute(
        "SELECT cwrq FROM policy WHERE title = ? ORDER BY cwrq",
        ("关于调整增值税纳税申报有关事项的公告",)).fetchall()
    assert [r["cwrq"] for r in rows] == ["2011-12-01", "2026-02-01"], \
        "逐年修订版被当成重复合并了 —— 省转载的新版本会因此丢失"
    assert out[0]["new"] == 2
    assert out[0]["skipped_duplicates"] == 0


def test_same_title_same_date_is_skipped_as_duplicate(conn, monkeypatch):
    """同名**且**同成文日期才是重复 —— 跨源转载的同一份文件只留一条。

    反面对照：如果这条也留下，库里会出现两条标题、日期完全相同的记录
    （实测广东站转载总局公告就是这个现象，会让人以为系统重复了）。
    """
    a = _adapter("dup_src")
    _patch_sources(monkeypatch, [a], {
        "dup_src": ([
            _item("dup_src", 1, title="关于乙事项的公告", cwrq="2020-06-01"),
            _item("dup_src", 2, title="关于乙事项的公告", cwrq="2020-06-01"),
        ], None, False),
    })

    out = pipeline.collect_provincial(conn)

    n = conn.execute("SELECT COUNT(*) FROM policy WHERE title = ?",
                     ("关于乙事项的公告",)).fetchone()[0]
    assert n == 1, "同名同日期没被判重 —— 跨源转载会在库里留下两份"
    assert out[0]["new"] == 1
    assert out[0]["skipped_duplicates"] == 1


def test_truncated_source_is_logged_incomplete_with_reason(conn, monkeypatch):
    """被总时长截断的源必须记 ``incomplete`` 并写明原因，**不能记 ok**。

    硬编码 "ok" 的后果：读 fetch_log 的人以为今天就这么些新政策。
    这条断言的是"读数的人能分辨出自己拿到的是不是全部"。
    """
    a = _adapter("slow_src")
    _patch_sources(monkeypatch, [a], {
        "slow_src": ([_item("slow_src", 1, title="关于丙事项的公告")], None, True),
    })

    out = pipeline.collect_provincial(conn)

    assert out[0]["truncated"] is True
    assert out[0]["status"] == "incomplete"
    row = conn.execute("SELECT status, error FROM fetch_log WHERE source_id = ?",
                       ("slow_src",)).fetchone()
    assert row["status"] == "incomplete", "截断被记成了 ok —— 读日志的人分不出来"
    assert "总时长" in (row["error"] or ""), "incomplete 但没写明原因"


def test_every_source_reports_its_counts(conn, monkeypatch):
    """每个源都要回报 fetched/new/updated/skipped —— 计数缺失让人无法对账。

    ``skipped`` 此前只进了返回值、没进日志也没进 fetch_log，于是"抓 4932 条、
    新增 0"看不出是被判重挡下还是入库失败（排查贵州那次为此绕了很久）。
    """
    a = _adapter("cnt_src")
    _patch_sources(monkeypatch, [a], {
        "cnt_src": ([
            _item("cnt_src", 1, title="关于丁事项的公告", cwrq="2021-01-01"),
            _item("cnt_src", 2, title="关于丁事项的公告", cwrq="2021-01-01"),
            _item("cnt_src", 3, title="关于戊事项的公告", cwrq="2021-02-01"),
        ], None, False),
    })

    out = pipeline.collect_provincial(conn)

    r = out[0]
    assert set(r) >= {"source_id", "status", "fetched", "new", "updated",
                      "skipped_duplicates", "truncated"}
    assert r["fetched"] == 3
    assert r["new"] == 2          # 丁 + 戊
    assert r["skipped_duplicates"] == 1   # 丁的第二份
