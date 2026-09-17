"""自绘的勾选框 / 单选框 / 下拉箭头。

为什么不用 QSS
==============
* ``QCheckBox::indicator`` 在 QSS 里只能用 ``image: url(...)`` 放一张图片来画勾，
  本项目**不带图片资源**（要求完全自包含、可整体搬走）；以前只好用"选中就填一块
  颜色"顶替，看起来就是个色块，勾在哪完全看不出来。
* ``QComboBox::down-arrow`` 同理。之前用 CSS 三角边框的偏方（``border-top`` 着色 +
  左右透明边框），画出来又小又暗，很难一眼找到"点哪儿能展开"。

所以这两处交给 :class:`控件样式`（``QProxyStyle``）自己画：

* 勾选框 = **空心**圆角方框；选中时框里画一个勾（**不做颜色填充**），悬停时边框
  变强调色，禁用时整体变暗；
* 单选框 = 空心圆圈 + 选中时圆心一个实心点；
* 下拉箭头 = 加粗折角箭头（比原来的三角大一圈），悬停/展开时换成强调色。

配套约束：QSS 里**不能**再写 ``::indicator`` / ``::down-arrow`` 规则 —— 一旦写了，
Qt 的样式表引擎会自己去画那个子控件，轮不到这里的 ``drawPrimitive``；尺寸也统一由
:meth:`控件样式.pixelMetric` 给出，避免 QSS 与代码两套数字（左侧栏按钮踩过这个坑）。
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QProxyStyle, QStyle

from .主题管理器 import 当前颜色

__all__ = ["控件样式", "安装控件样式", "指示器边长"]

#: 勾选框 / 单选框的边长（QSS 里不再写尺寸，统一走 pixelMetric）
指示器边长 = 16

#: 装到 QApplication 上的那一个实例（幂等安装用）
_已安装: "控件样式 | None" = None


def _颜色(名: str, 缺省: str) -> QColor:
    """取当前主题里的颜色；主题还没套上时用缺省值兜底。"""
    try:
        表 = 当前颜色() or {}
    except Exception:  # noqa: BLE001
        表 = {}
    return QColor(str(表.get(名) or 缺省))


class 控件样式(QProxyStyle):
    """只接管勾选框/单选框/下拉箭头，其余一律交回底层样式。"""

    # ---------------- 尺寸 ----------------

    def pixelMetric(self, 度量, 选项=None, 控件=None):  # noqa: N802 - Qt 命名
        if 度量 in (QStyle.PixelMetric.PM_IndicatorWidth,
                   QStyle.PixelMetric.PM_IndicatorHeight):
            return 指示器边长
        return super().pixelMetric(度量, 选项, 控件)

    # ---------------- 绘制 ----------------

    def drawPrimitive(self, 元素, 选项, 画笔, 控件=None):  # noqa: N802 - Qt 命名
        if 元素 == QStyle.PrimitiveElement.PE_IndicatorCheckBox:
            self._画勾选框(选项, 画笔)
            return
        if 元素 == QStyle.PrimitiveElement.PE_IndicatorRadioButton:
            self._画单选框(选项, 画笔)
            return
        if 元素 in (QStyle.PrimitiveElement.PE_IndicatorArrowDown,
                   QStyle.PrimitiveElement.PE_IndicatorArrowUp):
            self._画箭头(选项, 画笔, 向下=元素 == QStyle.PrimitiveElement.PE_IndicatorArrowDown)
            return
        super().drawPrimitive(元素, 选项, 画笔, 控件)


    # ---- 具体画法 ----

    @staticmethod
    def _方框(选项) -> QRectF:
        矩形 = QRectF(选项.rect)
        边 = min(矩形.width(), 矩形.height())
        框 = QRectF(0.0, 0.0, max(0.0, 边 - 1.0), max(0.0, 边 - 1.0))
        框.moveCenter(矩形.center())
        return 框

    @staticmethod
    def _配色(选项) -> tuple[QColor, QColor, QColor]:
        """返回 (底色, 边框色, 标记色)。"""
        可用 = bool(选项.state & QStyle.StateFlag.State_Enabled)
        悬停 = bool(选项.state & QStyle.StateFlag.State_MouseOver)
        选中 = bool(选项.state & (QStyle.StateFlag.State_On
                              | QStyle.StateFlag.State_NoChange))
        if not 可用:
            return (_颜色("背景", "#888888"), _颜色("次要文字", "#777777"),
                    _颜色("次要文字", "#777777"))
        底色 = _颜色("输入", "#ffffff")
        边框 = _颜色("边框", "#888888")
        标记 = _颜色("强调", "#4a9eff")
        if 悬停 or 选中:
            边框 = 标记
        return 底色, 边框, 标记

    def _画勾选框(self, 选项, 画笔):
        框 = self._方框(选项)
        if 框.width() < 4 or 框.height() < 4:
            return
        底色, 边框色, 标记色 = self._配色(选项)
        勾上 = bool(选项.state & QStyle.StateFlag.State_On)
        半选 = bool(选项.state & QStyle.StateFlag.State_NoChange)

        画笔.save()
        画笔.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        画笔.setPen(QPen(边框色, 1.4))
        画笔.setBrush(QBrush(底色))
        画笔.drawRoundedRect(框, 3.0, 3.0)

        if 勾上 or 半选:
            # 关键：只画笔画，**不填充**——就是"空框里打个勾"
            画笔.setPen(QPen(标记色, 2.0, Qt.PenStyle.SolidLine,
                           Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            画笔.setBrush(Qt.BrushStyle.NoBrush)
            if 勾上:
                路径 = QPainterPath()
                路径.moveTo(框.left() + 框.width() * 0.24, 框.top() + 框.height() * 0.53)
                路径.lineTo(框.left() + 框.width() * 0.43, 框.top() + 框.height() * 0.73)
                路径.lineTo(框.left() + 框.width() * 0.78, 框.top() + 框.height() * 0.29)
                画笔.drawPath(路径)
            else:
                画笔.drawLine(
                    QPointF(框.left() + 框.width() * 0.26, 框.center().y()),
                    QPointF(框.right() - 框.width() * 0.26, 框.center().y()))
        画笔.restore()

    def _画单选框(self, 选项, 画笔):
        框 = self._方框(选项)
        if 框.width() < 4 or 框.height() < 4:
            return
        底色, 边框色, 标记色 = self._配色(选项)
        勾上 = bool(选项.state & QStyle.StateFlag.State_On)

        画笔.save()
        画笔.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        画笔.setPen(QPen(边框色, 1.4))
        画笔.setBrush(QBrush(底色))
        画笔.drawEllipse(框)
        if 勾上:
            点 = QRectF(0.0, 0.0, 框.width() * 0.46, 框.height() * 0.46)
            点.moveCenter(框.center())
            画笔.setPen(Qt.PenStyle.NoPen)
            画笔.setBrush(QBrush(标记色))
            画笔.drawEllipse(点)
        画笔.restore()

    def _画箭头(self, 选项, 画笔, 向下: bool = True):
        矩形 = QRectF(选项.rect)
        if 矩形.width() < 4 or 矩形.height() < 3:
            return
        self._画折角(矩形, 画笔, self._箭头颜色(选项), 向下=向下)

    @staticmethod
    def _箭头颜色(选项) -> QColor:
        if not bool(选项.state & QStyle.StateFlag.State_Enabled):
            return _颜色("次要文字", "#777777")
        激活 = bool(选项.state & (QStyle.StateFlag.State_MouseOver
                              | QStyle.StateFlag.State_Sunken
                              | QStyle.StateFlag.State_On))
        return _颜色("强调", "#4a9eff") if 激活 else _颜色("文字", "#dddddd")

    @staticmethod
    def _画折角(区域: QRectF, 画笔, 颜色: QColor, 向下: bool = True):
        宽 = min(区域.width() * 0.42, 11.0)
        高 = 宽 * 0.55
        中心 = 区域.center()
        上 = 中心.y() - 高 / 2 if 向下 else 中心.y() + 高 / 2
        下 = 中心.y() + 高 / 2 if 向下 else 中心.y() - 高 / 2

        画笔.save()
        画笔.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        画笔.setPen(QPen(颜色, 2.2, Qt.PenStyle.SolidLine,
                        Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        画笔.setBrush(Qt.BrushStyle.NoBrush)
        画笔.drawPolyline([QPointF(中心.x() - 宽 / 2, 上),
                         QPointF(中心.x(), 下),
                         QPointF(中心.x() + 宽 / 2, 上)])
        画笔.restore()


def 安装控件样式(应用=None) -> 控件样式 | None:
    """给应用装上自绘样式（幂等：重复调用不会层层套娃）。

    要在**第一次 setStyleSheet 之前**调用：基样式取的是当时的应用样式，
    等样式表套上以后再装，拿到的就是被 QStyleSheetStyle 包过的壳了。
    """
    global _已安装
    if 应用 is None:
        from PySide6.QtWidgets import QApplication
        应用 = QApplication.instance()
    if 应用 is None:
        return None
    if _已安装 is not None:
        return _已安装
    _已安装 = 控件样式()          # 基样式 = 当前的应用样式（默认 Fusion 等）
    应用.setStyle(_已安装)
    return _已安装
