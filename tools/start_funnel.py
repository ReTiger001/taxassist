"""一键对外（Tailscale Funnel 版）：起本地服务 + 配**固定**地址。

与 start_public.py 的区别：那个走 Cloudflare 临时隧道（地址每次变、内容经
Cloudflare 边缘）；这个走 Tailscale Funnel，地址固定为
``https://<节点名>.<tailnet>.ts.net``，只经 Tailscale 自己的中继。

为什么写成 Python 而不是 .bat：中文 Windows 的 cmd 用 GBK 解析批处理，
UTF-8 的中文提示会乱码甚至把命令拆断（实测过 ``'棶' 不是内部或外部命令``）。

前置：Tailscale 已登录。未登录时先跑 ``D:\\Tailscale\\tailscale-ipn.exe``
在托盘图标上登录 —— 登录令牌会丢（本轮就遇到过一次 ``NoState``）。
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TS = Path(r"D:\Tailscale\tailscale.exe")
PORT = 8765


def _run(cmd: list[str]) -> str:
    p = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return ((p.stdout or "") + (p.stderr or "")).strip()


def main() -> int:
    print("=" * 64)
    print("  税务智能知识助手 —— 对外访问（Tailscale Funnel，地址固定）")
    print("=" * 64)
    print()

    if not TS.exists():
        print(f"  [!] 找不到 {TS}")
        return 1

    status = _run([str(TS), "status"])
    if "Logged out" in status or "NoState" in status or not status:
        print("  [!] Tailscale 未登录（或状态异常）。")
        print(f"      请运行 {TS.parent / 'tailscale-ipn.exe'}，在托盘图标上登录后重试。")
        return 1
    first = status.splitlines()[0] if status.splitlines() else ""
    print(f"  [1/3] Tailscale 已登录 ✓  {first[:60]}")

    # 只绑回环：隧道本来就从 127.0.0.1 连进来。绑 0.0.0.0 不会让对外更容易，
    # 却会让同网段（办公室 LAN、酒店 Wi-Fi）任何人明文直连并嗅到口令。
    print(f"  [2/3] 启动本地服务（127.0.0.1:{PORT}，已启用认证）…")
    server = subprocess.Popen(
        [sys.executable, "-m", "taxassist", "serve",
         "--host", "127.0.0.1", "--port", str(PORT), "--expose"],
        cwd=str(ROOT))
    time.sleep(4)

    print("  [3/3] 配置 Funnel（后台常驻）…")
    out = _run([str(TS), "funnel", "--bg", str(PORT)])
    print()
    print(out or "  （funnel 未返回信息，可用 tailscale funnel status 查看）")
    print()
    print("  固定地址即上面 Available on the internet 一行。")
    print("  本窗口保持打开，本地服务才在（Funnel 配置本身是常驻的）。")
    try:
        server.wait()
    except KeyboardInterrupt:
        pass
    finally:
        print("\n本地服务已停止（Funnel 配置仍保留，下次直接重跑本脚本即可）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
