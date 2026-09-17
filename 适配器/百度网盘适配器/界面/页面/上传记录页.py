# 百度网盘适配器/界面/页面/上传记录页.py
"""上传记录页（本地记录）

⚠️ 与原骨架的差异
----------------
原骨架通过 `POST /userres/v1/get_user_action` 查询服务端上传记录。
**百度侧未抓到对应的查询接口**，因此本页改为展示**本地上传记录**：
每次成功上传（含秒传命中）后由 `上传总调度` 追加一条到
`数据/上传记录.json`，本页读取展示。

记录是「本机视角」而非「账号视角」——换一台机器看不到历史，这是已知限制。
"""

from __future__ import annotations

import logging

from PySide6.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QMessageBox,
    QTableWidget, QTableWidgetItem, QHeaderView, QApplication,
)
from PySide6.QtCore import Qt

from 核心.上传.上传总调度 import 读取上传记录, 记录文件

from ..格式化工具 import 格式化大小, 格式化时间戳
from .基类 import 页面基类

logger = logging.getLogger("百度网盘.界面.上传记录")

列_时间, 列_文件名, 列_大小, 列_方式, 列_分片, 列_远端路径 = range(6)


class 上传记录页(页面基类):
    """本地上传记录。"""

    def __init__(self, 主窗口, parent=None):
        super().__init__(主窗口, parent)
        self.记录: list[dict] = []
        self._构建界面()

    # ==================== UI ====================

    def _构建界面(self):
        布局 = QVBoxLayout(self)

        栏 = QHBoxLayout()
        self.刷新按钮 = QPushButton("🔄 刷新")
        self.刷新按钮.clicked.connect(self.加载上传记录)
        栏.addWidget(self.刷新按钮)

        self.复制路径按钮 = QPushButton("📋 复制远端路径")
        self.复制路径按钮.clicked.connect(self.复制选中路径)
        栏.addWidget(self.复制路径按钮)

        self.清空按钮 = QPushButton("🗑 清空本地记录")
        self.清空按钮.setToolTip(f"仅删除 {记录文件.name}，不影响网盘文件")
        self.清空按钮.clicked.connect(self.清空记录)
        栏.addWidget(self.清空按钮)

        栏.addStretch()
        self.状态提示 = QLabel("就绪")
        self.状态提示.setStyleSheet("color: #666;")
        栏.addWidget(self.状态提示)
        布局.addLayout(栏)

        提示 = QLabel(
            "ℹ️ 这是**本机**的上传记录（百度未提供上传记录查询接口）。"
            "换机器看不到历史。")
        提示.setStyleSheet("color: #999; font-size: 11px;")
        布局.addWidget(提示)

        self.表格 = QTableWidget()
        self.表格.setColumnCount(6)
        self.表格.setHorizontalHeaderLabels(
            ["时间", "文件名", "大小", "方式", "分片", "远端路径"])
        self.表格.horizontalHeader().setSectionResizeMode(
            列_远端路径, QHeaderView.Stretch)
        self.表格.setColumnWidth(列_时间, 160)
        self.表格.setColumnWidth(列_文件名, 220)
        self.表格.setColumnWidth(列_大小, 100)
        self.表格.setColumnWidth(列_方式, 80)
        self.表格.setColumnWidth(列_分片, 60)
        self.表格.setAlternatingRowColors(True)
        self.表格.setSelectionBehavior(QTableWidget.SelectRows)
        self.表格.setEditTriggers(QTableWidget.NoEditTriggers)
        布局.addWidget(self.表格, 1)

    # ==================== 登录态 ====================

    def 设置启用(self, 已登录: bool):
        # 本地记录不依赖登录，始终可用
        for 控件 in (self.刷新按钮, self.复制路径按钮, self.清空按钮):
            控件.setEnabled(True)

    def 重置(self):
        # 本地记录不随登录态清空
        pass

    def 首次进入(self):
        self.加载上传记录()

    # ==================== 数据 ====================

    def 加载上传记录(self):
        """主界面「全局刷新」与刷新按钮共用入口。"""
        try:
            self.记录 = 读取上传记录()
        except Exception as e:
            logger.error(f"[上传记录] 读取失败：{e}")
            self.状态提示.setText(f"读取失败：{e}")
            return
        self._渲染()

    def _渲染(self):
        self.表格.setRowCount(len(self.记录))
        for 行, r in enumerate(self.记录):
            单元格 = [
                格式化时间戳(r.get("时间") or 0),
                str(r.get("文件名") or ""),
                格式化大小(r.get("大小") or 0),
                "秒传" if r.get("是否秒传") else "分片",
                str(r.get("分片数") or "—"),
                str(r.get("远端路径") or ""),
            ]
            for 列, 文本 in enumerate(单元格):
                格 = QTableWidgetItem(文本)
                if 列 == 列_方式 and r.get("是否秒传"):
                    格.setForeground(Qt.darkGreen)
                格.setData(Qt.UserRole, r)
                self.表格.setItem(行, 列, 格)
        self.状态提示.setText(
            f"共 {len(self.记录)} 条本地记录（文件：{记录文件.name}）")

    # ==================== 操作 ====================

    def _选中(self) -> dict | None:
        行 = self.表格.currentRow()
        if 0 <= 行 < len(self.记录):
            return self.记录[行]
        QMessageBox.information(self, "提示", "请先选中一条记录")
        return None

    def 复制选中路径(self):
        r = self._选中()
        if not r:
            return
        QApplication.clipboard().setText(str(r.get("远端路径") or ""))
        self.状态提示.setText("远端路径已复制")

    def 清空记录(self):
        应答 = QMessageBox.question(
            self, "清空本地记录",
            f"将删除本机记录文件：\n{记录文件}\n\n"
            f"**不会**影响网盘上的任何文件。是否继续？")
        if 应答 != QMessageBox.Yes:
            return
        try:
            if 记录文件.is_file():
                记录文件.unlink()
            self.记录 = []
            self._渲染()
            self.状态提示.setText("本地记录已清空")
        except Exception as e:
            QMessageBox.warning(self, "清空失败", f"删除记录文件失败：\n{e}")
