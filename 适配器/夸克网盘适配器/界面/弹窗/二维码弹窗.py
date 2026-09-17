# 夸克网盘适配器/界面/弹窗/二维码弹窗.py
"""扫码登录二维码弹窗（阶段三 3.3）

夸克二维码的特点（与原适配器不同）：
  - **没有「用户码」** —— 二维码本身就是完整的授权载体，
    因此用户码为空时该行会被隐藏。
  - 二维码内容是一个 `su.quark.cn` 链接，用**夸克 App 扫码**即可授权。
  - 二维码有效期约 5 分钟，过期需要重新获取。
"""
import io
import logging

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QLabel, QLineEdit, QPushButton,
    QDialogButtonBox, QMessageBox, QApplication,
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap, QImage

logger = logging.getLogger("夸克网盘.界面.扫码")


class 二维码弹窗(QDialog):
    """显示登录二维码并反馈扫码进度"""

    def __init__(self, 链接: str, 用户码: str = "", 父窗口=None):
        super().__init__(父窗口)
        self.setWindowTitle("扫码登录")
        self.setModal(True)
        self.setMinimumWidth(400)

        布局 = QVBoxLayout(self)
        布局.setSpacing(12)

        标题 = QLabel("📱 请用夸克 App 扫码登录")
        标题.setStyleSheet(
            "font-size: 15px; font-weight: bold; color: #2196F3;")
        标题.setAlignment(Qt.AlignCenter)
        布局.addWidget(标题)

        self.二维码标签 = QLabel("正在生成二维码…")
        self.二维码标签.setAlignment(Qt.AlignCenter)
        self.二维码标签.setMinimumSize(300, 300)
        self.二维码标签.setStyleSheet(
            "background: white; border: 1px solid #ddd;")
        布局.addWidget(self.二维码标签)

        self._生成二维码(链接)

        # 夸克没有用户码；只有真拿到时才显示该行
        if 用户码:
            码标签 = QLabel(f"用户码：<b>{用户码}</b>")
            码标签.setAlignment(Qt.AlignCenter)
            码标签.setStyleSheet("font-size: 13px; padding: 4px;")
            布局.addWidget(码标签)

        self.状态标签 = QLabel("等待扫码…（二维码约 5 分钟内有效）")
        self.状态标签.setAlignment(Qt.AlignCenter)
        self.状态标签.setWordWrap(True)
        self.状态标签.setStyleSheet(
            "color: #2196F3; font-size: 12px; padding: 2px 0;")
        布局.addWidget(self.状态标签)

        链接标签 = QLabel(
            "若无法扫码，可复制下方链接，在手机浏览器中打开"
            "（会唤起夸克 App）：")
        链接标签.setStyleSheet("color: #666; font-size: 11px;")
        链接标签.setWordWrap(True)
        布局.addWidget(链接标签)

        self.链接输入 = QLineEdit(链接)
        self.链接输入.setReadOnly(True)
        self.链接输入.setCursorPosition(0)
        self.链接输入.setStyleSheet("font-size: 11px;")
        布局.addWidget(self.链接输入)

        复制按钮 = QPushButton("📋 复制链接")
        复制按钮.clicked.connect(lambda: self._复制链接(链接))
        布局.addWidget(复制按钮)

        按钮组 = QDialogButtonBox(QDialogButtonBox.Close)
        按钮组.rejected.connect(self.reject)
        布局.addWidget(按钮组)

    # ---------------- 二维码 ----------------

    def _生成二维码(self, 链接: str):
        try:
            import qrcode
            二维码 = qrcode.QRCode(
                version=None,
                error_correction=qrcode.constants.ERROR_CORRECT_M,
                box_size=10, border=2,
            )
            二维码.add_data(链接)
            二维码.make(fit=True)
            图像 = 二维码.make_image(fill_color="black", back_color="white")
            缓冲 = io.BytesIO()
            图像.save(缓冲, format="PNG")
            缓冲.seek(0)
            qimg = QImage.fromData(缓冲.getvalue(), "PNG")
            pixmap = QPixmap.fromImage(qimg).scaled(
                300, 300, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self.二维码标签.setPixmap(pixmap)
        except ImportError:
            self.二维码标签.setText(
                "未安装 qrcode 库\n\n请运行：pip install qrcode[pil]")
        except Exception as e:
            logger.exception("二维码生成失败")
            self.二维码标签.setText(f"二维码生成失败：{e}")

    # ---------------- 对外 ----------------

    def 设置状态(self, 文本: str, 类型: str = "info"):
        """由主窗口在轮询过程中调用"""
        颜色 = {"info": "#2196F3", "ok": "#4CAF50",
                "warn": "#FF9800", "err": "#F44336"}.get(类型, "#2196F3")
        self.状态标签.setText(文本)
        self.状态标签.setStyleSheet(
            f"color: {颜色}; font-size: 12px; padding: 2px 0;")

    def _复制链接(self, 链接: str):
        QApplication.clipboard().setText(链接)
        QMessageBox.information(self, "提示", "链接已复制到剪贴板")
