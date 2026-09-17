# 百度网盘适配器/界面/页面/分享页.py
"""分享页（百度网盘）

功能（对应 PLAN.md §6.4 / §7 Phase 6a+6b）：
  - 分享列表（`GET /share/record`，分页按 `nextpage` 终止）
  - 复制链接 / 复制提取码
  - 取消分享（`POST /share/cancel`）
  - 一键转存（`verify → list → transfer → taskquery`，Phase 6b）
  - 接收来自文件浏览页的「创建分享」请求（`执行创建分享`）

⚠️ 分享列表条目的字段名与其它接口**不一致**（实测 40 个字段）：
    `shareId`（大写 I）/ `passwd`（提取码）/ `shortlink`（完整链接，而
    `shorturl` 只是短码）。统一走 `取shareid()` / `取提取码()` / `取链接()` 适配。
"""

from __future__ import annotations

import logging

from PySide6.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QMessageBox,
    QTableWidget, QTableWidgetItem, QHeaderView, QApplication,
    QInputDialog, QGroupBox,
)
from PySide6.QtCore import Qt

from 核心.接口.分享接口 import (
    分享接口, 取shareid, 取提取码, 取链接, 取文件数, 有效期选项,
)
from 核心.接口.管理接口 import 管理接口
from 核心.接口.文件接口 import 文件接口

from ..格式化工具 import 格式化时间戳
from .基类 import 页面基类

logger = logging.getLogger("百度网盘.界面.分享")

列_链接, 列_提取码, 列_文件数, 列_创建时间, 列_有效期 = range(5)


class 分享页(页面基类):
    """分享管理页。"""

    def __init__(self, 主窗口, parent=None):
        super().__init__(主窗口, parent)
        self.当前分享: list[dict] = []
        self._构建界面()

    # ==================== UI ====================

    def _构建界面(self):
        布局 = QVBoxLayout(self)

        # ---- 工具栏 ----
        栏 = QHBoxLayout()
        self.刷新按钮 = QPushButton("🔄 刷新列表")
        self.刷新按钮.clicked.connect(self.刷新当前视图)
        栏.addWidget(self.刷新按钮)

        self.复制链接按钮 = QPushButton("🔗 复制链接")
        self.复制链接按钮.clicked.connect(self.复制选中链接)
        栏.addWidget(self.复制链接按钮)

        self.复制码按钮 = QPushButton("🔑 复制提取码")
        self.复制码按钮.clicked.connect(self.复制选中提取码)
        栏.addWidget(self.复制码按钮)

        self.取消分享按钮 = QPushButton("🗑 取消分享")
        self.取消分享按钮.setStyleSheet(
            "background: #F44336; color: white; padding: 6px 14px; "
            "border-radius: 4px; font-weight: bold;")
        self.取消分享按钮.clicked.connect(self.取消选中分享)
        栏.addWidget(self.取消分享按钮)

        栏.addStretch()
        self.状态提示 = QLabel("就绪")
        self.状态提示.setStyleSheet("color: #666;")
        栏.addWidget(self.状态提示)
        布局.addLayout(栏)

        # ---- 转存 ----
        转存组 = QGroupBox("📥 转存他人分享到我的网盘")
        转存布局 = QHBoxLayout()
        self.转存链接输入 = QPushButton("选择分享链接并转存…")
        self.转存链接输入.setToolTip(
            "输入分享链接（可带 ?pwd=xxxx）与提取码，转存到指定目录")
        self.转存链接输入.clicked.connect(self.一键转存)
        转存布局.addWidget(self.转存链接输入)
        转存布局.addStretch()
        转存组.setLayout(转存布局)
        布局.addWidget(转存组)

        # ---- 表格 ----
        self.表格 = QTableWidget()
        self.表格.setColumnCount(5)
        self.表格.setHorizontalHeaderLabels(
            ["分享链接", "提取码", "文件数", "创建时间", "有效期"])
        self.表格.horizontalHeader().setSectionResizeMode(
            列_链接, QHeaderView.Stretch)
        self.表格.setColumnWidth(列_提取码, 80)
        self.表格.setColumnWidth(列_文件数, 70)
        self.表格.setColumnWidth(列_创建时间, 170)
        self.表格.setColumnWidth(列_有效期, 100)
        self.表格.setAlternatingRowColors(True)
        self.表格.setSelectionBehavior(QTableWidget.SelectRows)
        self.表格.setEditTriggers(QTableWidget.NoEditTriggers)
        布局.addWidget(self.表格, 1)

    # ==================== 登录态 ====================

    def 设置启用(self, 已登录: bool):
        for 控件 in (self.刷新按钮, self.复制链接按钮, self.复制码按钮,
                    self.取消分享按钮, self.转存链接输入):
            控件.setEnabled(已登录)

    def 重置(self):
        self.当前分享 = []
        self.表格.setRowCount(0)
        self.状态提示.setText("就绪")

    def 首次进入(self):
        if not self.当前分享:
            self.刷新当前视图()

    # ==================== 列表 ====================

    def 刷新当前视图(self):
        """主界面「全局刷新」与刷新按钮共用入口。"""
        if not self.网络:
            self.状态提示.setText("未登录")
            return
        self.状态提示.setText("加载中…")

        def 任务():
            return 分享接口(self.网络).取全部分享(num=100)

        self.启动网络任务("分享列表", 任务, self._列表成功, self._列表失败)

    def _列表成功(self, 任务名: str, 条目们):
        self.当前分享 = list(条目们 or [])
        self.表格.setRowCount(len(self.当前分享))
        for 行, e in enumerate(self.当前分享):
            有效期 = e.get("expiredType") or e.get("expired_type") or 0
            单元格 = [
                取链接(e) or f"shareId={取shareid(e)}",
                取提取码(e) or "—",
                str(取文件数(e)),
                格式化时间戳(e.get("ctime") or 0),
                有效期选项.get(int(有效期), "永久有效" if not 有效期 else str(有效期)),
            ]
            for 列, 文本 in enumerate(单元格):
                格 = QTableWidgetItem(str(文本))
                格.setData(Qt.UserRole, e)
                self.表格.setItem(行, 列, 格)
        self.状态提示.setText(f"共 {len(self.当前分享)} 条分享")

    def _列表失败(self, 任务名: str, 错误: str):
        self.状态提示.setText(f"加载失败：{错误}")
        logger.error(f"[分享页] 列表失败：{错误}")

    def _选中(self) -> dict | None:
        行 = self.表格.currentRow()
        if 0 <= 行 < len(self.当前分享):
            return self.当前分享[行]
        QMessageBox.information(self, "提示", "请先选中一条分享")
        return None

    # ==================== 复制 ====================

    def 复制选中链接(self):
        e = self._选中()
        if not e:
            return
        链接 = 取链接(e)
        if not 链接:
            QMessageBox.warning(self, "无法复制", "该条目没有可用链接")
            return
        QApplication.clipboard().setText(链接)
        self.状态提示.setText("链接已复制")

    def 复制选中提取码(self):
        e = self._选中()
        if not e:
            return
        码 = 取提取码(e)
        if not 码:
            QMessageBox.information(self, "提示", "该分享没有提取码")
            return
        QApplication.clipboard().setText(码)
        self.状态提示.setText("提取码已复制")

    # ==================== 取消 ====================

    def 取消选中分享(self):
        e = self._选中()
        if not e:
            return
        sid = 取shareid(e)
        if not sid:
            QMessageBox.warning(self, "无法取消", "该条目缺少 shareId")
            return
        应答 = QMessageBox.question(
            self, "取消分享",
            f"确定要取消这条分享吗？\n\n{取链接(e) or sid}\n\n"
            f"取消后链接立即失效，不可恢复。")
        if 应答 != QMessageBox.Yes:
            return

        def 任务():
            return 分享接口(self.网络).取消单个(sid)

        self.启动网络任务("取消分享", 任务, self._取消成功, self._取消失败)

    def _取消成功(self, 任务名: str, 结果):
        self.状态提示.setText("已取消")
        QMessageBox.information(self, "完成", "分享已取消。")
        self.刷新当前视图()

    def _取消失败(self, 任务名: str, 错误: str):
        QMessageBox.warning(self, "取消失败", f"取消分享失败：\n{错误}")

    # ==================== 创建（供文件浏览页调用） ====================

    def 执行创建分享(self, 配置: dict):
        """由主界面转发：文件浏览页选中项 → 分享对话框 → 这里真正创建。"""
        if not self.网络:
            QMessageBox.warning(self, "提示", "请先登录")
            return
        if not 配置 or not 配置.get("fid列表"):
            return
        self.状态提示.setText("正在创建分享…")

        def 任务():
            return 分享接口(self.网络).创建分享(
                fid列表=配置["fid列表"],
                pwd=配置.get("pwd"),
                period=int(配置.get("period") or 0),
                public=int(配置.get("public") or 0),
            )

        def 成功(任务名, 结果):
            链接 = 结果.get("link") or 结果.get("shorturl") or ""
            码 = 结果.get("返回的提取码") or 配置.get("pwd") or ""
            self.状态提示.setText("创建成功")
            QApplication.clipboard().setText(
                f"{链接} 提取码：{码}" if 码 else 链接)
            QMessageBox.information(
                self, "分享创建成功",
                f"链接：{链接}\n提取码：{码 or '（无）'}\n\n"
                f"已复制到剪贴板。")
            self.刷新当前视图()

        self.启动网络任务("创建分享", 任务, 成功, self._创建失败)

    def _创建失败(self, 任务名: str, 错误: str):
        self.状态提示.setText("创建失败")
        QMessageBox.warning(self, "创建分享失败", f"创建分享失败：\n{错误}")

    # ==================== 转存（Phase 6b） ====================

    def 一键转存(self):
        """输入分享链接 → 选择目标目录 → 转存并轮询到完成。"""
        if not self.网络:
            QMessageBox.warning(self, "提示", "请先登录")
            return

        链接, 确定 = QInputDialog.getText(
            self, "转存分享",
            "粘贴分享链接（可带 ?pwd=xxxx）：\n"
            "例如 https://pan.baidu.com/s/1xxxxxxxxxxxx?pwd=abcd",
            text="")
        if not 确定 or not (链接 or "").strip():
            return

        # 链接里没带 pwd 时再问一次
        码 = ""
        if "pwd=" not in 链接:
            码, 确定 = QInputDialog.getText(
                self, "提取码", "该分享的 4 位提取码（无密码直接留空）：", text="")
            if not 确定:
                return

        目标目录, 确定 = QInputDialog.getText(
            self, "目标目录",
            "转存到网盘中的哪个目录？（如 /我的资源，留空为根目录）",
            text="/")
        if not 确定:
            return

        self.状态提示.setText("转存中…")
        链接文本 = 链接.strip()

        def 任务():
            return 分享接口(self.网络).一键转存(
                链接文本, 目标路径=(目标目录 or "/").strip() or "/",
                提取码=码, 等待完成=True, 超时秒=180)

        def 成功(任务名, 结果):
            状态 = 结果.get("状态")
            self.状态提示.setText(f"转存{状态}")
            QMessageBox.information(
                self, "转存完成",
                f"状态：{状态}\n"
                f"转存文件数：{len(结果.get('fsid列表') or [])}\n"
                f"目标目录：{结果.get('目标路径')}")

        self.启动网络任务("转存", 任务, 成功, self._转存失败)

    def _转存失败(self, 任务名: str, 错误: str):
        self.状态提示.setText("转存失败")
        QMessageBox.warning(
            self, "转存失败",
            f"转存失败：\n{错误}\n\n"
            f"常见原因：提取码错误、分享已失效、或需要先在网页端验证。")
