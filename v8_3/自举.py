"""在入口处就地修正"跑错了解释器"。

为什么需要
==========
本项目是**完全自包含**的：解释器在 ``运行环境/python``，第三方依赖在
``运行环境/venv``，不依赖系统 python、也不依赖任何别的项目。

但 ``启动.py`` 的 shebang 是 ``#!/usr/bin/env python3`` —— 直接敲
``./启动.py`` 时，用的是 PATH 里的那个 python，很可能是系统 python：
那里既没有 PySide6 也没有 httpx，于是：

* ``ModuleNotFoundError: No module named 'PySide6'``（界面起不来）；
* ``No module named 'httpx'``（价格抓取器、AI 运行时降级）；
* 适配器子进程也会被拉到 ``/usr/bin/python3`` 上。

所以每个入口脚本在**导入任何重库之前**先调一次 :func:`确保项目环境`：
当前解释器只要不是项目自带的那套，就用项目内的解释器重新执行自己
（``启动.py`` 走 ``启动.sh``，顺带拿到缓存隔离与 pyvenv.cfg 自愈）。
换过去之后再进来条件已满足，不会套娃。

想强行用别的解释器（开发调试）::

    V8_3_不自动换解释器=1 python 启动.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

__all__ = ["项目根", "主环境", "项目解释器", "在主环境里", "确保项目环境"]

#: 本文件在 ``v8_3/`` 下，上一级就是项目根
项目根: Path = Path(__file__).resolve().parent.parent


def 环境解释器(环境根: str | Path, 带窗口: bool = False) -> Path:
    """某个环境里的解释器路径。

    Windows 的解释器在 ``Scripts\\`` 下（而且 GUI 用 ``pythonw.exe`` 才不弹控制台），
    其它平台在 ``bin/`` 下。绿色版两个平台都要能用，所以路径必须按平台算。
    """
    根 = Path(环境根)
    if os.name == "nt":
        return 根 / "Scripts" / ("pythonw.exe" if 带窗口 else "python.exe")
    return 根 / "bin" / ("pythonw" if 带窗口 else "python")


def 主环境目录(根: str | Path | None = None) -> Path:
    """主环境目录。

    Linux/macOS 绿色版用的是 venv（``运行环境/venv``）；
    Windows 绿色版把依赖直接装在自带的独立 Python 里（``运行环境/python``）。
    """
    基底 = Path(根) if 根 is not None else 项目根
    return 基底 / "运行环境" / ("python" if os.name == "nt" else "venv")


#: 项目自带的主环境（装了 PySide6 / httpx 的那套）
主环境: Path = 主环境目录()

#: 主环境的解释器
项目解释器: Path = 环境解释器(主环境)

#: 视作"开"的开关取值
_开 = ("1", "true", "yes", "on")


def 在主环境里() -> bool:
    """当前是否就跑在项目自带的主环境里。

    用 ``sys.prefix`` 判断而不是比较 ``sys.executable``：直接拿项目里的**基座**
    解释器（``运行环境/python/bin/python3.14``）跑时，可执行文件确实在项目内，
    但那个环境里并没有第三方依赖，照样得换过去。
    """
    try:
        return Path(sys.prefix).resolve() == 主环境.resolve()
    except Exception:  # noqa: BLE001
        return False


def _入口脚本() -> Path | None:
    """当前正在执行的脚本路径（拿不到就返回 None，调用方就别乱换）。"""
    主模块 = sys.modules.get("__main__")
    候选 = getattr(主模块, "__file__", None) or (sys.argv[0] if sys.argv else "")
    if not 候选:
        return None
    路径 = Path(候选)
    return 路径 if 路径.is_file() else None


def 确保项目环境() -> None:
    """不是项目自带解释器就重新执行自己；返回时保证已经在对的环境里。

    换不过去（没带环境、拿不到脚本路径、exec 失败）就**按原样继续跑**，
    让真实报错暴露出来，而不是在这里把错误吞掉。
    """
    if str(os.environ.get("V8_3_不自动换解释器", "")).strip().lower() in _开:
        return
    if not 项目解释器.is_file() or 在主环境里():
        return

    脚本 = _入口脚本()
    if 脚本 is None:
        return
    参数 = [str(项) for 项 in sys.argv[1:]]
    try:
        # 启动.py 走启动器：顺带做 pyvenv.cfg 自愈 + 把缓存钉在项目内
        启动器 = 项目根 / "启动.sh"
        if 脚本.name == "启动.py" and 启动器.is_file():
            sys.stderr.write(
                "[启动] 当前解释器不是项目自带的，改用 运行环境/venv 重新执行…\n")
            sys.stderr.flush()
            os.execv("/bin/bash", ["/bin/bash", str(启动器), *参数])
        # 其余工具脚本：直接用项目内解释器重跑自己
        sys.stderr.write(
            f"[启动] {脚本.name}：当前解释器不是项目自带的，"
            "改用 运行环境/venv/bin/python 重新执行…\n")
        sys.stderr.flush()
        os.execv(str(项目解释器), [str(项目解释器), str(脚本), *参数])
    except Exception:  # noqa: BLE001
        return
