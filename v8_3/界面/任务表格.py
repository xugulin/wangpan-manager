"""传输任务表格（V8 形态：分类标签 + 8 列 + 操作列）。

设计要点（对齐 网盘管理_V8 的「传输页面」）：

* **分类标签**：全部 / 进行中 / 已完成 / 等待 / 暂停 / 跳过 / 失败 / 未完成，
  每个带计数和颜色；点一下只看这一类，方便"快速管理和维护"。
* **列**：文件名 · 类型 · 大小 · 进度 · 速度 · 耗时 · 状态 · 操作，
  另加一列「完成时间 / 失败时间」——它只在"已完成"/"失败"分类里出现
  （其它分类里隐藏），满足"记录完成/失败时间点"的要求又不挤占常规视图。
* **失败原因**：状态列直接拼上原因摘要，鼠标悬停看全文，
  双击（或点 ℹ️）打开详情对话框看完整信息。
* **操作列**：⏸/▶ 暂停继续、🔄 重试、ℹ️ 详情、🗑 取消/移除。

这个模块只依赖 Qt 与纯函数，方便单独测试分类映射与排序。
"""

from __future__ import annotations

import csv
import time
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction, QColor, QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView, QFileDialog, QHBoxLayout, QHeaderView, QLabel, QMenu,
    QMessageBox, QProgressBar, QPushButton, QTableWidget, QTableWidgetItem,
    QToolButton, QWidget,
)

#: 分类标签定义：(键, 显示文本, 颜色)
分类定义: list[tuple[str, str, str]] = [
    ("全部", "📋 全部", "#607D8B"),
    ("进行中", "🔄 进行中", "#2196F3"),
    ("已完成", "✅ 已完成", "#4CAF50"),
    ("等待", "⏳ 等待", "#FF9800"),
    ("暂停", "⏸ 暂停", "#9C27B0"),
    ("跳过", "⏭️ 跳过", "#9E9E9E"),
    ("失败", "❌ 失败", "#F44336"),
    ("未完成", "🚧 未完成", "#795548"),
]

#: 引擎状态 → 分类。三种写法都认：引擎中文状态名（下载/上传/完成…）、
#: 界面本地任务的英文状态（downloading/uploading/done…）、以及分类名本身。
状态到分类 = {
    # 引擎中文状态名
    "等待": "等待", "枚举": "进行中", "下载": "进行中", "上传": "进行中",
    "完成": "已完成", "跳过": "跳过", "失败": "失败", "已取消": "失败",
    "暂停": "暂停", "已暂停": "暂停",
    # 界面本地任务用的英文状态
    "pending": "等待", "scanning": "进行中", "downloading": "进行中",
    "uploading": "进行中", "done": "已完成", "skipped": "跳过",
    "failed": "失败", "cancelled": "失败", "canceled": "失败",
    "paused": "暂停",
    # 直接就是分类名的（防御性：调用方可能已经转过一次）
    "进行中": "进行中", "已完成": "已完成", "未完成": "进行中",
}

#: "未完成"包含哪些分类（与 V8 的 未完成状态 一致：跑着/排队/暂停）
未完成分类 = ("进行中", "等待", "暂停")

#: 排序权重：正在跑的在最上面，已完成沉底
分类排序 = {"进行中": 0, "失败": 1, "暂停": 2, "等待": 3, "跳过": 4,
        "已完成": 5}

类型文本 = {
    "上传": "📤 上传", "下载": "📥 下载", "跨网盘": "🔀 跨网盘",
    "": "🔀 跨网盘",
}


def 取分类(任务: dict) -> str:
    """任务属于哪个分类标签。"""
    状态 = str(任务.get("状态") or 任务.get("status_name") or "等待")
    return 状态到分类.get(状态, "进行中")


def 在分类里(任务: dict, 分类: str) -> bool:
    if 分类 == "全部":
        return True
    属于 = 取分类(任务)
    if 分类 == "未完成":
        return 属于 in 未完成分类
    return 属于 == 分类


def 排序键(任务: dict):
    return (分类排序.get(取分类(任务), 9),
            -float(任务.get("结束") or 0),
            时间文本(任务.get("结束")))


def 时间文本(时间戳) -> str:
    try:
        值 = float(时间戳 or 0)
    except (TypeError, ValueError):
        return ""
    if 值 <= 0:
        return ""
    return time.strftime("%m-%d %H:%M:%S", time.localtime(值))


def 时长文本(秒) -> str:
    try:
        值 = float(秒 or 0)
    except (TypeError, ValueError):
        return "-"
    if 值 <= 0:
        return "-"
    if 值 < 60:
        return f"{值:.1f}s"
    if 值 < 3600:
        return f"{int(值 // 60)}m{int(值 % 60):02d}s"
    return f"{int(值 // 3600)}h{int(值 % 3600 // 60):02d}m"


def 大小文本(字节) -> str:
    try:
        值 = float(字节 or 0)
    except (TypeError, ValueError):
        return "0 B"
    for 单位 in ("B", "KiB", "MiB", "GiB", "TiB"):
        if 值 < 1024 or 单位 == "TiB":
            return f"{值:.0f} {单位}" if 单位 == "B" else f"{值:.1f} {单位}"
        值 /= 1024
    return f"{值:.1f} TiB"


def 统计分类(台账) -> dict[str, int]:
    """按分类标签统计任务数（纯函数，方便单测）。"""
    计数 = {k: 0 for k, _, _ in 分类定义}
    for 任务 in (台账.values() if isinstance(台账, dict) else 台账):
        计数["全部"] += 1
        for 键, _, _ in 分类定义:
            if 键 in ("全部", "未完成"):
                continue
            if 在分类里(任务, 键):
                计数[键] += 1
        if 在分类里(任务, "未完成"):
            计数["未完成"] += 1
    return 计数


class 任务表格(QTableWidget):
    """分类 + 8 列 + 操作列的传输任务表。"""

    请求暂停 = Signal(str)
    请求继续 = Signal(str)
    请求重试 = Signal(str)
    请求取消 = Signal(str)
    请求详情 = Signal(str)
    请求复制 = Signal(str)
    分类变化 = Signal(str)

    列名 = ["文件名", "类型", "大小", "进度", "速度", "耗时", "状态", "操作"]
    时间列 = 8          # 「完成时间 / 失败时间」列索引

    def __init__(self, 父=None):
        super().__init__(0, 9, 父)
        self.当前分类 = "全部"
        self._台账: dict[str, dict] = {}
        self._显示: list[str] = []
        self._速度: dict[str, tuple[float, int, float]] = {}
        self._构建表头()
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setAlternatingRowColors(True)
        self.verticalHeader().setDefaultSectionSize(30)
        self.doubleClicked.connect(self._双击)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._右键菜单)
        self._应用列宽()

    # ---------------- 表头 ----------------

    def _构建表头(self):
        self.setHorizontalHeaderLabels(self.列名 + ["完成/失败时间"])
        表头 = self.horizontalHeader()
        # 列不许被压到看不清：窗口不够宽时由外层页面滚动（配合 界面/滚动区.py），
        # 而不是把"文件名"挤成一条缝、其它列错位。
        表头.setMinimumSectionSize(88)
        表头.setSectionResizeMode(0, QHeaderView.Stretch)
        self.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.setWordWrap(False)
        self.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        for 列, 宽 in ((1, 92), (2, 150), (3, 130), (4, 96), (5, 84),
                    (6, 220), (7, 150), (8, 140)):
            self.setColumnWidth(列, 宽)
        # "文件名"列也要有下限，否则窗口一窄就被挤成一条缝、别的列跟着错位。
        # 整张表的最小宽度 = 各列下限之和，比这更窄时由外层页面横向滚动。
        self.setMinimumWidth(160 + 92 + 150 + 130 + 96 + 84 + 220 + 150 + 24)

    def _应用列宽(self):
        """时间列只在 已完成/失败 分类里显示。"""
        显示时间 = self.当前分类 in ("已完成", "失败")
        self.setColumnHidden(self.时间列, not 显示时间)
        self.setHorizontalHeaderItem(
            self.时间列,
            QTableWidgetItem("完成时间" if self.当前分类 == "已完成"
                          else "失败时间" if self.当前分类 == "失败"
                          else "完成/失败时间"))

    # ---------------- 数据 ----------------

    def 设置台账(self, 台账: dict[str, dict]) -> None:
        self._台账 = dict(台账)

    def 切换分类(self, 分类: str) -> None:
        if 分类 not in {k for k, _, _ in 分类定义}:
            return
        self.当前分类 = 分类
        self._应用列宽()
        self.分类变化.emit(分类)

    def 当前显示(self) -> list[dict]:
        return [self._台账[i] for i in self._显示 if i in self._台账]

    def 全部任务(self) -> list[dict]:
        return list(self._台账.values())

    def 分类计数(self) -> dict[str, int]:
        return 统计分类(self._台账)

    # ---------------- 渲染 ----------------

    def 刷新(self) -> None:
        列表 = sorted((t for t in self._台账.values()
                   if 在分类里(t, self.当前分类)), key=排序键)
        self._显示 = [str(t.get("任务ID") or t.get("task_id") or i)
                   for i, t in enumerate(列表)]
        # 用列表本身承载任务，避免 ID 生成规则不一致时错行
        self._可见任务 = 列表
        self.setUpdatesEnabled(False)
        try:
            if self.rowCount() != len(列表):
                self.setRowCount(len(列表))
            for 行, 任务 in enumerate(列表):
                self._渲染行(行, 任务)
        finally:
            self.setUpdatesEnabled(True)

    #: 兼容：刷新时按行取任务
    @property
    def _任务行(self):
        return getattr(self, "_可见任务", [])

    def _渲染行(self, 行: int, 任务: dict) -> None:
        状态 = str(任务.get("状态") or "等待")
        完成字节 = int(任务.get("已传输") or 0)
        总大小 = int(任务.get("大小") or 0)
        开始 = float(任务.get("开始") or 0)
        结束 = float(任务.get("结束") or 0)

        self._设文本(行, 0, str(任务.get("名称") or ""),
                  提示=str(任务.get("目标") or ""))
        self._设文本(行, 1, 类型文本.get(str(任务.get("类型") or ""), "🔀 跨网盘"))

        if 总大小 > 0:
            self._设文本(行, 2, f"{大小文本(完成字节)} / {大小文本(总大小)}")
        else:
            self._设文本(行, 2, "--")

        进度 = self.cellWidget(行, 3)
        if not isinstance(进度, QProgressBar):
            进度 = QProgressBar()
            进度.setRange(0, 100)
            self.setCellWidget(行, 3, 进度)
        self._渲染进度(进度, 状态, 完成字节, 总大小)

        速度文本, _ = self._速度文本(任务)
        self._设文本(行, 4, 速度文本)
        if 状态 == "已完成" and 开始 and 结束:
            self._设文本(行, 5, 时长文本(结束 - 开始))
        elif 状态 == "进行中" and 开始:
            self._设文本(行, 5, 时长文本(time.time() - 开始))
        elif 状态 == "失败" and 开始 and 结束:
            self._设文本(行, 5, 时长文本(结束 - 开始))
        else:
            self._设文本(行, 5, "-")

        状态文本 = self._状态文本(任务)
        self._设文本(行, 6, 状态文本, 提示=str(任务.get("错误") or ""))
        self._设文本(行, self.时间列,
                  时间文本(结束 if 状态 in ("已完成", "失败") else 0))
        self._渲染操作(行, 任务)

    @staticmethod
    def _渲染进度(进度: QProgressBar, 状态: str, 完成: int, 总量: int) -> None:
        if 状态 == "已完成":
            进度.setRange(0, 100); 进度.setValue(100); 进度.setFormat("✅ 100%")
        elif 状态 == "跳过":
            进度.setRange(0, 100); 进度.setValue(100); 进度.setFormat("⏭ 已跳过")
        elif 状态 == "失败":
            百分比 = int(完成 * 100 / 总量) if 总量 else 0
            进度.setRange(0, 100); 进度.setValue(百分比)
            进度.setFormat(f"❌ {百分比}%")
        elif 状态 == "暂停":
            百分比 = int(完成 * 100 / 总量) if 总量 else 0
            进度.setRange(0, 100); 进度.setValue(百分比)
            进度.setFormat(f"⏸ {百分比}%")
        elif 状态 == "等待":
            进度.setRange(0, 100); 进度.setValue(0)
            进度.setFormat("⏳ 等待中")
        elif 总量 > 0 and 完成 > 0:
            百分比 = min(int(完成 * 100 / 总量), 100)
            进度.setRange(0, 100); 进度.setValue(百分比)
            进度.setFormat(f"{百分比}%")
        else:
            进度.setRange(0, 0)
            进度.setFormat("传输中…")

    @staticmethod
    def _状态文本(任务: dict) -> str:
        状态 = str(任务.get("状态") or "等待")
        原始 = str(任务.get("原始状态") or "")
        if 状态 == "进行中":
            细 = {"下载": "🔄 下载中", "上传": "🔄 上传中",
                 "downloading": "🔄 下载中", "uploading": "🔄 上传中",
                 "枚举": "🔍 扫描中", "scanning": "🔍 扫描中"}.get(原始)
            if 细:
                return 细
        映射 = {"等待": "⏳ 等待", "进行中": "🔄 传输中", "已完成": "✅ 已完成",
              "跳过": "⏭️ 已跳过", "失败": "❌ 失败", "暂停": "⏸ 暂停"}
        文本 = 映射.get(状态, 状态)
        if 状态 == "失败":
            原因 = str(任务.get("错误") or "")
            简短 = 原因.split("\n")[0].strip()[:38]
            if 简短:
                文本 += f" · {简短}"
        elif 状态 == "跳过":
            原因 = str(任务.get("跳过原因") or "")
            if 原因:
                文本 += f" · {原因[:34]}"
        elif 状态 == "已完成" and "兜底" in str(任务.get("阶段详情") or ""):
            文本 += "（兜底）"
        return 文本

    def _速度文本(self, 任务: dict) -> tuple[str, str]:
        现在 = time.time()
        完成 = int(任务.get("已传输") or 0)
        总量 = int(任务.get("大小") or 0)
        键 = str(任务.get("任务ID") or "")
        速度 = 0.0
        上次 = self._速度.get(键)
        if 上次 is not None:
            上次时间, 上次字节, 上次速度 = 上次
            if 现在 > 上次时间:
                瞬时 = max(0.0, (完成 - 上次字节) / (现在 - 上次时间))
                速度 = 瞬时 if 瞬时 > 0 else 上次速度 * 0.5
        self._速度[键] = (现在, 完成, 速度)
        if str(任务.get("状态")) != "进行中":
            return ("✅" if 任务.get("状态") == "已完成"
                    else "❌" if 任务.get("状态") == "失败"
                    else "⏸" if 任务.get("状态") == "暂停" else "-"), "-"
        if 速度 <= 0:
            return "…", "-"
        剩余 = (f"{(总量 - 完成) / 速度:.0f}s" if 总量 > 完成 > 0 else "-")
        return f"{大小文本(int(速度))}/s", 剩余

    def _设文本(self, 行: int, 列: int, 文本: str, 提示: str = "") -> None:
        单元 = self.item(行, 列)
        if 单元 is None:
            单元 = QTableWidgetItem()
            self.setItem(行, 列, 单元)
        if 单元.text() != 文本:
            单元.setText(文本)
        if 提示 and 单元.toolTip() != 提示:
            单元.setToolTip(提示)

    # ---------------- 操作列 ----------------

    def _渲染操作(self, 行: int, 任务: dict) -> None:
        容器 = self.cellWidget(行, 7)
        if not isinstance(容器, QWidget):
            容器 = QWidget()
            布局 = QHBoxLayout(容器)
            布局.setContentsMargins(0, 0, 0, 0)
            布局.setSpacing(2)
            for 键, 文本, 提示 in (
                    ("暂停", "⏸", "暂停这个任务（保留断点，可继续）"),
                    ("继续", "▶", "继续/开始这个任务"),
                    ("重试", "🔄", "重新传输这个文件"),
                    ("详情", "ℹ️", "查看完整任务信息"),
                    ("取消", "🗑", "取消/移出列表")):
                按钮 = QToolButton()
                按钮.setText(文本)
                按钮.setToolTip(提示)
                按钮.setObjectName(键)
                按钮.setFixedSize(26, 22)
                按钮.setStyleSheet(
                    "QToolButton { border: 1px solid palette(mid); "
                    "border-radius: 3px; padding: 0px; }"
                    "QToolButton:hover { background: palette(midlight); }"
                    "QToolButton:disabled { color: palette(mid); }")
                按钮.clicked.connect(
                    lambda _=False, k=键, r=行: self._点操作(k, r))
                布局.addWidget(按钮)
            布局.addStretch(1)
            self.setCellWidget(行, 7, 容器)
        状态 = str(任务.get("状态") or "")
        按需 = {"暂停": 状态 in ("进行中", "等待"),
              "继续": 状态 in ("暂停", "失败", "等待"),
              "重试": 状态 in ("失败", "跳过", "已完成", "暂停"),
              "详情": True, "取消": 状态 not in ("已完成",)}
        for 按钮 in 容器.findChildren(QToolButton):
            按钮.setEnabled(bool(按需.get(按钮.objectName(), True)))

    def _点操作(self, 键: str, 行: int) -> None:
        任务 = self._任务行[行] if 0 <= 行 < len(self._任务行) else {}
        任务ID = str(任务.get("任务ID") or "")
        if not 任务ID:
            return
        {"暂停": self.请求暂停, "继续": self.请求继续, "重试": self.请求重试,
         "详情": self.请求详情, "取消": self.请求取消}[键].emit(任务ID)

    def _双击(self, 索引) -> None:
        任务 = self._任务行[索引.row()] if 0 <= 索引.row() < len(self._任务行) else {}
        任务ID = str(任务.get("任务ID") or "")
        if 任务ID:
            self.请求详情.emit(任务ID)

    # ---------------- 右键菜单 ----------------

    def _右键菜单(self, 位置) -> None:
        行 = self.rowAt(位置.y())
        if 行 < 0 or 行 >= len(self._任务行):
            return
        任务 = self._任务行[行]
        任务ID = str(任务.get("任务ID") or "")
        if not 任务ID:
            return
        菜单 = QMenu(self)
        动作表 = [
            ("ℹ️ 查看详情", lambda: self.请求详情.emit(任务ID)),
            ("⏸ 暂停", lambda: self.请求暂停.emit(任务ID)),
            ("▶ 继续", lambda: self.请求继续.emit(任务ID)),
            ("🔄 重新传输", lambda: self.请求重试.emit(任务ID)),
            None,
            ("📋 复制源路径", lambda: self._复制(str(任务.get("源路径") or ""))),
            ("📋 复制目标路径",
             lambda: self._复制(str(任务.get("目标路径") or ""))),
            ("📋 复制失败原因",
             lambda: self._复制(str(任务.get("错误") or ""))),
            ("📂 打开本地中转目录",
             lambda: self.请求复制.emit(
                 str(任务.get("本地缓存") or ""))),
            None,
            ("🗑 取消 / 移出列表", lambda: self.请求取消.emit(任务ID)),
        ]
        for 项 in 动作表:
            if 项 is None:
                菜单.addSeparator()
                continue
            名称, 回调 = 项
            动作 = QAction(名称, 菜单)
            动作.triggered.connect(回调)
            菜单.addAction(动作)
        菜单.exec(self.viewport().mapToGlobal(位置))

    @staticmethod
    def _复制(文本: str) -> None:
        if 文本:
            QGuiApplication.clipboard().setText(文本)

    # ---------------- 导出 ----------------

    def 导出CSV(self, 路径: str = "") -> str:
        if not 路径:
            路径, _ = QFileDialog.getSaveFileName(
                self, "导出任务列表", "传输任务.csv", "CSV (*.csv)")
        if not 路径:
            return ""
        行们 = self.当前显示() or self.全部任务()
        with Path(路径).open("w", newline="", encoding="utf-8-sig") as f:
            写 = csv.writer(f)
            写.writerow(["文件名", "类型", "大小(字节)", "已传输(字节)", "状态",
                      "进度", "开始时间", "结束时间", "耗时(秒)", "重试次数",
                      "源网盘", "源路径", "目标网盘", "目标路径",
                      "实际落盘名", "跳过原因", "失败原因", "阶段"])
            for 任务 in 行们:
                开始 = float(任务.get("开始") or 0)
                结束 = float(任务.get("结束") or 0)
                写.writerow([
                    str(任务.get("名称") or ""),
                    str(任务.get("类型") or ""),
                    int(任务.get("大小") or 0),
                    int(任务.get("已传输") or 0),
                    str(任务.get("状态") or ""),
                    (f"{int(int(任务.get('已传输') or 0) * 100 / int(任务['大小']))}%"
                     if int(任务.get("大小") or 0) else ""),
                    时间文本(开始), 时间文本(结束),
                    (f"{结束 - 开始:.1f}" if 开始 and 结束 else ""),
                    int(任务.get("重试次数") or 0),
                    str(任务.get("源网盘") or ""), str(任务.get("源路径") or ""),
                    str(任务.get("目标网盘") or ""), str(任务.get("目标路径") or ""),
                    str(任务.get("实际目标名") or ""),
                    str(任务.get("跳过原因") or ""),
                    str(任务.get("错误") or ""),
                    str(任务.get("阶段详情") or ""),
                ])
        return 路径
