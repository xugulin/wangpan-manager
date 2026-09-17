# 百度网盘适配器/界面/弹窗/分享对话框.py
"""创建分享对话框（百度网盘）

对应 `POST /share/pset`（见 PLAN.md §6.4①）。实测要点：
  - `pwd`（4 位提取码）**是必需的**：传空串会返回 `pwd length param error`，
    因此这里默认给一个随机 4 位码，也允许用户自定义。
  - 创建响应**不回显提取码**，所以对话框会把最终使用的码记下来供界面展示。
"""

from __future__ import annotations

import logging
import random

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel,
    QLineEdit, QComboBox, QCheckBox, QPushButton, QDialogButtonBox,
    QMessageBox, QGroupBox,
)

from 核心.接口.分享接口 import 有效期选项

logger = logging.getLogger("百度网盘.界面.分享对话框")


def 随机提取码() -> str:
    return f"{random.randint(0, 9999):04d}"


class 分享对话框(QDialog):
    """选择分享参数，返回可交给 `分享接口.创建分享` 的配置。"""

    def __init__(self, 文件列表: list[dict], 父窗口=None):
        super().__init__(父窗口)
        self.setWindowTitle("创建分享")
        self.setModal(True)
        self.setMinimumWidth(460)

        self.文件列表 = list(文件列表 or [])
        self._配置: dict | None = None

        self._构建界面()

    # ---------------- UI ----------------

    def _构建界面(self):
        布局 = QVBoxLayout(self)
        布局.setSpacing(12)

        # ---- 待分享条目 ----
        组 = QGroupBox(f"待分享（{len(self.文件列表)} 项）")
        组布局 = QVBoxLayout()
        预览 = QLabel()
        名称 = [str(e.get("server_filename") or e.get("path") or "?")
                for e in self.文件列表[:8]]
        文本 = "\n".join(f"  • {n}" for n in 名称)
        if len(self.文件列表) > 8:
            文本 += f"\n  … 其余 {len(self.文件列表) - 8} 项"
        预览.setText(文本 or "  （无）")
        预览.setStyleSheet("font-size: 12px; color: #333;")
        组布局.addWidget(预览)
        组.setLayout(组布局)
        布局.addWidget(组)

        # ---- 参数 ----
        表单 = QFormLayout()

        self.有效期下拉 = QComboBox()
        for 天, 文案 in 有效期选项.items():
            self.有效期下拉.addItem(文案, 天)
        self.有效期下拉.setCurrentIndex(0)          # 默认永久
        表单.addRow("有效期：", self.有效期下拉)

        码行 = QHBoxLayout()
        self.提取码输入 = QLineEdit(随机提取码())
        self.提取码输入.setMaxLength(4)
        self.提取码输入.setFixedWidth(80)
        self.提取码输入.setToolTip("必须是 4 位；百度要求分享必须带提取码")
        码行.addWidget(self.提取码输入)
        随机按钮 = QPushButton("🎲 随机")
        随机按钮.clicked.connect(lambda: self.提取码输入.setText(随机提取码()))
        码行.addWidget(随机按钮)
        码行.addStretch()
        表单.addRow("提取码：", 码行)

        self.公开勾选 = QCheckBox("公开分享（任何人都可访问）")
        self.公开勾选.setChecked(False)
        表单.addRow("", self.公开勾选)

        布局.addLayout(表单)

        提示 = QLabel(
            "⚠️ 百度要求分享必须带 4 位提取码；创建后请自行保存提取码，"
            "接口响应不回显它（但可从分享列表读回）。")
        提示.setWordWrap(True)
        提示.setStyleSheet("color: #999; font-size: 11px;")
        布局.addWidget(提示)

        # ---- 按钮 ----
        按钮组 = QDialogButtonBox()
        按钮组.addButton("创建分享", QDialogButtonBox.AcceptRole)
        按钮组.addButton("取消", QDialogButtonBox.RejectRole)
        按钮组.accepted.connect(self._点创建)
        按钮组.rejected.connect(self.reject)
        布局.addWidget(按钮组)

    # ---------------- 交互 ----------------

    def _点创建(self):
        fid列表 = [e.get("fs_id") for e in self.文件列表
                  if e.get("fs_id") is not None]
        if not fid列表:
            QMessageBox.warning(self, "无法分享", "选中项缺少 fs_id，无法创建分享。")
            return

        码 = (self.提取码输入.text() or "").strip()
        if len(码) != 4:
            QMessageBox.warning(self, "提取码无效", "提取码必须是 4 位字符。")
            return

        self._配置 = {
            "fid列表": fid列表,
            "pwd": 码,
            "period": int(self.有效期下拉.currentData() or 0),
            "public": 1 if self.公开勾选.isChecked() else 0,
        }
        logger.info(f"[分享对话框] 配置：{len(fid列表)} 个文件，"
                    f"有效期 {self._配置['period']} 天")
        self.accept()

    def 取配置(self) -> dict | None:
        return self._配置
