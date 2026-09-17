# 百度网盘适配器/界面/页面/账号信息页.py
"""账号信息页（百度网盘）

数据来源（均已在 Phase 1 实测）：
  - `GET /api/user/getinfo?need_boardinfo=1&user_list=[<uk>]`
    → `records[0]`：uname / avatar_url / vip_type / vip_level / priority_name
  - `GET /api/quota` → total / used / free
  - `GET /api/loginStatus` → 会话自检

⚠️ 原骨架的「下载记录 / 用户行为 / 回收站」三项依赖的接口在百度侧没有对应，
   已移除；回收站查询归 Phase 3 的 `回收站接口`，在文件浏览页提供入口。
"""

from __future__ import annotations

import json
import logging

from PySide6.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QPushButton, QMessageBox, QLabel,
    QTreeWidget, QTreeWidgetItem, QSplitter, QTextEdit, QGroupBox,
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont

from 核心.认证.认证服务 import 认证服务
from 核心.认证.会话仓库 import 全局会话仓库

from ..字段映射 import 翻译字段名, 格式化值, VIP类型映射
from ..格式化工具 import 格式化大小, 格式化时间戳
from .基类 import 页面基类

logger = logging.getLogger("百度网盘.界面.账号信息")


class 账号信息页(页面基类):
    """账号信息：用户资料 + 容量 + 会话状态。"""

    def __init__(self, 主窗口, parent=None):
        super().__init__(主窗口, parent)
        self.原始数据: dict = {}
        self._构建界面()

    # ==================== UI ====================

    def _构建界面(self):
        布局 = QVBoxLayout(self)

        栏 = QHBoxLayout()
        self.刷新按钮 = QPushButton("🔄 刷新全部")
        self.刷新按钮.clicked.connect(self.刷新全部)
        栏.addWidget(self.刷新按钮)

        self.概要标签 = QLabel("尚未加载")
        self.概要标签.setStyleSheet(
            "font-size: 14px; font-weight: bold; padding-left: 10px;")
        栏.addWidget(self.概要标签, 1)
        布局.addLayout(栏)

        分割 = QSplitter(Qt.Vertical)

        # ---- 字段树 ----
        self.树 = QTreeWidget()
        self.树.setHeaderLabels(["字段", "值", "原值"])
        self.树.setColumnWidth(0, 240)
        self.树.setColumnWidth(1, 340)
        分割.addWidget(self.树)

        # ---- 原始 JSON ----
        self.原始框 = QTextEdit()
        self.原始框.setReadOnly(True)
        self.原始框.setFont(QFont("Monospace", 10))
        self.原始框.setPlaceholderText("原始响应 JSON（已脱敏）")
        分割.addWidget(self.原始框)

        分割.setSizes([420, 240])
        布局.addWidget(分割, 1)

    # ==================== 登录态 ====================

    def 设置启用(self, 已登录: bool):
        self.刷新按钮.setEnabled(已登录)

    def 重置(self):
        self.原始数据 = {}
        self.树.clear()
        self.原始框.clear()
        self.概要标签.setText("尚未加载")

    def 首次进入(self):
        if not self.原始数据:
            self.刷新全部()

    # ==================== 刷新 ====================

    def 刷新全部(self):
        """一次性拉取 用户资料 + 容量 + 登录态（在后台线程串行执行）。"""
        if not self.网络:
            self.概要标签.setText("未登录")
            return
        self.概要标签.setText("加载中…")

        def 任务():
            认证 = 认证服务(网络=self.网络, 仓库=全局会话仓库)
            结果 = {}
            错误 = {}
            for 名称, 函数 in (
                ("用户信息", 认证.取当前用户),
                ("容量", 认证.取容量),
                ("登录态", 认证.检查登录态),
            ):
                try:
                    结果[名称] = 函数()
                except Exception as e:
                    logger.warning(f"[账号信息] {名称} 获取失败：{e}")
                    错误[名称] = f"{type(e).__name__}: {e}"
            return {"数据": 结果, "错误": 错误}

        self.启动网络任务("账号信息", 任务, self._加载成功, self._加载失败)

    def _加载成功(self, 任务名: str, 结果: dict):
        self.原始数据 = 结果.get("数据") or {}
        错误 = 结果.get("错误") or {}

        self.树.clear()

        用户 = self.原始数据.get("用户信息") or {}
        if 用户:
            顶层 = QTreeWidgetItem(["👤 用户信息", "", ""])
            self.树.addTopLevelItem(顶层)
            for 键, 值 in 用户.items():
                if isinstance(值, (dict, list)):
                    continue
                顶层.addChild(QTreeWidgetItem(
                    [翻译字段名(键), str(格式化值(键, 值)), str(值)]))

        容量 = self.原始数据.get("容量") or {}
        if 容量:
            顶层 = QTreeWidgetItem(["💾 容量", "", ""])
            self.树.addTopLevelItem(顶层)
            总 = 容量.get("total") or 0
            已用 = 容量.get("used") or 0
            for 键 in ("total", "used", "free"):
                if 键 in 容量:
                    顶层.addChild(QTreeWidgetItem(
                        [翻译字段名(键),
                         str(格式化值(键, 容量[键])), str(容量[键])]))
            if 总:
                顶层.addChild(QTreeWidgetItem(
                    ["使用率", f"{已用 * 100 / 总:.1f}%", ""]))

        登录态 = self.原始数据.get("登录态") or {}
        if 登录态:
            顶层 = QTreeWidgetItem(["🔐 登录态", "", ""])
            self.树.addTopLevelItem(顶层)
            for 键, 值 in 登录态.items():
                if isinstance(值, (dict, list)):
                    continue
                顶层.addChild(QTreeWidgetItem(
                    [翻译字段名(键), str(格式化值(键, 值)), str(值)]))

        # ---- 会话摘要 ----
        会话 = 全局会话仓库.取会话()
        顶层 = QTreeWidgetItem(["📁 本地会话", "", ""])
        self.树.addTopLevelItem(顶层)
        顶层.addChild(QTreeWidgetItem(
            ["会话文件", str(全局会话仓库.文件路径), ""]))
        顶层.addChild(QTreeWidgetItem(
            ["已保存 Cookie",
             "、".join(k for k in ("BDUSS", "BDUSS_BFESS", "STOKEN")
                      if 会话.get(k.lower())) or "（无）", ""]))
        顶层.addChild(QTreeWidgetItem(
            ["更新时间",
            格式化时间戳(会话.get("更新时间") or 0), str(会话.get("更新时间"))]))
        顶层.addChild(QTreeWidgetItem(
            ["会话是否有效", "是" if 全局会话仓库.是否有效() else "否", ""]))

        self.树.expandAll()

        # ---- 概要 ----
        名字 = 用户.get("uname") or "（未获取）"
        会员 = VIP类型映射.get(用户.get("vip_type"), "")
        概要 = f"{名字}"
        if 会员:
            概要 += f" · {会员}"
        if 总:
            概要 += f" · 已用 {格式化大小(已用)} / {格式化大小(总)}"
        self.概要标签.setText(概要)

        # ---- 原始 JSON（脱敏）----
        安全 = json.loads(json.dumps(self.原始数据, ensure_ascii=False, default=str))
        for 块 in 安全.values():
            if isinstance(块, dict):
                for k in ("bdstoken", "token", "avatar_url"):
                    if k in 块 and k != "avatar_url":
                        块[k] = "<已脱敏>"
        self.原始框.setPlainText(json.dumps(安全, ensure_ascii=False, indent=2))

        if 错误:
            logger.warning(f"[账号信息] 部分项失败：{错误}")

    def _加载失败(self, 任务名: str, 错误: str):
        self.概要标签.setText(f"加载失败：{错误}")
        QMessageBox.warning(self, "加载失败", f"账号信息加载失败：\n{错误}")
