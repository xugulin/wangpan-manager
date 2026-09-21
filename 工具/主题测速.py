#!/usr/bin/env python3
"""量"主题样式表的全局层"到底值多少毫秒 —— 同一棵控件树，三档写法各建一遍。

背景（真机 windows-latest 实测）：
  * 建主窗口 54 秒、切到播放页 12~36 秒、AI 页 24 秒；
  * 把样式表整个清掉（``setStyleSheet("")``）之后，同样切这几页只要 **40 毫秒**。
  ⇒ 贵的不是控件、不是布局，是这张 QSS 的**样式匹配/重新 polish**。
    其中 ``QWidget { background-color; color; font-size }`` 那条全局规则最可疑：
    它匹配**每一个**控件，字号还会连锁触发整棵树重算 sizeHint。

这个工具就在真机上把三档并排量出来，用数据决定默认值：

  ① 样式表  —— QSS 里保留全局 ``QWidget`` 规则（老写法，默认档）
  ② 无字号  —— 保留全局规则，但字号交给应用字体（``QApplication.setFont``）
  ③ 调色板  —— 背景/文字走 ``QPalette``，QSS 里**没有**全局规则
  ④ 无样式  —— 完全不设样式表（下限，拿来做参照）

用法：
    运行环境/venv/bin/python 工具/主题测速.py
Windows 上（CI 也一样）：
    python 工具/主题测速.py
"""
from __future__ import annotations

import os
import statistics
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtWidgets import (QApplication, QComboBox, QFormLayout,       # noqa: E402
                              QGroupBox, QHBoxLayout, QLabel, QLineEdit,
                              QPlainTextEdit, QPushButton, QScrollArea,
                              QTabWidget, QTableWidget, QTableWidgetItem,
                              QTextEdit, QVBoxLayout, QWidget)

from v8_3.界面.主题管理器 import (全局层, 全局字号像素, 应用全局外观,   # noqa: E402
                              主题管理器)
from v8_3.界面.主窗口 import _调优应用                                  # noqa: E402

#: 全局层 → 生成 QSS 时用的档位（工具自己切，不改应用默认值）
档位 = ("样式表", "无字号", "调色板", "无样式")


def 造一棵树() -> QWidget:
    """建一棵"像真页面"的控件树（各种控件都有，约 200 个）。"""
    根 = QWidget()
    外 = QVBoxLayout(根)

    页签 = QTabWidget()
    for 页号 in range(3):
        页 = QWidget()
        列 = QVBoxLayout(页)
        for 组号 in range(2):
            组 = QGroupBox(f"分组 {页号}-{组号}")
            表单 = QFormLayout(组)
            for 行 in range(4):
                表单.addRow(QLabel(f"字段{行}"), QLineEdit("值"))
            列.addWidget(组)
        上下 = QHBoxLayout()
        上下.addWidget(QComboBox())
        上下.addWidget(QPushButton("按钮 A"))
        上下.addWidget(QPushButton("按钮 B"))
        列.addLayout(上下)
        列.addWidget(QPlainTextEdit())
        页签.addTab(页, f"页{页号}")
    外.addWidget(页签)

    表 = QTableWidget(12, 5)
    for r in range(12):
        for c in range(5):
            表.setItem(r, c, QTableWidgetItem(f"{r}-{c}"))
    外.addWidget(表)

    滚动 = QScrollArea()
    内 = QWidget()
    内列 = QVBoxLayout(内)
    for i in range(30):
        内列.addWidget(QLabel(f"滚动里的标签 {i}"))
    滚动.setWidget(内)
    外.addWidget(滚动)

    外.addWidget(QTextEdit())
    return 根


def 量一档(应用, 档: str, 次数: int = 3) -> tuple[float, float]:
    """返回 ``(建+排版 毫秒, 首帧 毫秒)``，取多次的中位数。"""
    建们: list[float] = []
    画们: list[float] = []
    for _ in range(次数):
        if 档 == "无样式":
            应用.setStyleSheet("")
        else:
            应用.setStyleSheet(主题管理器.获取样式表_档(档, "Dracula"))
            if 档 == "调色板":
                应用全局外观(应用, "Dracula")
        t0 = time.perf_counter()
        树 = 造一棵树()
        树.resize(1100, 760)
        建 = (time.perf_counter() - t0) * 1000
        t0 = time.perf_counter()
        树.grab()                      # 真正走一遍样式匹配 + 布局 + 绘制
        画 = (time.perf_counter() - t0) * 1000
        树.deleteLater()
        应用.processEvents()
        建们.append(建)
        画们.append(画)
    return statistics.median(建们), statistics.median(画们)


def main() -> int:
    应用 = _调优应用(QApplication([]))
    print(f"环境：{sys.platform}｜Python {sys.version.split()[0]}｜"
          f"平台插件 {应用.platformName()}｜全局层默认 {全局层}｜"
          f"全局字号 {全局字号像素}px", flush=True)
    print(f"字体：{应用.font().family()} {应用.font().pointSizeF()}pt"
          f"（像素 {应用.font().pixelSize()}）", flush=True)

    基线: dict[str, tuple[float, float]] = {}
    for 档 in 档位:
        建, 画 = 量一档(应用, 档)
        基线[档] = (建, 画)
        print(f"{建 + 画:9.1f} ms（建 {建:8.1f} + 首帧 {画:8.1f}）  {档}", flush=True)

    最慢 = max(基线.values(), key=lambda x: x[0] + x[1])
    最慢名 = [k for k, v in 基线.items() if v == 最慢][0]
    最好 = min(基线.values(), key=lambda x: x[0] + x[1])
    最好名 = [k for k, v in 基线.items() if v == 最好][0]
    print(f"\n最贵：{最慢名} {sum(最慢):.1f} ms；最省：{最好名} {sum(最好):.1f} ms"
          f"（省 {sum(最慢) - sum(最好):.1f} ms）", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
