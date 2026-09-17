# 光鸭云盘适配器/界面/页面/上传记录页.py
"""上传记录页"""

import logging

from PySide6.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QMessageBox,
    QTableWidget, QTableWidgetItem, QHeaderView, QSpinBox,
)
from PySide6.QtCore import Qt

from 核心.接口.文件接口 import 文件接口

from ..格式化工具 import 格式化大小, 格式化时间戳
from ..字段映射 import 资源类型映射, 操作类型映射
from .基类 import 页面基类

logger = logging.getLogger("光鸭云盘.界面.上传记录")


class 上传记录页(页面基类):
    """上传记录页"""

    def __init__(self, 主窗口, parent=None):
        super().__init__(主窗口, parent)

        self.上传记录游标 = ""
        self.上传记录总数 = 0
        self.上传记录已加载 = 0

        self._构建界面()

    def _构建界面(self):
        布局 = QVBoxLayout(self)

        工具条 = QHBoxLayout()
        工具条.addWidget(QLabel("每页："))

        self.上传记录每页 = QSpinBox()
        self.上传记录每页.setRange(5, 200)
        self.上传记录每页.setValue(20)
        self.上传记录每页.setSuffix(" 条")
        工具条.addWidget(self.上传记录每页)

        self.加载上传记录按钮 = QPushButton("🔄 加载/刷新")
        self.加载上传记录按钮.setStyleSheet(
            "background: #4CAF50; color: white; padding: 6px 14px; "
            "border-radius: 4px; font-weight: bold;")
        self.加载上传记录按钮.clicked.connect(self.加载上传记录)
        工具条.addWidget(self.加载上传记录按钮)

        self.加载更多按钮 = QPushButton("⬇ 加载更多")
        self.加载更多按钮.setStyleSheet(
            "background: #2196F3; color: white; padding: 6px 14px; "
            "border-radius: 4px; font-weight: bold;")
        self.加载更多按钮.clicked.connect(self.加载更多上传记录)
        self.加载更多按钮.setEnabled(False)
        工具条.addWidget(self.加载更多按钮)

        工具条.addStretch()

        self.上传记录统计标签 = QLabel("尚未加载")
        self.上传记录统计标签.setStyleSheet(
            "color: #666; padding-left: 10px;")
        工具条.addWidget(self.上传记录统计标签)

        布局.addLayout(工具条)

        self.上传记录表格 = QTableWidget()
        self.上传记录表格.setColumnCount(6)
        self.上传记录表格.setHorizontalHeaderLabels(
            ["文件名", "大小", "类型", "父目录", "上传时间", "文件ID"])
        self.上传记录表格.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.Stretch)
        self.上传记录表格.setColumnWidth(1, 100)
        self.上传记录表格.setColumnWidth(2, 70)
        self.上传记录表格.setColumnWidth(3, 180)
        self.上传记录表格.setColumnWidth(4, 170)
        self.上传记录表格.setColumnWidth(5, 200)
        self.上传记录表格.setAlternatingRowColors(True)
        self.上传记录表格.setSelectionBehavior(QTableWidget.SelectRows)
        self.上传记录表格.setSelectionMode(
            QTableWidget.ExtendedSelection)
        self.上传记录表格.setEditTriggers(QTableWidget.NoEditTriggers)
        布局.addWidget(self.上传记录表格, 1)

        底部 = QLabel(
            "💡 提示：上传记录来自 /userres/v1/get_user_action，"
            "按批次返回并已扁平化展示。")
        底部.setStyleSheet("color: #888; font-size: 11px; padding: 4px;")
        底部.setWordWrap(True)
        布局.addWidget(底部)

    # ---------------- 登录态 ----------------

    def 设置启用(self, 已登录: bool):
        self.加载上传记录按钮.setEnabled(已登录)
        self.加载更多按钮.setEnabled(
            已登录 and bool(self.上传记录游标))

    def 首次进入(self):
        if self.上传记录表格.rowCount() == 0:
            self.加载上传记录()

    def 重置(self):
        self.上传记录表格.setRowCount(0)
        self.上传记录游标 = ""
        self.上传记录总数 = 0
        self.上传记录已加载 = 0
        self.上传记录统计标签.setText("尚未加载")

    # ---------------- 加载 ----------------

    def 加载上传记录(self, 仅刷新: bool = False):
        if not self.网络:
            if not 仅刷新:
                QMessageBox.warning(self, "提示", "请先登录")
            return

        self.上传记录表格.setRowCount(0)
        self.上传记录游标 = ""
        self.上传记录已加载 = 0
        self.上传记录总数 = 0

        每页 = self.上传记录每页.value()
        self.上传记录统计标签.setText("加载中...")
        self.上传记录统计标签.setStyleSheet("color: #2196F3;")
        self.加载上传记录按钮.setEnabled(False)

        def 任务():
            文件 = 文件接口(self.网络)
            return 文件.取上传记录(pageSize=每页, cursor="")

        self.启动网络任务(
            "上传记录第一页", 任务,
            lambda 名, 数据: self._上传记录成功(数据, 追加=False),
            self._上传记录失败,
        )

    def 加载更多上传记录(self):
        if not self.网络 or not self.上传记录游标:
            return

        每页 = self.上传记录每页.value()
        self.加载更多按钮.setEnabled(False)
        self.上传记录统计标签.setText("加载更多...")
        self.上传记录统计标签.setStyleSheet("color: #2196F3;")

        游标 = self.上传记录游标

        def 任务():
            文件 = 文件接口(self.网络)
            return 文件.取上传记录(pageSize=每页, cursor=游标)

        self.启动网络任务(
            "上传记录下一页", 任务,
            lambda 名, 数据: self._上传记录成功(数据, 追加=True),
            self._上传记录失败,
        )

    def _上传记录成功(self, 数据: dict, 追加: bool):
        if not isinstance(数据, dict):
            数据 = {}

        批次列表 = 数据.get("list", []) or []
        self.上传记录总数 = 数据.get("total", 0)
        self.上传记录游标 = 数据.get("cursor", "") or ""
        还有更多 = bool(数据.get("hasMore"))

        if not 追加:
            self.上传记录表格.setRowCount(0)

        行数 = 0
        for 批次 in 批次列表:
            操作类型 = 批次.get("actionType", 0)
            批次时间 = 批次.get("ctime")
            详情列表 = 批次.get("actionDetails") or []
            for 详情 in 详情列表:
                行 = self.上传记录表格.rowCount()
                self.上传记录表格.insertRow(行)
                行数 += 1
                self.上传记录已加载 += 1

                文件名 = 详情.get("fileName", "")
                大小 = 详情.get("fileSize", 0) or 0
                父目录 = 详情.get("parentName", "") or ""
                时间戳 = 详情.get("utime") or 批次时间
                文件ID = str(详情.get("fileId", ""))
                资源类型 = 详情.get("resType", 1)
                扩展名 = 详情.get("ext", "") or ""

                类型名 = 资源类型映射.get(资源类型, "文件")
                if 扩展名:
                    类型名 = f"{类型名} {扩展名}"

                时间字符串 = (格式化时间戳(时间戳)
                           if isinstance(时间戳, (int, float))
                           else str(时间戳 or "—"))

                名称项 = QTableWidgetItem(文件名)
                提示 = [f"文件名：{文件名}",
                        f"文件 ID：{文件ID}",
                        f"操作类型：{操作类型映射.get(操作类型, 操作类型)}"]
                缩略图 = 详情.get("thumbnail")
                if isinstance(缩略图, str) and 缩略图:
                    提示.append(f"缩略图：{缩略图[:80]}...")
                名称项.setToolTip("\n".join(提示))
                名称项.setData(Qt.UserRole, 详情)
                self.上传记录表格.setItem(行, 0, 名称项)

                self.上传记录表格.setItem(
                    行, 1, QTableWidgetItem(格式化大小(大小)))
                self.上传记录表格.setItem(
                    行, 2, QTableWidgetItem(类型名))
                self.上传记录表格.setItem(
                    行, 3, QTableWidgetItem(父目录 or "—"))
                self.上传记录表格.setItem(
                    行, 4, QTableWidgetItem(时间字符串))
                self.上传记录表格.setItem(
                    行, 5, QTableWidgetItem(文件ID))

        self.上传记录统计标签.setText(
            f"已加载 {self.上传记录已加载} 条 / "
            f"共 {self.上传记录总数} 条（本页新增 {行数}）")
        self.上传记录统计标签.setStyleSheet("color: #4CAF50;")
        self.加载上传记录按钮.setEnabled(True)
        self.加载更多按钮.setEnabled(还有更多 and bool(self.上传记录游标))

        logger.info(
            f"[上传记录] 加载 {行数} 条，累计 {self.上传记录已加载}，"
            f"hasMore={还有更多}")

    def _上传记录失败(self, 任务名: str, 错误: str):
        logger.error(f"[上传记录] 失败：{错误}")
        self.上传记录统计标签.setText(f"加载失败：{错误}")
        self.上传记录统计标签.setStyleSheet("color: #F44336;")
        self.加载上传记录按钮.setEnabled(True)
        self.加载更多按钮.setEnabled(bool(self.上传记录游标))
        QMessageBox.warning(self, "上传记录加载失败", 错误)