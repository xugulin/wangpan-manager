"""滚动容器：窗口变小、内容放不下时，让页面/弹窗上下左右滑动，而不是把控件压扁。

为什么需要
==========
以前页面直接铺在窗口里，窗口一缩小 Qt 就会去压缩子控件 —— 表现是：

* 表格列被挤成一条缝、文字换行错位（传输表格最明显）；
* 弹窗底部那几行说明直接被裁掉，用户根本看不到（新增网盘对话框）。

统一做法：给内容套一层 ``QScrollArea``，里面的**外框**把 ``minimumSizeHint``
定成 ``sizeHint``（:class:`不压缩内容`），于是内容保持自己的设计尺寸，装不下时
Qt 只能给滚动条 —— 空间够就不显示，不够就自动出现，上下左右都能拖。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QScrollArea, QVBoxLayout, QWidget

__all__ = ["不压缩内容", "包成滚动区", "包一层滚动", "已可滚动"]


class 不压缩内容(QWidget):
    """滚动区的内容外框：最小尺寸 = 理想尺寸，因此永远不会被压扁。"""

    def minimumSizeHint(self):  # noqa: N802 - Qt 命名
        return self.sizeHint()


def 包成滚动区(内容: QWidget, *, 名字: str = "PageScroll",
           横向: bool = True) -> QScrollArea:
    """把 ``内容`` 放进滚动区。``横向=False`` 时不出横向滚动条。"""
    滚动区 = QScrollArea()
    滚动区.setObjectName(名字)
    滚动区.setWidgetResizable(True)
    滚动区.setFrameShape(QScrollArea.NoFrame)
    滚动区.setHorizontalScrollBarPolicy(
        Qt.ScrollBarPolicy.ScrollBarAsNeeded if 横向
        else Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    滚动区.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    滚动区.viewport().setAutoFillBackground(False)
    滚动区.setWidget(内容)
    return 滚动区


def 已可滚动(页: QWidget) -> bool:
    """这个控件是不是已经自带滚动区（设置页 / AI 页是自己套好的）。"""
    布局 = 页.layout()
    if 布局 is None or 布局.count() == 0:
        return False
    return isinstance(布局.itemAt(0).widget(), QScrollArea)


def 包一层滚动(页: QWidget, *, 名字: str = "PageScroll",
           横向: bool = True) -> QWidget:
    """给已经排好布局的页面套上滚动区，返回外层控件（可直接塞进 QStackedWidget）。

    外框用**普通控件**（不是 :class:`不压缩内容`）：空间够时页面跟着窗口一起拉伸
    （表格铺满、不出滚动条），只有当窗口小于页面自身的最小尺寸时才出滚动条 ——
    页面里的表格等控件各自给足了最小宽度，所以不会被压变形。

    * 已经自带滚动区的页面原样返回 —— 免得"滚动区套滚动区"，滚轮事件会被里层吃掉；
    * 页面本身作为外框的子控件，布局一行都不用改。
    """
    if 已可滚动(页):
        return 页
    外框 = QWidget()
    外框.setObjectName(名字 + "外框")
    布局 = QVBoxLayout(外框)
    布局.setContentsMargins(0, 0, 0, 0)
    布局.setSpacing(0)
    布局.addWidget(页)
    return 包成滚动区(外框, 名字=名字, 横向=横向)
