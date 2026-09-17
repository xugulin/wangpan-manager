"""任务详情对话框：把一条传输任务的全部信息摊开看，并能一键复制。

信息来源是界面台账（引擎任务字典 + 界面补充的耗时/速度），所以即使任务已经
结束、甚至批次已经清空，只要还在表格里就能看到完整细节。
"""

from __future__ import annotations

import time

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QDialog, QFormLayout, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton,
    QVBoxLayout,
)


def _时间(值) -> str:
    try:
        秒 = float(值 or 0)
    except (TypeError, ValueError):
        return "—"
    if 秒 <= 0:
        return "—"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(秒))


def _大小(值) -> str:
    try:
        数 = float(值 or 0)
    except (TypeError, ValueError):
        return "0 B"
    for 单位 in ("B", "KiB", "MiB", "GiB", "TiB"):
        if 数 < 1024 or 单位 == "TiB":
            return f"{数:.0f} {单位}" if 单位 == "B" else f"{数:.1f} {单位}"
        数 /= 1024
    return f"{数:.1f} TiB"


class 任务详情对话框(QDialog):
    """一条任务的完整信息 + 失败原因 + 时间点。"""

    def __init__(self, 任务: dict, 父=None):
        super().__init__(父)
        self.任务 = dict(任务 or {})
        self.setWindowTitle("任务详情")
        self.resize(760, 560)
        布局 = QVBoxLayout(self)

        标题 = QLabel(f"📄 {self.任务.get('名称') or '（无文件名）'}")
        标题.setStyleSheet("font-size: 15px; font-weight: bold;")
        标题.setWordWrap(True)
        布局.addWidget(标题)

        表单 = QFormLayout()
        表单.setLabelAlignment(Qt.AlignRight)
        for 标签, 值 in self._字段列表():
            控件 = QLabel(str(值 if 值 not in (None, "") else "—"))
            控件.setWordWrap(True)
            控件.setTextInteractionFlags(Qt.TextSelectableByMouse)
            表单.addRow(f"{标签}：", 控件)
        布局.addLayout(表单)

        原因 = QLabel("失败原因 / 跳过原因")
        原因.setStyleSheet("font-weight: bold; margin-top: 6px;")
        布局.addWidget(原因)
        详情框 = QPlainTextEdit()
        详情框.setReadOnly(True)
        详情框.setPlainText(self._原因文本())
        详情框.setMaximumHeight(140)
        布局.addWidget(详情框)

        底部 = QHBoxLayout()
        底部.addStretch(1)
        for 文本, 取值 in (("📋 复制全部", None),
                        ("📋 复制失败原因", self._原因文本),
                        ("📋 复制源路径",
                         lambda: str(self.任务.get("源路径") or "")),
                        ("📋 复制目标路径",
                         lambda: str(self.任务.get("目标路径") or ""))):
            按钮 = QPushButton(文本)
            if 取值 is None:
                按钮.clicked.connect(lambda: self._复制(self._全部文本()))
            else:
                按钮.clicked.connect(lambda _=False, f=取值: self._复制(f()))
            底部.addWidget(按钮)
        关闭 = QPushButton("关闭")
        关闭.clicked.connect(self.accept)
        底部.addWidget(关闭)
        布局.addLayout(底部)

    def _状态文本(self) -> str:
        状态 = str(self.任务.get("状态") or "等待")
        if 状态 == "已完成" and "兜底" in str(self.任务.get("阶段详情") or ""):
            return "✅ 已完成（兜底：本地中转）"
        return {"等待": "⏳ 等待", "进行中": "🔄 传输中", "已完成": "✅ 已完成",
                "跳过": "⏭️ 已跳过", "失败": "❌ 失败",
                "暂停": "⏸ 暂停"}.get(状态, 状态)

    def _字段列表(self):
        任务 = self.任务
        开始 = float(任务.get("开始") or 0)
        结束 = float(任务.get("结束") or 0)
        耗时 = (f"{结束 - 开始:.1f} 秒" if 开始 and 结束
              else f"{time.time() - 开始:.1f} 秒（进行中）" if 开始 else "—")
        进度 = "—"
        总大小 = int(任务.get("大小") or 0)
        已传 = int(任务.get("已传输") or 0)
        if 总大小:
            进度 = f"{已传 * 100 // 总大小}%（{_大小(已传)} / {_大小(总大小)}）"
        return [
            ("任务 ID", 任务.get("任务ID")),
            ("状态", self._状态文本()),
            ("类型", 任务.get("类型") or "跨网盘"),
            ("进度", 进度),
            ("源网盘", 任务.get("源网盘")),
            ("源路径", 任务.get("源路径")),
            ("目标网盘", 任务.get("目标网盘")),
            ("目标路径", 任务.get("目标路径")),
            ("目标实际落盘名", 任务.get("实际目标名")),
            ("开始时间", _时间(开始)),
            ("结束时间", _时间(结束)),
            ("耗时", 耗时),
            ("重试次数", 任务.get("重试次数")),
            ("阶段", 任务.get("阶段详情")),
            ("本地中转", 任务.get("本地缓存")),
            ("批次", 任务.get("批次ID")),
        ]

    def _原因文本(self) -> str:
        部分 = []
        if self.任务.get("错误"):
            部分.append(str(self.任务["错误"]))
        if self.任务.get("跳过原因"):
            部分.append(f"跳过原因：{self.任务['跳过原因']}")
        return "\n".join(部分) if 部分 else "（无）"

    def _全部文本(self) -> str:
        行 = [f"{标签}：{值}" for 标签, 值 in self._字段列表()]
        行.append(f"失败/跳过原因：{self._原因文本()}")
        return "\n".join(行)

    def _复制(self, 文本: str) -> None:
        QGuiApplication.clipboard().setText(文本 or "")
