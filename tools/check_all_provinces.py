"""31 省入口核查：把「首页有、但当前没配」的政策栏目找出来。

======================================================================
为什么要全查一遍（而不是只查个位数的省）
======================================================================

上海的教训：我判过它"单页（文章 6）"，实际它有 **25 页分页、约 350 条** ——
判错一个省的代价是**整整一个数量级**。青海、新疆也都曾被我下过错误结论。
"我探过了"不是证据，**可复核的数据**才是。

======================================================================
判据
======================================================================

对每个省：
  ① 抓首页，列出所有政策类栏目的链接
  ② 与 ADAPTERS 里该省已配的 list_url / extra_urls 对比
  ③ 对**未覆盖**的栏目逐个抓一次，数页内有多少文章链接
     → 有文章 = **真缺口**；0 篇 = 壳页 / 检索页（记下原因）

注意：不同省的文章链接形态差别很大（/art/…、t20260930_xxx.html、
/content/123.shtml…），所以这里用一个宽松的通用正则 + 逐省核对现有源的正则，
宁可多报也不要漏报。
"""
from __future__ import annotations

import re
import sys
from collections import Counter

sys.path.insert(0, r"D:\EY-project\src")
from taxassist.collect.browser import fetch_html  # noqa: E402
from taxassist.province import ADAPTERS  # noqa: E402

# 政策类栏目的名字（首页链接文本精确匹配这些词才算候选）
KEYS = ("政策文件", "最新文件", "税收规范性文件", "政策法规库", "规范性文件",
        "税收法规", "地方性法规", "最新法规", "政策法规", "文件公告")

# 宽松的"这页有没有文章"判据：覆盖各省见过的形态
ART = re.compile(
    r"/art/\d+/\d+/\d+/art_\d+_\d+\.html?"
    r"|t\d{8}_\d+\.html?"
    r"|/\d{6}/t\d+_\d+\.html?"
    r"|content_\d+\.shtml"
    r"|/\d{4}/\d{2}-\d{2}/\d+\.html"
    r"|/\d+/\d+-\d+/\d+\.html"
    r"|/show/\d+"
    r"|/detail/[a-z0-9\-]{6,}"
    r"|/\d{6}/[0-9a-f]{32}\.shtml"
    r"|\./[a-z]+/\d{6}/t\d+\.html")

by_region: dict[str, list] = {}
for ad in ADAPTERS:
    by_region.setdefault(ad.region, []).append(ad)

print(f"共 {len(by_region)} 个地区待查\n", flush=True)
gaps: list[tuple[str, str, int]] = []

for region in sorted(by_region):
    ads = by_region[region]
    configured = set()
    for ad in ads:
        configured.add(ad.list_url)
        configured.update(ad.extra_urls)
    m = re.match(r"https?://[^/]+", ads[0].base_url)
    home = (m.group(0) if m else ads[0].base_url).rstrip("/") + "/"

    try:
        front = fetch_html(home, wait_ms=12000, timeout_ms=100000)
    except Exception as exc:  # noqa: BLE001
        print(f"[{region}] 首页失败 {type(exc).__name__}")
        continue

    # 首页里的政策类栏目
    seen: dict[str, str] = {}
    for href, raw in re.findall(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
                                front, re.S):
        t = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", raw)).strip()
        if t not in KEYS or not href or href.startswith("#"):
            continue
        full = (href if href.startswith("http")
                else home.rstrip("/") + (href if href.startswith("/")
                                         else "/" + href.lstrip("./")))
        seen[full] = t

    missing = {u: t for u, t in seen.items() if u not in configured}
    line = [f"[{region}] 首页栏目 {len(seen)} 个，已配 {len(configured)} 个"]
    if not missing:
        print("  ".join(line) + "  ✓ 无缺口")
        continue

    # 未覆盖的栏目逐个抓，数文章
    hits = []
    for url, label in missing.items():
        try:
            page = fetch_html(url, wait_ms=11000, timeout_ms=90000)
        except Exception:  # noqa: BLE001
            hits.append((label, url, -1))
            continue
        hits.append((label, url, len(ART.findall(page))))

    real = [h for h in hits if h[2] > 0]
    print("  ".join(line))
    for label, url, n in hits:
        mark = "** 缺口 **" if n > 0 else ("抓取失败" if n < 0 else "空/壳页")
        print(f"      {label:<14} {n:>4} 篇  {mark}  {url[:56]}")
        if n > 0:
            gaps.append((region, url, n))

print("\n" + "=" * 70)
print(f"真缺口（有文章但未覆盖）：{len(gaps)} 个")
for region, url, n in gaps:
    print(f"  {region:<6} {n:>4} 篇  {url[:70]}")
