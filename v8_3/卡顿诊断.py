"""卡顿诊断：找出"界面点一下要等很久"到底卡在哪一行代码。

## 为什么需要它

Windows 实机反馈"GUI 奇卡、点一下卡几分钟"，而 Linux 上同样的操作只要
100~700 毫秒 —— 只看代码猜不出来，必须**在现场抓证据**。

## 它怎么工作

主线程（Qt 界面线程）被卡住时，我们的看门狗线程**照样能跑**，于是：

1. 界面每转一圈事件循环，就往共享变量里写一个"心跳"；
2. 看门狗每秒检查一次心跳：超过 ``卡顿阈值毫秒`` 没更新，就认为主线程卡住了；
3. 卡住时用 ``sys._current_frames()`` 抓**主线程当前的调用栈**，
   连同阻塞时长写进日志 —— 卡在哪一行一目了然（这是标准库就有的能力，不需要额外依赖）；
4. 恢复后再记一条"恢复，共卡了 X 秒"。

## 怎么开

* 命令行：``启动.py --性能诊断``（或 ``--诊断``）；
* 环境变量：``V8_3_卡顿诊断=1``；
* 日志写在 ``数据/卡顿诊断.log``，随时可以直接发出来。

阈值默认 120ms（正常一次事件循环远小于它）。
"""

from __future__ import annotations

import os
import sys
import threading
import time
import traceback
from pathlib import Path

#: 主线程多久没动就算"卡"
卡顿阈值毫秒 = 120
#: 看门狗多久查一次
检查间隔秒 = 0.25

_心跳 = time.monotonic()
_运行中 = False
_线程: threading.Thread | None = None
_日志路径: Path | None = None
_统计 = {"次数": 0, "总卡顿秒": 0.0, "最长秒": 0.0, "最长栈": ""}


def 开着吗() -> bool:
    """诊断开着吗（命令行参数或环境变量任一即可）。"""
    if os.environ.get("V8_3_卡顿诊断", "") in ("1", "true", "True"):
        return True
    return any(x in ("--性能诊断", "--诊断", "--trace-lag") for x in sys.argv)


def 开始(日志路径: Path | str | None = None) -> bool:
    """启动看门狗；返回是否真的起来了。"""
    global _运行中, _线程, _日志路径, _心跳
    if _运行中:
        return True
    try:
        # ⚠️ 本文件是 v8_3/卡顿诊断.py（包的**第一层**），所以项目根是 parents[1]：
        #    parents[2] 会跑到项目外（实测写到了 <项目根>/../数据/ —— 正是
        #    用户反馈"设置不生效/文件找不到"这一类问题的同类根源：**项目根推错一层**）。
        根 = Path(__file__).resolve().parents[1]
        _日志路径 = Path(日志路径) if 日志路径 else (根 / "数据" / "卡顿诊断.log")
        _日志路径.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        return False
    _心跳 = time.monotonic()
    _运行中 = True
    _写(f"==== 卡顿诊断开始 ====")
    _写(f"阈值 {卡顿阈值毫秒}ms｜Python {sys.version.split()[0]}｜平台 {sys.platform}")
    _线程 = threading.Thread(target=_看门狗, name="卡顿诊断", daemon=True)
    _线程.start()
    _装心跳()
    return True


def 停止() -> None:
    global _运行中
    _运行中 = False


def 心跳() -> None:
    """界面每转一圈事件循环就调一次（极轻量：只写一个浮点数）。"""
    global _心跳
    _心跳 = time.monotonic()


def 摘要() -> dict:
    return dict(_统计)


def _写(文本: str) -> None:
    if _日志路径 is None:
        return
    try:
        with _日志路径.open("a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%H:%M:%S')}] {文本}\n")
    except Exception:
        pass


def _装心跳() -> None:
    """把心跳挂到 Qt 的事件循环上（每处理完一批事件就 ping 一下）。"""
    try:
        from PySide6.QtCore import QObject, QTimer
    except Exception:
        return

    class _心跳器(QObject):
        def __init__(self):
            super().__init__()
            self._计时 = QTimer(self)
            self._计时.setInterval(50)
            self._计时.timeout.connect(心跳)
            self._计时.start()

    # 持有引用，别被回收
    global _心跳对象
    _心跳对象 = _心跳器()


_心跳对象 = None


def _主线程栈() -> str:
    try:
        frame = sys._current_frames().get(threading.main_thread().ident)
        if frame is None:
            return "（拿不到主线程栈）"
        行们 = traceback.format_stack(frame, limit=18)
        return "".join(行们[-18:])
    except Exception as e:  # noqa: BLE001
        return f"（抓栈失败：{e}）"


def _看门狗() -> None:
    global _心跳
    上次卡 = 0.0
    while _运行中:
        time.sleep(检查间隔秒)
        现在 = time.monotonic()
        静默 = 现在 - _心跳
        if 静默 * 1000 >= 卡顿阈值毫秒:
            # 还在卡：先记一条"开始卡"，之后每 2 秒补一条进度
            if 上次卡 == 0.0:
                上次卡 = 现在
                _写(f"⚠️ 界面卡住（已 {静默 * 1000:.0f}ms）——主线程当前在：\n{_主线程栈()}")
            elif 现在 - 上次卡 >= 2.0:
                上次卡 = 现在
                _写(f"   …仍在卡（已 {静默:.1f}s）；主线程还在：\n{_主线程栈()}")
            continue
        if 上次卡 != 0.0:
            共 = 现在 - 上次卡
            _统计["次数"] += 1
            _统计["总卡顿秒"] += 共
            if 共 > _统计["最长秒"]:
                _统计["最长秒"] = 共
                _统计["最长栈"] = _主线程栈()
            _写(f"✅ 恢复：这次卡了 {共:.2f} 秒")
            上次卡 = 0.0
