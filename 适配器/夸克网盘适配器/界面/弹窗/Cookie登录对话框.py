# 夸克网盘适配器/界面/弹窗/Cookie登录对话框.py
"""手动 Cookie 登录对话框（阶段三 3.2b）

由 `短信登录对话框.py` 的骨架改造而来（骨架取自 git 历史 abeb2e0）：
保留 QDialog 布局、状态标签（info/ok/warn/err 四色）、按钮组、
后台线程清理与 closeEvent 逻辑；把「手机号 + 验证码」两行
替换为「Cookie 多行输入框」，并新增「跳过服务端校验」选项。

为什么需要这个对话框：
    CAS 扫码登录是最方便的通道，但**它不下发 `__puus`**，
    而下载直链的防盗链回调强制要求该字段。浏览器 Cookie 天然携带它，
    因此「手动 Cookie」是下载功能最可靠的登录方式（见 PLAN.md §6.1-Q3）。
"""
import logging

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPlainTextEdit,
    QPushButton, QDialogButtonBox, QCheckBox, QMessageBox,
)
from PySide6.QtCore import Qt, QThread

from 核心.认证.登录服务 import 登录服务
from 核心.认证.凭证仓库 import 校验Cookie, 解析Cookie字符串, 下载必需Cookie键

from ..公共线程 import 网络查询线程

logger = logging.getLogger("夸克网盘.界面.Cookie登录")


class Cookie登录对话框(QDialog):
    """粘贴浏览器 Cookie 完成登录"""

    def __init__(self, 父窗口=None):
        super().__init__(父窗口)
        self.setWindowTitle("🍪 手动 Cookie 登录")
        self.setModal(True)
        self.setMinimumWidth(560)

        self.登录服务实例 = 登录服务()
        self.登录结果: dict | None = None
        self._活动线程: list[QThread] = []

        self._构建界面()

    # ---------------- 界面 ----------------

    def _构建界面(self):
        布局 = QVBoxLayout(self)
        布局.setSpacing(12)

        标题 = QLabel("手动 Cookie 登录")
        标题.setStyleSheet(
            "font-size: 15px; font-weight: bold; color: #2196F3; "
            "padding: 4px 0;")
        标题.setAlignment(Qt.AlignCenter)
        布局.addWidget(标题)

        提示 = QLabel(
            "请粘贴从浏览器复制的夸克 Cookie（必须包含 "
            "<b>__kps</b> 与 <b>__uid</b>，建议同时含有 <b>__puus</b>）。<br>"
            "获取方式：浏览器登录 <b>pan.quark.cn</b> → 按 <b>F12</b> → "
            "<b>Network</b> → 任选一个请求 → <b>Request Headers</b> → "
            "复制 <b>Cookie</b> 那一整行的值。")
        提示.setStyleSheet("color: #666; font-size: 11px;")
        提示.setWordWrap(True)
        布局.addWidget(提示)

        self.Cookie输入 = QPlainTextEdit()
        self.Cookie输入.setPlaceholderText(
            "__pus=…; __kps=…; __uid=…; __puus=…")
        self.Cookie输入.setMinimumHeight(110)
        self.Cookie输入.setStyleSheet(
            "font-family: Consolas, 微软雅黑; font-size: 11px;")
        self.Cookie输入.textChanged.connect(self._输入变化)
        布局.addWidget(self.Cookie输入)

        选项行 = QHBoxLayout()
        self.跳过校验 = QCheckBox("跳过服务端校验（离线/网络异常时勾选）")
        self.跳过校验.setToolTip(
            "默认会调用一次 GET member 向服务端确认 Cookie 真的可用；"
            "勾选后只做本地字段校验。")
        选项行.addWidget(self.跳过校验)
        选项行.addStretch()
        布局.addLayout(选项行)

        self.状态标签 = QLabel("等待输入…")
        self.状态标签.setStyleSheet(
            "color: #666; font-size: 11px; padding: 4px 0;")
        self.状态标签.setWordWrap(True)
        布局.addWidget(self.状态标签)

        按钮组 = QDialogButtonBox()
        self.登录按钮 = QPushButton("🔐 登录")
        self.登录按钮.setStyleSheet(
            "background: #4CAF50; color: white; padding: 8px 20px; "
            "border-radius: 4px; font-weight: bold;")
        self.登录按钮.clicked.connect(self._点登录)
        self.登录按钮.setEnabled(False)
        按钮组.addButton(self.登录按钮, QDialogButtonBox.AcceptRole)

        取消按钮 = QPushButton("取消")
        取消按钮.clicked.connect(self.reject)
        按钮组.addButton(取消按钮, QDialogButtonBox.RejectRole)
        布局.addWidget(按钮组)

    # ---------------- 本地校验 / 状态 ----------------

    @property
    def Cookie文本(self) -> str:
        return self.Cookie输入.toPlainText().strip()

    def _输入变化(self):
        """边输入边做本地字段校验，实时反馈"""
        文本 = self.Cookie文本
        if not 文本:
            self.登录按钮.setEnabled(False)
            self._设置状态("等待输入…", "info")
            return

        字典 = 解析Cookie字符串(文本)
        通过, 原因 = 校验Cookie(字典)
        self.登录按钮.setEnabled(通过)
        if not 通过:
            self._设置状态(f"❌ {原因}", "err")
            return

        额外 = [k for k in 下载必需Cookie键 if k in 字典]
        提示 = f"✅ 已识别 {len(字典)} 个字段，含 __kps/__uid"
        if 额外:
            提示 += f"，并含 {'、'.join(额外)}（下载可用）"
        else:
            提示 += "；⚠ 未发现 __puus，下载可能被 CDN 拒绝（可先尝试登录）"
        self._设置状态(提示, "ok" if 额外 else "warn")

    def _设置状态(self, 文本: str, 类型: str = "info"):
        颜色 = {
            "info": "#2196F3",
            "ok": "#4CAF50",
            "warn": "#FF9800",
            "err": "#F44336",
        }.get(类型, "#666")
        self.状态标签.setText(文本)
        self.状态标签.setStyleSheet(
            f"color: {颜色}; font-size: 11px; padding: 4px 0;")

    # ---------------- 登录 ----------------

    def _点登录(self):
        文本 = self.Cookie文本
        通过, 原因 = 校验Cookie(文本)
        if not 通过:
            self._设置状态(f"❌ {原因}", "err")
            self.Cookie输入.setFocus()
            return

        self.登录按钮.setEnabled(False)
        验证 = not self.跳过校验.isChecked()
        self._设置状态(
            "正在校验并登录…" if 验证 else "正在本地保存…", "info")

        def 任务():
            # 用 with 保证 httpx 客户端被关闭
            with 登录服务() as 实例:
                return 实例.手动Cookie登录(
                    文本, 验证=验证, 保存=True)

        线程 = 网络查询线程("Cookie登录", 任务)
        线程.成功.connect(self._登录成功)
        线程.失败.connect(self._登录失败)
        线程.finished.connect(self._线程完成清理)
        self._活动线程.append(线程)
        线程.start()

    def _登录成功(self, 任务名: str, 结果: dict):
        self.登录结果 = 结果
        字段数 = len((结果 or {}).get("cookies") or {})
        含puus = "__puus" in ((结果 or {}).get("cookies") or {})
        self._设置状态(
            f"✅ 登录成功（{字段数} 个字段"
            + ("，含 __puus）" if 含puus else "；仍缺 __puus）"),
            "ok")
        self.accept()

    def _登录失败(self, 任务名: str, 错误: str):
        logger.error(f"[Cookie登录] 失败：{错误}")
        self._设置状态(f"❌ 登录失败：{错误}", "err")
        QMessageBox.warning(self, "登录失败", 错误)
        self.登录按钮.setEnabled(True)

    # ---------------- 线程与关闭 ----------------

    def _线程完成清理(self):
        线程 = self.sender()
        if 线程 is None:
            return
        try:
            if 线程 in self._活动线程:
                self._活动线程.remove(线程)
        except Exception:
            pass
        try:
            if isinstance(线程, QThread):
                线程.deleteLater()
        except Exception:
            pass

    def closeEvent(self, 事件):
        for 线程 in list(self._活动线程):
            try:
                if 线程.isRunning():
                    线程.quit()
                    线程.wait(500)
            except Exception:
                pass
        try:
            self.登录服务实例.关闭()
        except Exception:
            pass
        事件.accept()

    def reject(self):
        try:
            self.登录服务实例.关闭()
        except Exception:
            pass
        super().reject()
