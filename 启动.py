#!/usr/bin/env python3
"""网盘管理 V8_3 启动入口。

用法：
    python 启动.py                 # GUI
    python 启动.py --cli ...       # 命令行，等价于 python -m v8_3 ...
    python 启动.py --调试           # 终端打印调试级日志（V8 风格，最啰嗦）
    python 启动.py --静默           # 终端不打印（只写 数据/界面日志.txt）
    python 启动.py --日志级别 警告    # 自定义：调试/信息/警告/错误
    python 启动.py --跳过自检         # 不打印启动自检横幅（连通性也不测）
"""

from __future__ import annotations

import gc
import os
import sys
from pathlib import Path

# Python 3.14 + PySide6 的 GC 兼容策略：三家适配器都采用同样做法。
# 不手工 gc.collect()；只关闭自动整堆回收。
if sys.version_info >= (3, 14):
    gc.disable()
    gc.set_threshold(1_000_000, 1_000_000, 1_000_000)

项目根 = Path(__file__).resolve().parent
if str(项目根) not in sys.path:
    sys.path.insert(0, str(项目根))

# Windows 绿色版双击 启动.exe 时用的是 pythonw.exe（GUI 子系统，不弹控制台），
# 这种情况下 sys.stdout / sys.stderr 是 None：任何 print / logging 都可能炸。
# 这里给它们兜个底，界面日志照旧写进 数据/界面日志.txt。
if sys.stdout is None or sys.stderr is None:
    _空设备 = open(os.devnull, "w", encoding="utf-8")
    if sys.stdout is None:
        sys.stdout = _空设备
    if sys.stderr is None:
        sys.stderr = _空设备

# 自举：直接敲 ./启动.py 时，shebang 用的是 PATH 里的 python，可能是系统 python
# （没有 PySide6/httpx）。这里换成项目自带的解释器重新执行自己 —— 必须赶在
# 下面任何重库 import 之前。详见 v8_3/自举.py，可用 V8_3_不自动换解释器=1 关掉。
from v8_3.自举 import 确保项目环境  # noqa: E402

确保项目环境()

# V8_3：把 VLC（以及 Qt）要写的用户目录搬进项目内，避免污染系统环境。
# 必须在创建 QApplication / libvlc 之前执行，环境变量才对它们的初始化生效。
try:
    from v8_3.播放.vlc绑定 import 准备干净环境
    准备干净环境(项目根)
except Exception:
    pass

# V8_3：libvlc 只能往 X11 窗口画 —— Wayland 会话里必须切到 XWayland(xcb)，
# 否则把 wl_surface 的窗口号交给 set_xwindow 会**直接段错误闪退**（用户实测）。
# 这一句必须早于 QApplication 创建，环境变量才对 Qt 插件选择生效。
try:
    from v8_3.播放.显示环境 import 准备嵌入显示
    _显示 = 准备嵌入显示()
    if _显示.get("说明"):
        print(_显示["说明"])
except Exception:
    pass


def _取出日志参数(argv: list[str]) -> tuple[list[str], str, bool, bool]:
    """取出 --调试 / --静默 / --日志级别 X / --跳过自检（其余原样返回）。"""
    级别 = "信息"
    静默 = False
    自检 = True
    其余: list[str] = []
    i = 0
    while i < len(argv):
        项 = argv[i]
        if 项 in ("--调试", "-d", "--debug"):
            级别 = "调试"
        elif 项 in ("--静默", "-q", "--quiet"):
            静默 = True
        elif 项 in ("--跳过自检", "--no-check"):
            自检 = False
        elif 项 in ("--日志级别", "--log-level") and i + 1 < len(argv):
            i += 1
            级别 = argv[i]
        elif 项.startswith("--日志级别="):
            级别 = 项.split("=", 1)[1]
        else:
            其余.append(项)
        i += 1
    return 其余, 级别, 静默, 自检


def main() -> int:
    argv, 级别, 静默, 要自检 = _取出日志参数(list(sys.argv[1:]))

    from v8_3.日志 import (设置终端日志, 关闭 as 关闭终端,
                        强制关闭 as 强制关闭终端, 打印启动横幅)  # noqa: F401
    设置终端日志(级别=级别)
    if 静默:
        强制关闭终端()      # 比配置里的 界面.终端日志 优先级更高

    if "--cli" in argv:
        argv.remove("--cli")
        from v8_3.命令行 import main as 命令行入口
        return 命令行入口(argv)
    if argv and argv[0] == "cli":
        from v8_3.命令行 import main as 命令行入口
        return 命令行入口(argv[1:])

    # V8 风格的启动自检（主题/价格/时段/敏感词库/AI/网盘/连通性）
    运行时 = None
    主题名 = ""
    启动日志 = None
    if 要自检:
        from v8_3.配置 import 加载配置
        from v8_3.启动自检 import 运行启动自检
        自检 = 运行启动自检(加载配置(), None)
        运行时 = 自检.运行时
        主题名 = 自检.结果.get("主题", "")
        启动日志 = 自检.行
    else:
        打印启动横幅()

    from v8_3.界面.主窗口 import 运行界面
    return int(运行界面(AI运行时=运行时, 主题=主题名,
                      启动日志=启动日志) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
