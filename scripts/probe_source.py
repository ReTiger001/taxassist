"""政策源探测工具。

新增或维护抓取源之前，先用它确认四件事：

1. 能否直接抓到（状态码 / 页面大小 / 是否被 WAF 拦截）
2. 页面是静态渲染还是 JS 异步加载（决定要不要上 headless 浏览器）
3. 列表条目长什么样——标题、日期、详情链接各在哪个 DOM 层级
4. 内容是否新鲜——最新条目日期距今天数（防止把已停更的死栏目当成活跃源）

用法：
    python scripts/probe_source.py <url> [<url> ...] [--anchors 10]
"""
from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
import time
import urllib.error
import urllib.request

from lxml import html as LH

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
DATE_RE = re.compile(r"(20\d{2})\s*[-/年.]\s*(\d{1,2})\s*[-/月.]\s*(\d{1,2})\s*日?")
DETAIL_HINT_RE = re.compile(r"(content|detail|info|art|doc|show)\w*\.(html?|shtml?)$", re.I)
AJAX_RE = re.compile(r"""url\s*:\s*["']([^"']{5,120})["']""")


def fetch(url: str, timeout: int = 30) -> tuple[bytes, dict, float]:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
    )
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
        info = {
            "status": resp.status,
            "content_type": resp.headers.get("Content-Type", ""),
            "server": resp.headers.get("Server", ""),
        }
    return body, info, time.time() - t0


def decode(body: bytes, content_type: str) -> str:
    candidates: list[str] = []
    m = re.search(r"charset=([\w-]+)", content_type or "", re.I)
    if m:
        candidates.append(m.group(1))
    head = body[:4096].decode("ascii", "ignore")
    m2 = re.search(r"""charset=["']?([\w-]+)""", head, re.I)
    if m2:
        candidates.append(m2.group(1))
    candidates += ["utf-8", "gb18030"]
    for enc in candidates:
        try:
            return body.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return body.decode("utf-8", "replace")


def text_of(el) -> str:
    try:
        return " ".join((el.text_content() or "").split())
    except (ValueError, AttributeError):
        return ""


def extract_dates(text: str) -> list[dt.date]:
    out = []
    for y, mo, d in DATE_RE.findall(text):
        try:
            out.append(dt.date(int(y), int(mo), int(d)))
        except ValueError:
            continue
    return out


def analyze(url: str, html_text: str, n_anchors: int) -> int:
    """打印单个页面的结构摘要。返回发现的最新条目距今月数（越大越可疑）。"""
    doc = LH.fromstring(html_text)
    title = " ".join((doc.findtext(".//title") or "").split())[:90]
    anchors = doc.xpath("//a[@href]")
    details = [a for a in anchors if DETAIL_HINT_RE.search(a.get("href", ""))]

    print(f"\n{'=' * 78}\nURL   : {url}\nTITLE : {title}")
    print(f"锚点总数 {len(anchors)}   详情型链接 {len(details)}", end="")
    if len(anchors) and len(details) / len(anchors) > 0.2:
        print("  -> 静态列表页（可直接解析 HTML）")
    elif details:
        print("  -> 部分静态，条目可能不全")
    else:
        print("  -> 无静态条目，极可能是 JS 异步加载（需找 XHR 接口或上 headless）")

    print(f"--- 详情链接样本（前 {n_anchors} 条）---")
    all_dates: list[dt.date] = []
    for a in details[:n_anchors]:
        href = a.get("href", "")
        link_text = text_of(a)
        # 条目日期常在链接的父/祖父节点里
        ctx = ""
        for ancestor in (a.getparent(), a.getparent().getparent() if a.getparent() is not None else None):
            if ancestor is None:
                continue
            ctx = text_of(ancestor)
            if ctx and len(ctx) > len(link_text):
                break
        dates = extract_dates(f"{link_text} {ctx}")
        all_dates.extend(dates)
        stamp = dates[0].isoformat() if dates else "无日期"
        print(f"  [{stamp}] {link_text[:64] or '(空标题)'}")
        print(f"           href={href[:76]}")
        if ctx and ctx != link_text:
            print(f"           上下文={ctx[:88]}")

    # 全页日期扫描：判断这个栏目到底更到什么时候
    page_dates = extract_dates(" ".join(text_of(el) for el in doc.xpath("//*") if len(text_of(el)) < 200))
    all_dates.extend(page_dates)
    if all_dates:
        newest = max(all_dates)
        age_days = (dt.date.today() - newest).days
        print(f"--- 新鲜度 --- 发现 {len(set(all_dates))} 个不同日期，最新 {newest} "
              f"（距今 {age_days} 天）")
        if age_days > 120:
            print("    !! 警告：最新条目超过 120 天，疑似已停更或为历史归档栏目")
    else:
        print("--- 新鲜度 --- 页面上未发现任何日期，无法判断新鲜度")

    ajax = sorted(set(AJAX_RE.findall(html_text)))
    if ajax:
        print(f"--- 疑似 AJAX 接口（{len(ajax)} 个）---")
        for u in ajax[:8]:
            print(f"    {u[:100]}")

    scripts = [s.get("src") for s in doc.xpath("//script[@src]")]
    inline_len = sum(len(t) for t in doc.xpath("//script[not(@src)]/text()"))
    print(f"--- 脚本线索 ---  外部脚本 {len(scripts)} 个，内联脚本 {inline_len} 字符")
    for src in scripts[:10]:
        print(f"    src={src[:104]}")
    paths = sorted(set(re.findall(r"""["'](/[A-Za-z0-9_./\-]{4,90})["']""", html_text)))
    hot = [p for p in paths if re.search(r"(api|json|list|search|query|data|inter|ajax|page)", p, re.I)]
    for p in hot[:15]:
        print(f"    path={p}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="探测政策源可抓性与页面结构")
    ap.add_argument("urls", nargs="+")
    ap.add_argument("--anchors", type=int, default=10, help="每个源打印的条目样本数")
    ap.add_argument("--timeout", type=int, default=30)
    args = ap.parse_args()

    failures = 0
    for url in args.urls:
        try:
            body, info, elapsed = fetch(url, args.timeout)
        except urllib.error.HTTPError as e:
            print(f"\n{'=' * 78}\nURL   : {url}\n抓取失败: HTTP {e.code} {e.reason}")
            failures += 1
            continue
        except Exception as e:  # noqa: BLE001 - 探测工具需要报告任何异常
            print(f"\n{'=' * 78}\nURL   : {url}\n抓取失败: {type(e).__name__}: {e}")
            failures += 1
            continue
        print(f"\n{'=' * 78}\nURL   : {url}\n状态 {info['status']}  "
              f"{len(body)} 字节  {elapsed:.2f}s  Server={info['server'] or '-'}")
        ctype = info["content_type"]
        if "pdf" in ctype.lower():
            print("  该链接是 PDF，需走 PDF 解析通道")
            continue
        analyze(url, decode(body, ctype), args.anchors)

    print(f"\n{'=' * 78}\n完成：{len(args.urls) - failures}/{len(args.urls)} 个源抓取成功")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
