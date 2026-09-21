"""V8_3 主窗口：**顶部横向导航** + 切换式页面。

布局
====
```
┌──────┬──────────────────────────────────────────────┐
│ 网盘 │                                              │
│ 按钮 │            QStackedWidget                    │
│ 滚动 │  网盘页 / 跨网盘传输页 / 日志页              │
│ 区   │                                              │
├──────┤                                              │
│ 传输 │                                              │
│ 日志 │                                              │
├──────┤                                              │
│ 新增 │  ← 左下角纵向「网盘管理」面板                │
│ 编辑 │                                              │
│ 删除 │                                              │
└──────┴──────────────────────────────────────────────┘
```

* 顶部导航栏一行三段：网盘（横向滚动）｜功能｜网盘管理，高度固定 64px；
  数量超出可视高度时在面板内部滚动加载（QScrollArea + 滚轮）；
* 原来的「网盘管理」标签页已废弃：每个网盘在导航栏里各有一个按钮，
  点击即切换到该网盘的页面（登盘管理区 + V8 主页式文件浏览）；
* 左下角的「网盘管理」面板负责 新增网盘 / 编辑网盘 / 删除网盘（多实例）。
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QHBoxLayout, QInputDialog, QLabel,
    QMainWindow, QMessageBox, QPushButton, QScrollArea, QSizePolicy,
    QStackedWidget, QVBoxLayout, QWidget,
)

from ..配置 import (
    保存配置, 加载配置, 动作, 界面配置, 设置界面配置, 配置文件,
    网盘实例列表, 新增网盘实例, 更新网盘实例, 删除网盘实例, 适配器规格表,
    项目根,
)
from .控件样式 import 安装控件样式
from .主题管理器 import (主题管理器, 高度_管理按钮, 高度_功能按钮,
                    高度_网盘按钮)
from .传输页面 import 传输页面
from .播放页面 import 播放页面
from .日志页面 import 日志页面
from .网盘页面 import 网盘页面
from .网盘对话框 import 网盘编辑对话框
from .敏感词管理页面 import 敏感词管理页面
from .设置页面 import 设置页面
from .滚动区 import 包一层滚动

导航宽度 = 74
#: 顶部横向导航栏高度（按钮在里面垂直居中）
高度_顶栏 = 64
#: 顶部栏的最小宽度：小于它就让外层横向滚动，而不是把按钮压扁
最小顶栏宽 = 1180
#: 左侧功能导航栏宽度（功能按钮竖排用）
宽度_左侧功能栏 = 92
#: 顶部栏里的网盘按钮宽度（横排要按内容给宽，不能像竖排那样固定 74px）
宽度_网盘按钮 = 108
宽度_网盘按钮上限 = 190

#: 顶部栏各类按钮的固定高度。
#: 为什么要单独拎出来 + 每次套完主题重新钉一遍：主题 QSS 给按钮写了
#: ``padding: 12px 6px``，Qt 的样式引擎会据此把控件的**最小高度**改成
#: 「文字高 + 上下内边距」（60px 会被压成 42px）；窗口一矮，布局就一路把按钮
#: 挤到那个最小高度——按钮变矮、两行文字被裁掉。所以高度必须由代码说了算，
#: 见 :meth:`主窗口._钉死左侧栏尺寸`（名字沿用，实际钉的是顶部栏）。


class 日志桥(QObject):
    """适配器桥进程的日志在子线程里回调，用信号转到界面线程再写控件。"""

    消息 = Signal(str)


class 空状态页(QWidget):
    """没有配置任何网盘时的提示页。"""

    def __init__(self, 主窗口, 父=None):
        super().__init__(父)
        布局 = QVBoxLayout(self)
        布局.addStretch(1)
        标题 = QLabel("还没有配置任何网盘")
        标题.setAlignment(Qt.AlignCenter)
        标题.setStyleSheet("font-size: 18px; font-weight: bold;")
        布局.addWidget(标题)
        说明 = QLabel(
            "点击顶部右侧的「➕ 新增」添加百度 / 光鸭 / 夸克网盘，\n"
            "同一家网盘可以添加多个实例（各自一份适配器目录，凭证互不干扰）。\n"
            "加好后每个网盘会在顶部导航栏各占一个按钮，点击即切换页面。")
        说明.setAlignment(Qt.AlignCenter)
        说明.setWordWrap(True)
        布局.addWidget(说明)
        按钮行 = QHBoxLayout()
        按钮行.addStretch(1)
        新增 = QPushButton("➕ 新增网盘")
        新增.setObjectName("PrimaryButton")
        新增.clicked.connect(主窗口.新增网盘)
        按钮行.addWidget(新增)
        按钮行.addStretch(1)
        布局.addLayout(按钮行)
        布局.addStretch(1)


#: 记住装过的翻译器（QTranslator 必须有人持有引用，否则会被回收）
_翻译器 = None


def 安装中文翻译(应用) -> None:
    """让 Qt 自带的右键菜单/对话框按钮显示中文。

    Qt 内置的编辑框右键菜单（Undo/Cut/Copy/Paste/Select All…）默认是英文，
    用户反馈过。PySide6 自带 ``qtbase_zh_CN.qm``，装上就都变中文了
    （顺带把标准按钮也从 OK/Cancel 变成 确定/取消）。
    """
    global _翻译器
    if _翻译器 is not None:
        return
    try:
        from PySide6.QtCore import QLibraryInfo, QTranslator
        from PySide6.QtWidgets import QApplication
        # 注意：installTranslator 是 QApplication 的方法。以前这里传进来的是主窗口，
        # 于是抛 AttributeError 被 except 吞掉 —— 右键菜单一直是英文，还查不出原因。
        应用 = QApplication.instance() or 应用
        if 应用 is None or not hasattr(应用, "installTranslator"):
            return
        路径 = QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)
        翻译 = QTranslator(应用)
        if 翻译.load("qtbase_zh_CN", 路径):
            应用.installTranslator(翻译)
            _翻译器 = 翻译
    except Exception:  # noqa: BLE001 - 翻译装不上也不该影响启动
        pass


class 主窗口(QMainWindow):
    def __init__(self, 配置: dict | None = None,
                 配置路径: str | Path | None = None,
                 AI运行时=None, 主题: str = "", 启动日志=None):
        super().__init__()
        # 新开界面 = 复位"关窗闸门"（自检里会反复开关窗口）
        try:
            from .后台线程 import 正在关闭          # noqa: F401  （保持导入可用）
            _ = 正在关闭
        except Exception:
            pass
        self.配置路径 = Path(配置路径) if 配置路径 else 配置文件
        self.配置 = 配置 if 配置 is not None else 加载配置(self.配置路径)
        self._外部AI运行时 = AI运行时          # 启动自检装配好的那个
        self._启动主题 = 主题
        self._启动日志 = list(启动日志 or [])   # 启动自检的横幅原文
        self._日志桥 = 日志桥(self)
        self._日志桥.消息.connect(self._写日志界面, Qt.QueuedConnection)
        self.动作 = 动作(self.配置, 日志回调=self.追加日志)   # (消息, 级别)
        self._活动线程: list = []
        self._网盘按钮: dict[str, QPushButton] = {}
        self._网盘页面: dict[str, 网盘页面] = {}
        self._网盘状态: dict[str, bool] = {}
        self._传输页面: 传输页面 | None = None

        self._播放页面 = None
        self._日志页面: 日志页面 | None = None
        self._设置页面: 设置页面 | None = None
        self._敏感词页面: 敏感词管理页面 | None = None
        self._AI页面 = None
        self._AI运行时 = None
        self._AI不可用 = ""
        self._空状态页: 空状态页 | None = None
        self._当前标识 = ""

        self.setWindowTitle("网盘管理 V8_3 · 去 Alist 直连直传")
        self._按屏幕定尺寸()
        self._应用日志配置()
        self._构建界面()
        self._写入启动日志()
        self._应用主题(self._启动主题
                    or 界面配置(self.配置).get("主题")
                    or 主题管理器.获取默认主题())
        # 首屏直接落在上次用的网盘上（不用延时定时器，避免用户刚切页又被切回来）
        self.重建网盘导航(首选标识=界面配置(self.配置).get("上次网盘") or "")
        self._预热页面()

    # ==================== 屏幕适配 / 预热 ====================

    def _按屏幕定尺寸(self) -> None:
        """按**当前屏幕可用区**定初始尺寸与最小尺寸（小屏笔记本不再被窗口顶出屏幕）。

        以前是写死 ``resize(1400, 860)`` + ``setMinimumSize(1080, 620)``：
        1366×768 的笔记本上窗口比屏幕还大（标题栏/底栏被顶到屏幕外），
        1024×600 的上网本更惨 —— 最小尺寸就比屏幕大，Qt 也只能照做。
        现在：初始尺寸取"设计尺寸"和"屏幕可用区 - 边距"的较小值，
        最小尺寸也跟着屏幕收，页面本身有滚动区兜底，小屏上照样能用。
        """
        try:
            from PySide6.QtGui import QGuiApplication
            屏 = QGuiApplication.primaryScreen()
            可用 = 屏.availableGeometry() if 屏 is not None else None
        except Exception:  # noqa: BLE001
            可用 = None
        if 可用 is None or 可用.width() <= 0 or 可用.height() <= 0:
            self.resize(1400, 860)
            self.setMinimumSize(1080, 620)
            return
        # 留一点边距给标题栏/任务栏，别贴着屏幕边
        宽 = max(760, min(1400, 可用.width() - 60))
        高 = max(520, min(860, 可用.height() - 90))
        self.resize(宽, 高)
        # 最小尺寸再比初始小一档（小屏上也能再往小拉一点；页面本身有滚动区兜底），
        # 大屏上仍是设计值 1080×620，不会因为屏幕大就允许拉得比设计还小。
        self.setMinimumSize(min(1080, max(720, 宽 - 120)),
                            min(620, max(460, 高 - 80)))
        self._屏幕尺寸 = (可用.width(), 可用.height())

    def _预热页面(self) -> None:
        """窗口**还没显示**时，把各功能页先建出来 —— 首次点击就不再卡一秒。

        为什么要这么做：主题 QSS 有 126 条规则、界面 600+ 控件，某个页面第一次显示时
        Qt 要给其中每个控件做样式匹配 + 布局 + 首帧绘制。实测（Wine 真 Windows 运行时）：
        第一次切到 AI 页要 700~1150 ms、敏感词页 260 ms、播放页 250 ms ——
        用户感受到的就是"点一下卡一秒"。实测把这些页面在**窗口显示之前**先建好，
        之后每次切页都只要 60~70 ms（快 4~10 倍）。这段时间用户本来就看不到界面，
        等于白赚；页面切换发生在隐藏状态下，所以**不会闪**。

        设 ``V8_3_不预热界面=1`` 可跳过（排查启动问题时用）。
        """
        if os.environ.get("V8_3_不预热界面"):
            return
        try:
            导航 = (list(self._网盘按钮.values())
                  + [self.传输按钮, self.播放按钮, self.敏感词按钮,
                     self.AI按钮, self.日志按钮, self.设置按钮])
            起始页 = self.堆叠.currentWidget()
            起始按钮 = next((b for b in 导航 if b.objectName() == "active"), None)
            起始标签 = self.当前网盘标签.text()
        except Exception:  # noqa: BLE001
            return
        开始 = time.time()
        明细: list[str] = []
        for 名, 动作 in (("传输", self.切换到传输页), ("播放", self.切换到播放页),
                      ("敏感词", self.切换到敏感词页), ("日志", self.切换到日志页),
                      ("设置", self.切换到设置页), ("AI", self.切换到AI页)):
            t = time.time()
            try:
                动作()
                明细.append(f"{名} {(time.time() - t) * 1000:.0f}ms")
            except Exception as e:  # noqa: BLE001 - 某一页建不起来不该拖垮启动
                明细.append(f"{名} 跳过({type(e).__name__})")
        try:                      # 回到启动时那一页（窗口还没显示，用户看不到这一串切换）
            if 起始页 is not None:
                self.堆叠.setCurrentWidget(起始页)
            self._设置导航激活(起始按钮)
            self.当前网盘标签.setText(起始标签)
        except Exception:  # noqa: BLE001
            pass
        self.追加日志(f"[界面] 已预建各页（主题样式下首次显示最贵，先建好就不卡了）："
                  f"{'、'.join(明细)}，共 {(time.time() - 开始) * 1000:.0f} ms")

    # ==================== 界面搭建 ====================

    def _放进堆叠(self, 页: QWidget) -> QWidget:
        """页面入栈前统一套一层滚动区。

        窗口被拉小、或页面内容比可视区更宽时，用户可以上下/左右滑动查看，
        而不是让 Qt 压缩页面里的控件（传输表格被挤变形就是这么来的）。
        设置页/AI 页自己已经带滚动区，:func:`包一层滚动` 会原样返回。

        注意：入栈的是**外框**，所以切换页面不能直接 ``setCurrentWidget(页面)``，
        要走 :meth:`_切到` —— 那里会按页面把外框找出来（踩过：直接切会没反应）。
        """
        外层 = 包一层滚动(页)
        self._页面外框[id(页)] = 外层
        self._页面对象[id(页)] = 页
        return 外层

    def 当前页面(self) -> QWidget:
        """当前显示的**页面本身**（栈里放的是滚动外框，这里还原回去）。

        自检、脚本、将来任何"看看现在在哪一页"的地方都该用它，
        而不是直接看 ``堆叠.currentWidget()``（那拿到的是外框）。
        """
        当前 = self.堆叠.currentWidget()
        for 页, 外框 in self._页面外框.items():
            if 外框 is 当前 and 页 in self._页面对象:
                return self._页面对象[页]
        return 当前

    def _切到(self, 页: QWidget) -> None:
        """切到某个页面（自动处理"栈里放的是滚动外框"这件事）。"""
        self.堆叠.setCurrentWidget(self._页面外框.get(id(页), 页))

    def _构建界面(self):
        中央 = QWidget()
        self.setCentralWidget(中央)
        主布局 = QVBoxLayout(中央)
        主布局.setSpacing(8)
        主布局.setContentsMargins(10, 10, 10, 10)

        # 顶部：网盘（横向滚动）+ 网盘管理（新增/编辑/删除/退出）
        主布局.addWidget(self._构建顶部导航())

        安装中文翻译(self)
        self._页面外框: dict[int, QWidget] = {}   # id(页面) → 它的滚动外框
        self._页面对象: dict[int, QWidget] = {}   # id(页面) → 页面（活着引用，避免 id 被复用）
        self.堆叠 = QStackedWidget()

        # 中部：**左侧**功能导航（传输/播放/敏感词/AI/日志/设置）| 页面堆叠
        # 用户要求：功能按钮像以前一样竖排在左侧；网盘与管理留在顶部横排。
        中部 = QHBoxLayout()
        中部.setSpacing(8)
        中部.addWidget(self._构建左侧功能栏())
        中部.addWidget(self.堆叠, 1)
        主布局.addLayout(中部, 1)

        # 状态栏：左侧消息 + 右侧常驻（当前网盘 / 主题）
        self.状态标签 = QLabel("就绪")
        self.statusBar().addWidget(self.状态标签, 1)
        self.当前网盘标签 = QLabel("未选择网盘")
        self.statusBar().addPermanentWidget(self.当前网盘标签)
        self.statusBar().addPermanentWidget(QLabel("🎨"))
        self.主题下拉框 = QComboBox()
        for 主题名 in 主题管理器.获取所有主题名():
            self.主题下拉框.addItem(主题管理器.获取主题显示名(主题名), 主题名)
        self.主题下拉框.setMinimumWidth(160)
        self.主题下拉框.currentIndexChanged.connect(self._切换主题)
        self.statusBar().addPermanentWidget(self.主题下拉框)

    def _写入启动日志(self):
        """启动自检的核心信息也进日志页（终端已打印过，这里不再回显终端）。"""
        if not self._启动日志:
            return
        try:
            页 = self.日志页()
            for 行 in self._启动日志:
                页.追加(行, 落盘=True)
        except Exception:
            pass

    def _应用日志配置(self):
        """终端日志开关/级别来自 配置.json 的 界面.终端日志 / 界面.终端日志级别。"""
        try:
            界面 = self.配置.get("界面") or {}
            from ..日志 import 设置终端日志, 关闭 as 关闭终端, 开启 as 开启终端
            if not bool(界面.get("终端日志", True)):
                关闭终端()
            else:
                开启终端()
            设置终端日志(级别=str(界面.get("终端日志级别") or "信息"))
        except Exception:
            pass

    def _确保AI(self):
        """惰性构建 AI 运行时（AI 层不可用时返回 None，界面自动降级）。

        传输页与 AI 页共用这一个运行时：模型/价格/余额/时段/学习库/调度器。
        """
        已有 = getattr(self, "_AI运行时", None)
        if 已有 is None and getattr(self, "_外部AI运行时", None) is not None:
            # 启动自检已经建好了运行时，直接复用（避免重复初始化）
            self._AI运行时 = self._外部AI运行时
            已有 = self._AI运行时
        if 已有 is not None:
            if self._传输页面 is not None:
                self._传输页面.AI调度器 = getattr(已有, "调度器", None)
            return 已有
        if getattr(self, "_AI不可用", ""):
            return None
        try:
            from ..AI.运行时 import AI运行时
        except Exception as e:  # noqa: BLE001
            self._AI不可用 = f"AI 层不可用：{e}"
            self.追加日志(self._AI不可用)
            return None
        try:
            运行时 = AI运行时(self.配置, self.配置路径, 日志回调=self.追加日志)
        except Exception as e:  # noqa: BLE001
            self._AI不可用 = f"AI 运行时初始化失败：{e}"
            self.追加日志(self._AI不可用)
            return None
        self._AI运行时 = 运行时
        self._AI不可用 = ""
        if self._传输页面 is not None:
            self._传输页面.AI调度器 = 运行时.调度器
        return 运行时

    def AI运行时(self):
        return self._确保AI()

    def _构建顶部导航(self) -> QWidget:
        """顶部横向导航栏（用户要求：左侧竖排太挤，改成顶部横排）。

        布局（一行，全宽）：
            网盘 ▸ [🅱 百度网盘] [🦆 光鸭云盘] [🅠 夸克网盘] …   ← 横向滚动
            ｜ 📤 传输  🎬 播放  🔒 敏感词  🤖 AI  📋 日志  ⚙ 设置
            …（右侧）➕ 新增  ✏️ 编辑  🗑 删除  ⏻ 退出

        * 网盘按钮放在**横向滚动区**里：网盘再多也不会把功能按钮挤出去；
        * 整条栏再套一层横向滚动区：窗口很窄时不会把按钮压扁（只出滚动条）；
        * 按钮高度统一钉死（主题 QSS 的 padding 会改最小高度，所以要在套完主题后再钉一次）。
        """
        外层 = QScrollArea()
        外层.setObjectName("TopBarScroll")
        外层.setFrameShape(QScrollArea.NoFrame)
        外层.setWidgetResizable(True)
        外层.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        外层.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        外层.setFixedHeight(高度_顶栏)
        外层.viewport().setAutoFillBackground(False)

        栏 = QWidget()
        栏.setObjectName("TopBar")
        栏.setMinimumWidth(最小顶栏宽)
        行 = QHBoxLayout(栏)
        行.setContentsMargins(10, 6, 10, 6)
        行.setSpacing(6)

        # ---------------- 左：网盘（横向滚动） ----------------
        标题 = QLabel("网盘")
        标题.setObjectName("NavTitle")
        行.addWidget(标题)

        self.网盘滚动区 = QScrollArea()
        self.网盘滚动区.setObjectName("NavScroll")
        self.网盘滚动区.setFrameShape(QScrollArea.NoFrame)
        self.网盘滚动区.setWidgetResizable(True)
        self.网盘滚动区.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.网盘滚动区.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.网盘滚动区.setFixedHeight(高度_网盘按钮 + 12)
        self.网盘滚动区.setMinimumWidth(240)      # 至少露出一个半按钮
        self.网盘滚动区.viewport().setAutoFillBackground(False)
        self.网盘按钮容器 = QWidget()
        self.网盘按钮容器.setObjectName("NavScroll")
        self.网盘按钮布局 = QHBoxLayout(self.网盘按钮容器)
        self.网盘按钮布局.setContentsMargins(0, 0, 0, 0)
        self.网盘按钮布局.setSpacing(6)
        self.网盘按钮布局.addStretch(1)
        self.网盘滚动区.setWidget(self.网盘按钮容器)
        行.addWidget(self.网盘滚动区, 1)

        行.addWidget(self._建分隔线())

        # ---------------- 右：网盘管理 ----------------
        管理标题 = QLabel("网盘管理")
        管理标题.setObjectName("NavTitle")
        行.addWidget(管理标题)

        self.新增按钮 = QPushButton("➕ 新增")
        self.新增按钮.setFixedHeight(高度_管理按钮)
        self.新增按钮.setToolTip("添加网盘实例（同一家网盘可加多个账号）")
        self.新增按钮.clicked.connect(self.新增网盘)
        行.addWidget(self.新增按钮)

        self.编辑按钮 = QPushButton("✏️ 编辑")
        self.编辑按钮.setFixedHeight(高度_管理按钮)
        self.编辑按钮.setToolTip("修改当前网盘的名称/目录/线程数/启用状态")
        self.编辑按钮.clicked.connect(self.编辑当前网盘)
        行.addWidget(self.编辑按钮)

        self.删除按钮 = QPushButton("🗑 删除")
        self.删除按钮.setFixedHeight(高度_管理按钮)
        self.删除按钮.setObjectName("DeleteButton")
        self.删除按钮.setToolTip("从配置里移除当前网盘（不动适配器目录和登录数据）")
        self.删除按钮.clicked.connect(self.删除当前网盘)
        行.addWidget(self.删除按钮)

        self.退出按钮 = QPushButton("⏻ 退出")
        self.退出按钮.setFixedHeight(高度_管理按钮)
        self.退出按钮.setToolTip("关闭网盘管理（会先停掉正在跑的传输并清理后台进程）")
        self.退出按钮.clicked.connect(self.close)
        行.addWidget(self.退出按钮)

        行.addStretch(0)
        外层.setWidget(栏)
        return 外层

    def _构建左侧功能栏(self) -> QWidget:
        """左侧**纵向**功能导航（用户要求：功能按钮回到左侧竖排）。

        顶部横排留给"网盘 + 网盘管理"；这一列只有功能页入口：
        传输 / 播放 / 敏感词 / AI / 日志 / 设置。
        与旧版左侧栏一样：按钮高度钉死，窗口再矮也不压扁（装不下就内部滚动）。
        """
        列 = QWidget()
        列.setObjectName("LeftNav")
        列.setFixedWidth(宽度_左侧功能栏)
        列布局 = QVBoxLayout(列)
        列布局.setContentsMargins(6, 8, 6, 8)
        列布局.setSpacing(6)

        标题 = QLabel("功能")
        标题.setObjectName("NavTitle")
        标题.setAlignment(Qt.AlignCenter)
        列布局.addWidget(标题)

        self.传输按钮 = QPushButton("📤\n传输")
        self.传输按钮.setFixedHeight(高度_功能按钮)
        self.传输按钮.clicked.connect(self.切换到传输页)
        列布局.addWidget(self.传输按钮)

        self.播放按钮 = QPushButton("🎬\n播放")
        self.播放按钮.setFixedHeight(高度_功能按钮)
        self.播放按钮.setToolTip(
            "播放网盘里的视频（VLC 内核内嵌播放，AI 辅助加载/字幕/总结）")
        self.播放按钮.clicked.connect(self.切换到播放页)
        列布局.addWidget(self.播放按钮)

        self.敏感词按钮 = QPushButton("🔒\n敏感词")
        self.敏感词按钮.setFixedHeight(高度_功能按钮)
        self.敏感词按钮.setToolTip("敏感词库 + 上传预检改名 + 改名记录")
        self.敏感词按钮.clicked.connect(self.切换到敏感词页)
        列布局.addWidget(self.敏感词按钮)

        self.AI按钮 = QPushButton("🤖\nAI")
        self.AI按钮.setFixedHeight(高度_功能按钮)
        self.AI按钮.setToolTip("DeepSeek 余额/价格/模型与 AI 调度统计")
        self.AI按钮.clicked.connect(self.切换到AI页)
        列布局.addWidget(self.AI按钮)

        self.日志按钮 = QPushButton("📋\n日志")
        self.日志按钮.setFixedHeight(高度_功能按钮)
        self.日志按钮.clicked.connect(self.切换到日志页)
        列布局.addWidget(self.日志按钮)

        self.设置按钮 = QPushButton("⚙\n设置")
        self.设置按钮.setFixedHeight(高度_功能按钮)
        self.设置按钮.setToolTip("软件更新（一键从 GitHub 更新）、联系作者、关于")
        self.设置按钮.clicked.connect(self.切换到设置页)
        列布局.addWidget(self.设置按钮)

        列布局.addStretch(1)
        return 列

    @staticmethod
    def _建分隔线() -> QWidget:
        """顶部栏里的竖分隔线（把 网盘 / 功能 / 管理 三段分开，别再挤在一起）。"""
        线 = QWidget()
        线.setObjectName("TopBarSep")
        线.setFixedWidth(1)
        线.setFixedHeight(高度_功能按钮 - 12)
        return 线


    # ==================== 网盘导航（动态） ====================

    def 重建网盘导航(self, 首选标识: str = ""):
        """按配置重建左侧网盘按钮；启用中的实例才会出现。

        ``首选标识`` 用于启动时恢复上次使用的网盘（无效则退回第一个）。
        """
        for 按钮 in list(self._网盘按钮.values()):
            self.网盘按钮布局.removeWidget(按钮)
            按钮.deleteLater()
        self._网盘按钮.clear()

        实例们 = [x for x in 网盘实例列表(self.配置) if x["启用"]]
        规格表 = 适配器规格表(self.配置)
        for 索引, 实例 in enumerate(实例们):
            标识 = 实例["标识"]
            规格 = 规格表.get(标识)
            图标 = 规格.图标 if 规格 else "☁"
            显示名 = str(实例["名称"])
            # 横排：图标 + 名字一行显示（竖排时代用 \n 分两行）
            简称 = 显示名 if len(显示名) <= 8 else 显示名[:7] + "…"
            按钮 = QPushButton(f"{图标} {简称}")
            按钮.setProperty("navCloud", True)
            按钮.setFixedHeight(高度_网盘按钮)
            按钮.setToolTip(f"{实例['名称']}（{标识}）\n{实例['路径']}")
            按钮.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
            按钮.setMinimumWidth(宽度_网盘按钮)
            按钮.setMaximumWidth(宽度_网盘按钮上限)
            按钮.clicked.connect(lambda _=False, i=标识: self.切换网盘页(i))
            按钮.setContextMenuPolicy(Qt.CustomContextMenu)
            按钮.customContextMenuRequested.connect(
                lambda _p, i=标识: self._网盘右键菜单(i))
            self.网盘按钮布局.insertWidget(索引, 按钮)
            self._网盘按钮[标识] = 按钮

        # 新建的按钮同样会被主题 QSS 压低最小高度，建完立刻重新钉死
        self._钉死左侧栏尺寸()

        # 清掉已经不存在的页面
        有效 = {x["标识"] for x in 网盘实例列表(self.配置)}
        for 标识 in list(self._网盘页面):
            if 标识 not in 有效:
                页 = self._网盘页面.pop(标识)
                self.堆叠.removeWidget(页)
                页.关闭()
                页.deleteLater()
                按钮 = self._网盘按钮.pop(标识, None)
                if 按钮 is not None:
                    按钮.deleteLater()
                self._网盘状态.pop(标识, None)

        if not self._网盘按钮:
            if self._空状态页 is None:
                self._空状态页 = 空状态页(self)
                self.堆叠.addWidget(self._放进堆叠(self._空状态页))
            self._切到(self._空状态页)
            self._当前标识 = ""
            self.当前网盘标签.setText("未选择网盘")
            self._更新管理按钮()
            return

        if self._当前标识 not in self._网盘按钮:
            目标 = 首选标识 if 首选标识 in self._网盘按钮 \
                else next(iter(self._网盘按钮))
            self.切换网盘页(目标)
        else:
            self._设置导航激活(self._网盘按钮[self._当前标识])
        self._更新管理按钮()

    def _网盘右键菜单(self, 标识: str):
        from PySide6.QtWidgets import QMenu
        实例 = next((x for x in 网盘实例列表(self.配置) if x["标识"] == 标识), None)
        if 实例 is None:
            return
        菜单 = QMenu(self)
        菜单.addAction("打开该网盘页面",
                      lambda: self.切换网盘页(标识))
        菜单.addAction("✏️ 编辑网盘…",
                      lambda: (self.切换网盘页(标识), self.编辑当前网盘()))
        菜单.addAction("🔐 登录 / 换账号…",
                      lambda: (self.切换网盘页(标识), self._登录某网盘(标识)))
        菜单.addAction("🔄 刷新登录状态",
                      lambda: self._刷新某网盘状态(标识))
        菜单.addSeparator()
        菜单.addAction("🗑 删除网盘…",
                      lambda: (self.切换网盘页(标识), self.删除当前网盘()))
        菜单.exec(self.cursor().pos())

    def _登录某网盘(self, 标识: str):
        from .登录对话框 import 登录对话框
        实例 = next((x for x in 网盘实例列表(self.配置) if x["标识"] == 标识), None)
        if 实例 is None:
            return
        try:
            对话框 = 登录对话框(self, 实例, self)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "登录入口不可用", str(e))
            return
        if 对话框.exec() == QDialog.Accepted and 对话框.成功:
            self.追加日志(f"[{实例['名称']}] 登录成功（统一登录）")
        self._刷新某网盘状态(标识)

    def 刷新网盘状态(self, 标识: str):
        """让某个网盘页重新拉一次账号状态（登录成功后调用）。"""
        self._刷新某网盘状态(标识)

    def _刷新某网盘状态(self, 标识: str):
        页 = self._网盘页面.get(标识)
        if 页 is None:
            self.切换网盘页(标识)
            页 = self._网盘页面.get(标识)
        if 页 is not None:
            页.刷新管理区()

    def 切换网盘页(self, 标识: str):
        if not 标识:
            return
        实例 = next((x for x in 网盘实例列表(self.配置) if x["标识"] == 标识), None)
        if 实例 is None:
            QMessageBox.warning(self, "提示", f"网盘不存在：{标识}")
            return
        if not 实例["启用"]:
            QMessageBox.information(
                self, "已停用", f"「{实例['名称']}」当前是停用状态，"
                "请先「编辑网盘」启用它。")
            return
        if 标识 not in self._网盘页面:
            self._网盘页面[标识] = 网盘页面(self, 实例)
            self.堆叠.addWidget(self._放进堆叠(self._网盘页面[标识]))
        self._当前标识 = 标识
        self._切到(self._网盘页面[标识])
        self._设置导航激活(self._网盘按钮.get(标识))
        self.当前网盘标签.setText(f"📁 {实例['名称']}")
        设置界面配置(self.配置, 上次网盘=标识)
        self.状态消息(f"已切换到：{实例['名称']}")
        self._更新管理按钮()

    def 切换到播放页(self):
        if self._播放页面 is None:
            self._播放页面 = 播放页面(self)
            # 后台准备线程 → 界面线程（Qt 信号跨线程安全）
            self._播放页面.状态更新.connect(
                self._播放页面.处理准备结果,
                Qt.QueuedConnection)
            self.堆叠.addWidget(self._放进堆叠(self._播放页面))
        else:
            self._播放页面.刷新网盘列表()
        self._切到(self._播放页面)
        self._设置导航激活(self.播放按钮)
        self.当前网盘标签.setText("🎬 视频播放")
        self._更新管理按钮()

    def 播放页面(self) -> 播放页面:
        if self._播放页面 is None:
            self.切换到播放页()
        return self._播放页面

    def 播放网盘视频(self, 标识: str, 远端路径: str,
                独立窗口: bool = False) -> None:
        """网盘文件表格里双击视频时走这里：切到播放页并开始播放。

        :param 独立窗口: True 则直接在独立窗口里播（不占主窗口）。
        """
        self.切换到播放页()
        页面 = self.播放页面()
        页面.播放指定视频(标识, 远端路径, 独立窗口=独立窗口)
        self.追加日志(f"🎬 播放：{远端路径}"
                  + ("（独立窗口）" if 独立窗口 else ""))

    def 切换到传输页(self):
        if self._传输页面 is None:
            self._传输页面 = 传输页面(self)
            self.堆叠.addWidget(self._放进堆叠(self._传输页面))
        else:
            self._传输页面.刷新网盘列表()
        self._切到(self._传输页面)
        self._设置导航激活(self.传输按钮)
        self.当前网盘标签.setText("📤 跨网盘传输")
        self._更新管理按钮()

    def 传输页面(self) -> 传输页面:
        if self._传输页面 is None:
            self._传输页面 = 传输页面(self)
            self.堆叠.addWidget(self._放进堆叠(self._传输页面))
            运行时 = getattr(self, "_AI运行时", None)
            if 运行时 is not None:
                self._传输页面.AI调度器 = 运行时.调度器
        return self._传输页面

    def 日志页(self) -> 日志页面:
        if self._日志页面 is None:
            self._日志页面 = 日志页面(self)
            self.堆叠.addWidget(self._放进堆叠(self._日志页面))
        return self._日志页面

    def 切换到敏感词页(self):
        if self._敏感词页面 is None:
            self._敏感词页面 = 敏感词管理页面(self)
            self.堆叠.addWidget(self._放进堆叠(self._敏感词页面))
        else:
            self._敏感词页面.刷新()
        self._切到(self._敏感词页面)
        self._设置导航激活(self.敏感词按钮)
        self.当前网盘标签.setText("🔒 敏感词")
        self._更新管理按钮()

    def 切换到AI页(self):
        self._确保AI()
        if self._AI页面 is None:
            from .AI页面 import AI状态页面
            self._AI页面 = AI状态页面(self)
            self.堆叠.addWidget(self._放进堆叠(self._AI页面))
        else:
            self._AI页面.刷新()
        self._切到(self._AI页面)
        self._设置导航激活(self.AI按钮)
        self.当前网盘标签.setText("🤖 AI")
        self._更新管理按钮()

    def AI页面(self):
        self.切换到AI页()
        return self._AI页面

    def 切换到日志页(self):
        if self._日志页面 is None:
            self._日志页面 = 日志页面(self)
            self.堆叠.addWidget(self._放进堆叠(self._日志页面))
        self._切到(self._日志页面)
        self._设置导航激活(self.日志按钮)
        self.当前网盘标签.setText("📋 日志")
        self._更新管理按钮()

    def 设置页(self) -> 设置页面:
        if self._设置页面 is None:
            self._设置页面 = 设置页面(self)
            self.堆叠.addWidget(self._放进堆叠(self._设置页面))
        return self._设置页面

    def 切换到设置页(self):
        self.设置页().刷新()
        self._切到(self._设置页面)
        self._设置导航激活(self.设置按钮)
        self.当前网盘标签.setText("⚙ 设置")
        self._更新管理按钮()

    def _设置导航激活(self, 激活按钮):
        for 按钮 in (list(self._网盘按钮.values())
                   + [self.传输按钮, self.播放按钮, self.敏感词按钮,
                      self.AI按钮, self.日志按钮, self.设置按钮]):
            按钮.setObjectName("active" if 按钮 is 激活按钮 else "")
            try:
                按钮.style().unpolish(按钮)
                按钮.style().polish(按钮)
                按钮.update()
            except Exception:
                pass

    def _更新管理按钮(self):
        有当前 = bool(self._当前标识) and self._当前标识 in self._网盘按钮
        self.编辑按钮.setEnabled(有当前)
        self.删除按钮.setEnabled(有当前)

    # ==================== 新增 / 编辑 / 删除 ====================

    def 新增网盘(self):
        对话框 = 网盘编辑对话框(self.配置, None, self)
        if 对话框.exec() != QDialog.Accepted or not 对话框.结果:
            return
        数据 = 对话框.结果
        try:
            新增网盘实例(
                self.配置, 数据["类型"], 名称=数据["名称"],
                路径=数据["路径"], 线程数=数据["线程数"],
                启用=数据["启用"], 标识=数据["标识"])
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "新增失败", str(e))
            return
        self._配置已变(f"已新增网盘：{数据['名称']}（{数据['标识']}）")
        self.重建网盘导航()
        self.切换网盘页(数据["标识"])

    def 编辑当前网盘(self):
        标识 = self._当前标识 or self._选择网盘("编辑哪个网盘？")
        if not 标识:
            return
        实例 = next((x for x in 网盘实例列表(self.配置) if x["标识"] == 标识), None)
        if 实例 is None:
            return
        if not self._确认可打断("编辑网盘"):
            return
        对话框 = 网盘编辑对话框(self.配置, 实例, self)
        if 对话框.exec() != QDialog.Accepted or not 对话框.结果:
            return
        数据 = 对话框.结果
        try:
            更新网盘实例(
                self.配置, 标识, 名称=数据["名称"], 路径=数据["路径"],
                线程数=数据["线程数"], 启用=数据["启用"])
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "编辑失败", str(e))
            return
        self.动作.移除适配器(标识)      # 目录/线程数可能变了，重建桥进程
        self._移除页面(标识)            # 名称/路径可能变了，页面重建一次更省心
        self._配置已变(f"已更新网盘：{数据['名称']}（{标识}）")
        self.重建网盘导航()
        if 数据["启用"]:
            self.切换网盘页(标识)

    def 删除当前网盘(self, 自动选择: str = "") -> None:
        """删除当前网盘。

        ``自动选择`` 为空时弹确认框（三个按钮：只删除配置 / 连数据一起删 / 取消）；
        传 ``"只删除配置"`` 或 ``"连数据"`` 时跳过弹窗直接执行 —— 自检和脚本用得上
        （模态弹窗在离屏自检里会一直等下去）。
        """
        标识 = self._当前标识 or self._选择网盘("删除哪个网盘？")
        if not 标识:
            return
        实例 = next((x for x in 网盘实例列表(self.配置) if x["标识"] == 标识), None)
        if 实例 is None:
            return
        if not self._确认可打断("删除网盘"):
            return
        目录 = str(实例.get("路径") or "")
        框 = QMessageBox(self)
        框.setWindowTitle("确认删除网盘")
        框.setIcon(QMessageBox.Warning)
        框.setText(f"删除网盘「{实例['名称']}」（{标识}）？")
        框.setInformativeText(
            f"适配器目录：\n{目录}\n\n"
            "「删除配置」= 只从配置里移除，目录和登录数据留着（下次可复用）；\n"
            "「连数据一起删」= 同时删掉上面的目录（登录凭证、缓存都没了，"
            "下次新增会分配一个新目录）。\n"
            "两种都不会动网盘上的任何文件。")
        只删配置 = 框.addButton("只删除配置", QMessageBox.AcceptRole)
        连数据 = 框.addButton("连数据一起删", QMessageBox.DestructiveRole)
        框.addButton("取消", QMessageBox.RejectRole)
        框.setDefaultButton(只删配置)
        if 自动选择:
            删数据 = 自动选择 in ("连数据", "连数据一起删")
        else:
            框.exec()
            点了 = 框.clickedButton()
            if 点了 not in (只删配置, 连数据):
                return
            删数据 = 点了 is 连数据
        try:
            删除网盘实例(self.配置, 标识)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "删除失败", str(e))
            return
        if 删数据 and 目录:
            # 自动选择（自检/脚本）时不要弹模态框：在离屏自检里模态框会一直等下去，
            # 把整轮自检挂死；改成写日志，人工操作时才提示。
            def 提示(标题: str, 文本: str) -> None:
                if 自动选择:
                    self.追加日志(f"[{实例['名称']}] {标题}：{文本}")
                else:
                    QMessageBox.information(self, 标题, 文本)

            try:
                目标 = Path(目录)
                真实 = 目标.resolve()
                if 目标.is_dir() and (项目根 in 真实.parents
                                 or 真实.parent == 项目根):
                    shutil.rmtree(目标)
                    self.追加日志(f"[{实例['名称']}] 已删除适配器目录：{目录}")
                else:
                    提示("未删除目录",
                       f"这个目录不在项目内，为安全起见没有自动删除：\n{目录}")
            except Exception as e:  # noqa: BLE001
                提示("目录删除失败", str(e))
        self.动作.移除适配器(标识)
        self._移除页面(标识)
        if self._当前标识 == 标识:
            self._当前标识 = ""
        self._配置已变(f"已删除网盘：{实例['名称']}（{标识}）")
        self.重建网盘导航()

    def _有批次在跑(self) -> bool:
        页 = self._传输页面
        线程 = getattr(页, "_批次线程", None) if 页 is not None else None
        return bool(线程 is not None and 线程.isRunning())

    def _确认可打断(self, 操作: str) -> bool:
        """编辑/删除网盘会重启或关掉它的桥进程，正在跑的批次会失败。"""
        if not self._有批次在跑():
            return True
        return QMessageBox.question(
            self, "有传输任务在跑",
            f"当前还有跨网盘批次在传输。{操作}会重启/关闭该网盘的桥进程，"
            "正在进行的文件会失败。\n\n仍要继续吗？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No) == QMessageBox.Yes

    def _移除页面(self, 标识: str):
        页 = self._网盘页面.pop(标识, None)
        if 页 is not None:
            self.堆叠.removeWidget(页)
            页.关闭()
            页.deleteLater()
        self._网盘状态.pop(标识, None)

    def _选择网盘(self, 提示: str) -> str:
        实例们 = [x for x in 网盘实例列表(self.配置) if x["启用"]]
        if not 实例们:
            QMessageBox.information(self, "提示", "还没有可用的网盘，请先「新增网盘」")
            return ""
        标签 = [f"{x['名称']}（{x['标识']}）" for x in 实例们]
        选择, ok = QInputDialog.getItem(self, "选择网盘", 提示, 标签, 0, False)
        if not ok or not 选择:
            return ""
        return 实例们[标签.index(选择)]["标识"]

    def _配置已变(self, 消息: str):
        保存配置(self.配置, self.配置路径)
        self.动作.刷新规格表()
        self.追加日志(消息)
        self.状态消息(消息)

    # ==================== 主题 ====================

    def _切换主题(self, _索引=None):
        主题名 = self.主题下拉框.currentData()
        if not 主题名:
            return
        self._应用主题(主题名)
        设置界面配置(self.配置, 主题=主题名)
        保存配置(self.配置, self.配置路径)
        self.追加日志(f"已切换主题：{主题管理器.获取主题显示名(主题名)}")

    def _钉死左侧栏尺寸(self):
        """把顶部导航按钮的高度重新钉死（名字沿用，见下）。

        必须在**每次套完主题之后**调用：主题 QSS 的 ``padding`` 会让 Qt 把握件的
        最小高度改成「文字高 + 上下内边距」，``setFixedHeight`` 设的 60px 会被压成
        42px；窗口一矮，布局就顺着这个最小高度把按钮挤扁（文字被裁掉）。
        在样式生效后再钉一遍，布局就没有压缩余地了 —— 装不下时改由外层滚动区滚动。
        """
        功能按钮 = (getattr(self, "传输按钮", None), getattr(self, "播放按钮", None),
                 getattr(self, "敏感词按钮", None), getattr(self, "AI按钮", None),
                 getattr(self, "日志按钮", None))
        for 按钮 in 功能按钮:
            if 按钮 is not None:
                按钮.setFixedHeight(高度_功能按钮)
        for 按钮 in (getattr(self, "新增按钮", None), getattr(self, "编辑按钮", None),
                   getattr(self, "删除按钮", None),
                   getattr(self, "退出按钮", None)):
            if 按钮 is not None:
                按钮.setFixedHeight(高度_管理按钮)
        for 按钮 in getattr(self, "_网盘按钮", {}).values():
            按钮.setFixedHeight(高度_网盘按钮)
            # 横排：宽按内容自适应（给个上下限），别像竖排那样固定宽度
            按钮.setMinimumWidth(宽度_网盘按钮)
            按钮.setMaximumWidth(宽度_网盘按钮上限)
        容器 = getattr(self, "网盘按钮容器", None)
        滚动 = getattr(self, "网盘滚动区", None)
        if 容器 is not None and getattr(self, "_网盘按钮", None):
            需要 = len(self._网盘按钮) * (宽度_网盘按钮 + 6) + 8
            容器.setMinimumWidth(需要)
            # 按内容收窄：网盘少的时候别把「功能」那一段推到最右边
            if 滚动 is not None:
                滚动.setMaximumWidth(min(需要 + 18, 宽度_网盘按钮上限 * 6))
                滚动.setMinimumWidth(min(需要 + 18, 240))

    def _应用主题(self, 主题名: str):
        主题名 = 主题名 or 主题管理器.获取默认主题()
        应用 = QApplication.instance()
        if 应用 is not None:
            # 自绘的勾选框/下拉箭头：第一次必须赶在 setStyleSheet 之前装好基样式
            安装控件样式(应用)
            应用.setStyleSheet(主题管理器.获取样式表(主题名))
        idx = self.主题下拉框.findData(主题名)
        if idx >= 0 and self.主题下拉框.currentIndex() != idx:
            self.主题下拉框.blockSignals(True)
            self.主题下拉框.setCurrentIndex(idx)
            self.主题下拉框.blockSignals(False)
        # 样式刚换过，控件的最小高度被 QSS 重算过：立刻把左侧栏高度钉回去
        self._钉死左侧栏尺寸()

    # ==================== 日志 / 状态 ====================

    def 追加日志(self, 消息: str, 级别: str = "信息"):
        """线程安全：任何线程都可以调用。

        * 终端：立刻打印（V8 风格，方便调试和维护）；
        * 日志页：通过信号转回界面线程再写控件。
        """
        文本 = str(消息)
        try:
            from ..日志 import 终端输出
            终端输出(文本, 级别=级别)
        except Exception:
            pass
        try:
            self._日志桥.消息.emit(文本)
        except Exception:
            pass

    def _写日志界面(self, 消息: str):
        self.日志页().追加(str(消息), 落盘=True)

    def 状态消息(self, 消息: str, 毫秒: int = 6000):
        self.状态标签.setText(str(消息))
        if 毫秒:
            QTimer.singleShot(毫秒, lambda: self.状态标签.setText("就绪"))

    def 设置网盘状态(self, 标识: str, 已登录: bool):
        """网盘页刷新到登录状态后，顺手更新导航按钮的提示（绿点/灰点）。"""
        self._网盘状态[标识] = bool(已登录)
        按钮 = self._网盘按钮.get(标识)
        实例 = next((x for x in 网盘实例列表(self.配置) if x["标识"] == 标识), None)
        if 按钮 is None or 实例 is None:
            return
        状态行 = "🟢 已登录" if 已登录 else "⚪ 未登录"
        按钮.setToolTip(f"{实例['名称']}（{标识}）　{状态行}\n{实例['路径']}")

    def 登记线程(self, 线程):
        self._活动线程.append(线程)
        线程.finished.connect(lambda t=线程: self._清理线程(t))
        return 线程

    def _清理线程(self, 线程):
        try:
            self._活动线程.remove(线程)
        except ValueError:
            pass
        线程.deleteLater()

    # ==================== 关闭 ====================

    #: 关窗时"等在飞行中的桥调用"的总预算（秒）。桥调用超时是分钟级，
    #: 真等满会像卡死；实测正常调用 1~3 秒就回来，给 20 秒足够，
    #: 实在等不动的会在后面"关闭适配器 → 再等一轮"里被快速放掉。
    关窗等待预算秒 = 20.0

    def _所有在跑的线程(self) -> list:
        """主窗口 + 各网盘页 + 登录对话框 + AI 页里所有还在跑的 QThread。"""
        线程们 = list(self._活动线程)
        for 页 in list(getattr(self, "_网盘页面", {}).values()):
            线程们 += list(getattr(页, "_操作线程", []) or [])
        for 对话 in self.findChildren(QDialog):
            线程们 += list(getattr(对话, "_线程", []) or [])
            线程们 += list(getattr(对话, "_核对线程们", []) or [])
        页 = getattr(self, "_AI页面", None)
        if 页 is not None:
            线程们 += list(getattr(页, "_市场计时器们", []) or [])
        结果 = []
        for 线程 in 线程们:
            try:
                if 线程 is not None and 线程.isRunning():
                    结果.append(线程)
            except Exception:
                continue            # 已经失效的对象直接跳过
        return 结果

    def _等在跑的线程(self, 预算秒: float) -> int:
        """等在跑的线程收工；返回超时后仍在跑的数量。"""
        import time as _time
        截止 = _time.time() + max(0.5, float(预算秒))
        剩 = 0
        for 线程 in self._所有在跑的线程():
            剩余 = 截止 - _time.time()
            if 剩余 <= 0.2:
                剩 += 1
                continue
            try:
                线程.wait(int(剩余 * 1000))
            except Exception:
                pass
            try:
                if 线程.isRunning():
                    剩 += 1
            except Exception:
                pass
        return 剩

    def closeEvent(self, 事件):
        """关窗：**先让在飞的调用落地，再让 Qt 销毁控件**。

        现场崩溃（2026-09-19 02:01:33）：
            QThread: Destroyed while thread '' is still running
            Fatal Python error: Aborted
        原因是关窗时还有一个 `账号状态线程` 卡在适配器调用里（future.result），
        而 Qt 在销毁父控件时把它的 C++ 对象一起删了 —— Qt 遇到"运行中的
        QThread 被销毁"会直接 abort，这**不是 Python 异常，try/except 抓不住**。
        所以顺序必须是：停轮询 → 等在飞线程 → 关适配器（让还卡着的快速失败）
        → 再等一轮 → 才交给 Qt 销毁。
        """
        # ⓪ 先关上"关窗闸门"：之后界面线程不再发起任何适配器调用
        #    （否则关了适配器又会被重建、还会造出新的运行中线程 → Qt abort）
        try:
            from .后台线程 import 进入关闭态
            进入关闭态(self)
        except Exception:
            pass
        # ① 还开着的登录对话框先关（它自己会等"账号状态核对"线程）
        for 对话 in list(self.findChildren(QDialog)):
            try:
                对话.close()
            except Exception:
                pass
        # ② 停掉各页的凭证轮询、别再制造新线程
        for 页 in list(getattr(self, "_网盘页面", {}).values()):
            try:
                计时 = getattr(页, "_凭证计时", None)
                if 计时 is not None:
                    计时.stop()
            except Exception:
                pass
        # ③ 等在飞的桥调用落地（给足预算，别让它们带着"运行中"被删）
        self._等在跑的线程(self.关窗等待预算秒)
        # ④ 关适配器：还卡着的调用会立刻报"适配器已关闭"而退出
        try:
            self.动作.关闭()
        except Exception:
            pass
        # ⑤ 再等一轮，把④放掉的线程收干净
        self._等在跑的线程(6.0)
        try:
            if self._传输页面 is not None:
                self._传输页面.关闭()
        except Exception:
            pass
        for 页 in list(getattr(self, "_网盘页面", {}).values()):
            try:
                页.关闭()
            except Exception:
                pass
        self._活动线程.clear()
        try:
            保存配置(self.配置, self.配置路径)
        except Exception:
            pass
        super().closeEvent(事件)


def _调优应用(应用):
    """按平台关掉几个"拖慢界面"的开关，并把环境信息写进诊断。

    Windows 上实测过"点一下卡几分钟"，常见放大因素：
      * **可访问性桥（UIA）**：装了某些输入法/屏幕阅读器时，Qt 会为每个控件建
        可访问性节点，控件一多就成倍变慢 —— 本项目界面有几百个控件，
        这里显式关掉（默认也没有辅助功能依赖）；
      * **原生菜单栏**：Windows 上是原生菜单，主题样式会失效还会触发额外重绘；
      * 高 DPI 缩放策略：取整会让 125%/150% 缩放下出现半像素重绘，用 PassThrough 更稳。
    所有开关都是"失败就跳过"，不影响功能。
    """
    try:
        # Windows：把"中文 + emoji"字体族**显式**列给 Qt。
        # 不列的话，每个 emoji 都要走一遍"字体回退查找"（彩色字体更贵），
        # 而本项目界面里 emoji 上千个 —— 真机 windows-latest 实测：
        # AI 页建控件 24 秒、刷新又 24 秒（同一份代码 Linux 只要 0.4 秒）。
        # 显式列出后 Qt 按顺序直接取字体，不再逐字符回退查询。
        import sys as _sys
        if _sys.platform.startswith("win"):
            from PySide6.QtGui import QFont
            字体 = QFont()
            字体.setFamilies(["Microsoft YaHei UI", "Microsoft YaHei",
                            "Segoe UI", "Segoe UI Emoji"])
            应用.setFont(字体)
    except Exception:
        pass
    try:
        from PySide6.QtCore import Qt
        # 不要可访问性桥（Windows 上最明显的卡顿来源之一；可用环境变量强制打开）
        import os as _os
        if _os.environ.get("V8_3_保留可访问性", "") not in ("1", "true", "True"):
            应用.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
            try:
                from PySide6.QtCore import QCoreApplication
                QCoreApplication.setAttribute(Qt.AA_DontUseNativeMenuBar, True)
            except Exception:
                pass
    except Exception:
        pass
    try:
        # 环境/渲染器 → **同时**写诊断日志和启动日志（卡顿排查全靠它；
        # 诊断没开时也要能拿到，所以这里直接调追加日志）
        import platform as _pf
        from PySide6.QtGui import QGuiApplication
        屏 = ""
        try:
            主屏 = QGuiApplication.primaryScreen()
            if 主屏 is not None:
                比例 = 主屏.devicePixelRatio() or 1.0
                几何 = 主屏.geometry()
                屏 = (f"{几何.width()}x{几何.height()} @{比例:.2f}x"
                     f"（逻辑 {主屏.size().width()}x{主屏.size().height()}）")
        except Exception:
            pass
        文本 = (f"环境：{_pf.platform()} {_pf.release()}｜Python {_pf.python_version()}"
              f"｜平台插件 {QGuiApplication.platformName()}｜屏幕 {屏}")
        print(f"[环境] {文本}", flush=True)
        try:
            from ..卡顿诊断 import _写 as _诊断写
            _诊断写(文本)
        except Exception:
            pass
    except Exception:
        pass
    return 应用


def 运行界面(AI运行时=None, 主题: str = "", 启动日志=None):
    import sys as _sys
    应用 = QApplication.instance() or QApplication(_sys.argv)
    应用 = _调优应用(应用)
    if 主题:
        try:
            应用.setStyleSheet(主题管理器.获取样式表(主题))
        except Exception:
            pass
    窗口 = 主窗口(AI运行时=AI运行时, 主题=主题, 启动日志=启动日志)
    窗口.show()
    return 应用.exec()
