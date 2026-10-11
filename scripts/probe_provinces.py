"""批量探站：一次摸多个省税务局有哪些政策栏目、是静态还是 JS。

======================================================================
为什么写成一个脚本
======================================================================

逐省手跑 probe_source 要几十轮，而这一步在各省之间是**同构**的（进首页 →
抓栏目链接 → 逐个试解析）。批量做完一次看清全局，再按「静态 / 需要 JS」
分开处理 —— 实测这两类做法完全不同：

  · 广东新栏目是**静态**的 → 直接加 list_url 就行
  · 江苏新栏目是 **JS 异步**的 → 要 needs_js 或找 XHR 接口

先归类，再动手，比一个个试快得多，也不会把两类混为一谈。

用法：
    python scripts/probe_provinces.py --limit 6        # 先探 6 省
    python scripts/probe_provinces.py                  # 全部省
    python scripts/probe_provinces.py --only 山东,河南
"""
from __future__ import annotations

import argparse
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from taxassist import province_sources as ps  # noqa: E402

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
#: 目录型链接（栏目页），排除详情页与静态资源
COL_RE = re.compile(r'href="(/[a-z0-9_]+(?:/[a-z0-9_]+){0,2}/[a-z0-9_]*\.s?html?)"', re.I)
DETAIL_RE = re.compile(r"(content|detail|info|art|doc|show|c)\w*/\d|/\d{4}-\d{2}/\d{2}/",
                       re.I)
DATE_RE = re.compile(r"20\d{2}\s*[-/年.]\s*\d{1,2}\s*[-/月.]\s*\d{1,2}")


def fetch(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    return urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "replace")


def scan_site(base: str, max_cols: int = 14) -> list[tuple[str, str, int, str]]:
    """抓首页→提取候选栏目→逐个试解析。返回 [(栏目URL, 状态, 详情链接数, 最新日期)]。

    **状态里必须带 HTTP 状态码**。2026-10 的教训：这里原来只写
    ``type(e).__name__``，于是 412 与 404 都显示成"HTTPError" —— 我据此得出
    "20 省入口失效"的结论，而真相是那些省在**加速乐 WAF 后面**（412），
    站点好好活着，只是普通 HTTP 请求过不去。判断方向被完全带偏。

    现在 412 / 403 / 404 各是各的：404 才是"这个地址没有了"，
    412 是"要过 WAF 挑战"（该走 needs_js，不是该换地址）。
    """
    try:
        home = fetch(base)
    except urllib.error.HTTPError as e:
        return [(base, f"首页 HTTP {e.code}", -1, "")]
    except Exception as e:  # noqa: BLE001
        return [(base, f"首页失败:{type(e).__name__}", -1, "")]

    cols: list[str] = []
    for href in COL_RE.findall(home):
        if href.rstrip("/").endswith("index.html") or href.count("/") <= 3:
            if href not in cols:
                cols.append(href)
    out = []
    for href in cols[:max_cols]:
        url = base.rstrip("/") + href
        try:
            html = fetch(url)
        except urllib.error.HTTPError as e:
            out.append((url, f"HTTP {e.code}", -1, ""))
            continue
        except Exception as e:  # noqa: BLE001
            out.append((url, f"失败:{type(e).__name__}", -1, ""))
            continue
        # 数「像详情页的链接」：既有 detail 关键词，又带日期或数字路径
        hits = [h for h in re.findall(r'href="([^"]+\.s?html?)"', html, re.I)
                if DETAIL_RE.search(h)]
        dates = DATE_RE.findall(html)
        out.append((url, "静态" if hits else "JS异步", len(hits),
                    dates[0] if dates else ""))
    return out


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--only", default="")
    args = ap.parse_args()

    # 每省取一个已知的 base_url 作为入口
    sites: dict[str, str] = {}
    for a in ps.ADAPTERS:
        sites.setdefault(a.region, a.base_url or "")
    if args.only:
        want = {x.strip() for x in args.only.split(",")}
        sites = {k: v for k, v in sites.items() if k in want}
    items = [(k, v) for k, v in sites.items() if v]
    if args.limit:
        items = items[:args.limit]

    print(f"探 {len(items)} 个省\n")
    for region, base in items:
        rows = scan_site(base)
        good = [r for r in rows if r[2] > 0]
        js = [r for r in rows if r[1] == "JS异步"]
        print(f"【{region}】{base}")
        print(f"   候选栏目 {len(rows)} 个：静态可解析 {len(good)} ／ JS异步 {len(js)}")
        for url, st, n, d in rows[:8]:
            mark = "✓" if n > 0 else "·"
            print(f"     {mark} [{st}] 详情{n:>3}  {d:<12} {url[len(base):][:44]}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
