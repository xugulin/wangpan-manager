"""播放控制条：播放页与独立播放窗口**共用同一套控件**。

为什么要抽出来
==============
需求是"独立窗口仍带播放器相关功能的控件"，如果照抄一份到新窗口里，以后
改一个按钮就得改两处（必然改漏）。所以控制条只写一次：

* :class:`播放控制条` —— 纯控件 + 信号，**不认识 libvlc**，谁用谁接信号；
* 播放页把它放进页面底部；独立窗口把它放进窗口底部（全屏时整体隐藏）。

对齐方式参考播放页既有形态：``⏸ ⏹ ──进度── 时间 音量 ─ 倍速 字幕 截图 全屏``。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QComboBox, QHBoxLayout, QLabel, QPushButton,
                               QSlider, QVBoxLayout, QWidget)

__all__ = ["播放控制条", "视频窗", "时间文本"]


class 视频窗(QWidget):
    """交给 libvlc 画视频的容器（黑底 + 双击发信号）。

    必须是**原生窗口**（``WA_NativeWindow``）：libvlc 要用 X11 的 window id 往上画。
    """

    双击 = Signal()

    def __init__(self, 父=None):
        super().__init__(父)
        self.setStyleSheet("background: #000;")
        self.setMinimumHeight(240)
        self.setAttribute(Qt.WA_NativeWindow, True)   # 必须有真实 X11 窗口
        self.setAttribute(Qt.WA_DontCreateNativeAncestors, True)
        self.setMouseTracking(True)

    def mouseDoubleClickEvent(self, 事件):  # noqa: N802
        self.双击.emit()
        super().mouseDoubleClickEvent(事件)


def 时间文本(秒: float) -> str:
    """秒 → ``HH:MM:SS``（不足 1 小时给 ``MM:SS``）。"""
    秒 = max(0, int(秒 or 0))
    时, 余 = divmod(秒, 3600)
    分, 秒 = divmod(余, 60)
    if 时:
        return f"{时:d}:{分:02d}:{秒:02d}"
    return f"{分:02d}:{秒:02d}"


class 播放控制条(QWidget):
    """播放控制条（**两行**：上面进度条、下面按钮行）。

    布局（独立播放器用）::

        ┌──────────────────────────────────────────────────────────┐
        │ 00:07 / 00:10  ──────────●──────────────────────────      │  ← 进度独立一行
        ├──────────────────────────────────────────────────────────┤
        │ [⏸] [⏹] [⏮ 上一个] [⏭ 下一个]       速度 [1.0×] [💬字幕] [📷截图] [⛶全屏] │
        └──────────────────────────────────────────────────────────┘

    :param 含音量: 是否带"音量 + 滑块"。独立播放器把它放到视频**上方**的工具栏里
        （带静音），所以传 False；播放页保留在下方。
    :param 含上下一个: 是否带「上一个 / 下一个」（独立播放器要求放在这里）。
    """

    请求播放 = Signal()              # 「▶ 播放」（播放页用；放在暂停按钮前面）
    请求暂停 = Signal()
    请求停止 = Signal()
    请求上一个 = Signal()
    请求下一个 = Signal()
    请求循环 = Signal()
    请求随机 = Signal(bool)          # 勾选状态
    请求静音 = Signal(bool)          # 勾选状态
    请求跳转 = Signal(float)          # 比例 0~1（拖动结束）
    拖动开始 = Signal()
    请求音量 = Signal(int)            # 0~150
    请求倍速 = Signal(float)
    请求字幕 = Signal()
    请求截图 = Signal()
    请求全屏 = Signal()
    请求独立窗口 = Signal()          # 「🗗 独立窗口」（放在全屏按钮后面）

    def __init__(self, 父=None, *, 紧凑: bool = False, 含音量: bool = True,
                 含上下一个: bool = True, 含循环随机: bool = False,
                 含播放按钮: bool = False, 含独立窗口: bool = False,
                 进度条高度: int = 12):
        super().__init__(父)
        self.紧凑 = bool(紧凑)
        self.含上下一个 = bool(含上下一个)
        self.含循环随机 = bool(含循环随机)
        竖 = QVBoxLayout(self)
        边距 = 2 if self.紧凑 else 0
        竖.setContentsMargins(边距, 边距, 边距, 边距)
        竖.setSpacing(2)

        # ---------------- 第一行：时间 + 进度条（紧贴视频窗口） ----------------
        进度行 = QHBoxLayout()
        进度行.setSpacing(6)
        self.时间标签 = QLabel("00:00 / 00:00")
        self.时间标签.setMinimumWidth(112)
        进度行.addWidget(self.时间标签)
        # 进度条用 QSlider（要能拖动跳转；QProgressBar 没有 sliderPressed）
        self.进度条 = QSlider(Qt.Horizontal)
        self.进度条.setRange(0, 1000)
        self.进度条.setPageStep(50)
        self.进度条.setToolTip("拖动跳转")
        # 需求：进度条加粗 —— 高度给足 + 圆角/更粗的滑块，远看也看得清
        self.进度条.setFixedHeight(max(10, int(进度条高度)))
        self.进度条.setStyleSheet(
            "QSlider::groove:horizontal{height:%dpx;border-radius:%dpx;"
            "background:#3a3f44;}"
            "QSlider::sub-page:horizontal{height:%dpx;border-radius:%dpx;"
            "background:#2d8cf0;}"
            "QSlider::handle:horizontal{width:14px;margin:-4px 0;"
            "border-radius:7px;background:#e8e8e8;}"
            % (max(6, int(进度条高度) - 4), max(3, (int(进度条高度) - 4) // 2),
               max(6, int(进度条高度) - 4), max(3, (int(进度条高度) - 4) // 2)))
        self.进度条.sliderPressed.connect(self.拖动开始.emit)
        self.进度条.sliderReleased.connect(self._拖动结束)
        进度行.addWidget(self.进度条, 1)
        竖.addLayout(进度行)

        # ---------------- 第二行：播放/停止/上一个/下一个 + 速度 + 字幕截图全屏 ----------------
        行 = QHBoxLayout()
        行.setSpacing(6 if self.紧凑 else 8)

        # 需求：「▶ 播放」放在「⏸ 暂停」**前面**（播放页）
        self.播放按钮 = QPushButton("▶ 播放")
        self.播放按钮.setToolTip("播放（按路径栏里的视频）")
        self.播放按钮.clicked.connect(self.请求播放.emit)
        if 含播放按钮:
            行.addWidget(self.播放按钮)
        else:
            self.播放按钮.hide()

        self.播放暂停按钮 = QPushButton("⏸")
        self.播放暂停按钮.setFixedWidth(38)
        self.播放暂停按钮.setToolTip("暂停 / 继续（空格）")
        self.播放暂停按钮.clicked.connect(self.请求暂停.emit)
        行.addWidget(self.播放暂停按钮)

        # 顺序按需求：⏸ 播放暂停 → ⏮ 上一个 → ⏹ 停止 → ⏭ 下一个
        self.上一个按钮 = QPushButton("⏮ 上一个")
        self.上一个按钮.setToolTip("播放清单里的上一个（N）")
        self.上一个按钮.clicked.connect(self.请求上一个.emit)
        self.停止按钮 = QPushButton("⏹")
        self.停止按钮.setFixedWidth(38)
        self.停止按钮.setToolTip("停止")
        self.停止按钮.clicked.connect(self.请求停止.emit)
        self.下一个按钮 = QPushButton("⏭ 下一个")
        self.下一个按钮.setToolTip("播放清单里的下一个（B）")
        self.下一个按钮.clicked.connect(self.请求下一个.emit)
        if self.含上下一个:
            行.addWidget(self.上一个按钮)
            行.addWidget(self.停止按钮)
            行.addWidget(self.下一个按钮)
        else:
            行.addWidget(self.停止按钮)
            self.上一个按钮.hide()
            self.下一个按钮.hide()

        # 需求：「不循环 / 随机」跟在「下一个」后面
        self.循环按钮 = QPushButton("🔁 不循环")
        self.循环按钮.setToolTip("循环模式：不循环 / 单曲 / 列表")
        self.循环按钮.clicked.connect(lambda: self.请求循环.emit())
        self.随机按钮 = QPushButton("🔀 随机")
        self.随机按钮.setCheckable(True)
        self.随机按钮.setToolTip("随机播放")
        self.随机按钮.clicked.connect(self.请求随机.emit)
        if self.含循环随机:
            行.addWidget(self.循环按钮)
            行.addWidget(self.随机按钮)
        else:
            self.循环按钮.hide()
            self.随机按钮.hide()

        行.addStretch(1)

        # 需求：音量（静音 + 滑块）放在「速度」**前面**
        self.静音按钮 = QPushButton("🔊")
        self.静音按钮.setCheckable(True)
        self.静音按钮.setFixedWidth(34)
        self.静音按钮.setToolTip("静音 / 取消静音（M）")
        self.静音按钮.clicked.connect(self.请求静音.emit)

        if 含音量:
            行.addWidget(self.静音按钮)
            self.音量条 = QSlider(Qt.Horizontal)
            self.音量条.setRange(0, 150)
            self.音量条.setValue(100)
            self.音量条.setFixedWidth(110 if not self.紧凑 else 90)
            self.音量条.setToolTip("音量（↑/↓ 也能调）")
            self.音量条.valueChanged.connect(self.请求音量.emit)
            行.addWidget(self.音量条)
            self.音量标签 = QLabel("100%")
            self.音量标签.setMinimumWidth(38)
            行.addWidget(self.音量标签)
        else:
            self.音量条 = None
            self.音量标签 = None
            self.静音按钮.hide()

        # 速度：带「速度」文字标签（用户要求）
        self.速度标签 = QLabel("速度")
        self.速度标签.setToolTip("播放速度")
        行.addWidget(self.速度标签)
        self.倍速框 = QComboBox()
        for 倍速 in ("0.25", "0.5", "0.75", "1.0", "1.25", "1.5", "2.0", "3.0", "4.0"):
            self.倍速框.addItem(f"{倍速}×", float(倍速))
        self.倍速框.setCurrentIndex(3)          # 1.0×
        self.倍速框.setFixedWidth(76)
        self.倍速框.currentIndexChanged.connect(
            lambda _i: self.请求倍速.emit(float(self.倍速框.currentData() or 1.0)))
        行.addWidget(self.倍速框)

        self.字幕按钮 = QPushButton("💬 字幕")
        self.字幕按钮.setToolTip("在内嵌/外挂字幕轨之间循环切换")
        self.字幕按钮.clicked.connect(self.请求字幕.emit)
        行.addWidget(self.字幕按钮)

        self.截图按钮 = QPushButton("📷 截图")
        self.截图按钮.setToolTip("把当前画面存到项目的 数据/截图/ 目录（Shift+S）")
        self.截图按钮.clicked.connect(self.请求截图.emit)
        行.addWidget(self.截图按钮)

        self.全屏按钮 = QPushButton("⛶ 全屏")
        self.全屏按钮.setToolTip("全屏播放（Esc 退出全屏；全屏时控件自动隐藏）")
        self.全屏按钮.clicked.connect(self.请求全屏.emit)
        行.addWidget(self.全屏按钮)

        # 需求：「🗗 独立窗口」放在「全屏」**后面**
        self.独立窗口按钮 = QPushButton("🗗 独立窗口")
        self.独立窗口按钮.setToolTip("在独立窗口里播放（同一个播放会话，画面搬过去）")
        self.独立窗口按钮.clicked.connect(self.请求独立窗口.emit)
        if 含独立窗口:
            行.addWidget(self.独立窗口按钮)
        else:
            self.独立窗口按钮.hide()

        竖.addLayout(行)

    # ---------------- 对外接口 ----------------

    def _拖动结束(self):
        self.请求跳转.emit(self.进度条.value() / 1000.0)

    def 设置进度(self, 进度秒: float, 时长秒: float):
        self.时间标签.setText(f"{时间文本(进度秒)} / {时间文本(时长秒)}")
        if 时长秒 and 时长秒 > 0:
            self.进度条.setValue(int(1000 * max(0.0, 进度秒) / 时长秒))

    def 设置暂停图标(self, 正在播放: bool):
        self.播放暂停按钮.setText("⏸" if 正在播放 else "▶")

    def 设置全屏图标(self, 全屏: bool):
        self.全屏按钮.setText("🗗 退出全屏" if 全屏 else "⛶ 全屏")

    def 设置倍速(self, 倍速: float):
        for i in range(self.倍速框.count()):
            if abs(float(self.倍速框.itemData(i) or 0) - float(倍速)) < 0.01:
                self.倍速框.blockSignals(True)
                self.倍速框.setCurrentIndex(i)
                self.倍速框.blockSignals(False)
                return

    def 设置音量(self, 值: int):
        if self.音量条 is None:
            return
        self.音量条.blockSignals(True)
        self.音量条.setValue(int(值))
        self.音量条.blockSignals(False)
        if self.音量标签 is not None:
            self.音量标签.setText(f"{int(值)}%")

    def 设置静音图标(self, 静音: bool):
        self.静音按钮.blockSignals(True)
        self.静音按钮.setChecked(bool(静音))
        self.静音按钮.setText("🔇" if 静音 else "🔊")
        self.静音按钮.blockSignals(False)

    def 设置循环文本(self, 文本: str):
        self.循环按钮.setText(文本)

    def 设置随机勾选(self, 勾选: bool):
        self.随机按钮.blockSignals(True)
        self.随机按钮.setChecked(bool(勾选))
        self.随机按钮.blockSignals(False)
