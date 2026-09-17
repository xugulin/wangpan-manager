"""跨网盘直传对话框（V8_3 版）。

沿用 网盘管理_V8 主窗口里那个「🔀 跨网盘直传」对话框的形态：
源/目标两块 + 网盘下拉 + 路径输入 + 📂 选择 + 开始/取消。
差别是源和目标都来自 V8_3 的网盘实例，路径用适配器直接浏览。
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QComboBox, QDialog, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QMessageBox, QPushButton, QVBoxLayout,
)

from ..核心.模型 import 规范路径
from .路径选择对话框 import 路径选择对话框


class 跨盘直传对话框(QDialog):
    """返回 :attr:`结果` = (源标识, 源路径, 目标标识, 目标路径)。"""

    def __init__(self, 主窗口, 默认源: str = "", 默认路径: str = "/",
                 父窗口=None):
        super().__init__(父窗口 or 主窗口)
        self.主窗口 = 主窗口
        self.结果: tuple[str, str, str, str] | None = None
        self.规格表 = 主窗口.动作.规格表
        if not self.规格表:
            raise RuntimeError("没有可用的网盘，请先在左下角「新增网盘」")

        self.setWindowTitle("🔀 跨网盘直传")
        self.setMinimumWidth(820)
        布局 = QVBoxLayout(self)

        源组 = QGroupBox("📤 源")
        源行 = QHBoxLayout(源组)
        源行.addWidget(QLabel("网盘:"))
        self.源下拉 = QComboBox()
        self._填充网盘(self.源下拉, 默认源)
        源行.addWidget(self.源下拉)
        源行.addWidget(QLabel("路径:"))
        self.源路径 = QLineEdit(默认路径 or "/")
        源行.addWidget(self.源路径, 1)
        源选择 = QPushButton("📂 选择")
        源选择.clicked.connect(lambda: self._选择(self.源下拉, self.源路径, True))
        源行.addWidget(源选择)
        布局.addWidget(源组)

        目标组 = QGroupBox("📥 目标")
        目标行 = QHBoxLayout(目标组)
        目标行.addWidget(QLabel("网盘:"))
        self.目标下拉 = QComboBox()
        self._填充网盘(self.目标下拉, 默认源)
        目标行.addWidget(self.目标下拉)
        目标行.addWidget(QLabel("路径:"))
        self.目标路径 = QLineEdit(默认路径 or "/")
        目标行.addWidget(self.目标路径, 1)
        目标选择 = QPushButton("📂 选择")
        目标选择.clicked.connect(lambda: self._选择(self.目标下拉, self.目标路径, False))
        目标行.addWidget(目标选择)
        布局.addWidget(目标组)

        提示 = QLabel("源是文件时，目标路径就是目标文件名；源是目录时，"
                      "把源目录内容递归放进目标目录。目标已存在默认跳过，"
                      "可在传输页勾选「覆盖」。")
        提示.setWordWrap(True)
        布局.addWidget(提示)

        按钮行 = QHBoxLayout()
        按钮行.addStretch(1)
        开始 = QPushButton("▶ 开始")
        开始.setObjectName("SuccessButton")
        开始.clicked.connect(self._确定)
        取消 = QPushButton("✖ 取消")
        取消.clicked.connect(self.reject)
        按钮行.addWidget(开始)
        按钮行.addWidget(取消)
        布局.addLayout(按钮行)

    def _填充网盘(self, 下拉: QComboBox, 默认: str):
        for 标识, 规格 in self.规格表.items():
            下拉.addItem(f"{规格.图标} {规格.显示名}", 标识)
        idx = 下拉.findData(默认)
        if idx >= 0:
            下拉.setCurrentIndex(idx)

    def _选择(self, 下拉: QComboBox, 输入: QLineEdit, 允许文件: bool):
        标识 = 下拉.currentData()
        规格 = self.规格表.get(标识)
        if 规格 is None:
            return
        try:
            适配器 = self.主窗口.动作.适配器(标识)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "适配器不可用", str(e))
            return
        picker = 路径选择对话框(
            适配器, 标识, 初始路径=输入.text() or "/",
            允许选择文件=允许文件, 允许新建=not 允许文件,
            标题=f"选择路径 · {规格.显示名}", 父窗口=self)
        if picker.exec() == QDialog.Accepted and picker.选中路径:
            输入.setText(picker.选中路径)

    def _确定(self):
        源标识 = self.源下拉.currentData()
        目标标识 = self.目标下拉.currentData()
        源路径 = 规范路径(self.源路径.text() or "/")
        目标路径 = 规范路径(self.目标路径.text() or "/")
        if 源标识 == 目标标识 and 源路径 == 目标路径:
            QMessageBox.warning(self, "提示", "源和目标完全相同")
            return
        self.结果 = (源标识, 源路径, 目标标识, 目标路径)
        self.accept()
