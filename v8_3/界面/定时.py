# v8_3/界面/定时.py
"""安全单次定时：**绑定到宿主对象**的 ``QTimer.singleShot`` 替代品。

为什么要这个（真机 coredump 教训）
==================================
``QTimer.singleShot(毫秒, self.某方法)`` 只保存了一个 Python 可调用对象，
**不持有宿主对象**。窗口/页面在这次定时到点之前被关掉/销毁，
到点时 Qt 就会往一个已经析构的 C++ 对象上派发事件 —— 崩溃栈长这样::

    QCoreApplication::notifyInternal2
    QTimerInfoList::activateTimers
    ...
    SEGV

本机测试套件里就复现过一次（同一份代码第二次跑又过了，典型的"销毁竞态"），
用户那边也报过"播放视频导致程序崩溃"。所以：**凡是要延迟回调到某个窗口/控件上的，
都用 :func:`安全单发`** —— 它建的 QTimer 以宿主为父对象，宿主一销毁，定时器跟着
销毁，回调绝不会落到已析构的对象上。

用法::

    from .定时 import 安全单发
    安全单发(self, 900, self._跳到续播位置, 秒)
    for 毫秒 in (0, 120, 400):
        安全单发(self, 毫秒, self._复核)
"""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QTimer

__all__ = ["安全单发", "安全单发一串"]


def 安全单发(宿主, 毫秒: int, 回调: Callable, *参数) -> QTimer | None:
    """延迟调用 ``回调``，并把定时器挂在 ``宿主`` 名下（宿主没了就自动取消）。"""
    if 宿主 is None:
        return None
    try:
        定时 = QTimer(宿主)
        定时.setSingleShot(True)

        # ⚠️ 先**安全地**把定时器自己回收掉再执行回调：
        #    回调里很可能把宿主窗口/页面关掉（父对象一死，这个 QTimer 就没了），
        #    而 Qt 此刻正处在"这个定时器自己的 timerEvent"里继续往回走 ——
        #    实测会崩在 `QTimerInfoList::activateTimers → notifyInternal2`（SEGV）。
        #    用 deleteLater 让回收发生在事件循环的安全点上，就没有这个问题。
        #    注意参数要**从闭包带过来**（timeout 信号不带参数，写 `回调(*_a)` 会丢参）。
        def 到点了():
            try:
                定时.deleteLater()
            except Exception:  # noqa: BLE001
                pass
            回调(*参数)

        定时.timeout.connect(到点了)
        定时.start(max(0, int(毫秒)))
        return 定时
    except Exception:  # noqa: BLE001 - 定时只是优化，失败不该影响主流程
        return None


def 安全单发一串(宿主, 毫秒们, 回调: Callable, *参数) -> list[QTimer]:
    """一批延迟（例如 0/120/400 毫秒各复核一次），全部挂在宿主名下。"""
    结果 = []
    for 毫秒 in 毫秒们 or (0,):
        定时 = 安全单发(宿主, 毫秒, 回调, *参数)
        if 定时 is not None:
            结果.append(定时)
    return 结果
