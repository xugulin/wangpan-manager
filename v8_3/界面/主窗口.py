"""V8_3 主窗口：V8 风格的左侧导航 + 切换式页面。

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

* 左侧导航栏宽度固定（70px），高度随窗口；网盘按钮从上往下排列，
  数量超出可视高度时在面板内部滚动加载（QScrollArea + 滚轮）；
* 原来的「网盘管理」标签页已废弃：每个网盘在导航栏里各有一个按钮，
  点击即切换到该网盘的页面（登盘管理区 + V8 主页式文件浏览）；
* 左下角的「网盘管理」面板负责 新增网盘 / 编辑网盘 / 删除网盘（多实例）。
"""

from __future__ import annotations

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

导航宽度 = 74

#: 左侧栏各类按钮的固定高度。
#: 为什么要单独拎出来 + 每次套完主题重新钉一遍：主题 QSS 给按钮写了
#: ``padding: 12px 6px``，Qt 的样式引擎会据此把控件的**最小高度**改成
#: 「文字高 + 上下内边距」（60px 会被压成 42px）；窗口一矮，布局就一路把按钮
#: 挤到那个最小高度——按钮变矮、两行文字被裁掉。所以高度必须由代码说了算，
#: 见 :meth:`主窗口._钉死左侧栏尺寸`。


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
            "点击左下角的「➕ 新增网盘」添加百度 / 光鸭 / 夸克网盘，\n"
            "同一家网盘可以添加多个实例（各自一份适配器目录，凭证互不干扰）。\n"
            "加好后每个网盘会在左侧导航栏各占一个按钮，点击即切换页面。")
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


class 主窗口(QMainWindow):
    def __init__(self, 配置: dict | None = None,
                 配置路径: str | Path | None = None,
                 AI运行时=None, 主题: str = "", 启动日志=None):
        super().__init__()
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
        self.resize(1400, 860)
        self.setMinimumSize(1080, 620)
        self._应用日志配置()
        self._构建界面()
        self._写入启动日志()
        self._应用主题(self._启动主题
                    or 界面配置(self.配置).get("主题")
                    or 主题管理器.获取默认主题())
        # 首屏直接落在上次用的网盘上（不用延时定时器，避免用户刚切页又被切回来）
        self.重建网盘导航(首选标识=界面配置(self.配置).get("上次网盘") or "")

    # ==================== 界面搭建 ====================

    def _构建界面(self):
        中央 = QWidget()
        self.setCentralWidget(中央)
        主布局 = QHBoxLayout(中央)
        主布局.setSpacing(10)
        主布局.setContentsMargins(10, 10, 10, 10)

        主布局.addWidget(self._构建左侧栏())

        self.堆叠 = QStackedWidget()
        主布局.addWidget(self.堆叠, 1)

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

    def _构建左侧栏(self) -> QWidget:
        """左侧竖向导航栏。

        高度**不随内容变**：整根栏目固定占满窗口高度，按钮保持各自的高度；
        内容装不下时出现纵向滚动条（上下滑动看溢出的按钮），而不是把按钮压扁。
        所以这里是「外层滚动区（整栏）+ 内层滚动区（只有网盘按钮）」两层：
        网盘多到放不下时内层先滚，功能按钮（传输/播放/敏感词/AI/日志）始终露着。
        """
        列 = QWidget()
        列.setFixedWidth(导航宽度)
        列布局 = QVBoxLayout(列)
        列布局.setContentsMargins(0, 0, 0, 0)
        列布局.setSpacing(0)

        # 整栏都在滚动区里：窗口再矮也只是出滚动条，绝不压扁按钮
        self.导航滚动区 = QScrollArea()
        self.导航滚动区.setObjectName("NavScroll")
        self.导航滚动区.setWidgetResizable(True)
        self.导航滚动区.setFrameShape(QScrollArea.NoFrame)
        self.导航滚动区.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.导航滚动区.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.导航滚动区.viewport().setAutoFillBackground(False)

        self.导航内容 = QWidget()
        self.导航内容.setObjectName("NavScroll")
        内容布局 = QVBoxLayout(self.导航内容)
        内容布局.setContentsMargins(0, 0, 0, 0)
        内容布局.setSpacing(8)

        # ---- 上：导航面板（网盘按钮 + 传输/日志）----
        导航 = QWidget()
        导航.setObjectName("NavPanel")
        导航布局 = QVBoxLayout(导航)
        导航布局.setContentsMargins(6, 10, 6, 10)
        导航布局.setSpacing(6)

        标题 = QLabel("网盘")
        标题.setObjectName("NavTitle")
        标题.setAlignment(Qt.AlignCenter)
        导航布局.addWidget(标题)

        # 网盘按钮放在滚动区里：数量超出高度就在面板内部滚动
        self.网盘滚动区 = QScrollArea()
        self.网盘滚动区.setObjectName("NavScroll")
        self.网盘滚动区.setWidgetResizable(True)
        self.网盘滚动区.setFrameShape(QScrollArea.NoFrame)
        self.网盘滚动区.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.网盘滚动区.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.网盘滚动区.viewport().setAutoFillBackground(False)
        self.网盘按钮容器 = QWidget()
        self.网盘按钮容器.setObjectName("NavScroll")
        self.网盘按钮布局 = QVBoxLayout(self.网盘按钮容器)
        self.网盘按钮布局.setContentsMargins(0, 0, 0, 0)
        self.网盘按钮布局.setSpacing(6)
        self.网盘按钮布局.addStretch(1)
        self.网盘滚动区.setWidget(self.网盘按钮容器)
        self.网盘滚动区.setMinimumHeight(120)   # 再矮也至少露出两个网盘按钮
        导航布局.addWidget(self.网盘滚动区, 1)

        功能标题 = QLabel("功能")
        功能标题.setObjectName("NavTitle")
        功能标题.setAlignment(Qt.AlignCenter)
        导航布局.addWidget(功能标题)

        self.传输按钮 = QPushButton("📤\n传输")
        self.传输按钮.setFixedHeight(高度_功能按钮)
        self.传输按钮.clicked.connect(self.切换到传输页)
        导航布局.addWidget(self.传输按钮)

        self.播放按钮 = QPushButton("🎬\n播放")
        self.播放按钮.setFixedHeight(高度_功能按钮)
        self.播放按钮.setToolTip(
            "播放网盘里的视频（VLC 内核内嵌播放，AI 辅助加载/字幕/总结）")
        self.播放按钮.clicked.connect(self.切换到播放页)
        导航布局.addWidget(self.播放按钮)

        self.敏感词按钮 = QPushButton("🔒\n敏感词")
        self.敏感词按钮.setFixedHeight(高度_功能按钮)
        self.敏感词按钮.setToolTip("敏感词库 + 上传预检改名 + 改名记录")
        self.敏感词按钮.clicked.connect(self.切换到敏感词页)
        导航布局.addWidget(self.敏感词按钮)

        self.AI按钮 = QPushButton("🤖\nAI")
        self.AI按钮.setFixedHeight(高度_功能按钮)
        self.AI按钮.setToolTip("DeepSeek 余额/价格/模型与 AI 调度统计")
        self.AI按钮.clicked.connect(self.切换到AI页)
        导航布局.addWidget(self.AI按钮)

        self.日志按钮 = QPushButton("📋\n日志")
        self.日志按钮.setFixedHeight(高度_功能按钮)
        self.日志按钮.clicked.connect(self.切换到日志页)
        导航布局.addWidget(self.日志按钮)

        self.设置按钮 = QPushButton("⚙\n设置")
        self.设置按钮.setFixedHeight(高度_功能按钮)
        self.设置按钮.setToolTip("软件更新（一键从 GitHub 更新）、联系作者、关于")
        self.设置按钮.clicked.connect(self.切换到设置页)
        导航布局.addWidget(self.设置按钮)

        内容布局.addWidget(导航)

        # ---- 下：网盘管理面板（新增/编辑/删除）----
        管理 = QWidget()
        管理.setObjectName("ManagePanel")
        管理布局 = QVBoxLayout(管理)
        管理布局.setContentsMargins(6, 10, 6, 10)
        管理布局.setSpacing(6)

        管理标题 = QLabel("网盘管理")
        管理标题.setObjectName("NavTitle")
        管理标题.setAlignment(Qt.AlignCenter)
        管理布局.addWidget(管理标题)

        self.新增按钮 = QPushButton("➕\n新增网盘")
        self.新增按钮.setFixedHeight(高度_管理按钮)
        self.新增按钮.setToolTip("添加网盘实例（同一家网盘可加多个账号）")
        self.新增按钮.clicked.connect(self.新增网盘)
        管理布局.addWidget(self.新增按钮)

        self.编辑按钮 = QPushButton("✏️\n编辑网盘")
        self.编辑按钮.setFixedHeight(高度_管理按钮)
        self.编辑按钮.setToolTip("修改当前网盘的名称/目录/线程数/启用状态")
        self.编辑按钮.clicked.connect(self.编辑当前网盘)
        管理布局.addWidget(self.编辑按钮)

        self.删除按钮 = QPushButton("🗑\n删除网盘")
        self.删除按钮.setFixedHeight(高度_管理按钮)
        self.删除按钮.setObjectName("DeleteButton")
        self.删除按钮.setToolTip("从配置里移除当前网盘（不动适配器目录和登录数据）")
        self.删除按钮.clicked.connect(self.删除当前网盘)
        管理布局.addWidget(self.删除按钮)

        # 多出来的高度全给导航面板（它会喂给内部的网盘列表，多露几个网盘按钮），
        # 「网盘管理」自然被顶到底部；不够高时整体上下滚动，绝不压缩按钮。
        内容布局.addWidget(导航, 1)
        内容布局.addWidget(管理)

        self.导航滚动区.setWidget(self.导航内容)
        列布局.addWidget(self.导航滚动区)
        return 列

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
            按钮 = QPushButton(f"{图标}\n{显示名 if len(显示名) <= 6 else 显示名[:5] + '…'}")
            按钮.setProperty("navCloud", True)
            按钮.setFixedHeight(高度_网盘按钮)
            按钮.setToolTip(f"{实例['名称']}（{标识}）\n{实例['路径']}")
            按钮.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
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
                self.堆叠.addWidget(self._空状态页)
            self.堆叠.setCurrentWidget(self._空状态页)
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
            self.堆叠.addWidget(self._网盘页面[标识])
        self._当前标识 = 标识
        self.堆叠.setCurrentWidget(self._网盘页面[标识])
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
            self.堆叠.addWidget(self._播放页面)
        else:
            self._播放页面.刷新网盘列表()
        self.堆叠.setCurrentWidget(self._播放页面)
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
            self.堆叠.addWidget(self._传输页面)
        else:
            self._传输页面.刷新网盘列表()
        self.堆叠.setCurrentWidget(self._传输页面)
        self._设置导航激活(self.传输按钮)
        self.当前网盘标签.setText("📤 跨网盘传输")
        self._更新管理按钮()

    def 传输页面(self) -> 传输页面:
        if self._传输页面 is None:
            self._传输页面 = 传输页面(self)
            self.堆叠.addWidget(self._传输页面)
            运行时 = getattr(self, "_AI运行时", None)
            if 运行时 is not None:
                self._传输页面.AI调度器 = 运行时.调度器
        return self._传输页面

    def 日志页(self) -> 日志页面:
        if self._日志页面 is None:
            self._日志页面 = 日志页面(self)
            self.堆叠.addWidget(self._日志页面)
        return self._日志页面

    def 切换到敏感词页(self):
        if self._敏感词页面 is None:
            self._敏感词页面 = 敏感词管理页面(self)
            self.堆叠.addWidget(self._敏感词页面)
        else:
            self._敏感词页面.刷新()
        self.堆叠.setCurrentWidget(self._敏感词页面)
        self._设置导航激活(self.敏感词按钮)
        self.当前网盘标签.setText("🔒 敏感词")
        self._更新管理按钮()

    def 切换到AI页(self):
        self._确保AI()
        if self._AI页面 is None:
            from .AI页面 import AI状态页面
            self._AI页面 = AI状态页面(self)
            self.堆叠.addWidget(self._AI页面)
        else:
            self._AI页面.刷新()
        self.堆叠.setCurrentWidget(self._AI页面)
        self._设置导航激活(self.AI按钮)
        self.当前网盘标签.setText("🤖 AI")
        self._更新管理按钮()

    def AI页面(self):
        self.切换到AI页()
        return self._AI页面

    def 切换到日志页(self):
        if self._日志页面 is None:
            self._日志页面 = 日志页面(self)
            self.堆叠.addWidget(self._日志页面)
        self.堆叠.setCurrentWidget(self._日志页面)
        self._设置导航激活(self.日志按钮)
        self.当前网盘标签.setText("📋 日志")
        self._更新管理按钮()

    def 设置页(self) -> 设置页面:
        if self._设置页面 is None:
            self._设置页面 = 设置页面(self)
            self.堆叠.addWidget(self._设置页面)
        return self._设置页面

    def 切换到设置页(self):
        self.设置页().刷新()
        self.堆叠.setCurrentWidget(self._设置页面)
        self._设置导航激活(self.设置按钮)
        self.当前网盘标签.setText("⚙ 设置")
        self._更新管理按钮()

    def _设置导航激活(self, 激活按钮):
        for 按钮 in (list(self._网盘按钮.values())
                   + [self.传输按钮, self.敏感词按钮, self.AI按钮,
                      self.日志按钮, self.设置按钮]):
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

    def 删除当前网盘(self):
        标识 = self._当前标识 or self._选择网盘("删除哪个网盘？")
        if not 标识:
            return
        实例 = next((x for x in 网盘实例列表(self.配置) if x["标识"] == 标识), None)
        if 实例 is None:
            return
        if not self._确认可打断("删除网盘"):
            return
        答案 = QMessageBox.question(
            self, "确认删除网盘",
            f"从 V8_3 配置里删除「{实例['名称']}」（{标识}）？\n\n"
            f"适配器目录与其登录数据都会原样保留：\n{实例['路径']}\n"
            "（不会删除任何网盘上的文件）",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if 答案 != QMessageBox.Yes:
            return
        try:
            删除网盘实例(self.配置, 标识)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "删除失败", str(e))
            return
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
        """把左侧栏按钮的高度重新钉死。

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
                   getattr(self, "删除按钮", None)):
            if 按钮 is not None:
                按钮.setFixedHeight(高度_管理按钮)
        for 按钮 in getattr(self, "_网盘按钮", {}).values():
            按钮.setFixedHeight(高度_网盘按钮)

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

    def closeEvent(self, 事件):
        try:
            if self._传输页面 is not None:
                self._传输页面.关闭()
        except Exception:
            pass
        for 页 in list(self._网盘页面.values()):
            try:
                页.关闭()
            except Exception:
                pass
        try:
            self.动作.关闭()
        except Exception:
            pass
        for 线程 in list(self._活动线程):
            try:
                if 线程.isRunning():
                    线程.wait(3000)
            except Exception:
                pass
        self._活动线程.clear()
        try:
            保存配置(self.配置, self.配置路径)
        except Exception:
            pass
        super().closeEvent(事件)


def 运行界面(AI运行时=None, 主题: str = "", 启动日志=None):
    import sys as _sys
    应用 = QApplication.instance() or QApplication(_sys.argv)
    if 主题:
        try:
            应用.setStyleSheet(主题管理器.获取样式表(主题))
        except Exception:
            pass
    窗口 = 主窗口(AI运行时=AI运行时, 主题=主题, 启动日志=启动日志)
    窗口.show()
    return 应用.exec()
