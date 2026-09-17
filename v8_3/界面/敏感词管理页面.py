"""敏感词管理页面（切换页）。

沿用 网盘管理_V8「敏感词管理页面」的两张表（敏感词 / 改名记录）与底部统计栏，
并按 V8_3 的实际结构做了这些改动：

* 「网盘」列用**网盘实例标识**（quark / quark_2…），同一家网盘多账号各学各的；
* 顶部加了开关：敏感词总开关、上传前自动改名（改完会保存配置并重建上传守卫）；
* 底部统计栏除了词/改名数量，还显示上传守卫的运行计数（预检改名、绕过成功…）；
* 改名记录支持按"云端文件名"反查原名（下载还原名字用）。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QHBoxLayout,
    QHeaderView, QInputDialog, QLabel, QLineEdit, QMessageBox, QPushButton,
    QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)

from ..配置 import 敏感词配置, 设置界面配置, 保存配置, 网盘实例列表
from ..核心.模型 import 标识文本


class 敏感词管理页面(QWidget):
    def __init__(self, 主窗口, 父=None):
        super().__init__(父)
        self.主窗口 = 主窗口
        self.动作 = 主窗口.动作
        self.配置 = 主窗口.配置
        self._构建()
        self.刷新()

    # ==================== 界面 ====================

    def _构建(self):
        布局 = QVBoxLayout(self)
        布局.setContentsMargins(0, 0, 0, 0)
        布局.setSpacing(8)

        标题行 = QHBoxLayout()
        标题 = QLabel("🔒 敏感词管理")
        标题.setStyleSheet("font-size: 16px; font-weight: bold;")
        标题行.addWidget(标题)
        标题行.addStretch(1)
        刷新按钮 = QPushButton("🔄 刷新")
        刷新按钮.clicked.connect(self.刷新)
        标题行.addWidget(刷新按钮)
        打开目录 = QPushButton("📂 打开词库目录")
        打开目录.clicked.connect(self._打开词库目录)
        标题行.addWidget(打开目录)
        布局.addLayout(标题行)

        说明 = QLabel(
            "💡 上传前会先扫描文件名里的敏感词并改名上传；上传被拦时会自动做"
            "探测式绕过（P1 已知词 → P2 单段 → P3 组合 → P4 全中文化），"
            "学到的敏感词与安全词都记在这里。改名记录用于下载还原原名。")
        说明.setWordWrap(True)
        说明.setStyleSheet("color: #95a5a6; font-size: 12px;")
        布局.addWidget(说明)

        开关行 = QHBoxLayout()
        self.启用框 = QCheckBox("启用敏感词功能（上传前预检改名 + 失败绕过）")
        self.启用框.toggled.connect(self._开关变化)
        开关行.addWidget(self.启用框)
        self.自动改名框 = QCheckBox("上传前自动改名（不改内容，只换安全词）")
        self.自动改名框.toggled.connect(self._开关变化)
        开关行.addWidget(self.自动改名框)
        开关行.addStretch(1)
        布局.addLayout(开关行)

        self.tabs = QTabWidget()
        布局.addWidget(self.tabs, 1)

        # ---- Tab1：敏感词 ----
        词页 = QWidget()
        词布局 = QVBoxLayout(词页)
        词工具 = QHBoxLayout()
        添加按钮 = QPushButton("➕ 添加敏感词")
        添加按钮.clicked.connect(self._添加敏感词)
        词工具.addWidget(添加按钮)
        删除按钮 = QPushButton("🗑 删除选中")
        删除按钮.clicked.connect(self._删除敏感词)
        词工具.addWidget(删除按钮)
        候选按钮 = QPushButton("🎲 看候选安全词")
        候选按钮.setToolTip("查看某个敏感词当前可用的候选安全词（P1–P4 会用它们）")
        候选按钮.clicked.connect(self._查看候选安全词)
        词工具.addWidget(候选按钮)
        词工具.addStretch(1)
        self.词统计标签 = QLabel("")
        词工具.addWidget(self.词统计标签)
        词布局.addLayout(词工具)

        self.敏感词表格 = QTableWidget()
        self.敏感词表格.setColumnCount(6)
        self.敏感词表格.setHorizontalHeaderLabels(
            ["网盘(实例)", "敏感词", "最后一次成功安全词", "已失效安全词",
             "来源", "发现时间"])
        self.敏感词表格.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.Stretch)
        for 列, 宽 in ((0, 200), (2, 170), (3, 170), (4, 90), (5, 170)):
            self.敏感词表格.setColumnWidth(列, 宽)
        self.敏感词表格.setAlternatingRowColors(True)
        self.敏感词表格.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.敏感词表格.setSelectionBehavior(QAbstractItemView.SelectRows)
        词布局.addWidget(self.敏感词表格)
        self.tabs.addTab(词页, "🚫 敏感词")

        # ---- Tab2：改名记录 ----
        改名页 = QWidget()
        改名布局 = QVBoxLayout(改名页)
        改名工具 = QHBoxLayout()
        反查按钮 = QPushButton("🔍 按云端文件名反查原名")
        反查按钮.clicked.connect(self._反查原名)
        改名工具.addWidget(反查按钮)
        删记录按钮 = QPushButton("🗑 删除选中记录")
        删记录按钮.clicked.connect(self._删除改名记录)
        改名工具.addWidget(删记录按钮)
        改名工具.addStretch(1)
        self.改名统计标签 = QLabel("")
        改名工具.addWidget(self.改名统计标签)
        改名布局.addLayout(改名工具)

        self.改名表格 = QTableWidget()
        self.改名表格.setColumnCount(7)
        self.改名表格.setHorizontalHeaderLabels(
            ["网盘(实例)", "原文件名", "云端文件名", "命中的敏感词",
             "改名策略", "状态", "创建时间"])
        self.改名表格.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.Stretch)
        for 列, 宽 in ((0, 200), (2, 240), (3, 150), (4, 110), (5, 80), (6, 170)):
            self.改名表格.setColumnWidth(列, 宽)
        self.改名表格.setAlternatingRowColors(True)
        self.改名表格.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.改名表格.setSelectionBehavior(QAbstractItemView.SelectRows)
        改名布局.addWidget(self.改名表格)
        self.tabs.addTab(改名页, "📝 改名记录")

        # ---- 底部统计栏（V8 风格的一条横栏）----
        self.统计标签 = QLabel("")
        self.统计标签.setStyleSheet(
            "background: #2c3e50; color: #ecf0f1; padding: 8px; "
            "border-radius: 4px; font-size: 12px;")
        self.统计标签.setWordWrap(True)
        布局.addWidget(self.统计标签)

    # ==================== 数据 ====================

    @property
    def 数据库(self):
        return self.动作.敏感词库()

    def _实例标签(self, 标识: str) -> str:
        if not 标识:
            return "（未指定）"
        名称 = None
        for 项 in 网盘实例列表(self.配置):
            if 项["标识"] == 标识:
                名称 = 项["名称"]
                break
        return f"{名称}（{标识}）" if 名称 else 标识

    def _解析实例文本(self, 文本: str) -> str:
        """把界面上的「名称（标识）」还原成标识。"""
        文本 = str(文本 or "").strip()
        if "（" in 文本 and 文本.endswith("）"):
            return 标识文本(文本[文本.rfind("（") + 1:-1])
        return 标识文本(文本)

    def 刷新(self):
        参数 = 敏感词配置(self.配置)
        self.启用框.blockSignals(True)
        self.自动改名框.blockSignals(True)
        self.启用框.setChecked(参数["启用"])
        self.自动改名框.setChecked(参数["自动改名上传"])
        self.启用框.blockSignals(False)
        self.自动改名框.blockSignals(False)
        self._刷新敏感词表()
        self._刷新改名表()
        self._刷新统计()

    def _刷新敏感词表(self):
        库 = self.数据库
        列表 = 库.列出敏感词() if 库 is not None else []
        列表 = sorted(列表, key=lambda x: (x.get("网盘名称", ""),
                                       x.get("敏感词", "")))
        表格 = self.敏感词表格
        表格.setUpdatesEnabled(False)
        try:
            表格.setRowCount(len(列表))
            for i, info in enumerate(列表):
                失效集合 = info.get("已失效安全词", {}) or {}
                失效文本 = "、".join(list(失效集合.keys())[:5])
                if len(失效集合) > 5:
                    失效文本 += f" 等 {len(失效集合)} 个"
                列 = [
                    self._实例标签(info.get("网盘名称", "")),
                    info.get("敏感词", ""),
                    info.get("最后一次成功安全词", "") or "—",
                    失效文本 or "—",
                    info.get("来源", "") or "—",
                    info.get("发现时间", ""),
                ]
                for j, 文本 in enumerate(列):
                    项 = 表格.item(i, j)
                    if 项 is None:
                        项 = QTableWidgetItem()
                        表格.setItem(i, j, 项)
                    if 项.text() != 文本:
                        项.setText(文本)
        finally:
            表格.setUpdatesEnabled(True)
        self.词统计标签.setText(f"共 {len(列表)} 个敏感词")

    def _刷新改名表(self):
        库 = self.数据库
        列表 = 库.列出改名记录(状态="") if 库 is not None else []
        表格 = self.改名表格
        表格.setUpdatesEnabled(False)
        try:
            表格.setRowCount(len(列表))
            for i, info in enumerate(列表):
                列 = [
                    self._实例标签(info.get("网盘名称", "")),
                    info.get("原文件名", ""),
                    info.get("云端文件名", ""),
                    info.get("命中的敏感词", "") or "—",
                    info.get("改名策略", ""),
                    info.get("状态", "") or "—",
                    info.get("创建时间", ""),
                ]
                for j, 文本 in enumerate(列):
                    项 = 表格.item(i, j)
                    if 项 is None:
                        项 = QTableWidgetItem()
                        表格.setItem(i, j, 项)
                    if 项.text() != 文本:
                        项.setText(文本)
        finally:
            表格.setUpdatesEnabled(True)
        self.改名统计标签.setText(f"共 {len(列表)} 条改名记录")

    def _刷新统计(self):
        库 = self.数据库
        if 库 is None:
            self.统计标签.setText(
                "⚠️ 敏感词功能当前是关闭的（或词库打不开）："
                "勾选上方开关即可启用，上传会照常进行、不做改名。")
            return
        try:
            统计 = 库.获取统计() or {}
        except Exception:
            统计 = {}
        词列表 = 库.列出敏感词()
        有安全词 = sum(1 for x in 词列表 if x.get("最后一次成功安全词"))
        改名数 = len(库.列出改名记录(状态=""))
        守卫统计 = {}
        for 标识 in (self.动作.规格表 or {}):
            守卫 = self.动作.守卫(标识)
            if 守卫 is not None:
                for 键, 值 in 守卫.统计().items():
                    守卫统计[键] = 守卫统计.get(键, 0) + int(值)
        守卫文本 = "、".join(f"{k} {v}" for k, v in 守卫统计.items() if v) or "暂无"
        命中 = 统计.get("预检命中", 0)
        成功 = 统计.get("安全词成功", 0)
        self.统计标签.setText(
            f"📊 敏感词 {len(词列表)} 个（有安全词 {有安全词}） | "
            f"改名记录 {改名数} 条 | 累计命中 {命中} | 安全词生效 {成功} | "
            f"上传守卫：{守卫文本}")

    # ==================== 开关 ====================

    def _开关变化(self, *_):
        配置 = dict(self.配置.get("敏感词") or {})
        配置["启用"] = bool(self.启用框.isChecked())
        配置["自动改名上传"] = bool(self.自动改名框.isChecked())
        self.配置["敏感词"] = 配置
        保存配置(self.配置, getattr(self.主窗口, "配置路径", None))
        # 让守卫按新配置重建
        for 标识 in list(self.动作.规格表):
            self.动作.移除守卫(标识)
        self.主窗口.追加日志(
            f"敏感词功能：{'开启' if 配置['启用'] else '关闭'}，"
            f"上传前自动改名：{'开' if 配置['自动改名上传'] else '关'}")
        self.刷新()

    # ==================== 敏感词操作 ====================

    def _添加敏感词(self):
        库 = self.数据库
        if 库 is None:
            QMessageBox.warning(self, "提示", "请先启用敏感词功能")
            return
        对话框 = QDialog(self)
        对话框.setWindowTitle("添加敏感词")
        对话框.setMinimumWidth(420)
        布局 = QVBoxLayout(对话框)
        布局.addWidget(QLabel("网盘实例："))
        网盘框 = QComboBox()
        已有 = {x["网盘名称"] for x in 库.列出敏感词()}
        for 项 in 网盘实例列表(self.配置):
            网盘框.addItem(self._实例标签(项["标识"]), 项["标识"])
        for 标识 in sorted(已有 - {x["标识"] for x in 网盘实例列表(self.配置)}):
            网盘框.addItem(self._实例标签(标识), 标识)
        布局.addWidget(网盘框)
        布局.addWidget(QLabel("敏感词："))
        敏感词框 = QLineEdit()
        布局.addWidget(敏感词框)
        布局.addWidget(QLabel("安全词（可选，留空则自动生成候选）："))
        安全词框 = QLineEdit()
        布局.addWidget(安全词框)
        按钮行 = QHBoxLayout()
        按钮行.addStretch(1)
        确定 = QPushButton("确定")
        确定.setObjectName("PrimaryButton")
        取消 = QPushButton("取消")
        按钮行.addWidget(确定)
        按钮行.addWidget(取消)
        布局.addLayout(按钮行)

        def 提交():
            标识 = 标识文本(网盘框.currentData() or "")
            词 = 敏感词框.text().strip()
            if not 标识 or not 词:
                QMessageBox.warning(对话框, "提示", "网盘和敏感词都不能为空")
                return
            库.添加敏感词(标识, 词, 安全词框.text().strip())
            对话框.accept()
            self.刷新()

        确定.clicked.connect(提交)
        取消.clicked.connect(对话框.reject)
        对话框.exec()

    def _删除敏感词(self):
        库 = self.数据库
        选中 = self.敏感词表格.selectionModel().selectedRows()
        if 库 is None or not 选中:
            QMessageBox.information(self, "提示", "请先选中一行")
            return
        行 = 选中[0].row()
        标识 = self._解析实例文本(self.敏感词表格.item(行, 0).text())
        词 = self.敏感词表格.item(行, 1).text()
        if QMessageBox.question(
                self, "确认删除",
                f"删除敏感词「{词}」（网盘 {self._实例标签(标识)}）？"
        ) != QMessageBox.Yes:
            return
        库.删除敏感词(标识, 词)
        self.刷新()

    def _查看候选安全词(self):
        库 = self.数据库
        选中 = self.敏感词表格.selectionModel().selectedRows()
        if 库 is None or not 选中:
            QMessageBox.information(self, "提示", "请先选中一行")
            return
        行 = 选中[0].row()
        标识 = self._解析实例文本(self.敏感词表格.item(行, 0).text())
        词 = self.敏感词表格.item(行, 1).text()
        候选 = 库.生成候选安全词列表(标识, 词)
        信息 = 库.获取敏感词(标识, 词) or {}
        QMessageBox.information(
            self, f"候选安全词 · {词}",
            f"网盘：{self._实例标签(标识)}\n"
            f"当前成功安全词：{信息.get('最后一次成功安全词') or '（还没有）'}\n"
            f"已失效：{'、'.join((信息.get('已失效安全词') or {}).keys()) or '无'}\n\n"
            f"候选（按优先级）：\n" + "\n".join(f"  {i+1}. {x}"
                                        for i, x in enumerate(候选[:15])))

    # ==================== 改名记录操作 ====================

    def _反查原名(self):
        库 = self.数据库
        if 库 is None:
            QMessageBox.warning(self, "提示", "请先启用敏感词功能")
            return
        云端名, ok = QInputDialog.getText(
            self, "反查原名", "云端文件名（或它的完整路径）：")
        if not ok or not 云端名.strip():
            return
        查询 = 云端名.strip()
        候选网盘 = [x["标识"] for x in 网盘实例列表(self.配置)]
        命中 = []
        for 标识 in 候选网盘:
            try:
                原名 = 库.反查原文件名(标识, 查询)
            except Exception:
                原名 = None
            if 原名:
                命中.append((标识, 原名))
        if not 命中:
            行 = []
            for info in 库.列出改名记录(状态="")[:200]:
                if 查询 in str(info.get("云端文件名") or "") or \
                        查询 in str(info.get("云端实际路径") or ""):
                    行.append(f"  {info.get('原文件名')} ← {info.get('云端文件名')}"
                            f"（{info.get('网盘名称')}）")
            QMessageBox.information(
                self, "反查结果",
                f"云端：{查询}\n没有直接命中的改名记录。"
                + ("\n\n模糊匹配：\n" + "\n".join(行[:20]) if 行 else ""))
            return
        文本 = "\n".join(f"  {self._实例标签(标识)}：{原名}"
                       for 标识, 原名 in 命中)
        QMessageBox.information(self, "反查结果", f"云端：{查询}\n原名：\n{文本}")

    def _删除改名记录(self):
        库 = self.数据库
        选中 = self.改名表格.selectionModel().selectedRows()
        if 库 is None or not 选中:
            QMessageBox.information(self, "提示", "请先选中一行")
            return
        行 = 选中[0].row()
        标识 = self._解析实例文本(self.改名表格.item(行, 0).text())
        云端名 = self.改名表格.item(行, 2).text()
        原路径 = self.改名表格.item(行, 1).text()
        if QMessageBox.question(
                self, "确认删除",
                f"删除改名记录「{云端名}」？"
        ) != QMessageBox.Yes:
            return
        try:
            # 删除按「原云端路径」定位，回退用原文件名
            删除成功 = 库.删除改名记录(标识, 原路径)
            if not 删除成功:
                for info in 库.列出改名记录(状态=""):
                    if info.get("云端文件名") == 云端名:
                        库.删除改名记录(标识, info.get("原云端路径"))
                        break
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "删除失败", str(e))
        self.刷新()

    # ==================== 目录 ====================

    def _打开词库目录(self):
        路径 = Path(敏感词配置(self.配置)["数据库路径"]).parent
        路径.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(路径)))
