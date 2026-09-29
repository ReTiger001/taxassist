"""一键对外：起本地服务 + 起 Cloudflare 隧道，并打印对外地址。

为什么不把提示写进 .bat：中文 Windows 的 cmd 用 GBK 解析批处理文件，
UTF-8 写的中文提示会变成乱码、甚至把命令拆断（实测：
``'棶' 不是内部或外部命令``）。把逻辑放进 Python，编码就与代码页无关了。
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLOUDFLARED = ROOT / "tools" / "cloudflared.exe"
PORT = 8765


def main() -> int:
    print("=" * 64)
    print("  税务智能知识助手 —— 对外访问")
    print("=" * 64)
    print()

    if not CLOUDFLARED.exists():
        print("  [!] 找不到 tools\\cloudflared.exe")
        print("      按 docs\\对外部署.md 里的说明下载，或改用 Tailscale。")
        return 1
    if not (ROOT / ".venv" / "Scripts" / "python.exe").exists():
        print("  [!] 找不到 .venv —— 请在项目目录下先建好虚拟环境。")
        return 1

    print("  本窗口保持打开，对外访问才存在；关掉即断。")
    print()
    print("  三点须知：")
    print("   1. 本机浏览器若开着代理（127.0.0.1:7897），需要把")
    print("      *.trycloudflare.com 设为直连，否则你自己也打不开 ——")
    print("      curl 不受影响，因为它不读系统代理。")
    print("   2. 地址每次重启都会变，这是临时地址。")
    print("   3. 页面内容会经过 Cloudflare 的服务器（已加密到其边缘）。")
    print()

    print(f"[1/2] 启动本地服务（0.0.0.0:{PORT}，已启用认证）…")
    server = subprocess.Popen(
        [sys.executable, "-m", "taxassist", "serve",
         "--host", "0.0.0.0", "--port", str(PORT)],
        cwd=str(ROOT))
    time.sleep(4)

    print("[2/2] 建立 Cloudflare 隧道…")
    print()
    print("  下方出现的 https://****.trycloudflare.com 就是对外地址，")
    print("  连同邀请码一起发给使用者。")
    print()
    try:
        subprocess.run(
            [str(CLOUDFLARED), "tunnel", "--url", f"http://127.0.0.1:{PORT}",
             "--no-autoupdate"],
            cwd=str(ROOT))
    except KeyboardInterrupt:
        pass
    finally:
        print()
        print("隧道已停止，正在关闭本地服务…")
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
        print("已全部停止。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
