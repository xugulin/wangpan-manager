# 夸克网盘适配器/界面/页面/分享页.py
"""分享页（列表 / 复制链接 / 取消分享 / 创建分享）"""

import logging
import time

from PySide6.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QMessageBox,
    QTableWidget, QTableWidgetItem, QHeaderView, QSpinBox,
    QApplication, QInputDialog,
)
from PySide6.QtCore import Qt

from 核心.接口.分享接口 import 分享接口

# 阶段二 2.9：展示常量已从 分享接口 迁到 字段映射（表现层）
# 阶段三 3.6：删掉「下载方式」列后，下载类型映射已不再使用
from ..字段映射 import 分享类型映射, 有效期映射, 分享状态映射

from ..格式化工具 import 格式化时间戳
from .基类 import 页面基类

logger = logging.getLogger("夸克网盘.界面.分享")


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

        # 阶段三 3.6：夸克特性 —— 从分享链接转存到自己的网盘
        self.转存按钮 = QPushButton("📥 转存分享链接")
        self.转存按钮.setStyleSheet(
            "background: #9C27B0; color: white; padding: 6px 14px; "
            "border-radius: 4px; font-weight: bold;")
        self.转存按钮.setToolTip(
            "粘贴他人的夸克分享链接，一键转存到自己的网盘根目录")
        self.转存按钮.clicked.connect(self.转存分享链接)
        工具条.addWidget(self.转存按钮)

        # 阶段三修复：`GET share/mypage/detail` **不返回** `passcode`，
        # 提取码只能由 `POST share/password` 单个查询，所以做成按需拉取。
        self.提取码按钮 = QPushButton("🔑 取选中项提取码")
        self.提取码按钮.setStyleSheet(
            "background: #FF9800; color: white; padding: 6px 14px; "
            "border-radius: 4px; font-weight: bold;")
        self.提取码按钮.setToolTip(
            "夸克分享列表接口不返回提取码，点此按需查询选中行的提取码")
        self.提取码按钮.clicked.connect(self.取选中提取码)
        工具条.addWidget(self.提取码按钮)

        工具条.addStretch()

        self.状态标签 = QLabel("尚未加载")
        self.状态标签.setStyleSheet(
            "color: #666; padding-left: 10px;")
        工具条.addWidget(self.状态标签)

        布局.addLayout(工具条)

        self.表格 = QTableWidget()
        # 阶段三 3.6：删掉夸克不存在的「下载方式」列（7 列 → 6 列）
        self.表格.setColumnCount(6)
        self.表格.setHorizontalHeaderLabels(
            ["分享 ID", "标题", "密码类型", "密码", "创建时间", "分享链接"])
        self.表格.horizontalHeader().setSectionResizeMode(
            5, QHeaderView.Stretch)
        self.表格.setColumnWidth(0, 210)
        self.表格.setColumnWidth(1, 200)
        self.表格.setColumnWidth(2, 100)
        self.表格.setColumnWidth(3, 80)
        self.表格.setColumnWidth(4, 170)
        self.表格.setAlternatingRowColors(True)
        self.表格.setSelectionBehavior(QTableWidget.SelectRows)
        self.表格.setSelectionMode(QTableWidget.ExtendedSelection)
        self.表格.setEditTriggers(QTableWidget.NoEditTriggers)
        self.表格.doubleClicked.connect(self._复制某行分享链接)
        布局.addWidget(self.表格, 1)

        底部 = QLabel(
            "💡 提示：双击某行可复制其分享链接；选中多行后可批量取消分享；"
            "提取码需点「🔑 取选中项提取码」按需查询（列表接口不返回）；"
            "「转存分享链接」可把别人的分享保存到自己的网盘。")
        底部.setStyleSheet("color: #888; font-size: 11px; padding: 4px;")
        底部.setWordWrap(True)
        布局.addWidget(底部)

    # ---------------- 登录态 ----------------

    def 设置启用(self, 已登录: bool):
        self.加载按钮.setEnabled(已登录)
        self.复制链接按钮.setEnabled(已登录)
        self.取消分享按钮.setEnabled(已登录)
        self.转存按钮.setEnabled(已登录)
        self.提取码按钮.setEnabled(已登录)

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
            shareUrl = 结果.get("share_url") or 结果.get("shareUrl", "") if isinstance(结果, dict) else ""
            shareId = 结果.get("share_id") or 结果.get("shareId", "") if isinstance(结果, dict) else ""
            code = 结果.get("passcode") or 结果.get("code", "") if isinstance(结果, dict) else ""
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
        """把一条 `share/mypage/detail` 记录渲染成表格行

        夸克记录的关键字段（2026-09-14 实测）：
            `share_id` / `pwd_id` / `share_url` / `title` / `passcode` /
            `status` / `first_fid` / `created_at` / `updated_at` /
            `expired_at` / `file_num`
        注意 `created_at` / `updated_at` / `expired_at` 都是**毫秒**时间戳。

        ⚠️ 整个渲染过程包在 try 里：字段缺失只会显示占位符，
        绝不让单条脏数据把整个列表拖崩（历史 bug：
        `NameError: name '时间字符串' is not defined`）。
        """
        行 = self.表格.rowCount()
        self.表格.insertRow(行)

        def 取(*键列表, 默认=None):
            for 键 in 键列表:
                if isinstance(项, dict) and 键 in 项 \
                        and 项[键] not in (None, ""):
                    return 项[键]
            return 默认

        def 时间(值) -> str:
            if 值 in (None, ""):
                return "—"
            if isinstance(值, (int, float)) and 值 > 1_000_000_000:
                return 格式化时间戳(值)      # 内部已能识别毫秒
            return str(值)

        try:
            shareId = str(取("share_id", "shareId") or "")
            title = str(取("title", "share_name", "shareName") or "—")
            shareUrl = str(取("share_url", "shareUrl", "url") or "")
            code = str(取("passcode", "code", "shareCode") or "")
            shareType = 取("share_type", "shareType", 默认=0)
            validateDuration = 取("validate_duration", "validateDuration")
            createdAt = 取("created_at", "createdAt", "ctime", "create_time")
            expiredAt = 取("expired_at", "expire_at")
            fileNum = 取("file_num", "fileNum")
            status = 取("status")

            # share_url 缺失时用 URL slug 兜底
            if not shareUrl and shareId:
                链接源 = str(取("pwd_id") or shareId)
                shareUrl = 分享接口.生成分享URL(链接源)

            时间字符串 = 时间(createdAt)
            if expiredAt:
                时间字符串 += f"（{时间(expiredAt)} 到期）"

            类型名 = 分享类型映射.get(
                shareType, "—" if shareType in (None, "") else str(shareType))
            if isinstance(validateDuration, (int, float)) \
                    and validateDuration in 有效期映射:
                类型名 += f" · {有效期映射[validateDuration]}"
            if fileNum not in (None, ""):
                类型名 += f" · {fileNum} 个文件"
            if status in (2, 3):
                类型名 += f" · {分享状态映射.get(status, status)}"
        except Exception as e:
            logger.exception(f"[分享] 行渲染异常：{e}")
            shareId, title, 类型名, code, 时间字符串, shareUrl = ("—",) * 6

        id项 = QTableWidgetItem(shareId or "—")
        id项.setToolTip(
            f"share_id（管理/取消用）：{shareId}\n"
            f"URL slug（访问/转存用）：{分享接口.链接ID(shareUrl)}")
        # 取消分享用 share_id（夸克无「纯数字 id」概念）
        id项.setData(Qt.UserRole, shareId)
        self.表格.setItem(行, 0, id项)

        self.表格.setItem(行, 1, QTableWidgetItem(title))
        self.表格.setItem(行, 2, QTableWidgetItem(类型名))
        密码项 = QTableWidgetItem(code or "（点 🔑 查询）")
        密码项.setToolTip(
            "夸克分享列表接口不返回提取码；\n"
            "点工具条「🔑 取选中项提取码」按需查询。")
        self.表格.setItem(行, 3, 密码项)
        self.表格.setItem(行, 4, QTableWidgetItem(时间字符串))

        链接项 = QTableWidgetItem(shareUrl)
        链接项.setData(Qt.UserRole, 项)
        self.表格.setItem(行, 5, 链接项)

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
            项 = self.表格.item(索引.row(), 5)
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
        项 = self.表格.item(索引.row(), 5)
        if 项 is None or not 项.text():
            return
        QApplication.clipboard().setText(项.text())
        QMessageBox.information(self, "提示", "链接已复制到剪贴板")

    # ---------------- 提取码 ----------------

    def 取选中提取码(self):
        """按需查询选中行的提取码并填回「密码」列

        ⚠️ 为什么不能随列表一起显示：`GET share/mypage/detail` 的返回里
        **根本没有 `passcode` 字段**（实测键名见 `_追加行` 的文档），
        提取码只能通过 `POST share/password` 逐条查询，因此做成手动触发。
        """
        if not self.网络:
            QMessageBox.warning(self, "提示", "请先登录")
            return
        选中行 = self.表格.selectionModel().selectedRows()
        if not 选中行:
            QMessageBox.warning(self, "提示", "请先选中至少一行")
            return

        行号列表 = sorted(索引.row() for 索引 in 选中行)
        self.提取码按钮.setEnabled(False)
        self.状态标签.setText(f"正在查询 {len(行号列表)} 条提取码…")
        self.状态标签.setStyleSheet("color: #FF9800;")

        def 任务():
            接口 = 分享接口(self.网络)
            结果: dict[int, str] = {}
            for 行 in 行号列表:
                项 = self.表格.item(行, 0)
                if 项 is None:
                    continue
                shareId = str(项.data(Qt.UserRole) or 项.text() or "")
                if not shareId:
                    continue
                try:
                    详情 = 接口.取分享详情(shareId)
                except Exception as e:
                    logger.warning(f"[分享] 取提取码失败 share_id={shareId}：{e}")
                    结果[行] = "查询失败"
                    continue
                结果[行] = str(详情.get("passcode") or "无（公开分享）")
            return 结果

        def 成功(任务名, 结果):
            self.提取码按钮.setEnabled(True)
            数量 = 0
            for 行, 提取码 in (结果 or {}).items():
                if 行 >= self.表格.rowCount():
                    continue
                self.表格.setItem(行, 3, QTableWidgetItem(提取码))
                数量 += 1
            self.状态标签.setText(f"✅ 已更新 {数量} 条提取码")
            self.状态标签.setStyleSheet("color: #4CAF50;")

        def 失败(任务名, 错误):
            self.提取码按钮.setEnabled(True)
            logger.error(f"[分享] 查询提取码失败：{错误}")
            self.状态标签.setText(f"❌ 查询提取码失败：{错误}")
            self.状态标签.setStyleSheet("color: #F44336;")

        self.启动网络任务("查询提取码", 任务, 成功, 失败)

    # ---------------- 取消分享 ----------------

    def 取消选中分享(self):
        """取消（删除）选中的分享

        阶段三 3.6：夸克的 `POST share/delete` 用 `share_id`
        （32 位十六进制，从第 0 列 UserRole 取），
        不再是原适配器的「纯数字 id」。
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
            # `取消分享` 现在会回查确认，所以这里能分辨「真删了」和「服务端假成功」
            条目 = 结果 if isinstance(结果, list) else []
            已删 = [x for x in 条目 if x.get("已删除")]
            未删 = [x for x in 条目 if x.get("已删除") is False]
            if 未删:
                logger.error(f"[分享] ❌ 服务端报成功但未生效：{len(未删)} 条")
                self.状态标签.setText(
                    f"⚠️ 已取消 {len(已删)} 条，{len(未删)} 条未生效")
                self.状态标签.setStyleSheet("color: #FF9800;")
                QMessageBox.warning(
                    self, "取消分享",
                    f"有 {len(未删)} 条分享服务端返回成功但实际未删除，\n"
                    "请稍后刷新列表确认。")
            else:
                logger.info(f"[分享] ✅ 已确认取消 {len(已删)} 条")
                self.状态标签.setText(f"✅ 已取消 {len(已删)} 个分享")
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

    # ---------------- 转存分享链接（夸克特性，3.6）----------------

    def 转存分享链接(self):
        """把别人的夸克分享链接转存到自己的网盘根目录

        走后端的 `分享接口.一键转存()`：
            解析链接 → sharepage/token → sharepage/save → 轮询 task
        """
        if not self.网络:
            QMessageBox.warning(self, "提示", "请先登录")
            return

        链接, 确认 = QInputDialog.getText(
            self, "转存分享链接",
            "请粘贴夸克分享链接（可带提取码，例如：\n"
            "https://pan.quark.cn/s/xxxxxxxx 密码：1234）：")
        if not 确认 or not 链接.strip():
            return

        self.转存按钮.setEnabled(False)
        self.状态标签.setText("正在转存…")
        self.状态标签.setStyleSheet("color: #FF9800;")
        文本 = 链接.strip()

        def 任务():
            接口 = 分享接口(self.网络)
            return 接口.一键转存(文本, 目标目录ID="0")

        def 成功(任务名, 结果):
            self.转存按钮.setEnabled(True)
            sid = (结果 or {}).get("share_id", "")
            self.状态标签.setText(f"✅ 已转存（分享 {sid}）")
            self.状态标签.setStyleSheet("color: #4CAF50;")
            QMessageBox.information(
                self, "转存成功",
                f"已转存到网盘根目录。\n分享 ID：{sid}\n\n"
                "可切到「📁 文件浏览」查看。")

        def 失败(任务名, 错误):
            self.转存按钮.setEnabled(True)
            self.状态标签.setText(f"❌ 转存失败：{错误}")
            self.状态标签.setStyleSheet("color: #F44336;")
            QMessageBox.warning(self, "转存失败", 错误)

        self.启动网络任务("转存分享", 任务, 成功, 失败)