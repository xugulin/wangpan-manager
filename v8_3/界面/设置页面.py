"""设置页：一键更新 + 联系作者 + 关于。

从左侧导航栏的「⚙ 设置」进入，三块内容：

1. **软件更新** —— 查 GitHub Releases、比版本、下载当前平台对应的包，
   再交给独立脚本套用（等本程序退出后把新版本**合并**覆盖上去，
   用户的 配置.json / 数据 / 网盘登录凭证一个都不动）；
2. **联系作者** —— QQ、邮箱、GitHub（都能一键复制）；
3. **关于** —— 版本、技术栈（Python + PySide6）、绿色版说明与环境信息，
   求助时可以直接「复制环境信息」贴给对方。

联网逻辑全在 :mod:`v8_3.更新` 里（纯函数、可单测），这里只负责界面与线程。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from PySide6.QtCore import Qt, QThread, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QMessageBox, QProgressBar, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from .. import 更新
from .后台线程 import 线程池管理器


class 不压缩内容(QWidget):
    """滚动区内容：最小高度取 sizeHint，控件保持设计高度（与 AI 页同一套做法）。"""

    def minimumSizeHint(self):  # noqa: N802 - Qt 命名
        return self.sizeHint()


class 更新检查线程(QThread):
    成功 = Signal(dict)
    失败 = Signal(str)

    def run(self):
        try:
            self.成功.emit(更新.检查最新发布())
        except Exception as e:  # noqa: BLE001 - 界面要展示任何失败
            self.失败.emit(str(e))


class 更新下载线程(QThread):
    进度 = Signal(int, int)
    成功 = Signal(str)
    失败 = Signal(str)

    def __init__(self, 资源: dict, 目录: Path, 父=None):
        super().__init__(父)
        self.资源 = dict(资源)
        self.目录 = Path(目录)

    def run(self):
        try:
            路径 = 更新.下载资源(
                self.资源, self.目录,
                进度=lambda 已下, 总数: self.进度.emit(int(已下), int(总数)))
            self.成功.emit(str(路径))
        except Exception as e:  # noqa: BLE001
            self.失败.emit(str(e))


def _大小文本(字节: int) -> str:
    字节 = max(0, int(字节))
    for 单位 in ("B", "KB", "MB", "GB"):
        if 字节 < 1024 or 单位 == "GB":
            return f"{字节:.1f} {单位}" if 单位 != "B" else f"{字节} B"
        字节 /= 1024.0
    return f"{字节:.1f} GB"


class 设置页面(QWidget):
    def __init__(self, 主窗口, 父=None):
        super().__init__(父)
        self.主窗口 = 主窗口
        self._登记线程 = 线程池管理器(主窗口)
        self._最新发布: dict | None = None
        self._选中的资源: dict | None = None
        self._下载好的包: Path | None = None
        self._构建()
        self.刷新()

    # ==================== 便捷 ====================

    @property
    def 项目根(self) -> Path:
        return Path(__file__).resolve().parents[2]

    def _记日志(self, 文本: str, 级别: str = "信息"):
        try:
            self.主窗口.追加日志(文本, 级别)
        except Exception:  # noqa: BLE001
            pass

    # ==================== 界面 ====================

    def _构建(self):
        外层 = QVBoxLayout(self)
        外层.setContentsMargins(0, 0, 0, 0)
        外层.setSpacing(0)
        self.页面滚动区 = QScrollArea()
        self.页面滚动区.setObjectName("PageScroll")
        self.页面滚动区.setWidgetResizable(True)
        self.页面滚动区.setFrameShape(QScrollArea.NoFrame)
        self.页面滚动区.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.页面滚动区.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.页面滚动区.viewport().setAutoFillBackground(False)
        self.页面内容 = 不压缩内容()
        self.页面内容.setObjectName("PageScroll")
        外层.addWidget(self.页面滚动区)

        布局 = QVBoxLayout(self.页面内容)
        布局.setContentsMargins(0, 0, 0, 0)
        布局.setSpacing(10)

        顶部 = QHBoxLayout()
        标题 = QLabel("⚙ 设置")
        标题.setStyleSheet("font-size: 18px; font-weight: bold;")
        顶部.addWidget(标题)
        顶部.addStretch(1)
        布局.addLayout(顶部)

        布局.addWidget(self._建更新区())
        布局.addWidget(self._建联系区())
        布局.addWidget(self._建关于区())
        布局.addStretch(0)

        self.页面滚动区.setWidget(self.页面内容)

    # ---- 软件更新 ----

    def _建更新区(self) -> QWidget:
        组 = QGroupBox("🔄 软件更新（一键从 GitHub 更新）")
        外层 = QVBoxLayout(组)

        self.版本标签 = QLabel()
        self.版本标签.setStyleSheet("font-size: 14px; font-weight: bold;")
        外层.addWidget(self.版本标签)

        self.更新状态标签 = QLabel("点「🔍 检查更新」看看有没有新版本。")
        self.更新状态标签.setWordWrap(True)
        self.更新状态标签.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.更新状态标签.setStyleSheet(
            "font-size: 12px; padding: 8px 12px; border-radius: 4px;"
            "background: #2c3e50; color: #ecf0f1;")
        外层.addWidget(self.更新状态标签)

        self.进度条 = QProgressBar()
        self.进度条.setRange(0, 100)
        self.进度条.setValue(0)
        self.进度条.setTextVisible(True)
        self.进度条.setVisible(False)
        外层.addWidget(self.进度条)

        行 = QHBoxLayout()
        self.检查按钮 = QPushButton("🔍 检查更新")
        self.检查按钮.setObjectName("PrimaryButton")
        self.检查按钮.clicked.connect(self.检查更新)
        行.addWidget(self.检查按钮)

        self.下载按钮 = QPushButton("⬇ 下载并更新")
        self.下载按钮.setEnabled(False)
        self.下载按钮.setToolTip("下载当前系统对应的发布包，然后自动套用并重启")
        self.下载按钮.clicked.connect(self.下载并更新)
        行.addWidget(self.下载按钮)

        self.打开发布页按钮 = QPushButton("🌐 打开发布页")
        self.打开发布页按钮.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(更新.发布页)))
        行.addWidget(self.打开发布页按钮)

        self.打开更新目录按钮 = QPushButton("📂 打开下载目录")
        self.打开更新目录按钮.clicked.connect(self._打开更新目录)
        行.addWidget(self.打开更新目录按钮)
        行.addStretch(1)
        外层.addLayout(行)

        self.更新说明标签 = QLabel(
            f"更新地址：{更新.仓库地址}/releases（只从你自己的仓库取包）\n"
            f"{更新.保留说明}。")
        self.更新说明标签.setWordWrap(True)
        self.更新说明标签.setStyleSheet("font-size: 11px; color: #95a5a6;")
        外层.addWidget(self.更新说明标签)
        return 组

    # ---- 联系方式 ----

    def _建联系区(self) -> QWidget:
        组 = QGroupBox("📇 联系作者")
        表单 = QFormLayout(组)

        表单.addRow("QQ：", self._一行联系(
            更新.QQ, f"QQ {更新.QQ}（点右边复制，加好友请说明来意）"))
        表单.addRow("邮箱：", self._一行联系(更新.邮箱, f"邮箱 {更新.邮箱}"))
        表单.addRow("GitHub：", self._一行链接(
            更新.仓库拥有人, 更新.仓库地址,
            f"https://github.com/{更新.仓库拥有人}"))
        表单.addRow("项目主页：", self._一行链接(
            更新.仓库, 更新.仓库地址, 更新.发布页))

        提示 = QLabel("用着有问题、想加网盘、想提需求，都可以直接找我。")
        提示.setWordWrap(True)
        提示.setStyleSheet("font-size: 11px; color: #95a5a6;")
        表单.addRow("", 提示)
        return 组

    def _一行联系(self, 值: str, 提示: str = "") -> QWidget:
        行 = QWidget()
        布局 = QHBoxLayout(行)
        布局.setContentsMargins(0, 0, 0, 0)
        框 = QLineEdit(值)
        框.setReadOnly(True)
        框.setToolTip(提示 or 值)
        布局.addWidget(框, 1)
        复制 = QPushButton("📋 复制")
        复制.clicked.connect(lambda: self._复制(值))
        布局.addWidget(复制)
        return 行

    def _一行链接(self, 显示: str, 地址: str, 打开: str) -> QWidget:
        行 = QWidget()
        布局 = QHBoxLayout(行)
        布局.setContentsMargins(0, 0, 0, 0)
        框 = QLineEdit(显示)
        框.setReadOnly(True)
        框.setToolTip(地址)
        布局.addWidget(框, 1)
        复制 = QPushButton("📋 复制")
        复制.clicked.connect(lambda: self._复制(地址))
        布局.addWidget(复制)
        前往 = QPushButton("🌐 打开")
        前往.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(打开)))
        布局.addWidget(前往)
        return 行

    # ---- 关于 ----

    def _建关于区(self) -> QWidget:
        组 = QGroupBox("ℹ️ 关于")
        表单 = QFormLayout(组)

        版本 = QLabel(f"网盘管理 {更新.版本显示()}（绿色版 · 解压即用）")
        版本.setStyleSheet("font-weight: bold;")
        表单.addRow("版本：", 版本)
        表单.addRow("技术栈：", QLabel("Python 3.14 + PySide6（Qt 6）桌面界面"))

        说明 = QLabel(
            "绿色无污染：解释器、依赖、AI 模型全都在项目文件夹里，"
            "不写注册表、不装系统包、不碰 ~/.config 与 ~/.cache；"
            "整个文件夹复制/改名/搬到别的电脑都能直接用，不想要了直接删文件夹。")
        说明.setWordWrap(True)
        表单.addRow("绿色版：", 说明)

        self.环境框 = QLineEdit()
        self.环境框.setReadOnly(True)
        表单.addRow("环境信息：", self.环境框)

        行 = QWidget()
        布局 = QHBoxLayout(行)
        布局.setContentsMargins(0, 0, 0, 0)
        复制环境 = QPushButton("📋 复制环境信息")
        复制环境.setToolTip("出问题时把这段贴给我，能省很多来回")
        复制环境.clicked.connect(lambda: self._复制(self._环境信息()))
        布局.addWidget(复制环境)
        布局.addStretch(1)
        表单.addRow("", 行)
        return 组

    def _环境信息(self) -> str:
        try:
            from ..自举 import 项目解释器
            解释器 = str(项目解释器)
        except Exception:  # noqa: BLE001
            解释器 = sys.executable
        return (f"网盘管理 {更新.版本显示()} | {更新.自我说明()} | "
                f"项目根 {self.项目根} | 解释器 {解释器} | "
                f"配置文件 {getattr(self.主窗口, '配置路径', '')}")

    # ==================== 行为 ====================

    def _复制(self, 文本: str):
        try:
            QApplication.clipboard().setText(str(文本))
            self.更新状态标签.setText(f"📋 已复制：{文本}")
        except Exception as e:  # noqa: BLE001
            self.更新状态标签.setText(f"复制失败：{e}")

    def _打开更新目录(self):
        目录 = 更新.更新目录(self.项目根)
        try:
            目录.mkdir(parents=True, exist_ok=True)
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(目录)))
        except Exception as e:  # noqa: BLE001
            self.更新状态标签.setText(f"打不开目录：{e}")

    def _本地是否完整版(self) -> bool:
        """看本地有没有那个 AI 语音模型，决定默认下"完整版"还是"精简版"。"""
        try:
            from ..播放.语音识别 import 默认模型目录, 默认模型大小
            return (默认模型目录 / f"faster-whisper-{默认模型大小}" / "model.bin").is_file()
        except Exception:  # noqa: BLE001
            return False

    def 刷新(self):
        self.版本标签.setText(f"当前版本：{更新.版本显示()}")
        try:
            self.环境框.setText(self._环境信息())
        except Exception:  # noqa: BLE001
            pass

    def 检查更新(self):
        self.检查按钮.setEnabled(False)
        self.检查按钮.setText("⏳ 检查中…")
        self.更新状态标签.setText(f"正在查询 {更新.仓库} 的最新发布…")
        self._最新发布 = None
        self._选中的资源 = None
        self.下载按钮.setEnabled(False)

        线程 = 更新检查线程(self)
        线程.成功.connect(self._检查成功)
        线程.失败.connect(self._检查失败)
        self._登记线程(线程, self._检查收尾)
        线程.start()

    def _检查收尾(self):
        self.检查按钮.setEnabled(True)
        self.检查按钮.setText("🔍 检查更新")

    def _检查成功(self, 发布: dict):
        self._最新发布 = dict(发布)
        远端 = str(发布.get("版本") or "")
        本地 = 更新.当前版本()
        if not 更新.有新版(远端, 本地):
            self.更新状态标签.setText(
                f"✅ 已是最新版本（{更新.版本显示(本地)}）。"
                f"最新发布：{更新.版本显示(远端)}")
            self.下载按钮.setEnabled(False)
            return
        完整 = self._本地是否完整版()
        资源 = 更新.选择资源(list(发布.get("资源") or []), 要完整版=完整)
        self._选中的资源 = dict(资源) if 资源 else None
        名字 = (资源 or {}).get("name") or "（这个平台还没有对应的包）"
        说明 = str(发布.get("说明") or "").strip()
        说明 = (说明[:400] + "…") if len(说明) > 400 else 说明
        self.更新状态标签.setText(
            f"🎉 发现新版本 {更新.版本显示(远端)}（当前 {更新.版本显示(本地)}）\n"
            f"将下载：{名字}\n"
            f"大小：{_大小文本(int((资源 or {}).get('size') or 0))}"
            f"｜{'完整版（含 AI 语音模型）' if 完整 else '精简版（不含模型）'}"
            + (f"\n更新说明：{说明}" if 说明 else ""))
        self.下载按钮.setEnabled(self._选中的资源 is not None)
        self._记日志(f"发现新版本 {更新.版本显示(远端)}（当前 {更新.版本显示(本地)}）")

    def _检查失败(self, 错误: str):
        self.更新状态标签.setText(
            f"❌ 检查更新失败：{错误}\n"
            f"（网络不通/被墙时可以直接去 {更新.发布页} 手动下载）")
        self._记日志(f"检查更新失败：{错误}", "警告")

    def 下载并更新(self):
        if not self._选中的资源:
            QMessageBox.information(self, "提示", "先点「🔍 检查更新」")
            return
        目录 = 更新.更新目录(self.项目根) / "下载"
        try:
            目录.mkdir(parents=True, exist_ok=True)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "下载失败", f"建不了下载目录：{e}")
            return
        self.下载按钮.setEnabled(False)
        self.检查按钮.setEnabled(False)
        self.进度条.setVisible(True)
        self.进度条.setValue(0)
        self.更新状态标签.setText(f"⬇ 正在下载 {self._选中的资源.get('name')} …")

        线程 = 更新下载线程(self._选中的资源, 目录, self)
        线程.进度.connect(self._下载进度)
        线程.成功.connect(self._下载成功)
        线程.失败.connect(self._下载失败)
        self._登记线程(线程, self._下载收尾)
        线程.start()

    def _下载收尾(self):
        self.检查按钮.setEnabled(True)
        self.进度条.setVisible(False)

    def _下载进度(self, 已下: int, 总数: int):
        if 总数 > 0:
            百分比 = int(已下 * 100 / 总数)
            self.进度条.setValue(百分比)
            self.更新状态标签.setText(
                f"⬇ 下载中… {_大小文本(已下)} / {_大小文本(总数)}（{百分比}%）")
        else:
            self.更新状态标签.setText(f"⬇ 下载中… {_大小文本(已下)}")

    def _下载失败(self, 错误: str):
        self.下载按钮.setEnabled(True)
        self.更新状态标签.setText(f"❌ 下载失败：{错误}")
        self._记日志(f"更新包下载失败：{错误}", "警告")

    def _下载成功(self, 路径: str):
        包 = Path(路径)
        self._下载好的包 = 包
        self.更新状态标签.setText(f"✅ 下载完成：{包.name}（{_大小文本(包.stat().st_size)}）")
        self._记日志(f"更新包已下载：{包}")
        self.下载按钮.setEnabled(True)
        self._套用更新(包)

    def _套用更新(self, 包: Path):
        """解压 → 写更新脚本 → 确认后拉起脚本并退出本程序。"""
        版本 = str((self._最新发布 or {}).get("版本") or "新版本")
        暂存 = 更新.更新目录(self.项目根) / 版本
        try:
            根 = 更新.解压到(包, 暂存)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "解压失败", str(e))
            return
        答案 = QMessageBox.question(
            self, "准备就绪，现在更新？",
            f"新版本已经解压好：\n{根}\n\n"
            f"点「是」之后本程序会关掉，由独立脚本把新版本覆盖上去并自动重启。\n"
            f"{更新.保留说明}。\n\n（更新包会留在「打开下载目录」里，可随时删）",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
        if 答案 != QMessageBox.Yes:
            self.更新状态标签.setText(
                f"已解压到 {根}，随时可以点「⬇ 下载并更新」再来一次。")
            return
        try:
            脚本 = 更新.写更新脚本(根, self.项目根, 等进程=os.getpid())
            更新.拉起更新脚本(脚本)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "启动更新脚本失败", str(e))
            return
        self._记日志("开始套用更新：程序即将退出并自动重启")
        QMessageBox.information(
            self, "马上更新",
            "更新脚本已经启动，本程序现在关闭。\n几秒后会自动用新版本重新打开。")
        self.主窗口.close()
