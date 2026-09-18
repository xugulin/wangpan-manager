"""单个网盘页面（左侧导航点一个网盘后切换到的页面）。

页面 = V8_3 的登盘管理区 + V8 主页式文件浏览页
=============================================
* 管理区（沿用 V8_3「网盘管理」页的内容，只是从标签页改成切换页）：
  登录状态、登录 / 管理（打开适配器原 GUI 的子进程窗口）、刷新状态、
  打开数据目录、打开适配器目录；
* 文件浏览区（沿用 网盘管理_V8 主页的形态）：路径栏 + 上级/根/刷新 +
  上传文件/上传文件夹/下载/跨网盘直传 + 文件表格（双击进出目录、首屏 200 项）。

所有网络调用都走 QThread（见 后台线程.py），并且用「加载世代」丢弃过期回调，
避免快速切换目录时旧结果覆盖新列表。
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from PySide6.QtCore import QFileSystemWatcher, Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QFileDialog, QGroupBox, QHBoxLayout,
    QHeaderView, QInputDialog, QLabel, QMessageBox, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from ..核心.模型 import 规范路径, 拼路径, 显示名
from ..播放.媒体信息 import 视频后缀, 是视频文件
from .后台线程 import 账号状态线程, 列目录线程, 文件操作线程
from .跨盘直传对话框 import 跨盘直传对话框

图标映射 = {
    '.pdf': '📄', '.doc': '📄', '.docx': '📄',
    '.xls': '📊', '.xlsx': '📊', '.ppt': '📑', '.pptx': '📑',
    '.jpg': '🖼️', '.jpeg': '🖼️', '.png': '🖼️', '.gif': '🖼️',
    '.mp4': '🎬', '.avi': '🎬', '.mkv': '🎬',
    '.mp3': '🎵', '.wav': '🎵', '.flac': '🎵',
    '.zip': '📦', '.rar': '📦', '.7z': '📦',
    '.py': '🐍', '.js': '📜', '.txt': '📝', '.md': '📝',
    '.json': '📋', '.xml': '📋', '.exe': '⚙️',
}


class 网盘页面(QWidget):
    首屏显示量 = 200
    双击防抖间隔 = 0.3

    def __init__(self, 主窗口, 实例: dict, 父=None):
        super().__init__(父)
        self.主窗口 = 主窗口
        self.动作 = 主窗口.动作
        self.实例 = dict(实例)
        self.标识 = self.实例["标识"]
        self.名称 = self.实例.get("名称") or self.标识
        self.当前目录 = "/"
        self._上次已登录 = False      # 用来识别"刚刚登录成功"，好自动列一次目录
        self._文件列表: list = []
        self._显示列表: list[dict] = []
        self._显示量 = self.首屏显示量
        self._加载世代 = 0
        self._上次双击 = 0.0
        self._登录后重试剩余 = 0
        self._凭证同步中 = False
        self._操作线程: list = []

        self._构建()
        self._刷新管理区(首次=True)
        self._启动凭证监视()
        self.加载当前目录()

    # ==================== 凭证监视（原 GUI 里退出登录也能同步） ====================

    #: 安全网：文件监视器偶尔收不到事件（删文件/换文件/网络盘），
    #: 再叠一个低频轮询，保证"凭证没了 → 界面收干净"最迟 10 秒内发生。
    凭证轮询毫秒 = 10000

    def _启动凭证监视(self) -> None:
        """盯住该网盘的凭证文件/数据目录：一有变化就重新核对登录态。

        用户现场反馈：在**原生 GUI** 里退出登录后，网盘管理这边列表还在、
        上传下载还能用。原因是界面从来不检查凭证是否还在。这里用
        QFileSystemWatcher 盯目录 + 文件，再叠一个 10 秒轮询兜底。
        """
        try:
            self._凭证监视 = QFileSystemWatcher(self)
        except Exception:
            self._凭证监视 = None
        self._凭证路径 = None
        if self._凭证监视 is not None:
            try:
                self._凭证监视.directoryChanged.connect(
                    lambda _p: self._凭证有变化())
                self._凭证监视.fileChanged.connect(
                    lambda _p: self._凭证有变化())
            except Exception:
                pass
        self._重挂凭证监视()
        try:
            self._凭证计时 = QTimer(self)
            self._凭证计时.setInterval(int(self.凭证轮询毫秒))
            self._凭证计时.timeout.connect(self._轮询凭证)
            self._凭证计时.start()
        except Exception:
            self._凭证计时 = None

    def _重挂凭证监视(self) -> None:
        监视 = getattr(self, "_凭证监视", None)
        if 监视 is None:
            return
        规格 = self.动作.规格(self.标识)
        if 规格 is None:
            return
        目标 = []
        try:
            目标.append(str(规格.数据目录))
        except Exception:
            pass
        try:
            目标.append(str(规格.凭证文件))
        except Exception:
            pass
        for 路径 in 目标:
            try:
                if 路径 not in 监视.directories() and 路径 not in 监视.files():
                    监视.addPath(路径)
            except Exception:
                pass
        self._凭证路径 = 目标[-1] if 目标 else None

    def _轮询凭证(self) -> None:
        """低频兜底：凭证文件在不在。只在**状态可能变化**时才动界面。"""
        try:
            规格 = self.动作.规格(self.标识)
        except Exception:
            规格 = None
        if 规格 is None:
            return
        存在 = False
        try:
            存在 = Path(规格.凭证文件).is_file()
        except Exception:
            存在 = False
        上次 = getattr(self, "_凭证存在", None)
        self._凭证存在 = 存在
        if 上次 is not None and 上次 and not 存在:
            # 凭证刚被删（多半是在原生 GUI 里退出登录了）
            self.主窗口.追加日志(
                f"[{self.名称}] 检测到登录凭证已被删除，正在同步为未登录…")
            self._重挂凭证监视()
            self._置为未登录("登录凭证已被删除")
            self._凭证有变化()
        elif 上次 is not None and not 上次 and 存在:
            self._凭证有变化()

    def _凭证有变化(self) -> None:
        """凭证文件/数据目录有变化：重挂监视（删了要重新 addPath）并核一次状态。

        加一个"同步中"闸门：监视器 + 10 秒兜底轮询 + 退出登录三路都可能触发，
        没有闸门时会叠出好几次"检查中…"，界面看着像卡住了。
        """
        self._重挂凭证监视()
        if getattr(self, "_凭证同步中", False):
            return
        self._凭证同步中 = True
        self._刷新管理区()

    # ==================== 界面 ====================

    def _构建(self):
        布局 = QVBoxLayout(self)
        布局.setContentsMargins(0, 0, 0, 0)
        布局.setSpacing(8)

        布局.addWidget(self._构建管理区())
        布局.addLayout(self._构建路径栏())
        布局.addLayout(self._构建操作栏())

        self.文件表格 = QTableWidget()
        self.文件表格.setColumnCount(5)
        self.文件表格.setHorizontalHeaderLabels(
            ["", "文件名", "大小", "修改时间", "类型"])
        表头 = self.文件表格.horizontalHeader()
        表头.setSectionResizeMode(0, QHeaderView.Fixed)
        self.文件表格.setColumnWidth(0, 32)
        表头.setSectionResizeMode(1, QHeaderView.Stretch)
        self.文件表格.setColumnWidth(2, 100)
        self.文件表格.setColumnWidth(3, 170)
        self.文件表格.setColumnWidth(4, 110)
        self.文件表格.setAlternatingRowColors(True)
        self.文件表格.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.文件表格.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.文件表格.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.文件表格.doubleClicked.connect(self._双击项目)
        self.文件表格.itemSelectionChanged.connect(self._更新按钮状态)
        布局.addWidget(self.文件表格, 1)

    def _构建管理区(self) -> QWidget:
        组 = QGroupBox(f"{self.类型中文名} · {self.名称}（{self.标识}）")
        组.setObjectName("CloudManageBox")
        布局 = QVBoxLayout(组)

        状态行 = QHBoxLayout()
        self.状态标签 = QLabel("⚪ 未检查")
        self.状态标签.setObjectName("CloudState")
        self.状态标签.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.状态标签.setWordWrap(True)
        状态行.addWidget(self.状态标签, 1)
        布局.addLayout(状态行)

        按钮行 = QHBoxLayout()
        self.登录按钮 = QPushButton("🔐 登录 / 管理")
        self.登录按钮.setObjectName("PrimaryButton")
        self.登录按钮.setToolTip(
            "一个入口管登录：扫码 / 导入Cookie / 短信 / 邮箱 / 令牌 / 账号密码"
            "（各网盘支持的方式由适配器申报，不支持的那页会置灰并说明原因）")
        self.登录按钮.clicked.connect(self._统一登录)
        按钮行.addWidget(self.登录按钮)

        self.原GUI按钮 = QPushButton("🖥 适配器原 GUI")
        self.原GUI按钮.setToolTip(
            "需要适配器自带完整界面时用（独立进程，V8_3 只读其结果）")
        self.原GUI按钮.clicked.connect(self._登录)
        按钮行.addWidget(self.原GUI按钮)

        self.刷新状态按钮 = QPushButton("🔄 刷新状态")
        self.刷新状态按钮.clicked.connect(lambda: self._刷新管理区())
        按钮行.addWidget(self.刷新状态按钮)

        # 用户要求：GUI 里要能直接「退出登录 / 重新登录」。
        # 以前只能去适配器原 GUI 里退，退了之后这边状态也不跟着变
        # （列表还在、上传下载照用），看起来像"没退成功"。
        self.退出登录按钮 = QPushButton("🚪 退出登录")
        self.退出登录按钮.setToolTip(
            "清除该网盘的本地登录凭证（数据/会话/令牌/凭证文件）"
            "并立即把本页置为未登录：清空列表、禁用上传下载")
        self.退出登录按钮.clicked.connect(self._退出登录)
        按钮行.addWidget(self.退出登录按钮)

        self.重新登录按钮 = QPushButton("🔁 重新登录")
        self.重新登录按钮.setToolTip(
            "先退出当前账号，再打开统一登录对话框重新登录（换账号用这个）")
        self.重新登录按钮.clicked.connect(self._重新登录)
        按钮行.addWidget(self.重新登录按钮)

        数据目录按钮 = QPushButton("📂 打开数据目录")
        数据目录按钮.clicked.connect(lambda: self._打开目录(self._目录("数据")))
        按钮行.addWidget(数据目录按钮)

        适配器目录按钮 = QPushButton("📁 打开适配器目录")
        适配器目录按钮.clicked.connect(lambda: self._打开目录(self._目录("项目")))
        按钮行.addWidget(适配器目录按钮)
        按钮行.addStretch(1)
        布局.addLayout(按钮行)
        return 组

    def _构建路径栏(self) -> QHBoxLayout:
        栏 = QHBoxLayout()
        栏.addWidget(QLabel("📂 路径:"))
        self.路径标签 = QLabel("/")
        self.路径标签.setObjectName("PathLabel")
        self.路径标签.setTextInteractionFlags(Qt.TextSelectableByMouse)
        栏.addWidget(self.路径标签, 1)
        self.上级按钮 = QPushButton("⬆ 上级")
        self.上级按钮.clicked.connect(self._返回上级)
        栏.addWidget(self.上级按钮)
        根按钮 = QPushButton("🏠 根")
        根按钮.clicked.connect(self._返回根)
        栏.addWidget(根按钮)
        刷新按钮 = QPushButton("🔄 刷新")
        刷新按钮.clicked.connect(self.加载当前目录)
        栏.addWidget(刷新按钮)
        return 栏

    def _构建操作栏(self) -> QHBoxLayout:
        栏 = QHBoxLayout()
        self.上传按钮 = QPushButton("📤 上传文件")
        self.上传按钮.setObjectName("SuccessButton")
        self.上传按钮.clicked.connect(self._上传文件)
        栏.addWidget(self.上传按钮)

        self.上传目录按钮 = QPushButton("📁 上传文件夹")
        self.上传目录按钮.setObjectName("WarningButton")
        self.上传目录按钮.clicked.connect(self._上传文件夹)
        栏.addWidget(self.上传目录按钮)

        self.下载按钮 = QPushButton("📥 下载")
        self.下载按钮.clicked.connect(self._下载)
        栏.addWidget(self.下载按钮)

        self.直传按钮 = QPushButton("🔀 跨网盘直传")
        self.直传按钮.setObjectName("PrimaryButton")
        self.直传按钮.clicked.connect(self._跨网盘直传)
        栏.addWidget(self.直传按钮)

        self.新建目录按钮 = QPushButton("🆕 新建文件夹")
        self.新建目录按钮.clicked.connect(self._新建文件夹)
        栏.addWidget(self.新建目录按钮)

        self.删除按钮 = QPushButton("🗑 删除")
        self.删除按钮.setObjectName("ErrorButton")
        self.删除按钮.clicked.connect(self._删除选中)
        栏.addWidget(self.删除按钮)

        栏.addStretch(1)
        return 栏

    # ==================== 规格 / 管理区 ====================

    @property
    def _规格(self):
        规格 = self.动作.规格(self.标识)
        if 规格 is None:
            raise KeyError(f"网盘未启用或已删除：{self.标识}")
        return 规格

    @property
    def 类型中文名(self) -> str:
        from ..核心.模型 import 网盘类型
        try:
            return 网盘类型.解析(self.实例.get("类型")).中文名
        except Exception:  # noqa: BLE001
            return str(self.实例.get("类型") or "网盘")

    def _目录(self, 种类: str):
        """取数据目录 / 适配器项目目录；网盘被停用时退回已保存的路径。"""
        规格 = self.动作.规格(self.标识)
        if 规格 is not None:
            return 规格.数据目录 if 种类 == "数据" else 规格.路径
        from pathlib import Path
        路径 = Path(str(self.实例.get("路径") or ".")).expanduser()
        return 路径 / "数据" if 种类 == "数据" else 路径

    def _打开目录(self, 目录):
        try:
            from pathlib import Path
            路径 = Path(目录)
            路径.mkdir(parents=True, exist_ok=True)
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(路径)))
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "打开失败", str(e))

    def _统一登录(self):
        """打开统一登录对话框（6 种方式）。"""
        from .登录对话框 import 登录对话框
        if self.动作.规格(self.标识) is None:
            QMessageBox.warning(self, "未启用", "该网盘当前未启用，请先「编辑网盘」启用")
            return
        try:
            对话框 = 登录对话框(self.主窗口, self.实例, self)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "登录入口不可用", str(e))
            return
        if 对话框.exec() == QDialog.Accepted and 对话框.成功:
            self.主窗口.追加日志(f"[{self.名称}] 登录成功（统一登录）")
        self._刷新管理区()

    def _登录(self):
        """主路径：打开该网盘适配器原 GUI（登录 / 换账号 / 账号管理都在里面）。"""
        规格 = self.动作.规格(self.标识)
        if 规格 is None:
            QMessageBox.warning(self, "未配置", f"网盘不存在：{self.标识}")
            return
        try:
            规格.启动适配器GUI()
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "启动失败", str(e))
            return
        self.主窗口.追加日志(
            f"已启动「{规格.显示名}」原 GUI（独立进程）：{规格.启动脚本()}；"
            "登录完成后回到这里点「🔄 刷新状态」。")

    def 刷新管理区(self):
        self._刷新管理区()

    # ==================== 退出登录 / 重新登录 ====================

    def _退出登录(self, 静默: bool = False) -> bool:
        """清掉本地凭证，并**立刻**把本页置为未登录。

        用户现场反馈（原生 GUI 里退出登录后）：网盘管理这边状态不对 ——
        列表还在、上传下载照样能用，只是左边绿点变了。根因是界面从来没在
        "凭证被清掉"时同步过自己。现在退出登录由本页主导：清凭证 →
        清列表 → 禁写 → 刷新左侧绿点，一步不落。
        """
        if not 静默:
            回答 = QMessageBox.question(
                self, "退出登录",
                f"确定要退出「{self.名称}」吗？\n\n"
                "会删除该网盘的本地登录凭证（会话/令牌/Cookie 文件），"
                "下次需要重新登录。云端文件不受影响。",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if 回答 != QMessageBox.Yes:
                return False
        try:
            适配器 = self.动作.适配器(self.标识)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "不可用", f"适配器不可用：{error短(str(e))}")
            return False
        线程 = 文件操作线程(
            "退出登录", lambda 进度: dict(适配器.退出登录() or {}), self)

        def 完成(_描述: str, 结果: dict):
            if str((结果 or {}).get("状态")) == "成功":
                self.主窗口.追加日志(
                    f"[{self.名称}] ✅ {(结果 or {}).get('消息', '已退出登录')}")
                self._置为未登录((结果 or {}).get("消息") or "已退出登录")
            else:
                提示 = (结果 or {}).get("提示") or ""
                QMessageBox.warning(
                    self, "退出登录失败",
                    f"{(结果 or {}).get('消息', '退出登录失败')}\n{提示}")

        def 失败(_描述: str, 错误: str):
            QMessageBox.warning(self, "退出登录失败", error短(错误, 400))

        线程.完成.connect(完成)
        线程.失败.connect(失败)
        self._操作线程.append(线程)
        线程.finished.connect(lambda t=线程: self._清理线程(t))
        线程.start()
        return True

    def 置为未登录(self, 原因: str = "") -> None:
        """外部（主窗口/监视器）发现凭证没了时调这个，状态与界面一起收干净。"""
        self._置为未登录(原因)

    def _置为未登录(self, 原因: str = "") -> None:
        self._上次已登录 = False
        self._登录后重试剩余 = 0
        self._文件列表 = []
        self._显示列表 = []
        self._渲染表格()
        self._更新写按钮(False)
        文字 = "⚪ 未登录"
        if 原因:
            文字 += f"（{原因}）"
        self.状态标签.setText(文字)
        try:
            self.主窗口.设置网盘状态(self.标识, False)
        except Exception:
            pass

    def _更新写按钮(self, 可写: bool) -> None:
        """未登录时把"需要权限"的按钮置灰（用户要求：清列表 + 禁写）。"""
        for 按钮 in (getattr(self, "上传按钮", None),
                    getattr(self, "上传目录按钮", None),
                    getattr(self, "下载按钮", None),
                    getattr(self, "新建目录按钮", None),
                    getattr(self, "删除按钮", None)):
            if 按钮 is None:
                continue
            try:
                按钮.setEnabled(bool(可写))
            except Exception:
                pass
        for 按钮, 提示 in ((getattr(self, "上传按钮", None),
                        "未登录：先点「🔐 登录 / 管理」"),
                       (getattr(self, "下载按钮", None),
                        "未登录：先点「🔐 登录 / 管理」")):
            if 按钮 is not None:
                try:
                    if not 可写:
                        按钮.setToolTip(提示)
                except Exception:
                    pass

    def _重新登录(self):
        """先退出当前账号，再打开统一登录（换账号用）。"""
        if not self._退出登录():
            return
        self.主窗口.追加日志(f"[{self.名称}] 已请求重新登录…")
        QTimer.singleShot(800, self._统一登录)

    #: 登录后"立即列目录"允许的重试次数：桥进程里那份会话仓库要重读一次盘，
    #: 偶尔第一枪会撞上未登录（errno:-6）。重试一次即可稳定出列表，
    #: 比让用户自己点「🔄 刷新」快得多。
    登录后列目录重试 = 2

    def 登录后立即加载(self, 路径: str | None = None) -> None:
        """登录成功 → **马上去列目录**，不再等账号状态查询回来。

        用户反馈：「网盘管理的 GUI 登录后要等很久才能列出文件，没有原 GUI 快」。
        原因是列表要等 `账号状态()`（取模板变量 + 取用户信息，慢时十几秒）回来
        才在 `_状态成功` 里补一次列目录。现在两者并行：**列表先出，状态栏后到**。
        """
        self._上次已登录 = True
        self._登录后重试剩余 = int(self.登录后列目录重试)
        self.加载当前目录(路径)

    def _刷新管理区(self, 首次: bool = False):
        if self.动作.规格(self.标识) is None:
            self.状态标签.setText("⚪ 该网盘未启用（在左下角「编辑网盘」里启用）")
            return
        self.状态标签.setText("⏳ 检查中…")
        线程 = 账号状态线程(self.标识, self.动作.适配器, self)
        线程.成功.connect(self._状态成功)
        线程.失败.connect(self._状态失败)
        self._操作线程.append(线程)
        线程.finished.connect(lambda t=线程: self._清理线程(t))
        线程.start()

    def _状态成功(self, 标识: str, 信息: dict):
        if 标识 != self.标识:
            return
        self._凭证同步中 = False
        登录 = "🟢 已登录" if 信息.get("logged_in") else "⚪ 未登录"
        用户 = 信息.get("user") or "-"
        详情 = 信息.get("detail") or {}
        容量 = ""
        会员 = 详情.get("member") if isinstance(详情.get("member"), dict) else {}
        for 键, 标签 in (("total_capacity", "总容量"), ("use_capacity", "已用")):
            if 键 in 会员:
                容量 += f"，{标签}={self._格式化大小(会员[键])}"
        警告 = ""
        if 详情.get("write_error"):
            # 写权限探针（不产生副作用）给了明确原因：直接摆出来，
            # 用户才知道"能列目录但上传/改名/删除全失败"到底该怎么办。
            警告 = f"；⚠️ 写操作不可用：{详情['write_error']}"
        elif 详情.get("refresh_error") or 详情.get("user_error"):
            警告 = "；⚠️ 读正常，写操作可能需要重新登录"
        # 只报登录态/账号/容量/警告：数据目录不在这里堆（要看/要开有下面那排按钮），
        # 也免得把本机绝对路径一直摆在界面上。
        self.状态标签.setText(f"{登录}，用户：{用户}{容量}{警告}")
        可写 = bool(详情.get("write_error") is None)
        self._更新写按钮(bool(信息.get("logged_in")) and 可写)
        if 登录.startswith("🟢"):
            self.主窗口.设置网盘状态(self.标识, True)
            # 刚登录成功、或列表还是空的：自动列一次目录 ——
            # 用户反馈"登录成功后文件列表不会自动加载，得手动刷新"。
            刚登录 = not self._上次已登录
            self._上次已登录 = True
            if 刚登录 or self.文件表格.rowCount() == 0:
                self.加载当前目录()
        else:
            # 凭证没了（在原生 GUI 里退出登录 / 会话被清空）：
            # 之前这里只把标志位归零，列表和上传下载全都留着，用户看到的是
            # "明明退了还在、还能传"。现在一并收干净。
            if self._上次已登录 or self.文件表格.rowCount():
                原因 = "登录凭证已失效或已被清除，请重新登录"
                if isinstance(详情, dict) and 详情.get("no_credential"):
                    原因 = "本地登录凭证已不存在"
                self._置为未登录(原因)
            else:
                self._上次已登录 = False
                self._文件列表 = []
                self._显示列表 = []
                self._渲染表格()
                self._更新写按钮(False)

    def _状态失败(self, 标识: str, 错误: str):
        if 标识 != self.标识:
            return
        self._凭证同步中 = False
        self.状态标签.setText(f"🔴 状态获取失败：{error短(错误)}")
        self.主窗口.追加日志(f"[{self.名称}] 状态获取失败：{错误}")

    def _清理线程(self, 线程):
        try:
            self._操作线程.remove(线程)
        except ValueError:
            pass
        线程.deleteLater()

    # ==================== 目录浏览 ====================

    def 加载当前目录(self, 目录: str | None = None):
        if 目录 is not None:
            self.当前目录 = 规范路径(目录)
        self._加载世代 += 1
        世代 = self._加载世代
        路径 = self.当前目录
        self.路径标签.setText(路径)
        self.文件表格.setEnabled(False)
        self.主窗口.状态消息(f"[{self.名称}] 正在加载 {路径} …")
        try:
            适配器 = self.动作.适配器(self.标识)
        except Exception as e:  # noqa: BLE001
            self.文件表格.setEnabled(True)
            self.主窗口.状态消息(f"[{self.名称}] 适配器不可用：{error短(str(e))}")
            return
        线程 = 列目录线程(self.标识, 适配器, 路径, self)
        线程.成功.connect(lambda i, p, 条目, g=世代: self._加载成功(g, p, 条目))
        线程.失败.connect(lambda i, p, e, g=世代: self._加载失败(g, p, e))
        self._操作线程.append(线程)
        线程.finished.connect(lambda t=线程: self._清理线程(t))
        线程.start()

    def _加载成功(self, 世代: int, 路径: str, 条目):
        if 世代 != self._加载世代 or 路径 != self.当前目录:
            return
        self.文件表格.setEnabled(True)
        self._登录后重试剩余 = 0
        self._文件列表 = list(条目)
        self._构造显示列表()
        self._渲染表格()
        self.主窗口.状态消息(f"[{self.名称}] {self.当前目录}：{len(self._显示列表)} 项")

    def _加载失败(self, 世代: int, 路径: str, 错误: str):
        if 世代 != self._加载世代:
            return
        self.文件表格.setEnabled(True)
        # 刚登录完的第一次列目录偶尔会撞上"桥进程还没重读会话"（errno:-6）：
        # 自动再试一次，而不是让用户看着空列表自己点刷新。
        剩余 = int(getattr(self, "_登录后重试剩余", 0) or 0)
        if 剩余 > 0 and 路径 == self.当前目录:
            self._登录后重试剩余 = 剩余 - 1
            self.主窗口.状态消息(
                f"[{self.名称}] {路径} 第一次没列出来（{error短(错误, 60)}），正在重试…")
            QTimer.singleShot(600, self.加载当前目录)
            return
        self.主窗口.状态消息(f"[{self.名称}] 加载失败：{error短(错误)}")
        self.主窗口.追加日志(f"[{self.名称}] 列目录失败 {路径}：{错误}")

    def _构造显示列表(self):
        self._显示列表 = []
        if self.当前目录 != "/":
            self._显示列表.append({
                "name": "..", "is_dir": True, "size": 0,
                "modified": "", "is_parent": True, "path": self._父目录(),
            })
        for 项 in sorted(self._文件列表, key=lambda x: (not x.is_dir, x.name.lower())):
            self._显示列表.append({
                "name": 项.name, "is_dir": bool(项.is_dir),
                "size": int(项.size or 0), "modified": 项.modified or "",
                "path": 项.path, "is_parent": False,
            })
        self._显示量 = min(self.首屏显示量, len(self._显示列表))

    def _渲染表格(self):
        数量 = min(self._显示量, len(self._显示列表))
        表格 = self.文件表格
        表格.setUpdatesEnabled(False)
        try:
            表格.setRowCount(数量)
            for i, 文件 in enumerate(self._显示列表[:数量]):
                self._设置行(i, 文件)
        finally:
            表格.setUpdatesEnabled(True)
        self._更新按钮状态()

    def _设置行(self, 行: int, 文件: dict):
        是否目录 = bool(文件.get("is_dir"))
        是否返回 = bool(文件.get("is_parent"))
        名称 = 文件.get("name") or "-"
        if 是否返回:
            显示 = "📁 .."
        elif 是否目录:
            显示 = f"📁 {名称}"
        else:
            图标 = 图标映射.get(os.path.splitext(名称)[1].lower(), "📄")
            显示 = f"{图标} {名称}"
        值 = [
            "",
            显示,
            "-" if 是否返回 else ("文件夹" if 是否目录
                              else self._格式化大小(文件.get("size"))),
            "-" if 是否返回 else (文件.get("modified") or "-"),
            "📁 文件夹" if 是否目录 else "📄 文件",
        ]
        for 列, 文本 in enumerate(值):
            项 = self.文件表格.item(行, 列)
            if 项 is None:
                项 = QTableWidgetItem()
                if 列 == 0:
                    项.setFlags(Qt.ItemIsEnabled)
                self.文件表格.setItem(行, 列, 项)
            if 项.text() != 文本:
                项.setText(文本)

    def _父目录(self) -> str:
        if self.当前目录 == "/":
            return "/"
        return 规范路径(self.当前目录.rsplit("/", 1)[0] or "/")

    def _双击项目(self, 索引):
        现在 = time.time()
        if 现在 - self._上次双击 < self.双击防抖间隔:
            return
        self._上次双击 = 现在
        行 = 索引.row()
        if 行 < 0 or 行 >= len(self._显示列表):
            return
        文件 = self._显示列表[行]
        if 文件.get("is_parent"):
            self._返回上级()
        elif 文件.get("is_dir"):
            self.加载当前目录(拼路径(self.当前目录, 文件["name"]))
        elif 是视频文件(文件.get("name", "")):
            # 视频双击直接播（其余文件类型双击仍然是"打开/下载"的老行为）
            self.播放这个视频(文件)

    def 播放这个视频(self, 文件: dict) -> None:
        """双击视频：切到播放页并开播（Ctrl/⌘+双击 = 独立窗口播）。"""
        路径 = 拼路径(self.当前目录, 文件.get("name", ""))
        from PySide6.QtWidgets import QApplication as _App
        修饰 = _App.keyboardModifiers()
        独立 = bool(修饰 & (Qt.ControlModifier | Qt.MetaModifier))
        try:
            self.主窗口.播放网盘视频(self.标识, 路径, 独立窗口=独立)
        except Exception as e:  # noqa: BLE001
            self.主窗口.追加日志(f"❌ 播放失败：{e}", "错误")

    def _返回上级(self):
        if self.当前目录 != "/":
            self.加载当前目录(self._父目录())

    def _返回根(self):
        self.加载当前目录("/")

    def _选中文件(self) -> list[dict]:
        结果 = []
        for 索引 in self.文件表格.selectionModel().selectedRows():
            if 0 <= 索引.row() < len(self._显示列表):
                文件 = self._显示列表[索引.row()]
                if not 文件.get("is_parent"):
                    结果.append(文件)
        return 结果

    def _更新按钮状态(self):
        有选中 = bool(self._选中文件())
        for 按钮 in (getattr(self, "下载按钮", None),
                    getattr(self, "删除按钮", None)):
            if 按钮 is not None:
                按钮.setEnabled(有选中)

    def 选中路径(self) -> str:
        """当前页面里选中的第一个文件/目录的完整逻辑路径。"""
        选中 = self._选中文件()
        if not 选中:
            return ""
        return 选中[0]["path"]

    # ==================== 操作 ====================

    def _上传文件(self):
        路径列表, _ = QFileDialog.getOpenFileNames(self, "选择要上传的文件")
        if not 路径列表:
            return
        传输 = self.主窗口.传输页面()
        for 路径 in 路径列表:
            传输.添加上传任务(self.标识, 路径, self.当前目录)
        self.主窗口.切换到传输页()
        self.主窗口.追加日志(
            f"[{self.名称}] 已加入 {len(路径列表)} 个上传任务 → {self.当前目录}")

    def _上传文件夹(self):
        目录 = QFileDialog.getExistingDirectory(self, "选择要上传的文件夹")
        if not 目录:
            return
        传输 = self.主窗口.传输页面()
        传输.添加上传任务(self.标识, 目录, self.当前目录)
        self.主窗口.切换到传输页()
        self.主窗口.追加日志(
            f"[{self.名称}] 已加入文件夹上传任务：{os.path.basename(目录)} "
            f"→ {self.当前目录}")

    def _下载(self):
        选中 = self._选中文件()
        if not 选中:
            QMessageBox.information(self, "提示", "请先选择要下载的文件")
            return
        保存目录 = QFileDialog.getExistingDirectory(self, "选择保存目录")
        if not 保存目录:
            return
        传输 = self.主窗口.传输页面()
        数量 = 0
        for 文件 in 选中:
            if 文件.get("is_dir"):
                continue
            传输.添加下载任务(self.标识, 文件["path"],
                            os.path.join(保存目录, 文件["name"]),
                            大小=int(文件.get("size") or 0))
            数量 += 1
        if not 数量:
            QMessageBox.information(self, "提示", "选中的都是文件夹，暂不支持整目录下载")
            return
        self.主窗口.切换到传输页()
        self.主窗口.追加日志(f"[{self.名称}] 已加入 {数量} 个下载任务 → {保存目录}")

    def _跨网盘直传(self):
        try:
            对话框 = 跨盘直传对话框(
                self.主窗口, 默认源=self.标识, 默认路径=self.当前目录)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "无法直传", str(e))
            return
        选中 = self.选中路径()
        if 选中:
            对话框.源路径.setText(选中)
        if 对话框.exec() != QDialog.Accepted or not 对话框.结果:
            return
        源标识, 源路径, 目标标识, 目标路径 = 对话框.结果
        传输 = self.主窗口.传输页面()
        传输.添加跨网盘传输(源标识, 源路径, 目标标识, 目标路径)
        self.主窗口.切换到传输页()

    def _新建文件夹(self):
        名称, ok = QInputDialog.getText(self, "新建文件夹", "文件夹名：")
        if not ok or not 名称.strip():
            return
        路径 = 拼路径(self.当前目录, 名称.strip())

        def 动作(进度):
            return self.动作.适配器(self.标识).确保目录(路径)

        线程 = 文件操作线程(f"新建文件夹 {名称.strip()}", 动作, self)
        线程.完成.connect(lambda d, r: (
            self.主窗口.状态消息(f"[{self.名称}] 已创建 {路径}"),
            self.主窗口.追加日志(f"[{self.名称}] 已创建目录 {路径}"),
            self.加载当前目录()))
        线程.失败.connect(lambda d, e: QMessageBox.warning(self, "新建失败", e))
        self._操作线程.append(线程)
        线程.finished.connect(lambda t=线程: self._清理线程(t))
        线程.start()

    def _删除选中(self):
        选中 = self._选中文件()
        if not 选中:
            return
        名字 = "、".join(x["name"] for x in 选中[:5])
        if len(选中) > 5:
            名字 += f" 等 {len(选中)} 项"
        答案 = QMessageBox.question(
            self, "确认删除",
            f"确定删除「{名字}」吗？\n（网盘端删除通常不可恢复，会进回收站）",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if 答案 != QMessageBox.Yes:
            return
        路径们 = [x["path"] for x in 选中]

        def 动作(进度):
            适配器 = self.动作.适配器(self.标识)
            失败: list[str] = []
            for 路径 in 路径们:
                进度("删除", 0, 0)
                try:
                    适配器.删除(路径)
                except Exception as e:  # noqa: BLE001
                    失败.append(f"{路径}: {e}")
            if 失败:
                raise RuntimeError("；".join(失败))
            return {"删除": len(路径们)}

        线程 = 文件操作线程(f"删除 {len(路径们)} 项", 动作, self)
        线程.完成.connect(lambda d, r: (
            self.主窗口.状态消息(f"[{self.名称}] 已删除 {len(路径们)} 项"),
            self.主窗口.追加日志(f"[{self.名称}] 已删除：{名字}"),
            self.加载当前目录()))
        线程.失败.connect(lambda d, e: QMessageBox.warning(self, "删除失败", e))
        self._操作线程.append(线程)
        线程.finished.connect(lambda t=线程: self._清理线程(t))
        线程.start()

    # ==================== 工具 ====================

    @staticmethod
    def _格式化大小(n) -> str:
        try:
            n = int(n or 0)
        except (TypeError, ValueError):
            return "0 B"
        if n < 1024:
            return f"{n} B"
        n /= 1024
        if n < 1024:
            return f"{n:.1f} KB"
        n /= 1024
        if n < 1024:
            return f"{n:.1f} MB"
        return f"{n / 1024:.2f} GB"

    def 关闭(self):
        self._加载世代 += 1
        for 线程 in list(self._操作线程):
            try:
                线程.wait(2000)
            except Exception:
                pass
            self._清理线程(线程)


def error短(文本: str, 长度: int = 300) -> str:
    return str(文本 or "")[:长度]
