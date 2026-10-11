"""把仓库里的 git 钩子装到 ``.git/hooks/``。

**为什么要有这个脚本**：``.git/hooks/`` **不被 git 跟踪** —— 钩子的"源"必须在
仓库里（``tools/hooks/``），再由这个脚本复制过去。否则换机器或重新 clone 之后
钩子就没了，而"没有钩子"这件事**不会报错**：只是安静地少了一道闸门，一切照旧
运行到某天出了问题才会发现。

**为什么复制时要统一换行**：Windows 上 git 可能把工作区的 shell 脚本写成 CRLF，
而 CRLF 的 shell 脚本会让 git 调用时报 ``\r: command not found`` —— 钩子直接
失效。所以这里读进来先归一成 ``\n`` 再写出去（``.gitattributes`` 里也钉了一条
``tools/hooks/* text eol=lf``，两头都防）。

用法::

    python tools/install_hooks.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "tools" / "hooks" / "pre-commit"
HOOKS = ("pre-commit",)


def install(hooks_dir: Path, source: Path = SOURCE) -> list[str]:
    """把钩子装到 ``hooks_dir``，返回装好的名字列表。

    拆成纯函数是为了能测：测试拿一个 tmp 目录当 ``.git/hooks`` 调它，
    不必碰真实的仓库钩子。
    """
    hooks_dir.mkdir(parents=True, exist_ok=True)
    text = source.read_text(encoding="utf-8").replace("\r\n", "\n")
    written = []
    for name in HOOKS:
        (hooks_dir / name).write_text(text, encoding="utf-8", newline="\n")
        written.append(name)
    return written


def main() -> int:
    hooks_dir = ROOT / ".git" / "hooks"
    if not hooks_dir.parent.exists():
        print(f"没找到 git 目录：{hooks_dir.parent} —— 这不是一个 git 仓库？")
        return 1
    for name in install(hooks_dir):
        print(f"已安装 {name} → {hooks_dir / name}")
    print("逃生舱：git commit --no-verify，或 TAXASSIST_SKIP_HOOKS=1")
    return 0


if __name__ == "__main__":
    sys.exit(main())
