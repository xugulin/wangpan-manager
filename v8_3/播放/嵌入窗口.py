# v8_3/播放/嵌入窗口.py
"""嵌入宿主：**由我们自己**建一个朴素的 X11 子窗口交给 libvlc 画。

为什么需要它（三轮复发的"多出一个 VLC media player 窗口"的真正病根）
====================================================================
libvlc 3 嵌入播放只有一条路：``libvlc_media_player_set_xwindow(窗口号)``。给了句柄之后，
VLC 会去找一个 ``vout window`` 模块（搜索串是 ``"embed-xid,any"``）：
**优先尝试把画面嵌进我们给的窗口；一旦这次尝试失败，它会退到 "any" —— 也就是自己开一个
顶层窗口**（标题就是 "VLC media player"），而且不会报错、不会回退回来。

那么"嵌进去"为什么会失败？实测出来的两个原因（本机 VLC 3.0.23 + Xvfb，2026-09-21）：

1. **窗口还没真的映射到屏幕**：直接把 Qt 控件的 ``winId()`` 交出去，而 ``QWidget.show()``
   之后 X 那边可能还没 map（Qt 说"可见"≠ X 已 map）→ embed 失败 → 自开窗口；
2. **父窗口的"出身"不好**：交给它的是 Qt 的窗口（合成器/主题下常是 ARGB、depth 32、
   自定义 visual）——embed 时要按显示模块的要求挑 visual，挑不到就失败 → 自开窗口。

我们控制不了 VLC 那个 ``,any`` 回退，但**能控制交给它的是哪个窗口**。所以：
自己用 Xlib 建一个**朴素的 24 位 TrueColor 子窗口**、自己 map 并等 ``IsViewable``，
再把它的窗口号交给 VLC。这样一来：

* 映射由我们保证（等到了才交出去）；
* visual 是"最普通的那种"，GL / XVideo / X11 三种输出都能在里面建画布；
* VLC 的 embed 尝试几乎不可能失败 → ``,any`` 那条自开窗口的路走不到。

实测（同一台机器，起播 + 两次重载 + 一次缩放）：

    交给 Qt 控件窗口   → ``matching "embed-xid,any"`` → 有时落到 "any" → 自开窗口
    交给我们自建的子窗口 → ``matching "embed-xid,any"`` → 全部嵌住，3/3 无游离窗口

附带好处：窗口号在**控件被重建/换页**时也不会变来变去（我们持有它），
``set_xwindow`` 的重绑次数大幅减少 —— 而"重绑 + 旧 vout 还没释放"正是另一个诱因。

参考：``v8_3/播放/游离窗口.py``（发现游离窗口）、``播放核心.起播``（起播前一定先停住）。
"""

from __future__ import annotations

import ctypes
import os
from typing import Optional

__all__ = ["可以自建", "不可用原因", "嵌入宿主", "建子窗口", "调整子窗口", "销毁子窗口"]

_库 = None
_加载错误 = ""
_显示 = None


def _载入():
    """加载 libX11（和 游离窗口.py 一样：只依赖系统自带的 X11）。"""
    global _库, _加载错误, _显示
    if _库 is not None or _加载错误:
        return _库
    if not os.environ.get("DISPLAY"):
        _加载错误 = "没有 DISPLAY（不是 X11 会话）"
        return None
    for 名 in ("libX11.so.6", "libX11.so"):
        try:
            _库 = ctypes.CDLL(名)
            break
        except OSError:
            continue
    if _库 is None:
        _加载错误 = "找不到 libX11"
        return None
    L = _库
    L.XOpenDisplay.restype = ctypes.c_void_p
    L.XOpenDisplay.argtypes = [ctypes.c_char_p]
    L.XDefaultScreen.argtypes = [ctypes.c_void_p]
    L.XDefaultScreen.restype = ctypes.c_int
    L.XRootWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
    L.XRootWindow.restype = ctypes.c_ulong
    L.XCreateSimpleWindow.restype = ctypes.c_ulong
    L.XCreateSimpleWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_uint, ctypes.c_uint,
                                    ctypes.c_uint, ctypes.c_ulong, ctypes.c_ulong]
    L.XReparentWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong,
                                ctypes.c_int, ctypes.c_int]
    L.XMapWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    L.XUnmapWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    L.XResizeWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_uint,
                              ctypes.c_uint]
    L.XMoveResizeWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_uint, ctypes.c_uint]
    L.XDestroyWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    L.XFlush.argtypes = [ctypes.c_void_p]
    L.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
    _显示 = L.XOpenDisplay(None)
    if not _显示:
        _加载错误 = "XOpenDisplay 失败（DISPLAY 无效）"
        _库 = None
        return None
    return _库


def 可以自建() -> bool:
    """当前环境能不能自建 X11 子窗口（X11 会话 + 有 libX11）。"""
    return _载入() is not None


def 不可用原因() -> str:
    if _载入() is not None:
        return ""
    return _加载错误 or "未知原因"


def _已映射(窗口号: int) -> bool:
    try:
        from .游离窗口 import 窗口已映射
        return bool(窗口已映射(int(窗口号 or 0)))
    except Exception:  # noqa: BLE001
        return True


def 建子窗口(父窗口号: int, 宽: int = 640, 高: int = 360,
          等映射秒: float = 2.5) -> int:
    """在 ``父窗口号`` 里建一个朴素子窗口并 map，等它 ``IsViewable``；返回窗口号。

    失败返回 0（调用方退回"直接把 Qt 控件窗口交给 libvlc"的老做法）。

    为什么要"自己建"：见模块顶部 —— 交给 VLC 的窗口只要映射没到位或 visual 不寻常，
    它的 embed 尝试就会失败，然后**自己开一个顶层窗口**放画面（而且不报错）。
    """
    L = _载入()
    if L is None or not int(父窗口号 or 0):
        return 0
    import time
    try:
        父 = int(父窗口号)
        屏 = L.XDefaultScreen(_显示)
        根 = L.XRootWindow(_显示, 屏)
        # 黑底、无边框、24 位 TrueColor（用根窗口的默认 visual/depth，最普通的那种）
        子 = int(L.XCreateSimpleWindow(_显示, 根, 0, 0, max(1, int(宽)),
                                    max(1, int(高)), 0, 0, 0))
        if not 子:
            return 0
        L.XReparentWindow(_显示, 子, 父, 0, 0)
        L.XMapWindow(_显示, 子)
        L.XFlush(_显示)
        截止 = time.time() + max(0.1, float(等映射秒))
        while time.time() < 截止:
            if _已映射(子):
                return 子
            time.sleep(0.03)
            # 再 map 一次：父窗口刚上屏时第一次 map 可能被忽略
            L.XMapWindow(_显示, 子)
            L.XFlush(_显示)
        # 等不到也别把没映射的窗口交出去（那正是 VLC 自开窗口的诱因）
        L.XDestroyWindow(_显示, 子)
        L.XFlush(_显示)
        return 0
    except Exception:  # noqa: BLE001
        return 0


def 调整子窗口(窗口号: int, 宽: int, 高: int) -> None:
    L = _载入()
    if L is None or not int(窗口号 or 0):
        return
    try:
        L.XMoveResizeWindow(_显示, int(窗口号), 0, 0, max(1, int(宽)),
                          max(1, int(高)))
        L.XFlush(_显示)
    except Exception:  # noqa: BLE001
        pass


def 销毁子窗口(窗口号: int) -> None:
    L = _载入()
    if L is None or not int(窗口号 or 0):
        return
    try:
        L.XUnmapWindow(_显示, int(窗口号))
        L.XDestroyWindow(_显示, int(窗口号))
        L.XFlush(_显示)
    except Exception:  # noqa: BLE001
        pass


class 嵌入宿主:
    """把"自建子窗口"包成一个跟着 Qt 控件走的对象。

    用法::

        宿主 = 嵌入宿主(视频控件)      # 懒建：真正要用时才建窗口
        句柄 = 宿主.句柄()             # 0 = 没建成（调用方退回 Qt 窗口号）
        ...
        宿主.销毁()

    控件尺寸变化、显示/隐藏都会自动同步（用事件过滤器，不侵入页面代码）。
    """

    def __init__(self, 控件):
        self.控件 = 控件
        self._窗口号 = 0
        self._装的过滤器 = False
        try:
            self._装过滤器()
        except Exception:  # noqa: BLE001
            pass

    # ---------------- 事件过滤器：尺寸/显隐同步 ----------------

    def _装过滤器(self):
        from PySide6.QtCore import QEvent, QObject

        宿主 = self

        class _过滤(QObject):
            def eventFilter(self, 对象, 事件):  # noqa: N802
                try:
                    if 事件.type() in (QEvent.Type.Resize, QEvent.Type.Move,
                                     QEvent.Type.Show):
                        宿主.同步()
                    elif 事件.type() in (QEvent.Type.Hide, QEvent.Type.Close):
                        pass            # 隐藏时不动它：再显示时同步即可
                except Exception:  # noqa: BLE001
                    pass
                return False

        self._过滤器 = _过滤()
        self.控件.installEventFilter(self._过滤器)
        self._装的过滤器 = True

    # ---------------- 句柄 ----------------

    def 控件窗口号(self) -> int:
        try:
            return int(self.控件.winId())
        except Exception:  # noqa: BLE001
            return 0

    def 句柄(self, 重建: bool = False) -> int:
        """拿到"该交给 libvlc"的窗口号（自建子窗口优先，不行就退回控件自己的）。"""
        if not 可以自建():
            return self.控件窗口号()
        if self._窗口号 and not 重建:
            self.同步()
            return self._窗口号
        if 重建 and self._窗口号:
            销毁子窗口(self._窗口号)
            self._窗口号 = 0
        宽, 高 = self._像素尺寸()
        self._窗口号 = 建子窗口(self.控件窗口号(), 宽, 高)
        if not self._窗口号:
            # 自建失败：老实退回控件窗口（并让调用方/日志知道）
            return self.控件窗口号()
        return self._窗口号

    def _像素尺寸(self) -> tuple[int, int]:
        try:
            比 = float(self.控件.devicePixelRatio() or 1.0)
            宽 = max(1, int(self.控件.width() * 比))
            高 = max(1, int(self.控件.height() * 比))
            return 宽, 高
        except Exception:  # noqa: BLE001
            return 640, 360

    def 同步(self) -> None:
        """把子窗口拉到与控件一致的尺寸（控件 resize/move 后调用）。"""
        if not self._窗口号:
            return
        宽, 高 = self._像素尺寸()
        调整子窗口(self._窗口号, 宽, 高)

    def 销毁(self) -> None:
        if self._窗口号:
            销毁子窗口(self._窗口号)
            self._窗口号 = 0
