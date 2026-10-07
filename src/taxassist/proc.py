"""子进程工具：在 Windows 上不让子进程弹出控制台窗口。

============================================================
这个坑是怎么来的（实测）
============================================================

翻译进程改由守护以无控制台方式（pythonw + DETACHED）启动后，桌面开始
**不断弹命令窗**。根因是 Windows 的控制台分配规则：

    父进程**没有控制台**时，启动任何**控制台程序**（tasklist、python、
    cmd、UnRAR…）Windows 都会给它**新建一个控制台窗口**。

于是每次探活一个 PID、每次起一个 python 子进程，都会在屏幕上闪出一个黑框。
而写锁的 ``_pid_alive`` 每批拿锁都要探一次、等锁时每 0.2~2 秒探一次，
看上去就是"不断"在弹。

修法只有一个：创建子进程时带上 ``CREATE_NO_WINDOW``。带上之后控制台程序
照样跑、输出照样能从管道读，只是**不会有可见窗口**。

**别用 ``DETACHED_PROCESS`` 代替它。** 实测（本机 Windows 11，A/B 跑过
六种组合）：用 DETACHED_PROCESS 启动 python 会**弹出一个 Windows Terminal
窗口** —— venv launcher 与真 python 都一样，单独用会弹、与 CREATE_NO_WINDOW
组合也弹。只有 CREATE_NO_WINDOW 不弹。而且它给的是一份**不继承父进程**的、
不可见的控制台，"关掉终端服务照样跑"这个目标它已经满足，没有任何理由再用
DETACHED。

反过来说：**父进程有控制台时（从终端手动跑）本来就不会弹**，所以这个坑
只在服务化之后才暴露 —— 谁把进程变成无控制台的，谁就得让它的所有子进程
都带上这个标志。
"""
from __future__ import annotations

import os
import subprocess


def hidden_flags() -> int:
    """创建子进程用的 creationflags：Windows 不弹窗口，其他平台为 0。

    用法::

        subprocess.run([...], creationflags=proc.hidden_flags())
    """
    if os.name == "nt":
        return subprocess.CREATE_NO_WINDOW
    return 0
