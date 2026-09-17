# 光鸭云盘适配器/界面/页面/云添加页.py
"""云添加（离线下载）页"""

import logging
import time

from PySide6.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QGroupBox,
    QMessageBox, QTableWidget, QTableWidgetItem, QHeaderView,
    QLineEdit, QCheckBox, QFileDialog,
)
from PySide6.QtCore import Qt, QTimer

from 核心.接口.云添加接口 import 云添加接口, 任务状态映射, 全部状态

from ..格式化工具 import 格式化大小, 格式化时间戳
from .基类 import 页面基类

logger = logging.getLogger("光鸭云盘.界面.云添加")


class 云添加页(页面基类):
    """云添加页"""

    def __init__(self, 主窗口, parent=None):
        super().__init__(主窗口, parent)

        self.刷新中 = False
        self.待提交种子信息: dict | None = None
        self.上次刷新时间 = 0.0

        self._构建界面()

        self.定时器 = QTimer(self)
        self.定时器.setInterval(5000)
        self.定时器.timeout.connect(self.刷新任务)

    def _构建界面(self):
        布局 = QVBoxLayout(self)

        输入组 = QGroupBox("☁ 云添加（离线下载）")
        输入布局 = QVBoxLayout()

        url行 = QHBoxLayout()
        url行.addWidget(QLabel("链接："))
        self.URL输入 = QLineEdit()
        self.URL输入.setPlaceholderText(
            "支持 HTTP/HTTPS 直链，例如 "
            "https://mirrors.tuna.tsinghua.edu.cn/linuxmint.iso")
        self.URL输入.returnPressed.connect(self.添加直链)
        url行.addWidget(self.URL输入, 1)

        self.直链按钮 = QPushButton("➕ 添加直链")
        self.直链按钮.setStyleSheet(
            "background: #4CAF50; color: white; padding: 6px 14px; "
            "border-radius: 4px; font-weight: bold;")
        self.直链按钮.clicked.connect(self.添加直链)
        url行.addWidget(self.直链按钮)
        输入布局.addLayout(url行)

        bt行 = QHBoxLayout()
        bt行.addWidget(QLabel("BT 种子："))
        self.种子标签 = QLabel("（未选择）")
        self.种子标签.setStyleSheet(
            "background: #f5f5f5; padding: 6px; "
            "border: 1px solid #ddd; border-radius: 4px; color: #666;")
        bt行.addWidget(self.种子标签, 1)

        self.选择种子按钮 = QPushButton("📎 选择 .torrent")
        self.选择种子按钮.setStyleSheet(
            "background: #9C27B0; color: white; padding: 6px 14px; "
            "border-radius: 4px; font-weight: bold;")
        self.选择种子按钮.clicked.connect(self.选择种子)
        bt行.addWidget(self.选择种子按钮)

        self.提交BT按钮 = QPushButton("➕ 添加 BT 任务")
        self.提交BT按钮.setStyleSheet(
            "background: #4CAF50; color: white; padding: 6px 14px; "
            "border-radius: 4px; font-weight: bold;")
        self.提交BT按钮.clicked.connect(self.提交BT)
        self.提交BT按钮.setEnabled(False)
        bt行.addWidget(self.提交BT按钮)
        输入布局.addLayout(bt行)

        说明 = QLabel(
            "💡 任务将异步下载到根目录下的「来自：云添加」文件夹。"
            "创建后自动开始下载，可在下方查看进度。")
        说明.setStyleSheet("color: #888; font-size: 11px; padding: 2px;")
        说明.setWordWrap(True)
        输入布局.addWidget(说明)

        输入组.setLayout(输入布局)
        布局.addWidget(输入组)

        列表组 = QGroupBox("任务列表")
        列表布局 = QVBoxLayout()

        工具行 = QHBoxLayout()
        self.刷新按钮 = QPushButton("🔄 刷新")
        self.刷新按钮.setStyleSheet(
            "background: #2196F3; color: white; padding: 6px 14px; "
            "border-radius: 4px; font-weight: bold;")
        self.刷新按钮.clicked.connect(self.刷新任务)
        工具行.addWidget(self.刷新按钮)

        self.自动刷新 = QCheckBox("自动刷新（每 5 秒）")
        self.自动刷新.setChecked(True)
        self.自动刷新.stateChanged.connect(self._切换自动刷新)
        工具行.addWidget(self.自动刷新)

        工具行.addStretch()
        self.状态标签 = QLabel("")
        self.状态标签.setStyleSheet("color: #666; padding-left: 10px;")
        工具行.addWidget(self.状态标签)
        列表布局.addLayout(工具行)

        self.任务表格 = QTableWidget()
        self.任务表格.setColumnCount(6)
        self.任务表格.setHorizontalHeaderLabels(
            ["状态", "文件名", "大小", "进度", "创建时间", "任务 ID"])
        self.任务表格.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.Stretch)
        self.任务表格.setColumnWidth(0, 80)
        self.任务表格.setColumnWidth(2, 100)
        self.任务表格.setColumnWidth(3, 80)
        self.任务表格.setColumnWidth(4, 160)
        self.任务表格.setColumnWidth(5, 200)
        self.任务表格.setAlternatingRowColors(True)
        self.任务表格.setSelectionBehavior(QTableWidget.SelectRows)
        self.任务表格.setEditTriggers(QTableWidget.NoEditTriggers)
        列表布局.addWidget(self.任务表格, 1)

        列表组.setLayout(列表布局)
        布局.addWidget(列表组, 1)

    # ---------------- 登录态 ----------------

    def 设置启用(self, 已登录: bool):
        self.直链按钮.setEnabled(已登录)
        self.选择种子按钮.setEnabled(已登录)
        self.刷新按钮.setEnabled(已登录)
        if not 已登录:
            self.提交BT按钮.setEnabled(False)
            self.定时器.stop()

    def 首次进入(self):
        if self.任务表格.rowCount() == 0:
            self.刷新任务()

    def 重置(self):
        self.任务表格.setRowCount(0)
        self.待提交种子信息 = None
        self.种子标签.setText("（未选择）")
        self.状态标签.setText("")

    def 根据Tab激活状态(self, 激活: bool):
        """Tab 切换时由主窗口调用，控制定时器启停"""
        if not self.网络:
            self.定时器.stop()
            return
        if 激活 and self.自动刷新.isChecked():
            if not self.定时器.isActive():
                self.定时器.start()
        else:
            self.定时器.stop()

    # ---------------- 交互 ----------------

    def _切换自动刷新(self, _):
        # 由主窗口的 Tab 切换逻辑去驱动，这里不直接启动
        pass

    def 添加直链(self):
        if not self.网络:
            QMessageBox.warning(self, "提示", "请先登录")
            return
        url = self.URL输入.text().strip()
        if not url:
            QMessageBox.warning(self, "提示", "请输入链接")
            return
        if not (url.startswith("http://") or url.startswith("https://")):
            QMessageBox.warning(
                self, "提示", "仅支持 http:// 或 https:// 链接")
            return

        self.直链按钮.setEnabled(False)
        self.状态标签.setText("解析链接并创建任务...")
        self.状态标签.setStyleSheet("color: #2196F3;")
        logger.info(f"[界面] 云添加直链：{url}")

        def 任务():
            接口 = 云添加接口(self.网络)
            信息 = 接口.解析直链(url)
            父ID = 接口.确保默认目录()
            taskId = 接口.创建直链任务(url, 父ID)
            return {"taskId": taskId, "信息": 信息}

        def 成功(任务名, 结果):
            self.直链按钮.setEnabled(True)
            taskId = 结果.get("taskId") if isinstance(结果, dict) else None
            信息 = 结果.get("信息", {}) if isinstance(结果, dict) else {}
            文件名 = 信息.get("fileName", url)
            logger.info(
                f"[界面] ✅ 直链任务已创建 taskId={taskId} 文件={文件名}")
            self.状态标签.setText(f"✅ 已添加：{文件名}")
            self.状态标签.setStyleSheet("color: #4CAF50;")
            self.URL输入.clear()
            self.刷新任务()

        def 失败(任务名, 错误):
            self.直链按钮.setEnabled(True)
            logger.error(f"[界面] ❌ 云添加直链失败：{错误}")
            self.状态标签.setText(f"❌ {错误}")
            self.状态标签.setStyleSheet("color: #F44336;")
            QMessageBox.warning(self, "云添加失败", 错误)

        self.启动网络任务("云添加直链", 任务, 成功, 失败)

    def 选择种子(self):
        if not self.网络:
            QMessageBox.warning(self, "提示", "请先登录")
            return

        路径, _ = QFileDialog.getOpenFileName(
            self, "选择 .torrent 种子文件", "",
            "BT 种子 (*.torrent);;所有文件 (*)")
        if not 路径:
            return

        self.选择种子按钮.setEnabled(False)
        self.状态标签.setText("正在解析种子...")
        self.状态标签.setStyleSheet("color: #2196F3;")
        logger.info(f"[界面] 选择种子：{路径}")

        def 任务():
            接口 = 云添加接口(self.网络)
            信息 = 接口.解析种子(路径)
            return {"信息": 信息, "路径": 路径}

        def 成功(任务名, 结果):
            self.选择种子按钮.setEnabled(True)
            信息 = 结果.get("信息", {}) if isinstance(结果, dict) else {}
            文件名 = 信息.get("fileName", "")
            大小 = 信息.get("fileSize", 0)
            infoHash = 信息.get("infoHash", "")
            self.待提交种子信息 = 信息
            self.种子标签.setText(
                f"📄 {文件名}  ({格式化大小(大小)})  "
                f"hash={infoHash[:16]}...")
            self.种子标签.setStyleSheet(
                "background: #E8F5E9; padding: 6px; "
                "border: 1px solid #A5D6A7; border-radius: 4px; "
                "color: #2E7D32;")
            self.提交BT按钮.setEnabled(True)
            self.状态标签.setText(f"✅ 已解析：{文件名}")
            self.状态标签.setStyleSheet("color: #4CAF50;")
            logger.info(
                f"[界面] ✅ 种子解析成功：{文件名} hash={infoHash}")

        def 失败(任务名, 错误):
            self.选择种子按钮.setEnabled(True)
            self.待提交种子信息 = None
            self.提交BT按钮.setEnabled(False)
            self.种子标签.setText("（未选择）")
            self.种子标签.setStyleSheet(
                "background: #f5f5f5; padding: 6px; "
                "border: 1px solid #ddd; border-radius: 4px; "
                "color: #666;")
            logger.error(f"[界面] ❌ 解析种子失败：{错误}")
            self.状态标签.setText(f"❌ {错误}")
            self.状态标签.setStyleSheet("color: #F44336;")
            QMessageBox.warning(self, "解析失败", 错误)

        self.启动网络任务("云添加解析种子", 任务, 成功, 失败)

    def 提交BT(self):
        if not self.网络:
            QMessageBox.warning(self, "提示", "请先登录")
            return
        信息 = self.待提交种子信息
        if not 信息 or not 信息.get("infoHash"):
            QMessageBox.warning(
                self, "提示", "请先选择并解析 .torrent 文件")
            return

        infoHash = 信息["infoHash"]
        fileName = 信息.get("fileName", "")

        self.提交BT按钮.setEnabled(False)
        self.状态标签.setText("创建 BT 任务...")
        self.状态标签.setStyleSheet("color: #2196F3;")
        logger.info(
            f"[界面] 云添加 BT：hash={infoHash} name={fileName}")

        def 任务():
            接口 = 云添加接口(self.网络)
            父ID = 接口.确保默认目录()
            taskId = 接口.创建BT任务(
                infoHash, 父ID, newName=fileName)
            return {"taskId": taskId}

        def 成功(任务名, 结果):
            self.提交BT按钮.setEnabled(True)
            taskId = 结果.get("taskId") if isinstance(结果, dict) else None
            logger.info(f"[界面] ✅ BT 任务已创建 taskId={taskId}")
            self.状态标签.setText(f"✅ 已添加 BT：{fileName}")
            self.状态标签.setStyleSheet("color: #4CAF50;")
            self.待提交种子信息 = None
            self.种子标签.setText("（未选择）")
            self.种子标签.setStyleSheet(
                "background: #f5f5f5; padding: 6px; "
                "border: 1px solid #ddd; border-radius: 4px; "
                "color: #666;")
            self.刷新任务()

        def 失败(任务名, 错误):
            self.提交BT按钮.setEnabled(True)
            logger.error(f"[界面] ❌ 创建 BT 任务失败：{错误}")
            self.状态标签.setText(f"❌ {错误}")
            self.状态标签.setStyleSheet("color: #F44336;")
            QMessageBox.warning(self, "云添加失败", 错误)

        self.启动网络任务("云添加提交BT", 任务, 成功, 失败)

    def 刷新任务(self):
        if not self.网络:
            return
        if self.刷新中:
            return
        现在 = time.time()
        if 现在 - self.上次刷新时间 < 1.0:
            return
        self.上次刷新时间 = 现在
        self.刷新中 = True

        def 任务():
            接口 = 云添加接口(self.网络)
            return 接口.查询任务(status=全部状态, pageSize=100)

        def 成功(任务名, 数据):
            self.刷新中 = False
            if not isinstance(数据, dict):
                return
            列表 = 数据.get("list", []) or []
            self._渲染任务(列表)
            self.状态标签.setText(
                f"共 {数据.get('total', len(列表))} 条任务")
            self.状态标签.setStyleSheet("color: #666;")

        def 失败(任务名, 错误):
            self.刷新中 = False
            logger.error(f"[界面] 云添加任务列表失败：{错误}")
            self.状态标签.setText(f"❌ 刷新失败：{错误}")
            self.状态标签.setStyleSheet("color: #F44336;")

        self.启动网络任务("云添加任务列表", 任务, 成功, 失败)

    def _渲染任务(self, 列表: list):
        表格 = self.任务表格
        滚动 = 表格.verticalScrollBar().value()
        表格.setRowCount(0)

        for 项 in 列表:
            行 = 表格.rowCount()
            表格.insertRow(行)

            状态码 = 项.get("status", 0)
            状态名 = 任务状态映射.get(状态码, str(状态码))
            文件名 = 项.get("fileName", "—")
            大小 = 项.get("totalSize", 0) or 0
            进度 = 项.get("progress", 0)
            try:
                进度 = int(进度)
            except Exception:
                进度 = 0
            时间戳 = 项.get("createTime")
            任务ID = str(项.get("taskId", ""))
            资源 = 项.get("res", "")

            emoji = {
                0: "⏳", 1: "⬇️", 2: "✅", 3: "❌", 4: "⏸", 5: "🗑"
            }.get(状态码, "❓")
            状态项 = QTableWidgetItem(f"{emoji} {状态名}")
            状态项.setToolTip(f"状态码：{状态码}")
            表格.setItem(行, 0, 状态项)

            名称项 = QTableWidgetItem(文件名)
            名称项.setToolTip(
                f"文件名：{文件名}\n"
                f"任务 ID：{任务ID}\n"
                f"资源：{资源[:200]}")
            表格.setItem(行, 1, 名称项)

            表格.setItem(行, 2, QTableWidgetItem(格式化大小(大小)))

            进度项 = QTableWidgetItem(f"{进度}%")
            if 状态码 == 2:
                进度项.setText("100%")
            表格.setItem(行, 3, 进度项)

            时间字符串 = (
                格式化时间戳(时间戳)
                if isinstance(时间戳, (int, float))
                else str(时间戳 or "—")
            )
            表格.setItem(行, 4, QTableWidgetItem(时间字符串))
            表格.setItem(行, 5, QTableWidgetItem(任务ID))

        try:
            表格.verticalScrollBar().setValue(滚动)
        except Exception:
            pass