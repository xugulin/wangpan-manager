"""播放清单（VLC 的「播放清单」面板）：队列 + 上一个/下一个 + 循环/随机。

VLC 的行为这里都要对齐：

* 清单里**双击**某一项 = 立刻播它（并把它设为"当前项"）；
* 「⏭ 下一个 / ⏮ 上一个」按清单顺序走，**循环模式**决定走到头怎么办：
  ``不循环`` 播完最后一个就停、``单曲循环`` 一直重复当前项、``列表循环`` 回到第一项；
* 「🔀 随机」开着时下一个/自动续播都随机挑一项（且尽量不重复挑当前项）；
* 自动续播：一首放完（libvlc 进入"已结束"）时如果清单里还有下一项，就接着播。

清单只存**引用信息**（网盘标识 + 远端路径 / 本地路径 + 标题），不存直链 ——
直链有时效，播之前都要重新取。
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QAbstractItemView, QHBoxLayout, QLabel,
                               QListWidget, QListWidgetItem, QPushButton,
                               QVBoxLayout, QWidget)

__all__ = ["播放项", "播放清单"]

#: 循环模式
循环_不循环 = "不循环"
循环_单曲 = "单曲循环"
循环_列表 = "列表循环"
循环模式们 = (循环_不循环, 循环_单曲, 循环_列表)


@dataclass
class 播放项:
    """清单里的一项（只存"从哪取"，不存直链）。"""

    标题: str = ""
    网盘标识: str = ""          # ""=本地文件
    远端路径: str = ""          # 网盘路径；本地文件时为空
    本地路径: str = ""          # 本地文件路径
    大小: int = 0
    时长秒: float = 0.0
    来源: str = ""              # 网盘显示名/「本地」

    @property
    def 是本地(self) -> bool:
        return bool(self.本地路径) and not self.网盘标识

    def 取值路径(self) -> str:
        """要交给播放会话的路径（网盘给远端路径，本地给绝对路径）。"""
        return self.本地路径 if self.是本地 else self.远端路径

    def 唯一键(self) -> tuple[str, str]:
        return (self.网盘标识, self.取值路径())

    def to_dict(self) -> dict:
        return {"标题": self.标题, "网盘标识": self.网盘标识,
                "远端路径": self.远端路径, "本地路径": self.本地路径,
                "大小": self.大小, "时长秒": self.时长秒, "来源": self.来源}

    @classmethod
    def from_dict(cls, 数据: dict) -> "播放项":
        return cls(**{k: v for k, v in (数据 or {}).items()
                      if k in cls.__dataclass_fields__})


class 播放清单(QWidget):
    """清单面板（列表 + 操作按钮 + 循环/随机）。"""

    请求播放 = Signal(object)          # 播放项
    请求移除后 = Signal()

    def __init__(self, 父=None):
        super().__init__(父)
        self._项们: list[播放项] = []
        self._当前 = -1
        self.循环模式 = 循环_不循环
        self.随机 = False

        布局 = QVBoxLayout(self)
        布局.setContentsMargins(4, 4, 4, 4)
        布局.setSpacing(4)

        顶 = QHBoxLayout()
        顶.addWidget(QLabel("📋 播放清单"))
        self.计数标签 = QLabel("0 项")
        self.计数标签.setStyleSheet("color: #95a5a6;")
        顶.addWidget(self.计数标签)
        顶.addStretch(1)
        布局.addLayout(顶)

        self.列表 = QListWidget()
        self.列表.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.列表.itemDoubleClicked.connect(self._双击)
        self.列表.setToolTip("双击某一项立刻播放；选中后可以移除")
        布局.addWidget(self.列表, 1)

        底部 = QHBoxLayout()
        self.移除按钮 = QPushButton("➖ 移除")
        self.移除按钮.clicked.connect(self.移除选中)
        底部.addWidget(self.移除按钮)
        self.清空按钮 = QPushButton("🗑 清空")
        self.清空按钮.clicked.connect(self.清空)
        底部.addWidget(self.清空按钮)
        底部.addStretch(1)
        布局.addLayout(底部)

    # ---------------- 增删查 ----------------

    def 项们(self) -> list[播放项]:
        return list(self._项们)

    def __len__(self) -> int:
        return len(self._项们)

    def 添加(self, 项: 播放项, 去重: bool = True) -> bool:
        """加入清单；返回是否真的加进去了（重复项默认不重复加）。"""
        if 项 is None:
            return False
        if 去重:
            键 = 项.唯一键()
            for 已有 in self._项们:
                if 已有.唯一键() == 键:
                    return False
        self._项们.append(项)
        self.刷新()
        if self._当前 < 0:
            self._当前 = 0
        return True

    def 移除选中(self) -> int:
        # ⚠️ QListWidget.row() 只吃 QListWidgetItem；selectedIndexes() 给的是
        # QModelIndex，要用 index.row()（写错会 TypeError，被单测逮到过）
        行们 = sorted({i.row() for i in self.列表.selectedIndexes()}, reverse=True)
        去掉了 = 0
        for 行 in 行们:
            if 0 <= 行 < len(self._项们):
                self._项们.pop(行)
                去掉了 += 1
                if 行 < self._当前:
                    self._当前 -= 1
                elif 行 == self._当前:
                    self._当前 = min(self._当前, len(self._项们) - 1)
        if 去掉了:
            self.刷新()
            self.请求移除后.emit()
        return 去掉了

    def 清空(self) -> None:
        self._项们.clear()
        self._当前 = -1
        self.刷新()

    # ---------------- 导航 ----------------

    def 当前项(self) -> Optional[播放项]:
        if 0 <= self._当前 < len(self._项们):
            return self._项们[self._当前]
        return None

    def 当前行(self) -> int:
        return self._当前

    def 设为当前(self, 行: int) -> Optional[播放项]:
        if 0 <= 行 < len(self._项们):
            self._当前 = 行
            self.刷新()
            return self._项们[行]
        return None

    def 下一个项(self, 自动: bool = False) -> Optional[播放项]:
        """下一个要播的项。

        :param 自动: True = 一首放完后的自动续播（受循环模式影响；
            不循环时到末尾返回 None 表示"播完就停"）。
        """
        if not self._项们:
            return None
        if self.随机 and len(self._项们) > 1:
            候选 = [i for i in range(len(self._项们)) if i != self._当前]
            self._当前 = random.choice(候选)
            self.刷新()
            return self._项们[self._当前]
        if self.循环模式 == 循环_单曲 and 自动 and self.当前项() is not None:
            return self.当前项()
        下 = self._当前 + 1
        if 下 >= len(self._项们):
            if self.循环模式 == 循环_列表 or not 自动:
                下 = 0
            else:
                return None
        self._当前 = 下
        self.刷新()
        return self._项们[下]

    def 上一个项(self) -> Optional[播放项]:
        if not self._项们:
            return None
        if self.随机 and len(self._项们) > 1:
            候选 = [i for i in range(len(self._项们)) if i != self._当前]
            self._当前 = random.choice(候选)
        else:
            self._当前 = (self._当前 - 1) % len(self._项们)
        self.刷新()
        return self.当前项()

    # ---------------- 显示 ----------------

    def 刷新(self) -> None:
        当前 = self._当前
        self.列表.blockSignals(True)
        self.列表.clear()
        for i, 项 in enumerate(self._项们):
            标记 = "▶ " if i == 当前 else "   "
            时长 = f"  {_时长文本(项.时长秒)}" if 项.时长秒 else ""
            来源 = f"  [{项.来源}]" if 项.来源 else ""
            self.列表.addItem(QListWidgetItem(f"{标记}{项.标题}{时长}{来源}"))
            self.列表.item(i).setData(Qt.UserRole, 项.to_dict())
        self.列表.blockSignals(False)
        self.计数标签.setText(f"{len(self._项们)} 项"
                         + (f" · 第 {当前 + 1} 项" if 当前 >= 0 else ""))
        if 0 <= 当前 < self.列表.count():
            self.列表.setCurrentRow(当前)

    def 循环按钮文本(self) -> str:
        return {"不循环": "🔁 不循环", "单曲循环": "🔂 单曲循环",
                "列表循环": "🔁 列表循环"}.get(self.循环模式, "🔁 不循环")

    def 切换循环(self) -> str:
        序号 = 循环模式们.index(self.循环模式) if self.循环模式 in 循环模式们 else 0
        self.循环模式 = 循环模式们[(序号 + 1) % len(循环模式们)]
        return self.循环模式

    def 切换随机(self) -> bool:
        self.随机 = not self.随机
        return self.随机

    # ---------------- 事件 ----------------

    def _双击(self, 条目: QListWidgetItem) -> None:
        行 = self.列表.row(条目)
        项 = self.设为当前(行)
        if 项 is not None:
            self.请求播放.emit(项)


def _时长文本(秒: float) -> str:
    秒 = int(秒 or 0)
    if 秒 <= 0:
        return ""
    时, 余 = divmod(秒, 3600)
    分, 秒 = divmod(余, 60)
    return f"{时}:{分:02d}:{秒:02d}" if 时 else f"{分:02d}:{秒:02d}"
