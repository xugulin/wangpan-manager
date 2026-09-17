"""跨网盘传输页面（切换页版）。

布局
====
```
源网盘：[下拉]   源路径：[输入] [📂 浏览…]
目标网盘：[下拉] 目标路径：[输入] [📂 浏览…]
☐覆盖 总并发[自动] 重试[3] ☐保留中转 ☑用AI调整并发/优先级
[▶ 开始传输] [⏹ 停止] [♻ 恢复批次] [🔁 重试失败] [🧹 清空列表]  统计文本
┌ 任务表格：文件 / 状态 / 进度 / 大小 / 速度 / 剩余 / 错误 ─────────┐
└──────────────────────────────────────────────────────────────┘
┌ 传输状态面板（沿用 网盘管理_V8 表格下方那条横向统计栏）──────────┐
│ 📋 队列: N │ 📝 任务: <状态>                                    │
│ 📊 总大小 · 🆕 新上传 · 🔄 覆盖 · ⏳ 等待 · ⏭️ 跳过 · ❌ 失败      │
│ 计算网速 ⬆/⬇ │ 📦 文件速率(个/秒·分·时·天 自动) │ 已耗时/预计 │ 倒计时 │
└──────────────────────────────────────────────────────────────┘
```

与 V8 的差别：V8 那条统计栏第三行有「Alist网速」，V8_3 已经没有 Alist，
换成**文件速率**（个/秒 → 个/分 → 个/小时 → 个/天，按数值自动切换单位）。

任务来自三条路：跨网盘批次（传输引擎）、本地上传、本地下载。
上传统一走网盘实例的「敏感词上传守卫」（预检改名 + 失败绕过），
批次传输由引擎内部调用同一个守卫。
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from PySide6.QtCore import QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QFrame, QGridLayout,
    QHBoxLayout,
    QInputDialog, QLabel, QLineEdit, QMessageBox, QProgressBar, QPushButton,
    QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from ..配置 import 传输参数, 缓存参数, 数据库路径
from ..核心.传输引擎 import 传输请求
from ..核心.模型 import 规范路径, 拼路径
from .后台线程 import 传输线程, 恢复线程
from .任务表格 import 分类定义, 任务表格, 取分类
from .任务详情对话框 import 任务详情对话框
from .路径选择对话框 import 路径选择对话框


class 本地任务工作线程(QThread):
    """上传/下载任务的小并发执行器（每个任务一条进度事件流）。"""

    事件 = Signal(dict)
    完成 = Signal(dict)

    def __init__(self, 取适配器, 任务列表: list[dict], 取消: threading.Event,
                 并发: int = 4, 取守卫=None, 父=None):
        super().__init__(父)
        self._取适配器 = 取适配器
        self._取守卫 = 取守卫 or (lambda 标识: None)
        self.任务列表 = list(任务列表)
        self.取消 = 取消
        self.并发 = max(1, min(8, int(并发 or 4)))
        self._目录锁 = threading.Lock()
        self._已建目录: set[tuple[str, str]] = set()
        self._计数锁 = threading.Lock()
        self._成功 = 0
        self._失败 = 0
        self._取消数 = 0

    def _确保目录(self, 适配器, 标识: str, 目录: str) -> None:
        键 = (标识, 目录)
        with self._目录锁:
            if 键 in self._已建目录:
                return
        适配器.确保目录(目录)
        with self._目录锁:
            self._已建目录.add(键)

    @staticmethod
    def _任务字典(任务: dict, 状态: str, 进度: int = 0, 错误: str = "") -> dict:
        return {
            "task_id": 任务["task_id"],
            "source_path": 任务.get("显示", ""),
            "target_path": 任务.get("远端", ""),
            "status": 状态,
            "status_name": {"pending": "等待", "downloading": "下载",
                            "uploading": "上传", "done": "完成",
                            "failed": "失败", "cancelled": "已取消"}[状态],
            "size": int(任务.get("大小") or 0),
            "done_bytes": int(进度 or 0),
            "error": 错误,
            "cloud": 任务.get("标识", ""),
            "kind": 任务.get("kind", ""),
            "覆盖": bool(任务.get("覆盖")),
        }

    def _跑一个(self, 任务: dict) -> None:
        标识 = 任务["标识"]
        适配器 = self._取适配器(标识)
        if self.取消.is_set():
            with self._计数锁:
                self._取消数 += 1
            self.事件.emit({"type": "任务取消",
                          "task": self._任务字典(任务, "cancelled")})
            return
        状态 = "downloading" if 任务["kind"] == "download" else "uploading"
        self.事件.emit({"type": "任务开始",
                      "task": self._任务字典(任务, 状态)})

        总量 = {"值": int(任务.get("大小") or 0)}

        def 进度(阶段, 当前, 总量值):
            当前 = int(当前 or 0)
            总量["值"] = max(总量["值"], int(总量值 or 0))
            self.事件.emit({"type": "任务进度",
                          "task": self._任务字典(任务, 状态, 当前)})

        try:
            if 任务["kind"] == "upload":
                self._确保目录(适配器, 标识, 任务["远端"] or "/")
                守卫 = self._取守卫(标识)
                名称 = 任务.get("名称") or Path(任务["本地"]).name
                if 守卫 is not None and getattr(守卫, "生效", False):
                    守卫.上传(任务["本地"], 任务["远端"] or "/", 名称=名称,
                            任务ID=任务["task_id"], 进度=进度)
                else:
                    适配器.上传(任务["本地"], 任务["远端"] or "/", 名称=名称,
                             任务ID=任务["task_id"], 进度=进度)
            else:
                Path(任务["本地"]).parent.mkdir(parents=True, exist_ok=True)
                适配器.下载(任务["远端"], 任务["本地"],
                         任务ID=任务["task_id"], 进度=进度)
            大小 = 总量["值"] or int(任务.get("大小") or 0)
            with self._计数锁:
                self._成功 += 1
            self.事件.emit({"type": "任务完成",
                          "task": self._任务字典(任务, "done", 大小)})
        except Exception as e:  # noqa: BLE001
            with self._计数锁:
                self._失败 += 1
            self.事件.emit({"type": "任务失败",
                          "task": self._任务字典(任务, "failed", 0, str(e))})

    def run(self):
        try:
            with ThreadPoolExecutor(max_workers=self.并发,
                                    thread_name_prefix="本地任务") as 池:
                未来 = {池.submit(self._跑一个, 任务): 任务
                       for 任务 in self.任务列表}
                for 未 in as_completed(未来):
                    未.result()
        except Exception as e:  # noqa: BLE001
            self.完成.emit({"error": str(e)})
            return
        with self._计数锁:
            统计 = {
                "总数": len(self.任务列表),
                "成功": self._成功,
                "失败": self._失败,
                "取消": self._取消数,
                "请求停止": self.取消.is_set(),
            }
        self.完成.emit(统计)


class 传输状态面板(QWidget):
    """表格下方那条横向统计栏（沿用 V8 的三行结构）。

    * 第一行：📋 队列 / 📝 任务状态
    * 第二行：📊 总大小 · 🆕 新上传 · 🔄 覆盖 · ⏳ 等待 · ⏭️ 跳过 · ❌ 失败
    * 第三行：计算网速 ⬆/⬇ · 📦 文件速率 · 已耗时/预计 · 倒计时
    """

    队列值宽 = 70
    内存值宽 = 84
    网速值宽 = 200
    速率值宽 = 150
    耗时值宽 = 170
    倒计时值宽 = 150

    def __init__(self, 父=None):
        super().__init__(父)
        self.setObjectName("传输状态面板")
        self.setStyleSheet("""
            QWidget#传输状态面板 {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 #2c3e50, stop:1 #34495e);
                border-radius: 8px;
            }
            QWidget#传输状态面板 QLabel { background: transparent; }
        """)
        外层 = QVBoxLayout(self)
        外层.setContentsMargins(12, 6, 12, 6)
        外层.setSpacing(4)

        self.标题样式 = ("color: #95a5a6; font-size: 12px; "
                    "background: transparent;")
        self.值样式 = ("color: #ecf0f1; font-size: 12px; "
                   "font-weight: bold; background: transparent;")

        第一行 = QHBoxLayout()
        第一行.addWidget(self._标题("📋 队列:", 55))
        self.队列数标签 = QLabel("0")
        self.队列数标签.setStyleSheet(
            "color: #f39c12; font-size: 12px; font-weight: bold; "
            "background: transparent;")
        self.队列数标签.setFixedWidth(self.队列值宽)
        第一行.addWidget(self.队列数标签)
        第一行.addWidget(self._分隔线())
        第一行.addWidget(self._标题("📝 任务:", 55))
        self.状态文本标签 = QLabel("就绪")
        self.状态文本标签.setStyleSheet(
            "color: #bdc3c7; font-size: 12px; font-weight: bold; "
            "background: transparent;")
        第一行.addWidget(self.状态文本标签, 1)
        第一行.addWidget(self._分隔线())
        # ★ 决策来源 + 当前决策（V8_3 新增：一眼看出这批任务听谁的）
        第一行.addWidget(self._标题("🧭 决策来源:"))
        self.决策来源标签 = QLabel("规则基线")
        self.决策来源标签.setStyleSheet(
            "color: #9b59b6; font-size: 12px; font-weight: bold; "
            "background: transparent;")
        self.决策来源标签.setMinimumWidth(150)
        第一行.addWidget(self.决策来源标签)
        第一行.addWidget(self._分隔线())
        第一行.addWidget(self._标题("⚙ 当前决策:"))
        self.决策标签 = QLabel("—")
        self.决策标签.setStyleSheet(
            "color: #1abc9c; font-size: 12px; font-weight: bold; "
            "background: transparent;")
        self.决策标签.setMinimumWidth(280)
        第一行.addWidget(self.决策标签, 1)
        外层.addLayout(第一行)
        外层.addWidget(self._水平线())

        第二行 = QHBoxLayout()
        self.内存值标签: dict[str, QLabel] = {}
        for 标题文本, 键 in (("📊 总大小:", "总大小"), ("🆕 新上传:", "新上传"),
                          ("🔄 覆盖:", "覆盖"), ("⏳ 等待:", "等待"),
                          ("⏭️ 跳过:", "跳过"), ("❌ 失败:", "失败"),
                          ("⏸ 暂停:", "暂停"), ("🛟 兜底:", "兜底")):
            第二行.addWidget(self._标题(标题文本))
            值 = QLabel("0 B")
            值.setStyleSheet(self.值样式)
            值.setFixedWidth(self.内存值宽)
            第二行.addWidget(值)
            self.内存值标签[键] = 值
        第二行.addStretch()
        外层.addLayout(第二行)
        外层.addWidget(self._水平线())

        第三行 = QHBoxLayout()
        第三行.addWidget(self._标题("计算网速:"))
        self.计算网速标签 = QLabel("⬆ 0 B/s | ⬇ 0 B/s")
        self.计算网速标签.setStyleSheet(self.值样式)
        self.计算网速标签.setFixedWidth(self.网速值宽)
        第三行.addWidget(self.计算网速标签)
        第三行.addWidget(self._分隔线())

        第三行.addWidget(self._标题("📦 文件速率:"))
        self.文件速率标签 = QLabel("0.00 个/秒")
        self.文件速率标签.setStyleSheet(
            "color: #2ecc71; font-size: 12px; font-weight: bold; "
            "background: transparent;")
        self.文件速率标签.setFixedWidth(self.速率值宽)
        self.文件速率标签.setToolTip(
            "已完成文件数 ÷ 本次耗时；单位按数值自动切换 个/秒·分·小时·天")
        第三行.addWidget(self.文件速率标签)
        第三行.addWidget(self._分隔线())

        第三行.addWidget(self._标题("已耗时/预计:"))
        self.耗时对比标签 = QLabel("--:--:-- / --:--:--")
        self.耗时对比标签.setStyleSheet(
            "color: #3498db; font-size: 12px; font-weight: bold; "
            "background: transparent;")
        self.耗时对比标签.setFixedWidth(self.耗时值宽)
        第三行.addWidget(self.耗时对比标签)
        第三行.addWidget(self._分隔线())

        第三行.addWidget(self._标题("倒计时:"))
        self.倒计时标签 = QLabel("--:--:-- (空闲)")
        self.倒计时标签.setStyleSheet(
            "color: #e74c3c; font-size: 12px; font-weight: bold; "
            "background: transparent;")
        self.倒计时标签.setFixedWidth(self.倒计时值宽)
        第三行.addWidget(self.倒计时标签)
        第三行.addStretch()
        外层.addLayout(第三行)

    # ---------- 小部件工厂 ----------

    def _标题(self, 文本: str, 宽: int | None = None) -> QLabel:
        标签 = QLabel(文本)
        标签.setStyleSheet(self.标题样式)
        if 宽:
            标签.setFixedWidth(宽)
        return 标签

    def _分隔线(self) -> QFrame:
        线 = QFrame()
        线.setFrameShape(QFrame.VLine)
        线.setStyleSheet("color: #4a6274; background: #4a6274; max-width: 1px;")
        return 线

    def _水平线(self) -> QFrame:
        线 = QFrame()
        线.setFrameShape(QFrame.HLine)
        线.setStyleSheet("color: #4a6274; background: #4a6274; max-height: 1px;")
        return 线

    # ---------- 对外更新 ----------

    def 设置状态(self, 文本: str, 颜色: str = "#bdc3c7"):
        self.状态文本标签.setText(文本 or "就绪")
        self.状态文本标签.setStyleSheet(
            f"color: {颜色}; font-size: 12px; font-weight: bold; "
            "background: transparent;")

    def 设置决策(self, 来源: str, 摘要: str,
                 颜色: str = "#9b59b6", 决策颜色: str = "#1abc9c"):
        self.决策来源标签.setText(来源 or "—")
        self.决策来源标签.setToolTip(来源 or "")
        self.决策来源标签.setStyleSheet(
            f"color: {颜色}; font-size: 12px; font-weight: bold; "
            "background: transparent;")
        self.决策标签.setText(摘要 or "—")
        self.决策标签.setToolTip(摘要 or "")
        self.决策标签.setStyleSheet(
            f"color: {决策颜色}; font-size: 12px; font-weight: bold; "
            "background: transparent;")

    def 设置队列(self, 数量: int):
        self.队列数标签.setText(str(int(数量)))

    def 设置字节(self, 统计: dict):
        for 键, 标签 in self.内存值标签.items():
            标签.setText(传输页面.格式化大小(统计.get(键, 0)))

    def 设置网速(self, 上传速度: float, 下载速度: float):
        self.计算网速标签.setText(
            f"⬆ {传输页面.格式化速度(上传速度)}"
            f" | ⬇ {传输页面.格式化速度(下载速度)}")

    def 设置文件速率(self, 每秒个数: float):
        self.文件速率标签.setText(传输页面.格式化文件速率(每秒个数))

    def 设置耗时(self, 已耗时: float, 预计: float | None):
        已 = 传输页面.格式化时长(已耗时)
        预 = "计算中…" if 预计 is None else 传输页面.格式化时长(预计)
        self.耗时对比标签.setText(f"{已} / {预}")

    def 设置倒计时(self, 剩余秒: float | None, 后缀: str):
        if 剩余秒 is None:
            self.倒计时标签.setText(f"计算中… {后缀}")
        else:
            self.倒计时标签.setText(
                f"{传输页面.格式化时长(剩余秒)} {后缀}")


class 传输页面(QWidget):
    """跨网盘传输 + 本地上传/下载的统一任务页。"""

    def __init__(self, 主窗口, 父=None):
        super().__init__(父)
        self.主窗口 = 主窗口
        self.动作 = 主窗口.动作
        self.配置 = 主窗口.配置
        self._任务行: dict[str, int] = {}
        self._任务速度: dict[str, tuple[float, int, float]] = {}
        self._任务: dict[str, dict] = {}          # 统计用任务台账
        self._任务线程: list[QThread] = []
        self._批次线程: QThread | None = None
        self._本地线程: QThread | None = None
        self._取消 = threading.Event()
        self._本地取消 = threading.Event()
        self._当前批次ID = ""
        self._本地序号 = 0
        self._本地待办: list[dict] = []      # 上一批还在跑时，任务先排队
        self._本地累计 = {"成功": 0, "失败": 0, "取消": 0}   # 跨排队批次累计
        self._计时起点 = 0.0
        self._传输结束时间 = 0.0
        # 单任务操作（暂停/继续/重试）需要的批次上下文
        self._引擎 = None
        self._当前请求 = None
        self._统计对象 = None
        self._统计锁 = threading.Lock()
        self.AI调度器 = None                       # 由主窗口注入（可选）
        self._构建()
        self.刷新网盘列表()
        self._刷新统计面板()

    # ==================== 构建 ====================

    def _构建(self):
        布局 = QVBoxLayout(self)
        布局.setContentsMargins(0, 0, 0, 0)
        布局.setSpacing(8)

        # 源/目标各占一行：网盘 + 路径 在同一行
        表单 = QGridLayout()
        表单.setHorizontalSpacing(8)
        表单.setVerticalSpacing(6)

        表单.addWidget(QLabel("源网盘："), 0, 0)
        self.源网盘框 = QComboBox()
        self.源网盘框.setMinimumWidth(190)
        表单.addWidget(self.源网盘框, 0, 1)
        表单.addWidget(QLabel("源路径："), 0, 2)
        self.源路径框 = QLineEdit("/")
        表单.addWidget(self.源路径框, 0, 3)
        源浏览 = QPushButton("📂 浏览…")
        源浏览.clicked.connect(
            lambda: self._浏览(self.源网盘框, self.源路径框, True))
        表单.addWidget(源浏览, 0, 4)

        表单.addWidget(QLabel("目标网盘："), 1, 0)
        self.目标网盘框 = QComboBox()
        self.目标网盘框.setMinimumWidth(190)
        表单.addWidget(self.目标网盘框, 1, 1)
        表单.addWidget(QLabel("目标路径："), 1, 2)
        self.目标路径框 = QLineEdit("/")
        表单.addWidget(self.目标路径框, 1, 3)
        目标浏览 = QPushButton("📂 浏览…")
        目标浏览.clicked.connect(
            lambda: self._浏览(self.目标网盘框, self.目标路径框, False))
        表单.addWidget(目标浏览, 1, 4)

        表单.setColumnStretch(3, 1)
        布局.addLayout(表单)

        选项 = QHBoxLayout()
        self.覆盖框 = QCheckBox("覆盖目标已存在文件")
        self.并发框 = QSpinBox()
        self.并发框.setRange(0, 64)
        self.并发框.setSpecialValueText("自动")
        self.重试框 = QSpinBox()
        self.重试框.setRange(0, 10)
        self.重试框.setValue(int(传输参数(self.配置).get("重试次数") or 3))
        self.保留中转框 = QCheckBox("保留失败中转文件")
        self.兜底框 = QCheckBox("失败后本地中转兜底")
        self.兜底框.setChecked(bool(传输参数(self.配置).get("失败兜底", True)))
        self.兜底框.setToolTip(
            "常规重试（含直链多段/续传）都失败后，改用「源→本地→目标」保守模式再试一次：\n"
            "清干净中转、跳过直链、改用适配器原生下载，并**串行**执行（默认 1 个）"
            "以免同时铺开多个大文件占磁盘。\n"
            "磁盘占用受 传输.暂存上限GB / 传输.磁盘余量GB 约束。")
        self.用AI框 = QCheckBox("用 AI 调整并发/优先级")
        self.用AI框.setChecked(True)
        self.用AI框.setToolTip(
            "开启后：扫描完源目录会把任务特征交给 AI 调度器，用它返回的策略"
            "调整并发/重试；结束时回填效果，失败时请求诊断")
        选项.addWidget(self.覆盖框)
        选项.addWidget(QLabel("总并发："))
        选项.addWidget(self.并发框)
        选项.addWidget(QLabel("重试："))
        选项.addWidget(self.重试框)
        选项.addWidget(self.保留中转框)
        选项.addWidget(self.兜底框)
        选项.addWidget(self.用AI框)
        选项.addStretch(1)
        布局.addLayout(选项)

        操作 = QHBoxLayout()
        self.开始按钮 = QPushButton("▶ 开始传输")
        self.开始按钮.setObjectName("PrimaryButton")
        self.开始按钮.clicked.connect(self._开始传输)
        self.停止按钮 = QPushButton("⏹ 停止")
        self.停止按钮.clicked.connect(self._停止)
        self.停止按钮.setEnabled(False)
        恢复按钮 = QPushButton("♻ 恢复批次")
        恢复按钮.clicked.connect(self._选择并恢复)
        重试按钮 = QPushButton("🔁 重试失败")
        self.重试按钮 = 重试按钮
        重试按钮.clicked.connect(self._重试失败)
        重试按钮.setEnabled(False)
        清空按钮 = QPushButton("🧹 清空列表")
        清空按钮.clicked.connect(self._清空列表)
        操作.addWidget(self.开始按钮)
        操作.addWidget(self.停止按钮)
        操作.addWidget(恢复按钮)
        操作.addWidget(重试按钮)
        操作.addWidget(清空按钮)
        self.统计标签 = QLabel("就绪")
        操作.addWidget(self.统计标签, 1)
        布局.addLayout(操作)

        # ---------- V8 形态：分类标签栏（带计数） ----------
        分类栏 = QHBoxLayout()
        分类栏.setSpacing(6)
        self.分类按钮组: dict[str, QPushButton] = {}
        for 键, 文本, 颜色 in 分类定义:
            按钮 = QPushButton(f"{文本} (0)")
            按钮.setCheckable(True)
            按钮.setAutoExclusive(True)
            按钮.setStyleSheet(
                "QPushButton { background: transparent; border: 1px solid "
                "palette(mid); border-radius: 4px; padding: 6px 12px; "
                "font-weight: bold; }"
                "QPushButton:hover { background: palette(midlight); }"
                f"QPushButton:checked {{ background: {颜色}; color: white; "
                f"border: 1px solid {颜色}; }}")
            按钮.clicked.connect(lambda _=False, k=键: self._切换分类(k))
            self.分类按钮组[键] = 按钮
            分类栏.addWidget(按钮)
        self.分类按钮组["全部"].setChecked(True)
        分类栏.addStretch(1)
        self.全部暂停按钮 = QPushButton("⏸ 全部暂停")
        self.全部暂停按钮.clicked.connect(self._全部暂停)
        分类栏.addWidget(self.全部暂停按钮)
        self.全部继续按钮 = QPushButton("▶ 全部继续")
        self.全部继续按钮.clicked.connect(self._全部继续)
        分类栏.addWidget(self.全部继续按钮)
        导出按钮 = QPushButton("📤 导出列表")
        导出按钮.setToolTip("把当前分类的可见任务导出成 CSV（含时间点与失败原因）")
        导出按钮.clicked.connect(self._导出列表)
        分类栏.addWidget(导出按钮)
        布局.addLayout(分类栏)

        # ---------- V8 形态：8 列 + 完成/失败时间列 ----------
        self.任务表 = 任务表格(self)
        self.任务表.请求暂停.connect(self._暂停任务)
        self.任务表.请求继续.connect(self._继续任务)
        self.任务表.请求重试.connect(self._重试任务)
        self.任务表.请求取消.connect(self._取消任务)
        self.任务表.请求详情.connect(self._显示详情)
        self.任务表.请求复制.connect(self._复制中转路径)
        布局.addWidget(self.任务表, 1)

        # V8 那条贴在表格下方的横向统计栏
        self.状态面板 = 传输状态面板(self)
        布局.addWidget(self.状态面板)

        # 每秒刷新一次统计（速度/速率/倒计时）
        self.刷新定时器 = QTimer(self)
        self.刷新定时器.timeout.connect(self._定时刷新界面)
        self.刷新定时器.start(1000)

    # ==================== 网盘下拉 ====================

    def 刷新网盘列表(self):
        规格表 = self.动作.规格表
        当前源 = self.源网盘框.currentData()
        当前目标 = self.目标网盘框.currentData()
        标签 = [(标识, f"{规格.图标} {规格.显示名}")
                for 标识, 规格 in 规格表.items()]
        for 框, 选中 in ((self.源网盘框, 当前源), (self.目标网盘框, 当前目标)):
            框.blockSignals(True)
            框.clear()
            for 标识, 文本 in 标签:
                框.addItem(文本, 标识)
            idx = 框.findData(选中)
            框.setCurrentIndex(idx if idx >= 0 else 0)
            框.blockSignals(False)
        self.开始按钮.setEnabled(bool(标签))

    def _浏览(self, 网盘框: QComboBox, 路径框: QLineEdit, 允许文件: bool):
        标识 = 网盘框.currentData()
        规格 = self.动作.规格(标识)
        if 规格 is None:
            QMessageBox.warning(self, "提示", "请先选择网盘（左下角可新增网盘）")
            return
        try:
            适配器 = self.动作.适配器(标识)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "适配器不可用", str(e))
            return
        对话框 = 路径选择对话框(
            适配器, 标识, 初始路径=路径框.text() or "/",
            允许选择文件=允许文件, 允许新建=not 允许文件,
            标题=f"选择路径 · {规格.显示名}", 父窗口=self)
        if 对话框.exec() == QDialog.Accepted and 对话框.选中路径:
            路径框.setText(对话框.选中路径)

    # ==================== 任务台账 / 表格 ====================

    def _记任务(self, 任务: dict):
        任务ID = str(任务.get("task_id") or "")
        if not 任务ID:
            return
        台账 = self._任务.setdefault(任务ID, {})
        状态名 = str(任务.get("status_name") or 任务.get("status") or "")
        台账.update({
            "任务ID": 任务ID,
            "名称": str(任务.get("source_path") or ""),
            "源路径": str(任务.get("source_path") or ""),
            "目标路径": str(任务.get("target_path") or ""),
            "源网盘": str(任务.get("source_cloud_name")
                      or 任务.get("source_cloud") or ""),
            "目标网盘": str(任务.get("target_cloud_name")
                       or 任务.get("target_cloud") or ""),
            # 状态统一存**分类名**（进行中/已完成/等待/暂停/跳过/失败），
            # 表格与统计都按它分组；原始状态（下载/上传/完成…）另存，用于更细的显示。
            "状态": 取分类({**任务, "状态": 状态名}),
            "原始状态": 状态名,
            "大小": int(任务.get("size") or 0),
            "已传输": int(任务.get("done_bytes") or 0),
            "类型": str(任务.get("kind") or 台账.get("类型") or ""),
            "覆盖": bool(任务.get("覆盖", 台账.get("覆盖"))),
            "重试次数": int(任务.get("retries") or 0),
            "跳过原因": str(任务.get("skip_reason") or ""),
            "错误": str(任务.get("error") or ""),
            "实际目标名": str(任务.get("actual_target_name") or ""),
            "批次ID": str(任务.get("batch_id") or ""),
            "本地缓存": str(任务.get("local_cache") or ""),
            # 兜底完成的任务会带「兜底：本地中转」字样，状态栏单独计数
            "阶段详情": str(任务.get("stage") or 任务.get("阶段详情")
                        or 台账.get("阶段详情") or ""),
        })
        # 时间点：开始/结束（完成时间、失败时间都靠它）
        开始 = float(任务.get("started_at") or 0)
        结束 = float(任务.get("finished_at") or 0)
        if 开始:
            台账["开始"] = 开始
        elif 状态名 in ("下载", "上传", "进行中", "downloading", "uploading") \
                and not 台账.get("开始"):
            台账["开始"] = time.time()
        if 结束 and not 台账.get("结束"):
            台账["结束"] = 结束
        elif 状态名 in ("完成", "失败", "已取消", "done", "failed",
                      "cancelled") and not 台账.get("结束"):
            台账["结束"] = time.time()
        if 状态名 in ("下载", "上传") and not 台账.get("开始"):
            台账["开始"] = time.time()
            if not self._计时起点:
                self._计时起点 = 台账["开始"]
        if 状态名 in ("完成", "失败", "已取消") and not 台账.get("结束"):
            台账["结束"] = time.time()

    def _刷新任务表(self) -> None:
        """把台账喂给任务表格并刷新（分类过滤在表格内部做）。"""
        self.任务表.设置台账(self._任务)
        self.任务表.刷新()
        self._更新分类计数()

    def _更新分类计数(self) -> None:
        计数 = self.任务表.分类计数()
        for 键, 按钮 in self.分类按钮组.items():
            文本 = next((t for k, t, _ in 分类定义 if k == 键), 键)
            按钮.setText(f"{文本} ({计数.get(键, 0)})")

    def _切换分类(self, 分类: str) -> None:
        self.任务表.切换分类(分类)
        for 键, 按钮 in self.分类按钮组.items():
            按钮.setChecked(键 == 分类)
        self._刷新任务表()

    # ---------- 单任务操作（操作列 / 右键菜单） ----------

    def _取任务台账(self, 任务ID: str) -> dict:
        return self._任务.get(str(任务ID)) or {}

    def _暂停任务(self, 任务ID: str) -> None:
        引擎 = getattr(self, "_引擎", None)
        台账 = self._取任务台账(任务ID)
        if 引擎 is not None:
            引擎.暂停任务(任务ID)
        台账["状态"] = "暂停"
        台账["阶段详情"] = "暂停（已保留断点，继续时可续传）"
        self.主窗口.追加日志(f"⏸ 已请求暂停：{台账.get('名称') or 任务ID}")
        self._刷新任务表()

    def _继续任务(self, 任务ID: str) -> None:
        引擎 = getattr(self, "_引擎", None)
        台账 = self._取任务台账(任务ID)
        if 引擎 is None:
            return
        引擎.继续任务(任务ID)
        台账["状态"] = "等待"
        台账["错误"] = ""
        self._重跑任务(任务ID, 说明="继续")

    def _重试任务(self, 任务ID: str) -> None:
        if getattr(self, "_引擎", None) is None:
            return
        self._重跑任务(任务ID, 说明="重新传输")

    def _重跑任务(self, 任务ID: str, 说明: str = "重试") -> None:
        引擎 = getattr(self, "_引擎", None)
        请求 = getattr(self, "_当前请求", None)
        台账 = self._取任务台账(任务ID)
        if 引擎 is None or 请求 is None:
            QMessageBox.information(self, "提示", "请先开始一次传输（批次上下文需要它）")
            return
        try:
            from ..核心.模型 import 传输任务
            任务对象 = 传输任务.from_dict({
                "task_id": 任务ID,
                "source_cloud": str(台账.get("源网盘") or 请求.源网盘),
                "target_cloud": str(台账.get("目标网盘") or 请求.目标网盘),
                "source_path": 台账.get("源路径") or "",
                "target_path": 台账.get("目标路径") or "",
                "size": int(台账.get("大小") or 0),
                "status": "pending",
                "batch_id": 台账.get("批次ID") or 请求.批次ID,
            })
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "无法重跑", str(e))
            return
        self.主窗口.追加日志(f"🔄 {说明}：{台账.get('名称') or 任务ID}")
        引擎.重跑单任务(请求, 任务对象, self._批次事件,
                    统计=self._统计对象, 统计锁=self._统计锁)
        self._刷新任务表()

    def _取消任务(self, 任务ID: str) -> None:
        引擎 = getattr(self, "_引擎", None)
        if 引擎 is not None:
            引擎.暂停任务(任务ID)          # 先让它安全停下
        台账 = self._取任务台账(任务ID)
        台账["状态"] = "失败"
        台账["错误"] = 台账.get("错误") or "已取消（用户从任务表移除）"
        台账["结束"] = time.time()
        self._任务.pop(任务ID, None)       # 从列表移除
        self.主窗口.追加日志(f"🗑 已移除任务：{台账.get('名称') or 任务ID}")
        self._刷新任务表()

    def _显示详情(self, 任务ID: str) -> None:
        台账 = self._取任务台账(任务ID)
        if not 台账:
            return
        try:
            对话框 = 任务详情对话框(台账, self)
            对话框.exec()
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "无法显示详情", str(e))

    def _复制中转路径(self, 路径: str) -> None:
        路径 = str(路径 or "")
        if not 路径:
            return
        from PySide6.QtGui import QGuiApplication
        QGuiApplication.clipboard().setText(路径)

    def _导出列表(self) -> None:
        try:
            路径 = self.任务表.导出CSV()
            if 路径:
                self.主窗口.追加日志(f"📤 已导出任务列表：{路径}")
                self._设置状态(f"已导出 {路径}", "#27ae60")
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "导出失败", str(e))

    def _全部暂停(self) -> None:
        引擎 = getattr(self, "_引擎", None)
        if 引擎 is None:
            return
        引擎.全部暂停()
        for 台账 in self._任务.values():
            if 台账.get("状态") in ("进行中", "等待", "下载", "上传"):
                台账["状态"] = "暂停"
        self._设置状态("⏸ 已全部暂停（运行中的任务会在安全点停下）", "#9b27b0")
        self.主窗口.追加日志("⏸ 全部暂停")
        self._刷新任务表()

    def _全部继续(self) -> None:
        引擎 = getattr(self, "_引擎", None)
        if 引擎 is None:
            return
        引擎.全部继续()
        暂停们 = [k for k, v in self._任务.items()
                if v.get("状态") == "暂停"]
        for 任务ID in 暂停们:
            self._重跑任务(任务ID, 说明="继续")
        self._设置状态("▶ 已继续", "#27ae60")
        self.主窗口.追加日志("▶ 全部继续")
        self._刷新任务表()

    # ==================== 统计面板 ====================

    @staticmethod
    def 格式化大小(字节) -> str:
        try:
            值 = float(字节 or 0)
        except (TypeError, ValueError):
            return "0 B"
        for 单位 in ("B", "KiB", "MiB", "GiB", "TiB"):
            if 值 < 1024 or 单位 == "TiB":
                return f"{值:.1f} {单位}" if 单位 != "B" else f"{int(值)} B"
            值 /= 1024
        return str(字节)

    @staticmethod
    def 格式化速度(字节每秒: float) -> str:
        try:
            值 = float(字节每秒 or 0)
        except (TypeError, ValueError):
            值 = 0.0
        if 值 <= 0:
            return "0 B/s"
        return f"{传输页面.格式化大小(int(值))}/s"

    @staticmethod
    def 格式化文件速率(每秒个数: float) -> str:
        """自动切换单位：个/秒 → 个/分 → 个/小时 → 个/天。"""
        try:
            速率 = float(每秒个数 or 0)
        except (TypeError, ValueError):
            速率 = 0.0
        if 速率 <= 0:
            return "0.00 个/秒"
        if 速率 >= 1.0:
            数值, 单位 = 速率, "个/秒"
        elif 速率 >= 1.0 / 60:
            数值, 单位 = 速率 * 60, "个/分"
        elif 速率 >= 1.0 / 3600:
            数值, 单位 = 速率 * 3600, "个/小时"
        else:
            数值, 单位 = 速率 * 86400, "个/天"
        if 数值 >= 100:
            return f"{数值:.0f} {单位}"
        if 数值 >= 10:
            return f"{数值:.1f} {单位}"
        return f"{数值:.2f} {单位}"

    @staticmethod
    def 格式化时长(秒: float) -> str:
        秒 = max(0.0, float(秒 or 0))
        天 = int(秒 // 86400)
        余 = 秒 - 天 * 86400
        文本 = (f"{int(余 // 3600):02d}:{int(余 % 3600 // 60):02d}:"
              f"{int(余 % 60):02d}")
        return f"{天}天{文本}" if 天 else 文本

    def _台账统计(self) -> dict:
        统计 = {
            "队列": 0, "总大小": 0, "新上传": 0, "覆盖": 0, "等待": 0,
            "跳过": 0, "失败": 0, "已完成": 0, "兜底": 0, "暂停": 0,
            "上传字节": 0, "下载字节": 0, "剩余字节": 0,
            "完成文件": 0, "剩余文件": 0, "未完成": 0,
        }
        for 台账 in self._任务.values():
            大小 = int(台账.get("大小") or 0)
            已传 = int(台账.get("已传输") or 0)
            状态 = 台账.get("状态") or ""
            类型 = 台账.get("类型") or ""
            统计["总大小"] += 大小
            if 状态 in ("已完成", "完成"):
                统计["已完成"] += 1
                统计["完成文件"] += 1
                if "兜底" in str(台账.get("阶段详情") or ""):
                    统计["兜底"] += 大小 or 已传
                if 类型 == "download":
                    统计["下载字节"] += 已传 or 大小
                elif 台账.get("覆盖"):
                    统计["覆盖"] += 大小 or 已传
                else:
                    统计["新上传"] += 大小 or 已传
            elif 状态 in ("跳过", "已跳过"):
                统计["跳过"] += 大小
                统计["完成文件"] += 1
            elif 状态 in ("暂停", "已暂停"):
                统计["暂停"] += 大小
                统计["剩余文件"] += 1
                统计["未完成"] += 1
            elif 状态 in ("失败", "已取消"):
                # 失败/取消不算"未完成"（与分类标签一致：未完成=跑着/排队/暂停），
                # 也不算"剩余文件"，否则预计耗时会把这些不可能再传的文件算进去。
                统计["失败"] += 大小
            elif 状态 == "进行中":
                # 正在跑的：算未完成/剩余文件，但不占"等待"的字节
                统计["未完成"] += 1
                统计["剩余文件"] += 1
            else:
                统计["等待"] += 大小
                统计["未完成"] += 1
                统计["剩余文件"] += 1
        统计["队列"] = 统计["未完成"]
        统计["上传字节"] = 统计["新上传"] + 统计["覆盖"]
        统计["剩余字节"] = max(
            0, 统计["总大小"] - 统计["上传字节"] - 统计["下载字节"]
            - 统计["跳过"] - 统计["失败"])
        if 统计["未完成"] == 0 and self._传输结束时间 == 0 and self._计时起点:
            self._传输结束时间 = time.time()
        return 统计

    def _定时刷新界面(self) -> None:
        """每秒刷一次：统计面板 + 任务表（速度/耗时是实时量，必须重画）。"""
        self._刷新统计面板()
        if self._任务:
            self.刷新任务表()
            self._更新分类计数()

    def 刷新任务表(self) -> None:
        """公开版：把台账喂给表格并重画（供定时器与事件回调调用）。"""
        try:
            self.任务表.设置台账(self._任务)
            self.任务表.刷新()
        except Exception:
            pass

    def _刷新统计面板(self, *_):
        统计 = self._台账统计()
        现在 = time.time()
        结束 = self._传输结束时间 or 现在
        墙钟 = max(0.0, 结束 - self._计时起点) if self._计时起点 else 0.0
        有效 = 墙钟 >= 0.2
        上传速度 = 统计["上传字节"] / 墙钟 if 有效 else 0.0
        下载速度 = 统计["下载字节"] / 墙钟 if 有效 else 0.0
        文件速率 = 统计["完成文件"] / 墙钟 if 有效 else 0.0

        self.状态面板.设置队列(统计["队列"])
        self.状态面板.设置字节(统计)
        self.状态面板.设置网速(上传速度, 下载速度)
        self.状态面板.设置文件速率(文件速率)

        未完成 = 统计["未完成"]
        if self._传输结束时间:
            后缀 = "(已结束)"
        elif 未完成 > 0:
            后缀 = "(传输中)"
        else:
            后缀 = "(空闲)"
        if not self._计时起点:
            self.状态面板.设置耗时(0.0, 0.0)
            self.状态面板.设置倒计时(None, 后缀)
            return
        if 未完成 <= 0:
            self.状态面板.设置耗时(墙钟, 墙钟)
            self.状态面板.设置倒计时(0.0, 后缀)
            return
        平均速度 = ((统计["上传字节"] + 统计["下载字节"]) / 墙钟
                 if 有效 else 0.0)
        剩余秒 = None
        if 统计["剩余字节"] > 0 and 平均速度 > 0:
            剩余秒 = min(统计["剩余字节"] / 平均速度, 86400 * 7)
        elif 文件速率 > 0:
            剩余秒 = min(统计["剩余文件"] / 文件速率, 86400 * 7)
        if 剩余秒 is None:
            self.状态面板.设置耗时(墙钟, None)
            self.状态面板.设置倒计时(None, 后缀)
            return
        self.状态面板.设置耗时(墙钟, min(墙钟 + 剩余秒, 86400 * 7))
        self.状态面板.设置倒计时(剩余秒, 后缀)

    def _设置状态(self, 文本: str, 颜色: str = "#bdc3c7"):
        self.状态面板.设置状态(文本, 颜色)
        self.统计标签.setText(文本)

    def _设置决策(self, 来源: str, 摘要: str, 颜色: str = "#9b59b6",
                  决策颜色: str = "#1abc9c"):
        self.状态面板.设置决策(来源, 摘要, 颜色, 决策颜色)

    @staticmethod
    def _决策摘要(数据: dict, 消息: str = "") -> str:
        """把 AI/规则返回的策略压成一行：并发 + 原因（+ 优先级规则）。"""
        片段: list[str] = []
        并发 = int(数据.get("并发") or 0)
        片段.append(f"并发 {并发}" if 并发 else "并发 自动")
        if 数据.get("优先级规则"):
            片段.append("优先 " + "、".join(str(x) for x in
                                      数据["优先级规则"][:2]))
        if 数据.get("分批策略") and str(数据["分批策略"]) not in ("顺序处理", ""):
            片段.append(str(数据["分批策略"]))
        原因 = str(数据.get("调度原因") or "")
        if 原因:
            片段.append(原因)
        elif 消息:
            片段.append(str(消息))
        文本 = " ｜ ".join(x for x in 片段 if x)
        if 数据.get("来源") == "AI" and 数据.get("置信度"):
            文本 += f"（AI 置信度 {float(数据['置信度']):.2f}）"
        if 数据.get("吞吐_MBps"):
            文本 += f"（吞吐 {数据['吞吐_MBps']} MiB/s）"
        return 文本

    # ==================== 清空 ====================

    def _清空列表(self):
        if self._批次线程 is not None or self._本地线程 is not None:
            QMessageBox.information(self, "提示", "还有任务在跑，先停止再清空")
            return
        self.任务表.setRowCount(0)
        self._任务行.clear()
        self._任务速度.clear()
        self._任务.clear()
        self._本地累计 = {"成功": 0, "失败": 0, "取消": 0}
        self._本地待办.clear()
        self._计时起点 = 0.0
        self._传输结束时间 = 0.0
        self._设置状态("就绪")
        self._刷新任务表()
        self._刷新统计面板()

    # ==================== 批次传输 ====================

    def _选择网盘值(self, 框: QComboBox) -> str:
        return str(框.currentData() or "")

    def 添加跨网盘传输(self, 源标识: str, 源路径: str,
                     目标标识: str, 目标路径: str):
        idx = self.源网盘框.findData(源标识)
        if idx >= 0:
            self.源网盘框.setCurrentIndex(idx)
        self.源路径框.setText(规范路径(源路径))
        idx = self.目标网盘框.findData(目标标识)
        if idx >= 0:
            self.目标网盘框.setCurrentIndex(idx)
        self.目标路径框.setText(规范路径(目标路径))

    def _开始传输(self):
        if self._批次线程 is not None and self._批次线程.isRunning():
            QMessageBox.information(self, "提示", "上一批次还在跑，先停止")
            return
        源标识 = self._选择网盘值(self.源网盘框)
        目标标识 = self._选择网盘值(self.目标网盘框)
        if not 源标识 or not 目标标识:
            QMessageBox.warning(self, "提示", "请先在左下角「新增网盘」添加网盘")
            return
        if 源标识 == 目标标识 and QMessageBox.question(
                self, "确认", "源和目标为同一个网盘实例，确定继续吗？"
        ) != QMessageBox.Yes:
            return
        默认 = 传输参数(self.配置)
        缓存 = 缓存参数(self.配置)
        缓存.保留失败文件 = self.保留中转框.isChecked()
        请求 = 传输请求(
            源网盘=源标识,
            源路径=self.源路径框.text() or "/",
            目标网盘=目标标识,
            目标路径=self.目标路径框.text() or "/",
            覆盖=self.覆盖框.isChecked(),
            并发=self.并发框.value(),
            重试次数=self.重试框.value(),
            小文件阈值=int(默认.get("小文件阈值") or 8 * 1024 * 1024),
            小文件并发=int(默认.get("小文件并发") or 16),
            大文件并发=int(默认.get("大文件并发") or 3),
            断点续传=bool(默认.get("断点续传", True)),
            失败兜底=bool(self.兜底框.isChecked()),
            兜底串行=int(默认.get("兜底串行") or 1),
            缓存=缓存,
        )
        try:
            引擎 = self.动作.构建引擎([源标识, 目标标识])
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "初始化失败", str(e))
            return
        self._引擎 = 引擎
        self._当前请求 = 请求
        self._挂AI(引擎, 用AI=self.用AI框.isChecked())
        self._当前批次ID = 请求.批次ID
        self.重试按钮.setEnabled(False)
        self._取消 = threading.Event()
        if not self._计时起点:
            self._计时起点 = time.time()
        self._传输结束时间 = 0.0
        线程 = 传输线程(引擎, 请求, self._取消, self)
        线程.事件.connect(self._批次事件)
        线程.完成.connect(self._批次完成)
        # 统计对象回传（单任务"重跑"要复用它，才能把结果记进同一份统计）
        线程.统计就绪.connect(self._接收统计对象)
        self._批次线程 = 线程
        self._任务线程.append(线程)
        self.开始按钮.setEnabled(False)
        self.停止按钮.setEnabled(True)
        self._设置状态("传输中…", "#f39c12")
        if self.用AI框.isChecked() and self.AI调度器 is not None:
            self._设置决策("等待 AI 策略…", "正在扫描源目录并请求策略")
        else:
            self._设置决策("规则基线", "未启用 AI 调度（按配置并发/重试）")
        self.主窗口.追加日志(
            f"开始传输 {self.动作.名称(源标识)}:{请求.源路径} → "
            f"{self.动作.名称(目标标识)}:{请求.目标路径}")
        线程.start()

    def _接收统计对象(self, 统计) -> None:
        self._统计对象 = 统计

    def _挂AI(self, 引擎, 用AI: bool = True):
        """把 AI 调度器挂到引擎上（没有 AI 时引擎自动走非 AI 路径）。

        调度器是惰性创建的：用户还没打开过 AI 页时，这里现取一次
        （``AI运行时`` 构造阶段不联网，代价很小）。
        """
        if 用AI and self.AI调度器 is None:
            try:
                运行时 = self.主窗口.AI运行时()
                if 运行时 is not None:
                    self.AI调度器 = 运行时.调度器
            except Exception:
                self.AI调度器 = None
        try:
            引擎.AI调度器 = self.AI调度器 if 用AI else None
        except Exception:
            pass

    def _停止(self):
        self._取消.set()
        self._本地取消.set()
        self._设置状态("正在停止（等待当前文件结束）…", "#e74c3c")

    def _批次事件(self, 数据: dict):
        类型 = 数据.get("type")
        任务 = 数据.get("task") or {}
        if 任务:
            self._记任务(任务)
            self._刷新任务表()
        if 类型 in ("任务完成", "任务失败", "任务跳过", "任务取消"):
            路径 = 任务.get("source_path") or ""
            if 类型 == "任务完成":
                self.主窗口.追加日志(f"✅ {路径}")
            elif 类型 == "任务跳过":
                self.主窗口.追加日志(f"⏭ {路径}：{任务.get('skip_reason')}")
            else:
                self.主窗口.追加日志(f"❌ {路径}：{任务.get('error') or 类型}")
        elif 类型 == "批次完成" and 数据.get("stats"):
            self._设置状态(数据.get("message") or "完成", "#2ecc71")
        elif 类型 == "AI策略":
            决策 = 数据.get("data") or {}
            来源 = str(决策.get("来源") or "规则")
            标签 = {"规则": "规则基线", "AI": "AI 决策",
                  "缓存": "AI 缓存命中", "相似复用": "AI 相似复用",
                  "规则基线": "规则基线"}.get(来源, f"规则/{来源}")
            if 决策.get("置信度"):
                标签 += f"（置信度 {float(决策['置信度']):.2f}）"
            self._设置决策(标签, self._决策摘要(决策, 数据.get("message", "")),
                       "#9b59b6", "#1abc9c")
            self._设置状态(数据.get("message") or "AI 已应用策略", "#9b59b6")
        elif 类型 == "AI调优":
            决策 = 数据.get("data") or {}
            self._设置决策("AI 运行时调优", self._决策摘要(决策), "#8e44ad",
                       "#16a085")
            self._设置状态(数据.get("message") or "AI 已调整并发", "#9b59b6")
        self._刷新统计面板()

    def _批次完成(self, 统计: dict):
        self.开始按钮.setEnabled(True)
        self.停止按钮.setEnabled(False)
        if "error" in 统计:
            self._设置状态(f"失败：{统计['error']}", "#e74c3c")
            self.主窗口.追加日志(f"传输异常：{统计['error']}")
        else:
            文本 = (f"完成 {统计.get('已完成', 0)}，跳过 {统计.get('跳过', 0)}，"
                  f"失败 {统计.get('失败', 0)}，"
                  f"平均 {统计.get('平均速度', 0):.2f} MiB/s")
            self._设置状态(文本, "#2ecc71")
            self.重试按钮.setEnabled(
                bool(统计.get("失败")) and bool(self._当前批次ID))
        self._批次线程 = None
        self._刷新统计面板()

    def _选择并恢复(self):
        from ..核心.任务数据库 import 任务数据库
        try:
            db = 任务数据库(数据库路径(self.配置))
            try:
                批次 = db.未完成批次(50)
            finally:
                db.关闭()
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "读取批次失败", str(e))
            return
        if not 批次:
            QMessageBox.information(self, "恢复批次", "没有未完成批次")
            return
        标签 = [
            f"{b['batch_id']}  {self.动作.名称(b['source_cloud'])}:"
            f"{b['source_path']} → {self.动作.名称(b['target_cloud'])}:"
            f"{b['target_path']}"
            for b in 批次
        ]
        选择, ok = QInputDialog.getItem(
            self, "恢复批次", "选择要恢复的批次：", 标签, 0, False)
        if not ok or not 选择:
            return
        self._开始恢复(str(批次[标签.index(选择)]["batch_id"]))

    def _重试失败(self):
        if self._当前批次ID:
            self._开始恢复(self._当前批次ID)

    def _开始恢复(self, 批次ID: str):
        if self._批次线程 is not None and self._批次线程.isRunning():
            QMessageBox.information(self, "提示", "还有批次在跑，先停止")
            return
        from ..核心.任务数据库 import 任务数据库
        try:
            db = 任务数据库(数据库路径(self.配置))
            try:
                记录 = db.读取批次(批次ID)
            finally:
                db.关闭()
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "读取批次失败", str(e))
            return
        if 记录 is None:
            QMessageBox.warning(self, "恢复失败", f"批次不存在：{批次ID}")
            return
        配置 = 记录.get("config") or {}
        源标识 = str(配置.get("source_cloud") or "")
        目标标识 = str(配置.get("target_cloud") or "")
        try:
            引擎 = self.动作.构建引擎([源标识, 目标标识])
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "初始化失败", str(e))
            return
        self._挂AI(引擎, 用AI=self.用AI框.isChecked())
        self._当前批次ID = 批次ID
        self.重试按钮.setEnabled(False)
        self._取消 = threading.Event()
        if not self._计时起点:
            self._计时起点 = time.time()
        self._传输结束时间 = 0.0
        线程 = 恢复线程(引擎, 批次ID, self._取消, self)
        线程.事件.connect(self._批次事件)
        线程.完成.connect(self._批次完成)
        self._批次线程 = 线程
        self._任务线程.append(线程)
        self.开始按钮.setEnabled(False)
        self.停止按钮.setEnabled(True)
        self._设置状态(f"恢复批次 {批次ID}…", "#f39c12")
        self._设置决策("恢复批次", f"{批次ID}（沿用批次里的配置）")
        self.主窗口.追加日志(f"恢复批次 {批次ID}")
        线程.start()

    # ==================== 上传 / 下载 ====================

    def 添加上传任务(self, 标识: str, 本地路径: str, 远端目录: str,
                    覆盖: bool = False):
        """本地文件 → 远端目录；本地目录 → 远端目录/目录名（递归）。"""
        本地 = Path(本地路径)
        远端目录 = 规范路径(远端目录)
        if not 本地.exists():
            QMessageBox.warning(self, "上传失败", f"本地路径不存在：{本地}")
            return
        任务们: list[dict] = []
        if 本地.is_file():
            任务们.append(self._上传任务(标识, 本地, 远端目录, 覆盖))
        else:
            顶层 = 拼路径(远端目录, 本地.name)
            文件们 = sorted(p for p in 本地.rglob("*") if p.is_file())
            for 文件 in 文件们:
                相对 = 文件.parent.relative_to(本地).as_posix()
                目录 = 顶层 if 相对 in ("", ".") else f"{顶层}/{相对}"
                任务们.append(self._上传任务(标识, 文件, 目录, 覆盖))
        if not 任务们:
            QMessageBox.information(self, "提示", "目录里没有可上传的文件")
            return
        self._启动本地任务(任务们, f"上传 {len(任务们)} 个文件")

    def _上传任务(self, 标识: str, 本地: Path, 远端目录: str,
                 覆盖: bool = False) -> dict:
        self._本地序号 += 1
        try:
            大小 = 本地.stat().st_size
        except OSError:
            大小 = 0
        return {
            "task_id": f"local_{int(time.time())}_{self._本地序号:05d}",
            "kind": "upload",
            "标识": 标识,
            "本地": str(本地),
            "远端": 远端目录,
            "名称": 本地.name,
            "大小": 大小,
            "覆盖": bool(覆盖),
            "显示": f"⬆ {self.动作.名称(标识)}:{远端目录}/{本地.name}",
        }

    def 添加下载任务(self, 标识: str, 远端路径: str, 本地路径: str,
                    大小: int = 0):
        self._本地序号 += 1
        远端 = 规范路径(远端路径)
        任务 = {
            "task_id": f"local_{int(time.time())}_{self._本地序号:05d}",
            "kind": "download",
            "标识": 标识,
            "本地": str(Path(本地路径)),
            "远端": 远端,
            "名称": Path(本地路径).name,
            "大小": int(大小 or 0),
            "显示": f"⬇ {self.动作.名称(标识)}:{远端} → {本地路径}",
        }
        self._启动本地任务([任务], f"下载 {Path(本地路径).name}")

    def _启动本地任务(self, 任务们: list[dict], 描述: str):
        if not 任务们:
            return
        if self._本地线程 is not None and self._本地线程.isRunning():
            # 已经有一批在跑：排队接着传，而不是弹窗打断用户
            self._本地待办.extend(任务们)
            self._设置状态(f"{描述}（已排队 {len(self._本地待办)} 个，"
                       f"等当前这批结束）…", "#f39c12")
            self.主窗口.追加日志(
                f"已有上传/下载在跑，{len(任务们)} 个任务排队等待")
            return
        self._本地取消 = threading.Event()
        if not self._计时起点:
            self._计时起点 = time.time()
        self._传输结束时间 = 0.0
        线程 = 本地任务工作线程(
            self.动作.适配器, 任务们, self._本地取消, 并发=4,
            取守卫=self.动作.守卫, 父=self)
        线程.事件.connect(self._本地事件)
        线程.完成.connect(self._本地完成)
        self._本地线程 = 线程
        self._任务线程.append(线程)
        self._设置状态(f"{描述}…", "#f39c12")
        self._设置决策("本地任务", "上传/下载按实例并发 4 传输（不走批次策略）")
        self.停止按钮.setEnabled(True)
        线程.start()

    def _本地事件(self, 数据: dict):
        类型 = 数据.get("type")
        任务 = 数据.get("task") or {}
        if 任务:
            self._记任务(任务)
            self._刷新任务表()
        if 类型 == "任务完成":
            self.主窗口.追加日志(f"✅ {任务.get('source_path')}")
        elif 类型 == "任务失败":
            self.主窗口.追加日志(
                f"❌ {任务.get('source_path')}：{任务.get('error')}")
        self._刷新统计面板()

    def _本地完成(self, 统计: dict):
        self._本地线程 = None
        if "error" not in 统计:
            for 键 in ("成功", "失败", "取消"):
                self._本地累计[键] += int(统计.get(键, 0) or 0)
        if self._本地待办:
            下一批 = self._本地待办
            self._本地待办 = []
            self._启动本地任务(下一批, f"上传/下载排队任务 {len(下一批)} 个")
            return
        if "error" in 统计:
            self._设置状态(f"失败：{统计['error']}", "#e74c3c")
            return
        累计 = dict(self._本地累计)
        self._设置状态(
            f"本地上传/下载结束：成功 {累计['成功']}，失败 {累计['失败']}，"
            f"取消 {累计['取消']}"
            + ("（已请求停止）" if 统计.get("请求停止") else ""),
            "#2ecc71")
        if self._批次线程 is None:
            self.停止按钮.setEnabled(False)
        self._刷新统计面板()

    # ==================== 关闭 ====================

    def 关闭(self):
        self._取消.set()
        self._本地取消.set()
        try:
            self.刷新定时器.stop()
        except Exception:
            pass
        for 线程 in list(self._任务线程):
            try:
                if 线程.isRunning():
                    线程.wait(4000)
            except Exception:
                pass
        self._任务线程.clear()
