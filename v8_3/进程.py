# v8_3/进程.py
"""起子进程的统一入口：**Windows 上不许弹黑框**。

为什么要有这个模块（VIP 用户实测反馈）
======================================
Windows 版"下载 AI 模型"时，屏幕上会**弹出两个大黑框**（标题是
``…\\运行环境\\本地模型\\ollama.exe``）：一个是后台的 ``ollama serve``、
一个是 ``ollama pull``。它们是控制台程序，而 GUI 程序（``启动.exe`` /
``pythonw.exe``）起控制台程序时，**Windows 默认给它分配一个新控制台窗口** ——
于是用户看到两个关不掉的黑色窗口（关掉就等于中断下载）。

对策就是创建进程时带上：

* ``CREATE_NO_WINDOW``（0x08000000）：**不创建控制台窗口**（这才是关键，
  只设 ``SW_HIDE`` 有时仍会闪一下）；
* 再配 ``STARTUPINFO(SW_HIDE)`` 双保险（某些老工具/包装器认这个）。

Linux/macOS 不需要（本来就没有控制台窗口一说），返回空参数即可。

用法::

    from ..进程 import 无窗口参数, 起
    子 = 起([可执行, "serve"], stdout=句柄, stderr=subprocess.STDOUT)
    进程 = 起([可执行, "pull", 模型], capture_output=True, text=True)
    # 需要自己拼 kwargs 时： subprocess.run(命令, **无窗口参数(), ...)
"""

from __future__ import annotations

import os
import subprocess
from typing import Any

__all__ = ["无窗口参数", "起", "起并等待", "命令行跑一下"]

#: CREATE_NO_WINDOW：Windows 上"不要给我开控制台窗口"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


def 无窗口参数() -> dict[str, Any]:
    """返回"不弹黑框"的 subprocess 参数（非 Windows 返回空字典）。"""
    if os.name != "nt":
        return {}
    参数: dict[str, Any] = {"creationflags": CREATE_NO_WINDOW}
    try:
        启动信息 = subprocess.STARTUPINFO()
        启动信息.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        启动信息.wShowWindow = subprocess.SW_HIDE
        参数["startupinfo"] = 启动信息
    except Exception:  # noqa: BLE001 - 拿不到 STARTUPINFO 也不影响 CREATE_NO_WINDOW
        pass
    return 参数


def 起(命令, **关键字) -> subprocess.Popen:
    """``subprocess.Popen`` + 不弹黑框（用户自己的参数优先）。"""
    全部 = {**无窗口参数(), **关键字}
    return subprocess.Popen(命令, **全部)


def 起并等待(命令, **关键字) -> subprocess.CompletedProcess:
    """``subprocess.run`` + 不弹黑框。"""
    全部 = {**无窗口参数(), **关键字}
    return subprocess.run(命令, **全部)


def 命令行跑一下(命令: str, **关键字) -> subprocess.CompletedProcess:
    """跑一条命令行（shell=True）+ 不弹黑框。"""
    全部 = {**无窗口参数(), **关键字}
    return subprocess.run(命令, shell=True, **全部)
