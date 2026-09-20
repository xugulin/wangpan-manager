"""游离窗口：libvlc 偶尔会**自己开一个 VLC 窗口**放视频，这里负责发现并收拾它。

为什么会发生
============
libvlc 3 嵌入播放靠 ``set_xwindow(窗口号)``。如果那一刻我们给的 X11 窗口**还没
映射到屏幕**（刚 ``show()``、WM 还没处理完），VLC 就找不到可用的父窗口，于是
**自己开一个顶层窗口**放画面 —— 用户看到的就是"视频和播放器分离"（标题栏写着
``VLC media player``，我们的窗口一片黑）。它只是**偶尔**发生，所以很难靠时序调试，
必须能在运行时发现并纠正。

怎么发现
========
直接问 X 服务器：列出根窗口的所有子窗口，看有没有名字以 ``VLC media player`` 结尾的
顶层窗口（VLC 自己的窗口标题就是 "<文件名> - VLC media player" 或 "VLC media player"）。
用 ctypes 直连 ``libX11``，不额外依赖任何 Python 包。

怎么收拾
========
1. 先把我们自己的画面**重新绑回来**（调用方做：绑句柄 + 重开媒体 + 跳回原位置）；
2. 若那个游离窗口还在，用标准的 ``WM_DELETE_WINDOW`` 客气体面地请它关闭；
3. 再不行才 ``XDestroyWindow``（它本来就是 libvlc 的 vout 窗口，关掉画布会回到
   我们绑定的窗口 —— 用户自己也验证过"把那个独立窗口关掉，画面就回到 GUI 里"）。
"""

from __future__ import annotations

import ctypes
import os
from typing import Optional

__all__ = ["可用", "不可用原因", "找游离窗口", "请关闭窗口", "销毁窗口",
           "VLC窗口标题尾巴", "VLC窗口类名"]

#: libvlc 自己那个窗口的标题尾巴（VLC 3 默认标题就是 "<文件名> - VLC media player"）
VLC窗口标题尾巴 = "vlc media player"

#: VLC 自己那个窗口的 **WM_CLASS**（比标题可靠：中文界面下标题是
#: "VLC 媒体播放器"，标题匹配会漏；类名始终是 vlc）
VLC窗口类名 = "vlc"

_库 = None
_加载错误 = ""


def _载入():
    global _库, _加载错误
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
    L.XCloseDisplay.argtypes = [ctypes.c_void_p]
    L.XGetWindowAttributes.argtypes = [ctypes.c_void_p, ctypes.c_ulong,
                                     ctypes.c_void_p]
    L.XGetWindowAttributes.restype = ctypes.c_int
    L.XDefaultRootWindow.restype = ctypes.c_ulong
    L.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
    L.XQueryTree.argtypes = [ctypes.c_void_p, ctypes.c_ulong,
                            ctypes.POINTER(ctypes.c_ulong),
                            ctypes.POINTER(ctypes.c_ulong),
                            ctypes.POINTER(ctypes.POINTER(ctypes.c_ulong)),
                            ctypes.POINTER(ctypes.c_uint)]
    L.XQueryTree.restype = ctypes.c_int
    L.XFetchName.argtypes = [ctypes.c_void_p, ctypes.c_ulong,
                            ctypes.POINTER(ctypes.c_char_p)]
    L.XFetchName.restype = ctypes.c_int
    L.XFree.argtypes = [ctypes.c_void_p]
    L.XInternAtom.restype = ctypes.c_ulong
    L.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
    L.XGetWindowProperty.argtypes = [
        ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_long,
        ctypes.c_long, ctypes.c_int, ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_ulong),
        ctypes.POINTER(ctypes.POINTER(ctypes.c_ubyte))]
    L.XGetWindowProperty.restype = ctypes.c_int
    L.XSendEvent.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int,
                            ctypes.c_long, ctypes.c_void_p]
    L.XSendEvent.restype = ctypes.c_int
    L.XDestroyWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    L.XDestroyWindow.restype = ctypes.c_int
    L.XGetClassHint.argtypes = [ctypes.c_void_p, ctypes.c_ulong,
                               ctypes.POINTER(_类提示)]
    L.XGetClassHint.restype = ctypes.c_int
    L.XFlush.argtypes = [ctypes.c_void_p]
    L.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
    return _库


def 可用() -> bool:
    return _载入() is not None


def 不可用原因() -> str:
    _载入()
    return _加载错误


class _类提示(ctypes.Structure):
    # ⚠️ 必须用 c_void_p 存指针：用 c_char_p 的话 ctypes 只给 Python 的 bytes 值，
    # 拿不到原始指针去 XFree —— 传 bytes 给 XFree 会**堆损坏崩溃**（实测
    # "free(): invalid size"）。
    _fields_ = [("res_name", ctypes.c_void_p), ("res_class", ctypes.c_void_p)]


class _窗口属性(ctypes.Structure):
    """XWindowAttributes 的**前缀**（我们只读 width/height，后面字段不关心）。"""

    _fields_ = [("x", ctypes.c_int), ("y", ctypes.c_int),
                ("width", ctypes.c_int), ("height", ctypes.c_int),
                ("border_width", ctypes.c_int), ("depth", ctypes.c_int),
                ("visual", ctypes.c_void_p), ("root", ctypes.c_ulong),
                ("class_", ctypes.c_int), ("bit_gravity", ctypes.c_int),
                ("win_gravity", ctypes.c_int), ("backing_store", ctypes.c_int),
                ("backing_planes", ctypes.c_ulong),
                ("backing_pixel", ctypes.c_ulong), ("save_under", ctypes.c_int),
                ("colormap", ctypes.c_ulong), ("map_installed", ctypes.c_int),
                ("map_state", ctypes.c_int), ("all_event_masks", ctypes.c_long),
                ("your_event_mask", ctypes.c_long),
                ("do_not_propagate_mask", ctypes.c_long),
                ("override_redirect", ctypes.c_int), ("screen", ctypes.c_void_p)]


class _客户端消息数据(ctypes.Union):
    _fields_ = [("b", ctypes.c_char * 20), ("s", ctypes.c_short * 10),
                ("l", ctypes.c_long * 5)]


class _客户端消息(ctypes.Structure):
    _fields_ = [("type", ctypes.c_int), ("serial", ctypes.c_ulong),
                ("send_event", ctypes.c_int), ("display", ctypes.c_void_p),
                ("window", ctypes.c_ulong), ("message_type", ctypes.c_ulong),
                ("format", ctypes.c_int), ("data", _客户端消息数据)]


def _窗口名(显示, 窗口号: int) -> str:
    """取窗口标题：先 WM_NAME，再 _NET_WM_NAME（UTF-8）。"""
    L = _载入()
    名 = ctypes.c_char_p()
    try:
        if L.XFetchName(显示, 窗口号, ctypes.byref(名)) and 名.value:
            文本 = 名.value.decode("utf-8", "replace")
            L.XFree(名)
            if 文本.strip():
                return 文本
    except Exception:  # noqa: BLE001
        pass
    原子 = L.XInternAtom(显示, b"_NET_WM_NAME", 0)
    if not 原子:
        return ""
    类型 = ctypes.c_ulong()
    格式 = ctypes.c_int()
    个数 = ctypes.c_ulong()
    剩余 = ctypes.c_ulong()
    数据 = ctypes.POINTER(ctypes.c_ubyte)()
    状态 = L.XGetWindowProperty(显示, 窗口号, 原子, 0, 1024, 0, 0,
                            ctypes.byref(类型), ctypes.byref(格式),
                            ctypes.byref(个数), ctypes.byref(剩余),
                            ctypes.byref(数据))
    if 状态 != 0 or not 数据:
        return ""
    try:
        return ctypes.string_at(数据, int(个数.value)).decode("utf-8", "replace")
    finally:
        try:
            L.XFree(数据)
        except Exception:  # noqa: BLE001
            pass


def _窗口类名(显示, 窗口号: int) -> str:
    """取窗口的 WM_CLASS（res_class 优先）。VLC 的窗口类名就是 ``vlc``。"""
    L = _载入()
    提示 = _类提示()
    try:
        if not L.XGetClassHint(显示, 窗口号, ctypes.byref(提示)):
            return ""
        指针 = 提示.res_class or 提示.res_name
        if not 指针:
            return ""
        return ctypes.string_at(指针).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return ""
    finally:
        for 字段 in ("res_name", "res_class"):
            指针 = getattr(提示, 字段, None)
            if 指针:
                try:
                    L.XFree(ctypes.c_void_p(指针))
                except Exception:  # noqa: BLE001
                    pass
                setattr(提示, 字段, None)


def 是VLC窗口(类名: str, 标题: str) -> bool:
    """判断是不是 libvlc 自己开的那个播放窗口。

    **以 WM_CLASS 为准**（``vlc``）—— 中文界面下 VLC 的标题是"VLC 媒体播放器"，
    只按英文标题匹配会漏掉（用户实测"分离"仍然发生，就是因为这个）。
    类名拿不到时退回标题匹配（中英文都认）。
    """
    类 = str(类名 or "").strip().lower()
    if 类:
        # 类名可能是 "vlc" 或两行 "vlc\nVlc" 之类，取任意一段判断即可
        for 段 in 类.replace("\n", " ").split():
            if 段 == VLC窗口类名 or 段.startswith(VLC窗口类名):
                return True
        return False
    名 = str(标题 or "").strip().lower()
    if not 名:
        return False
    return (名.endswith(VLC窗口标题尾巴) or "vlc 媒体播放器" in 名
            or 名 == "vlc")


def 窗口尺寸(窗口号: int) -> tuple[int, int]:
    """取窗口宽高（拿不到就 (0, 0)）—— 诊断"那个窗口比屏幕还大"用。"""
    if not 可用():
        return (0, 0)
    L = _载入()
    显示 = L.XOpenDisplay(None)
    if not 显示:
        return (0, 0)
    try:
        属性 = _窗口属性()
        L.XGetWindowAttributes(ctypes.c_void_p(显示), ctypes.c_ulong(窗口号),
                             ctypes.byref(属性))
        return (int(属性.width), int(属性.height))
    except Exception:
        return (0, 0)
    finally:
        try:
            L.XCloseDisplay(ctypes.c_void_p(显示))
        except Exception:
            pass


def 找游离窗口(排除窗口号=()) -> list[tuple[int, str]]:
    """找 libvlc 自己开的那个顶层窗口；返回 ``[(窗口号, 标题)]``。

    :param 排除窗口号: 我们自己的窗口号（Qt 里 ``winId()``），避免误判。
    """
    if not 可用():
        return []
    L = _载入()
    显示 = L.XOpenDisplay(None)
    if not 显示:
        return []
    结果: list[tuple[int, str]] = []
    try:
        根 = L.XDefaultRootWindow(显示)
        根返回 = ctypes.c_ulong()
        父返回 = ctypes.c_ulong()
        子们 = ctypes.POINTER(ctypes.c_ulong)()
        个数 = ctypes.c_uint()
        if not L.XQueryTree(显示, 根, ctypes.byref(根返回), ctypes.byref(父返回),
                          ctypes.byref(子们), ctypes.byref(个数)):
            return []
        try:
            排除 = {int(x) for x in (排除窗口号 or ()) if x}
            for i in range(int(个数.value)):
                窗口号 = int(子们[i])
                if 窗口号 in 排除:
                    continue
                名 = _窗口名(显示, 窗口号)
                类 = _窗口类名(显示, 窗口号)
                if not 名 and not 类:
                    continue
                # WM_CLASS == vlc 是**决定性**依据（中文界面标题会变，类名不变）；
                # 类名拿不到时才退回标题匹配（中文"VLC 媒体播放器"也认）
                if 是VLC窗口(类, 名):
                    结果.append((窗口号, (名 or 类).strip()))
        finally:
            if 子们:
                L.XFree(子们)
    except Exception:  # noqa: BLE001
        return 结果
    finally:
        L.XCloseDisplay(显示)
    return 结果


def 请关闭窗口(窗口号: int) -> bool:
    """发 ``WM_DELETE_WINDOW`` 客气体面地请求关闭（首选）。"""
    if not 可用():
        return False
    L = _载入()
    显示 = L.XOpenDisplay(None)
    if not 显示:
        return False
    try:
        协议 = L.XInternAtom(显示, b"WM_PROTOCOLS", 0)
        删除 = L.XInternAtom(显示, b"WM_DELETE_WINDOW", 0)
        缓冲 = ctypes.create_string_buffer(192)      # XEvent 是 24 个 long 的联合
        事件 = ctypes.cast(缓冲, ctypes.POINTER(_客户端消息)).contents
        事件.type = 33                              # ClientMessage
        事件.serial = 0
        事件.send_event = 1
        事件.display = 显示
        事件.window = int(窗口号)
        事件.message_type = 协议
        事件.format = 32
        事件.data.l[0] = int(删除)
        事件.data.l[1] = 0
        结果 = L.XSendEvent(显示, int(窗口号), 0, 0, ctypes.byref(事件))
        L.XFlush(显示)
        L.XSync(显示, 0)
        return bool(结果)
    except Exception:  # noqa: BLE001
        return False
    finally:
        L.XCloseDisplay(显示)


def 销毁窗口(窗口号: int) -> bool:
    """最后手段：直接销毁那个画布（它只是 libvlc 的 vout 窗口）。"""
    if not 可用():
        return False
    L = _载入()
    显示 = L.XOpenDisplay(None)
    if not 显示:
        return False
    try:
        L.XDestroyWindow(显示, int(窗口号))
        L.XFlush(显示)
        L.XSync(显示, 0)
        return True
    except Exception:  # noqa: BLE001
        return False
    finally:
        L.XCloseDisplay(显示)
