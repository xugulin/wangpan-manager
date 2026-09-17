# 光鸭云盘适配器/界面/页面/分享页.py
"""分享页（列表 / 复制链接 / 取消分享 / 创建分享）"""

import logging
import time

from PySide6.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QMessageBox,
    QTableWidget, QTableWidgetItem, QHeaderView, QSpinBox,
    QApplication,
)
from PySide6.QtCore import Qt

from 核心.接口.分享接口 import (
    分享接口, 分享类型映射, 下载类型映射, 有效期映射,
)

from ..格式化工具 import 格式化时间戳
from .基类 import 页面基类

logger = logging.getLogger("光鸭云盘.界面.分享")


class 分享页(页面基类):
    """分享页"""

    def __init__(self, 主窗口, parent=None):
        super().__init__(主窗口, parent)

        self.列表加载中 = False
        self.上次刷新时间 = 0.0

        self._构建界面()

    def _构建界面(self):
        布局 = QVBoxLayout(self)

        工具条 = QHBoxLayout()
        工具条.addWidget(QLabel("每页："))

        self.每页 = QSpinBox()
        self.每页.setRange(10, 200)
        self.每页.setValue(50)
        self.每页.setSuffix(" 条")
        工具条.addWidget(self.每页)

        self.加载按钮 = QPushButton("🔄 加载/刷新")
        self.加载按钮.setStyleSheet(
            "background: #4CAF50; color: white; padding: 6px 14px; "
            "border-radius: 4px; font-weight: bold;")
        self.加载按钮.clicked.connect(self.加载分享列表)
        工具条.addWidget(self.加载按钮)

        self.复制链接按钮 = QPushButton("📋 复制选中链接")
        self.复制链接按钮.setStyleSheet(
            "background: #2196F3; color: white; padding: 6px 14px; "
            "border-radius: 4px; font-weight: bold;")
        self.复制链接按钮.clicked.connect(self.复制选中分享链接)
        工具条.addWidget(self.复制链接按钮)

        self.取消分享按钮 = QPushButton("🗑 取消选中分享")
        self.取消分享按钮.setStyleSheet(
            "background: #F44336; color: white; padding: 6px 14px; "
            "border-radius: 4px; font-weight: bold;")
        self.取消分享按钮.clicked.connect(self.取消选中分享)
        工具条.addWidget(self.取消分享按钮)

        工具条.addStretch()

        self.状态标签 = QLabel("尚未加载")
        self.状态标签.setStyleSheet(
            "color: #666; padding-left: 10px;")
        工具条.addWidget(self.状态标签)

        布局.addLayout(工具条)

        self.表格 = QTableWidget()
        self.表格.setColumnCount(7)
        self.表格.setHorizontalHeaderLabels(
            ["分享 ID", "标题", "密码类型", "下载方式", "密码",
             "创建时间", "分享链接"])
        self.表格.horizontalHeader().setSectionResizeMode(
            6, QHeaderView.Stretch)
        self.表格.setColumnWidth(0, 200)
        self.表格.setColumnWidth(1, 200)
        self.表格.setColumnWidth(2, 100)
        self.表格.setColumnWidth(3, 100)
        self.表格.setColumnWidth(4, 80)
        self.表格.setColumnWidth(5, 170)
        self.表格.setAlternatingRowColors(True)
        self.表格.setSelectionBehavior(QTableWidget.SelectRows)
        self.表格.setSelectionMode(QTableWidget.ExtendedSelection)
        self.表格.setEditTriggers(QTableWidget.NoEditTriggers)
        self.表格.doubleClicked.connect(self._复制某行分享链接)
        布局.addWidget(self.表格, 1)

        底部 = QLabel(
            "💡 提示：双击某行可复制其分享链接；"
            "选中多行后可点击「取消选中分享」批量取消。")
        底部.setStyleSheet("color: #888; font-size: 11px; padding: 4px;")
        底部.setWordWrap(True)
        布局.addWidget(底部)

    # ---------------- 登录态 ----------------

    def 设置启用(self, 已登录: bool):
        self.加载按钮.setEnabled(已登录)
        self.复制链接按钮.setEnabled(已登录)
        self.取消分享按钮.setEnabled(已登录)

    def 首次进入(self):
        if self.表格.rowCount() == 0:
            self.加载分享列表()

    def 重置(self):
        self.表格.setRowCount(0)
        self.状态标签.setText("尚未加载")

    # ---------------- 创建分享（外部入口） ----------------

    def 执行创建分享(self, 配置: dict):
        """由主窗口 / 文件浏览页调用：根据配置创建分享"""
        self.状态标签.setText("正在创建分享...")
        self.状态标签.setStyleSheet("color: #2196F3;")
        logger.info(
            f"[界面] 创建分享 shareType={配置.get('shareType')} "
            f"files={len(配置.get('fileIds', []))}")

        def 任务():
            接口 = 分享接口(self.网络)
            return 接口.创建分享(**配置)

        def 成功(任务名, 结果):
            shareUrl = 结果.get("shareUrl", "") if isinstance(结果, dict) else ""
            shareId = 结果.get("shareId", "") if isinstance(结果, dict) else ""
            code = 结果.get("code", "") if isinstance(结果, dict) else ""
            logger.info(
                f"[界面] ✅ 分享创建成功 shareId={shareId} "
                f"url={shareUrl}")
            self.状态标签.setText(f"✅ 分享已创建：{shareId}")
            self.状态标签.setStyleSheet("color: #4CAF50;")
            self._显示分享结果(shareUrl, code, shareId)
            if self.表格.rowCount() > 0:
                self.上次刷新时间 = 0.0
                self.加载分享列表()

        def 失败(任务名, 错误):
            logger.error(f"[界面] ❌ 创建分享失败：{错误}")
            self.状态标签.setText(f"❌ 创建分享失败：{错误}")
            self.状态标签.setStyleSheet("color: #F44336;")
            QMessageBox.warning(self, "创建分享失败", 错误)

        self.启动网络任务("创建分享", 任务, 成功, 失败)

    def _显示分享结果(self, shareUrl: str, code: str, shareId: str):
        框 = QMessageBox(self)
        框.setWindowTitle("分享创建成功")
        框.setIcon(QMessageBox.Information)

        文本 = f"分享 ID：{shareId or '—'}\n\n分享链接：\n{shareUrl}"
        if code:
            文本 += f"\n\n提取密码：{code}"
        框.setText(文本)
        框.setTextInteractionFlags(Qt.TextSelectableByMouse)

        复制按钮 = 框.addButton("📋 复制链接", QMessageBox.ActionRole)
        框.addButton("关闭", QMessageBox.AcceptRole)
        框.exec()

        if 框.clickedButton() is 复制按钮:
            QApplication.clipboard().setText(shareUrl)
            QMessageBox.information(self, "提示", "链接已复制到剪贴板")

    # ---------------- 列表 ----------------

    def 加载分享列表(self):
        if not self.网络:
            QMessageBox.warning(self, "提示", "请先登录")
            return
        if self.列表加载中:
            return
        现在 = time.time()
        if 现在 - self.上次刷新时间 < 1.0:
            return
        self.上次刷新时间 = 现在
        self.列表加载中 = True

        每页 = self.每页.value()
        self.加载按钮.setEnabled(False)
        self.状态标签.setText("加载中...")
        self.状态标签.setStyleSheet("color: #2196F3;")

        def 任务():
            接口 = 分享接口(self.网络)
            return 接口.取分享列表(page=0, pageSize=每页)

        self.启动网络任务(
            "分享列表", 任务, self._列表成功, self._列表失败)

    def _列表成功(self, 任务名: str, 数据):
        self.列表加载中 = False
        self.加载按钮.setEnabled(True)

        if not isinstance(数据, dict):
            数据 = {}

        列表 = 数据.get("list") or []
        if not isinstance(列表, list):
            列表 = []

        self.表格.setRowCount(0)
        for 项 in 列表:
            if not isinstance(项, dict):
                continue
            self._追加行(项)

        self.状态标签.setText(
            f"共 {数据.get('total', len(列表))} 条分享")
        self.状态标签.setStyleSheet("color: #4CAF50;")
        logger.info(f"[分享] 列表已加载，共 {len(列表)} 条")

    def _列表失败(self, 任务名: str, 错误: str):
        self.列表加载中 = False
        self.加载按钮.setEnabled(True)
        logger.error(f"[分享] 列表加载失败：{错误}")
        self.状态标签.setText(f"❌ 加载失败：{错误}")
        self.状态标签.setStyleSheet("color: #F44336;")

    def _追加行(self, 项: dict):
        行 = self.表格.rowCount()
        self.表格.insertRow(行)

        def 取(*键列表, 默认=""):
            for 键 in 键列表:
                if 键 in 项 and 项[键] not in (None, ""):
                    return 项[键]
            return 默认

        shareId = str(取("shareId", "share_id"))
        title = str(取("title", "shareName", "share_name", "name",
                       默认="—"))
        shareType = 取("shareType", "share_type", 默认=0)
        downloadType = 取("downloadType", "download_type", 默认=1)
        code = str(取("code", "shareCode", "share_code"))
        createTime = 取("createTime", "create_time", "ctime")
        shareUrl = str(取("shareUrl", "share_url", "url"))
        validateDuration = 取("validateDuration", "validate_duration")

        # ★ 取消分享需用纯数字 id，而不是带后缀 shareId
        原始id = 项.get("id")
        if 原始id not in (None, ""):
            纯id = str(原始id)
        elif shareId:
            纯id = shareId.split("_", 1)[0]
        else:
            纯id = ""

        if isinstance(createTime, (int, float)) and createTime > 1_000_000_000:
            时间字符串 = 格式化时间戳(createTime)
        else:
            时间字符串 = str(createTime or "—")

        类型名 = 分享类型映射.get(shareType, str(shareType))
        if isinstance(validateDuration, (int, float)) \
                and validateDuration in 有效期映射:
            类型名 += f" · {有效期映射[validateDuration]}"
        下载名 = 下载类型映射.get(downloadType, str(downloadType))

        id项 = QTableWidgetItem(shareId)
        id项.setToolTip(
            f"shareId（对外）：{shareId}\n"
            f"id（取消用）：  {纯id}")
        id项.setData(Qt.UserRole, 纯id)
        self.表格.setItem(行, 0, id项)

        self.表格.setItem(行, 1, QTableWidgetItem(title))
        self.表格.setItem(行, 2, QTableWidgetItem(类型名))
        self.表格.setItem(行, 3, QTableWidgetItem(下载名))
        self.表格.setItem(行, 4, QTableWidgetItem(code or "—"))
        self.表格.setItem(行, 5, QTableWidgetItem(时间字符串))

        链接项 = QTableWidgetItem(shareUrl)
        链接项.setData(Qt.UserRole, 项)
        self.表格.setItem(行, 6, 链接项)

    # ---------------- 复制链接 ----------------

    def 复制选中分享链接(self):
        if not self.网络:
            QMessageBox.warning(self, "提示", "请先登录")
            return
        选中行 = self.表格.selectionModel().selectedRows()
        if not 选中行:
            QMessageBox.warning(self, "提示", "请先选中至少一行")
            return

        链接列表 = []
        for 索引 in 选中行:
            项 = self.表格.item(索引.row(), 6)
            if 项 is not None and 项.text():
                链接列表.append(项.text())

        if not 链接列表:
            QMessageBox.warning(self, "提示", "选中的行没有分享链接")
            return

        QApplication.clipboard().setText("\n".join(链接列表))
        QMessageBox.information(
            self, "提示",
            f"已复制 {len(链接列表)} 条链接到剪贴板")

    def _复制某行分享链接(self, 索引):
        项 = self.表格.item(索引.row(), 6)
        if 项 is None or not 项.text():
            return
        QApplication.clipboard().setText(项.text())
        QMessageBox.information(self, "提示", "链接已复制到剪贴板")

    # ---------------- 取消分享 ----------------

    def 取消选中分享(self):
        """取消（删除）选中的分享

        与 HAR 确认的 POST /userres/v1/delete_share 对应：
          请求体 {"ids": ["<纯数字id>"]}
        从第 0 列 UserRole 取纯数字 id（不是表格显示的 shareId）。
        """
        if not self.网络:
            QMessageBox.warning(self, "提示", "请先登录")
            return
        选中行 = self.表格.selectionModel().selectedRows()
        if not 选中行:
            QMessageBox.warning(self, "提示", "请先选中至少一行")
            return

        ids: list[str] = []
        for 索引 in 选中行:
            项 = self.表格.item(索引.row(), 0)
            if 项 is None:
                continue
            纯id = 项.data(Qt.UserRole)
            if 纯id:
                ids.append(str(纯id))
            elif 项.text():
                ids.append(项.text().split("_", 1)[0])

        去重后 = list(dict.fromkeys(i for i in ids if i))
        if not 去重后:
            QMessageBox.warning(self, "提示", "选中的行没有分享 ID")
            return

        应答 = QMessageBox.question(
            self, "取消分享",
            f"确定要取消选中的 {len(去重后)} 个分享吗？\n"
            "取消后分享链接将立即失效。")
        if 应答 != QMessageBox.Yes:
            return

        self.取消分享按钮.setEnabled(False)
        self.状态标签.setText("正在取消...")
        self.状态标签.setStyleSheet("color: #FF9800;")
        logger.info(f"[分享] 取消 ids={去重后}")

        def 任务():
            接口 = 分享接口(self.网络)
            return 接口.取消分享(去重后)

        def 成功(任务名, 结果):
            self.取消分享按钮.setEnabled(True)
            logger.info("[分享] ✅ 取消成功")
            self.状态标签.setText(f"✅ 已取消 {len(去重后)} 个分享")
            self.状态标签.setStyleSheet("color: #4CAF50;")
            self.上次刷新时间 = 0.0
            self.加载分享列表()

        def 失败(任务名, 错误):
            self.取消分享按钮.setEnabled(True)
            logger.error(f"[分享] ❌ 取消失败：{错误}")
            self.状态标签.setText(f"❌ 取消失败：{错误}")
            self.状态标签.setStyleSheet("color: #F44336;")
            QMessageBox.warning(self, "取消分享失败", 错误)

        self.启动网络任务("取消分享", 任务, 成功, 失败)