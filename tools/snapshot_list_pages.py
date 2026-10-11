"""抓取省级源的列表页并存档为 golden 样本，供 test_parser_golden 回归。

======================================================================
为什么需要它
======================================================================

改解析器最怕的是「看着没坏、其实某个源已经解析不出条目了」—— 这类故障
**是静默的**：parse_list_page 只在「零条目」时才抛错，而"条数变少一半"
它不吭声，fetch_log 还记 ok。项目头部那节「假成功」讲的正是这个。

有了每个源的列表页存档，改完解析器跑一次测试就知道有没有弄坏：
`pytest tests/test_parser_golden.py` 里会有每个样本的条目数断言。

======================================================================
样本与 manifest
======================================================================

    tests/golden/<source_id>.html    原始列表页（不压缩，便于人工查看）
    tests/golden/manifest.json       {"<source_id>": {"min_items": N, ...}}

``min_items`` 是**取样时实测**的条目数（不是猜的）。测试判据用"不低于它"
而不是"精确相等"：站点会陆续发新政策，列表页条数只增不减，下限是稳定的；
要等值的话每次站点更新都得重做样本，测试会被频繁改动而失去意义。

======================================================================
用法
======================================================================

    python tools/snapshot_list_pages.py --source hebei_zxwj --source sh_zcfw
    python tools/snapshot_list_pages.py --all          # 45 个源，约 15 分钟
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(r"D:\EY-project")
sys.path.insert(0, str(ROOT / "src"))

from taxassist.collect.http import GuardedClient  # noqa: E402
from taxassist.province import ADAPTERS_BY_ID, parse_list_page  # noqa: E402

GOLDEN = ROOT / "tests" / "golden"
MANIFEST = GOLDEN / "manifest.json"


def load_manifest() -> dict:
    if MANIFEST.exists():
        return json.loads(MANIFEST.read_text(encoding="utf-8"))
    return {}


def snapshot_one(client: GuardedClient, source_id: str, man: dict) -> bool:
    ad = ADAPTERS_BY_ID.get(source_id)
    if ad is None:
        print(f"  ✗ {source_id}：没有这个源")
        return False
    # 存**列表页原文**：走浏览器的那类要拿渲染后的 HTML —— 这正是
    # parse_list_page 在生产里看到的输入，用别的输入做样本没有意义。
    from taxassist.province import fetch_list_page as _flp  # noqa: F401

    try:
        if ad.needs_js:
            from taxassist.collect.browser import fetch_html
            kw = {}
            if ad.wait_ms:
                kw["wait_ms"] = ad.wait_ms
            if ad.timeout_ms:
                kw["timeout_ms"] = ad.timeout_ms
            html = fetch_html(ad.list_url, use_nodriver=ad.use_nodriver, **kw)
        else:
            html = client.get(ad.list_url).content.decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        print(f"  ✗ {source_id}：抓取失败 {type(exc).__name__}: {str(exc)[:60]}")
        return False

    try:
        for i, ln in enumerate(html.splitlines(), 1):
            if len(ln) > 100_000:      # 单行超长的页面存下来也无意义
                print(f"  ✗ {source_id}：第 {i} 行过长，跳过")
                return False
    except Exception:  # noqa: BLE001
        pass

    n = len(parse_list_page(html, ad))
    GOLDEN.mkdir(parents=True, exist_ok=True)
    (GOLDEN / f"{source_id}.html").write_text(html, encoding="utf-8")
    man[source_id] = {"min_items": n, "list_url": ad.list_url,
                      "needs_js": ad.needs_js,
                      "note": "min_items 为取样时实测；判据是「不低于」它"}
    print(f"  ✓ {source_id}：{len(html)} 字节　解出 {n} 条")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="省级列表页 golden 取样")
    ap.add_argument("--source", action="append", default=[],
                    help="源 ID，可重复")
    ap.add_argument("--all", action="store_true", help="全部源（约 15 分钟）")
    args = ap.parse_args()

    ids = list(ADAPTERS_BY_ID) if args.all else args.source
    if not ids:
        ap.error("要么给 --source，要么给 --all")

    man = load_manifest()
    ok = 0
    with GuardedClient() as client:
        for sid in ids:
            if snapshot_one(client, sid, man):
                ok += 1
    MANIFEST.write_text(json.dumps(man, ensure_ascii=False, indent=2,
                                   sort_keys=True), encoding="utf-8")
    print(f"\n取样完成 {ok}/{len(ids)}，manifest 现有 {len(man)} 个样本")
    print(f"目录：{GOLDEN}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
