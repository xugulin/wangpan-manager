# 光鸭云盘适配器/界面/页面/账号信息页.py
"""账号信息页"""

import json
import logging

from PySide6.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QPushButton, QMessageBox,
    QTreeWidget, QTreeWidgetItem, QSplitter, QTextEdit,
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont

from 核心.接口.资产接口 import 资产接口
from 核心.接口.文件接口 import 文件接口
from 核心.接口.杂项接口 import 杂项接口

from ..字段映射 import 翻译字段名, 格式化值
from .基类 import 页面基类

logger = logging.getLogger("光鸭云盘.界面.账号信息")


class 账号信息页(页面基类):
    """账号信息页"""

    类型列表 = [
        "用户信息", "资产信息", "用户权益",
        "下载记录", "分享列表",
        "云收集任务", "回收站", "用户行为",
    ]

    def __init__(self, 主窗口, parent=None):
        super().__init__(主窗口, parent)
        self.缓存: dict = {}
        self._构建界面()

    def _构建界面(self):
        布局 = QVBoxLayout(self)

        按钮条 = QHBoxLayout()
        for 名称 in self.类型列表:
            按钮 = QPushButton(名称)
            按钮.clicked.connect(
                lambda _, n=名称: self.查询账号信息(n))
            按钮条.addWidget(按钮)
        按钮条.addStretch()

        刷新全部 = QPushButton("🔄 刷新全部")
        刷新全部.setStyleSheet(
            "background: #2196F3; color: white; padding: 6px 12px; "
            "border-radius: 4px; font-weight: bold;")
        刷新全部.clicked.connect(self.刷新全部)
        按钮条.addWidget(刷新全部)
        布局.addLayout(按钮条)

        分栏 = QSplitter(Qt.Horizontal)

        self.树 = QTreeWidget()
        self.树.setHeaderLabels(["项目", "值"])
        self.树.setColumnWidth(0, 240)
        self.树.setColumnWidth(1, 320)
        self.树.itemClicked.connect(self._树点击)
        分栏.addWidget(self.树)

        self.JSON = QTextEdit()
        self.JSON.setReadOnly(True)
        self.JSON.setFont(QFont("Consolas, 微软雅黑", 10))
        self.JSON.setStyleSheet(
            "background: #1e1e1e; color: #d4d4d4; border: 1px solid #333;")
        分栏.addWidget(self.JSON)
        分栏.setSizes([550, 500])
        布局.addWidget(分栏, 1)

    def 设置启用(self, 已登录: bool):
        # 按钮组由主窗口统一启用/禁用（不强求）；这里不做额外处理
        pass

    def 重置(self):
        self.树.clear()
        self.缓存.clear()
        self.JSON.clear()

    # ---------------- 查询 ----------------

    def 查询账号信息(self, 类型: str):
        if not self.网络:
            QMessageBox.warning(self, "提示", "请先登录")
            return
        方法 = self._取方法(类型)
        if 方法 is None:
            return
        self.启动网络任务(
            类型, 方法, self._显示账号信息, self._账号信息失败)

    def 刷新全部(self):
        if not self.网络:
            QMessageBox.warning(self, "提示", "请先登录")
            return
        for 名称 in self.类型列表:
            self.查询账号信息(名称)

    def _取方法(self, 类型: str):
        资产 = 资产接口(self.网络)
        文件 = 文件接口(self.网络)
        杂项 = 杂项接口(self.网络)
        映射 = {
            "用户信息": lambda: self.认证.取当前用户(),
            "资产信息": 资产.取资产信息,
            "用户权益": 资产.取用户权益,
            "下载记录": lambda: 文件.取用户操作(
                pageSize=50, excludeFileTypes=[2]),
            "分享列表": 杂项.取分享列表,
            "云收集任务": 杂项.取云收集任务列表,
            "回收站": lambda: 文件.取回收站列表(pageSize=100),
            "用户行为": lambda: 文件.取用户操作(pageSize=50),
        }
        return 映射.get(类型)

    def _账号信息失败(self, 任务名: str, 错误: str):
        logger.error(f"{任务名} 查询失败：{错误}")
        顶层节点 = None
        for i in range(self.树.topLevelItemCount()):
            节点 = self.树.topLevelItem(i)
            if 节点.text(0) == 任务名:
                顶层节点 = 节点
                break
        if 顶层节点 is None:
            顶层节点 = QTreeWidgetItem(self.树)
            顶层节点.setText(0, 任务名)
        顶层节点.takeChildren()
        子 = QTreeWidgetItem(顶层节点)
        子.setText(0, "❌ 查询失败")
        子.setText(1, 错误)
        顶层节点.setExpanded(True)

    def _显示账号信息(self, 类型: str, 数据):
        self.缓存[类型] = 数据
        try:
            文本 = json.dumps(数据, ensure_ascii=False, indent=2)
        except Exception:
            文本 = str(数据)
        self.JSON.setPlainText(文本)

        顶层节点 = None
        for i in range(self.树.topLevelItemCount()):
            节点 = self.树.topLevelItem(i)
            if 节点.text(0) == 类型:
                顶层节点 = 节点
                break
        if 顶层节点 is None:
            顶层节点 = QTreeWidgetItem(self.树)
            顶层节点.setText(0, 类型)
        顶层节点.takeChildren()
        顶层节点.setExpanded(True)

        if 数据 is None:
            子 = QTreeWidgetItem(顶层节点)
            子.setText(0, "（无数据）")
            子.setText(1, "—")
            return

        self._填节点(顶层节点, None, 数据)

    def _填节点(self, 父节点, 键, 值):
        if 键 is None:
            中文名 = None
        else:
            中文名 = 翻译字段名(键)

        if isinstance(值, dict):
            子 = QTreeWidgetItem(父节点)
            子.setText(0, 中文名 or "（根）")
            子.setText(1, f"（{len(值)} 项）")
            for 子键, 子值 in 值.items():
                self._填节点(子, 子键, 子值)
            子.setExpanded(True)
        elif isinstance(值, list):
            子 = QTreeWidgetItem(父节点)
            子.setText(0, 中文名 or "（列表）")
            子.setText(1, f"[{len(值)} 项]")
            for idx, 项 in enumerate(值):
                self._填节点(子, f"[{idx}]", 项)
            子.setExpanded(False)
        else:
            显示值 = 格式化值(str(键), 值) if 键 is not None else 值
            子 = QTreeWidgetItem(父节点)
            子.setText(0, 中文名 or "值")
            子.setText(1, str(显示值))

    def _树点击(self, 项, 列号):
        if 项 is None:
            return
        父 = 项
        while 父.parent() is not None:
            父 = 父.parent()
        类型 = 父.text(0)
        数据 = self.缓存.get(类型)
        if 数据 is None:
            return
        try:
            文本 = json.dumps(数据, ensure_ascii=False, indent=2)
        except Exception:
            文本 = str(数据)
        self.JSON.setPlainText(文本)