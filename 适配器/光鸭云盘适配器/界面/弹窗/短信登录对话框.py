# 光鸭云盘适配器/界面/弹窗/短信登录对话框.py
"""手机短信验证码登录对话框"""

import logging

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QDialogButtonBox, QFormLayout, QMessageBox,
)
from PySide6.QtCore import Qt, QThread, QTimer
from PySide6.QtGui import QIntValidator

from 核心.认证.登录服务 import 登录服务

from ..格式化工具 import 规范化手机号
from ..公共线程 import 网络查询线程

logger = logging.getLogger("光鸭云盘.界面.短信登录")


class 短信登录对话框(QDialog):
    """手机短信验证码登录"""

    def __init__(self, 父窗口=None):
        super().__init__(父窗口)
        self.setWindowTitle("📱 短信验证码登录")
        self.setModal(True)
        self.setMinimumWidth(440)

        self.登录服务实例 = 登录服务()
        self.captcha_token = ""
        self.verification_id = ""
        self.手机号 = ""
        self.登录结果: dict | None = None
        self.倒计时 = 0
        self._活动线程: list[QThread] = []

        self.倒计时定时器 = QTimer(self)
        self.倒计时定时器.setInterval(1000)
        self.倒计时定时器.timeout.connect(self._倒计时滴答)

        self._构建界面()

    def _构建界面(self):
        布局 = QVBoxLayout(self)
        布局.setSpacing(12)

        标题 = QLabel("手机短信验证码登录")
        标题.setStyleSheet(
            "font-size: 15px; font-weight: bold; color: #2196F3; "
            "padding: 4px 0;")
        标题.setAlignment(Qt.AlignCenter)
        布局.addWidget(标题)

        提示 = QLabel(
            "请输入手机号，点击「发送验证码」，然后输入收到的短信验证码登录。")
        提示.setStyleSheet("color: #666; font-size: 11px;")
        提示.setWordWrap(True)
        布局.addWidget(提示)

        表单 = QFormLayout()
        表单.setSpacing(10)

        手机行 = QHBoxLayout()
        self.手机号输入 = QLineEdit()
        self.手机号输入.setPlaceholderText(
            "例如：18512828303 或 +86 18512828303")
        self.手机号输入.setMaxLength(25)
        self.手机号输入.returnPressed.connect(self._点发送验证码)
        手机行.addWidget(self.手机号输入, 1)

        self.发送验证码按钮 = QPushButton("📨 发送验证码")
        self.发送验证码按钮.setStyleSheet(
            "background: #2196F3; color: white; padding: 6px 14px; "
            "border-radius: 4px; font-weight: bold;")
        self.发送验证码按钮.clicked.connect(self._点发送验证码)
        手机行.addWidget(self.发送验证码按钮)
        表单.addRow(QLabel("手机号："), 手机行)

        验证行 = QHBoxLayout()
        self.验证码输入 = QLineEdit()
        self.验证码输入.setPlaceholderText(
            "请输入短信验证码（4~6 位数字）")
        self.验证码输入.setMaxLength(6)
        self.验证码输入.setValidator(QIntValidator(0, 999999, self))
        self.验证码输入.returnPressed.connect(self._点登录)
        验证行.addWidget(self.验证码输入, 1)
        表单.addRow(QLabel("验证码："), 验证行)

        布局.addLayout(表单)

        self.状态标签 = QLabel("")
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
        按钮组.addButton(self.登录按钮, QDialogButtonBox.AcceptRole)

        取消按钮 = QPushButton("取消")
        取消按钮.clicked.connect(self.reject)
        按钮组.addButton(取消按钮, QDialogButtonBox.RejectRole)
        布局.addWidget(按钮组)

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

    def _开始倒计时(self, 秒: int = 60):
        self.倒计时 = 秒
        self.发送验证码按钮.setEnabled(False)
        self._更新发送按钮文字()
        self.倒计时定时器.start()

    def _倒计时滴答(self):
        self.倒计时 -= 1
        if self.倒计时 <= 0:
            self.倒计时定时器.stop()
            self.倒计时 = 0
            self.发送验证码按钮.setEnabled(True)
            self.发送验证码按钮.setText("📨 重新发送")
        else:
            self._更新发送按钮文字()

    def _更新发送按钮文字(self):
        self.发送验证码按钮.setText(f"⏱ {self.倒计时}s")

    def _点发送验证码(self):
        原始 = self.手机号输入.text().strip()
        if not 原始:
            self._设置状态("请输入手机号", "err")
            self.手机号输入.setFocus()
            return

        手机号 = 规范化手机号(原始)
        号码部分 = 手机号.split(" ", 1)[-1] if " " in 手机号 else ""
        if not 号码部分 or len(号码部分) < 6:
            self._设置状态(
                "手机号格式不正确，请检查后重试（示例：18512828303）",
                "err")
            return

        self.发送验证码按钮.setEnabled(False)
        self._设置状态(f"正在向 {手机号} 发送验证码...", "info")
        self.手机号 = 手机号

        def 任务():
            实例 = self.登录服务实例
            captcha = 实例.短信登录_初始化盾(手机号)
            数据 = 实例.短信登录_发送验证码(手机号, captcha)
            return {"captcha_token": captcha, **数据}

        线程 = 网络查询线程("短信登录发送", 任务)
        线程.成功.connect(self._发送成功)
        线程.失败.connect(self._发送失败)
        线程.finished.connect(self._线程完成清理)
        self._活动线程.append(线程)
        线程.start()

    def _发送成功(self, 任务名: str, 结果: dict):
        self.captcha_token = 结果.get("captcha_token", "")
        self.verification_id = 结果.get("verification_id", "")
        is_user = 结果.get("is_user", True)
        通道 = 结果.get("selected_channel", "VERIFICATION_PHONE")
        有效期 = 结果.get("expires_in", 300)

        if not self.verification_id:
            self._设置状态("服务端未返回 verification_id，无法继续", "err")
            self.发送验证码按钮.setEnabled(True)
            return

        if is_user:
            self._设置状态(
                f"✅ 验证码已发送（通道：{通道}，有效期 {有效期}s），"
                f"请在下方输入收到的短信验证码",
                "ok")
        else:
            self._设置状态(
                f"⚠️ 该手机号未注册光鸭账号（通道：{通道}）。"
                f"如已注册请检查号码，或先在 App 端注册",
                "warn")

        self.验证码输入.setFocus()
        self._开始倒计时(60)

    def _发送失败(self, 任务名: str, 错误: str):
        logger.error(f"[短信登录] 发送验证码失败：{错误}")
        self._设置状态(f"❌ 发送失败：{错误}", "err")
        self.发送验证码按钮.setEnabled(True)
        self.发送验证码按钮.setText("📨 发送验证码")

    def _点登录(self):
        if not self.verification_id:
            self._设置状态("请先点击「发送验证码」", "err")
            return
        验证码 = self.验证码输入.text().strip()
        if not 验证码 or not 验证码.isdigit():
            self._设置状态("请输入收到的短信验证码", "err")
            self.验证码输入.setFocus()
            return

        self.登录按钮.setEnabled(False)
        self._设置状态("正在校验并登录...", "info")

        手机号 = self.手机号
        verification_id = self.verification_id

        def 任务():
            实例 = self.登录服务实例
            校验结果 = 实例.短信登录_校验验证码(
                verification_id, 验证码)
            verification_token = 校验结果.get("verification_token", "")
            if not verification_token:
                raise RuntimeError("未获取到 verification_token")
            令牌数据 = 实例.短信登录_登录(
                手机号, 验证码, verification_token)
            return 令牌数据

        线程 = 网络查询线程("短信登录登录", 任务)
        线程.成功.connect(self._登录成功)
        线程.失败.connect(self._登录失败)
        线程.finished.connect(self._线程完成清理)
        self._活动线程.append(线程)
        线程.start()

    def _登录成功(self, 任务名: str, 令牌数据: dict):
        self.登录结果 = 令牌数据
        self._设置状态(
            f"✅ 登录成功（用户 ID：{令牌数据.get('sub', '?')}）",
            "ok")
        self.accept()

    def _登录失败(self, 任务名: str, 错误: str):
        logger.error(f"[短信登录] 登录失败：{错误}")
        self._设置状态(f"❌ 登录失败：{错误}", "err")
        self.登录按钮.setEnabled(True)

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
        try:
            self.倒计时定时器.stop()
        except Exception:
            pass
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