"""候选栏目的重叠检测：判断「有文章但未覆盖」的栏目是不是**真缺口**。

======================================================================
为什么必须先跑这一步
======================================================================

``check_all_provinces.py`` 判缺口的依据是「首页上有这个栏目、ADAPTERS 里
没配它」。但**「没配这个 URL」不等于「这些文章没抓进来」** —— 同一批文件
常同时挂在多个栏目下（政策法规库 / 最新文件 / 某专项行动专栏），也可能
已被同省另一个源抓到了。

只按 URL 判，会把「文章其实已在库里」的栏目也算成缺口，于是补一堆
``extra_urls`` 去重复抓同一批文件：白等一轮抓取，还在库里留下重复。

**判据（用户早先定的）：看翻页后内容是否变化、看文章是否已在库，
不是看有没有分页链接、也不是看有没有配 URL。**

======================================================================
判定
======================================================================

  · 已有比例 ≥ 90%  → 重复栏目，**不补**（补了只是重复抓）
  · 已有比例 < 90%  → **真缺口**，记下新增篇数
  · 抓不到文章      → 壳页/检索页，不补（并注明）

======================================================================
用法
======================================================================

    python tools/check_overlap.py --region 上海 --url http://.../zcfw/
    python tools/check_overlap.py --batch gaps.txt
    （gaps.txt 每行「省名 空格 URL」，可直接粘贴 check_all_provinces 的输出）
"""
from __future__ import annotations

import argparse
import re
import sys
from urllib.parse import urljoin

sys.path.insert(0, r"D:\EY-project\src")
from taxassist import db as dbmod  # noqa: E402
from taxassist.collect.browser import fetch_html  # noqa: E402

# 与 check_all_provinces.py 用同一套宽松正则：宁可多报也不要漏报
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


def _path_of(url: str) -> str:
    """取 URL 的路径部分做比对。

    归一化的理由：同一篇文章在不同来源里可能写作 http/https、带或不带
    ``www.``、带或不带查询串。按整串比会把同一篇算成两篇。
    **但光靠它不够** —— 见 ``check()`` 里的说明。
    """
    m = re.match(r"https?://[^/]+(/.*)$", url or "")
    return ((m.group(1) if m else (url or "")).split("?")[0]).rstrip("/")


def _norm_title(t: str) -> str:
    """标题归一：去标签、去空白（含全角空格）、去栏目页爱加的尾巴。

    为什么必须用标题做第二个判据：栏目页与详情页给出的 URL 形态可能不同，
    而且省级站大量**转载总局文件**（文章 url 指向总局站，本地栏目里的
    URL 跟自己库里的对不上）。实测：北京库内 4190 条，只比 URL 得出
    「已有 0」——差得离谱。标题是稳定的。
    """
    t = re.sub(r"<[^>]+>", "", t or "")
    t = re.sub(r"[\s\u3000]+", "", t)
    t = re.sub(r"[（(]\s*(全文|附解读|解读|附件|原文)\s*[)）]$", "", t)
    return t.strip("　 ·•-—*")


def _extract_articles(page: str, base: str) -> list[tuple[str, str]]:
    """从列表页抽出 (绝对 URL, 归一标题)。"""
    out: list[tuple[str, str]] = []
    for href, raw in re.findall(
            r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', page, re.S):
        if not href or not ART.search(href):
            continue
        full = href if href.startswith("http") else urljoin(base, href)
        title = _norm_title(raw)
        if title:
            out.append((full, title))
    return out


def known_paths(region: str = "") -> set[str]:
    """库里已有的详情页路径。

    **默认查全库，不是只看该地区。** 踩过的坑（2026-10-06）：省级站的绝大
    多数文章是**总局文件的转载** —— 云南某个栏目列出的 15 篇，标题清一色是
    「国家税务总局关于…」；库里其实以 ``p_region='全国'`` 存着。只按本地区
    比对会把它们全判成缺口，然后补一堆源去重复抓已在库的文件：白抓一轮，
    库里还会多出一批同文号的省级副本。

    传 region 则只查该地区（保留此能力，用于「只看本省原创」的场景）。
    """
    conn = dbmod.connect()
    try:
        if region:
            rows = conn.execute(
                "SELECT url FROM policy WHERE p_region = ? AND url IS NOT NULL",
                (region,)).fetchall()
        else:
            rows = conn.execute(
                "SELECT url FROM policy WHERE url IS NOT NULL").fetchall()
    finally:
        conn.close()
    return {_path_of(r[0]) for r in rows if r[0]}


def known_titles(region: str = "") -> set[str]:
    """库里已有政策的标题（归一后的前 20 字）。

    取前 20 字而不是整串：栏目页标题与库内标题常有细微出入（栏目页可能截断、
    加后缀，或反过来是库里存的更全）。前 20 字对政策标题来说已足够唯一。

    默认查全库 —— 同 ``known_paths``。
    """
    conn = dbmod.connect()
    try:
        if region:
            rows = conn.execute(
                "SELECT title FROM policy WHERE p_region = ? AND title IS NOT NULL",
                (region,)).fetchall()
        else:
            rows = conn.execute(
                "SELECT title FROM policy WHERE title IS NOT NULL").fetchall()
    finally:
        conn.close()
    return {_norm_title(r[0])[:20] for r in rows if r[0]}


def check(region: str, url: str, *, wait_ms: int = 11000) -> dict:
    try:
        page = fetch_html(url, wait_ms=wait_ms, timeout_ms=90000)
    except Exception as exc:  # noqa: BLE001 - 抓取失败要如实报，不能当"无文章"
        return {"region": region, "url": url,
                "error": f"{type(exc).__name__}: {exc}"[:80]}

    arts = _extract_articles(page, url)
    kp = known_paths()          # 全库，不只本地区 —— 见 known_paths 的说明
    kt = known_titles()
    # **两个判据取并集**：URL 命中或标题命中，都算"已在库里"。
    # 只用 URL 会严重漏判（北京 4190 条被判成 0），只用标题会漏掉标题被
    # 改写的（同一文件在不同站上标题可能微调）。政策检索系统里"宁可不补
    # 也不要重复抓"更划算：重复抓的代价是库里出现重复条目。
    hit = 0
    for full, title in arts:
        if _path_of(full) in kp or title[:20] in kt:
            hit += 1
    total = len(arts)
    ratio = hit / total if total else 0.0
    if total == 0:
        verdict = "壳页/无文章"
    elif ratio >= 0.9:
        verdict = "重复栏目（不补）"
    else:
        verdict = "真缺口（补）"
    return {"region": region, "url": url, "total": total, "known": hit,
            "new": total - hit, "ratio": ratio, "verdict": verdict}


def main() -> int:
    ap = argparse.ArgumentParser(description="候选栏目的重叠检测")
    ap.add_argument("--region", help="省名，如 上海")
    ap.add_argument("--url", help="候选栏目 URL")
    ap.add_argument("--batch", help="批量文件，每行「省名 URL」")
    ap.add_argument("--wait", type=int, default=11000, help="渲染等待毫秒")
    args = ap.parse_args()

    items: list[tuple[str, str]] = []
    if args.batch:
        with open(args.batch, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split(None, 1)
                if len(parts) == 2:
                    items.append((parts[0], parts[1]))
    elif args.region and args.url:
        items.append((args.region, args.url))
    else:
        ap.error("要么给 --region + --url，要么给 --batch")

    results = []
    for region, url in items:
        r = check(region, url, wait_ms=args.wait)
        results.append(r)
        if r.get("error"):
            print(f"  {region:<6} 抓取失败  {r['error']:<24} {url[:52]}",
                  flush=True)
        else:
            print(f"  {region:<6} {r['total']:>4} 篇　库内已有 {r['known']:>4} "
                  f"({r['ratio'] * 100:>5.1f}%)　新增 {r['new']:>4}　"
                  f"{r['verdict']:<16} {url[:48]}", flush=True)

    gaps = [r for r in results if r.get("verdict", "").startswith("真缺口")]
    print(f"\n真缺口 {len(gaps)} 个 / 候选 {len(results)} 个"
          f"（重复 {sum(1 for r in results if r.get('verdict', '').startswith('重复'))}，"
          f"壳页 {sum(1 for r in results if r.get('verdict') == '壳页/无文章')}，"
          f"失败 {sum(1 for r in results if r.get('error'))}）")
    for r in gaps:
        print(f"  {r['region']:<6} 新增 {r['new']:>4} 篇  {r['url']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
