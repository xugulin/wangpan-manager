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

from PySide6.QtCore import Qt, QTimer, Signal, QRect
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (QLabel, QSplitter, QTabWidget, QVBoxLayout,
                               QWidget)

from ..播放.播放核心 import 播放会话
from ..播放.显示环境 import (可嵌入窗口, 允许VLC自带窗口, 是桌面平台,
                          无窗口原因)
from .AI播放面板 import AI播放面板, AI字幕动作
from .播放控件 import 播放控制条, 视频窗, 时间文本
from .定时 import 安全单发
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
        # ⚠️ **必须是真正的顶层窗口**（parent=None）。
        #    一度为了让合成器"听话"改成挂在主窗口下，结果它变成了主窗口里的
        #    原生**子控件**：没有标题栏/最小化/最大化/关闭，而且被父窗口边界裁掉
        #    （实测：底部的进度条那一排、播放暂停那一排、状态栏全被裁没了）。
        #    独立播放器就该是独立窗口 —— 摆位问题由 贴合屏幕()/落点收紧 解决，
        #    不靠"当别人的子窗口"。
        super().__init__(None)
        self._父窗口参考 = 父窗口        # 只留个引用（父窗口关了要跟着收），不改父子关系
        self.会话 = 会话
        if 会话 is not None:
            try:                        # 会话跟着本窗口的出口走（统一出口）
                会话.出口 = self._出口()
            except Exception:  # noqa: BLE001
                pass
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
        #: 观察到"合成器坚持把窗口放在哪"（取最右/最下的落点）。尺寸按这个落点收，
        #: 保证不管它把我们放哪，窗口都完整可见（上限取屏幕的 35%，免得越收越小）。
        self._落点记忆: tuple[int, int] | None = None
        self._落点记忆上限 = 0.35
        self._右栏手动隐藏 = False               # 用户点按钮藏了右侧面板（别自动弹回）
        self.setWindowTitle(f"🎬 {标题 or 'V8_3 播放器（VLC 风格）'}")
        # 最小尺寸：**必须放得下菜单栏 + 视频区 + 进度条那排 + 播放暂停那排 + 状态栏**
        # —— 用户反馈过"进度条那排、播放暂停那排没了"，窗口被压太矮时它们就是被裁掉的。
        # 高度按各行实测值算：菜单 29 + 视频最小 240 + 控制条 62 + 状态栏 20 ≈ 351，
        # 留点余量取 380；宽度 360 与视频容器的最小宽度一致（也够竖屏视频算比例）。
        self.setMinimumSize(360, 380)
        self.resize(1180, 720)
        self.setFocusPolicy(Qt.StrongFocus)

        self._构建(自动调优=自动调优)
        self._装事件过滤()
        # 构建完再摆窗口：屏幕比默认尺寸小时自动缩小并居中
        # （4K 片源在 1080p/小屏上也不会跑到屏幕外）
        self.适应屏幕(移动窗口=False)
        self._居中到屏幕()
        self._装主线程泵()
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
        # 挂到 self 上：按视频比例算窗口尺寸时要读**布局的真实下限**
        # （控件自己的 minimumWidth() 常是 0，真正卡住视频区的是这个容器）
        self.视频容器 = 视频容器
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
        try:
            出口 = getattr(self, "_播放出口", None)
            if 出口 is not None:
                出口.同步()
        except Exception:  # noqa: BLE001
            pass
        # 视频上方那行已撤销，这里不再需要工具栏宽度自适应

    def showEvent(self, 事件):  # noqa: N802
        # 每次显示都确认窗口装得进屏幕：HiDPI 缩放、换显示器、上次留下的几何
        # 都可能让它超出屏幕（用户实测过"窗口过大超出屏幕"）
        try:
            super().showEvent(事件)
            if not self._全屏:
                区域 = self.屏幕几何()
                if 区域 is not None and not 区域.contains(self.frameGeometry()):
                    # 显示后装饰尺寸才算得准：用真实几何再夹一次（否则标题栏/边框
                    # 会把窗口顶出屏幕 —— 用户实测"超出屏幕侧边缘"）
                    self.适应屏幕()
                self._延迟适应视频比例(30)   # 显示后按视频比例校准一次
                self._延迟贴合屏幕()          # 显示后再贴几次（WM 摆位可能顶掉我们的位置）
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

    def _排空播放操作(self) -> None:
        """界面线程里执行后台线程排队的播放操作（碰 libvlc 的活只能在这里做）。"""
        会话 = getattr(self, "会话", None)
        if 会话 is None:
            return
        try:
            会话.排空主线程队列()
        except Exception:  # noqa: BLE001
            pass

    def _截图提示(self):
        """截图提示浮层（懒建）：透明、贴视频区右下角、自动淡出。"""
        提示 = getattr(self, "_截图提示层", None)
        if 提示 is None:
            from .截图提示 import 截图提示 as _截图提示
            提示 = _截图提示(self, 视频控件=getattr(self, "视频", None))
            self._截图提示层 = 提示
        return 提示

    def 同步全屏图标(self) -> None:
        """按钮文字跟着全屏状态走（Esc / 助手退出时也要复位）。"""
        try:
            self.控制条.设置全屏图标(self._全屏助手().是全屏())
        except Exception:  # noqa: BLE001
            pass

    def _装主线程泵(self) -> None:
        """给本窗口的会话装"后台线程 → 界面线程"的队列泵（libvlc 非线程安全）。"""
        try:
            会话 = getattr(self, "会话", None)
            if 会话 is None or not hasattr(会话, "装主线程泵"):
                return
            会话.装主线程泵()
            if getattr(self, "_泵定时器", None) is None:
                self._泵定时器 = QTimer(self)
                self._泵定时器.setInterval(25)
                self._泵定时器.timeout.connect(self._排空播放操作)
                self._泵定时器.start()
        except Exception:  # noqa: BLE001
            pass

    def 写日志(self, 文本: str) -> None:
        """写一条界面日志（有外部回调就走回调，否则只更新状态栏）。"""
        try:
            self._外部日志(文本)
        except Exception:  # noqa: BLE001
            pass
        try:
            self.状态标签.setText(文本.split("] ", 1)[-1][:80])
        except Exception:  # noqa: BLE001
            pass

    def 跟着父窗口退出(self, 父) -> None:
        """主窗口关掉时把独立窗口也关掉（它本身是顶层窗口，不会自动跟着走）。"""
        def _关自己(*_):
            try:
                self.close()
            except Exception:  # noqa: BLE001 - 控件可能已经被销毁
                pass
        try:
            if 父 is not None:
                父.destroyed.connect(_关自己)
        except Exception:  # noqa: BLE001
            pass

    def 换会话(self, 会话: 播放会话, 标题: str = "") -> None:
        self.会话 = 会话
        self._装主线程泵()
        # 会话是共享的（接管模式）：把它切到**本窗口的出口**，
        # 这样"画面往哪画"始终只有一处实现（见 播放/播放出口.py）
        try:
            会话.出口 = self._出口()
        except Exception:  # noqa: BLE001
            pass
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
            等次数 = int(getattr(self, "_等映射次数", 0)) + 1
            self._等映射次数 = 等次数
            self.状态标签.setText(f"⏳ 等待窗口就绪…（第 {等次数} 次）")
            if 等次数 > 28:      # ≈ 2.5 秒还没上屏
                self.状态标签.setText("❌ 窗口迟迟没上屏，先不起播"
                                 "（避免多出 VLC 窗口）")
                self._写日志("[显示] 独立窗口 2.5 秒仍未映射，已中止起播；"
                          "把窗口拉到前台后重新点 ▶")
                return True
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

    #: **硬上限**：窗口最多占屏幕可用区的比例（超过就一定往回收，留出任务栏/标题栏的余地）。
    #: 0.96 = 左右各留 2%：既保证"完整可见"，又不会把竖屏视频逼到"比例算不出来"
    #: （0.92 时 800x800 的屏上竖屏视频差 2% 就放不下 —— 老测试逮到过）。
    屏幕占比 = 0.96

    #: **舒适默认**：按视频比例开窗时占屏幕可用区的比例。
    #: 用户反馈"启动时太大了" —— 4K 片源按"铺满屏幕"算出来会占 88%，
    #: 看着像全屏；这里给一个舒服的默认（用户想更大可以自己拉/全屏）。
    初始占比 = 0.78

    def 贴合屏幕(self, 目标宽: int = 0, 目标高: int = 0,
              居中: bool = True, 留边: float = 0.0,
              按落点收紧: bool = False,
              落点: tuple[int, int] | None = None) -> tuple[int, int]:
        """把窗口**按当前屏幕分辨率**压到合适大小并摆进屏幕内（唯一的尺寸决策处）。

        用户实测反馈："独立窗口启动时太大了，超过了屏幕侧边缘"。原因有两个：

        1. 位置是按 ``self.width()`` 算的，而 ``resize()`` 是**异步生效**的 ——
           刚算完"居中的 x"用的是**旧宽度**，等 Qt 真的把窗口放大后，右边就伸到屏幕外了；
        2. 只夹了尺寸、没夹位置：多屏/左侧面板/任务栏让 ``availableGeometry()`` 的
           原点不是 (0,0) 时，居中算出来的 x 也可能为负。

        所以这里一次做全：**先夹尺寸（≤ 屏幕可用区 × 屏幕占比，且不小于最小尺寸），
        再用"目标尺寸"算位置并夹进屏幕**。任何屏幕分辨率下窗口都完整可见 ——
        没有任何写死的像素尺寸。

        :param 目标宽/目标高: 想要的尺寸（0 = 用当前尺寸）
        :param 居中: 是否摆到屏幕中央（False = 只夹位置，不动用户摆好的位置）
        """
        区域 = self.屏幕几何()
        if 区域 is None:
            return (int(self.width()), int(self.height()))
        比例 = float(留边 or self.屏幕占比)
        最小宽 = max(200, int(self.minimumWidth() or 360))
        最小高 = max(160, int(self.minimumHeight() or 260))
        宽 = int(目标宽 or self.width() or 1180)
        高 = int(目标高 or self.height() or 720)

        # ① 先按"屏幕占比"夹一次
        上限宽 = max(最小宽, int(区域.width() * 比例))
        上限高 = max(最小高, int(区域.height() * 比例))

        # ①' **落点记忆**：真机实测 COSMIC 会忽略客户端 move()，而且是在我们 move
        #     之后**再**搬一次 —— 于是"按当前几何算，明明在屏幕里"，它一搬就出屏
        #     （用户反馈："退出全屏播放 4K 时窗口过长超出屏幕"，就是这样来的）。
        #     对策：把它坚持的落点记下来，**任何一次尺寸计算都按这个落点收**，
        #     这样不管它把我们放哪、什么时候搬，窗口都完整可见。
        记忆 = getattr(self, "_落点记忆", None)
        if 记忆:
            记x = min(max(区域.x(), int(记忆[0])),
                    区域.x() + int(区域.width() * self._落点记忆上限))
            记y = min(max(区域.y(), int(记忆[1])),
                    区域.y() + int(区域.height() * self._落点记忆上限))
            剩余宽 = max(1, 区域.x() + 区域.width() - 记x)
            剩余高 = max(1, 区域.y() + 区域.height() - 记y)
            上限宽 = max(最小宽, min(上限宽, 剩余宽))
            上限高 = max(最小高, min(上限高, 剩余高))

        # ② `按落点收紧`：给 COSMIC 这类**忽略客户端 move()** 的合成器兜底
        #    （实测 xdotool windowmove 都搬不动它，我们算好的"居中"全白算，
        #    窗口右边就伸到屏幕外）。这时位置它说了算，我们就**把尺寸收到
        #    "从它放的位置到屏幕边"以内**，保证完整可见。
        #    默认不开：会听话的合成器（以及离屏/无 WM 环境）只要摆正位置就够，
        #    照它收紧反而会把竖屏视频挤成最小宽度、比例全乱（老测试逮到过）。
        if 按落点收紧:
            if 落点 is None:
                实际 = self.frameGeometry()
                落点 = (实际.x(), 实际.y())
            落点x = max(区域.x(), int(落点[0]))
            落点y = max(区域.y(), int(落点[1]))
            剩余宽 = max(1, 区域.x() + 区域.width() - 落点x)
            剩余高 = max(1, 区域.y() + 区域.height() - 落点y)
            # ⚠️ **等比**缩（不能分别夹宽和高）：分别夹会把视频区比例搞坏 ——
            # 面板一藏一显之后自带自检量到 0.07/0.14 的误差就是这么做出来的。
            缩放 = min(1.0, 剩余宽 / max(1, 宽), 剩余高 / max(1, 高))
            if 缩放 < 1.0:
                宽 = max(最小宽, int(宽 * 缩放))
                高 = max(最小高, int(高 * 缩放))

        宽 = max(最小宽, min(宽, 上限宽, 区域.width()))
        高 = max(最小高, min(高, 上限高, 区域.height()))
        if (宽, 高) != (self.width(), self.height()):
            self.resize(宽, 高)
        if 居中:
            # 用**目标尺寸**算位置（不能用 self.width()：resize 还没生效），再夹进屏幕。
            # 尊重客户端的合成器会听这一句；不听的那类（COSMIC）靠上面的尺寸收紧兜底。
            x = 区域.x() + max(0, (区域.width() - 宽) // 2)
            y = 区域.y() + max(0, (区域.height() - 高) // 2)
            x = min(max(区域.x(), x), 区域.x() + max(0, 区域.width() - 宽))
            y = min(max(区域.y(), y), 区域.y() + max(0, 区域.height() - 高))
            self.move(x, y)
        return (宽, 高)

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
        原宽, 原高 = int(self.width()), int(self.height())
        目标宽 = min(原宽 or 1180, int(区域.width() * self.屏幕占比))
        目标高 = min(原高 or 720, int(区域.height() * self.屏幕占比))
        宽, 高 = self.贴合屏幕(目标宽, 目标高, 居中=bool(移动窗口))
        改了 = (宽, 高) != (原宽, 原高)
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
        # 两级预算：先按"舒适默认"（初始占比）；要是连视频区自己的最小尺寸都放不下
        # （小屏 + 竖屏就是这种），就放宽到硬上限 —— 否则比例一定对不上、出黑边。
        舒适宽 = max(240, int(区域.width() * self.初始占比) - 装饰宽)
        舒适高 = max(180, int(区域.height() * self.初始占比) - 装饰高)
        硬宽 = max(240, int(区域.width() * self.屏幕占比) - 装饰宽)
        硬高 = max(180, int(区域.height() * self.屏幕占比) - 装饰高)
        # 视频区的**真实**下限：控件自己的 minimumWidth() 常是 0，真正卡住它的是
        # 布局树（视频容器 setMinimumWidth(360)）。用 minimumSizeHint 拿到布局算出来的值。
        最小视宽 = 240
        最小视高 = 180
        for 候选 in (getattr(self, "视频容器", None), getattr(self, "视频", None)):
            if 候选 is None:
                continue
            try:
                最小视宽 = max(最小视宽, int(候选.minimumWidth() or 0),
                            int(候选.minimumSizeHint().width() or 0))
                最小视高 = max(最小视高, int(候选.minimumHeight() or 0),
                            int(候选.minimumSizeHint().height() or 0))
            except Exception:  # noqa: BLE001
                continue
        最小视宽 = min(最小视宽, int(区域.width() * self.屏幕占比))
        最小视高 = min(最小视高, int(区域.height() * self.屏幕占比))
        媒体 = getattr(self.会话, "媒体", None) if self.会话 is not None else None
        原生宽 = int(getattr(媒体, "宽", 0) or 0) or 1280
        可用宽, 可用高 = 舒适宽, 舒适高
        目标视频宽, 目标视频高 = self.算视频区尺寸(比例, 可用宽, 可用高, 原生宽)
        if 目标视频宽 + 2 < 最小视宽 or 目标视频高 + 2 < 最小视高:
            可用宽, 可用高 = 硬宽, 硬高
            目标视频宽, 目标视频高 = self.算视频区尺寸(比例, 可用宽, 可用高, 原生宽)
        新宽 = 目标视频宽 + 装饰宽
        新高 = 目标视频高 + 装饰高
        # 尺寸与位置一次做完（按屏幕分辨率自适应 + 保证完整可见）
        宽, 高 = self.贴合屏幕(新宽, 新高, 居中=True)
        # ⚠️ 夹完要**复核比例**：窗口的"最小宽度"（360）或屏幕上限可能把宽度顶大/压小，
        #    那样视频区比例就跟视频对不上了（竖屏视频实测：视频区 360x509，
        #    比例误差 0.14 —— 老测试逮到过）。宽度定下来之后，按比例把高度补上。
        try:
            视宽 = max(1, int(宽) - int(装饰宽))
            目标高2 = int(round(视宽 / float(比例))) + int(装饰高)
            if abs(目标高2 - int(高)) > 2 and 目标高2 <= int(区域.height() * self.屏幕占比):
                self.贴合屏幕(宽, 目标高2, 居中=True)
        except Exception:  # noqa: BLE001
            pass
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
        # ⚠️ 收尾必须**再贴合一次**：上面的尺寸是按"我们以为的位置"算的，而
        # COSMIC 这类合成器会把窗口异步搬到它自己的位置（实测连 xdotool 都搬不动），
        # 于是右边就伸到屏幕外。每次重排都收一次尾，尺寸才会收敛到
        # "在它给的位置上也装得下"。
        try:
            if self.isVisible():
                self._再贴合一次()
        except Exception:  # noqa: BLE001
            pass
        return True

    def _延迟适应视频比例(self, 毫秒: int = 0) -> None:
        """布局变化（面板显隐等）之后重排窗口 —— 必须等 Qt 把布局跑完再算。

        为什么要重排：藏起右侧面板后视频区会变宽，窗口尺寸不变的话，视频区比例
        就和视频不一致了 → **左右黑边**（用户实测）。藏面板时把窗口同步收窄
        （等于把面板的宽度还给窗口），视频区尺寸保持不变，比例自然还是对的。
        """
        if self._全屏:
            return
        安全单发(self, 毫秒, self.适应视频比例)
        安全单发(self, 毫秒 + 90, self.适应视频比例)

    def 视频区比例误差(self) -> float:
        """视频区"实际宽高比"与"视频比例"的偏差（自检/测试用；越小越没黑边）。"""
        比例 = self.视频比例()
        if not 比例 or self.视频.height() <= 0:
            return 999.0
        return abs(self.视频.width() / self.视频.height() - 比例)

    def _居中到屏幕(self) -> None:
        """居中并保证完整可见（统一走 贴合屏幕，别再自己算一遍）。"""
        self.贴合屏幕(居中=True)

    def _延迟贴合屏幕(self, *毫秒们: int) -> None:
        """窗口显示后再贴几次。

        ⚠️ 为什么非要有这个：**窗口管理器会在窗口 map 那一刻自己摆位**（KWin 的
        智能放置会把新窗口摆在当前窗口旁边），把我们 show() 之前算好的位置顶掉 ——
        实测：独立窗口 1996 宽被摆到 x=848，右边直接超出屏幕 284px。
        所以显示后 0/150/400 毫秒各再贴一次（幂等：只在真的超出去时才动）。
        """
        from PySide6.QtCore import QTimer
        # 时机要分几档：合成器（COSMIC 实测）会在窗口 map 之后**才**把它摆到自己的位置，
        # 太早检查会看到"还没被摆过"的假几何、以为没问题就早退了。0.9/1.6/2.6 秒这几档
        # 是留给它的（幂等：已经在屏幕里就什么都不做）。
        for 毫秒 in (毫秒们 or (0, 150, 400, 900, 1600, 2600, 4000, 6000)):
            try:
                安全单发(self, int(毫秒), self._再贴合一次)
            except Exception:  # noqa: BLE001
                pass

    def _用户或WM弄大了(self) -> bool:
        """窗口是不是被用户/合成器弄大了（最大化/平铺/接近满屏）。

        为什么要问：用户点了最大化，我们过一会儿又"按视频比例"缩回去 —— 那是
        最招人烦的一类行为（而且 COSMIC 这种合成器**不会**把状态同步给 Qt，
        `isMaximized()` 一直是 False，只能按"尺寸接近屏幕"来判断）。
        """
        try:
            if self.isMaximized() or self.isFullScreen() or self._全屏:
                return True
        except Exception:  # noqa: BLE001
            return False
        try:
            区域 = self.屏幕几何()
            if 区域 is None:
                return False
            # ① 大到接近满屏
            if (self.width() >= 区域.width() * 0.95
                    and self.height() >= 区域.height() * 0.90):
                return True
            # ② 或者**贴着屏幕边**（平铺/最大化后就是这种：左边贴左、右边贴右），
            #    这时也别按比例缩回去 —— 那是跟用户/合成器抢窗口。
            框 = self.frameGeometry()
            if (abs(框.x() - 区域.x()) <= 8
                    and abs(框.x() + 框.width() - (区域.x() + 区域.width())) <= 8):
                return True
            return (abs(框.y() - 区域.y()) <= 8
                    and abs(框.y() + 框.height() - (区域.y() + 区域.height())) <= 8
                    and 框.height() >= 区域.height() * 0.6)
        except Exception:  # noqa: BLE001
            return False

    def _再贴合一次(self) -> None:
        """显示后复核：窗口要是没完整落在屏幕里 —— 先摆位，再按落点把尺寸收进去。

        为什么分两步（COSMIC 实测：它自己决定新窗口位置、**忽略客户端 move()**，
        而且是在我们 move 之后**再**搬一次）：
        ① 先按它给的位置把尺寸收进去（居中=False，不动位置）；
        ② 再礼貌地请求居中 —— 听话的合成器会挪过去；不听的那类保持原位，
           而尺寸已经保证"在它给的落点上也装得下"。
        """
        try:
            if self._全屏 or not self.isVisible() or self._用户或WM弄大了():
                return
            区域 = self.屏幕几何()
            if 区域 is None:
                return
            框 = self.frameGeometry()
            if 区域.contains(框):
                return                      # 已经完整可见：什么都不做
            # 它把我们放在了屏幕外 → 记下这个落点，之后所有尺寸都按它收
            落点 = (框.x(), 框.y())
            旧记忆 = getattr(self, "_落点记忆", None)
            if 旧记忆 is None:
                self._落点记忆 = 落点
            else:
                self._落点记忆 = (max(int(旧记忆[0]), 框.x()),
                               max(int(旧记忆[1]), 框.y()))
            self.写日志(f"[显示] 窗口 {框.width()}x{框.height()}@{框.x()},{框.y()} "
                     f"超出屏幕 {区域.width()}x{区域.height()}：按落点收紧尺寸")
            宽, 高 = self.贴合屏幕(居中=False, 按落点收紧=True, 落点=落点)
            self.贴合屏幕(居中=True)
            self.写日志(f"[显示] 已收紧到 {宽}x{高}"
                     + ("（窗口管理器不理会摆位，保持它给的位置）"
                        if not 区域.contains(self.frameGeometry()) else "（并已回到屏幕中央）"))
        except Exception as 错误:  # noqa: BLE001
            try:
                self.写日志(f"[显示] 贴合屏幕出错（忽略）：{错误}")
            except Exception:  # noqa: BLE001
                pass

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
        self._延迟贴合屏幕()      # 显示后再贴（WM 摆位会顶掉我们算好的位置）
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
        # （实测：句柄记下了、画面还是留在原来的窗口/自开窗口里）。所以交接仪式是
        # "停干净 → 换出口 → 换绑 → 重开 → 跳回原位置"，统一实现在
        # `播放出口.交接()`（页面 ⇄ 独立窗口都走它，不再各写一套）。
        self._等待映射中 = False
        self.保证视频区可见()
        位置 = 0.0
        try:
            位置 = float(self.会话.播放器.进度秒())
        except Exception:  # noqa: BLE001
            位置 = 0.0
        成功 = self._出口().交接(self.会话, 位置, 理由="画面已交给独立窗口")
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
            安全单发(self, 900, _跳)
            安全单发(self, 1800, _跳)
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
        """独立窗口的视频区是不是**真的在屏幕上**（交给 libvlc 之前必须为真）。

        ⚠️ 踩过三次坑：原来只看 Qt 的 isExposed()，而它说"可见"时 X 服务器可能
        还没映射窗口 —— libvlc 于是**自己开一个 "VLC media player" 顶层窗口**放画面
        （用户实测三次，独立窗口这条路尤其容易中）。判定统一走 界面/窗口就绪.py
        （以 X 的 map_state == IsViewable 为准）。
        """
        try:
            from .窗口就绪 import 窗口就绪 as _就绪
            return bool(_就绪(self.视频, 重试上限=0))
        except Exception:
            return self._已映射_旧()

    def _已映射_旧(self) -> bool:
        """（兜底）老的 Qt 判断——只有公共判断抛异常时才会走到。"""
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
                发现回调=self._发现游离窗口,
                日志=self._写日志,
                间隔毫秒=3000, 巡检次数=0)      # 0 = 无限
        self._守护.开始(无限=True)

    def _发现游离窗口(self, 找到=None) -> bool:
        """巡检发现画面跑到 libvlc 自己的窗口里 → 交给会话做**安全回退**。

        ⚠️ 不再走 _自愈画面 那套"停→绑→重播"，更不销毁 libvlc 的窗口：
        那两条路要么修不好（重播还是同一个输出模块），要么把 VLC 弄僵导致卡死。
        """
        if self.会话 is None:
            return False
        标题 = "、".join(str(名) for _号, 名 in list(找到 or [])[:2])
        try:
            return bool(self.会话.安全回退画面(标题=标题))
        except Exception as e:  # noqa: BLE001
            self._写日志(f"[显示] 安全回退失败：{e}")
            return False

    def _自愈画面(self) -> None:
        """把画面收回本窗口：绑句柄 + 重开媒体 + 跳回原位置（同一个播放器）。

        ⚠️ 保留只为兼容旧调用；自动巡检**不再用它**（换成 :meth:`_发现游离窗口`
        的安全回退）——"停→绑→重播"在画面跑到 VLC 自己窗口时修不好，
        反复重播还会让用户看到画面反复重来。
        """
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
        # 与播放页**同一个出口**（v8_3/播放/播放出口.py）——独立窗口也是"嵌进本窗口"，
        # 规矩必须一致：自建画布 + set_xwindow。以前这里各写一套，所以同一类 bug
        # （VLC 自己开窗口）在换条路径后又复发。
        try:
            return int(self._出口().句柄())
        except Exception:  # noqa: BLE001
            return 0

    def _出口(self):
        """本窗口的播放出口（懒建）。"""
        出口 = getattr(self, "_播放出口", None)
        if 出口 is None:
            from ..播放.播放出口 import 播放出口 as _播放出口
            出口 = _播放出口(控件=self.视频, 日志回调=self._写日志
                          if hasattr(self, "_写日志") else None)
            self._播放出口 = 出口
            if getattr(self, "会话", None) is not None:
                self.会话.出口 = 出口
        return 出口

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
            self._截图提示().显示已保存(路径)      # ★ 与播放页同一个透明浮层
        else:
            self.状态标签.setText("📷 截图失败（可能还没出画面）")
            self._截图提示().显示失败("可能还没出画面")

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
            # 统一走 全屏助手（合成器忽略 showFullScreen 时它自己铺满）
            self._全屏助手().进入()
            self._重置隐藏计时()
        else:
            self._全屏助手().退出()
            self.显示控件()
            # 退出全屏后：**先夹一次屏幕**（全屏时窗口是满屏的，装饰/面板尺寸都不准），
            # 再按比例重排 —— 否则算出来的窗口会偏大、下边伸出屏幕（用户实测过）。
            self.适应屏幕(移动窗口=False)
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

    def _全屏助手(self):
        """本窗口的全屏助手（与播放页同一个实现）。"""
        助手 = getattr(self, "_全屏助手实例", None)
        if 助手 is None:
            from .全屏助手 import 全屏助手 as _全屏助手
            助手 = _全屏助手(self, 退出回调=self._退出全屏后的收尾)
            self._全屏助手实例 = 助手
        return 助手

    def _退出全屏后的收尾(self) -> None:
        """不管是按钮、菜单还是 Esc 退出全屏，都走这里复位（真机实测：
        Esc 退出后按钮还写着"退出全屏" —— 因为那条路没经过 设置全屏()）。"""
        self._全屏 = False
        try:
            self.显示控件()
        except Exception:  # noqa: BLE001
            pass
        self.同步全屏图标()
        try:
            self._延迟适应视频比例(120)     # 退出全屏后恢复"无黑边"的窗口比例
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
            self._清掉出口()
            super().closeEvent(事件)
            return
        交接 = self.交接信息()
        try:
            if self.会话:
                self.会话.关闭()
        except Exception:  # noqa: BLE001
            pass
        self.状态更新.emit(交接 or {"类型": "独立窗口关闭"})
        self._清掉出口()
        super().closeEvent(事件)

    def _清掉出口(self) -> None:
        """清掉本窗口的出口引用（**不拆窗口** —— 那是 Qt 的窗口，VLC 可能还在画）。

        以前这里是"拆掉自建画布"（XDestroyWindow）：把 VLC 正在渲染的 drawable
        从底下抽走，VLC 就另开一个顶层窗口放画面 —— 用户实测的
        "关掉独立窗口又冒出 VLC media player" 就是这么来的。
        """
        try:
            出口 = getattr(self, "_播放出口", None)
            if 出口 is not None:
                出口.销毁()
                self._播放出口 = None
        except Exception:  # noqa: BLE001
            pass
