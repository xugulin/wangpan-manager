"""游离窗口守护：定期巡检 libvlc 是否又自己开了窗口，发现就自愈。

配合 :mod:`v8_3.播放.游离窗口` 使用：

* 每 2 秒（前 20 秒）查一次"根窗口下有没有以 ``VLC media player`` 结尾的顶层窗口"；
* 发现 → 调宿主给的**自愈回调**（绑定我们自己的窗口 + 重开媒体 + 跳回原位置）；
* 1.5 秒后如果那个窗口还在 → 发 ``WM_DELETE_WINDOW`` 客气地请它关；
* 再 1.2 秒还在 → ``XDestroyWindow`` 收掉（它只是 libvlc 的 vout 画布，
  关掉之后画面会回到我们绑定的窗口里 —— 用户也验证过这个行为）。
"""

from __future__ import annotations

from typing import Callable, Optional, Sequence

from PySide6.QtCore import QObject, QTimer

from ..播放 import 游离窗口

__all__ = ["游离窗口守护"]


class 游离窗口守护(QObject):
    """巡检 + 自愈（只在 X11 可用时工作，其它平台直接不启动）。"""

    def __init__(self, 套件, 取自己窗口号们: Callable[[], Sequence[int]],
                 自愈回调: Callable[[], None], 日志: Optional[Callable] = None,
                 间隔毫秒: int = 2000, 巡检次数: int = 12):
        super().__init__(套件)
        self._取窗口号 = 取自己窗口号们
        self._自愈 = 自愈回调
        self._日志 = 日志 or (lambda _t: None)
        self._间隔 = int(间隔毫秒)
        self._剩余次数 = int(巡检次数)
        self._无限 = False
        self._定时器 = QTimer(self)
        self._定时器.setInterval(self._间隔)
        self._定时器.timeout.connect(self._检查)
        self._发现过 = 0

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
        找到 = 游离窗口.找游离窗口(排除窗口号=tuple(self._取窗口号() or ()))
        if not 找到:
            return
        self._发现过 += 1
        self._剩余次数 = max(self._剩余次数, 6)   # 发现过就再多盯一会儿
        标题 = "、".join(名 for _号, 名 in 找到[:2])
        self._日志(f"[显示] ⚠️ 发现 libvlc 自己开的窗口（{标题}），"
                 f"正在把画面收回到播放器窗口")
        try:
            self._自愈()
        except Exception as e:  # noqa: BLE001
            self._日志(f"[显示] 画面收回失败：{e}")
        # 自愈没成功（窗口还在）再逐步升级处理
        QTimer.singleShot(1500, self._请它关闭)
        QTimer.singleShot(2800, self._最后销毁)

    def _剩余的游离窗口(self):
        return 游离窗口.找游离窗口(排除窗口号=tuple(self._取窗口号() or ()))

    def _请它关闭(self) -> None:
        剩 = self._剩余的游离窗口()
        if not 剩:
            return
        for 号, _名 in 剩:
            游离窗口.请关闭窗口(号)
        self._日志(f"[显示] 画面仍未收回，已请求关闭 {len(剩)} 个游离窗口")

    def _最后销毁(self) -> None:
        剩 = self._剩余的游离窗口()
        if not 剩:
            return
        for 号, _名 in 剩:
            游离窗口.销毁窗口(号)
        self._日志(f"[显示] 已强制收掉 {len(剩)} 个游离窗口（画面回到播放器里）")
