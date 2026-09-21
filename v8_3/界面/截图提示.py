# v8_3/界面/截图提示.py
"""截图保存提示：**透明浮层**，显示保存位置和图片名（用户要求）。

用户原话："视频截图时应该弹出透明的截图保存位置和图片名的提示。"

这个浮层试过四种做法，前三种在真机（COSMIC Wayland + XWayland）上都不成立 ——
注释留着，免得以后又走回去：

1. ``QLabel`` + ``QGraphicsOpacityEffect`` + ``WA_TranslucentBackground``：
   **画不出来**，真机上只剩一块纯色方块、文字全不见，``grab()`` 出来是空白；
2. **普通子控件**：画面是 libvlc 画在视频控件的**原生 X11 子窗口**里的，而 X11 里
   原生子窗口永远盖在父窗口自绘内容之上 → 提示被画面整个盖住（抓图证实）；
3. **原生子窗口**（给提示自己也开 ``WA_NativeWindow``）：能浮上去了，但会连累
   视频那个原生窗口被重建 → **画面跑到 libvlc 自己开的窗口里**（真机复现 ✗）；
4. ✅ 现在这版：**无边框 + 绕过窗口管理器（override-redirect）+ 置顶 + 半透明**的
   顶层浮窗，自己用 QPainter 画圆角半透明底和三行字。绕过窗口管理器意味着位置
   由我们说了算（普通顶层浮窗会被 COSMIC 随便挪，实测想放右下角却跑到别处），
   而且必然浮在画面之上；``WA_TranslucentBackground`` 在 XWayland 上是真 ARGB，
   画面能透出来（真机抓图确认 ✅）。

特性（都按需求）：

* **透明**：半透明深色圆角底（alpha 205/255），不挡画面；
* 文案含**保存位置**（完整目录）与**图片名**（单独一行、绿色加粗，最醒目）；
* 自动淡出（默认 4.2 秒，淡出 0.7 秒），鼠标穿透（不挡视频区的双击/右键）；
* 贴着视频区右下角，窗口缩放时跟着走；
* 只此一处实现：播放页与独立播放器窗口共用。
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import (Property, QByteArray, QEasingCurve, QPoint,
                          QPropertyAnimation, Qt, QTimer)
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import QWidget

__all__ = ["截图提示"]

#: 停留多久后开始淡出（毫秒）
默认停留毫秒 = 4200
#: 淡出时长（毫秒）
默认淡出毫秒 = 700
#: 淡入时长（毫秒）
默认淡入毫秒 = 160
#: 内边距与行距
_内边距 = (14, 10, 14, 10)
_行距 = 4


class 截图提示(QWidget):
    """透明浮层：截图保存位置 + 图片名（自己画，见模块说明）。"""

    def __init__(self, 父窗口: QWidget, 视频控件: Optional[QWidget] = None,
                 停留毫秒: int = 默认停留毫秒):
        super().__init__(None)
        self.视频控件 = 视频控件
        self.宿主 = 父窗口
        self.停留毫秒 = int(停留毫秒)
        # 无边框 + 绕过窗口管理器：位置我们说了算，且一定浮在画面之上
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.X11BypassWindowManagerHint
                            | Qt.WindowType.WindowStaysOnTopHint
                            | Qt.WindowType.NoDropShadowWindowHint)
        # ⚠️ 为什么是**独立顶层浮窗**而不是普通子控件（真机实测踩过）：
        #    画面是交给 libvlc 画在视频控件的**原生 X11 子窗口**里的（见 播放出口.py），
        #    而 X11 里原生子窗口**永远盖在父窗口自绘内容之上** —— 普通子控件形态的
        #    提示会被视频整个盖住，屏幕上根本看不到（抓图也证实了）。
        #    所以这里做成"无边框 + 半透明 + 置顶 + 鼠标穿透"的工具窗，浮在画面之上。
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        # 不吃鼠标：否则挡住视频区的双击全屏 / 右键菜单
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        self._标题 = "截图已保存"
        self._图标 = "📷"
        self._名字 = ""
        self._位置 = ""
        self._透明度 = 0.0
        self._尺寸 = (240, 78)
        self._计算尺寸()

        self._动画 = QPropertyAnimation(self, QByteArray(b"\xe9\x80\x8f\xe6\x98\x8e\xe5\xba\xa6"), self)
        self._动画.setEasingCurve(QEasingCurve.Type.InOutQuad)
        self._收尾 = QTimer(self)
        self._收尾.setSingleShot(True)
        self._收尾.timeout.connect(self.淡出)
        self.setVisible(False)
        def _宿主没了(*_):
            try:                              # 浮层的 C++ 对象可能已经先被删了
                self.close()
            except Exception:  # noqa: BLE001
                pass
        try:                                  # 宿主窗口没了就跟着收掉浮层
            父窗口.destroyed.connect(_宿主没了)
        except Exception:  # noqa: BLE001
            pass

    # ---------------- 属性（给动画用） ----------------

    def _取透明度(self) -> float:
        return float(self._透明度)

    def _设透明度(self, 值: float) -> None:
        self._透明度 = max(0.0, min(1.0, float(值)))
        self.update()

    透明度 = Property(float, _取透明度, _设透明度)

    # ---------------- 对外 ----------------

    def 显示已保存(self, 路径) -> None:
        """显示"已保存 + 位置 + 图片名"（透明浮层，自动淡出）。"""
        目标 = Path(str(路径))
        self._标题 = "截图已保存"
        self._图标 = "📷"
        self._名字 = 目标.name or str(路径)
        self._位置 = f"保存在：{目标.parent}"
        self._弹出()

    def 显示失败(self, 原因: str = "可能还没出画面") -> None:
        self._标题 = "截图失败"
        self._图标 = "⚠️"
        self._名字 = str(原因)
        self._位置 = ""
        self._弹出()

    def 关闭提示(self) -> None:
        self._收尾.stop()
        self.淡出()

    # ---------------- 内部 ----------------

    def _字体们(self):
        小 = QFont(self.font())
        小.setPointSizeF(max(8.0, 小.pointSizeF() - 1.0))
        名 = QFont(self.font())
        名.setPointSizeF(max(9.0, 名.pointSizeF()))
        名.setBold(True)
        标题 = QFont(self.font())
        标题.setPointSizeF(max(9.0, 标题.pointSizeF()))
        标题.setBold(True)
        return 标题, 名, 小

    def _计算尺寸(self) -> None:
        标题字体, 名字体, 位置字体 = self._字体们()
        标题尺 = QFontMetrics(标题字体)
        名字尺 = QFontMetrics(名字体)
        位置尺 = QFontMetrics(位置字体)
        左, 上, 右, 下 = _内边距
        第一行 = 标题尺.horizontalAdvance(f"{self._图标} {self._标题}")
        第二行 = 名字尺.horizontalAdvance(self._名字)
        第三行 = 位置尺.horizontalAdvance(self._位置) if self._位置 else 0
        宽 = max(220, 第一行, 第二行, 第三行) + 左 + 右
        高 = (标题尺.height() + 名字尺.height()
             + (位置尺.height() if self._位置 else 0)
             + _行距 * 2 + 上 + 下)
        self._尺寸 = (int(宽), int(高))
        self.resize(*self._尺寸)

    def _弹出(self) -> None:
        self._计算尺寸()
        self.调整大小与位置()
        self.setVisible(True)
        self.raise_()
        try:                              # 置顶浮窗：别被别的窗口压下去
            self.activateWindow() if False else None
        except Exception:  # noqa: BLE001
            pass
        self._动画.stop()
        self._动画.setDuration(默认淡入毫秒)
        self._动画.setStartValue(float(self._透明度))
        self._动画.setEndValue(1.0)
        self._动画.start()
        self._收尾.start(max(600, self.停留毫秒))

    def 淡出(self) -> None:
        self._动画.stop()
        self._动画.setDuration(默认淡出毫秒)
        self._动画.setStartValue(float(self._透明度))
        self._动画.setEndValue(0.0)
        try:
            self._动画.finished.connect(self._淡出结束)
        except Exception:  # noqa: BLE001
            pass
        self._动画.start()

    def _淡出结束(self) -> None:
        try:
            self._动画.finished.disconnect(self._淡出结束)
        except Exception:  # noqa: BLE001
            pass
        if float(self._透明度) <= 0.01:
            self.setVisible(False)

    def 调整大小与位置(self) -> None:
        """贴到"视频区**真正露出来**的那块"的右下角（全局坐标）。"""
        父 = getattr(self, "宿主", None)
        if 父 is None:
            return
        宽, 高 = self._尺寸
        self.resize(宽, 高)
        参考 = (self.视频控件 if (self.视频控件 is not None and self.视频控件.isVisible())
              else 父)
        # ⚠️ 必须用**真正露出来**的那块区域（visibleRegion）：播放页的视频控件在
        #    滚动区/分割器里，geometry 可能比可见视口还宽 —— 按 geometry 算右下角
        #    会把提示挪到视口外面（真机实测：提示跑到窗口右边看不见了）。
        try:
            可见 = 参考.visibleRegion().boundingRect()
        except Exception:  # noqa: BLE001
            可见 = None
        try:
            if 可见 is not None and not 可见.isEmpty():
                右下 = 参考.mapToGlobal(可见.bottomRight())
                左上 = 参考.mapToGlobal(可见.topLeft())
            else:
                左上 = 参考.mapToGlobal(QPoint(0, 0))
                右下 = 参考.mapToGlobal(QPoint(参考.width(), 参考.height()))
        except Exception:  # noqa: BLE001
            左上 = 父.mapToGlobal(QPoint(0, 0))
            右下 = 父.mapToGlobal(QPoint(父.width(), 父.height()))
        边距 = 18
        x = min(右下.x() - 宽 - 边距, 右下.x() - 宽 - 4)
        y = 右下.y() - 高 - 边距
        x = max(左上.x() + 4, x)
        y = max(左上.y() + 4, min(y, 右下.y() - 高 - 4))
        self.move(int(x), int(y))

    def 父窗口(self):
        try:
            return self.视频控件.window() if self.视频控件 is not None else self.parent()
        except Exception:  # noqa: BLE001
            return self.parent()

    def 重新置顶(self) -> None:
        """把提示窗重新提到画面之上（原生子窗口要显式 raise 才压在视频窗口上面）。"""
        try:
            if self.isVisible():
                self.raise_()
        except Exception:  # noqa: BLE001
            pass

    # ---------------- 绘制 ----------------

    def paintEvent(self, _事件):  # noqa: N802 - Qt 命名
        画 = QPainter(self)
        画.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        画.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        画.setOpacity(max(0.0, min(1.0, float(self._透明度))))
        矩形 = self.rect().adjusted(0, 0, -1, -1)
        # 半透明深色圆角底 —— "透明提示"但不糊字
        画.setBrush(QColor(18, 20, 26, 200))
        画.setPen(QPen(QColor(255, 255, 255, 70), 1))
        画.drawRoundedRect(矩形, 10, 10)

        标题字体, 名字体, 位置字体 = self._字体们()
        标题尺 = QFontMetrics(标题字体)
        名字尺 = QFontMetrics(名字体)
        位置尺 = QFontMetrics(位置字体)
        左, 上, _右, _下 = _内边距
        x = 左
        y = 上

        # 第一行：📷 截图已保存
        画.setFont(标题字体)
        画.setPen(QColor(255, 255, 255, 235))
        画.drawText(x, y + 标题尺.ascent(), f"{self._图标} {self._标题}")
        y += 标题尺.height() + _行距

        # 第二行：图片名（绿色加粗，最醒目）
        画.setFont(名字体)
        画.setPen(QColor(140, 233, 154, 255))
        画.drawText(x, y + 名字尺.ascent(), self._名字)
        y += 名字尺.height() + _行距

        # 第三行：保存位置
        if self._位置:
            画.setFont(位置字体)
            画.setPen(QColor(255, 255, 255, 185))
            画.drawText(x, y + 位置尺.ascent(), self._位置)
        画.end()

    def resizeEvent(self, 事件):  # noqa: N802 - Qt 命名
        super().resizeEvent(事件)
