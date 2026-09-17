# 百度网盘适配器/界面/弹窗/二维码弹窗.py
"""扫码登录二维码弹窗

⚠️ 与原骨架的关键差异（Phase 7.7）
--------------------------------
原骨架是**本地用 `qrcode` 库生成**二维码；百度网盘是**服务端下发 base64 PNG**
（`登录服务.取登录二维码()` 的 `qrcode_b64` 字段）。
因此本弹窗改为直接解码显示服务端图片，**不再依赖 qrcode 库**。

四态文案（对应 `/channel/unicast` 的 status）
    0 等待扫码 / 1 已扫描待确认 / 2 已确认 / -1 超时或过期
"""

from __future__ import annotations

import base64
import logging

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QLabel, QLineEdit,
    QDialogButtonBox,
)
from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtGui import QPixmap, QImage

logger = logging.getLogger("百度网盘.界面.二维码")

# 四态文案（与 登录服务.查询扫码状态 的 status 取值对应）
状态文案 = {
    0: ("⏳ 等待扫码", "请用「百度网盘」App 扫描上方二维码", "#666666"),
    1: ("✅ 已扫描", "请在手机上点击「确认登录」", "#2196F3"),
    2: ("🎉 已确认", "登录成功，正在建立会话…", "#4CAF50"),
}
状态_超时 = ("⌛ 二维码已过期", "请关闭本窗口后重新发起扫码登录", "#F44336")


class 二维码弹窗(QDialog):
    """扫码登录二维码弹窗。

    :param 二维码内容: 服务端下发的 **base64 PNG** 字符串（或图片 URL 兜底）
    :param 标识:       二维码标识（百度侧为 `sign`；百度无「用户码」概念）
    """

    状态更新 = Signal(int)

    def __init__(self, 二维码内容: str, 标识: str = "", 父窗口=None):
        super().__init__(父窗口)
        self.setWindowTitle("扫码登录")
        self.setModal(True)
        self.setMinimumWidth(380)

        布局 = QVBoxLayout(self)
        布局.setSpacing(12)

        self.标题 = QLabel("📱 请用「百度网盘」App 扫码授权")
        self.标题.setStyleSheet(
            "font-size: 15px; font-weight: bold; color: #2196F3;")
        self.标题.setAlignment(Qt.AlignCenter)
        布局.addWidget(self.标题)

        # ---- 二维码图片（服务端下发） ----
        self.二维码标签 = QLabel("正在获取二维码…")
        self.二维码标签.setAlignment(Qt.AlignCenter)
        self.二维码标签.setMinimumSize(300, 300)
        self.二维码标签.setStyleSheet(
            "background: white; border: 1px solid #ddd;")
        布局.addWidget(self.二维码标签)

        # ---- 状态行（四态） ----
        self.状态标签 = QLabel(状态文案[0][1])
        self.状态标签.setAlignment(Qt.AlignCenter)
        self.状态标签.setStyleSheet("font-size: 13px; padding: 4px;")
        布局.addWidget(self.状态标签)

        # ---- 标识（百度无「用户码」，展示 sign 便于排查） ----
        if 标识:
            码标签 = QLabel(f"标识：<code>{标识[:16]}</code>")
            码标签.setAlignment(Qt.AlignCenter)
            码标签.setStyleSheet("font-size: 11px; color: #999;")
            布局.addWidget(码标签)

        # ---- 兜底：若拿到的是图片地址，允许复制 ----
        self.链接输入 = QLineEdit(
            二维码内容 if 二维码内容.startswith("http") else "")
        self.链接输入.setReadOnly(True)
        self.链接输入.setCursorPosition(0)
        self.链接输入.setVisible(bool(self.链接输入.text()))
        布局.addWidget(self.链接输入)

        按钮组 = QDialogButtonBox(QDialogButtonBox.Close)
        按钮组.rejected.connect(self.reject)
        布局.addWidget(按钮组)

        if 二维码内容:
            self.设置二维码(二维码内容)

        # 登录线程通过信号把状态投递回主线程
        self.状态更新.connect(self._应用状态)

    # ---------------- 二维码渲染 ----------------

    def 设置二维码(self, 内容: str):
        """渲染二维码：按 base64 PNG 解码；失败则给出明确指引。"""
        try:
            原始 = base64.b64decode(内容, validate=False)
            if not 原始:
                raise ValueError("base64 解码为空")
            qimg = QImage.fromData(原始, "PNG")
            if qimg.isNull():
                raise ValueError("不是有效的 PNG 数据")
            pixmap = QPixmap.fromImage(qimg).scaled(
                300, 300, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self.二维码标签.setPixmap(pixmap)
            logger.debug(f"[二维码] 已显示服务端二维码（{len(原始)} 字节）")
        except Exception as e:
            logger.warning(f"[二维码] 解码失败：{type(e).__name__}: {e}")
            self.二维码标签.setText(
                "二维码图片解析失败。\n\n"
                "可改用「导入 Cookie」方式登录，\n"
                "或从下方图片地址手动获取二维码。")

    # ---------------- 四态 ----------------

    def _应用状态(self, status: int):
        if status in 状态文案:
            标题, 说明, 颜色 = 状态文案[status]
        else:
            标题, 说明, 颜色 = 状态_超时
        self.标题.setText(标题)
        self.状态标签.setText(说明)
        self.状态标签.setStyleSheet(
            f"font-size: 13px; padding: 4px; color: {颜色};")
        if status == 2:
            # 已确认：短暂反馈后自动关闭
            QTimer.singleShot(800, self.accept)

    def 更新状态(self, status: int):
        """供外部（登录线程）调用；经由信号回到主线程执行。"""
        self.状态更新.emit(int(status))

    def 标记超时(self):
        self._应用状态(-1)
