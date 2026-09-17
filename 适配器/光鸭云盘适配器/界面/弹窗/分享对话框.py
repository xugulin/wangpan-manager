# 光鸭云盘适配器/界面/弹窗/分享对话框.py
"""创建分享的配置对话框"""

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QLabel, QLineEdit, QDialogButtonBox,
    QPushButton, QSpinBox, QComboBox, QFormLayout,
)
from PySide6.QtCore import Qt

from 核心.接口.分享接口 import (
    分享类型_无密码, 分享类型_随机密码, 分享类型_自定义密码,
    下载类型_直链, 下载类型_非直链,
    有效期_永久, 有效期_1天, 有效期_7天, 有效期_30天,
)


class 分享对话框(QDialog):
    """创建分享的配置对话框

    与 HAR 中三种模式一一对应：
      - 无密码        shareType=0
      - 随机密码      shareType=1 + autoFillCode
      - 自定义密码    shareType=2 + code
    """

    def __init__(self, 文件列表: list[dict], 父窗口=None):
        super().__init__(父窗口)
        self.setWindowTitle("🔗 创建分享")
        self.setModal(True)
        self.setMinimumWidth(520)
        self.文件列表 = 文件列表 or []
        self._构建界面()

    def _构建界面(self):
        布局 = QVBoxLayout(self)
        布局.setSpacing(12)

        标题 = QLabel("🔗 创建分享")
        标题.setStyleSheet(
            "font-size: 15px; font-weight: bold; color: #9C27B0; "
            "padding: 4px 0;")
        标题.setAlignment(Qt.AlignCenter)
        布局.addWidget(标题)

        提示 = QLabel(f"共选中 {len(self.文件列表)} 项")
        提示.setStyleSheet("color: #666; font-size: 11px;")
        布局.addWidget(提示)

        表单 = QFormLayout()
        表单.setSpacing(10)

        self.标题输入 = QLineEdit()
        if self.文件列表:
            名字 = self.文件列表[0].get("名称", "")
            if len(self.文件列表) > 1:
                名字 += f" 等 {len(self.文件列表)} 项"
            self.标题输入.setText(名字)
        表单.addRow(QLabel("分享标题："), self.标题输入)

        self.分享类型下拉 = QComboBox()
        self.分享类型下拉.addItem("无密码（公开分享）", 分享类型_无密码)
        self.分享类型下拉.addItem("随机密码（服务端生成）", 分享类型_随机密码)
        self.分享类型下拉.addItem("自定义密码", 分享类型_自定义密码)
        self.分享类型下拉.currentIndexChanged.connect(self._分享类型变化)
        表单.addRow(QLabel("密码类型："), self.分享类型下拉)

        self.密码输入 = QLineEdit()
        self.密码输入.setPlaceholderText("请输入自定义密码（建议 6 位以上）")
        self.密码输入.setMaxLength(32)
        self.密码输入.setEchoMode(QLineEdit.Password)
        self.密码输入.setEnabled(False)
        表单.addRow(QLabel("自定义密码："), self.密码输入)

        self.有效期下拉 = QComboBox()
        self.有效期下拉.addItem("永久", 有效期_永久)
        self.有效期下拉.addItem("1 天", 有效期_1天)
        self.有效期下拉.addItem("7 天", 有效期_7天)
        self.有效期下拉.addItem("30 天", 有效期_30天)
        表单.addRow(QLabel("有效期："), self.有效期下拉)

        self.下载类型下拉 = QComboBox()
        self.下载类型下拉.addItem("非直链（走分享页）", 下载类型_非直链)
        self.下载类型下拉.addItem("直链（直接下载）", 下载类型_直链)
        表单.addRow(QLabel("下载方式："), self.下载类型下拉)

        self.流量输入 = QSpinBox()
        self.流量输入.setRange(0, 1024 * 1024)
        self.流量输入.setSuffix(" MB")
        self.流量输入.setSpecialValueText("不限")
        self.流量输入.setValue(0)
        表单.addRow(QLabel("流量限制："), self.流量输入)

        self.转存次数输入 = QSpinBox()
        self.转存次数输入.setRange(0, 999999)
        self.转存次数输入.setSpecialValueText("不限")
        self.转存次数输入.setValue(0)
        表单.addRow(QLabel("转存次数："), self.转存次数输入)

        布局.addLayout(表单)

        self.状态标签 = QLabel("")
        self.状态标签.setStyleSheet("color: #666; font-size: 11px;")
        self.状态标签.setWordWrap(True)
        布局.addWidget(self.状态标签)

        按钮组 = QDialogButtonBox()
        self.创建按钮 = QPushButton("🔗 创建分享")
        self.创建按钮.setStyleSheet(
            "background: #9C27B0; color: white; padding: 8px 20px; "
            "border-radius: 4px; font-weight: bold;")
        self.创建按钮.clicked.connect(self._点创建)
        按钮组.addButton(self.创建按钮, QDialogButtonBox.AcceptRole)

        取消按钮 = QPushButton("取消")
        取消按钮.clicked.connect(self.reject)
        按钮组.addButton(取消按钮, QDialogButtonBox.RejectRole)

        布局.addWidget(按钮组)

    def _分享类型变化(self):
        类型 = self.分享类型下拉.currentData()
        self.密码输入.setEnabled(类型 == 分享类型_自定义密码)
        if 类型 == 分享类型_随机密码:
            self.状态标签.setText("密码由服务端随机生成，创建后自动显示")
            self.状态标签.setStyleSheet("color: #FF9800; font-size: 11px;")
        elif 类型 == 分享类型_自定义密码:
            self.状态标签.setText("请设置自定义密码（会随链接一起给出）")
            self.状态标签.setStyleSheet("color: #2196F3; font-size: 11px;")
        else:
            self.状态标签.setText("任何人都可以访问该分享链接")
            self.状态标签.setStyleSheet("color: #666; font-size: 11px;")

    def _点创建(self):
        标题 = self.标题输入.text().strip()
        if not 标题:
            self.状态标签.setText("❌ 请输入分享标题")
            self.状态标签.setStyleSheet("color: #F44336; font-size: 11px;")
            return

        分享类型 = self.分享类型下拉.currentData()
        if 分享类型 == 分享类型_自定义密码:
            if not self.密码输入.text().strip():
                self.状态标签.setText("❌ 请输入自定义密码")
                self.状态标签.setStyleSheet(
                    "color: #F44336; font-size: 11px;")
                return

        if not self.文件列表:
            self.状态标签.setText("❌ 没有选中任何文件")
            self.状态标签.setStyleSheet("color: #F44336; font-size: 11px;")
            return

        self.accept()

    def 取配置(self) -> dict | None:
        if self.result() != QDialog.Accepted:
            return None

        分享类型 = self.分享类型下拉.currentData()
        流量MB = self.流量输入.value()
        流量字节 = str(流量MB * 1024 * 1024) if 流量MB > 0 else "0"

        return {
            "fileIds": [f["文件ID"] for f in self.文件列表],
            "title": self.标题输入.text().strip(),
            "shareType": 分享类型,
            "validateDuration": self.有效期下拉.currentData(),
            "downloadType": self.下载类型下拉.currentData(),
            "trafficLimit": 流量字节,
            "maxRestoreCount": self.转存次数输入.value(),
            "code": (
                self.密码输入.text().strip()
                if 分享类型 == 分享类型_自定义密码 else ""
            ),
            "autoFillCode": 分享类型 == 分享类型_随机密码,
        }