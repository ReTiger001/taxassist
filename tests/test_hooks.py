"""提交闸门（git pre-commit 钩子 + 安装器）的测试。

**为什么值得单独测**：这个钩子的职责是"别让问题溜进历史"，而**它自己失效时
不会报错** —— 两条安静路径都真发生过同类问题：

  · ``.git/hooks/`` 不被 git 跟踪 → 换机器/重新 clone 之后钩子就没了，
    而"没有钩子"不报错，只是少了一道闸门；
  · Windows 上 CRLF 的 shell 脚本会让 git 报 ``\\r: command not found``，
    钩子静默不执行 —— 与"根本没装"的后果一模一样。

所以这里钉四件事：源文件真的跑那两个命令、安装器会归一换行、脚本语法合法、
以及**本仓库确实装了且与源文件一致**（最后一件事是在替"我忘了装"发声）。
"""
from __future__ import annotations

import importlib.util
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOOK_SRC = ROOT / "tools" / "hooks" / "pre-commit"
INSTALLER = ROOT / "tools" / "install_hooks.py"


def _load_installer():
    """按路径加载安装器模块（tools/ 不是包，不能直接 import）。"""
    spec = importlib.util.spec_from_file_location("install_hooks", INSTALLER)
    assert spec and spec.loader, f"加载不了 {INSTALLER}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_hook_runs_both_gates_and_has_an_escape_hatch():
    """钩子必须真的跑 ruff 与 pytest，并留出逃生舱。

    只跑其中一个是不够的：ruff 抓不到逻辑错误（我三次粘行是靠测试抓的），
    pytest 抓不到未使用导入与格式问题。
    """
    text = HOOK_SRC.read_text(encoding="utf-8")
    assert "ruff check" in text, "钩子没跑 ruff"
    assert "pytest" in text, "钩子没跑测试"
    assert "src/ scripts/ tests/" in text, "钩子的检查范围没写清"
    assert "TAXASSIST_SKIP_HOOKS" in text, "缺少脚本化场景的逃生舱"
    # 范围说明也要在：以后有人想扩到 tools/，得先知道为什么现在不含它
    assert "tools/" in text, "没说明为什么范围不含 tools/"


def test_installer_normalizes_crlf_to_lf(tmp_path):
    """安装器必须把 CRLF 归一成 LF —— 否则钩子在 Windows 上静默失效。

    这是真会发生的事：git 在某些配置下会把工作区的 shell 脚本写成 CRLF，
    而 CRLF 的脚本被 sh 执行时报 ``\\r: command not found``。
    """
    mod = _load_installer()
    src = tmp_path / "pre-commit"
    src.write_bytes(b"#!/bin/sh\r\necho hi\r\n")          # 故意写成 CRLF
    hooks_dir = tmp_path / "hooks"

    written = mod.install(hooks_dir, source=src)

    assert written == ["pre-commit"]
    out = (hooks_dir / "pre-commit").read_bytes()
    assert b"\r\n" not in out, "换行没被归一 —— 钩子会在 Windows 上静默失效"
    assert out.startswith(b"#!/bin/sh\n")


def test_hook_is_valid_shell_syntax():
    """钩子必须是合法的 shell 脚本 —— 语法错等于钩子失效。

    没有 sh（比如纯 Windows 环境）时跳过并说明原因：这里要证的是"我们写的
    脚本对不对"，不是"这台机器有没有 sh"。
    """
    sh = shutil.which("sh") or shutil.which("bash")
    if not sh:
        pytest.skip("本机没有 sh/bash，无法做语法检查")
    r = subprocess.run([sh, "-n", str(HOOK_SRC)], capture_output=True, text=True)
    assert r.returncode == 0, f"钩子语法不合法：{r.stderr.strip()[:300]}"


def test_repo_hook_is_installed_and_matches_source():
    """本仓库**确实装了**钩子，且与源文件一致。

    这条是在替"我忘了跑安装器"发声：``.git/hooks/`` 不被跟踪，clone 之后必须
    手动装一次，而忘了装是**无声的**。失败信息直接给出要跑的命令。
    """
    installed = ROOT / ".git" / "hooks" / "pre-commit"
    if not installed.exists():
        pytest.fail(
            "本仓库没装提交闸门（.git/hooks/pre-commit 不存在）—— "
            "跑一下：python tools/install_hooks.py")

    want = HOOK_SRC.read_text(encoding="utf-8").replace("\r\n", "\n")
    have = installed.read_text(encoding="utf-8").replace("\r\n", "\n")
    assert have == want, (
        "装着的钩子与源文件不一致 —— 源文件改过之后要重跑："
        "python tools/install_hooks.py")
