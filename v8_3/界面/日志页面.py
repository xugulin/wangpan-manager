"""日志页面：所有适配器桥日志、传输事件、界面操作的汇总输出。"""

from __future__ import annotations

import time
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QDesktopServices
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import (
    QCheckBox, QFileDialog, QHBoxLayout, QLabel, QMessageBox,
    QPlainTextEdit, QPushButton, QVBoxLayout, QWidget,
)

from ..配置 import 项目根


class 日志页面(QWidget):
    """只读日志 + 清空/保存；日志同时写入 数据/界面日志.txt。"""

    最大行数 = 5000

    def __init__(self, 主窗口, 父=None):
        super().__init__(父)
        self.主窗口 = 主窗口
        self._行数 = 0
        self._构建()

    def _构建(self):
        布局 = QVBoxLayout(self)
        布局.setContentsMargins(0, 0, 0, 0)
        布局.setSpacing(8)

        顶部 = QHBoxLayout()
        顶部.addWidget(QLabel(f"📋 运行日志（最多保留 {self.最大行数} 行）"))
        顶部.addStretch(1)
        self.自动滚动 = QCheckBox("自动滚动")
        self.自动滚动.setChecked(True)
        顶部.addWidget(self.自动滚动)
        保存 = QPushButton("💾 另存为…")
        保存.clicked.connect(self._另存为)
        顶部.addWidget(保存)
        打开目录 = QPushButton("📂 打开日志目录")
        打开目录.clicked.connect(self._打开日志目录)
        顶部.addWidget(打开目录)
        打开配置 = QPushButton("⚙ 打开配置文件")
        打开配置.clicked.connect(self._打开配置文件)
        顶部.addWidget(打开配置)
        打开数据 = QPushButton("🗂 打开项目数据目录")
        打开数据.clicked.connect(self._打开项目数据目录)
        顶部.addWidget(打开数据)
        清空 = QPushButton("🧹 清空")
        清空.clicked.connect(self.清空)
        顶部.addWidget(清空)
        布局.addLayout(顶部)

        self.日志框 = QPlainTextEdit()
        self.日志框.setReadOnly(True)
        self.日志框.setMaximumBlockCount(self.最大行数)
        self.日志框.setLineWrapMode(QPlainTextEdit.NoWrap)
        布局.addWidget(self.日志框, 1)

    # ---------------- 写入 ----------------

    def 追加(self, 消息: str, 落盘: bool = False):
        行 = time.strftime("[%H:%M:%S] ") + str(消息)
        self.日志框.appendPlainText(行)
        self._行数 += 1
        if self.自动滚动.isChecked():
            滚动条 = self.日志框.verticalScrollBar()
            滚动条.setValue(滚动条.maximum())
        if 落盘:
            self.写文件(行)

    @staticmethod
    def 写文件(行: str) -> None:
        try:
            目录 = 项目根 / "数据"
            目录.mkdir(parents=True, exist_ok=True)
            文件 = 目录 / "界面日志.txt"
            if 文件.is_file() and 文件.stat().st_size > 4 * 1024 * 1024:
                文件.write_text("", encoding="utf-8")
            with 文件.open("a", encoding="utf-8") as f:
                f.write(行 + "\n")
        except Exception:
            pass

    def 清空(self):
        self.日志框.clear()
        self._行数 = 0

    def 文本(self) -> str:
        return self.日志框.toPlainText()

    # ---------------- 导出 ----------------

    def _另存为(self):
        路径, _ = QFileDialog.getSaveFileName(
            self, "保存日志", str(Path.home() / "v8_3_界面日志.txt"),
            "文本文件 (*.txt)")
        if not 路径:
            return
        try:
            Path(路径).write_text(self.文本(), encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "保存失败", str(e))
            return
        self.主窗口.状态消息(f"日志已保存：{路径}")

    def _打开配置文件(self):
        from ..配置 import 保存配置
        路径 = getattr(self.主窗口, "配置路径", None) or (项目根 / "配置.json")
        try:
            if not Path(路径).is_file():
                保存配置(self.主窗口.配置, 路径)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "写入配置失败", str(e))
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(路径)))

    def _打开项目数据目录(self):
        目录 = 项目根 / "数据"
        目录.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(目录)))

    def _打开日志目录(self):
        目录 = 项目根 / "数据"
        目录.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(目录)))
