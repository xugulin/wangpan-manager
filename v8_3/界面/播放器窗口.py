"""独立播放窗口：**VLC 风格**的播放器（菜单 + 工具栏 + 清单 + AI 面板）。

对齐原生 VLC 的地方
====================
* 顶部**菜单栏**：媒体 / 播放 / 音频 / 视频 / 字幕 / 视图 / 工具 / 帮助
  （结构与条目见 :mod:`v8_3.界面.vlc风格`）；
* 顶部**工具栏**：打开 · 播放控制 · 全屏 · 播放清单 · 循环 · 随机 · 音量 · 速度；
* 右侧**播放清单**（可切换，Ctrl+L），双击即播，上一个/下一个按清单走；
* 底部**进度条 + 时间 + 控制按钮**；底部**状态栏**：状态 / 丢帧 / 帧数 / 音量 / 速度；
* 画面上**右键菜单**：播放暂停、全屏、截图、速度、音频轨、字幕轨、宽高比、AI；
* 快捷键与 VLC 尽量一致：空格、`S` 停、`N/B` 上下一个、`F` 全屏、`M` 静音、
  `E` 逐帧、`←/→`（`Ctrl` 加大步长）、`↑/↓` 音量、`Ctrl+L` 清单、`Esc` 退出全屏。

与 VLC 的**区别**（需求要求）：多了 ``🤖 AI`` 菜单与 AI 面板 —— 翻译字幕、
生成字幕、内容总结、卡顿诊断、自动换参数重载。

全屏行为（需求要求）：全屏时菜单栏/工具栏/清单/控制条/状态栏**一起自动隐藏**，
整屏都是视频；鼠标移动、点击、滚轮、任意按键都会让它们立刻回来；`Esc` 回到窗口化。
"""

from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (QLabel, QSplitter, QTabWidget, QVBoxLayout,
                               QWidget)

from ..播放.播放核心 import 播放会话
from ..播放.显示环境 import (可嵌入窗口, 允许VLC自带窗口, 是桌面平台,
                          无窗口原因)
from .AI播放面板 import AI播放面板, AI字幕动作
from .播放控件 import 播放控制条, 视频窗, 时间文本
from .播放清单 import 播放项, 播放清单
from .vlc风格 import 构建菜单栏, 构建工具栏, 构建右键菜单

__all__ = ["播放器窗口"]

#: 全屏时无操作多久自动隐藏控件（毫秒）
自动隐藏毫秒 = 2500

#: 快进/快退步长
跳转步长秒 = 10.0

#: 速度档位（与 VLC 工具栏一致）
速度档 = (0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 3.0, 4.0)


class 播放器窗口(QWidget):
    """VLC 风格的独立播放窗口。"""

    状态更新 = Signal(dict)
    请求播放项 = Signal(object)         # 清单里选了某项（宿主负责取直链/起播）

    def __init__(self, 会话: Optional[播放会话] = None, 标题: str = "",
                 父窗口=None, 日志回调=None, AI动作: Optional[AI字幕动作] = None,
                 自动调优: bool = True, 接管: bool = False):
        super().__init__(None)          # 顶层窗口：不挂父窗口，才能真正独立
        self.会话 = 会话
        self._外部日志 = 日志回调 or (lambda _t: None)
        self.AI动作 = AI动作
        #: True = 本窗口**接管**别处（播放页）已经在播的那个会话：
        #: 只把 vout 绑到本窗口，**不**重新起播、也不新建播放器（单播放器架构）
        self._接管模式 = bool(接管)
        self._全屏 = False
        self._控件隐藏 = False
        self._拖拽中 = False
        self._全屏前几何 = None
        self._音频轨: list[tuple[int, str]] = []
        self._字幕轨: list[tuple[int, str]] = []
        self._上次决策 = ""
        self._诊断中 = False
        self._续播中 = False
        self._允许自带窗口 = 允许VLC自带窗口()   # 用户显式同意才让 VLC 自己开窗
        self._守护 = None                        # 游离窗口巡检（懒创建）
        self._等待映射中 = False                  # 正在等窗口映射（不是失败）
        self._右栏手动隐藏 = False               # 用户点按钮藏了右侧面板（别自动弹回）
        self.setWindowTitle(f"🎬 {标题 or 'V8_3 播放器（VLC 风格）'}")
        # 最小尺寸别太大：竖屏视频（9:16）的合理窗口比这窄得多，卡在 480 宽
        # 会让视频区比例对不上、出现左右黑边（实测踩过）
        self.setMinimumSize(360, 260)
        self.resize(1180, 720)
        self.setFocusPolicy(Qt.StrongFocus)

        self._构建(自动调优=自动调优)
        self._装事件过滤()
        # 构建完再摆窗口：屏幕比默认尺寸小时自动缩小并居中
        # （4K 片源在 1080p/小屏上也不会跑到屏幕外）
        self.适应屏幕(移动窗口=False)
        self._居中到屏幕()
        self.定时器 = QTimer(self)
        self.定时器.setInterval(500)
        self.定时器.timeout.connect(self._刷新状态)
        self._隐藏定时器 = QTimer(self)
        self._隐藏定时器.setSingleShot(True)
        self._隐藏定时器.timeout.connect(self._隐藏控件)

    # ==================== 构建 ====================

    def _构建(self, 自动调优: bool = True) -> None:
        布局 = QVBoxLayout(self)
        布局.setContentsMargins(0, 0, 0, 0)
        布局.setSpacing(0)

        # 「🗂 面板」按需求放在菜单栏「帮助」后面
        self.菜单栏 = 构建菜单栏(self._动作表(), self, 带侧栏按钮=True)
        布局.addWidget(self.菜单栏)
        # 需求：视频上方那行（工具栏）**整行撤销** —— 控件全部搬到下面的控制条：
        #   播放/停止/上一个/下一个/循环/随机/音量(含静音)/速度/字幕/截图/全屏
        # 需要"打开文件/网盘/清单/AI"时用菜单（媒体 / 视图）。
        self.工具栏 = None

        主体 = QSplitter(Qt.Horizontal)
        主体.setChildrenCollapsible(False)

        视频容器 = QWidget()
        视频布局 = QVBoxLayout(视频容器)
        视频布局.setContentsMargins(0, 0, 0, 0)
        self.视频 = 视频窗(self)
        self.视频.双击.connect(self.切换全屏)
        self.视频.setContextMenuPolicy(Qt.CustomContextMenu)
        self.视频.customContextMenuRequested.connect(self._弹出右键菜单)
        视频布局.addWidget(self.视频, 1)
        主体.addWidget(视频容器)

        self.右栏 = QTabWidget()
        self.清单 = 播放清单(self.右栏)
        self.清单.请求播放.connect(lambda 项: self.请求播放项.emit(项))
        self.右栏.addTab(self.清单, "📋 播放清单")
        # 紧凑模式：右侧面板只有 ~380px，两行布局 + 短标签才不会把字截断
        self.AI面板 = AI播放面板(self.右栏, 自动调优=自动调优, 紧凑=True)
        self.AI面板.翻译.connect(lambda: self._AI("翻译字幕"))
        self.AI面板.生成字幕.connect(lambda: self._AI("生成字幕"))
        self.AI面板.总结.connect(lambda: self._AI("总结"))
        self.AI面板.诊断.connect(lambda: self._AI("诊断"))
        self.右栏.addTab(self.AI面板, "🤖 AI 助手")
        # 两个页签内容宽度不同 → 切换后视频区宽度会变，跟着重排才不留黑边
        self.右栏.currentChanged.connect(lambda _i: self._延迟适应视频比例(60))
        主体.addWidget(self.右栏)
        主体.setStretchFactor(0, 5)
        主体.setStretchFactor(1, 1)
        self.主体 = 主体
        # 右侧面板（清单/AI）会抢宽度：给它一个上限，并保证视频区至少能显示
        # —— 实测过视频控件被挤成 **0 宽**，libvlc 无处可画 → 窗口一片黑
        self.右栏.setMaximumWidth(380)
        视频容器.setMinimumWidth(360)
        布局.addWidget(主体, 1)

        # 控制条两行：上面**加粗**进度条（紧贴视频），下面按钮行：
        #   ⏸ 播放暂停 | ⏮ 上一个 | ⏹ 停止 | ⏭ 下一个 | 🔁 不循环 | 🔀 随机 |
        #   🔊 音量 100% | 速度 [1.0×] | 💬字幕 📷截图 ⛶全屏
        self.控制条 = 播放控制条(self, 含音量=True, 含上下一个=True,
                          含循环随机=True, 进度条高度=14)
        self.控制条.请求暂停.connect(self._播放暂停)
        self.控制条.请求停止.connect(self._停止)
        self.控制条.请求上一个.connect(self.上一个)
        self.控制条.请求下一个.connect(self.下一个)
        self.控制条.请求循环.connect(self.切换循环)
        self.控制条.请求随机.connect(self.切换随机)
        self.控制条.请求静音.connect(self.设置静音)
        self.控制条.拖动开始.connect(lambda: setattr(self, "_拖拽中", True))
        self.控制条.请求跳转.connect(self._跳转比例)
        self.控制条.请求音量.connect(self.设置音量)
        self.控制条.请求倍速.connect(self.设置速度)
        self.控制条.请求字幕.connect(self.切换字幕轨)
        self.控制条.请求截图.connect(self.截图)
        self.控制条.请求全屏.connect(self.切换全屏)
        布局.addWidget(self.控制条)

        self.状态栏 = QWidget()
        状态布局 = QVBoxLayout(self.状态栏)
        状态布局.setContentsMargins(6, 2, 6, 2)
        self.状态标签 = QLabel("就绪")
        self.状态标签.setStyleSheet("font-size: 11px; color: #95a5a6;")
        状态布局.addWidget(self.状态标签)
        布局.addWidget(self.状态栏)

        self.右键菜单 = 构建右键菜单(self._动作表(), self)

    def resizeEvent(self, 事件):  # noqa: N802
        super().resizeEvent(事件)
        # 视频上方那行已撤销，这里不再需要工具栏宽度自适应

    def showEvent(self, 事件):  # noqa: N802
        # 每次显示都确认窗口装得进屏幕：HiDPI 缩放、换显示器、上次留下的几何
        # 都可能让它超出屏幕（用户实测过"窗口过大超出屏幕"）
        try:
            super().showEvent(事件)
            if not self._全屏:
                区域 = self.屏幕几何()
                if 区域 is not None and (self.width() > 区域.width()
                                    or self.height() > 区域.height()
                                    or not 区域.contains(self.frameGeometry())):
                    self.适应屏幕()
                self._延迟适应视频比例(30)   # 显示后按视频比例校准一次
        except Exception:  # noqa: BLE001
            pass

    def _装事件过滤(self):
        # 全屏时视频区会吃掉鼠标事件，所以窗口和视频区都要装
        for 目标 in (self, self.视频):
            目标.installEventFilter(self)

    # ==================== 动作表（菜单/工具栏都走这里）====================

    def _动作表(self) -> dict:
        return {
            # 媒体
            "打开网盘": lambda: self.回调("打开网盘"),
            "打开本地": lambda: self.回调("打开本地"),
            "加入清单": self.把当前加入清单,
            "添加本地到清单": lambda: self.回调("添加本地到清单"),
            "退出": self.close,
            # 播放
            "播放暂停": self._播放暂停,
            "停止": self._停止,
            "上一个": self.上一个,
            "下一个": self.下一个,
            "逐帧": self.逐帧,
            "快进": lambda: self.相对跳转(跳转步长秒),
            "快退": lambda: self.相对跳转(-跳转步长秒),
            "到开头": lambda: self.跳转绝对(0.0),
            "跳到结尾": self.跳到结尾,
            "速度列表": lambda: [(f"{x}×", x) for x in 速度档],
            "当前速度": self.当前速度,
            "设置速度": self.设置速度,
            "章节数": self.章节数,
            "当前章节": self.当前章节,
            "跳章节": self.跳章节,
            "下一章": self.下一章,
            "上一章": self.上一章,
            # 音频
            "音量加": lambda: self._调音量(5),
            "音量减": lambda: self._调音量(-5),
            "静音切换": self.设置静音,
            "是否静音切换": self.是否静音,
            "音量": self.当前音量,
            "设置音量": self.设置音量,
            "音频轨列表": lambda: list(self._音频轨),
            "当前音频轨": self.当前音频轨,
            "选择音频轨": self.选择音频轨,
            # 视频
            "全屏": self.设置全屏,
            "是否全屏": lambda: bool(self._全屏),
            "截图": self.截图,
            "宽高比": self.当前宽高比,
            "设置宽高比": self.设置宽高比,
            "缩放": self.当前缩放,
            "设置缩放": self.设置缩放,
            "置顶窗口": self.切换置顶,
            "适应屏幕": lambda: self.适应屏幕(),
            "适应视频比例": lambda: self.适应视频比例(),
            # 字幕
            "添加字幕文件": lambda: self.回调("添加字幕文件"),
            "字幕轨列表": lambda: list(self._字幕轨),
            "当前字幕轨": self.当前字幕轨,
            "选择字幕轨": self.选择字幕轨,
            # 视图
            "切换清单": self.切换清单,
            "切换AI面板": self.切换AI面板,
            "切换状态栏": lambda 显示: self.状态栏.setVisible(bool(显示)),
            "显示控件": self.切换控件,
            # 工具
            "播放参数": self.显示播放参数,
            "AI诊断": lambda: self._AI("诊断"),
            "流畅优先": self.流畅优先,
            "用VLC自带窗口": self.用VLC自带窗口播放,
            "切换自动调优": self.设置自动调优,
            "效果说明": lambda: self._写日志(
                "效果与滤镜：VLC 的 3D/视频滤镜未接入（本项目只做解码参数与 AI 优化）"),
            "偏好说明": lambda: self._写日志(
                "偏好设置：播放参数由 AI 顾问决定；缓存写在 数据/运行环境/ 下，不改系统设置"),
            # AI
            "AI翻译字幕": lambda: self._AI("翻译字幕"),
            "AI生成字幕": lambda: self._AI("生成字幕"),
            "AI总结": lambda: self._AI("总结"),
            "AI帮助": self.显示AI帮助,
            # 清单
            "切换侧栏": self.切换侧栏,
            "循环切换": self.切换循环,
            "随机切换": self.切换随机,
        }

    #: 宿主回调（播放页打开窗口时会注入：打开网盘/打开本地/加字幕/取直链播放）
    宿主回调: dict = {}

    def 回调(self, 名字: str, *参数):
        函数 = (self.宿主回调 or {}).get(名字)
        if callable(函数):
            return 函数(*参数)
        self.状态标签.setText(f"ℹ️ 这个入口需要在播放页里用（{名字}）")
        return None

    # ==================== 起播 / 会话 ====================

    def 换会话(self, 会话: 播放会话, 标题: str = "") -> None:
        self.会话 = 会话
        if 标题:
            self.setWindowTitle(f"🎬 {标题}")

    def 起播(self) -> bool:
        """在独立窗口里起播（句柄就是这个窗口的视频区）。"""
        if self.会话 is None:
            self.状态标签.setText("❌ 没有播放会话")
            return False
        self.show()
        self._置顶()
        self._起守护()
        if not self._已映射():
            # 刚 show() 时 X11 窗口可能还没映射好 —— 这时把句柄交给 libvlc，
            # 它会找不到可用的父窗口，于是**自己开一个 VLC 窗口**（实测的"分离"）。
            # 等一小会儿再绑，竞态就没了。
            # ⚠️ 这里必须返回 True：调用方（播放页/自检）把 False 当"起播失败"，
            # 会把刚建好的窗口直接关掉（踩过）。_等待映射中 记录真实状态。
            self._等待映射中 = True
            self.状态标签.setText("⏳ 等待窗口就绪…")
            QTimer.singleShot(90, self.起播)
            return True
        句柄 = self._安全句柄()
        if 句柄 == 0 and 是桌面平台() and not self._允许自带窗口:
            # ⚠️ 这里必须拦住：句柄 0 会让 libvlc **自己开一个 VLC 窗口**放视频，
            # 那个窗口不归我们管（用户实测：标题栏 VLC media player、比屏幕还大）。
            平台 = QGuiApplication.platformName()
            self.状态标签.setText("❌ 无法把画面嵌进本窗口（窗口号不是 X11 的）")
            # 用状态栏 + 日志说明，**不要弹模态框**：模态框会把自动化/自检挂死
            # （这个坑本项目踩过：QMessageBox 让离屏截图脚本永远卡住）
            原因 = 无窗口原因(平台)
            self._写日志("[显示] ⚠️ 没有可用的 X11 窗口号，已阻止开播：\n" + 原因)
            self.状态标签.setText(
                "❌ 无法嵌入播放：请用 XWayland 启动（QT_QPA_PLATFORM=xcb）；"
                "或「工具 → 用 VLC 自带窗口播放」强制继续")
            self.AI面板.追加("（提示：直接开播会让 VLC 自己弹一个不受控的窗口）")
            return False
        self._写日志(f"[显示] 平台 {QGuiApplication.platformName()} · 窗口句柄 {句柄}"
                 + ("（嵌入本窗口）" if 句柄 else "（无窗口解码：桌面环境下画面会由 VLC 自开窗口显示）"))
        成功 = self.会话.起播(句柄)
        if 成功:
            self.保证视频区可见()
            self.适应视频比例()          # 需求：按视频分辨率调窗口，避免黑边
            self.控制条.设置循环文本(self.清单.循环按钮文本())
            self.控制条.设置随机勾选(self.清单.随机)
            self.控制条.设置静音图标(self.是否静音())
            self.定时器.start()
            self._重置隐藏计时()
            self._刷新音视频轨()
            self._起守护()
            self.状态标签.setText("▶ 播放中（双击画面或 F 全屏，Esc 退出全屏）")
            self._同步视图勾选()
        else:
            self.状态标签.setText("❌ 起播失败（VLC 拒绝了这条直链）")
        return 成功

    def 屏幕几何(self):
        """当前窗口所在屏幕的**可用**区域（扣掉任务栏/顶栏）。"""
        屏幕 = (QGuiApplication.screenAt(self.frameGeometry().center())
              or QGuiApplication.primaryScreen())
        if 屏幕 is None:
            return None
        return 屏幕.availableGeometry()

    def 适应屏幕(self, *, 移动窗口: bool = True) -> bool:
        """把窗口缩到屏幕装得下（4K 片源在 1080p 屏上也能整屏看完）。

        为什么要这一步：窗口默认 1180x720，但小屏（1366x768）、HiDPI 缩放或
        上一次留下的几何都可能比屏幕大 —— 那样"看不成"（标题栏都跑到屏幕外）。
        同时把 VLC 的缩放复位成"自动适应窗口"，避免用户之前手动放大过。
        """
        区域 = self.屏幕几何()
        if 区域 is None:
            return False
        宽 = min(self.width() or 1180, int(区域.width() * 0.92))
        高 = min(self.height() or 720, int(区域.height() * 0.92))
        宽 = max(480, 宽)
        高 = max(300, 高)
        改了 = (宽 != self.width()) or (高 != self.height())
        if 改了:
            self.resize(宽, 高)
        if 移动窗口:
            self._居中到屏幕()
        try:
            if self.会话 and self.会话.播放器:
                self.会话.播放器.设置缩放(0.0)      # 0 = 自动适应窗口
        except Exception:  # noqa: BLE001
            pass
        标签 = getattr(self, "状态标签", None)     # __init__ 早期可能还没建
        if 标签 is not None:
            标签.setText(f"🖥 已适应屏幕：{宽}×{高}（视频按窗口缩放）")
        return 改了

    @staticmethod
    def 算视频区尺寸(比例: float, 可用宽: int, 可用高: int,
                原生宽: int = 1280) -> tuple[int, int]:
        """算"没有黑边"的视频区尺寸（**纯函数**，方便确定性测试）。

        规则：**永远沿比例缩放**，绝不同时夹宽和高 —— 早先分别对宽、高做
        ``max(...)`` 下限，把竖屏视频夹成 100x240（比例全乱，照样有黑边）。
        做法：先按"可用宽 vs 可用高"决定以哪一边为准，再用最小尺寸**等比**放大，
        最后整体等比缩回可用空间。
        """
        比例 = float(比例 or 0.0)
        if 比例 <= 0:
            return max(1, int(可用宽)), max(1, int(可用高))
        可用宽 = max(1, int(可用宽))
        可用高 = max(1, int(可用高))
        最小宽, 最小高 = 240, 180
        原生宽 = max(1, int(原生宽))
        if 可用宽 * 比例 <= 可用高:
            宽 = min(原生宽, 可用宽)
            高 = int(round(宽 / 比例))
        else:
            高 = 可用高
            宽 = int(round(高 * 比例))
            if 宽 > 原生宽:
                # 小片子**不放大**：按"高"定宽时也要受原生分辨率上限约束
                # （漏了这一句，720p 的片子会被算成 3840 宽 —— 单测逮到过）
                宽 = 原生宽
                高 = int(round(宽 / 比例))
        # 触到下限 → 沿比例放大（保持比例）
        if 高 < 最小高:
            高 = 最小高
            宽 = int(round(高 * 比例))
        if 宽 < 最小宽:
            宽 = 最小宽
            高 = int(round(宽 / 比例))
        # 超出可用空间 → 等比缩小（保持比例）
        if 宽 > 可用宽 or 高 > 可用高:
            缩放 = min(可用宽 / max(1, 宽), 可用高 / max(1, 高))
            if 缩放 < 1.0:
                宽 = max(1, int(宽 * 缩放))
                高 = max(1, int(高 * 缩放))
        return max(1, int(宽)), max(1, int(高))

    def 视频比例(self):
        """视频的宽高比（拿不到返回 None）。

        来源是 ffprobe 探测结果 ``会话.媒体``（宽/高），不依赖 libvlc 是否已出画面。
        """
        媒体 = getattr(self.会话, "媒体", None) if self.会话 is not None else None
        try:
            宽 = int(getattr(媒体, "宽", 0) or 0)
            高 = int(getattr(媒体, "高", 0) or 0)
        except Exception:  # noqa: BLE001
            宽 = 高 = 0
        if 宽 > 0 and 高 > 0:
            return float(宽) / float(高)
        return None

    def 适应视频比例(self) -> bool:
        """把窗口调成视频的宽高比（**消除黑边**），并保证装得进屏幕。

        * 目标视频区 = 视频原生分辨率；4K/8K 片源自动缩到屏幕可用区域内；
        * 窗口 = 目标视频区 + 装饰尺寸（菜单栏、进度条行、按钮行、状态栏、右侧面板）；
        * 顺手复位 VLC 的"缩放 / 宽高比"，避免之前手动调过导致变形或留边。
        """
        比例 = self.视频比例()
        区域 = self.屏幕几何()
        if 比例 is None or 区域 is None or self._全屏:
            return False
        # 装饰尺寸 = 窗口 - 视频区（菜单/进度/按钮/状态栏/右侧面板都算进去）
        装饰宽 = max(0, self.width() - self.视频.width())
        装饰高 = max(0, self.height() - self.视频.height())
        if self.width() <= 0 or self.视频.width() <= 0:
            装饰宽, 装饰高 = 0, 0
        可用宽 = max(240, int(区域.width() * 0.94) - 装饰宽)
        可用高 = max(180, int(区域.height() * 0.94) - 装饰高)
        媒体 = getattr(self.会话, "媒体", None) if self.会话 is not None else None
        原生宽 = int(getattr(媒体, "宽", 0) or 0) or 1280
        目标视频宽, 目标视频高 = self.算视频区尺寸(比例, 可用宽, 可用高, 原生宽)
        新宽 = 目标视频宽 + 装饰宽
        新高 = 目标视频高 + 装饰高
        if (新宽, 新高) != (self.width(), self.height()):
            self.resize(新宽, 新高)
        self._居中到屏幕()
        self.保证视频区可见()
        try:
            if self.会话 is not None and self.会话.播放器 is not None:
                self.会话.播放器.设置缩放(0.0)      # 0 = 自动适应窗口
                self.会话.播放器.设置宽高比("")       # 默认（原始）比例
        except Exception:  # noqa: BLE001
            pass
        # 复核：小屏幕上"窗口最小尺寸 + 右侧面板"可能挤得放不下目标比例，
        # 那就如实说明（提示收起面板），而不是让用户看着黑边猜
        try:
            泵后误差 = abs(self.视频.width() / max(1, self.视频.height()) - 比例)
        except Exception:  # noqa: BLE001
            泵后误差 = 0.0
        if 泵后误差 > 0.03:
            self.状态标签.setText(
                f"🖼 已按视频比例调整，但屏幕空间不足（当前比例偏差 "
                f"{泵后误差:.3f}）：收起右侧「🗂 面板」或缩小窗口可消除黑边")
        else:
            self.状态标签.setText(
                f"🖼 窗口已按视频比例调整：{目标视频宽}×{目标视频高}"
                f"（比例 {比例:.3f}，无黑边）")
        return True

    def _延迟适应视频比例(self, 毫秒: int = 0) -> None:
        """布局变化（面板显隐等）之后重排窗口 —— 必须等 Qt 把布局跑完再算。

        为什么要重排：藏起右侧面板后视频区会变宽，窗口尺寸不变的话，视频区比例
        就和视频不一致了 → **左右黑边**（用户实测）。藏面板时把窗口同步收窄
        （等于把面板的宽度还给窗口），视频区尺寸保持不变，比例自然还是对的。
        """
        if self._全屏:
            return
        QTimer.singleShot(毫秒, self.适应视频比例)
        QTimer.singleShot(毫秒 + 90, self.适应视频比例)

    def 视频区比例误差(self) -> float:
        """视频区"实际宽高比"与"视频比例"的偏差（自检/测试用；越小越没黑边）。"""
        比例 = self.视频比例()
        if not 比例 or self.视频.height() <= 0:
            return 999.0
        return abs(self.视频.width() / self.视频.height() - 比例)

    def _居中到屏幕(self) -> None:
        区域 = self.屏幕几何()
        if 区域 is None:
            return
        宽, 高 = self.width(), self.height()
        x = 区域.x() + max(0, (区域.width() - 宽) // 2)
        y = 区域.y() + max(0, (区域.height() - 高) // 2)
        self.move(x, y)

    def 接管播放(self) -> bool:
        """把**已经在播的**会话接到本窗口：只把画面挪过来，不重新起播。

        为什么还要"再绑一次"：libvlc 在起播瞬间如果没能把画面放进我们的窗口，
        会自己开一个 VLC 窗口放（用户实测过：标题栏 VLC media player、比屏幕还大）。
        稍后再调一次 ``set_xwindow`` 会把 vout 收回到本窗口 —— 用户自己也验证了
        "把那个独立窗口关掉，画面就回到 GUI 里"，这里就是把它自动化。
        """
        if self.会话 is None or self.会话.播放器 is None:
            self.状态标签.setText("❌ 没有可接管的播放")
            return False
        self.show()
        self._置顶()
        self._起守护()          # 起播前就盯着（VLC 是在 set_xwindow 那刻自开窗口的）
        if not self._已映射():
            self._等待映射中 = True
            self.状态标签.setText("⏳ 等待窗口就绪…")
            QTimer.singleShot(90, self.接管播放)
            return True          # 同上：别让调用方误判成失败而关掉窗口
        句柄 = self._安全句柄()
        if 句柄 == 0 and 是桌面平台() and not self._允许自带窗口:
            self.状态标签.setText("❌ 无法把画面嵌进本窗口（窗口号不是 X11 的）")
            self._写日志("[显示] ⚠️ 没有可用的 X11 窗口号，独立窗口无法接管播放")
            return False
        # ⚠️ 关键：**播放中**改 set_xwindow 在 VLC 3 里不会真的把 vout 搬过去
        # （实测：句柄记下了、画面还是留在原来的窗口/自开窗口里）。所以这里是
        # "绑窗口 → 重开媒体 → 跳回原位置" —— 注意仍然是**同一个播放器/会话**，
        # 不会出现第二路声音（那才是用户遇到的问题）。
        位置 = 0.0
        try:
            位置 = float(self.会话.播放器.进度秒())
        except Exception:  # noqa: BLE001
            位置 = 0.0
        self._等待映射中 = False
        self.保证视频区可见()
        成功 = self.会话.起播(句柄)
        if not 成功:
            self.状态标签.setText("❌ 接管播放失败（直链可能已过期，请重新播放）")
            self._写日志("[播放] 独立窗口接管失败：直链可能过期")
            return False
        if 位置 > 1.0:
            def _跳(秒=位置):
                try:
                    if self.会话 and self.会话.播放器:
                        self.会话.跳转(秒)
                except Exception:  # noqa: BLE001
                    pass
            QTimer.singleShot(900, _跳)
            QTimer.singleShot(1800, _跳)
        self.适应视频比例()              # 需求：按视频分辨率调窗口，避免黑边
        self.定时器.start()
        self._重置隐藏计时()
        self._刷新音视频轨()
        self._同步视图勾选()
        self._起守护()
        self.状态标签.setText(
            f"🗗 独立窗口播放中（从 {位置:.0f}s 接着放；F 全屏 / Esc 退出全屏）")
        return True

    def _已映射(self) -> bool:
        """窗口是否已经真的映射到屏幕（X11 里这决定 libvlc 能不能嵌入）。

        ⚠️ 只有 X11/xcb 才需要等：离屏/无头平台没有"把窗口号交给 libvlc"这回事，
        一律当作已就绪，否则会把起播卡死（离屏窗口永远不算 exposed）。
        另外最多重试有限次，超了就照常起播（宁可试一把，也不能卡住不播）。
        """
        try:
            if not 可嵌入窗口(QGuiApplication.platformName()):
                return True
            if not self.isVisible():
                return False
            句柄 = self.windowHandle()
            就绪 = bool(句柄.isExposed()) if (句柄 is not None
                                        and hasattr(句柄, "isExposed")) else True
            if not 就绪:
                self._映射重试 = getattr(self, "_映射重试", 0) + 1
                if self._映射重试 > 15:          # ~1.4 秒还没好就别等了
                    self._写日志("[显示] 窗口映射等待超时，按现状起播（会巡检自愈）")
                    return True
            else:
                self._映射重试 = 0
            return 就绪
        except Exception:  # noqa: BLE001
            return True

    def _起守护(self) -> None:
        """起"游离窗口"巡检：libvlc 要是又自己开窗口，就自动把画面收回。"""
        if getattr(self, "_守护", None) is None:
            from .游离窗口守护 import 游离窗口守护
            self._守护 = 游离窗口守护(
                self,
                取自己窗口号们=lambda: [int(self.winId()),
                                 int(self.视频.winId())],
                自愈回调=self._自愈画面,
                日志=self._写日志,
                间隔毫秒=3000, 巡检次数=0)      # 0 = 无限
        self._守护.开始(无限=True)

    def _自愈画面(self) -> None:
        """把画面收回本窗口：绑句柄 + 重开媒体 + 跳回原位置（同一个播放器）。"""
        if self.会话 is None or self.会话.播放器 is None:
            return
        位置 = 0.0
        try:
            位置 = float(self.会话.播放器.进度秒())
        except Exception:  # noqa: BLE001
            位置 = 0.0
        self.保证视频区可见()
        if self.会话.起播(self._安全句柄()):
            def _跳(秒=位置):
                try:
                    if self.会话 is not None and self.会话.播放器 is not None:
                        self.会话.跳转(秒)
                except Exception:  # noqa: BLE001
                    pass
            QTimer.singleShot(900, _跳)
            self._写日志(f"[显示] 画面已收回独立窗口（从 {位置:.0f}s 继续）")
        self.定时器.start()

    def 保证视频区可见(self) -> bool:
        """确保视频控件有真实尺寸（宽>0）。

        为什么专门做这件事：libvlc 是**按窗口尺寸**画画面的，视频控件被右侧面板
        挤成 0 宽时，画面就"消失了"（用户实测：窗口一片黑、声音还在）。这里在
        显示之后强制给视频区留出宽度。
        """
        try:
            宽 = max(0, self.width())
            目标 = max(360, 宽 - min(360, self.右栏.width() if self.右栏.isVisible() else 0))
            if self.视频.width() <= 0 or self.视频.height() <= 0:
                if self.右栏.isVisible():
                    self.主体.setSizes([目标, min(340, max(200, 宽 - 目标))])
                else:
                    self.主体.setSizes([宽, 0])
            return self.视频.width() > 0 and self.视频.height() > 0
        except Exception:  # noqa: BLE001
            return False

    def _再绑窗口(self) -> None:
        try:
            if self.会话 is not None and self.会话.播放器 is not None:
                self.保证视频区可见()
                self.会话.播放器.绑定窗口(self._安全句柄())
        except Exception:  # noqa: BLE001
            pass

    def _置顶(self) -> None:
        """把窗口提到最前（离屏平台不支持 raise()，会刷无用告警）。"""
        try:
            if QGuiApplication.platformName() in ("offscreen", "minimal"):
                return
        except Exception:  # noqa: BLE001
            pass
        self.raise_()
        self.activateWindow()

    def _安全句柄(self) -> int:
        """给 libvlc 的窗口句柄；没有 X11 窗口号时返回 0（不嵌窗口，避免段错误）。"""
        try:
            if not 可嵌入窗口(QGuiApplication.platformName()):
                # Wayland/离屏平台的 winId() 不是 X11 窗口号：交出去会直接闪退
                return 0
        except Exception:  # noqa: BLE001
            pass
        try:
            return int(self.视频.winId())
        except Exception:  # noqa: BLE001
            return 0

    # ==================== 播放控制 ====================

    def _播放暂停(self):
        if self.会话:
            self.会话.暂停()
        self.显示控件()

    def _停止(self):
        if self.会话:
            self.会话.关闭()
        self.定时器.stop()
        self.控制条.设置进度(0, 0)
        self.状态标签.setText("⏹ 已停止")

    def _跳转比例(self, 比例: float):
        self._拖拽中 = False
        if self.会话 and self.会话.播放器:
            时长 = self.会话.播放器.时长秒()
            if 时长 > 0:
                self.跳转绝对(max(0.0, min(1.0, float(比例))) * 时长)

    def 相对跳转(self, 秒: float):
        if self.会话 is None or self.会话.播放器 is None:
            return
        self.跳转绝对(max(0.0, self.会话.播放器.进度秒() + float(秒)))

    def 跳转绝对(self, 秒: float):
        if self.会话 is None:
            return
        self.会话.跳转(max(0.0, float(秒)))
        self.状态标签.setText(f"⏩ {时间文本(秒)}")

    def 跳到结尾(self):
        if self.会话 is None or self.会话.播放器 is None:
            return
        时长 = self.会话.播放器.时长秒()
        if 时长 > 0:
            self.跳转绝对(max(0.0, 时长 - 1.0))
        elif self.清单.当前项() is not None:
            self.下一个()

    def 逐帧(self):
        if self.会话 and self.会话.播放器:
            self.会话.播放器.下一帧()
            self.状态标签.setText("⏯ 逐帧")

    # ---- 速度 / 音量 / 静音 ----

    def 当前速度(self) -> float:
        try:
            return (float(self.会话.播放器.取速率())
                    if self.会话 and self.会话.播放器 else 1.0)
        except Exception:  # noqa: BLE001
            return 1.0

    def 设置速度(self, 倍速: float):
        值 = float(倍速)
        if self.会话:
            self.会话.设置速率(值)
        self.控制条.设置倍速(值)
        框 = getattr(self.控制条, "倍速框", None)
        if 框 is not None:
            for i in range(框.count()):
                if abs(float(框.itemData(i) or 0) - 值) < 0.01:
                    框.blockSignals(True)
                    框.setCurrentIndex(i)
                    框.blockSignals(False)
                    break
        self.状态标签.setText(f"⏩ 速度 {值}×")

    def 当前音量(self) -> int:
        try:
            return (int(self.会话.播放器.取音量())
                    if self.会话 and self.会话.播放器 else 100)
        except Exception:  # noqa: BLE001
            return 100

    def 设置音量(self, 值: int):
        if self.会话:
            self.会话.设置音量(int(值))
        self.控制条.设置音量(int(值))

    def _调音量(self, 增减: int):
        self.设置音量(max(0, min(150, self.当前音量() + int(增减))))

    def 是否静音(self) -> bool:
        try:
            return (bool(self.会话.播放器.是否静音())
                    if self.会话 and self.会话.播放器 else False)
        except Exception:  # noqa: BLE001
            return False

    def 设置静音(self, 静音: bool):
        if self.会话 and self.会话.播放器:
            self.会话.播放器.设置静音(bool(静音))
        self.控制条.设置静音图标(bool(静音))
        self.状态标签.setText("🔇 静音" if 静音 else "🔊 取消静音")

    # ---- 轨道 / 章节 / 比例 ----

    def 切换字幕轨(self):
        if self.会话 is None or self.会话.播放器 is None:
            return
        编号 = self.会话.切换字幕()
        self._刷新音视频轨()
        名称 = next((n for i, n in self._字幕轨 if i == 编号), "关闭")
        self.状态标签.setText(f"💬 字幕：{名称}")

    def 选择字幕轨(self, 编号: int):
        if self.会话 and self.会话.播放器:
            self.会话.播放器.选择字幕(int(编号))
            self.状态标签.setText(f"💬 字幕轨 → {编号}")

    def 当前字幕轨(self) -> int:
        try:
            return (int(self.会话.播放器.当前字幕())
                    if self.会话 and self.会话.播放器 else -1)
        except Exception:  # noqa: BLE001
            return -1

    def 选择音频轨(self, 编号: int):
        if self.会话 and self.会话.播放器:
            self.会话.播放器.选择音频(int(编号))
            self.状态标签.setText(f"🔊 音频轨 → {编号}")

    def 当前音频轨(self) -> int:
        return -1

    def 章节数(self) -> int:
        try:
            return (int(self.会话.播放器.章节数())
                    if self.会话 and self.会话.播放器 else 0)
        except Exception:  # noqa: BLE001
            return 0

    def 当前章节(self) -> int:
        try:
            return (int(self.会话.播放器.当前章节())
                    if self.会话 and self.会话.播放器 else -1)
        except Exception:  # noqa: BLE001
            return -1

    def 跳章节(self, 编号: int):
        if self.会话 and self.会话.播放器:
            self.会话.播放器.跳章节(int(编号))
            self.状态标签.setText(f"📑 第 {int(编号) + 1} 章")

    def 下一章(self):
        if self.会话 and self.会话.播放器:
            self.会话.播放器.下一章()

    def 上一章(self):
        if self.会话 and self.会话.播放器:
            self.会话.播放器.上一章()

    def 当前宽高比(self) -> str:
        try:
            return (str(self.会话.播放器.宽高比())
                    if self.会话 and self.会话.播放器 else "")
        except Exception:  # noqa: BLE001
            return ""

    def 设置宽高比(self, 比例: str):
        if self.会话 and self.会话.播放器:
            self.会话.播放器.设置宽高比(比例)
            self.状态标签.setText(f"▭ 宽高比 {'默认' if not 比例 else 比例}")

    def 当前缩放(self) -> float:
        try:
            return (float(self.会话.播放器.缩放())
                    if self.会话 and self.会话.播放器 else 0.0)
        except Exception:  # noqa: BLE001
            return 0.0

    def 设置缩放(self, 倍率: float):
        if self.会话 and self.会话.播放器:
            self.会话.播放器.设置缩放(float(倍率))
            self.状态标签.setText(
                f"🔍 缩放 {'自动适应窗口' if not 倍率 else 倍率}")

    def 用VLC自带窗口播放(self):
        """用户明确同意后：允许句柄 0，让 VLC 自己开窗口放（界面控件仍可用）。"""
        self._允许自带窗口 = True
        self._写日志("[显示] 已改用 VLC 自带窗口播放（该窗口不受本程序控制，"
                 "大小/位置请用系统窗口管理操作）")
        if self.会话 is not None and self.会话.播放器 is None:
            self.起播()

    def 切换置顶(self, 选中: bool = False):
        置顶 = bool(选中)
        try:
            self.setWindowFlag(Qt.WindowStaysOnTopHint, 置顶)
            self.show()
        except Exception:  # noqa: BLE001
            pass
        self.状态标签.setText("📌 窗口置顶" if 置顶 else "窗口不再置顶")

    def 截图(self, *_):
        目录 = Path("数据/截图")
        目录.mkdir(parents=True, exist_ok=True)
        标题 = str(getattr(self.会话, "标题", "") or "截图")
        路径 = 目录 / f"{Path(标题).stem}_{datetime.now():%Y%m%d_%H%M%S}.png"
        if self.会话 and self.会话.截图(str(路径)):
            self.状态标签.setText(f"📷 已保存 {路径}")
            self._写日志(f"[播放] 截图：{路径}")
        else:
            self.状态标签.setText("📷 截图失败（可能还没出画面）")

    # ---- 清单 ----

    def 下一个(self):
        项 = self.清单.下一个项()
        if 项 is None:
            self.状态标签.setText("⏭ 清单里没有下一项")
            return
        self.请求播放项.emit(项)

    def 上一个(self):
        项 = self.清单.上一个项()
        if 项 is None:
            self.状态标签.setText("⏮ 清单里没有上一项")
            return
        self.请求播放项.emit(项)

    def 把当前加入清单(self):
        if self.会话 is None or not str(getattr(self.会话, "远端路径", "") or ""):
            self.状态标签.setText("⚠️ 还没有正在播的视频")
            return
        项 = 播放项(标题=str(self.会话.标题 or "未命名"),
                 网盘标识=str(self.会话.网盘标识 or ""),
                 远端路径=str(self.会话.远端路径 or ""),
                 来源=str(self.会话.网盘标识 or "本地"))
        if self.清单.添加(项):
            self.状态标签.setText(f"📋 已加入清单（共 {len(self.清单)} 项）")
        else:
            self.状态标签.setText("📋 清单里已经有这个视频了")

    def 切换循环(self, *_):
        模式 = self.清单.切换循环()
        self.控制条.设置循环文本(self.清单.循环按钮文本())
        self.状态标签.setText(f"🔁 {模式}")

    def 切换随机(self, 选中: bool = False):
        self.清单.随机 = bool(选中) if 选中 else not self.清单.随机
        self.控制条.设置随机勾选(self.清单.随机)
        self.状态标签.setText("🔀 随机播放" if self.清单.随机 else "顺序播放")

    def 清单播完了(self):
        """一首放完：按清单继续（不循环且已是最后一项就停）。"""
        项 = self.清单.下一个项(自动=True)
        if 项 is None:
            self.状态标签.setText("✅ 清单已播完")
            return
        self.请求播放项.emit(项)

    # ---- 视图 ----

    def 切换侧栏(self, 显示: bool = True):
        """显示/隐藏整个右侧面板（播放清单 + AI 助手）——菜单栏「🗂 面板」。

        需求：**藏起面板后不能出现左右黑边** —— 所以显隐之后立刻按视频比例重排窗口
        （藏面板 = 窗口跟着收窄，视频区尺寸不变）。
        """
        目标 = bool(显示)
        if 目标:
            self._右栏手动隐藏 = False
            self.右栏.show()
        else:
            self._右栏手动隐藏 = True
            self.右栏.hide()
        self.保证视频区可见()
        self._同步视图勾选()
        self._延迟适应视频比例()

    def 切换清单(self, 显示: bool = True):
        """显示/隐藏右侧面板（播放清单页）。"""
        目标 = bool(显示)
        if 目标:
            self._右栏手动隐藏 = False
            self.右栏.show()
            self.右栏.setCurrentIndex(0)
        else:
            # ⚠️ 必须记下"是用户主动藏的"：否则下一次鼠标移动触发 显示控件()
            # 又会把它 show 回来 —— 用户的感觉就是"按钮点了没反应"
            self._右栏手动隐藏 = True
            self.右栏.hide()
        self.保证视频区可见()
        self._同步视图勾选()
        self._延迟适应视频比例()          # 藏/显示面板后不留黑边

    def 切换AI面板(self, 显示: bool = True):
        """显示/隐藏右侧面板（AI 助手页）。"""
        目标 = bool(显示)
        if 目标:
            self._右栏手动隐藏 = False
            self.右栏.show()
            self.右栏.setCurrentIndex(1)
        elif self.右栏.currentIndex() == 1:
            self._右栏手动隐藏 = True
            self.右栏.hide()
        else:
            # 当前在清单页：这个动作只负责"切到 AI 页"，不该把面板关掉
            self._右栏手动隐藏 = False
            self.右栏.show()
            self.右栏.setCurrentIndex(1)
        self.保证视频区可见()
        self._同步视图勾选()
        self._延迟适应视频比例()

    def 切换控件(self):
        if self._控件隐藏:
            self.显示控件()
        else:
            self.隐藏控件()

    def _同步视图勾选(self):
        可见 = bool(self.右栏.isVisible())
        # 侧栏开关挂在菜单栏上（工具栏那行已撤销）
        侧栏动作 = getattr(self.菜单栏, "侧栏动作", None)
        if 侧栏动作 is not None:
            侧栏动作.setChecked(可见)


    def 流畅优先(self):
        """一键流畅优先：允许丢帧 + vout=gl + 缓存加大，并从原位置重载。"""
        if self.会话 is None:
            self.状态标签.setText("⚠️ 先播放一个视频再用「流畅优先」")
            return
        新设置 = self.会话.流畅优先()
        self.状态标签.setText(
            f"⚡ 流畅优先：缓存 {新设置.网络缓存毫秒}ms · 允许丢帧 · vout=gl（重载中）")
        self._写日志("[播放] ⚡ 流畅优先：允许丢迟到帧 + vout=gl + 缓存加大，正在重载")
        self.会话.应用新参数(新设置.to_dict(), 自动重载=True)

    def 显示播放参数(self):
        会话 = self.会话
        if 会话 is None:
            return
        设置 = 会话.设置
        self._写日志(
            f"[播放] 起播参数（{设置.来源}）：缓存 {设置.网络缓存毫秒}ms，"
            f"硬解 {设置.硬解}；媒体 "
            f"{会话.媒体.摘要() if 会话.媒体 else '未知'}")
        self.状态标签.setText(
            f"⚙️ 缓存 {设置.网络缓存毫秒}ms · 硬解 {设置.硬解}")

    def 设置自动调优(self, 选中: bool):
        self.AI面板.自动调优框.setChecked(bool(选中))
        if self.会话 is not None:
            self.会话.自动调优 = bool(选中)

    def 显示AI帮助(self):
        self._写日志(
            "🤖 AI 功能：翻译字幕（本地模型优先）· 生成字幕（Whisper 语音识别）· "
            "内容总结（摘要/章节/标签）· 卡顿诊断（丢帧/缓冲 → 换参数重载）。"
            "没有云端密钥也能用：本地 Ollama 免费离线；都没有则回落规则。")

    # ---- AI ----

    def _AI(self, 动作: str):
        if self.AI动作 is None:
            self.状态标签.setText("⚠️ AI 功能没接上（没有 AI 客户端）")
            return
        会话 = self.会话
        if 会话 is None or not str(getattr(会话, "远端路径", "") or ""):
            self.状态标签.setText("⚠️ 先播放一个视频再用 AI 功能")
            return
        映射 = {"翻译字幕": self.AI动作.翻译字幕,
              "生成字幕": self.AI动作.生成字幕,
              "总结": self.AI动作.总结,
              "诊断": self.AI动作.诊断}
        函数 = 映射.get(动作)
        if 函数 is None:
            return
        self.AI面板.设置忙碌(True)
        self._写日志(f"[AI] 开始：{动作}")
        函数()

    def AI结果(self, 数据: dict):
        """AI 动作的状态回调：由宿主接到这个窗口上（按钮/日志/跳章）。"""
        类型 = str((数据 or {}).get("类型") or "")
        if 类型 == "字幕提示":
            self._写日志(f"[字幕] {数据.get('说明', '')}")
            self.状态标签.setText(f"ℹ️ {str(数据.get('说明', ''))[:60]}")
            return
        if 类型 in ("翻译完成", "生成字幕完成"):
            self.AI面板.设置忙碌(False)
            路径 = str(数据.get("路径") or "")
            self._写日志(f"[字幕] ✅ 已写出：{路径}")
            if self.会话 is not None and 路径:
                self.会话.挂字幕(路径, 选中=True)
                self._刷新音视频轨()
                self._写日志("[字幕] 已挂到播放器并选中")
            return
        if 类型 == "总结完成":
            self.AI面板.设置忙碌(False)
            结果 = 数据.get("结果") or {}
            self._写日志(f"📝 摘要（{结果.get('来源', '?')}）：{结果.get('摘要', '')}")
            for 章节 in (结果.get("章节") or [])[:20]:
                self._写日志(f"   [{章节.get('时间')}] {章节.get('标题')}"
                          f"　{章节.get('要点', '')}")
            if 结果.get("标签"):
                self._写日志(f"🏷 标签：{'、'.join(结果['标签'][:8])}")
            return
        if 类型 == "调优建议":
            建议 = 数据.get("建议") or {}
            if not 建议.get("需要调整"):
                规则 = None
                try:
                    规则 = self.会话.规则诊断() if self.会话 is not None else None
                except Exception:  # noqa: BLE001
                    规则 = None
                if not 规则:
                    return
                建议 = 规则
                self._写日志("[自动调优] AI 没给建议，按规则兜底判断")
            if not 建议.get("需要调整"):
                return
            self._写日志(f"[自动调优] {建议.get('理由')} → 重载参数")
            for 动作 in 建议.get("动作") or []:
                self._写日志(f"   · {动作}")
            if self.会话 is not None and 建议.get("新参数"):
                self.会话.应用新参数(建议["新参数"], 自动重载=True)
            return
        if 类型 in ("翻译失败", "生成字幕失败", "总结失败"):
            self.AI面板.设置忙碌(False)
            self._写日志(f"❌ {类型}：{数据.get('错误')}")

    def _写日志(self, 文本: str):
        self.AI面板.追加(文本)          # 窗口自己的面板
        try:
            self._外部日志(文本)         # 顺带写宿主日志（页面/日志页）
        except Exception:  # noqa: BLE001
            pass

    # ==================== 全屏 / 控件显示 ====================

    def 切换全屏(self):
        self.设置全屏(not self._全屏)

    def 设置全屏(self, 全屏: bool):
        全屏 = bool(全屏)
        if 全屏 == self._全屏:
            return
        self._全屏 = 全屏
        if 全屏:
            self._全屏前几何 = self.geometry()
            self.showFullScreen()
            # 让窗口管理器先处理；只有它没铺满时才自己铺（无 WM 的 Xvfb 就是这种）
            QTimer.singleShot(0, self._全屏兜底铺满)
            QTimer.singleShot(250, self._全屏兜底铺满)
            self._重置隐藏计时()
        else:
            self.showNormal()
            self.显示控件()
            self._延迟适应视频比例(120)     # 退出全屏后恢复"无黑边"的窗口比例
        self.控制条.设置全屏图标(全屏)
        self._置顶()
        self._同步视图勾选()
        # 全屏/窗口化会重建原生窗口，画面有可能又"分离"→ 稍后确认并自愈
        QTimer.singleShot(600, self._确认画面在本窗口)
        try:
            if self.会话 and self.会话.播放器:
                self.会话.播放器.绑定窗口(self._安全句柄())
        except Exception:  # noqa: BLE001
            pass

    def _确认画面在本窗口(self) -> None:
        """切完全屏/窗口化后确认画面还在本窗口（不在就重绑一次）。"""
        try:
            if self.会话 is None or self.会话.播放器 is None:
                return
            self.保证视频区可见()
            self.会话.播放器.绑定窗口(self._安全句柄())
            self.定时器.start()
        except Exception:  # noqa: BLE001
            pass

    def _全屏兜底铺满(self) -> None:
        """先看 WM 有没有铺满；没铺满才自己铺（保证"整屏都是视频"）。"""
        if not self._全屏:
            return
        屏幕 = (QGuiApplication.screenAt(self.frameGeometry().center())
              or QGuiApplication.primaryScreen())
        if 屏幕 is None:
            return
        目标 = 屏幕.geometry()
        if self.width() >= 目标.width() and self.height() >= 目标.height():
            return
        self.setGeometry(目标)
        self.视频.setGeometry(self.rect())
        self.显示控件()

    def 显示控件(self):
        for 部件 in (self.菜单栏, self.控制条, self.状态栏):
            部件.show()
        if not getattr(self, "_右栏手动隐藏", False):
            self.右栏.show()
        self._控件隐藏 = False
        self._重置隐藏计时()

    def 隐藏控件(self):
        for 部件 in (self.菜单栏, self.控制条, self.状态栏, self.右栏):
            部件.hide()
        self._控件隐藏 = True

    def _隐藏控件(self):
        if not self._全屏:                # 只在全屏时自动隐藏
            return
        self.隐藏控件()

    def _重置隐藏计时(self):
        if self._全屏:
            self._隐藏计时 = self._隐藏定时器.start(自动隐藏毫秒)
        else:
            self._隐藏定时器.stop()

    # ==================== 事件 ====================

    def eventFilter(self, 对象, 事件):
        类型 = 事件.type()
        if 类型 in (事件.Type.MouseMove, 事件.Type.MouseButtonPress,
                  事件.Type.Wheel, 事件.Type.KeyPress,
                  事件.Type.MouseButtonDblClick):
            # 全屏时任何"动静"都让控件回来（视频区会吃掉这些事件，
            # 所以过滤器装在窗口和视频区两处）
            self.显示控件()
        return super().eventFilter(对象, 事件)

    def _弹出右键菜单(self, 位置):
        self.显示控件()
        try:
            self.右键菜单.exec(self.视频.mapToGlobal(位置))
        except Exception:  # noqa: BLE001
            pass

    def keyPressEvent(self, 事件):
        键 = 事件.key()
        Ctrl = bool(事件.modifiers() & Qt.ControlModifier)
        if 键 == Qt.Key_Escape:
            if self._全屏:
                self.设置全屏(False)       # Esc：全屏 → 窗口化（不关窗口）
            else:
                self.close()
            return
        if 键 == Qt.Key_Space:
            self._播放暂停()
            return
        if 键 == Qt.Key_F or 键 == Qt.Key_F11:
            self.切换全屏()
            return
        if 键 == Qt.Key_M:
            self.设置静音(not self.是否静音())
            return
        if 键 == Qt.Key_E:
            self.逐帧()
            return
        if 键 == Qt.Key_N:
            self.上一个()
            return
        if 键 == Qt.Key_B:
            self.下一个()
            return
        if 键 == Qt.Key_S and not Ctrl:
            self._停止()
            return
        if 键 == Qt.Key_Left:
            self.相对跳转(-跳转步长秒 if Ctrl else -5.0)
            return
        if 键 == Qt.Key_Right:
            self.相对跳转(跳转步长秒 if Ctrl else 5.0)
            return
        if 键 == Qt.Key_Up:
            self._调音量(5)
            return
        if 键 == Qt.Key_Down:
            self._调音量(-5)
            return
        if 键 == Qt.Key_L and Ctrl:
            self.切换清单(not self.右栏.isVisible())
            return
        super().keyPressEvent(事件)

    # ==================== 状态刷新 ====================

    def _刷新音视频轨(self):
        if self.会话 is None or self.会话.播放器 is None:
            self._字幕轨, self._音频轨 = [], []
            return
        try:
            self._字幕轨 = [(t.编号, t.名称) for t in self.会话.播放器.字幕轨()]
        except Exception:  # noqa: BLE001
            self._字幕轨 = []
        try:
            self._音频轨 = [(t.编号, t.名称) for t in self.会话.播放器.音频轨()]
        except Exception:  # noqa: BLE001
            self._音频轨 = []

    def _刷新状态(self):
        if self.会话 is None:
            return
        快照 = self.会话.状态快照()
        if not self._拖拽中 and 快照["时长秒"] > 0:
            self.控制条.设置进度(快照["进度秒"], 快照["时长秒"])
        self.控制条.设置暂停图标(
            str(快照.get("状态", "")) in ("播放中", "正在播放"))
        决策 = getattr(self.会话, "AI决策状态", "")
        if 决策 and 决策 != self._上次决策:
            self._上次决策 = 决策
            self._写日志(f"[AI 决策] {决策}")
        提示 = "" if self._全屏 else "　（F 全屏 / Ctrl+L 清单 / M 静音）"
        self.状态标签.setText(
            f"{快照['状态']} · 丢帧 {快照['丢帧']} · {快照['已解码视频']} 帧 · "
            f"音量 {快照.get('音量', 0)} · {float(快照.get('倍速', 1.0)):.2f}× · "
            f"已播 {快照['已播秒']:.0f}s{提示}")
        # 一首放完 → 按清单续播
        状态 = str(快照.get("状态", ""))
        if 状态 in ("已结束", "错误") and self.清单.当前项():
            if not self._续播中:
                self._续播中 = True
                QTimer.singleShot(300, self._续播)
        elif 状态 in ("播放中", "正在播放"):
            self._续播中 = False
        # 自动调优（后台线程，别冻界面）
        if self.AI面板.自动调优框.isChecked() and not self._诊断中:
            self._诊断中 = True

            def 诊断():
                try:
                    建议 = self.会话.采样并诊断()
                except Exception as e:  # noqa: BLE001
                    建议 = {"需要调整": False, "理由": f"诊断失败：{e}"}
                finally:
                    self._诊断中 = False
                if 建议:
                    self.状态更新.emit({"类型": "调优建议", "建议": 建议})

            threading.Thread(target=诊断, name="播放诊断", daemon=True).start()

    def _续播(self):
        try:
            self.清单播完了()
        finally:
            QTimer.singleShot(1500, lambda: setattr(self, "_续播中", False))

    def 交接信息(self) -> dict:
        """关窗前把"接着播需要的东西"打包给页面（直链有时效，别让页面重取）。

        需求：**关闭独立窗口后视频回到播放页继续播**。
        """
        会话 = self.会话
        if 会话 is None:
            return {}
        位置 = 0.0
        try:
            位置 = float(会话.播放器.进度秒()) if 会话.播放器 else 0.0
        except Exception:  # noqa: BLE001
            位置 = 0.0
        return {"类型": "独立窗口关闭", "位置秒": 位置,
                "网盘标识": str(getattr(会话, "网盘标识", "") or ""),
                "远端路径": str(getattr(会话, "远端路径", "") or ""),
                "标题": str(getattr(会话, "标题", "") or ""),
                "直链信息": dict(getattr(会话, "直链信息", {}) or {}),
                "媒体": getattr(会话, "媒体", None),
                "探测": getattr(会话, "探测", None),
                "设置": getattr(会话, "设置", None)}

    def closeEvent(self, 事件):
        self.定时器.stop()
        if getattr(self, "_守护", None) is not None:
            self._守护.停止()
        交接 = self.交接信息()
        if self._接管模式:
            # 单播放器架构：**不要关会话**，只把画面还给宿主（播放页），
            # 这样声音和进度都不中断（用户要求"关掉独立窗口后回到播放页继续播"）
            归还 = (self.宿主回调 or {}).get("归还播放")
            if callable(归还):
                try:
                    归还(self)
                except Exception:  # noqa: BLE001
                    pass
            交接 = dict(交接 or {})
            交接.update({"类型": "独立窗口关闭", "接管": True})
            self.状态更新.emit(交接)
            super().closeEvent(事件)
            return
        交接 = self.交接信息()
        try:
            if self.会话:
                self.会话.关闭()
        except Exception:  # noqa: BLE001
            pass
        self.状态更新.emit(交接 or {"类型": "独立窗口关闭"})
        super().closeEvent(事件)
