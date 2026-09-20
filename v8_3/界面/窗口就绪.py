"""窗口就绪判断：**唯一的判定入口**（X11 嵌入 libvlc 前必须过这一关）。

## 为什么需要这个模块（两次踩坑的结论）

libvlc 3.x 嵌入播放靠 ``libvlc_media_player_set_xwindow``。**如果那一刻给的 X11
窗口还没有映射到屏幕**，libvlc 找不到可用父窗口，就会**自己开一个顶层窗口**放画面 ——
用户看到的就是"多出来一个 `VLC media player` 窗口在放同一个视频"（实测反馈过三次）。

而 Qt 的 ``QWidget.isVisible()`` / ``QWindow.isExposed()`` **说"可见"并不代表
X 服务器已经把窗口映射好了**。实测（Xvfb 真显示）：

```
① 还没 show 的窗口 → 窗口已映射: False ｜ X map_state: 0
② show 之后        → 窗口已映射: True  ｜ X map_state: 2
```

所以判定必须以 **X 的 ``map_state == IsViewable(2)``** 为准，Qt 状态只作兜底。

## 用法

```python
from .窗口就绪 import 窗口就绪

if not 窗口就绪(self.视频, 重试上限=20):      # 会自己等（每次 60ms）
    ...                                       # 还没就绪就别把句柄交给 libvlc
```

* 没有 X11 可用 / 非 xcb 平台 → 直接返回 True（离屏、Wayland 没有"嵌入"这回事，
  别把起播卡死）；
* ``拿不到映射状态`` 时返回 True（宁可照旧，也别把正常环境卡住）。
"""

from __future__ import annotations

#: 每次等待之间睡多久（毫秒）
等待毫秒 = 60


def 可嵌入(Qt部件) -> bool:
    """这个部件在当前平台能不能被 libvlc 嵌入（X11/xcb 才行）。"""
    try:
        from PySide6.QtGui import QGuiApplication
        from ..播放.显示环境 import 可嵌入窗口
        return bool(可嵌入窗口(QGuiApplication.platformName()))
    except Exception:
        return True


def X窗口号(Qt部件) -> int:
    """取部件的 X11 窗口号（拿不到/平台不对 → 0）。"""
    try:
        if not 可嵌入(Qt部件):
            return 0
        return int(Qt部件.winId())
    except Exception:
        return 0


def 已映射(窗口号: int) -> bool:
    """X 服务器认为这个窗口已经在屏幕上了吗（拿不到状态时返回 True）。"""
    try:
        from ..播放.游离窗口 import 窗口已映射
        return bool(窗口已映射(int(窗口号 or 0)))
    except Exception:
        return True


def 窗口就绪(Qt部件, 重试上限: int = 20, 每次毫秒: int = 0) -> bool:
    """部件的窗口是不是**真的**可以交给 libvlc 了。

    :param 重试上限: 最多等几轮（每轮 ``每次毫秒`` 毫秒，默认 60ms）；
        设为 0 表示"只看一眼，不等"。
    """
    import time as _t
    间隔 = (每次毫秒 or 等待毫秒) / 1000.0
    for 第次 in range(max(0, int(重试上限)) + 1):
        就绪 = False
        try:
            if not 可嵌入(Qt部件):
                return True                     # 离屏/Wayland：没有嵌入这回事
            可见 = bool(getattr(Qt部件, "isVisible", lambda: True)())
            if not 可见:
                就绪 = False
            else:
                号 = X窗口号(Qt部件)
                if 号:
                    就绪 = 已映射(号)
                else:
                    # 没有 X11 窗口号：退回 Qt 的 isExposed（拿不到就当就绪）
                    句柄 = None
                    try:
                        句柄 = Qt部件.windowHandle()
                    except Exception:
                        句柄 = None
                    就绪 = True if 句柄 is None or not hasattr(句柄, "isExposed") \
                        else bool(句柄.isExposed())
        except Exception:
            return True
        if 就绪:
            return True
        if 第次 < int(重试上限):
            _t.sleep(间隔)
    return False
