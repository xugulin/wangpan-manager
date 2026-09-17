# 光鸭云盘适配器/界面/弹窗/二维码弹窗.py
"""扫码登录二维码弹窗"""

import io

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QLabel, QLineEdit, QPushButton,
    QDialogButtonBox, QMessageBox, QApplication,
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap, QImage


class 二维码弹窗(QDialog):
    """扫码登录二维码弹窗"""

    def __init__(self, 链接: str, 用户码: str, 父窗口=None):
        super().__init__(父窗口)
        self.setWindowTitle("扫码登录")
        self.setModal(True)
        self.setMinimumWidth(380)

        布局 = QVBoxLayout(self)
        布局.setSpacing(12)

        标题 = QLabel("📱 请用光鸭 App 扫码授权")
        标题.setStyleSheet(
            "font-size: 15px; font-weight: bold; color: #2196F3;")
        标题.setAlignment(Qt.AlignCenter)
        布局.addWidget(标题)

        self.二维码标签 = QLabel("正在生成二维码...")
        self.二维码标签.setAlignment(Qt.AlignCenter)
        self.二维码标签.setMinimumSize(300, 300)
        self.二维码标签.setStyleSheet(
            "background: white; border: 1px solid #ddd;")
        布局.addWidget(self.二维码标签)

        try:
            import qrcode
            二维码 = qrcode.QRCode(
                version=None,
                error_correction=qrcode.constants.ERROR_CORRECT_M,
                box_size=10, border=2,
            )
            二维码.add_data(链接)
            二维码.make(fit=True)
            图像 = 二维码.make_image(
                fill_color="black", back_color="white")
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
            self.二维码标签.setText(f"二维码生成失败：{e}")

        码标签 = QLabel(f"用户码：<b>{用户码}</b>")
        码标签.setAlignment(Qt.AlignCenter)
        码标签.setStyleSheet("font-size: 13px; padding: 4px;")
        布局.addWidget(码标签)

        链接标签 = QLabel("若无法扫码，可复制以下链接在浏览器打开：")
        链接标签.setStyleSheet("color: #666; font-size: 11px;")
        布局.addWidget(链接标签)

        self.链接输入 = QLineEdit(链接)
        self.链接输入.setReadOnly(True)
        self.链接输入.setCursorPosition(0)
        布局.addWidget(self.链接输入)

        复制按钮 = QPushButton("📋 复制链接")
        复制按钮.clicked.connect(lambda: self._复制链接(链接))
        布局.addWidget(复制按钮)

        按钮组 = QDialogButtonBox(QDialogButtonBox.Close)
        按钮组.rejected.connect(self.reject)
        布局.addWidget(按钮组)

    def _复制链接(self, 链接: str):
        QApplication.clipboard().setText(链接)
        QMessageBox.information(self, "提示", "链接已复制到剪贴板")