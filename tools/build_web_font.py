"""把 Sarasa Mono SC 子集化并转成 woff2，供网页 @font-face 使用。

为什么要自托管而不是只靠系统安装：
  1. 浏览器进程在字体安装之前就启动了，看不到新装的字体（实测：canvas 量
     "Sarasa Mono SC" 的宽度 = 量乱写字体名的宽度 = sans-serif 的宽度）。
  2. 25MB 的 TTF 直接当 web 字体太重。
  自托管 + 子集化后只有几百 KB，且立刻生效、不依赖系统状态。

字符集构成：
  · ASCII 全量（代码、数字、英文）
  · GB2312 一级汉字 3755 个（覆盖日常中文文本约 99.7%）
  · 全角标点与常用符号（公文里大量出现：《》〈〉〔〕【】、·、— 等）
  · 模板文件与政策标题里实际出现过的字符（兜底，防止漏字）
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

from fontTools.subset import Options, Subsetter
from fontTools.ttLib import TTFont

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "src" / "taxassist" / "web" / "templates"
DB = ROOT / "data" / "taxassist.db"
# 源字体：装到「用户字体目录」的那两个 TTF（装法见文件头）。
# 不放进仓库 —— 单个 25MB、两个 50MB，不该进版本库。
# 若脚本报"缺源文件"，说明系统里还没装 Sarasa Mono SC，先去
# https://github.com/be5invis/Sarasa-Gothic/releases 下载
# SarasaMonoSC-TTF-*.7z，把其中 Regular/Bold 两个 ttf 拷进这个目录。
SRC_DIR = (Path.home() / "AppData" / "Local" / "Microsoft"
           / "Windows" / "Fonts")
OUT_DIR = ROOT / "src" / "taxassist" / "web" / "static" / "fonts"


def gb2312_level1() -> str:
    """GB2312 一级汉字：区位 16-55，即 0xB0A1-0xD7F9。"""
    chars = []
    for hi in range(0xB0, 0xD8):
        for lo in range(0xA1, 0xFA):
            try:
                chars.append(bytes([hi, lo]).decode("gb2312"))
            except UnicodeDecodeError:
                pass
    return "".join(chars)


def punctuation_and_symbols() -> str:
    """公文和界面里高频出现的标点、符号、全角字符。"""
    pieces = [
        "　、。〃〈〉《》「」『』【】〔〕〖〗"
        "！＂＃＄％＆＇（）＊＋，－．／：；＜＝＞？＠［＼］＾＿｀｛｜｝～"
        "·—…‘’“”※→←↑↓★☆○●◇◆□■△▲▽▼°±×÷≈≠≤≥∞§¶†‡"
        "①②③④⑤⑥⑦⑧⑨⑩⑴⑵⑶⑷⑸⑹⑺⑻⑼⑽"
        "ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ"
        "０１２３４５６７８９"
        "ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺ"
        "ａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚ"
        "¥€£¢₹"
    ]
    return "".join(pieces)


def chars_from_templates() -> str:
    if not TEMPLATES.is_dir():
        return ""
    buf = []
    for path in TEMPLATES.rglob("*.html"):
        buf.append(path.read_text(encoding="utf-8", errors="ignore"))
    return "".join(buf)


def chars_from_titles() -> str:
    if not DB.is_file():
        return ""
    try:
        conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        rows = conn.execute("SELECT title FROM policy").fetchall()
        conn.close()
    except sqlite3.Error as exc:
        print(f"读标题失败（忽略）: {exc}")
        return ""
    return "".join(r[0] or "" for r in rows)


def ascii_chars() -> str:
    return "".join(chr(c) for c in range(0x20, 0x7F))


def build_text() -> str:
    parts = [
        ascii_chars(),
        gb2312_level1(),
        punctuation_and_symbols(),
        chars_from_templates(),
        chars_from_titles(),
    ]
    text = "".join(parts)
    # 去重后返回，顺便统计
    uniq = sorted(set(text))
    print(f"字符集大小（去重后）: {len(uniq)}")
    return "".join(uniq)


def subset_one(src: Path, dst: Path, text: str) -> None:
    font = TTFont(str(src), lazy=True)
    options = Options()
    options.layout_features = ["*"]
    options.name_IDs = ["*"]
    options.name_legacy = True
    options.notdef_outline = True
    options.recalc_bounds = True
    options.drop_tables += ["DSIG"]
    subsetter = Subsetter(options=options)
    subsetter.populate(text=text)
    subsetter.subset(font)
    font.flavor = "woff2"
    dst.parent.mkdir(parents=True, exist_ok=True)
    font.save(str(dst))
    font.close()
    src_mb = src.stat().st_size / 1024 / 1024
    dst_kb = dst.stat().st_size / 1024
    print(f"  {src.name} ({src_mb:.1f}MB) -> {dst.name} ({dst_kb:.0f}KB)")


def main() -> int:
    text = build_text()
    jobs = [
        (SRC_DIR / "SarasaMonoSC-Regular.ttf", OUT_DIR / "sarasa-mono-sc-regular.woff2"),
        (SRC_DIR / "SarasaMonoSC-Bold.ttf", OUT_DIR / "sarasa-mono-sc-bold.woff2"),
    ]
    for src, dst in jobs:
        if not src.is_file():
            print(f"缺源文件: {src}", file=sys.stderr)
            return 1
        subset_one(src, dst, text)
    print(f"输出目录: {OUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
