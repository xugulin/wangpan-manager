# v8_3/界面/全屏助手.py
"""全屏助手：让"全屏"在**不理会 ``showFullScreen()`` 的合成器**上也真的全屏。

真机实测（用户这台机器：COSMIC Wayland + XWayland）：
点播放页的「⛶ 全屏」按钮**什么都没发生** ——
``QWidget.showFullScreen()`` 发出去之后 ``isFullScreen()`` 依然是 False、
窗口几何一点没变（COSMIC 直接忽略了请求）。独立窗口那边之所以"能全屏"，
是因为它自己写了一份"没铺满就自己铺"的兜底；播放页/主窗口没有，于是按钮形同虚设。

所以这里把兜底做成**唯一实现**，播放页、独立播放器窗口都用它：

* :meth:`全屏助手.进入` —— 先请合成器全屏；隔几拍复核，**没真全屏就自己铺满**
  （记住原几何，并置一个"我们自己铺的"标记）；
* :meth:`全屏助手.退出` —— 还原几何（不管是合成器全屏还是我们自己铺的）；
* :meth:`全屏助手.是全屏` —— 合成器状态 or 我们自己铺的标记，两者取或；
* :meth:`全屏助手.装Esc` —— 全屏时按 Esc 退出（需求：Esc 退全屏）。
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QObject, Qt

from .定时 import 安全单发
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QWidget

__all__ = ["全屏助手"]


class 全屏助手(QObject):
    """给一个顶层窗口提供"一定生效"的全屏切换。"""

    def __init__(self, 窗口: QWidget, 退出回调=None):
        super().__init__(窗口)
        self.窗口 = 窗口
        self._自己铺的 = False
        self._原几何 = None
        self._退出回调 = 退出回调 or (lambda: None)
        self._Esc快捷键 = None

    # ---------------- 查询 ----------------

    def 是全屏(self) -> bool:
        try:
            return bool(self.窗口.isFullScreen()) or bool(self._自己铺的)
        except Exception:  # noqa: BLE001
            return bool(self._自己铺的)

    # ---------------- 切换 ----------------

    def 进入(self) -> bool:
        if self.是全屏():
            return True
        try:
            if self._原几何 is None:
                self._原几何 = self.窗口.geometry()
        except Exception:  # noqa: BLE001
            self._原几何 = None
        try:
            self.窗口.showFullScreen()
        except Exception:  # noqa: BLE001
            pass
        # 合成器可能忽略：0/120/400 毫秒各复核一次，没铺满就自己铺。
        # ⚠️ 必须用"挂在 self 名下"的定时器：self 随窗口销毁时，回调绝不能落到
        #    已析构的 C++ 对象上（否则就是 QTimerInfoList::activateTimers 里 SEGV）。
        for 毫秒 in (0, 120, 400):
            安全单发(self, 毫秒, self._复核铺满)
        self.装Esc()
        return True

    def 退出(self) -> bool:
        self._自己铺的 = False
        try:
            self.窗口.showNormal()
        except Exception:  # noqa: BLE001
            pass
        if self._原几何 is not None:
            几何 = self._原几何
            self._原几何 = None
            安全单发(self, 0, self._还原, 几何)
            安全单发(self, 200, self._还原, 几何)
        self._卸Esc()
        try:
            self._退出回调()
        except Exception:  # noqa: BLE001
            pass
        return True

    # ---------------- 内部 ----------------

    def _屏幕(self):
        try:
            屏幕 = (QGuiApplication.screenAt(self.窗口.frameGeometry().center())
                  or QGuiApplication.primaryScreen())
            return 屏幕.geometry() if 屏幕 is not None else None
        except Exception:  # noqa: BLE001
            return None

    def _复核铺满(self) -> None:
        """合成器没全屏就自己铺满（这一步是按钮"有没有反应"的关键）。"""
        try:
            if self.窗口.isFullScreen():
                self._自己铺的 = False
                return
            if not self.窗口.isVisible():
                return
            目标 = self._屏幕()
            if 目标 is None:
                return
            框 = self.窗口.frameGeometry()
            if (框.width() >= 目标.width() - 4 and 框.height() >= 目标.height() - 4
                    and abs(框.x() - 目标.x()) <= 4 and abs(框.y() - 目标.y()) <= 4):
                return                       # 已经铺满了
            self._自己铺的 = True
            self.窗口.setGeometry(目标)
        except Exception:  # noqa: BLE001
            pass

    def _还原(self, 几何) -> None:
        try:
            if self.窗口.isFullScreen():
                self.窗口.showNormal()
            self.窗口.setGeometry(几何)
        except Exception:  # noqa: BLE001
            pass

    # ---------------- Esc ----------------

    def 装Esc(self) -> None:
        try:
            if self._Esc快捷键 is not None:
                return
            from PySide6.QtGui import QKeySequence, QShortcut
            self._Esc快捷键 = QShortcut(QKeySequence(Qt.Key.Key_Escape), self.窗口)
            self._Esc快捷键.setContext(Qt.ShortcutContext.WindowShortcut)
            self._Esc快捷键.activated.connect(self._按了Esc)
        except Exception:  # noqa: BLE001
            self._Esc快捷键 = None

    def _卸Esc(self) -> None:
        try:
            if self._Esc快捷键 is not None:
                self._Esc快捷键.setEnabled(False)
                self._Esc快捷键.deleteLater()
        except Exception:  # noqa: BLE001
            pass
        self._Esc快捷键 = None

    def _按了Esc(self) -> None:
        if self.是全屏():
            self.退出()
