"""脚本文件守卫：仓库内的 .bat 必须是纯 ASCII。

实测踩过这个坑：把中文提示写进 .bat，中文 Windows 的 cmd 会用 **GBK**
解析该文件，UTF-8 的中文变成乱码，甚至被当成命令去执行：

    '棶' 不是内部或外部命令，也不是可运行的程序

`chcp 65001` 救不了 —— 代码页在读文件那一刻就已经定下了。
所以规矩是：**.bat 只放 ASCII，中文提示由它调起的 Python 打印**。
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _bat_files() -> list[Path]:
    return sorted(ROOT.glob("*.bat")) + sorted(ROOT.glob("tools/*.bat"))


def test_there_is_at_least_one_bat():
    """先确认 glob 真的找得到文件，否则下面的测试会空跑通过。"""
    assert _bat_files(), f"在 {ROOT} 下没找到任何 .bat，路径可能写错了"


def test_bat_files_are_ascii_only():
    for bat in _bat_files():
        raw = bat.read_bytes()
        try:
            raw.decode("ascii")
        except UnicodeDecodeError:
            bad = next(ln for ln in raw.replace(b"\r\n", b"\n").split(b"\n")
                       if any(b > 127 for b in ln))
            raise AssertionError(
                f"{bat.name} 含非 ASCII 字节 —— 中文 Windows 的 cmd 会按 GBK 解析，"
                f"中文会变乱码甚至被当命令执行。首个问题行：{bad[:70]!r}"
            ) from None


def test_bat_files_have_no_utf8_bom():
    """BOM 会让第一行 `@echo off` 失效（cmd 把 BOM 当成命令的一部分）。"""
    for bat in _bat_files():
        assert not bat.read_bytes().startswith(b"\xef\xbb\xbf"), f"{bat.name} 带了 UTF-8 BOM"


def test_start_public_prints_address_from_python_not_bat():
    """中文提示必须由 Python 打印（那里没有代码页问题）。

    这条是在守一个具体的设计决定：bat 只做启动，措辞在 start_public.py。
    """
    script = (ROOT / "tools" / "start_public.py").read_text(encoding="utf-8")
    assert "trycloudflare" in script or "cloudflare" in script.lower()
    assert "对外访问" in script
