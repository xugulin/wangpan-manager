# v8_3/界面/播放页面.py
"""视频播放页（V8_3 新增）。

界面结构（自上而下）
====================
::

    ┌ 路径栏：网盘 ▾ / 远端路径 / [▶ 播放] [⏸] [⏹] [📂 网盘选片] ───────────┐
    ├ AI 信息栏：媒体信息 · 直链探测 · 起播参数（含"决策来源"与理由） ─────┤
    ├ 视频区：libvlc 内嵌窗口（黑底，等比，鼠标双击全屏） ─────────────────┤
    ├ 控制条：▶ ⏯ ⏹ | ━━进度━━ | 时间 | 音量 | 倍速 | 字幕 | 截图 | 全屏 ──┤
    └ AI 面板（可折叠）：字幕翻译 / 生成字幕 / 内容总结 / 播放诊断 ─────────┘

关键设计
========
* **视频窗口**：用一个 ``QWidget`` 占位并把它的 ``winId()`` 交给 libvlc
  （``libvlc_media_player_set_xwindow``），VLC 直接画在 Qt 控件里 —— 不是另开
  一个播放器窗口，符合"封装到 V8_3 里调用"的要求；
* **AI 面板**是"按需出现"的：字幕/总结/诊断都在后台线程跑，界面不卡；
* 播放核心（``v8_3.播放.播放核心``）**不依赖 Qt**，这里只做展示与转发。
"""
from __future__ import annotations

import threading
import time
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QAction, QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QFileDialog, QHBoxLayout, QLabel, QLineEdit,
    QMessageBox, QPlainTextEdit, QPushButton, QSplitter, QTabWidget,
    QVBoxLayout, QWidget,
)

from ..播放.播放核心 import 播放会话
from ..播放.媒体信息 import 是视频文件, 视频后缀
from ..播放.显示环境 import 是桌面平台
from ..播放.vlc绑定 import 可用 as vlc可用, 不可用原因
from .AI播放面板 import AI播放面板, AI字幕动作
from .路径选择对话框 import 路径选择对话框
from .播放控件 import 播放控制条, 视频窗
from .播放清单 import 播放清单, 播放项
from .vlc风格 import 构建菜单栏, 构建工具栏


class 播放页面(QWidget):
    """播放页：一个播放会话 + 一套控件 + 一块 AI 面板。"""

    状态更新 = Signal(dict)
    #: 日志信号：**必须走信号**，因为 VLC/探测/语音识别都会从各自线程回调日志，
    #: 直接在别的线程里 appendPlainText 是跨线程碰控件 —— 实测会偶发段错误。
    日志追加 = Signal(str)

    def __init__(self, 主窗口, 父=None):
        super().__init__(父)
        self.主窗口 = 主窗口
        self.动作 = 主窗口.动作
        self.配置 = 主窗口.配置
        self.会话: 播放会话 | None = None
        self.顾问 = None
        self.日志追加.connect(self._写日志, Qt.QueuedConnection)
        self._诊断中 = False        # 自动调优诊断是否在跑（同时只允许一个）
        self._上次诊断时间 = 0.0    # 上次自动诊断时间（冷却用，防止刷屏）
        self._上次诊断丢帧 = 0      # 上次诊断时的丢帧计数（算增量用）
        self._独立窗口 = None       # 独立播放窗口（顶层，可全屏）
        self._续播位置 = 0.0        # 独立窗口关窗后要接着播的位置
        self._守护 = None           # 游离窗口巡检（懒创建）
        # 是否已同意"用 VLC 自带窗口播放"（默认不同意：那种窗口不受本程序控制）
        from ..播放.显示环境 import 允许VLC自带窗口 as _允许自带
        self._允许自带窗口 = _允许自带()
        self._本次独立窗口 = False   # 这一次 ▶ 是不是要交给独立窗口
        self.字幕助手 = None
        self.语音识别器 = None
        # AI 观影动作（翻译/生成/总结/诊断）——与独立窗口共用同一套实现
        self.AI动作 = AI字幕动作(
            取会话=lambda: self.会话,
            日志=self._AI写,
            状态=lambda 数据: self.状态更新.emit(dict(数据 or {})))
        self._子线程: list[threading.Thread] = []
        self._全屏前状态 = None
        self._拖拽中 = False
        self._构建()
        self.右栏 = self.左面板        # 兼容旧名字（右侧面板已改到视频左侧）
        self.刷新网盘列表()          # 构造时就填好网盘下拉（否则第一次播放会没得选）
        self.定时器 = QTimer(self)
        self.定时器.setInterval(500)
        self.定时器.timeout.connect(self._刷新状态)
        self._准备AI()

    # ==================== 构建 ====================

    def _构建(self) -> None:
        布局 = QVBoxLayout(self)
        布局.setContentsMargins(0, 0, 0, 0)
        布局.setSpacing(6)

        # ---- 菜单栏（撤掉工具栏那行；按钮落到「帮助」后面）----
        from .vlc风格 import 构建菜单栏 as _建菜单
        self.菜单栏 = _建菜单(
            self._动作表(), self,
            带侧栏按钮=True)          # 「🗂 面板」：收起/展开侧边面板
        布局.addWidget(self.菜单栏)
        self.工具栏 = None            # 需求 7：视频上方那行撤销

        # ---- 路径栏（形态与「跨网盘传输」页的源/目标一致）----
        表单 = QHBoxLayout()
        表单.setSpacing(8)
        表单.addWidget(QLabel("网盘："))
        self.网盘框 = QComboBox()
        self.网盘框.setMinimumWidth(190)
        表单.addWidget(self.网盘框)
        表单.addWidget(QLabel("视频路径："))
        self.路径框 = QLineEdit("/")
        self.路径框.setPlaceholderText("网盘里的视频文件，例如 /电影/xxx.mp4")
        self.路径框.returnPressed.connect(self._播放)
        表单.addWidget(self.路径框, 1)
        self.浏览按钮 = QPushButton("📂 浏览…")
        self.浏览按钮.setToolTip("和传输页一样：在网盘目录里进进出出，选中视频文件")
        self.浏览按钮.clicked.connect(self._浏览)
        表单.addWidget(self.浏览按钮)
        布局.addLayout(表单)

        # ---- 视频左侧面板：视频信息 / 播放清单 / AI 助手 ----
        主体 = QSplitter(Qt.Horizontal)
        self.左面板 = QTabWidget()
        self.左面板.setMinimumWidth(260)
        self.左面板.setMaximumWidth(460)
        self._建信息面板()             # 页 1：视频信息
        self.清单 = 播放清单(self.左面板)
        self.清单.请求播放.connect(self._清单选了某项)
        self.左面板.addTab(self.清单, "📋 播放清单")
        self.左面板.addTab(self._建AI面板(), "🤖 AI 助手")
        self.左面板.currentChanged.connect(
            lambda _i: self._延迟按视频比例调整窗口(60))
        self.视频 = 视频窗(self)
        self.视频.双击.connect(self._切换全屏)
        主体.addWidget(self.视频)
        主体.addWidget(self.左面板)       # 需求：面板放视频**右侧**
        主体.setStretchFactor(0, 1)
        主体.setStretchFactor(1, 0)
        主体.setSizes([900, 320])
        self.主体 = 主体
        布局.addWidget(主体, 1)

        # ---- 控制条（两行：加粗进度条 + 按钮行），与独立窗口共用同一套控件 ----
        self.控制条 = 播放控制条(self, 含音量=True, 含上下一个=True,
                          含循环随机=True, 含播放按钮=True,
                          含独立窗口=True, 进度条高度=14)
        # 兼容旧名字（自检/其它代码按这些名字找控件）
        self.播放按钮 = self.控制条.播放按钮
        self.独立窗口按钮 = self.控制条.独立窗口按钮
        self.本地按钮 = None          # 需求 5：那行撤销，改由菜单「📁 文件」
        self.播放暂停按钮 = self.控制条.播放暂停按钮
        self.停止按钮 = self.控制条.停止按钮
        self.上一个按钮 = self.控制条.上一个按钮
        self.下一个按钮 = self.控制条.下一个按钮
        self.进度条 = self.控制条.进度条
        self.时间标签 = self.控制条.时间标签
        self.音量条 = self.控制条.音量条
        self.倍速框 = self.控制条.倍速框
        self.字幕按钮 = self.控制条.字幕按钮
        self.截图按钮 = self.控制条.截图按钮
        self.全屏按钮 = self.控制条.全屏按钮
        self.控制条.请求播放.connect(self._播放)
        self.控制条.请求暂停.connect(self._播放暂停)
        self.控制条.请求停止.connect(self._停止)
        self.控制条.请求上一个.connect(self.上一个)
        self.控制条.请求下一个.connect(self.下一个)
        self.控制条.请求循环.connect(self._切换循环)
        self.控制条.请求随机.connect(self._切换随机)
        self.控制条.请求静音.connect(self._设置静音)
        self.控制条.请求独立窗口.connect(self.独立窗口播放)
        self.控制条.拖动开始.connect(lambda: setattr(self, "_拖拽中", True))
        self.控制条.请求跳转.connect(self._跳转比例)
        self.控制条.请求音量.connect(self._音量变化)
        self.控制条.请求倍速.connect(self._倍速变化)
        self.控制条.请求字幕.connect(self._切换字幕)
        self.控制条.请求截图.connect(self._截图)
        self.控制条.请求全屏.connect(self._切换全屏)
        布局.addWidget(self.控制条)

        self.状态标签 = QLabel("就绪")
        self.状态标签.setStyleSheet("font-size: 11px; color: #95a5a6;")
        布局.addWidget(self.状态标签)

    def _建信息面板(self) -> QWidget:
        """左侧面板第 1 页：**视频信息**（原来在视频上方那一条）。"""
        页 = QWidget()
        竖 = QVBoxLayout(页)
        竖.setContentsMargins(4, 4, 4, 4)
        竖.setSpacing(4)
        self.信息栏 = QLabel("🅥 V8_3 播放器就绪" +
                         ("" if vlc可用() else "（libvlc 不可用）"))
        self.信息栏.setWordWrap(True)
        self.信息栏.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.信息栏.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.信息栏.setStyleSheet(
            "font-size: 12px; padding: 6px; border-radius: 4px;"
            "background: #2c3e50; color: #ecf0f1;")
        竖.addWidget(self.信息栏)
        竖.addStretch(1)
        self.左面板.addTab(页, "ℹ️ 视频信息")
        return 页

    def _建AI面板(self) -> QWidget:
        """AI 面板（与独立窗口共用同一个控件类）。"""
        # 紧凑模式：面板不宽，四个按钮排两行才显示得全（用户反馈按钮被截断）
        self.AI面板 = AI播放面板(self, 自动调优=True, 紧凑=True)
        # 属性名保持与原实现一致：自检/其它代码直接按名字找这些控件
        self.自动调优框 = self.AI面板.自动调优框
        self.翻译按钮 = self.AI面板.翻译按钮
        self.生字幕按钮 = self.AI面板.生字幕按钮
        self.总结按钮 = self.AI面板.总结按钮
        self.诊断按钮 = self.AI面板.诊断按钮
        self.AI输出 = self.AI面板.AI输出
        self.AI面板.翻译.connect(self._AI翻译字幕)
        self.AI面板.生成字幕.connect(self._AI生成字幕)
        self.AI面板.总结.connect(self._AI总结)
        self.AI面板.诊断.connect(self._手动诊断)
        return self.AI面板

    # ==================== VLC 风格动作（菜单/工具栏共用） ====================

    def _动作表(self) -> dict:
        """给 vlc风格.构建菜单栏/构建工具栏 的动作表（缺的自动置灰）。"""
        return {
            "打开网盘": self._浏览,
            "打开本地": self._打开本地,
            "加入清单": self.把当前加入清单,
            "添加本地到清单": self._本地加入清单,
            "退出": lambda: self.主窗口.close(),
            "播放暂停": self._播放暂停,
            "停止": self._停止,
            "上一个": self.上一个,
            "下一个": self.下一个,
            "逐帧": self.逐帧,
            "快进": lambda: self.相对跳转(10.0),
            "快退": lambda: self.相对跳转(-10.0),
            "到开头": lambda: self._跳转绝对(0.0),
            "跳到结尾": self.跳到结尾,
            "速度列表": lambda: [(f"{x}×", x) for x in
                            (0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 3.0, 4.0)],
            "当前速度": self.当前速度,
            "设置速度": self._设置速度,
            "章节数": self.章节数,
            "当前章节": self.当前章节,
            "跳章节": self.跳章节,
            "下一章": self.下一章,
            "上一章": self.上一章,
            "音量加": lambda: self._调音量(5),
            "音量减": lambda: self._调音量(-5),
            "静音切换": self._设置静音,
            "是否静音切换": self._是否静音,
            "音量": self.当前音量,
            "设置音量": self._设置音量,
            "音频轨列表": self._音频轨列表,
            "当前音频轨": lambda: self._当前轨("音频"),
            "选择音频轨": self._选择音频轨,
            "全屏": self._设置全屏,
            "是否全屏": lambda: bool(self.window().isFullScreen()),
            "截图": self._截图,
            "宽高比": self.当前宽高比,
            "设置宽高比": self._设置宽高比,
            "缩放": self.当前缩放,
            "设置缩放": self._设置缩放,
            "置顶窗口": self._切换置顶,
            "添加字幕文件": self._添加字幕文件,
            "字幕轨列表": self._字幕轨列表,
            "当前字幕轨": lambda: self._当前轨("字幕"),
            "选择字幕轨": self._选择字幕轨,
            "切换清单": self._切换清单,
            "切换AI面板": self._切换AI面板,
            "播放参数": self._显示播放参数,
            "AI诊断": self._手动诊断,
            "流畅优先": self.流畅优先,
            "用VLC自带窗口": self.用VLC自带窗口播放,
            "适应视频比例": self.按视频比例调整窗口,
            "切换自动调优": lambda 选中: self.自动调优框.setChecked(bool(选中)),
            "AI翻译字幕": self._AI翻译字幕,
            "AI生成字幕": self._AI生成字幕,
            "AI总结": self._AI总结,
            "AI帮助": lambda: self._AI写(
                "🤖 AI：翻译字幕 / 生成字幕 / 内容总结 / 卡顿诊断 / 自动换参数重载。"
                "免费离线走本地模型，没有则回落规则。"),
            "切换侧栏": self.切换侧栏,
            "循环切换": self._切换循环,
            "随机切换": self._切换随机,
        }

    def 按视频比例调整窗口(self) -> None:
        """让主窗口的视频区按视频宽高比显示（消除黑边）。

        播放页的视频区在 QSplitter 里，尺寸由窗口决定；这里把**主窗口**调成
        "视频区比例 = 视频比例 + 装饰"的大小，然后复位 VLC 的缩放/宽高比。
        """
        会话 = self.会话
        媒体 = getattr(会话, "媒体", None) if 会话 is not None else None
        try:
            宽 = int(getattr(媒体, "宽", 0) or 0)
            高 = int(getattr(媒体, "高", 0) or 0)
        except Exception:  # noqa: BLE001
            宽 = 高 = 0
        if 宽 <= 0 or 高 <= 0:
            self.状态标签.setText("⚠️ 还不知道视频分辨率（先播放一次）")
            return
        比例 = 宽 / 高
        窗口 = self.window()
        装饰宽 = max(0, 窗口.width() - self.视频.width())
        装饰高 = max(0, 窗口.height() - self.视频.height())
        try:
            屏幕 = self.screen() or QGuiApplication.primaryScreen()
            区域 = 屏幕.availableGeometry()
        except Exception:  # noqa: BLE001
            区域 = None
        可用宽 = (int(区域.width() * 0.94) - 装饰宽) if 区域 else 宽
        可用高 = (int(区域.height() * 0.94) - 装饰高) if 区域 else 高
        目标视频宽 = min(宽, max(320, 可用宽))
        目标视频高 = int(round(目标视频宽 / 比例))
        if 可用高 and 目标视频高 > 可用高:
            目标视频高 = 可用高
            目标视频宽 = int(round(目标视频高 * 比例))
        窗口.resize(max(480, 目标视频宽 + 装饰宽), max(320, 目标视频高 + 装饰高))
        try:
            if 会话 is not None and 会话.播放器 is not None:
                会话.播放器.设置缩放(0.0)
                会话.播放器.设置宽高比("")
        except Exception:  # noqa: BLE001
            pass
        self.状态标签.setText(
            f"🖼 已按视频比例调整窗口：目标视频区 {目标视频宽}×{目标视频高}")

    def 切换侧栏(self, 显示: bool = True):
        """显示/隐藏整个右侧面板（播放清单 + AI 助手）。

        需求：藏起面板后视频区会变宽 —— 不重排窗口就会出现**左右黑边**，
        所以显隐之后按视频比例把主窗口重排一次（藏面板 = 窗口跟着收窄）。
        """
        目标 = bool(显示)
        if 目标:
            self.右栏.show()
        else:
            self.右栏.hide()
        self._同步侧栏勾选()
        self._延迟按视频比例调整窗口()

    def _延迟按视频比例调整窗口(self, 毫秒: int = 0) -> None:
        """等 Qt 把布局跑完再按视频比例重排（藏/显示面板、切页签后调用）。"""
        QTimer.singleShot(毫秒, self.按视频比例调整窗口)
        QTimer.singleShot(毫秒 + 90, self.按视频比例调整窗口)

    def _同步侧栏勾选(self):
        # 面板开关挂在**菜单栏**（那行工具栏已撤销）
        动作 = getattr(getattr(self, "菜单栏", None), "侧栏动作", None)
        if 动作 is not None:
            动作.setChecked(bool(self.左面板.isVisible()))

    def _切换清单(self, 显示: bool = True):
        """切到左侧面板的「📋 播放清单」页（页号 1）。"""
        self.左面板.show()
        self.左面板.setCurrentIndex(1)
        self._同步侧栏勾选()
        self._延迟按视频比例调整窗口()

    def _切换AI面板(self, 显示: bool = True):
        """切到左侧面板的「🤖 AI 助手」页（页号 2）。"""
        self.左面板.show()
        self.左面板.setCurrentIndex(2)
        self._同步侧栏勾选()
        self._延迟按视频比例调整窗口()

    # ---- 播放控制（页面这边操作自己的会话）----

    def _播放器(self):
        return self.会话.播放器 if self.会话 is not None else None

    def 相对跳转(self, 秒: float):
        播放器 = self._播放器()
        if 播放器 is None:
            return
        self._跳转绝对(max(0.0, 播放器.进度秒() + float(秒)))

    def _跳转绝对(self, 秒: float):
        if self.会话 is not None:
            self.会话.跳转(max(0.0, float(秒)))
            self.状态标签.setText(f"⏩ {self._时间文本(秒)}")

    def 跳到结尾(self):
        播放器 = self._播放器()
        if 播放器 is None:
            return
        时长 = 播放器.时长秒()
        if 时长 > 0:
            self._跳转绝对(max(0.0, 时长 - 1.0))

    def 逐帧(self):
        if self._播放器() is not None:
            self._播放器().下一帧()

    def 当前速度(self) -> float:
        try:
            return float(self._播放器().取速率()) if self._播放器() else 1.0
        except Exception:  # noqa: BLE001
            return 1.0

    def _设置速度(self, 倍速: float):
        self._倍速变化(float(倍速))

    def 当前音量(self) -> int:
        try:
            return int(self._播放器().取音量()) if self._播放器() else 100
        except Exception:  # noqa: BLE001
            return 100

    def _设置音量(self, 值: int):
        self._音量变化(int(值))
        条 = getattr(getattr(self, "控制条", None), "音量条", None)
        if 条 is not None:
            条.blockSignals(True)
            条.setValue(int(值))
            条.blockSignals(False)

    def _调音量(self, 增减: int):
        self._设置音量(max(0, min(150, self.当前音量() + int(增减))))

    def _是否静音(self) -> bool:
        try:
            return bool(self._播放器().是否静音()) if self._播放器() else False
        except Exception:  # noqa: BLE001
            return False

    def _设置静音(self, 静音: bool):
        if self._播放器() is not None:
            self._播放器().设置静音(bool(静音))
        if getattr(self, "控制条", None) is not None:
            self.控制条.设置静音图标(bool(静音))
        self.状态标签.setText("🔇 静音" if 静音 else "🔊 取消静音")

    def _音频轨列表(self):
        try:
            return ([(t.编号, t.名称) for t in self._播放器().音频轨()]
                    if self._播放器() else [])
        except Exception:  # noqa: BLE001
            return []

    def _字幕轨列表(self):
        try:
            return ([(t.编号, t.名称) for t in self._播放器().字幕轨()]
                    if self._播放器() else [])
        except Exception:  # noqa: BLE001
            return []

    def _当前轨(self, 种类: str) -> int:
        try:
            if self._播放器() is None:
                return -1
            return int(self._播放器().当前字幕() if 种类 == "字幕" else -1)
        except Exception:  # noqa: BLE001
            return -1

    def _选择音频轨(self, 编号: int):
        if self._播放器() is not None:
            self._播放器().选择音频(int(编号))
            self.状态标签.setText(f"🔊 音频轨 → {编号}")

    def _选择字幕轨(self, 编号: int):
        if self._播放器() is not None:
            self._播放器().选择字幕(int(编号))
            self.状态标签.setText(f"💬 字幕轨 → {编号}")

    def 章节数(self) -> int:
        try:
            return int(self._播放器().章节数()) if self._播放器() else 0
        except Exception:  # noqa: BLE001
            return 0

    def 当前章节(self) -> int:
        try:
            return int(self._播放器().当前章节()) if self._播放器() else -1
        except Exception:  # noqa: BLE001
            return -1

    def 跳章节(self, 编号: int):
        if self._播放器() is not None:
            self._播放器().跳章节(int(编号))

    def 下一章(self):
        if self._播放器() is not None:
            self._播放器().下一章()

    def 上一章(self):
        if self._播放器() is not None:
            self._播放器().上一章()

    def 当前宽高比(self) -> str:
        try:
            return str(self._播放器().宽高比()) if self._播放器() else ""
        except Exception:  # noqa: BLE001
            return ""

    def _设置宽高比(self, 比例: str):
        if self._播放器() is not None:
            self._播放器().设置宽高比(比例)
            self.状态标签.setText(f"▭ 宽高比 {'默认' if not 比例 else 比例}")

    def 当前缩放(self) -> float:
        try:
            return float(self._播放器().缩放()) if self._播放器() else 0.0
        except Exception:  # noqa: BLE001
            return 0.0

    def _设置缩放(self, 倍率: float):
        if self._播放器() is not None:
            self._播放器().设置缩放(float(倍率))

    def _切换置顶(self, 选中: bool = False):
        窗口 = self.window()
        置顶 = bool(选中)
        try:
            窗口.setWindowFlag(Qt.WindowStaysOnTopHint, 置顶)
            窗口.show()
        except Exception:  # noqa: BLE001
            pass
        self.状态标签.setText("📌 主窗口置顶" if 置顶 else "主窗口不再置顶")

    def _添加字幕文件(self):
        路径, _ = QFileDialog.getOpenFileName(
            self, "选择字幕文件", str(Path.home()),
            "字幕 (*.srt *.ass *.ssa *.vtt);;所有文件 (*)")
        if not 路径 or self._播放器() is None:
            return
        if self._播放器().挂字幕文件(路径, 选中=True):
            self.状态标签.setText(f"💬 已挂字幕：{Path(路径).name}")
        else:
            self.状态标签.setText("💬 挂字幕失败（VLC 拒绝了）")

    def _显示播放参数(self):
        if self.会话 is None:
            return
        设置 = self.会话.设置
        self._AI写(f"[播放] 起播参数（{设置.来源}）：缓存 {设置.网络缓存毫秒}ms，"
                 f"硬解 {设置.硬解}")
        self.状态标签.setText(
            f"⚙️ 缓存 {设置.网络缓存毫秒}ms · 硬解 {设置.硬解}")

    # ---- 清单 ----

    def 把当前加入清单(self):
        if self.会话 is None or not str(getattr(self.会话, "远端路径", "") or ""):
            self.状态标签.setText("⚠️ 还没有正在播的视频")
            return
        项 = 播放项(标题=str(self.会话.标题 or "未命名"),
                 网盘标识=str(self.会话.网盘标识 or ""),
                 远端路径=str(self.会话.远端路径 or ""),
                 来源=str(self.会话.网盘标识 or "本地"))
        self.状态标签.setText("📋 已加入清单" if self.清单.添加(项)
                        else "📋 清单里已有这个视频")

    def _本地加入清单(self):
        路径, _ = QFileDialog.getOpenFileName(
            self, "加入清单：选择本地视频", str(Path.home()),
            "视频 (*.mp4 *.mkv *.avi *.mov *.ts *.m2ts *.webm *.flv);;所有文件 (*)")
        if not 路径:
            return
        项 = 播放项(标题=Path(路径).name, 本地路径=str(Path(路径).resolve()),
                 来源="本地")
        self.状态标签.setText("📋 已加入清单" if self.清单.添加(项)
                        else "📋 清单里已有这个视频")

    def _清单选了某项(self, 项):
        """清单里双击 / 上一个 / 下一个 → 播放这一项。"""
        if 项 is None:
            return
        if getattr(项, "是本地", False):
            self._播放本地文件(Path(项.本地路径))
            return
        idx = self.网盘框.findData(项.网盘标识)
        if idx >= 0:
            self.网盘框.setCurrentIndex(idx)
        self.路径框.setText(项.远端路径)
        self._播放()

    def 下一个(self):
        项 = self.清单.下一个项()
        if 项 is None:
            self.状态标签.setText("⏭ 清单里没有下一项")
            return
        self._清单选了某项(项)

    def 上一个(self):
        项 = self.清单.上一个项()
        if 项 is None:
            self.状态标签.setText("⏮ 清单里没有上一项")
            return
        self._清单选了某项(项)

    def _切换循环(self, *_):
        模式 = self.清单.切换循环()
        self.控制条.设置循环文本(self.清单.循环按钮文本())
        self.状态标签.setText(f"🔁 {模式}")

    def _切换随机(self, 选中: bool = False):
        # 勾选控件（控制条按钮）会传新的勾选态；菜单项点击则传 False → 取反
        self.清单.随机 = bool(选中) if 选中 else not self.清单.随机
        if getattr(self, "控制条", None) is not None:
            self.控制条.设置随机勾选(self.清单.随机)
        self.状态标签.setText("🔀 随机播放" if self.清单.随机 else "顺序播放")

    # ==================== AI 组件（懒加载） ====================

    def _AI客户端(self):
        """拿 AI 客户端：优先本地 DeepSeek（免费），其次云端。

        ⚠️ 必须把客户端交给字幕助手/播放顾问，否则它们会"规则降级"——
        翻译只会照抄、总结只是截断（实测踩到）。
        """
        AI助手 = None
        本地模型 = None
        try:
            运行时 = self.主窗口.AI运行时()
            if 运行时 is not None:
                AI助手 = getattr(运行时, "助手", None)
                本地模型 = getattr(AI助手, "本地模型", None) if AI助手 else None
        except Exception as e:  # noqa: BLE001
            self._AI写(f"[AI] 运行时不可用，AI 功能会降级：{e}")
        return AI助手, 本地模型

    def _准备AI(self) -> None:
        """按需装配 AI 播放顾问 / 字幕助手（缺模块也不影响播放）。"""
        AI助手, 本地模型 = self._AI客户端()
        来源 = ("本地模型" if (本地模型 is not None and
                          getattr(本地模型, "配置", None) is not None and
                          本地模型.配置.启用)
              else "云端 AI" if AI助手 is not None and AI助手.api密钥
              else "规则（无可用 AI）")
        self.AI来源 = 来源
        try:
            from ..AI.播放顾问 import 播放顾问
            self.顾问 = 播放顾问(AI助手=AI助手, 本地模型=本地模型,
                            日志回调=self._AI写)
        except Exception as e:  # noqa: BLE001
            self.顾问 = None
            self._AI写(f"[AI] 播放顾问不可用（不影响播放）：{e}")
        try:
            from ..AI.字幕助手 import 字幕助手
            self.字幕助手 = 字幕助手(AI助手=AI助手, 本地模型=本地模型,
                                日志回调=self._AI写)
        except Exception as e:  # noqa: BLE001
            self.字幕助手 = None
            self._AI写(f"[AI] 字幕助手不可用：{e}")
        self._AI写(f"[AI] 播放助手就绪：决策/翻译/总结走【{来源}】"
                 + ("（本地 CPU 推理较慢，长视频建议分批或改用云端）"
                    if 来源 == "本地模型" else ""))
        try:
            from ..播放.语音识别 import 语音识别器
            识别器 = 语音识别器(日志回调=self._AI写)
            可用, 说明 = 识别器.可用()
            self.语音识别器 = 识别器 if 可用 else None
            if not 可用:
                self._AI写(f"[字幕] 本地语音识别不可用：{说明.splitlines()[0]}")
        except Exception as e:  # noqa: BLE001
            self.语音识别器 = None
            self._AI写(f"[字幕] 语音识别模块未就绪：{type(e).__name__}")
        # 把装配好的客户端交给"AI 动作"对象（独立窗口也复用它）
        self.AI动作.字幕助手 = self.字幕助手
        self.AI动作.语音识别器 = self.语音识别器

    def _AI写(self, 文本: str) -> None:
        """写一行 AI 日志（**任何线程都可调用**）。

        实现要点：**在界面线程里直接写**（保证立即出现、顺序不乱），在别的线程里
        走 ``日志追加`` 信号（``Qt.QueuedConnection``）转回界面线程再写。
        为什么必须分流：VLC/探测/语音识别都会从各自线程回调日志，直接
        ``appendPlainText`` 属于跨线程碰控件 —— 实测会偶发段错误；而全部走队列又
        会让同线程调用不再立即生效（自检里"写完立刻读"就抓不到了）。
        """
        文本 = str(文本)
        try:
            from PySide6.QtCore import QThread
            if QThread.currentThread() is self.thread():
                self._写日志(文本)
                return
        except Exception:  # noqa: BLE001
            pass
        try:
            self.日志追加.emit(文本)
        except Exception:  # noqa: BLE001
            pass

    def _写日志(self, 文本: str) -> None:
        """真正的控件更新（只在界面线程执行）。"""
        try:
            self.AI输出.appendPlainText(f"[{datetime.now():%H:%M:%S}] {文本}")
            滚动 = self.AI输出.verticalScrollBar()
            滚动.setValue(滚动.maximum())
        except Exception:
            pass

    # ==================== 网盘 / 选片 ====================

    def resizeEvent(self, 事件):  # noqa: N802
        super().resizeEvent(事件)
        # 视频上方那行（工具栏）已撤销，不再需要宽度自适应

    def 刷新网盘列表(self) -> None:
        当前 = self.网盘框.currentData()
        self.网盘框.clear()
        try:
            规格表 = dict(self.动作.规格表 or {})
        except Exception:
            规格表 = {}
        for 标识, 规格 in 规格表.items():
            if not 标识:
                continue
            self.网盘框.addItem(str(getattr(规格, "显示名", "") or 标识), 标识)
        if 当前:
            idx = self.网盘框.findData(当前)
            if idx >= 0:
                self.网盘框.setCurrentIndex(idx)
        elif self.网盘框.count():
            self.网盘框.setCurrentIndex(0)

    # ==================== 选片（与传输页同一套交互）====================

    def _浏览(self) -> None:
        """用「跨网盘传输」页同款的路径选择对话框挑视频文件。"""
        标识 = str(self.网盘框.currentData() or "")
        规格 = self.动作.规格(标识) if 标识 else None
        if 规格 is None:
            self.状态标签.setText("⚠️ 请先选择网盘（左下角可新增网盘）")
            return
        try:
            适配器 = self.动作.适配器(标识)
        except Exception as e:  # noqa: BLE001
            self.状态标签.setText(f"❌ 适配器不可用：{e}")
            return
        初始 = self.路径框.text().strip() or "/"
        # 路径框里大概率是**正在播的那个文件**（例如 /来自：分享/…/xxx.mp4）；
        # 直接拿它当目录去列，桥会回"不是目录"（用户实测到这个弹窗）。
        # 这里退到它的父目录，交互上也更符合直觉：从片子所在目录开始挑。
        if 是视频文件(Path(初始).name) or 初始.lower().endswith(
                (".srt", ".ass", ".ssa", ".vtt")):
            父 = str(Path(初始).parent)
            初始 = 父 if 父 and 父 != "." else "/"
            if not 初始.startswith("/"):
                初始 = "/" + 初始
        对话框 = 路径选择对话框(
            适配器, 标识, 初始路径=初始,
            允许选择文件=True,        # 视频是文件，所以允许选到文件
            允许新建=False,           # 播放不需要新建目录
            标题=f"选择要播放的视频 · {规格.显示名}",
            说明="双击进入文件夹；选中视频文件后点「确定」即可播放。"
                 "（与跨网盘传输页用的是同一个选择器，这里多了一层「只收视频」过滤）",
            只要文件=True,            # 选到目录不给确定：避免把目录交给播放器
            名字过滤=是视频文件,
            过滤提示="不是常见的视频文件（音频/图片/文档会被灰掉、不可选）",
            网盘列表=self.动作.规格表,          # 对话框里可以直接换网盘（需求）
            取适配器=self.动作.适配器,
            父窗口=self)
        if 对话框.exec() == QDialog.Accepted and 对话框.选中路径:
            self.路径框.setText(对话框.选中路径)
            选中盘 = str(getattr(对话框, "选中网盘标识", "") or "")
            if 选中盘 and 选中盘 != 标识:
                # 在对话框里换了网盘：页面的下拉也要跟着切，否则下次播的还是老盘
                idx2 = self.网盘框.findData(选中盘)
                if idx2 >= 0:
                    self.网盘框.setCurrentIndex(idx2)
                    self._AI写(f"[播放] 选择对话框里把网盘切到了 "
                            f"{self.网盘框.currentText()}")
            名称 = str(对话框.选中路径).rsplit("/", 1)[-1]
            if not 是视频文件(名称):
                # 不硬拦：有些流媒体后缀没列全，但要说清楚
                self._AI写(f"[播放] 注意：{名称} 看起来不是常见视频后缀，仍尝试播放")
            self._播放()

    def 播放指定视频(self, 标识: str, 远端路径: str,
                独立窗口: bool = False) -> None:
        """外部入口（网盘页双击视频就走这里）。"""
        idx = self.网盘框.findData(标识)
        if idx >= 0:
            self.网盘框.setCurrentIndex(idx)
        self.路径框.setText(str(远端路径))
        if 独立窗口:
            self._播放(独立窗口=True)
        else:
            self._播放()

    def _打开本地(self) -> None:
        路径, _ = QFileDialog.getOpenFileName(
            self, "选择本地视频", str(Path.home()),
            "视频 (*.mp4 *.mkv *.avi *.mov *.flv *.ts *.webm *.m4v);;所有文件 (*)")
        if not 路径:
            return
        self.路径框.setText(路径)
        self._播放(本地=True)

    # ==================== 播放控制 ====================

    def _新会话(self) -> 播放会话:
        if self.会话 is not None:
            return self.会话
        self.会话 = 播放会话(
            取适配器=self.动作.适配器,
            日志回调=self._AI写, 顾问=self.顾问,
            自动调优=True)
        return self.会话

    def _播放(self, 本地: bool = False, 独立窗口: bool = False) -> None:
        self._本次独立窗口 = bool(独立窗口)
        if not vlc可用():
            self.状态标签.setText(f"❌ 播放器不可用：{不可用原因().splitlines()[0]}")
            self.信息栏.setText(f"❌ libvlc 不可用：{不可用原因().splitlines()[0]}")
            return
        路径 = self.路径框.text().strip()
        if not 路径:
            self.状态标签.setText("⚠️ 先填视频路径，或点「📂 浏览…」选一个视频")
            return
        目标 = Path(路径)
        if 本地 or 目标.is_file():
            self._播放本地文件(目标)
            return
        标识 = str(self.网盘框.currentData() or "")
        if not 标识:
            # 用状态栏提示而不是模态框：模态框在无人值守/自动化场景会把进程挂死
            # （实测：离屏截图时卡在这里，faulthandler 定位到 QMessageBox）。
            self.状态标签.setText("⚠️ 左侧还没有可用网盘：先「新增网盘」")
            self.信息栏.setText("⚠️ 没有可用网盘")
            return
        # 先把"选到了目录"这种情况挡住：以前直接把目录交给桥去取直链，
        # 桥抛 IsADirectoryError（还刷一屏 traceback），界面只会显示"准备失败"。
        是目录, 目录说明 = self._是不是目录(标识, 路径)
        if 是目录:
            self.状态标签.setText(f"📁 {目录说明}")
            self.信息栏.setText(
                f"📁 这是一条**目录**路径：{路径}\n"
                "播放器只能播文件。点「📂 浏览…」进去选一个视频，"
                "或双击网盘文件表格里的视频。")
            self.路径框.setText(路径 if 路径.endswith("/") else 路径 + "/")
            return
        会话 = self._新会话()
        会话.自动调优 = bool(self.自动调优框.isChecked())
        self.状态标签.setText("⏳ 正在解析直链 / 探测带宽 / 决策参数…")
        self.信息栏.setText("⏳ 正在准备播放…")
        self.播放按钮.setEnabled(False)

        def 干活():
            try:
                摘要 = 会话.准备(标识, 路径)
                self.状态更新.emit({"类型": "准备完成", "摘要": 摘要})
            except Exception as e:  # noqa: BLE001
                self.状态更新.emit({"类型": "准备失败", "错误": str(e)})

        线程 = threading.Thread(target=干活, name="播放准备", daemon=True)
        线程.start()
        self._子线程.append(线程)

    def _是不是目录(self, 标识: str, 路径: str) -> tuple[bool, str]:
        """问适配器这条路径是不是目录（问不到就说"不知道"，仍然按文件试）。

        为什么要单独问：光看名字判断不了（分享目录常叫
        ``xx.4K.60fps`` 这种），而把目录交给"取播放直链"必然报错。
        """
        if not 标识:
            return False, ""
        try:
            适配器 = self.动作.适配器(标识)
        except Exception:  # noqa: BLE001
            return False, ""
        try:
            信息 = 适配器.文件信息(路径)
        except Exception:  # noqa: BLE001
            return False, ""          # 问不到就别拦，按文件试（报错也有友好提示）
        if 信息 is None:
            return False, ""
        if isinstance(信息, dict):
            是目录 = bool(信息.get("is_dir"))
            名 = str(信息.get("name") or "")
        else:
            是目录 = bool(getattr(信息, "is_dir", False))
            名 = str(getattr(信息, "name", "") or "")
        if 是目录:
            return True, f"{名 or 路径} 是目录，不是视频文件"
        return False, ""

    def _播放本地文件(self, 路径: Path) -> None:
        会话 = self._新会话()
        会话.自动调优 = bool(self.自动调优框.isChecked())
        try:
            会话.直链信息 = {"url": str(路径), "headers": {}, "name": 路径.name,
                        "size": 路径.stat().st_size}
            会话.网盘标识 = "本地"
            会话.远端路径 = str(路径)
            会话.标题 = 路径.name
            from ..播放.媒体信息 import 探测媒体
            会话.媒体 = 探测媒体(str(路径))
            会话.设置 = 会话._决策参数({"大小": 路径.stat().st_size})
            self._真正起播()
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "播放失败", str(e))

    def _真正起播(self) -> None:
        会话 = self.会话
        if 会话 is None:
            return
        try:
            # 必须先让视频容器有原生窗口，再把句柄交给 libvlc
            self.视频.show()
            self.视频.update()
            QTimer.singleShot(0, self._起播二段)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "播放失败", str(e))

    def _安全句柄(self) -> int:
        """给 libvlc 的窗口句柄；**没有真实显示环境时返回 0**（不嵌窗口）。

        为什么必须判断：离屏/无头环境（CI、界面自检）里 ``winId()`` 是假窗口，
        libvlc 往里画会**直接段错误崩掉整个进程**（实测 exit 139）。
        返回 0 时 VLC 用无窗口模式解码——链路照样验证，程序不会死。
        """
        try:
            from PySide6.QtGui import QGuiApplication
            from ..播放.显示环境 import 可嵌入窗口
            if not 可嵌入窗口(QGuiApplication.platformName()):
                # Wayland/离屏等平台的 winId() **不是 X11 窗口号**，交给 libvlc
                # 会段错误（实测闪退），所以一律退回无窗口模式
                return 0
        except Exception:
            pass
        try:
            return int(self.视频.winId())
        except Exception:
            return 0

    def _已映射(self) -> bool:
        """播放页的视频窗是否已经真的映射到屏幕（只有 X11 需要等）。"""
        try:
            from ..播放.显示环境 import 可嵌入窗口 as _可嵌
            from PySide6.QtGui import QGuiApplication as _App
            if not _可嵌(_App.platformName()):
                return True          # 离屏/无头平台没有嵌入这回事，别卡住起播
            if not self.视频.isVisible():
                return False
            句柄 = self.视频.windowHandle()
            就绪 = bool(句柄.isExposed()) if (句柄 is not None
                                        and hasattr(句柄, "isExposed")) else True
            if not 就绪:
                self._映射重试 = getattr(self, "_映射重试", 0) + 1
                if self._映射重试 > 15:
                    self._AI写("[显示] 窗口映射等待超时，按现状起播（会巡检自愈）")
                    return True
            else:
                self._映射重试 = 0
            return 就绪
        except Exception:  # noqa: BLE001
            return True

    def _起守护(self) -> None:
        """起"游离窗口"巡检：libvlc 要是自己开窗口放视频，就自动收回到页面里。"""
        if getattr(self, "_守护", None) is None:
            from .游离窗口守护 import 游离窗口守护
            self._守护 = 游离窗口守护(
                self,
                取自己窗口号们=lambda: [int(self.winId()),
                                 int(self.视频.winId())],
                自愈回调=self._自愈画面,
                日志=self._AI写,
                间隔毫秒=3000, 巡检次数=0)      # 0 = 无限，播放期间一直盯着
        self._守护.开始(无限=True)

    def _自愈画面(self) -> None:
        """把画面收回播放页（同一个播放器：**先停住** → 绑句柄 → 重开媒体 → 跳回位置）。

        ⚠️ 必须先停：播放中 set_xwindow 不会搬走已有的视频输出，直接重播会在新句柄上
        **再开一个 vout** —— 那就是"多出来一个超出屏幕的大窗口、两个窗口都在放"的来源。
        """
        if self.会话 is None or self.会话.播放器 is None:
            return
        位置 = 0.0
        try:
            位置 = float(self.会话.播放器.进度秒())
        except Exception:  # noqa: BLE001
            位置 = 0.0
        try:
            已停 = bool(self.会话.播放器.停止并等待(3.0))
        except Exception:
            已停 = True
        if not 已停:
            self._AI写("[显示] ⚠️ 播放器没能及时停住，仍尝试收回画面"
                     "（若出现第二个窗口，用「独立窗口」按钮或重开播放）")
        if self.会话.起播(self._安全句柄()):
            def _跳(秒=位置):
                try:
                    if self.会话 is not None and self.会话.播放器 is not None:
                        self.会话.跳转(秒)
                except Exception:  # noqa: BLE001
                    pass
            QTimer.singleShot(900, _跳)
            self._AI写(f"[显示] 画面已收回播放页（从 {位置:.0f}s 继续）")
        self.定时器.start()

    def _起播二段(self) -> None:
        会话 = self.会话
        # 巡检要**在起播前**就开始：VLC 恰恰是在 set_xwindow 那一刻自己开窗口的，
        # 等起播成功再开巡检，头几秒的"分离"就漏过去了（用户实测的现象）
        self._起守护()
        # 窗口还没映射好就把句柄交给 libvlc，它会自己开窗口放（"视频和播放器分离"）
        if not self._已映射() and not getattr(self, "_本次独立窗口", False):
            self.状态标签.setText("⏳ 等待视频窗口就绪…")
            QTimer.singleShot(90, self._起播二段)
            return
        # 句柄为 0 且是桌面平台时拦住：libvlc 会**自己开一个 VLC 窗口**放画面，
         # 那个窗口不归本程序管（实测：标题栏 "VLC media player"、比屏幕还大）。
        if 会话 is not None:
            句柄 = self._安全句柄()
            if 句柄 == 0 and 是桌面平台() and not getattr(self, "_允许自带窗口", False):
                from ..播放.显示环境 import 无窗口原因
                from PySide6.QtGui import QGuiApplication as _App
                self.播放按钮.setEnabled(True)
                self.状态标签.setText("❌ 无法把画面嵌进本窗口（窗口号不是 X11 的）")
                self.信息栏.setText("❌ " + 无窗口原因(_App.platformName()))
                self._AI写("[显示] ⚠️ 没有可用的 X11 窗口号，已阻止开播："
                        + 无窗口原因(_App.platformName()))
                return
            self._AI写(f"[显示] 平台 {self._平台名()} · 窗口句柄 {句柄}"
                    + ("（嵌入本页）" if 句柄 else "（无窗口解码）"))
        if getattr(self, "_本次独立窗口", False):
            # 要独立窗口：**建窗口 → 让同一个会话起播到窗口里 → 窗口接管**
            self._本次独立窗口 = False
            self.播放按钮.setEnabled(True)
            if self._独立窗口 is not None:
                self._独立窗口.raise_()
                self._独立窗口.activateWindow()
                return
            窗口 = self._建独立窗口(str(getattr(会话, "标题", "") or "视频"))
            句柄 = 窗口._安全句柄() if hasattr(窗口, "_安全句柄") else 0
            窗口.show()
            if hasattr(窗口, "_置顶"):
                窗口._置顶()
            if 会话.起播(句柄):
                窗口.接管播放()
                self.状态标签.setText(
                    "⏳ 独立窗口正在就绪…"
                    if getattr(窗口, "_等待映射中", False)
                    else "🗗 已在独立窗口播放（同一个播放会话）")
            else:
                self.状态标签.setText("❌ 独立窗口起播失败")
            窗口.定时器.start()
            return
        self._关闭独立窗口()
        try:
            句柄 = self._安全句柄()
            if not 句柄:
                self._AI写("[播放] 当前是无显示环境：用无窗口模式解码"
                         "（链路可用，但看不到画面）")
            成功 = 会话.起播(句柄)
        except Exception as e:  # noqa: BLE001
            成功 = False
            self._AI写(f"[播放] 起播异常：{e}")
        self.播放按钮.setEnabled(True)
        if not 成功:
            self.状态标签.setText("❌ 起播失败")
            self.信息栏.setText("❌ 起播失败（VLC 拒绝了这条直链）")
            return
        self.定时器.start()
        self.控制条.设置循环文本(self.清单.循环按钮文本())
        self.控制条.设置随机勾选(self.清单.随机)
        self.状态标签.setText("▶ 播放中")
        self._起守护()
        续 = float(getattr(self, "_续播位置", 0.0) or 0.0)
        if 续 > 1.0:
            self._续播位置 = 0.0
            # 刚起播就 seek 可能被忽略，等一会儿再跳
            QTimer.singleShot(1200, lambda 秒=续: self._跳到续播位置(秒))
        设置 = 会话.设置
        媒体 = 会话.媒体
        后台 = getattr(会话, "AI决策状态", "")
        self.信息栏.setText(
            f"🎬 {会话.标题}　｜　{媒体.摘要()}　｜　{会话.探测.摘要()}"
            f"　｜　起播参数（{设置.来源}）：缓存 {设置.网络缓存毫秒}ms · "
            f"硬解 {设置.硬解}　｜　{设置.理由}"
            + (f"　⚠️ {设置.风险}" if 设置.风险 else "")
            + (f"　｜　🤖 {后台}" if 后台 else ""))
        if not 设置.可流畅播放:
            self._AI写(f"⚠️ AI 判断这条视频当前网络下**可能卡顿**：{设置.风险}")
        self._AI写(f"[播放] 已起播：{会话.标题}")

    @staticmethod
    def _平台名() -> str:
        try:
            from PySide6.QtGui import QGuiApplication as _App
            return str(_App.platformName() or "?")
        except Exception:  # noqa: BLE001
            return "?"

    def 用VLC自带窗口播放(self):
        """用户明确同意后：允许句柄 0（VLC 自己开窗口放）。"""
        self._允许自带窗口 = True
        self._AI写("[显示] 已改用 VLC 自带窗口播放（该窗口不受本程序控制）")
        if self.会话 is not None:
            self._起播二段()

    def _跳到续播位置(self, 秒: float) -> None:
        if self.会话 is None:
            return
        self.会话.跳转(float(秒))
        self._AI写(f"[播放] 已跳到 {self._时间文本(秒)} 继续播放")

    def _活动播放器(self):
        """当前真正在播的那一路（独立窗口优先）。

        需求里"播放页的暂停/停止无效"就是这个原因：视频交给独立窗口后，页面
        自己的会话已清空，按钮点在空会话上毫无反应 —— 所以统一路由到活动那一路。
        """
        窗口 = getattr(self, "_独立窗口", None)
        if 窗口 is not None and getattr(窗口, "会话", None) is not None:
            return 窗口
        return self

    def _播放暂停(self) -> None:
        目标 = self._活动播放器()
        if 目标 is self:
            if self.会话 is not None:
                self.会话.暂停()
            return
        目标._播放暂停()

    def _停止(self) -> None:
        目标 = self._活动播放器()
        if 目标 is not self:
            目标._停止()
            self.状态标签.setText("⏹ 已在独立窗口停止")
            return
        if self.会话 is None:
            return
        self.会话.关闭()
        self.定时器.stop()
        if getattr(self, "_守护", None) is not None:
            self._守护.停止()
        self.进度条.setValue(0)
        self.时间标签.setText("00:00 / 00:00")
        self.状态标签.setText("⏹ 已停止")

    def _跳转比例(self, 比例: float) -> None:
        """控制条拖完进度条 → 按比例跳转（0~1）。"""
        self._拖拽中 = False
        if self.会话 is None:
            return
        时长 = self.会话.播放器.时长秒() if self.会话.播放器 else 0
        if 时长 > 0:
            self.会话.跳转(时长 * max(0.0, min(1.0, float(比例))))

    #: 兼容旧名字（自检/外部脚本里用过）
    _进度拖动完成 = _跳转比例

    def _音量变化(self, 值: int) -> None:
        if self.会话:
            self.会话.设置音量(int(值))

    def _倍速变化(self, 倍速: float | None = None) -> None:
        if self.会话:
            if 倍速 is None:
                倍速 = float(self.倍速框.currentData() or 1.0)
            self.会话.设置速率(float(倍速))

    def _切换字幕(self) -> None:
        if self.会话 is None or self.会话.播放器 is None:
            return
        编号 = self.会话.切换字幕()
        轨道 = self.会话.播放器.字幕轨()
        名称 = next((t.名称 for t in 轨道 if t.编号 == 编号), "关闭")
        self.状态标签.setText(f"💬 字幕：{名称}")

    # ==================== 独立窗口 ====================

    def 打开独立窗口(self) -> None:
        """把当前画面交给独立窗口（**同一个播放会话**，只把 vout 挪过去）。

        为什么不再新建会话：以前这里会 `会话.关闭()` 再 new 一个播放会话，于是同时
        存在过两个 libvlc player —— 用户实测到"画面跑到另一个窗口里、我们的窗口是黑的"、
        "关掉一个另一个还在响"。现在全局只有一个 player，谁在台前就把画面绑到谁那。
        """
        会话 = self.会话
        if 会话 is None or not str(getattr(会话, "远端路径", "") or ""):
            self.状态标签.setText("⚠️ 先选好视频（双击网盘里的视频，或点「▶ 播放」）")
            return
        if not dict(会话.直链信息 or {}).get("url"):
            self.状态标签.setText("⚠️ 还没拿到直链，先点一次「▶ 播放」")
            return
        if 会话.播放器 is None:
            # 还没起播（只准备过）：先在页面里起播，再交给窗口
            self._本次独立窗口 = True
            self._真正起播()
            return
        if self._独立窗口 is not None:
            self._独立窗口.raise_()
            self._独立窗口.activateWindow()
            return
        窗口 = self._建独立窗口(会话.标题)
        if not 窗口.接管播放():
            self.状态标签.setText("❌ 独立窗口接管播放失败")
            try:
                窗口.close()
            except Exception:  # noqa: BLE001
                pass
            return
        self.状态标签.setText("🗗 画面已交给独立窗口（同一个播放，位置/声音不中断）")
        self._AI写(f"[播放] 画面已交给独立窗口：{会话.标题}"
                 f"（{会话.播放器.进度秒():.0f}s 处继续，不重新起播）")

    def _建独立窗口(self, 标题: str):
        """建窗口并接好宿主回调（单播放器：窗口只驱动页面这个会话）。"""
        from .播放器窗口 import 播放器窗口      # 延迟导入：避免与页面的循环依赖
        窗口 = 播放器窗口(self.会话, 标题=标题, 日志回调=self._AI写,
                     AI动作=self.AI动作,
                     自动调优=bool(self.自动调优框.isChecked()),
                     接管=True)
        窗口.setAttribute(Qt.WA_DeleteOnClose, True)
        # destroyed 只清引用：**不要**再走"已关闭"提示，否则会把"回到播放页继续播"
        # 的状态盖掉（顺序上 destroyed 在 closeEvent 之后）
        窗口.destroyed.connect(lambda *_: setattr(self, "_独立窗口", None))
        self._接好独立窗口(窗口)
        self._独立窗口 = 窗口
        return 窗口

    def 收回播放到页面(self, 窗口=None) -> None:
        """独立窗口关掉后：把画面收回播放页（**同一个会话/播放器**）。

        与接管时同理：播放中改 set_xwindow 不会真的搬 vout，所以这里是
        "绑窗口 → 重开媒体 → 跳回原位置"。同一个播放器意味着**不会有两路声音**。
        """
        if self.会话 is None:
            return
        位置 = 0.0
        try:
            位置 = float(self.会话.播放器.进度秒()) if self.会话.播放器 else 0.0
        except Exception:  # noqa: BLE001
            位置 = 0.0
        句柄 = self._安全句柄()
        if 句柄 == 0 and 是桌面平台() and not getattr(self, "_允许自带窗口", False):
            self.状态标签.setText("❌ 无法把画面收回播放页（窗口号不是 X11 的）")
            return
        if not self.会话.起播(句柄):
            self.状态标签.setText("⚠️ 收回播放失败（直链可能已过期，请重新播放）")
            self._AI写("[播放] 回合播放失败：直链可能过期")
            return
        if 位置 > 1.0:
            def _跳(秒=位置):
                try:
                    if self.会话 is not None and self.会话.播放器 is not None:
                        self.会话.跳转(秒)
                except Exception:  # noqa: BLE001
                    pass
            QTimer.singleShot(900, _跳)
            QTimer.singleShot(1800, _跳)
        self.定时器.start()
        self.状态标签.setText(f"▶ 已回到播放页继续播放（从 {位置:.0f}s 接着放）")
        self._AI写("[播放] 独立窗口已关闭，画面已收回播放页继续播放")

    def _接好独立窗口(self, 窗口) -> None:
        """把独立窗口接到页面的能力上：宿主回调、清单续播、AI 结果路由。"""
        窗口.宿主回调 = {
            "打开网盘": self._浏览,
            "打开本地": self._打开本地,
            "添加本地到清单": self._本地加入清单,
            "添加字幕文件": self._添加字幕文件,
            # 单播放器架构：窗口关掉时把画面还回播放页（不关会话）
            "归还播放": self.收回播放到页面,
        }
        窗口.请求播放项.connect(
            lambda 项, w=窗口: self._在独立窗口播这一项(w, 项))
        窗口.状态更新.connect(窗口.AI结果, Qt.QueuedConnection)
        # 关窗交接（带位置/直链）→ 页面接着播；QueuedConnection 落回界面线程
        窗口.状态更新.connect(
            lambda 数据: self._独立窗口关了(数据)
            if str((数据 or {}).get("类型")) == "独立窗口关闭" else None,
            Qt.QueuedConnection)
        # AI 结果同时进页面面板与窗口面板；**从工作线程 emit 是安全的**，
        # 两边都用 QueuedConnection 落回界面线程（跨线程碰控件会段错误）
        原状态 = self.AI动作.状态

        def 双份(数据):
            数据 = dict(数据 or {})
            原状态(数据)
            try:
                窗口.状态更新.emit(数据)
            except Exception:  # noqa: BLE001
                pass

        self.AI动作.状态 = 双份

    def _在独立窗口播这一项(self, 窗口, 项) -> None:
        """清单里选了某一项 → 取直链（后台）→ 在**同一个**窗口里继续播。"""
        if 项 is None:
            return
        标题 = str(getattr(项, "标题", "") or "视频")
        if getattr(项, "是本地", False):
            路径 = Path(项.本地路径)
            会话 = self._新会话(独立=True)
            try:
                会话.直链信息 = {"url": str(路径), "headers": {},
                             "name": 路径.name, "size": 路径.stat().st_size}
            except OSError:
                会话.直链信息 = {"url": str(路径), "headers": {}, "name": 路径.name,
                             "size": 0}
            会话.网盘标识 = "本地"
            会话.远端路径 = str(路径)
            会话.标题 = 路径.name
            from ..播放.媒体信息 import 探测媒体
            from ..播放.播放核心 import 规则参数
            from ..播放.直链探测 import 探测结果
            会话.媒体 = 探测媒体(str(路径))
            会话.探测 = 探测结果(成功=True, 来源说明="本地文件（不经过网络，无需测带宽）")
            会话.设置 = 规则参数(会话.媒体, 会话.探测, 会话._探测本机硬解())
            窗口.换会话(会话, 标题=路径.name)
            if not 窗口.起播():
                self._AI写(f"[播放] 独立窗口起播失败：{路径.name}")
            return
        会话 = self.会话 if self.会话 is not None else self._新会话()
        窗口.换会话(会话, 标题=标题)
        窗口.状态标签.setText(f"⏳ 正在准备：{标题}")

        def 干活():
            try:
                摘要 = 会话.准备(项.网盘标识, 项.远端路径)
                self.状态更新.emit({"类型": "窗口准备完成", "会话": 会话,
                                "窗口": 窗口, "摘要": 摘要})
            except Exception as e:  # noqa: BLE001
                self.状态更新.emit({"类型": "窗口准备失败", "窗口": 窗口,
                                "错误": str(e)})

        线程 = threading.Thread(target=干活, name="窗口播放准备", daemon=True)
        线程.start()
        self._子线程.append(线程)

    def _新会话(self, 独立: bool = False) -> 播放会话:
        """建一个播放会话；``独立=True`` 时**总是新建**（给独立窗口用）。

        ⚠️ 页面这路（``独立=False``）必须把会话**存回 self.会话** —— 重构时漏过
        这一句，结果播放本身能跑，但暂停/进度/开独立窗口全都失效（页面里
        ``self.会话`` 一直是 None）。
        """
        if not 独立 and self.会话 is not None:
            return self.会话
        会话 = 播放会话(取适配器=self.动作.适配器, 日志回调=self._AI写,
                     顾问=self.顾问,
                     自动调优=bool(self.自动调优框.isChecked()))
        if not 独立:
            self.会话 = 会话
        return 会话

    def _关闭独立窗口(self) -> None:
        窗口 = getattr(self, "_独立窗口", None)
        if 窗口 is not None:
            try:
                窗口.close()
            except Exception:  # noqa: BLE001
                pass
            self._独立窗口 = None

    def _独立窗口关了(self, 交接: dict | None = None) -> None:
        """独立窗口关掉后：**把视频接回播放页继续播**（需求要求）。

        窗口关窗前会把当前位置与直链打包过来（直链有时效，重新取可能已过期，
        还要多等一次网络往返），这里直接用那份信息在页面里接着放。
        """
        self._独立窗口 = None
        交接 = dict(交接 or {})
        直链 = dict(交接.get("直链信息") or {})
        if 交接.get("接管"):
            # 单播放器：会话一直在播，收回画面就行（收回动作在"归还播放"里已做，
            # 这里只补状态与日志，**绝不能**再起播一次，否则会有两路声音）
            if self.会话 is not None and self.会话.播放器 is not None:
                self.定时器.start()
            self.状态标签.setText("▶ 已回到播放页继续播放")
            return
        if str(交接.get("类型")) == "独立窗口关闭" and 直链.get("url"):
            位置 = float(交接.get("位置秒") or 0.0)
            会话 = self._新会话()
            会话.网盘标识 = str(交接.get("网盘标识") or "")
            会话.远端路径 = str(交接.get("远端路径") or "")
            会话.标题 = str(交接.get("标题") or "视频")
            会话.直链信息 = 直链
            会话.媒体 = 交接.get("媒体")
            会话.探测 = 交接.get("探测")
            if 交接.get("设置") is not None:
                会话.设置 = 交接["设置"]
            self._续播位置 = 位置
            self.状态标签.setText(
                f"▶ 收到独立窗口的播放，正在接着播（{self._时间文本(位置)}）…")
            self._AI写(f"[播放] 独立窗口已关闭，回到播放页从 {位置:.0f}s 继续")
            self._真正起播()
            return
        self.状态标签.setText("🗗 独立窗口已关闭（要看就再点「▶ 播放」）")
        self._AI写("[播放] 独立窗口已关闭")

    def 独立窗口播放(self) -> None:
        """按钮入口：拿直链（若还没有）→ 在**同一个会话**里起播到独立窗口。"""
        if self.会话 is not None and self.会话.播放器 is not None:
            self.打开独立窗口()
            return
        self._播放(独立窗口=True)

    def _设置全屏(self, 全屏: bool):
        """菜单/工具栏的「全屏」勾选项（VLC 的 视频→全屏）。"""
        窗口 = self.window()
        if bool(全屏) == bool(窗口.isFullScreen()):
            return
        if not 全屏:
            窗口.showNormal()
            if self._全屏前状态:
                self.分隔.setSizes(self._全屏前状态)
            return
        self._全屏前状态 = self.分隔.sizes()
        self.分隔.setSizes([self.分隔.height(), 0])
        窗口.showFullScreen()
        同步 = getattr(getattr(self, "工具栏", None), "全屏动作", None)
        if 同步 is not None:
            同步.setChecked(True)

    def _切换全屏(self) -> None:
        窗口 = self.window()
        if 窗口.isFullScreen():
            窗口.showNormal()
            if self._全屏前状态:
                self.分隔.setSizes(self._全屏前状态)
            return
        self._全屏前状态 = self.分隔.sizes()
        self.分隔.setSizes([self.分隔.height(), 0])
        窗口.showFullScreen()

    def _截图(self) -> None:
        if self.会话 is None or self.会话.播放器 is None:
            return
        目录 = Path(self.主窗口.配置.get("缓存", {}).get("目录") or "") \
            if isinstance(self.主窗口.配置, dict) else Path("")
        保存 = Path("数据/截图")
        保存.mkdir(parents=True, exist_ok=True)
        名称 = f"{datetime.now():%Y%m%d_%H%M%S}.png"
        目标 = 保存 / 名称
        if self.会话.截图(str(目标)):
            self.状态标签.setText(f"📷 已保存 {目标}")
            self._AI写(f"[播放] 截图：{目标}")
        else:
            self.状态标签.setText("📷 截图失败（可能还没画面）")

    # ==================== AI 面板动作（实现搬到 AI播放面板.AI字幕动作） ====================

    def _缓存字幕目录(self) -> Path:
        return self.AI动作.缓存字幕目录()

    def _取字幕条目(self, 允许联网: bool = False):
        return self.AI动作.取字幕条目(允许联网=允许联网)

    def _当前字幕条目(self):
        return self._取字幕条目(允许联网=False)[0]

    def _AI翻译字幕(self) -> None:
        self.AI面板.设置忙碌(True)
        self.AI动作.翻译字幕()

    def _AI生成字幕(self) -> None:
        self.AI面板.设置忙碌(True)
        self.AI动作.生成字幕()

    def _AI总结(self) -> None:
        self.AI面板.设置忙碌(True)
        self.AI动作.总结()

    def _手动诊断(self) -> None:
        self.AI动作.诊断()

    def 流畅优先(self) -> None:
        """一键流畅优先：允许丢帧 + GPU 缩放(vout=gl) + 缓存加大，并从原位置重载。"""
        if self.会话 is None:
            self.状态标签.setText("⚠️ 先播放一个视频再用「流畅优先」")
            return
        新设置 = self.会话.流畅优先()
        self.状态标签.setText(
            f"⚡ 流畅优先：缓存 {新设置.网络缓存毫秒}ms · 允许丢帧 · vout=gl（重载中）")
        self._AI写("[播放] ⚡ 流畅优先：允许丢迟到帧 + vout=gl + 缓存加大，正在从当前位置重载")
        self.会话.应用新参数(新设置.to_dict(), 自动重载=True)

    # ==================== 状态刷新 ====================

    def _刷新状态(self) -> None:
        会话 = self.会话
        if 会话 is None or 会话.播放器 is None:
            return
        快照 = 会话.状态快照()
        if not self._拖拽中 and 快照["时长秒"] > 0:
            self.进度条.setValue(
                int(1000 * 快照["进度秒"] / 快照["时长秒"]))
        self.时间标签.setText(
            f"{self._时间文本(快照['进度秒'])} / {self._时间文本(快照['时长秒'])}")
        状态 = 快照["状态"]
        if 快照["缓冲中"]:
            状态 += "（缓冲…）"
        self.状态标签.setText(
            f"{状态} · 丢帧 {快照['丢帧']} · 输入码率 "
            f"{快照['输入码率bps']:.2f} Mbps · 已播 {快照['已播秒']:.0f}s")
        # AI 后台决策的结果：变了就写进 AI 面板（只写一次）
        决策 = getattr(会话, "AI决策状态", "")
        if 决策 and 决策 != getattr(self, "_上次决策", ""):
            self._上次决策 = 决策
            self._AI写(f"[AI 决策] {决策}")
            self.信息栏.setText(self.信息栏.text().split("　｜　🤖")[0]
                            + f"　｜　🤖 {决策}")
        # 自动调优：**后台线程**里诊断。
        # 为什么必须异步：诊断会问本地大模型，一次 40~50 秒；以前是同步调用，
        # 界面（含视频窗口）会整整卡住一分钟 —— 播放中卡界面是不能接受的。
        if (self.自动调优框.isChecked() and not self._诊断中
                and self._该自动诊断了吗(快照)):
            self._诊断中 = True
            self._上次诊断时间 = time.time()

            def 诊断():
                try:
                    建议 = 会话.采样并诊断()
                except Exception as e:  # noqa: BLE001
                    建议 = {"需要调整": False, "理由": f"诊断失败：{e}"}
                finally:
                    self._诊断中 = False
                if 建议:
                    self.状态更新.emit({"类型": "调优建议", "建议": 建议})

            threading.Thread(target=诊断, name="播放诊断", daemon=True).start()

    #: 两次自动诊断之间的最短间隔（秒）。以前是"每 500ms 无条件诊断"——
    #: 本地模型没起来时诊断瞬间返回，于是**每秒刷一轮日志**，用户完全没法看。
    _诊断冷却秒 = 90.0

    def _该自动诊断了吗(self, 快照: dict) -> bool:
        """只在**真的在掉帧 / 在缓冲**且离上次诊断够久时才诊断。

        这样日志里只留下"确实需要调优"的时刻，不再有周期性噪音。
        """
        现在 = time.time()
        上次 = float(getattr(self, "_上次诊断时间", 0.0) or 0.0)
        if 现在 - 上次 < self._诊断冷却秒:
            return False
        会话 = self.会话
        if 会话 is None or 会话.播放器 is None:
            return False
        状态 = str(快照.get("状态") or "")
        if 状态 not in ("播放中", "缓冲中", "暂停") and "播" not in 状态:
            return False
        丢帧 = int(快照.get("丢帧") or 0)
        上次丢帧 = int(getattr(self, "_上次诊断丢帧", 0) or 0)
        增量 = max(0, 丢帧 - 上次丢帧)
        缓冲中 = bool(快照.get("缓冲中"))
        # 阈值：累计新丢帧 ≥10 帧，或正在缓冲（够明显才值得打扰 AI）
        if not 缓冲中 and 增量 < 10:
            return False
        self._上次诊断丢帧 = 丢帧
        return True

    @staticmethod
    def _时间文本(秒: float) -> str:
        秒 = max(0, int(秒 or 0))
        if 秒 >= 3600:
            return f"{秒 // 3600}:{秒 % 3600 // 60:02d}:{秒 % 60:02d}"
        return f"{秒 // 60:02d}:{秒 % 60:02d}"

    # ==================== 与主窗口的接口 ====================

    def 处理准备结果(self, 数据: dict) -> None:
        """后台准备线程回到界面线程后调用（由主窗口转发信号）。"""
        类型 = 数据.get("类型")
        if 类型 == "准备失败":
            self.播放按钮.setEnabled(True)
            原文 = str(数据.get("错误") or "")
            友好 = 原文
            if "是目录" in 原文 or "IsADirectoryError" in 原文:
                友好 = f"这是一条目录路径，不能直接播放：{原文}"
            elif "不存在" in 原文 or "FileNotFoundError" in 原文:
                友好 = f"网盘里找不到这个文件（可能被移动/改名）：{原文}"
            elif "超时" in 原文:
                友好 = f"取直链超时了，稍后重试或换一个文件：{原文}"
            self.状态标签.setText(f"❌ 准备失败：{友好[:80]}")
            self.信息栏.setText(f"❌ {友好}")
            self._AI写(f"[播放] 准备失败：{友好}")
            return
        if 类型 == "准备完成":
            self._真正起播()
            return
        if 类型 == "调优建议":
            建议 = 数据.get("建议") or {}
            if not 建议.get("需要调整"):
                # AI 没给建议（本地模型没起来/返回不可解析时很常见）→ 用规则兜底：
                # 明显在掉帧/缓冲就自动"流畅优先"，别让用户自己去猜
                规则 = None
                try:
                    规则 = self.会话.规则诊断() if self.会话 is not None else None
                except Exception:  # noqa: BLE001
                    规则 = None
                if not 规则:
                    return                  # 确实正常：不打扰用户
                建议 = 规则
                self._AI写("[自动调优] AI 没给建议，按规则兜底判断")
            if not 建议.get("需要调整"):
                return
            self._AI写(f"[自动调优] {建议.get('理由')} → 重载参数")
            for 动作 in 建议.get("动作") or []:
                self._AI写(f"   · {动作}")
            if self.会话 is not None and 建议.get("新参数"):
                self.会话.应用新参数(建议["新参数"], 自动重载=True)
            return
        if 类型 in ("窗口准备完成", "窗口准备失败"):
            窗口 = 数据.get("窗口")
            if 类型 == "窗口准备失败":
                if 窗口 is not None:
                    窗口.状态标签.setText(f"❌ 准备失败：{str(数据.get('错误'))[:70]}")
                self._AI写(f"[播放] 独立窗口准备失败：{数据.get('错误')}")
                return
            会话 = 数据.get("会话")
            if 窗口 is not None and 会话 is not None:
                窗口.换会话(会话, 标题=str(getattr(会话, "标题", "")))
                句柄 = 窗口._安全句柄() if hasattr(窗口, "_安全句柄") else 0
                if 会话.起播(句柄):
                    窗口.接管播放()
                    self._AI写(f"[播放] 独立窗口播放：{会话.标题}")
                else:
                    窗口.状态标签.setText("❌ 起播失败（VLC 拒绝了这条直链）")
            return
        if 类型 == "字幕提示":
            # 进度/失败原因都要让用户看得见（以前这类消息没有分支，直接被丢掉）
            self._AI写(f"[字幕] {数据.get('说明', '')}")
            self.状态标签.setText(f"ℹ️ {str(数据.get('说明', ''))[:60]}")
            if "没找到字幕" in str(数据.get("说明")) or "还没开始播放" in str(
                    数据.get("说明")):
                self.翻译按钮.setEnabled(True)
                self.生字幕按钮.setEnabled(True)
                self.总结按钮.setEnabled(True)
            return
        if 类型 in ("翻译完成", "生成字幕完成"):
            self.AI面板.设置忙碌(False)
            self.翻译按钮.setEnabled(True)
            self.生字幕按钮.setEnabled(True)
            路径 = str(数据.get("路径") or "")
            self._AI写(f"[字幕] ✅ 已写出：{路径}"
                     + (f"（{数据.get('条数')} 条）" if 数据.get("条数") else ""))
            if self.会话 is not None and 路径:
                self.会话.挂字幕(路径, 选中=True)
                self._AI写("[字幕] 已挂到播放器并选中")
            return
        if 类型 == "总结完成":
            self.总结按钮.setEnabled(True)
            结果 = 数据.get("结果") or {}
            self._AI写("")
            self._AI写(f"📝 摘要（{结果.get('来源', '?')}）：{结果.get('摘要', '')}")
            for 章节 in (结果.get("章节") or [])[:20]:
                self._AI写(f"   [{章节.get('时间')}] {章节.get('标题')}"
                         f"　{章节.get('要点', '')}")
            if 结果.get("标签"):
                self._AI写(f"🏷 标签：{'、'.join(结果['标签'][:8])}")
            章节们 = 结果.get("章节") or []
            if 章节们 and self.会话 is not None:
                self._AI写("（点下面的「跳到第一章」可直接跳转）")
                self._待跳章节 = 章节们
                self.总结按钮.setText("⏱ 跳到第一章")
                try:
                    self.总结按钮.clicked.disconnect()
                except Exception:
                    pass
                self.总结按钮.clicked.connect(self._跳到第一章)
            return
        if 类型 in ("翻译失败", "生成字幕失败", "总结失败"):
            self.AI面板.设置忙碌(False)
            self.翻译按钮.setEnabled(True)
            self.生字幕按钮.setEnabled(True)
            self.总结按钮.setEnabled(True)
            self._AI写(f"❌ {类型}：{数据.get('错误')}")

    def _跳到第一章(self) -> None:
        章节们 = getattr(self, "_待跳章节", [])
        if not 章节们 or self.会话 is None:
            return
        秒 = float(章节们[0].get("秒") or 0)
        self.会话.跳转(秒)
        self._AI写(f"[播放] 跳到 {章节们[0].get('时间')}")

    def 关闭(self) -> None:
        self.定时器.stop()
        if getattr(self, "_守护", None) is not None:
            self._守护.停止()
        self._关闭独立窗口()
        try:
            if self.会话 is not None:
                self.会话.关闭()
        except Exception:
            pass
        self.会话 = None
