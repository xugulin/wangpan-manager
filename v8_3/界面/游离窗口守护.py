"""游离窗口守护：定期巡检 libvlc 是不是又自己开了窗口，发现就**报告 + 安全回退**。

配合 :mod:`v8_3.播放.游离窗口` 使用：

* 每 3 秒查一次"根窗口下有没有以 ``vlc`` 为 WM_CLASS 的顶层窗口"；
* 发现 → 调宿主给的**发现回调**（宿主做"换回能嵌入的输出并重载"这种安全动作）；
* 没有回调、或回调说"我处理不了" → 只如实写日志（每个窗口最多报 3 次）。

⚠️ 这里**故意不做任何破坏性动作**（不再发 ``WM_DELETE_WINDOW``、更不 ``XDestroyWindow``）：

    那个窗口是 libvlc 正在渲染的画布。从外面把它销毁，VLC 的 vout 线程就废了，
    之后任何 ``停止/释放`` 都要一直等它 —— 用户看到的就是
    "关一下播放，界面彻底卡死"。

所以现在只报告；真正把画面收回来的活，交给宿主用"改回默认视频输出并重载"完成。
"""

from __future__ import annotations

from typing import Callable, Optional, Sequence

from PySide6.QtCore import QObject, QTimer

from ..播放 import 游离窗口

__all__ = ["游离窗口守护"]


class 游离窗口守护(QObject):
    """巡检 + 报告/安全回退（只在 X11 可用时工作，其它平台直接不启动）。"""

    def __init__(self, 套件, 取自己窗口号们: Callable[[], Sequence[int]],
                 自愈回调: Optional[Callable] = None,
                 日志: Optional[Callable] = None,
                 间隔毫秒: int = 3000, 巡检次数: int = 0,
                 发现回调: Optional[Callable] = None):
        super().__init__(套件)
        self._取窗口号 = 取自己窗口号们
        #: 宿主的处理函数（安全动作）。``自愈回调`` 是旧名字，保留兼容。
        self._发现回调 = 发现回调 or 自愈回调
        self._日志 = 日志 or (lambda _t: None)
        self._间隔 = int(间隔毫秒)
        self._剩余次数 = int(巡检次数)
        self._无限 = False
        self._定时器 = QTimer(self)
        self._定时器.setInterval(self._间隔)
        self._定时器.timeout.connect(self._检查)
        self._发现过 = 0
        self._报告过 = 0

    # ---------------- 生命周期 ----------------

    def 开始(self, 无限: bool = False) -> bool:
        """开始巡检；``无限=True`` 时一直盯着（播放期间用，直到 停止()）。"""
        if not 游离窗口.可用():
            self._日志(f"[显示] 游离窗口巡检不可用：{游离窗口.不可用原因()}")
            return False
        self._无限 = bool(无限) or self._剩余次数 <= 0
        if not self._无限:
            self._剩余次数 = max(self._剩余次数, 1)
        self._定时器.start()
        return True

    def 停止(self) -> None:
        try:
            self._定时器.stop()
        except Exception:  # noqa: BLE001
            pass

    def 还在跑(self) -> bool:
        return self._定时器.isActive()

    # ---------------- 巡检 ----------------

    def _检查(self) -> None:
        if not getattr(self, "_无限", False):
            if self._剩余次数 <= 0:
                self._定时器.stop()
                return
            self._剩余次数 -= 1
        try:
            找到 = 游离窗口.找游离窗口(排除窗口号=tuple(self._取窗口号() or ()))
        except Exception as e:  # noqa: BLE001
            self._日志(f"[显示] 游离窗口巡检失败：{e}")
            return
        if not 找到:
            return
        self._发现过 += 1
        标题 = "、".join(名 for _号, 名 in 找到[:2])
        尺寸们 = []
        for _号, _名 in 找到[:2]:
            try:
                宽, 高 = 游离窗口.窗口尺寸(_号)
                尺寸们.append(f"{宽}x{高}")
            except Exception:  # noqa: BLE001
                尺寸们.append("?")
        尺寸 = "、".join(尺寸们)
        if self._发现回调 is not None and self._交给宿主(找到):
            return
        # 宿主没接手 / 已经处理过：只报告（同一窗口最多报 3 次，别刷屏）
        if self._报告过 < 3:
            self._报告过 += 1
            self._日志(f"[显示] ⚠️ libvlc 自己开了窗口放画面（{标题}｜{尺寸}）——"
                     f"本程序**不会**去销毁它（销毁会把 VLC 弄僵、界面跟着卡死），"
                     f"请点「独立窗口」或重新点 ▶ 播放一次")

    def _交给宿主(self, 找到) -> bool:
        """调宿主的处理函数。返回 True 表示"宿主接手了，不用再报告"。"""
        for 参数 in ((找到,), ()):            # 新回调带参数，老回调不带
            try:
                return self._发现回调(*参数) is not False
            except TypeError:
                continue
            except Exception as e:  # noqa: BLE001
                self._日志(f"[显示] 处理游离窗口失败：{e}")
                return True
        return False
