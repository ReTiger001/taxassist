"""契约测试：别名导入的属性访问，必须真的存在。

============================================================
这条测试是怎么来的（一次真实事故）
============================================================

提交 f063f56「删净死代码」把 ``translate_llm.translate_long`` 删掉了，依据是
"零引用"。但真实调用点在 ``scripts/translate_batch.py``：

    from taxassist import translate_llm as tl
    ...
    out = tl.translate_long(src, model=..., max_chars=...)

**别名导入 + 属性访问** —— 按函数名做静态扫描看不到它。后果不是报错，
而是更糟的东西：每条正文的 AttributeError 被 ``except`` 吞掉记成"失败"，
整批以退出码 0"成功"结束。日志上看是"瞬间完成、全部阶段完成"，
实际一条都没译，而守护还在每分钟忠实地把它拉起来。

434 项测试全绿也没拦住 —— 因为没有测试盯住"脚本引用的东西是否存在"。

所以这里把契约钉死：脚本里 ``tl.X`` 引用的成员必须真的在 translate_llm 里。
"""
from __future__ import annotations

import re
from pathlib import Path

from taxassist import translate_llm as tl

ROOT = Path(__file__).resolve().parent.parent
SCAN_DIRS = (ROOT / "scripts", ROOT / "tools", ROOT / "src" / "taxassist")

#: translate_llm 在脚本里的常用别名（scripts/ 下统一用 tl）
_ALIAS = "tl"
_REF_RE = re.compile(rf"\b{_ALIAS}\.([a-zA-Z_][a-zA-Z_0-9]*)")


def _referenced() -> dict[str, set[str]]:
    """按文件列出 ``tl.X`` 用到的成员名（只统计真的 import 了别名的文件）。"""
    found: dict[str, set[str]] = {}
    for directory in SCAN_DIRS:
        for path in directory.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if f"translate_llm as {_ALIAS}" not in text:
                continue
            names = set(_REF_RE.findall(text))
            if names:
                # 统一用正斜杠：断言与报错信息里的路径在 Windows 上也要对得上
                found[path.relative_to(ROOT).as_posix()] = names
    return found


def test_scanner_finds_the_file_that_broke():
    """先确认扫描器真扫到了出事的那个文件，否则下面的检查会空跑通过。"""
    assert "scripts/translate_batch.py" in _referenced(), \
        "扫描器没找到 translate_batch.py —— 检查别名或目录配置"


def test_tl_attributes_exist():
    missing: list[str] = []
    for path, names in sorted(_referenced().items()):
        for name in sorted(names):
            if not hasattr(tl, name):
                missing.append(f"{path} -> tl.{name}")
    assert not missing, (
        "这些地方引用了 translate_llm 里**不存在**的成员。\n"
        "注意：这类调用是「别名 + 属性访问」，静态扫描看不到，\n"
        "绝不能据此把它们判成死代码删掉：\n  " + "\n  ".join(missing)
    )


def test_split_chunks_keeps_every_character():
    """分块翻译的前提是分块不丢字 —— 丢了就是静默的译文缺失。"""
    from taxassist.translate_llm import split_chunks

    assert split_chunks("") == []
    assert split_chunks("短文本") == ["短文本"]

    long_text = "。".join(f"第{i}句内容" for i in range(200))
    chunks = split_chunks(long_text, max_chars=50)
    assert len(chunks) > 1, "长文本应被切成多块"
    assert "".join(chunks).replace("\n", "") == long_text, "分块后不能丢字"


def test_translate_long_signature_matches_caller():
    """translate_batch 是按 (src, model=, max_chars=) 调的，签名不能变。"""
    import inspect

    from taxassist.translate_llm import translate_long

    params = inspect.signature(translate_long).parameters
    assert "text" in params
    assert "model" in params and params["model"].kind is inspect.Parameter.KEYWORD_ONLY
    assert "max_chars" in params and params["max_chars"].kind is inspect.Parameter.KEYWORD_ONLY
