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
* **Linux/X11**：直接问 X 服务器 —— 列出根窗口的所有子窗口，看有没有名字以
  ``VLC media player`` 结尾的顶层窗口；类名 ``vlc`` 更可靠（中文界面标题会变）；
* **Windows**：``EnumWindows`` 枚举本进程的顶层窗口，看有没有**不是我们 Qt 窗口**的
  可见顶层窗口（VLC 在 Windows 上自己开窗口时类名是 ``VLC video output`` /
  标题带 ``VLC``）；同一个判断也用来做"画面到底在不在我们窗口里"的自检
  （见 :func:`画面在我们窗口里`）。

Windows 这条是**必须**的：用户实测 Windows 版"能播了，但视频游离在 GUI 之外"，
而当时这里只支持 X11 → 守护整个是空转的，没人去发现和纠正。

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
           "VLC窗口标题尾巴", "VLC窗口类名", "窗口已映射", "窗口尺寸",
           "映射状态", "画面在我们窗口里", "平台说明"]

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
    """当前平台能不能发现游离窗口（X11 或 Windows 都行）。"""
    if os.name == "nt":
        return _载入win() is not None
    return _载入() is not None


def 不可用原因() -> str:
    if os.name == "nt":
        _载入win()
        return _win加载错误
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


# ==================== Windows 后端 ====================

#: VLC 在 Windows 上自己那个视频窗口的类名（VLC 3.x 用 direct3d/gl 输出时都是它）
Windows_VLC类名们 = ("vlc video output", "vlc", "videolan")

_user32 = None
_win加载错误 = ""


def _载入win():
    """Windows 上载入 user32（找不到就说明不是 Windows）。"""
    global _user32, _win加载错误
    if _user32 is not None or _win加载错误:
        return _user32
    if os.name != "nt":
        _win加载错误 = "不是 Windows"
        return None
    try:
        import ctypes as _c
        _user32 = _c.WinDLL("user32", use_last_error=True)
    except Exception as 错:  # noqa: BLE001
        _win加载错误 = f"载入 user32 失败：{错}"
        return None
    return _user32


def _win窗口文本(号: int) -> str:
    用户 = _载入win()
    if 用户 is None:
        return ""
    try:
        长度 = int(用户.GetWindowTextLengthW(int(号)) or 0)
        if 长度 <= 0:
            return ""
        缓冲 = ctypes.create_unicode_buffer(长度 + 1)
        用户.GetWindowTextW(int(号), 缓冲, 长度 + 1)
        return 缓冲.value or ""
    except Exception:  # noqa: BLE001
        return ""


def _win窗口类名(号: int) -> str:
    用户 = _载入win()
    if 用户 is None:
        return ""
    try:
        缓冲 = ctypes.create_unicode_buffer(256)
        用户.GetClassNameW(int(号), 缓冲, 256)
        return 缓冲.value or ""
    except Exception:  # noqa: BLE001
        return ""


def _win本进程窗口们() -> list[int]:
    """本进程所有顶层窗口（含不可见的）。"""
    用户 = _载入win()
    if 用户 is None:
        return []
    import ctypes as _c
    结果: list[int] = []

    def _回调(号, _参数):
        pid = _c.c_ulong()
        用户.GetWindowThreadProcessId(int(号), _c.byref(pid))
        if int(pid.value) == os.getpid():
            结果.append(int(号))
        return True

    原型 = _c.WINFUNCTYPE(_c.c_bool, _c.c_void_p, _c.c_void_p)
    用户.EnumWindows(原型(_回调), 0)
    return 结果


def _win是VLC窗口(号: int) -> bool:
    类名 = _win窗口类名(号).strip().lower()
    标题 = _win窗口文本(号).strip().lower()
    if any(名 in 类名 for 名 in Windows_VLC类名们):
        return True
    return "vlc" in 标题 and ("media player" in 标题 or "媒体播放器" in 标题
                          or 标题.endswith("vlc"))


def 平台说明() -> str:
    """给日志用的一句话：当前平台靠什么发现游离窗口。"""
    if os.name == "nt":
        return "Windows（EnumWindows 枚举本进程顶层窗口）"
    if os.environ.get("DISPLAY"):
        return "X11（XQueryTree 枚举根窗口子窗口）"
    return "当前平台没有可用的窗口枚举能力"


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


def _win可见(号: int) -> bool:
    用户 = _载入win()
    if 用户 is None:
        return True
    try:
        return bool(用户.IsWindow(int(号))) and bool(用户.IsWindowVisible(int(号)))
    except Exception:  # noqa: BLE001
        return True


def 映射状态(窗口号: int) -> int:
    """问 X：这个窗口映射了没有。0=没映射 1=没映射但可映射 2=已映射；-1=拿不到。"""
    if not 窗口号 or not 可用():
        return -1
    L = _载入()
    显示 = L.XOpenDisplay(None)
    if not 显示:
        return -1
    try:
        属性 = _窗口属性()
        L.XGetWindowAttributes(ctypes.c_void_p(显示), ctypes.c_ulong(窗口号),
                             ctypes.byref(属性))
        return int(属性.map_state)
    except Exception:
        return -1
    finally:
        try:
            L.XCloseDisplay(ctypes.c_void_p(显示))
        except Exception:
            pass


def 窗口已映射(窗口号: int) -> bool:
    """这个 X 窗口是不是**真的**已经在屏幕上了（map_state == IsViewable）。

    ⚠️ 为什么要问 X 而不是信 Qt：Qt 的 ``isVisible()/isExposed()`` 说"可见"，
    不代表 X 服务器已经把它映射好。libvlc 在那一刻拿不到可用父窗口，就会
    **自己开一个顶层窗口**放画面（用户实测两次："又多了一个 VLC media player 在放"）。
    拿不到状态时返回 True（宁可照旧，也别把正常环境卡住）。
    """
    if os.name == "nt":
        # Windows 没有 map_state：IsWindow + IsWindowVisible 就是最好的等价物
        # （用户实测的问题正是"窗口还没真的显示就把 HWND 交给 VLC" → VLC 自己开窗口）
        return _win可见(int(窗口号 or 0))
    状态 = 映射状态(窗口号)
    if 状态 < 0:
        return True
    return 状态 == 2


def 窗口尺寸(窗口号: int) -> tuple[int, int]:
    """取窗口宽高（拿不到就 (0, 0)）—— 诊断"那个窗口比屏幕还大"用。"""
    if os.name == "nt":
        用户 = _载入win()
        if 用户 is None:
            return (0, 0)
        try:
            import ctypes as _c

            class _矩形(_c.Structure):
                _fields_ = [("左", _c.c_long), ("上", _c.c_long),
                            ("右", _c.c_long), ("下", _c.c_long)]
            矩 = _矩形()
            if not 用户.GetWindowRect(int(窗口号), _c.byref(矩)):
                return (0, 0)
            return (int(矩.右 - 矩.左), int(矩.下 - 矩.上))
        except Exception:  # noqa: BLE001
            return (0, 0)
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
    if os.name == "nt":
        # Windows：枚举本进程的顶层窗口，挑出**不是我们 Qt 窗口**的可见大窗口。
        # 为什么不只按类名/标题匹配：VLC 自己开窗时的类名/标题会随版本与语言变
        # （实测中文界面标题是"VLC 媒体播放器"），而"本进程里多出来的那个顶层窗口"
        # 这个判据不依赖它的名字，最稳。
        try:
            排除 = {int(x) for x in (排除窗口号 or ()) if x}
        except Exception:  # noqa: BLE001
            排除 = set()
        用户 = _载入win()
        if 用户 is None:
            return []
        结果: list[tuple[int, str]] = []
        for 号 in _win本进程窗口们():
            if 号 in 排除:
                continue
            if not _win可见(号):
                continue
            类 = _win窗口类名(号)
            if 类.lower().startswith("qt"):      # Qt 自己的窗口（含各种辅助窗）
                continue
            宽, 高 = 窗口尺寸(号)
            if 宽 < 160 or 高 < 120:             # 太小的多半是提示/气泡/隐藏助手窗
                continue
            名 = _win窗口文本(号)
            尾巴 = "（VLC 的窗口）" if _win是VLC窗口(号) else ""
            结果.append((号, f"{(名 or 类).strip() or '未知窗口'}{尾巴}"))
        return 结果
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
    """客气体面地请窗口关闭（Windows: WM_CLOSE；X11: WM_DELETE_WINDOW）。"""
    if os.name == "nt":
        用户 = _载入win()
        if 用户 is None:
            return False
        try:
            return bool(用户.PostMessageW(int(窗口号), 0x0010, 0, 0))   # WM_CLOSE
        except Exception:  # noqa: BLE001
            return False
    return _请关闭窗口X11(窗口号)


def _请关闭窗口X11(窗口号: int) -> bool:
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


def _子窗口们(L, 显示, 窗口号: int) -> list[int]:
    """取一个窗口的直接子窗口。"""
    根 = ctypes.c_ulong(); 父 = ctypes.c_ulong()
    子们 = ctypes.POINTER(ctypes.c_ulong)()
    个数 = ctypes.c_uint()
    出: list[int] = []
    try:
        if L.XQueryTree(显示, int(窗口号), ctypes.byref(根), ctypes.byref(父),
                      ctypes.byref(子们), ctypes.byref(个数)):
            for i in range(int(个数.value)):
                出.append(int(子们[i]))
    except Exception:
        pass
    finally:
        if 子们:
            try:
                L.XFree(子们)
            except Exception:
                pass
    return 出


def 销毁窗口(窗口号: int) -> bool:
    """最后手段：直接销毁（Windows: DestroyWindow；X11: XDestroyWindow）。"""
    if os.name == "nt":
        用户 = _载入win()
        if 用户 is None:
            return False
        try:
            return bool(用户.DestroyWindow(int(窗口号)))
        except Exception:  # noqa: BLE001
            return False
    return _销毁窗口X11(窗口号)


def _销毁窗口X11(窗口号: int) -> bool:
    """最后手段：销毁那块画布（**连子窗口一起**）。

    只销毁父窗口有时会留下 vout 子窗口（用户会看到"窗口没了但画面还在"），
    所以先把直接子窗口逐个销毁，再销毁它自己。
    """
    if not 可用():
        return False
    L = _载入()
    显示 = L.XOpenDisplay(None)
    if not 显示:
        return False
    try:
        for 子 in _子窗口们(L, 显示, 窗口号):
            try:
                L.XDestroyWindow(显示, int(子))
            except Exception:
                pass
        L.XDestroyWindow(显示, int(窗口号))
        L.XFlush(显示)
        L.XSync(显示, 0)
        return True
    except Exception:  # noqa: BLE001
        return False
    finally:
        L.XCloseDisplay(显示)


def 画面在我们窗口里(视频窗口号: int, 排除窗口号=()) -> bool:
    """libvlc 的画面**真的**画在我们给的窗口里吗？

    判据（两端都能用、且和"用户看到什么"一致）：

    * 我们给出去的那个窗口**在屏幕上**（:func:`窗口已映射`）；
    * 本进程里**没有多出来的顶层窗口**（:func:`找游离窗口` 为空）——
      有游离窗口就说明画面跑到 VLC 自己开的窗口里去了（用户实测的
      "视频游离在 GUI 之外"就是这个）。

    为什么不用"我们的窗口里有没有子窗口"来判断：VLC 的 ``xcb_window`` 输出是
    **直接画进给定窗口**、不一定建子窗口（真机日志实测：``using vout window
    module "xcb_window"`` 之后我们那个窗口依然是空的 → 会误判成"不在我们窗口里"）。
    """
    号 = int(视频窗口号 or 0)
    if not 号:
        return False
    if not 窗口已映射(号):
        return False
    try:
        排除 = (号,) + tuple(排除窗口号 or ())
    except Exception:  # noqa: BLE001
        排除 = (号,)
    return not 找游离窗口(排除窗口号=排除)


def 清干净游离窗口(最多轮: int = 3) -> int:
    """反复"找 → 销毁"，直到再也找不到（返回销毁总数）。

    为什么需要"反复"：X 的窗口层次里，frame 与真正的 vout 可能是两个不同的顶层窗口，
    一轮（只销毁我们扫到的那一个）有时会留下另一个。**这条路径不做自愈**，
    只负责把不合时宜的窗口清掉 —— 播放器画面本身在我们自己的窗口里。
    """
    if not 可用():
        return 0
    总数 = 0
    for _ in range(max(1, int(最多轮))):
        剩 = 找游离窗口()
        if not 剩:
            break
        for 号, _名 in 剩:
            if 销毁窗口(号):
                总数 += 1
        # 给 X 一点时间真的处理掉
        try:
            import time as _t
            _t.sleep(0.2)
        except Exception:
            pass
    return 总数
