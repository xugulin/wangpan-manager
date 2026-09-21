"""AI 状态页面（切换页）。

移植 网盘管理_V8 / UI层/AI页面.py 的界面与信息（预算、时段、模型、价格表、
调度器统计），改成跟 V8_3 的 :class:`~v8_3.AI.运行时.AI运行时` 打交道：

* 顶部：刷新价格；
* 横幅：当前模型在"当前时段"的价格与便宜/中等/昂贵评价；
* 🔑 密钥：页面上直接粘贴 / 保存 / 测试 / 清除 DeepSeek 云端密钥；
* 🏠 本地模型：ollama 本地模型的开关、模型选择、检测/测速/启动/拉取；
* 三列：预算与消耗 / AI 状态（时段、模型下拉、模式切换）/ 价格表 + 运行详情；
* 底部：刷新余额、打开配置文件。

修复/优化（相对 V8）：
1. V8 页面直接读 ``AI助手.ai配置['model']`` 等内部字段，这里统一走运行时门面，
   任一部件缺失都不会让页面报错；
2. 加了"手动开/关/自动"三态按钮的说明与当前时段摘要（含折扣窗口提示）；
3. 加了一个"用 AI 调整并发"总开关，直接写回配置并保存——传输页勾选项与它联动；
4. 没有密钥/未启用时给出明确提示，而不是显示一堆 0；
5. 加了一张「🔑 DeepSeek API 密钥」卡片：页面上直接粘贴/保存/测试/清除，
   不用再手改 ``配置.json``（密钥只写本项目配置，界面与日志都只显示遮盖形态）。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QUrl

from .后台线程 import 任务线程, 线程池管理器

#: 常用本地模型（检测不到 ollama 时也先列出来，保证下拉框有东西可选）
推荐本地模型 = ["deepseek-r1:1.5b", "deepseek-r1:7b", "qwen2.5:7b",
            "qwen2.5:3b", "llama3.2:3b"]
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (
    QInputDialog,
    QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout,
    QGroupBox,
    QFileDialog, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox,
    QPushButton,
    QScrollArea,
    QTableWidget, QTableWidgetItem, QTextEdit, QVBoxLayout, QWidget,
)


class 不压缩内容(QWidget):
    """滚动区里的页面内容：**最小高度等于布局的建议高度**。

    QScrollArea 会把内容撑到 ``max(视口, 内容最小高度)``。默认的最小高度是布局的
    ``minimumSizeHint``（各控件的最小值之和，比设计高度小一截），那样在矮窗口里
    控件仍会被压到"最小"而不是它们该有的高度。这里直接把最小高度接到 ``sizeHint``，
    于是控件永远保持设计高度，装不下只出滚动条。
    """

    def minimumSizeHint(self):  # noqa: N802 - Qt 的命名
        return self.sizeHint()


class AI状态页面(QWidget):
    便宜阈值 = 1.0
    贵阈值 = 10.0
    图标_空闲 = "🌿"
    图标_高峰 = "🔥"

    def __init__(self, 主窗口, 父=None):
        super().__init__(父)
        self.主窗口 = 主窗口
        self._价格过期阈值 = 24 * 3600
        self._登记线程 = 线程池管理器(self.主窗口)
        self._本地模型忙 = False          # 检测/测速/启动/拉取进行中：按钮先禁用
        # 逐段计时：AI 页在个别机器上会莫名卡很久（真机 CI 上量到过 60 秒），
        # 只记"慢"的分段，正常启动不刷屏 —— 出问题时日志里能直接看到卡在哪一段。
        慢段: list[str] = []

        def 量(名字: str, 动作):
            t0 = time.time()
            try:
                return 动作()
            finally:
                耗毫秒 = (time.time() - t0) * 1000
                if 耗毫秒 >= 300:
                    慢段.append(f"{名字} {耗毫秒:.0f}ms")

        量("构建", self._构建)
        量("刷新", self.刷新)
        # 🛒 模型市场：先读缓存（秒开），没有缓存才联网重建 —— 绝不阻塞切页
        量("市场目录", lambda: self._刷新市场目录(强制=False))
        # 本地模型检测是阻塞操作（要戳本机端口），**不能在构建/刷新里同步做**，
        # 否则切到 AI 页就会卡住（用户反馈过）。这里改成后台跑，结果回来再填界面。
        量("本地模型后台检测", self.延迟检测本地模型)
        if 慢段:
            self.主窗口.追加日志("[AI页] 构建耗时：" + "、".join(慢段))

    # ==================== 便捷访问 ====================

    @property
    def 运行时(self):
        return self.主窗口.AI运行时()

    def _时段图标(self, 时段: str) -> str:
        return self.图标_空闲 if 时段 == "空闲" else self.图标_高峰

    def _抓取器(self):
        运行时 = self.运行时
        return getattr(运行时, "价格抓取器", None) if 运行时 else None

    # ==================== 界面 ====================

    def _构建(self):
        # 整页放进滚动区：AI 页控件多，按设计高度排下来要 ~1040px；窗口一矮，
        # 原来的布局会把控件一路压到 680px 以内 —— 横幅只剩一行、详情框几乎看不见，
        # 几个分组框还会挤到一起看着像"重叠"。现在控件保持各自高度，装不下就滚动。
        外层 = QVBoxLayout(self)
        外层.setContentsMargins(0, 0, 0, 0)
        外层.setSpacing(0)
        self.页面滚动区 = QScrollArea()
        self.页面滚动区.setObjectName("PageScroll")
        self.页面滚动区.setWidgetResizable(True)
        self.页面滚动区.setFrameShape(QScrollArea.NoFrame)
        self.页面滚动区.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)   # 窗口变窄时允许左右滑动
        # ⚠️ 外层**不做纵向滚动**：三个页签各自有滚动区，外层再滚就成了"两层滚动条"
        #    （实测：滚动到底看到的是内层被裁的位置，底部按钮永远露不出来）。
        self.页面滚动区.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.页面滚动区.viewport().setAutoFillBackground(False)
        self.页面内容 = 不压缩内容()
        self.页面内容.setObjectName("PageScroll")
        外层.addWidget(self.页面滚动区)

        布局 = QVBoxLayout(self.页面内容)
        布局.setContentsMargins(0, 0, 0, 0)
        布局.setSpacing(10)

        顶部 = QHBoxLayout()
        标题 = QLabel("🤖 AI 智能助手")
        标题.setStyleSheet("font-size: 18px; font-weight: bold;")
        顶部.addWidget(标题)

        # ---- 页面切换（用户要求：AI 页拆成「AI状态 / AI设置 / 模型商店」三页）----
        顶部.addSpacing(12)
        self.页签按钮: dict[str, QPushButton] = {}
        for 键, 名 in (("状态", "📊 AI状态"),
                     ("设置", "⚙ AI设置"),
                     ("商店", "🛒 模型商店")):
            钮 = QPushButton(名)
            钮.setObjectName("AITab")
            钮.setCheckable(True)
            钮.setToolTip({
                "状态": "预算与消耗、AI 状态、价格表",
                "设置": "云端密钥、本地模型、运行详情与预算刷新",
                "商店": "本地小模型市场：推荐指数排行 + 一键装/卸/升级",
            }[键])
            钮.clicked.connect(lambda _=False, k=键: self.切换AI页签(k))
            顶部.addWidget(钮)
            self.页签按钮[键] = 钮
        顶部.addStretch(1)
        # 顶部原来还有一个"价格来源"小标签（`💰 📦 builtin | 内置兜底 | 3 个模型`），
        # 按用户要求去掉了：这类内部信息（抓取来源、抓取时间、模型数）摆在页面顶部
        # 既占地方又只有维护者看得懂。价格本身仍然照常显示在下面的价格表里。
        self.刷新价格按钮 = QPushButton("🔄 刷新价格")
        self.刷新价格按钮.clicked.connect(self._手动刷新价格)
        顶部.addWidget(self.刷新价格按钮)
        打开配置 = QPushButton("⚙ 打开配置文件")
        打开配置.clicked.connect(self._打开配置)
        顶部.addWidget(打开配置)
        导入密钥 = QPushButton("📥 从配置文件导入密钥")
        导入密钥.setToolTip(
            "选一个 JSON 配置文件（例如本项目自己的 配置.json，或你自己导出的备份），"
            "把里面的 deepseek_api_key 读进来写进 V8_3 配置。\n"
            "V8_3 是完全独立的项目，不会去读别的项目目录。")
        导入密钥.clicked.connect(self._导入密钥)
        顶部.addWidget(导入密钥)
        布局.addLayout(顶部)

        self.提示横幅 = QLabel("💡 正在读取 AI 状态…")
        self.提示横幅.setWordWrap(True)
        self.提示横幅.setAlignment(Qt.AlignCenter)
        self.提示横幅.setStyleSheet(
            "font-size: 13px; font-weight: bold; padding: 10px 14px;"
            "border-radius: 6px; background: #e3f2fd; color: #0d47a1;"
            "border: 1px solid #90caf9;")
        布局.addWidget(self.提示横幅)

        # 三个页签各自的容器（同一时刻只显示一个）
        from PySide6.QtWidgets import QStackedWidget as _QStackedWidget
        self.AI页签堆叠 = _QStackedWidget()

        def _包滚动(页: QWidget):
            """给一个页签套滚动区：内容保持设计高度，装不下就自己滚。"""
            滚动 = QScrollArea()
            滚动.setObjectName("PageScroll")
            滚动.setWidgetResizable(True)
            滚动.setFrameShape(QScrollArea.NoFrame)
            滚动.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
            滚动.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
            滚动.viewport().setAutoFillBackground(False)
            滚动.setWidget(页)
            return 滚动

        # ---------------- 页①：AI 状态（预算与消耗 / AI 状态 / 价格表）----------------
        状态页 = QWidget()
        状态页布局 = QVBoxLayout(状态页)
        状态页布局.setContentsMargins(0, 0, 0, 0)
        状态页布局.setSpacing(10)
        三列 = QHBoxLayout()
        三列.setSpacing(12)
        三列.addWidget(self._建预算区())
        三列.addWidget(self._建状态区())
        右侧 = QVBoxLayout()
        右侧.addWidget(self._建价格区())
        右侧.addWidget(self._建详情区(), 1)
        右容器 = QWidget()
        右容器.setLayout(右侧)
        三列.addWidget(右容器, 1)
        状态页布局.addLayout(三列, 1)
        底部 = QHBoxLayout()
        # 文案缩短：原来"🔄 刷新余额并记账"在状态页底部会被裁掉（自检量到超出视口）
        self.刷新余额按钮 = QPushButton("🔄 刷新余额")
        self.刷新余额按钮.setToolTip("拉一次余额并记进账本（账本在 数据/ 下）")
        self.刷新余额按钮.setObjectName("PrimaryButton")
        self.刷新余额按钮.clicked.connect(self._手动刷新余额)
        底部.addWidget(self.刷新余额按钮)
        self.调度摘要标签 = QLabel("")
        self.调度摘要标签.setWordWrap(True)
        底部.addWidget(self.调度摘要标签, 1)
        状态页布局.addLayout(底部)
        self.状态滚动区 = _包滚动(状态页)
        self.AI页签堆叠.addWidget(self.状态滚动区)

        # ---------------- 页②：AI 设置（云端密钥 / 本地模型 / 运行详情）----------------
        设置页 = QWidget()
        设置页布局 = QVBoxLayout(设置页)
        设置页布局.setContentsMargins(0, 0, 0, 0)
        设置页布局.setSpacing(10)
        # ---- 云端密钥（在页面上直接填，不用再手改 配置.json）----
        设置页布局.addWidget(self._建密钥区())
        # ---- 本地 DeepSeek 模型（免费、离线）----
        设置页布局.addWidget(self._建本地模型区())
        # 用户要求：设置页**不放**"AI 运行详情"（它已经在「AI状态」页右侧）；
        # 末尾留一点弹性，两张卡片不会被顶到最上面显得空。
        设置页布局.addStretch(1)
        self.设置滚动区 = _包滚动(设置页)
        self.AI页签堆叠.addWidget(self.设置滚动区)

        # ---------------- 页③：模型商店（本地小模型市场）----------------
        商店页 = QWidget()
        商店页布局 = QVBoxLayout(商店页)
        商店页布局.setContentsMargins(0, 0, 0, 0)
        商店页布局.setSpacing(10)
        商店页布局.addWidget(self._建模型市场区(), 1)
        self.商店滚动区 = _包滚动(商店页)
        self.AI页签堆叠.addWidget(self.商店滚动区)

        布局.addWidget(self.AI页签堆叠, 1)
        # ⚠️ AI 页自己已经有一层滚动区，而页签内容普遍比窗口高（状态页三列 + 详情），
        #    只靠外层会让页签容器被压扁、底部控件被裁掉。所以每个页签**各自再套
        #    一层滚动区**：页签行为像"独立页面"，内容永远保持设计高度。
        self.页面滚动区.setWidget(self.页面内容)
        # 默认停在「AI 状态」
        self.当前AI页签 = ""
        self.切换AI页签("状态")

    # ==================== 页签切换 ====================

    @property
    def 当前页签滚动区(self):
        """当前页签自己的滚动区（脚本/自检用来滚到某处验证）。"""
        try:
            return self.AI页签堆叠.currentWidget()
        except Exception:
            return None

    def 切换AI页签(self, 键: str) -> None:
        """在「AI状态 / AI设置 / 模型商店」之间切换。"""
        顺序 = {"状态": 0, "设置": 1, "商店": 2}
        if 键 not in 顺序:
            return
        self.当前AI页签 = 键
        try:
            self.AI页签堆叠.setCurrentIndex(顺序[键])
        except Exception:
            return
        for 其他键, 钮 in getattr(self, "页签按钮", {}).items():
            try:
                钮.setChecked(其他键 == 键)
            except Exception:
                pass
        # 切到状态页时顺手把"当前多少钱"重算一次（价格可能刚刷新过）
        if 键 == "状态":
            try:
                self.刷新()
            except Exception:
                pass
        # 切到商店页时确保目录已加载（首次进来自动读缓存/联网）
        if 键 == "商店":
            try:
                if not getattr(self, "_市场条目", None):
                    self._刷新市场目录(强制=False)
            except Exception:
                pass

    def _建预算区(self) -> QWidget:
        组 = QGroupBox("💰 预算与消耗")
        组.setMaximumWidth(300)          # 让右侧价格表/详情拿到更多宽度
        表单 = QFormLayout(组)
        self.预算余额标签 = QLabel("¥0.00")
        self.预算余额标签.setStyleSheet("font-size: 14px; font-weight: bold;")
        表单.addRow("当前余额：", self.预算余额标签)
        self.阈值输入框 = QDoubleSpinBox()
        self.阈值输入框.setRange(0, 999999)
        self.阈值输入框.setDecimals(2)
        self.阈值输入框.setValue(2.0)
        self.阈值输入框.valueChanged.connect(self._阈值变化)
        表单.addRow("熔断阈值：", self.阈值输入框)
        self.预算摘要标签 = QLabel("—")
        self.预算摘要标签.setWordWrap(True)
        表单.addRow("预算摘要：", self.预算摘要标签)
        self.时段摘要标签 = QLabel("—")
        self.时段摘要标签.setWordWrap(True)
        表单.addRow("时段摘要：", self.时段摘要标签)
        return 组

    def _建状态区(self) -> QWidget:
        组 = QGroupBox("🎮 AI 状态")
        组.setMaximumWidth(320)
        布局 = QVBoxLayout(组)
        self.时段标签 = QLabel("…")
        self.时段标签.setAlignment(Qt.AlignCenter)
        布局.addWidget(self.时段标签)

        模型行 = QHBoxLayout()
        模型行.addWidget(QLabel("🧠 模型："))
        self.模型下拉框 = QComboBox()
        # 可输入 + 自动补全：有的厂家（如百炼）一次返回两百多个模型，必须能打字筛
        self.模型下拉框.setEditable(True)
        self.模型下拉框.setInsertPolicy(QComboBox.NoInsert)
        self.模型下拉框.currentIndexChanged.connect(self._切换模型)
        模型行.addWidget(self.模型下拉框, 1)
        self.刷新模型按钮 = QPushButton("🔄")
        self.刷新模型按钮.setFixedWidth(36)
        self.刷新模型按钮.clicked.connect(self._刷新模型列表)
        模型行.addWidget(self.刷新模型按钮)
        布局.addLayout(模型行)

        self.状态提示标签 = QLabel("💡 AI 根据时段自动启用/关闭")
        self.状态提示标签.setWordWrap(True)
        self.状态提示标签.setAlignment(Qt.AlignCenter)
        self.状态提示标签.setStyleSheet("font-size: 11px; color: #95a5a6;")
        布局.addWidget(self.状态提示标签)

        self.AI开关按钮 = QPushButton("🔁 切换模式（自动 → 强制开 → 强制关）")
        self.AI开关按钮.setObjectName("PrimaryButton")
        self.AI开关按钮.clicked.connect(self._切换AI模式)
        布局.addWidget(self.AI开关按钮)

        self.用AI框 = QPushButton("🧠 传输时用 AI 调整并发：开")
        self.用AI框.setCheckable(True)
        self.用AI框.setChecked(True)
        self.用AI框.clicked.connect(self._切换用AI)
        布局.addWidget(self.用AI框)
        布局.addStretch(1)
        return 组

    # ==================== 🛒 本地小模型市场 ====================
    #
    # 用户要求（2026-09-19）：
    #   * 列出"可查询到的、可下载的、适用于本项目的免费本地小模型"；
    #   * 显示模型图片、详细信息（可多行）、每个模型一句最核心的总结；
    #   * 每个模型后面有「一键安装 / 卸载 / 更新、下载链接、官网」；
    #   * 模型可**动态拉取**；
    #   * 针对本项目给每个模型打**推荐指数**，最推荐的排最前，
    #     第一/第二/第三名用不同 emoji 醒目标记。
    #
    # 数据来自 v8_3.AI.模型市场（策展目录 + ollama 官方接口动态核对），
    # 安装/卸载/更新走 v8_3.AI.本地模型.本地模型客户端。

    #: 一次最多渲染多少张卡片（渲染 40+ 张卡片会明显拖慢切页；其余用「显示全部」）
    市场首屏条数 = 12

    def _建模型市场区(self) -> QWidget:
        组 = QGroupBox("🛒 本地小模型市场（推荐指数按本项目任务打分 · 免费可下载）")
        外层 = QVBoxLayout(组)

        self.市场状态标签 = QLabel("🛒 正在准备模型目录…")
        self.市场状态标签.setWordWrap(True)
        self.市场状态标签.setStyleSheet(
            "font-size: 12px; padding: 6px 10px; border-radius: 4px;"
            "background: #1b3a4b; color: #eaf6ff;")
        外层.addWidget(self.市场状态标签)

        工具行 = QHBoxLayout()
        self.市场搜索框 = QLineEdit()
        self.市场搜索框.setPlaceholderText("🔎 过滤模型（名字 / 厂商 / 场景 / 总结里的词）")
        self.市场搜索框.textChanged.connect(lambda _t: self._重绘市场卡片())
        工具行.addWidget(self.市场搜索框, 1)

        self.市场刷新按钮 = QPushButton("🔄 刷新目录")
        self.市场刷新按钮.setToolTip(
            "从 Ollama 官方接口动态拉取：核对每个模型是否真的可下载、算精确体积。\n"
            "约 10~20 秒；结果会缓存 24 小时。")
        self.市场刷新按钮.clicked.connect(lambda: self._刷新市场目录(强制=True))
        工具行.addWidget(self.市场刷新按钮)

        self.运行时按钮 = QPushButton("⬆️ 更新内置ollama")
        # 详细提示（含当前内置版本）在 _刷新内置ollama提示() 里动态写，
        # 这里只给一句兜底，免得市场还没加载完时按钮是"哑"的。
        self.运行时按钮.setToolTip("包里已内置官方便携版 ollama 基座（CPU/Vulkan 后端）；点这里更新到官方最新版")
        self.运行时按钮.clicked.connect(self._更新内置ollama)
        工具行.addWidget(self.运行时按钮)

        self.市场全部按钮 = QPushButton("📜 显示全部")
        self.市场全部按钮.clicked.connect(self._切换显示全部)
        工具行.addWidget(self.市场全部按钮)

        权重按钮 = QPushButton("⚖️ 推荐权重")
        权重按钮.setToolTip("调整推荐指数的五个分项权重（任务适配/中文/推理/轻量/新鲜度）")
        权重按钮.clicked.connect(self._调推荐权重)
        工具行.addWidget(权重按钮)
        self.市场按钮们 = [self.市场刷新按钮, self.运行时按钮,
                       self.市场全部按钮, 权重按钮]
        工具行.addStretch(1)
        外层.addLayout(工具行)

        self.市场滚动 = QScrollArea()
        self.市场滚动.setWidgetResizable(True)
        # 市场自己有滚动条，而 AI 页本身也是滚动页 —— 嵌套滚动最怕"无限长"：
        # 卡片一多就会把 AI 页下半部分顶得没边。这里给市场区一个固定高度区间
        # （视口内滚动），AI 页上下都能正常翻。
        self.市场滚动.setMinimumHeight(420)
        self.市场滚动.setMaximumHeight(560)
        self.市场滚动.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.市场容器 = QWidget()
        self.市场布局 = QVBoxLayout(self.市场容器)
        self.市场布局.setSpacing(8)
        self.市场布局.addStretch(1)
        self.市场滚动.setWidget(self.市场容器)
        外层.addWidget(self.市场滚动, 1)

        self.市场明细标签 = QLabel(
            "推荐指数 = 任务适配 × 权重 + 中文能力 + 推理能力 + 轻量 + 新鲜度"
            "（分项都取自目录里明写的元数据，缺项按中性算）。\n"
            "🥇🥈🥉 是前三名；「已核对」表示刚刚向官方库确认过可以下载、体积是实测值。")
        self.市场明细标签.setWordWrap(True)
        self.市场明细标签.setStyleSheet("font-size: 11px; color: #95a5a6;")
        外层.addWidget(self.市场明细标签)

        self._市场条目: list = []
        self._市场显示全部 = False
        self._市场卡片们: list = []
        self._市场装表: dict = {}
        self._市场忙 = False
        return 组

    # ---------------- 目录加载 ----------------

    def _市场模块(self):
        try:
            from ..AI import 模型市场
            return 模型市场
        except Exception:
            return None

    def 本地客户端(self):
        """拿一个本地模型客户端（装/卸/升级、列已装模型都靠它）。

        优先复用 AI 助手里那个实例（配置一致）；助手没起来时自己造一个。
        """
        try:
            助手 = getattr(self.运行时, "助手", None)
            客户端 = getattr(助手, "本地模型", None)
            if 客户端 is not None:
                return 客户端
        except Exception:
            pass
        try:
            from ..AI.本地模型 import 本地模型客户端, 取本地模型配置
            AI配置 = (self.主窗口.配置.get("AI") or {})
            return 本地模型客户端(取本地模型配置(AI配置))
        except Exception:
            return None

    def _刷新市场目录(self, 强制: bool = False) -> None:
        """读缓存；缓存过期或强制刷新时联网重建（后台线程）。"""
        市场 = self._市场模块()
        if 市场 is None:
            self.市场状态标签.setText("❌ 模型市场模块不可用")
            return
        if not 强制 and 市场.联网被禁用():
            # 自检/离线模式：只用策展目录，绝不联网
            self._填市场(市场.构建目录(联网=False), 注明="（离线策展目录）")
            return
        if not 强制:
            缓存 = 市场.读取缓存()
            if 缓存:
                时间 = 市场.缓存时间()
                self._填市场(缓存)
                self.市场状态标签.setText(
                    f"📦 已加载缓存的目录：{len(缓存)} 个模型"
                    f"（缓存于 {time.strftime('%m-%d %H:%M', time.localtime(时间))}；"
                    "点「🔄 刷新目录」联网核对最新体积）")
                return
        if self._市场忙:
            return
        self._市场忙 = True
        for 钮 in self.市场按钮们:
            钮.setEnabled(False)
        self.市场状态标签.setText("🔄 正在从 Ollama 官方接口拉取并核对模型…")

        def 干(进度回调=None):
            权重 = self._读推荐权重(市场)
            return 市场.构建目录(联网=True, 权重=权重,
                            进度回调=lambda t: self._市场进度(t))

        线程 = 任务线程(干, 父=self)
        线程.成功.connect(self._市场刷新完成)
        线程.失败.connect(self._市场刷新失败)
        线程.finished.connect(self._市场收工)
        self._登记线程(线程)
        线程.start()

    def _市场进度(self, 文本: str) -> None:
        # 进度回调来自后台线程：只写文本，Qt 侧由 _市场收工 统一刷新
        try:
            self._市场进度文本 = str(文本)
            self.市场状态标签.setText(f"🔄 {文本}")
        except Exception:
            pass

    def _市场收工(self) -> None:
        self._市场忙 = False
        for 钮 in self.市场按钮们:
            钮.setEnabled(True)

    def _市场刷新失败(self, 错误: str) -> None:
        self.市场状态标签.setText(f"❌ 刷新目录失败：{错误}（仍可用缓存/策展目录）")
        市场 = self._市场模块()
        if 市场 is not None:
            self._填市场(市场.构建目录(联网=False), 注明="（离线策展目录）")

    def _市场刷新完成(self, 条目们) -> None:
        市场 = self._市场模块()
        条目们 = list(条目们 or [])
        if 市场 is not None and 条目们:
            市场.写入缓存(条目们)
        离线数 = sum(1 for x in 条目们 if getattr(x, "核对状态", "") == "已核对")
        self.市场状态标签.setText(
            f"✅ 目录已更新：共 {len(条目们)} 个模型，其中 {离线数} 个已向官方库"
            f"核到体积（{time.strftime('%H:%M:%S')}）")
        self._填市场(条目们)

    # ---------------- 渲染 ----------------

    def _读推荐权重(self, 市场):
        try:
            段 = ((self.主窗口.配置.get("AI") or {}).get("本地模型") or {})
            权重段 = 段.get("推荐权重") or {}
            if 权重段:
                return 市场.推荐权重.from_dict(权重段)
        except Exception:
            pass
        return 市场.默认权重

    def _填市场(self, 条目们, 注明: str = "") -> None:
        市场 = self._市场模块()
        if 市场 is None:
            return
        self._市场条目 = list(条目们 or [])
        # 本机已装状态（ollama list）——决定「安装/卸载/更新」三个按钮谁可用
        try:
            客户端 = self.本地客户端()
            已装 = 客户端.已装模型() if 客户端 is not None else {}
        except Exception:
            已装 = {}
        self._市场已装 = dict(已装 or {})
        市场.合并已装状态(self._市场条目, self._市场已装)
        self._重绘市场卡片()
        self._刷新内置ollama提示()
        if 注明:
            self.市场状态标签.setText(
                f"📦 {len(self._市场条目)} 个模型 {注明}")

    def _刷新内置ollama提示(self) -> None:
        """把「内置 ollama 基座」的现状写进那个按钮的提示里。

        故意不加新控件：AI 页已经很满，而用户看这个按钮时才知道要更新，
        版本号放提示里最合适（也顺带把"路径是项目相对的"这件事显出来）。
        """
        头顶 = ("包里**已经内置**官方便携版 ollama 基座（只有运行时，不含任何模型权重）：\n"
              "开箱即用，不需要自己装、不需要管理员权限，装模型之前也不用先联网。\n"
              "只带 **CPU / Vulkan** 推理后端（CUDA 那两份合计 2 GB，带上包就翻倍，所以裁掉了）。\n")
        尾巴 = ("\n这个按钮把它更新到官方最新版：下载 → 自检能跑 → 才替换，\n"
              "全程都在项目内 运行环境/本地模型；中途失败或新版跑不起来会保留现在这份，\n"
              "不会把能用的基座弄坏。\n"
              "想连 CUDA 后端一起要（N 卡加速）：设环境变量 "
              "V8_3_本地模型_带GPU后端=1 再点这个按钮。")
        try:
            from ..AI.本地模型 import 内置运行时说明, 内置运行时就位
            说明 = 内置运行时说明()
            缺 = not 内置运行时就位()
        except Exception as e:  # noqa: BLE001
            说明, 缺 = f"状态未知：{e}", False
        现状 = f"\n当前：{说明}"
        if 缺:
            现状 += "\n⚠️ 基座缺失/被删了：点这个按钮补一份官方基座。"
        self.运行时按钮.setToolTip(头顶 + 现状 + 尾巴)

    def _重绘市场卡片(self) -> None:
        市场 = self._市场模块()
        if 市场 is None:
            return
        关键词 = ""
        try:
            关键词 = self.市场搜索框.text().strip().lower()
        except Exception:
            pass
        条目们 = list(self._市场条目)
        if 关键词:
            条目们 = [x for x in 条目们
                    if 关键词 in " ".join([
                        x.名字, x.中文名, x.厂商, x.总结, x.简介,
                        " ".join(x.适用场景)]).lower()]
        显示上限 = len(条目们) if self._市场显示全部 else self.市场首屏条数
        显示 = 条目们[:显示上限]

        # 清空旧卡片
        while self.市场布局.count():
            项 = self.市场布局.takeAt(0)
            控件 = 项.widget()
            if 控件 is not None:
                控件.setParent(None)
                try:
                    控件.deleteLater()
                except Exception:
                    pass
        self._市场卡片们 = []
        for 序, 条目 in enumerate(显示, 1):
            卡片 = self._建模型卡片(序, 条目)
            self.市场布局.addWidget(卡片)
            self._市场卡片们.append(卡片)
        剩余 = len(条目们) - len(显示)
        if 剩余 > 0:
            更多 = QLabel(f"…… 还有 {剩余} 个模型没显示"
                       f"（点上方「📜 显示全部」看完整 {len(条目们)} 个）")
            更多.setStyleSheet("color: #95a5a6; font-size: 11px;")
            self.市场布局.addWidget(更多)
        self.市场布局.addStretch(1)
        try:
            self.市场全部按钮.setText(
                "📜 只显示前 %d" % self.市场首屏条数 if self._市场显示全部
                else "📜 显示全部")
        except Exception:
            pass

    def _建模型卡片(self, 序: int, 条目) -> QWidget:
        市场 = self._市场模块()
        卡 = QWidget()
        卡.setStyleSheet(
            "QWidget#模型卡 { border: 1px solid #3a4a5a; border-radius: 8px;"
            "background: #22303c; }")
        卡.setObjectName("模型卡")
        外 = QVBoxLayout(卡)
        外.setContentsMargins(10, 8, 10, 8)
        外.setSpacing(4)

        头 = QHBoxLayout()
        头.setSpacing(10)

        # 图片（本地生成，不抓网图）——见 v8_3/界面/图标.py
        图标签 = QLabel()
        图标签.setFixedSize(56, 56)
        try:
            from .图标 import 模型图
            角标 = f"{条目.参数B:g}B" if 条目.参数B else ""
            像素 = 模型图(条目.图片键, 条目.名字, 尺寸=96, 角标=角标)
            if 像素 is not None and not 像素.isNull():
                图标签.setPixmap(像素.scaled(56, 56, Qt.KeepAspectRatio,
                                        Qt.SmoothTransformation))
        except Exception:
            pass
        头.addWidget(图标签, 0, Qt.AlignTop)

        中 = QVBoxLayout()
        中.setSpacing(2)
        名次 = 市场.名次标记(序)
        标题 = QLabel(
            f"{名次 + '　' if 名次 else ''}"
            f"<b style='font-size:14px'>{条目.名字}</b>"
            f"　<span style='color:#f1c40f'>推荐指数 {条目.分数:.1f}</span>"
            f"　{市场.推荐等级(条目.分数)}")
        标题.setTextFormat(Qt.RichText)
        中.addWidget(标题)

        副 = QLabel(f"{条目.中文名 or ''}　·　{条目.厂商 or ''}　·　{条目.体积文本}")
        副.setStyleSheet("color: #9fb3c8; font-size: 11px;")
        中.addWidget(副)

        总结 = QLabel(f"📌 {条目.总结}")
        总结.setWordWrap(True)
        总结.setStyleSheet("font-size: 12px; font-weight: bold; color: #ecf0f1;")
        中.addWidget(总结)

        理由 = QLabel(f"💡 推荐理由：{条目.推荐理由}")
        理由.setWordWrap(True)
        理由.setStyleSheet("font-size: 11px; color: #8fd18f;")
        中.addWidget(理由)

        状态 = []
        if getattr(条目, "已安装", False):
            状态.append("✅ 本机已安装")
        if getattr(条目, "可更新", False):
            状态.append("⬆️ 官方有新版本")
        if 条目.核对状态 == "不存在":
            状态.append("❌ 官方库没有这个标签")
        状态行 = QLabel("　".join(状态) or "⚪ 本机未安装")
        状态行.setStyleSheet("font-size: 11px; color: #f39c12;")
        中.addWidget(状态行)
        头.addLayout(中, 1)
        外.addLayout(头)

        # ---- 操作按钮行：一键安装 / 卸载 / 更新 / 下载链接 / 官网 ----
        行 = QHBoxLayout()
        行.setSpacing(6)
        已装 = bool(getattr(条目, "已安装", False))

        装 = QPushButton("⬇️ 一键安装" if not 已装 else "✅ 已安装")
        装.setEnabled(not 已装)
        装.clicked.connect(lambda _=False, m=条目: self._装模型(m))
        行.addWidget(装)

        卸 = QPushButton("🗑 卸载")
        卸.setEnabled(已装)
        卸.clicked.connect(lambda _=False, m=条目: self._卸模型(m))
        行.addWidget(卸)

        更 = QPushButton("🔄 更新")
        更.setEnabled(已装)
        更.setToolTip("重新 pull 一次：ollama 只补差量层，很快")
        更.clicked.connect(lambda _=False, m=条目: self._更新模型(m))
        行.addWidget(更)

        下载 = QPushButton("📥 下载链接")
        下载.clicked.connect(
            lambda _=False, m=条目: self._打开链接(m.下载页, "下载页面"))
        行.addWidget(下载)

        官网 = QPushButton("🌐 官网")
        官网.clicked.connect(
            lambda _=False, m=条目: self._打开链接(m.官方页, "官方页面"))
        行.addWidget(官网)

        详情钮 = QPushButton("📖 详细信息")
        详情钮.setCheckable(True)
        行.addWidget(详情钮)
        行.addStretch(1)
        外.addLayout(行)

        # ---- 多行详细信息（默认折叠）----
        详情 = QLabel()
        详情.setTextFormat(Qt.RichText)
        详情.setWordWrap(True)
        详情.setText(self._详情HTML(条目))
        详情.setStyleSheet(
            "font-size: 11px; color: #cfd8dc; background: #1a2630;"
            "border-radius: 6px; padding: 8px;")
        详情.setVisible(False)
        外.addWidget(详情)

        def 切(开: bool):
            详情.setVisible(bool(开))
            详情钮.setText("📖 收起详细" if 开 else "📖 详细信息")

        详情钮.toggled.connect(切)
        return 卡

    @staticmethod
    def _详情HTML(条目) -> str:
        """把详细信息排成**多行**表格：每行「标签：值」，更清楚明了。"""
        行们 = []
        for 标签, 值 in 条目.详情行():
            行们.append(
                f"<tr><td style='color:#90a4ae; padding:2px 8px 2px 0;"
                f"white-space:nowrap'>{标签}</td>"
                f"<td style='padding:2px 0'>{值}</td></tr>")
        表 = "<table cellspacing='0' cellpadding='0'>" + "".join(行们) + "</table>"
        块 = [表]
        if 条目.简介:
            块.append("<div style='margin-top:6px'><b>详细介绍</b><br>"
                    + str(条目.简介).replace("\n", "<br>") + "</div>")
        if 条目.优点:
            块.append("<div style='margin-top:6px'><b>优点</b><br>· "
                    + "<br>· ".join(条目.优点) + "</div>")
        if 条目.注意:
            块.append("<div style='margin-top:6px'><b>注意事项</b><br>· "
                    + "<br>· ".join(条目.注意) + "</div>")
        return "".join(块)

    # ---------------- 操作 ----------------

    def _打开链接(self, 地址: str, 名称: str = "页面") -> None:
        if not 地址:
            QMessageBox.information(self, "提示", f"这个模型没有填{名称}")
            return
        try:
            from PySide6.QtGui import QDesktopServices
            from PySide6.QtCore import QUrl
            QDesktopServices.openUrl(QUrl(str(地址)))
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "打开失败", f"{地址}\n{e}")

    def _切换显示全部(self) -> None:
        self._市场显示全部 = not self._市场显示全部
        self._重绘市场卡片()

    def _调推荐权重(self) -> None:
        市场 = self._市场模块()
        if 市场 is None:
            return
        现在 = self._读推荐权重(市场)
        当前 = (f"任务适配={现在.任务适配:.2f}　中文能力={现在.中文能力:.2f}　"
              f"推理能力={现在.推理能力:.2f}　轻量={现在.轻量:.2f}　"
              f"新鲜度={现在.新鲜度:.2f}")
        文本, 好 = QInputDialog.getText(
            self, "推荐权重",
            "按「任务适配, 中文能力, 推理能力, 轻量, 新鲜度」顺序填五个数"
            "（会自动归一化）：\n当前：" + 当前,
            text=f"{现在.任务适配:.2f}, {现在.中文能力:.2f}, "
                 f"{现在.推理能力:.2f}, {现在.轻量:.2f}, {现在.新鲜度:.2f}")
        if not 好 or not str(文本).strip():
            return
        try:
            数 = [float(x) for x in str(文本).replace("，", ",").split(",")]
            if len(数) != 5:
                raise ValueError("需要 5 个数")
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "格式不对", f"请填 5 个数字：{e}")
            return
        权重 = 市场.推荐权重(*数).归一()
        try:
            配置 = self.主窗口.配置
            段 = 配置.setdefault("AI", {}).setdefault("本地模型", {})
            段["推荐权重"] = 权重.to_dict()
            self.主窗口.保存配置()
        except Exception:
            pass
        # 用新权重重新打分排序（离线即可，不用联网）
        条目们 = 市场.排序并打分(list(self._市场条目), 权重)
        self._填市场(条目们, 注明="（已按新权重重排）")

    def _更新内置ollama(self) -> None:
        """把**内置**的便携版 ollama 基座更新到官方最新（不装系统、不需要 sudo）。"""
        from ..AI.本地模型 import (下载便携运行时, 内置运行时版本,
                                  相对项目路径, 项目内可执行文件)
        可执行 = 项目内可执行文件()
        旧版本 = 内置运行时版本()
        现状 = f"v{旧版本}" if 旧版本 else ("未就位" if not 可执行.is_file() else "版本未知")
        确认 = QMessageBox.question(
            self, "更新内置 ollama",
            f"当前内置：{现状}\n位置：{相对项目路径(可执行)}（项目内，随包发布，不含模型权重）\n\n"
            "现在从官方下载最新的便携版并替换？\n"
            "（压缩包约 1.4 GB；下载完先自检能跑、再裁掉 GPU 后端，\n"
            "最终只占约 100 MB。更新期间现在这份仍可用，失败了也不会被弄坏。）")
        if 确认 != QMessageBox.Yes:
            return
        状态 = {"文本": "开始下载…"}

        def 进度(已下, 总):
            if 总:
                状态["文本"] = f"下载中 {已下 / 1048576:.0f} / {总 / 1048576:.0f} MB"

        def 干():
            return 下载便携运行时(进度回调=进度, 强制=True)

        线程 = 任务线程(干, 父=self)
        线程.成功.connect(self._运行时完成)
        线程.失败.connect(lambda e: self.市场状态标签.setText(f"❌ 更新内置 ollama 失败：{e}"))
        self._登记线程(线程)
        self.市场状态标签.setText("⬆️ 正在更新内置 ollama（下载官方基座，约 1.4 GB）…")
        计时 = QTimer(self)
        计时.setInterval(800)

        def 滴答():
            self.市场状态标签.setText(
                f"⬆️ {状态['文本']}（下载完会自检，再替换 运行环境/本地模型）")

        计时.timeout.connect(滴答)
        计时.start()
        self._运行时计时 = 计时
        线程.finished.connect(计时.stop)
        线程.start()

    def _运行时完成(self, 结果) -> None:
        好, 消息 = 结果 if isinstance(结果, (tuple, list)) else (False, str(结果))
        self.市场状态标签.setText(("✅ " if 好 else "❌ ") + str(消息))
        self._刷新内置ollama提示()
        if 好:
            self.刷新本地模型(重新检测=True)

    def _装模型(self, 条目) -> None:
        self._跑模型操作(条目, "安装", lambda 客户端, 回调, 取消:
                    客户端.拉取模型_带进度(条目.名字, 进度回调=回调,
                                    取消事件=取消))

    def _更新模型(self, 条目) -> None:
        self._跑模型操作(条目, "更新", lambda 客户端, 回调, 取消:
                    客户端.升级模型(条目.名字, 进度回调=回调))

    def _卸模型(self, 条目) -> None:
        if QMessageBox.question(
                self, "卸载模型",
                f"确定卸载 {条目.名字} 吗？\n会删除本机下载的模型文件"
                f"（{条目.体积文本}），以后想用可以再装。") != QMessageBox.Yes:
            return
        self._跑模型操作(条目, "卸载", lambda 客户端, 回调, 取消:
                    客户端.删除模型(条目.名字))

    def _跑模型操作(self, 条目, 动作名: str, 工作) -> None:
        """把「装/卸/升级」丢后台跑，并把进度打进状态栏。"""
        客户端 = self.本地客户端()
        if 客户端 is None:
            self.市场状态标签.setText("❌ 本地模型运行时不可用")
            return
        if self._市场装表.get(条目.名字):
            return
        self._市场装表[条目.名字] = True
        进度行: list[str] = []
        取消 = None
        if 动作名 == "安装":
            import threading
            取消 = threading.Event()

        def 回调(文本: str) -> None:
            进度行.append(str(文本))

        def 干():
            return 工作(客户端, 回调, 取消)

        计时 = QTimer(self)
        计时.setInterval(700)

        def 滴答():
            尾巴 = 进度行[-1] if 进度行 else "准备中…"
            self.市场状态标签.setText(
                f"⏳ {动作名} {条目.名字}：{尾巴}")

        计时.timeout.connect(滴答)
        计时.start()
        self.市场状态标签.setText(f"⏳ 正在{动作名} {条目.名字}…")

        线程 = 任务线程(干, 父=self)

        def 完成(结果):
            好, 消息 = 结果 if isinstance(结果, (tuple, list)) else (False, str(结果))
            计时.stop()
            self._市场装表.pop(条目.名字, None)
            self.市场状态标签.setText(
                ("✅ " if 好 else "❌ ") + f"{动作名} {条目.名字}：{消息}")
            # 模型商店动了本机模型 → 设置页的下拉框/测速/启动服务要跟着变
            self._已装模型缓存 = None
            self.刷新本地模型(重新检测=True)
            try:
                self._刷新本地模型下拉()
            except Exception:
                pass
            self._刷新市场目录(强制=False)   # 刷新"本机已装"状态
            if not 好 and "没找到 ollama" in str(消息):
                self.市场状态标签.setText(
                    "❌ 内置 ollama 运行时不可用：点上面的「⬆️ 更新内置ollama」补一份官方基座")

        def 失败(错误):
            计时.stop()
            self._市场装表.pop(条目.名字, None)
            self.市场状态标签.setText(f"❌ {动作名} {条目.名字} 失败：{错误}")

        线程.成功.connect(完成)
        线程.失败.connect(失败)
        线程.finished.connect(计时.stop)
        self._登记线程(线程)
        线程.start()

    # ==================== 本地模型（V8_3 新增） ====================

    def _建密钥区(self) -> QWidget:
        """🔑 云端 DeepSeek 密钥卡片：页面上直接填 / 直接测，不用手改 JSON。

        密钥只写进本项目自己的 ``配置.json``（``AI.api密钥``）；
        界面与日志都只显示遮盖后的形态，不打印明文。
        """
        组 = QGroupBox("🔑 在线模型（多家厂家，各自一把密钥）")
        外层 = QVBoxLayout(组)

        # ---- 厂家：每一家有自己的接口地址 + 密钥 + 模型，可随时切换 ----
        厂家行 = QHBoxLayout()
        厂家行.addWidget(QLabel("厂家："))
        self.厂家下拉 = QComboBox()
        self.厂家下拉.setMinimumWidth(220)
        self.厂家下拉.setToolTip(
            "点这里切换厂家。每家各存一份接口地址与密钥，互不干扰。\n"
            "模型名通过该厂家的接口用你的密钥实时拉取（不是内置写死的名单）。")
        self.厂家下拉.currentIndexChanged.connect(self._切换厂家)
        厂家行.addWidget(self.厂家下拉, 1)
        self.新增厂家按钮 = QPushButton("➕ 添加厂家")
        self.新增厂家按钮.setToolTip("从内置预设里挑一家（百炼/Kimi/智谱/火山/硅基流动…）或自定义")
        self.新增厂家按钮.clicked.connect(self._新增厂家)
        厂家行.addWidget(self.新增厂家按钮)
        self.删除厂家按钮 = QPushButton("🗑 删除")
        self.删除厂家按钮.clicked.connect(self._删除厂家)
        厂家行.addWidget(self.删除厂家按钮)
        外层.addLayout(厂家行)

        地址行 = QHBoxLayout()
        地址行.addWidget(QLabel("接口地址："))
        self.接口地址框 = QLineEdit()
        self.接口地址框.setPlaceholderText(
            "例如 https://dashscope.aliyuncs.com/compatible-mode/v1")
        self.接口地址框.setToolTip(
            "直接填该厂家的接口地址（OpenAI 兼容网关的 /v1 地址）即可 ——\n"
            "本项目不绑定任何品牌，只按这个地址发请求。")
        self.接口地址框.returnPressed.connect(self._保存厂家)
        地址行.addWidget(self.接口地址框, 1)
        外层.addLayout(地址行)

        self.密钥状态标签 = QLabel("🔑 正在读取密钥状态…")
        self.密钥状态标签.setWordWrap(True)
        self.密钥状态标签.setStyleSheet(
            "font-size: 12px; padding: 6px 10px; border-radius: 4px;"
            "background: #2c3e50; color: #ecf0f1;")
        外层.addWidget(self.密钥状态标签)

        行 = QHBoxLayout()
        行.addWidget(QLabel("密钥："))
        self.密钥输入框 = QLineEdit()
        self.密钥输入框.setEchoMode(QLineEdit.Password)
        self.密钥输入框.setPlaceholderText("粘贴该厂家的 API Key（只存本地 配置.json）")
        self.密钥输入框.setMinimumWidth(320)
        self.密钥输入框.setToolTip(
            "这是**当前厂家**的密钥。切换厂家时会各自记住，互不覆盖。\n"
            "保存写进本项目 配置.json（AI.在线模型），并立刻重建 AI 层。")
        self.密钥输入框.returnPressed.connect(self._保存密钥)
        行.addWidget(self.密钥输入框, 1)
        self.显示密钥框 = QCheckBox("👁 显示")
        self.显示密钥框.setToolTip("只在本地临时显示明文；密钥不会被写进日志")
        self.显示密钥框.toggled.connect(self._切换密钥可见)
        行.addWidget(self.显示密钥框)
        外层.addLayout(行)

        按钮行 = QHBoxLayout()
        self.保存密钥按钮 = QPushButton("💾 保存密钥")
        self.保存密钥按钮.setObjectName("PrimaryButton")
        self.保存密钥按钮.setToolTip(
            "写进 配置.json 的 AI.api密钥，并让 AI 层按新密钥重新初始化")
        self.保存密钥按钮.clicked.connect(self._保存密钥)
        按钮行.addWidget(self.保存密钥按钮)
        self.测试密钥按钮 = QPushButton("🧪 测试连接")
        self.测试密钥按钮.setToolTip(
            "拿输入框里的密钥请求一次 DeepSeek 的 /models 接口：\n"
            "能列出模型 = 密钥可用（不消耗 token，也不发聊天请求）")
        self.测试密钥按钮.clicked.connect(self._测试密钥)
        按钮行.addWidget(self.测试密钥按钮)
        self.清除密钥按钮 = QPushButton("🗑 清除")
        self.清除密钥按钮.setToolTip("清空本项目配置里的密钥（不影响别处，可随时重填）")
        self.清除密钥按钮.clicked.connect(self._清除密钥)
        按钮行.addWidget(self.清除密钥按钮)
        按钮行.addStretch(1)
        外层.addLayout(按钮行)

        self.密钥提示标签 = QLabel(
            "密钥只写进**本项目**的 配置.json，界面与日志都不打印明文；"
            "留空 = 只用本地模型 / 规则级调度。")
        self.密钥提示标签.setWordWrap(True)
        self.密钥提示标签.setStyleSheet("font-size: 11px; color: #95a5a6;")
        外层.addWidget(self.密钥提示标签)
        return 组

    # ==================== 云端密钥 ====================

    def _当前密钥(self) -> str:
        """当前**这个厂家**的密钥。

        多厂家之后密钥是按厂家分开存的（``AI.在线模型.厂家.<id>.密钥``），
        所以先读当前厂家；老配置里那份平铺的 ``AI.api密钥`` 只作为兜底。
        """
        try:
            厂家 = self._在线段()["厂家"].get(self._在线段()["当前"]) or {}
            值 = str(厂家.get("密钥") or "").strip()
            if 值:
                return 值
        except Exception:  # noqa: BLE001
            pass
        try:
            运行时 = self.运行时
            if 运行时 is not None:
                值 = str((运行时.AI配置 or {}).get("api密钥") or "").strip()
                if 值:
                    return 值
        except Exception:  # noqa: BLE001
            pass
        try:
            return str(((self.主窗口.配置 or {}).get("AI") or {})
                       .get("api密钥") or "").strip()
        except Exception:  # noqa: BLE001
            return ""

    @staticmethod
    def _遮盖密钥(密钥: str) -> str:
        """只露头尾，中间打码 —— 日志、状态条都用这个形态。"""
        密钥 = str(密钥 or "")
        if len(密钥) <= 10:
            return "*" * len(密钥)
        return f"{密钥[:6]}…{密钥[-4:]}"

    # ==================== 在线厂家（多家切换） ====================

    def _在线段(self) -> dict:
        from ..配置 import 在线模型段
        return 在线模型段(self.主窗口.配置)

    def _写回在线段(self, 段: dict) -> None:
        """写回配置并落盘 + 重建 AI 层。"""
        from ..配置 import 保存配置, 写回在线模型, 配置文件 as 默认配置路径
        写回在线模型(self.主窗口.配置, 段)
        路径 = getattr(self.主窗口, "配置路径", None) or 默认配置路径
        保存配置(self.主窗口.配置, 路径)
        try:
            self.主窗口._确保AI(强制=True)
        except TypeError:
            try:
                self.主窗口._确保AI()
            except Exception:  # noqa: BLE001
                pass
        except Exception:  # noqa: BLE001
            pass

    def _刷新厂家下拉(self) -> None:
        段 = self._在线段()
        self.厂家下拉.blockSignals(True)
        self.厂家下拉.clear()
        for 标识, 项 in 段["厂家"].items():
            标 = "🔑 " if str(项.get("密钥") or "").strip() else "⚪ "
            self.厂家下拉.addItem(f"{标}{项.get('名称') or 标识}", 标识)
        idx = self.厂家下拉.findData(段["当前"])
        self.厂家下拉.setCurrentIndex(max(0, idx))
        self.厂家下拉.blockSignals(False)
        self.删除厂家按钮.setEnabled(len(段["厂家"]) > 1)

    def _切换厂家(self, _索引=0) -> None:
        标识 = self.厂家下拉.currentData()
        if not 标识:
            return
        段 = self._在线段()
        if 标识 == 段["当前"]:
            self._载入厂家到界面()
            return
        # 先把当前界面上编辑到一半的内容存回原厂家，再切
        self._保存厂家(静默=True)
        段 = self._在线段()
        段["当前"] = 标识
        self._写回在线段(段)
        self._载入厂家到界面()
        self._记日志(f"[在线模型] 已切换到「{段['厂家'][标识].get('名称') or 标识}」")
        self.刷新()

    def _载入厂家到界面(self) -> None:
        from ..配置 import 当前厂家
        厂家 = 当前厂家(self.主窗口.配置)
        self.接口地址框.setText(str(厂家.get("接口地址") or ""))
        self.接口地址框.setModified(False)
        if not self.密钥输入框.isModified():
            self.密钥输入框.setText(str(厂家.get("密钥") or ""))
        self.密钥输入框.setModified(False)
        self._刷新密钥区()

    def _保存厂家(self, 静默: bool = False) -> None:
        段 = self._在线段()
        标识 = 段["当前"]
        厂家 = dict(段["厂家"].get(标识) or {})
        厂家["接口地址"] = self.接口地址框.text().strip().rstrip("/")
        厂家["密钥"] = self.密钥输入框.text().strip()
        模型 = self.模型下拉框.currentData() if hasattr(self, "模型下拉框") else ""
        if 模型:
            厂家["模型"] = str(模型)
        段["厂家"][标识] = 厂家
        self._写回在线段(段)
        self.接口地址框.setModified(False)
        self.密钥输入框.setModified(False)
        self._刷新厂家下拉()
        if not 静默:
            self._记日志(f"[在线模型] 已保存「{厂家.get('名称') or 标识}」的接口地址与密钥")
            self.刷新()

    def _新增厂家(self) -> None:
        from ..配置 import 在线厂家预设
        名单 = list(在线厂家预设.keys())
        选择, 好 = QInputDialog.getItem(
            self, "添加厂家", "选一家（自带的接口地址已填好，粘个密钥就能用）：",
            名单, 0, False)
        if not 好 or not 选择:
            return
        段 = self._在线段()
        标识 = 选择 if 选择 not in 段["厂家"] else f"{选择}_{len(段['厂家']) + 1}"
        from ..配置 import _规格化厂家
        段["厂家"][标识] = _规格化厂家(选择, {})
        段["当前"] = 标识
        self._写回在线段(段)
        self._刷新厂家下拉()
        self._载入厂家到界面()
        self._记日志(f"[在线模型] 已添加厂家「{选择}」，填好密钥后点「🔄 拉取模型」")

    def _删除厂家(self) -> None:
        段 = self._在线段()
        if len(段["厂家"]) <= 1:
            return
        标识 = 段["当前"]
        名称 = 段["厂家"][标识].get("名称") or 标识
        if QMessageBox.question(
                self, "删除厂家",
                f"删除厂家「{名称}」？\n它的接口地址与密钥会一起删掉。",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        段["厂家"].pop(标识, None)
        段["当前"] = next(iter(段["厂家"]))
        self._写回在线段(段)
        self._刷新厂家下拉()
        self._载入厂家到界面()
        self.刷新()

    def _拉取模型(self) -> None:
        """用当前厂家的密钥去它的接口拉一次模型清单（后台，不卡界面）。"""
        from ..配置 import 当前厂家
        厂家 = 当前厂家(self.主窗口.配置)
        地址 = str(厂家.get("接口地址") or "").strip()
        密钥 = self.密钥输入框.text().strip() or str(厂家.get("密钥") or "")
        if not 地址:
            QMessageBox.information(self, "提示", "先填这个厂家的接口地址。")
            return
        if not 密钥:
            QMessageBox.information(self, "提示", "先粘这个厂家的 API Key，再拉模型。")
            return
        if not 密钥.isascii():
            QMessageBox.warning(
                self, "密钥格式不对",
                "API Key 里混进了非 ASCII 字符（多半是从别处复制时带上了中文或空格）。\n"
                "请重新复制纯密钥再试。")
            return
        def 按钮(状态: str) -> None:
            钮 = getattr(self, "拉取模型按钮", None) or getattr(self, "刷新模型按钮", None)
            if 钮 is not None:
                钮.setEnabled(状态 == "开始")
                钮.setText("⏳" if 状态 == "开始" else "🔄")

        按钮("开始")

        def 干():
            import httpx
            基 = 地址.rstrip("/")
            应答 = httpx.get(f"{基}/models",
                          headers={"Authorization": f"Bearer {密钥}"},
                          timeout=20.0)
            应答.raise_for_status()
            数据 = 应答.json() or {}
            名单 = [str(m.get("id") or "") for m in (数据.get("data") or [])
                  if m.get("id")]
            if not 名单:
                raise RuntimeError("该接口没有返回任何模型（可能不支持 /models）")
            return 名单

        def 好了(名单):
            按钮("结束")
            段 = self._在线段()
            标识 = 段["当前"]
            厂家 = dict(段["厂家"].get(标识) or {})
            厂家["模型列表"] = list(名单)
            厂家["模型来源"] = "接口"
            段["厂家"][标识] = 厂家
            self._写回在线段(段)
            self._记日志(f"[在线模型] 拉到 {len(名单)} 个模型：{'、'.join(名单[:6])}"
                      + ("…" if len(名单) > 6 else ""))
            self.刷新()

        def 坏了(错误):
            按钮("结束")
            提示 = (f"拉取失败：{错误}\n\n"
                  "有些网关不提供 /models 接口（比如火山方舟要填接入点 ID）。\n"
                  "这种情况可以直接在下拉框里手输模型名，或点「➕ 添加到模型列表」。")
            QMessageBox.warning(self, "拉取模型失败", 提示)
            self._记日志(f"[在线模型] 拉取模型失败：{错误}")

        线程 = 任务线程(干, 父=self)
        线程.成功.connect(好了)
        线程.失败.connect(坏了)
        self._登记线程(线程)
        线程.start()

    def _刷新密钥区(self):
        # 厂家下拉与"接口地址"要跟着当前厂家走（每家一份密钥/地址）
        try:
            self._刷新厂家下拉()
            厂家 = self._在线段()["厂家"].get(self._在线段()["当前"], {})
            if not self.接口地址框.isModified():
                self.接口地址框.setText(str(厂家.get("接口地址") or ""))
            if not self.密钥输入框.isModified():
                self.密钥输入框.setText(str(厂家.get("密钥") or ""))
                self.密钥输入框.setModified(False)
        except Exception:  # noqa: BLE001
            pass
        密钥 = self._当前密钥()
        if 密钥:
            self.密钥状态标签.setText(
                f"✅ 已配置：{self._遮盖密钥(密钥)}（{len(密钥)} 字符）"
                "｜ 云端 AI 可用")
            self.密钥状态标签.setStyleSheet(
                "font-size: 12px; padding: 6px 10px; border-radius: 4px;"
                "background: #e8f5e9; color: #1b5e20; border: 1px solid #a5d6a7;")
        else:
            self.密钥状态标签.setText(
                "⚠ 未配置：AI 只做规则级调度（不影响传输）"
                "｜ 粘一个密钥进来，或改用下面的本地模型")
            self.密钥状态标签.setStyleSheet(
                "font-size: 12px; padding: 6px 10px; border-radius: 4px;"
                "background: #fff3e0; color: #e65100; border: 1px solid #ffcc80;")
        # 用户正在输入时不要覆盖他的内容（setText 会清掉 modified 标记）
        if not self.密钥输入框.isModified():
            self.密钥输入框.setText(密钥)

    def _切换密钥可见(self, 勾选: bool):
        self.密钥输入框.setEchoMode(
            QLineEdit.Normal if 勾选 else QLineEdit.Password)

    def _重建AI运行时(self):
        """密钥变了：把旧运行时丢掉，让 AI 助手按新密钥重新初始化。

        必须连 ``_外部AI运行时`` 一起清掉 —— 那是启动自检预先建好的运行时，
        只清 ``_AI运行时`` 的话 :meth:`主窗口._确保AI` 会把这个旧运行时又捞回来，
        新密钥就白填了。
        """
        self.主窗口._AI运行时 = None
        self.主窗口._外部AI运行时 = None
        self.主窗口._AI不可用 = ""
        self.刷新()

    def _保存密钥(self):
        from ..配置 import 保存配置          # 与 _导入密钥/_打开配置 一样按需导入
        密钥 = self.密钥输入框.text().strip()
        if 密钥 and not 密钥.startswith("sk-"):
            if QMessageBox.question(
                    self, "密钥格式不太像",
                    "DeepSeek 的密钥通常以 sk- 开头。\n"
                    "仍要按现在填的内容保存吗？（自建代理可以用别的格式）"
            ) != QMessageBox.Yes:
                return
        try:
            ai = dict(self.主窗口.配置.get("AI") or {})
            ai["api密钥"] = 密钥
            self.主窗口.配置["AI"] = ai
            保存配置(self.主窗口.配置, getattr(self.主窗口, "配置路径", None))
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "保存失败", str(e))
            return
        self.密钥输入框.setModified(False)
        self.主窗口.追加日志(
            f"DeepSeek 密钥已保存：{self._遮盖密钥(密钥)}（界面与日志不显示明文）"
            if 密钥 else
            "DeepSeek 密钥已清空（AI 只做规则级调度，不影响传输）")
        self.密钥提示标签.setText("✅ 已保存到 配置.json，AI 层已按新密钥重建。")
        self._重建AI运行时()

    def _测试密钥(self):
        """用输入框里的密钥请求一次 /models：不花 token，也能验真伪。"""
        密钥 = self.密钥输入框.text().strip()
        if not 密钥:
            QMessageBox.information(
                self, "提示", "先在输入框里填上 DeepSeek API 密钥，再点测试。")
            return
        地址 = ""
        try:
            运行时 = self.运行时
            if 运行时 is not None:
                地址 = str((运行时.AI配置 or {}).get("接口地址") or "")
        except Exception:  # noqa: BLE001
            pass
        if not 地址:
            try:
                地址 = str((self.主窗口.配置.get("AI") or {}).get("接口地址") or "")
            except Exception:  # noqa: BLE001
                地址 = ""
        地址 = (地址 or "https://api.deepseek.com").rstrip("/")

        self.测试密钥按钮.setEnabled(False)
        self.测试密钥按钮.setText("⏳ 测试中…")
        QApplication.processEvents()
        好 = False
        try:
            import httpx
            应答 = httpx.get(f"{地址}/models",
                            headers={"Authorization": f"Bearer {密钥}"},
                            timeout=15.0)
            if 应答.status_code == 200:
                模型 = [str(项.get("id")) for 项 in (应答.json().get("data") or [])]
                预览 = "、".join(模型[:3]) + ("…" if len(模型) > 3 else "")
                文本 = (f"✅ 密钥可用：{地址} 认得它，列出 {len(模型)} 个模型"
                       + (f"（{预览}）" if 模型 else ""))
                好 = True
            elif 应答.status_code in (401, 403):
                文本 = (f"❌ 密钥被拒绝（HTTP {应答.status_code}）："
                        "确认复制完整、没带多余空格，也没被删除/停用")
            else:
                文本 = (f"❌ 测试失败：HTTP {应答.status_code} "
                        f"{应答.text[:120]}")
        except Exception as e:  # noqa: BLE001
            文本 = f"❌ 测试失败：{e}"
        finally:
            self.测试密钥按钮.setEnabled(True)
            self.测试密钥按钮.setText("🧪 测试连接")
        self.密钥提示标签.setText(文本)
        self.密钥提示标签.setStyleSheet(
            f"font-size: 11px; color: {'#2e7d32' if 好 else '#c62828'};")
        self.主窗口.追加日志(f"DeepSeek 密钥测试：{文本}")   # 只记结论，不记密钥

    def _清除密钥(self):
        密钥 = self._当前密钥()
        if not 密钥:
            self.密钥输入框.clear()
            self.密钥输入框.setModified(False)
            self._刷新密钥区()
            return
        if QMessageBox.question(
                self, "确认清除",
                f"清空本项目配置里的 DeepSeek 密钥？\n当前：{self._遮盖密钥(密钥)}\n"
                "清空后 AI 只做规则级调度（不影响传输），随时可以再填回来。"
        ) != QMessageBox.Yes:
            return
        self.密钥输入框.clear()
        self._保存密钥()

    def _建本地模型区(self) -> QWidget:
        """本地 DeepSeek 模型卡片：开关、状态、模型选择、检测/测速/启动/拉取。"""
        组 = QGroupBox("🏠 本地 DeepSeek 模型（免费 · 离线 · 不上传数据）")
        外层 = QVBoxLayout(组)

        self.本地状态标签 = QLabel("🏠 正在检测本地模型…")
        self.本地状态标签.setWordWrap(True)
        self.本地状态标签.setStyleSheet(
            "font-size: 12px; padding: 6px 10px; border-radius: 4px;"
            "background: #2c3e50; color: #ecf0f1;")
        外层.addWidget(self.本地状态标签)

        行1 = QHBoxLayout()
        self.本地启用框 = QCheckBox("启用本地模型（优先本地，云端仅兜底）")
        self.本地启用框.toggled.connect(self._切换本地模型)
        行1.addWidget(self.本地启用框)
        self.本地优先框 = QCheckBox("优先本地")
        self.本地优先框.setToolTip(
            "勾选：所有 AI 请求先用本地模型；\n"
            "不勾：仍按云端为主（本地仅在云端不可用时兜底）")
        self.本地优先框.toggled.connect(self._保存本地模型选项)
        行1.addWidget(self.本地优先框)
        行1.addWidget(QLabel("用途："))
        self.本地用途框 = QComboBox()
        self.本地用途框.addItem("全部（本地跑所有决策）", "全部")
        self.本地用途框.addItem("仅轻量（策略仍走云端）", "仅轻量")
        self.本地用途框.setToolTip(
            "纯 CPU 上一次批次策略约 15 秒；选「仅轻量」可让\n"
            "优先级/诊断这类短请求走本地，重策略交云端（更快）")
        self.本地用途框.currentIndexChanged.connect(self._保存本地模型选项)
        行1.addWidget(self.本地用途框)
        行1.addStretch(1)
        外层.addLayout(行1)

        行2 = QHBoxLayout()
        行2.addWidget(QLabel("模型："))
        self.本地模型框 = QComboBox()
        self.本地模型框.setMinimumWidth(220)
        self.本地模型框.currentIndexChanged.connect(self._保存本地模型选项)
        行2.addWidget(self.本地模型框)
        self.本地按钮们: list = []
        检测按钮 = QPushButton("🔍 检测")
        检测按钮.clicked.connect(lambda: self.刷新本地模型(重新检测=True))
        行2.addWidget(检测按钮)
        self.测速按钮 = QPushButton("⏱ 测速")
        self.测速按钮.clicked.connect(self._本地模型测速)
        测速按钮 = self.测速按钮
        行2.addWidget(测速按钮)
        self.启动服务按钮 = QPushButton("▶ 启动服务")
        self.启动服务按钮.setToolTip("拉起 ollama serve（用户目录安装，不需要 root）")
        self.启动服务按钮.clicked.connect(self._启动本地服务)
        启动按钮 = self.启动服务按钮
        行2.addWidget(启动按钮)
        # 用户要求：去掉「📥 拉取模型 / ⬇️ 一键装好离线模型 / 📖 安装指引」三个按钮。
        # 装模型改到「🛒 模型商店」页（那里有完整的推荐排行与一键装/卸/更新）；
        # 拉取/一键装/指引这三个后端方法仍然保留（模型商店与命令行都在用）。
        self.本地按钮们.extend([检测按钮, 测速按钮, 启动按钮])
        行2.addStretch(1)
        外层.addLayout(行2)

        self.本地提示标签 = QLabel(
            "装模型请到「🛒 模型商店」页：按推荐指数排行，一键安装/卸载/更新。")
        self.本地提示标签.setWordWrap(True)
        self.本地提示标签.setStyleSheet("font-size: 11px; color: #95a5a6;")
        外层.addWidget(self.本地提示标签)
        return 组

    def _后台跑(self, 工作, 完成回调, *参数, 忙碌文本: str = "",
              失败前缀: str = "❌ ", **关键字) -> None:
        """把阻塞工作丢后台线程，主线程只更新界面。"""
        if 忙碌文本:
            self.本地状态标签.setText(忙碌文本)
        if hasattr(self, "本地按钮们"):
            for 钮 in self.本地按钮们:
                钮.setEnabled(False)
        线程 = 任务线程(工作, *参数, 父=self, **关键字)
        线程.成功.connect(lambda 结果: 完成回调(结果))
        线程.失败.connect(
            lambda 错误: self.本地状态标签.setText(f"{失败前缀}{错误}"))
        线程.finished.connect(self._本地模型收工)
        self._登记线程(线程)
        线程.start()

    #: "本机装了哪些本地模型"的缓存秒数：模型商店装/卸完会立刻清缓存，
    #: 平时刷新页面不必每次去起 ollama 子进程。
    已装模型缓存秒 = 5.0

    def _已装本地模型(self, 强制: bool = False) -> list[str]:
        """本机**已安装**的本地模型列表（来自 ollama list，带短缓存）。

        为什么不用摘要里的"模型列表"：那个在没装服务时会退回"推荐模型"，
        分不清"没装模型"和"服务没起来"。用户要求"没有本地模型时下拉框为空"
        —— 只有问实际安装情况才能给准。
        """
        现在 = time.time()
        缓存 = getattr(self, "_已装模型缓存", None)
        if (not 强制 and 缓存
                and 现在 - float(缓存[0]) < self.已装模型缓存秒):
            return list(缓存[1])
        try:
            客户端 = self.本地客户端()
            已装 = dict(客户端.已装模型() or {}) if 客户端 is not None else {}
        except Exception:
            已装 = {}
        列表 = sorted(已装) if 已装 else []
        self._已装模型缓存 = (现在, 列表)
        return 列表

    def _刷新本地模型下拉(self, 摘要: dict | None = None,
                    已装: list[str] | None = None) -> None:
        """按"本机装了哪些模型"调整下拉框与 测速/启动服务 的可用性。

        用户要求：
          * 没有本地模型 → 下拉框只显示占位提示、**测速/启动服务不可用**；
          * 有本地模型 → 下拉框列出来、按钮可用；
          * 模型商店里装/卸/更新完成后，这里也要跟着变。
        """
        已装 = list(已装 if 已装 is not None else self._已装本地模型())
        当前 = ""
        try:
            当前 = str((摘要 or {}).get("模型") or "").strip()
        except Exception:
            当前 = ""
        框 = getattr(self, "本地模型框", None)
        if 框 is None:
            return
        框.blockSignals(True)
        try:
            if not 已装:
                框.clear()
                # 空选项 + 温馨提示（用户要求）
                框.addItem("（还没有本地模型 · 请到「🛒 模型商店」安装）", "")
                框.setCurrentIndex(0)
                框.setEnabled(False)
                框.setToolTip("本机还没有可用的本地模型："
                            "切到「🛒 模型商店」按推荐指数排行挑一个，点「⬇️ 一键安装」")
            else:
                现有 = [框.itemText(i) for i in range(框.count())]
                if 现有 != 已装:
                    框.clear()
                    框.addItems(已装)
                目标 = 当前 if 当前 in 已装 else 已装[0]
                框.setCurrentIndex(max(0, 框.findText(目标)))
                框.setEnabled(True)
                框.setToolTip(f"本机已安装 {len(已装)} 个本地模型")
        finally:
            框.blockSignals(False)

        # 测速 / 启动服务：没有模型时不可用（启动服务仍要求"基座就位"，基座是内置的）
        usable = bool(已装)
        for 钮, 名字 in ((getattr(self, "测速按钮", None), "测速"),
                      (getattr(self, "启动服务按钮", None), "启动服务")):
            if 钮 is None:
                continue
            try:
                钮.setEnabled(usable)
                if not usable:
                    钮.setToolTip(f"本机还没有本地模型，无法{名字}；"
                                "请先到「🛒 模型商店」安装一个")
                else:
                    钮.setToolTip("")
            except Exception:
                pass
        if not usable and 摘要 is not None:
            self.本地提示标签.setText(
                "还没有本地模型：切到「🛒 模型商店」，按推荐指数挑一个"
                "（推荐 🥇 qwen3.5:4b），点「⬇️ 一键安装」即可；"
                "装好回来这里就能测速/启动服务。")
        return

    def _本地模型收工(self) -> None:
        self._本地模型忙 = False
        for 钮 in getattr(self, "本地按钮们", []):
            钮.setEnabled(True)
        # 收工后再按"有没有模型"校正一次：别把 测速/启动服务 又解开成可点
        try:
            self._刷新本地模型下拉()
        except Exception:
            pass

    def 延迟检测本地模型(self) -> None:
        """后台检测一次本地模型（不阻塞界面）。"""
        运行时 = self.运行时
        if 运行时 is None:
            return

        def 干():
            摘要 = 运行时.获取本地模型摘要() or {}
            运行时.获取本地模型状态(重新检测=True)
            return 运行时.获取本地模型摘要() or 摘要

        self._本地模型忙 = True
        self._后台跑(干, self._套用本地模型, 忙碌文本="🏠 正在检测本地模型…")

    def 刷新本地模型(self, 重新检测: bool = False) -> None:
        """刷新本地模型区。

        ``重新检测=True`` 时**后台**重新探测（以前是同步的，一点就卡住）；
        否则只读缓存，绝不碰网络/端口 —— 保证切页、刷新都不卡。
        """
        if 重新检测:
            self.延迟检测本地模型()
            return
        self._套用本地模型(None)

    def _套用本地模型(self, 摘要=None) -> None:
        运行时 = self.运行时
        if 运行时 is None:
            self.本地状态标签.setText("🏠 AI 运行时未就绪")
            return
        if 摘要 is None:
            # 只读缓存：绝不在这里探测端口（那是后台线程的活）
            try:
                摘要 = 运行时.获取本地模型摘要() or {}
            except Exception as e:  # noqa: BLE001
                self.本地状态标签.setText(f"🏠 本地模型刷新失败：{e}")
                return
        self.本地状态标签.setText(
            ("🏠 " + 运行时.获取本地模型一行())
            + (f"　｜　{t} tokens/s" if (t := 摘要.get("每秒tokens")) else ""))
        self.本地启用框.blockSignals(True)
        self.本地启用框.setChecked(bool(摘要.get("启用")))
        self.本地启用框.blockSignals(False)
        try:
            本地 = 运行时.助手.本地模型配置
            self.本地优先框.blockSignals(True)
            self.本地优先框.setChecked(bool(本地.优先本地))
            self.本地优先框.blockSignals(False)
            self.本地用途框.blockSignals(True)
            idx = self.本地用途框.findData(str(本地.用途 or "全部"))
            self.本地用途框.setCurrentIndex(max(0, idx))
            self.本地用途框.blockSignals(False)
        except Exception:
            pass
        # 下拉框只列**本机已安装**的模型；一个都没有就显示占位提示并禁用
        # （用户要求：没有本地模型时不要塞一堆"推荐模型"让人误以为已经装了）
        self._刷新本地模型下拉(摘要)
        if not 摘要.get("可用") and self._已装本地模型():
            self.本地提示标签.setText(
                str(摘要.get("说明") or "本地模型不可用").splitlines()[0])

    def _切换本地模型(self, 勾选: bool) -> None:
        运行时 = self.运行时
        if 运行时 is None:
            return
        运行时.保存本地模型配置(启用=bool(勾选))
        self.刷新本地模型(重新检测=True)
        self._记日志(f"{'✅ 已启用' if 勾选 else '⚪ 已关闭'}本地模型")

    def _保存本地模型选项(self, *_):
        运行时 = self.运行时
        if 运行时 is None:
            return
        模型 = self.本地模型框.currentText().strip()
        运行时.保存本地模型配置(
            优先本地=bool(self.本地优先框.isChecked()),
            用途=str(self.本地用途框.currentData() or "全部"),
            **({"模型": 模型} if 模型 else {}))
        self.刷新本地模型(重新检测=True)

    def _本地模型测速(self) -> None:
        """测速要加载模型（几十秒），必须后台跑，否则界面直接卡死。"""
        运行时 = self.运行时
        if 运行时 is None:
            return

        def 干():
            return 运行时.本地模型测速() or {}

        def 好了(结果):
            if not 结果.get("成功"):
                self.本地状态标签.setText(f"❌ 测速失败：{结果.get('错误')}")
                return
            self.本地状态标签.setText(
                f"✅ {结果.get('模型')} · 用时 {结果.get('用时秒')}s · "
                f"{结果.get('每秒tokens')} tokens/s · 免费")
            self._记日志(f"[本地模型] 测速：{结果.get('用时秒')}s / "
                      f"{结果.get('每秒tokens')} tokens/s")

        self._后台跑(干, 好了, 忙碌文本="⏱ 正在测速…（首次会加载模型，可能要十几秒）")

    def _启动本地服务(self) -> None:
        """启动 ollama 之类要拉起进程，可能要几十秒，必须后台跑。"""
        运行时 = self.运行时
        if 运行时 is None:
            return

        def 干():
            return 运行时.启动本地服务()

        def 好了(结果):
            try:
                成功, 消息 = 结果
            except Exception:  # noqa: BLE001
                成功, 消息 = False, str(结果)
            self.本地状态标签.setText(("✅ " if 成功 else "❌ ") + str(消息))
            if 成功:
                self.延迟检测本地模型()

        self._后台跑(干, 好了, 忙碌文本="▶ 正在启动本地服务…（可能要几十秒）")
        self.刷新本地模型(重新检测=True)

    def _拉取本地模型(self) -> None:
        if self.运行时 is None:
            return
        模型 = self.本地模型框.currentText().strip()
        if not 模型:
            QMessageBox.information(self, "提示", "先填一个模型名，例如 "
                                   "deepseek-r1:1.5b")
            return
        if QMessageBox.question(
                self, "确认拉取",
                f"将从 Ollama registry 拉取 {模型}（可能要下载 1GB 左右），继续？"
        ) != QMessageBox.Yes:
            return
        def 干():
            return self.运行时.拉取本地模型(模型)

        def 好了(结果):
            try:
                成功, 消息 = 结果
            except Exception:  # noqa: BLE001
                成功, 消息 = False, str(结果)
            self.本地状态标签.setText(("✅ " if 成功 else "❌ ") + str(消息))
            if 成功:
                self.延迟检测本地模型()

        self._后台跑(干, 好了,
                  忙碌文本=f"📥 正在拉取 {模型}…（可能要几分钟，日志页有进度）")

    def _一键装本地模型(self) -> None:
        """一键装好离线模型：检测 → 内置基座缺失就补 → 启动 → 拉模型 → 打开开关。

        整个过程（可能几十分钟的下载）都在后台线程里，界面全程可响应，
        进度实时写在状态标签上。
        """
        运行时 = self.运行时
        if 运行时 is None:
            return
        if QMessageBox.question(
                self, "一键装好离线模型",
                "将自动完成：\n"
                "  ① 检测本机推理服务\n"
                "  ② 用包里**内置**的 ollama 基座（缺失/损坏才补一份官方基座）\n"
                "  ③ 启动服务 ④ 拉取一个小模型（约 1 GB）⑤ 打开本地模型开关\n\n"
                "基座已经内置，所以只下载模型权重（约 1 GB，网络不好时会比较慢）。现在开始？"
        ) != QMessageBox.Yes:
            return

        def 进展(文本: str) -> None:
            # 这个回调在后台线程里，只能设置文本（QLabel 的 setText 是线程安全的）
            self.本地状态标签.setText(f"⚙️ {文本}")

        def 干():
            return 运行时.一键装本地模型(进度回调=进展)

        def 好了(结果):
            try:
                成功, 消息 = 结果
            except Exception:  # noqa: BLE001
                成功, 消息 = False, str(结果)
            self.本地状态标签.setText(("✅ " if 成功 else "❌ ") + str(消息))
            self._记日志(f"[本地模型] 一键安装：{消息}")
            self._套用本地模型(None)

        self._后台跑(干, 好了, 忙碌文本="⚙️ 正在准备离线模型…")

    def _显示本地模型指引(self) -> None:
        from ..AI.运行时 import AI运行时
        QMessageBox.information(self, "本地模型安装指引",
                              AI运行时.本地模型安装指引())

    def _记日志(self, 文本: str) -> None:
        try:
            self.主窗口.追加日志(文本)
        except Exception:
            pass

    def _建价格区(self) -> QWidget:
        组 = QGroupBox("💵 价格表（元/百万 tokens）")
        布局 = QVBoxLayout(组)
        self.价格表 = QTableWidget()
        self.价格表.setColumnCount(6)
        self.价格表.setHorizontalHeaderLabels(
            ["模型", "时段", "缓存命中", "缓存未命中", "输出", "标记"])
        self.价格表.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        for 列, 宽 in ((1, 84), (2, 92), (3, 100), (4, 88), (5, 82)):
            self.价格表.setColumnWidth(列, 宽)
        self.价格表.setAlternatingRowColors(True)
        self.价格表.setEditTriggers(QTableWidget.NoEditTriggers)
        self.价格表.setFixedHeight(190)
        布局.addWidget(self.价格表)
        self.价格状态标签 = QLabel("")
        self.价格状态标签.setWordWrap(True)
        self.价格状态标签.setStyleSheet("font-size: 11px; color: #95a5a6;")
        布局.addWidget(self.价格状态标签)
        return 组

    def _建详情区(self) -> QWidget:
        组 = QGroupBox("📊 AI 运行详情（调度器 / 学习库）")
        布局 = QVBoxLayout(组)
        self.详情框 = QTextEdit()
        self.详情框.setReadOnly(True)
        self.详情框.setStyleSheet(
            "font-family: Consolas, 'DejaVu Sans Mono', monospace;"
            "font-size: 12px;")
        布局.addWidget(self.详情框, 1)
        return 组

    # ==================== 模型 / 价格 ====================

    def _刷新模型列表(self):
        """🔄：用当前厂家的接口地址 + 密钥重新拉一次模型清单（后台执行）。

        用户的要求是"用 API Key 准确获取各厂家支持的模型"，所以这里不再走
        DeepSeek 专用的固定名单，而是直接问当前厂家的 ``/models``。
        拉不到（有的网关不提供）就退回该厂家的推荐清单，并在下拉框里保留手输。
        """
        自 = self
        if hasattr(自, "_拉取模型"):
            自._拉取模型()
            return
        运行时 = self.运行时
        if 运行时 is None:
            return
        try:
            运行时.刷新模型()
        except Exception:  # noqa: BLE001
            pass
        self._填充模型下拉()

    def _填充模型下拉(self):
        运行时 = self.运行时
        if 运行时 is None:
            return
        try:
            模型列表 = list(运行时.获取模型列表() or [])
        except Exception:
            模型列表 = []
        # 当前厂家"用密钥拉到过的清单"也并进来：切厂家后下拉框立刻是这家真实支持的模型
        try:
            from ..配置 import 当前厂家
            厂家 = 当前厂家(self.主窗口.配置)
            已有 = set(模型列表)
            for 名 in (厂家.get("模型列表") or []):
                if 名 and 名 not in 已有:
                    模型列表.append(名)
            当前厂家模型 = str(厂家.get("模型") or "")
        except Exception:  # noqa: BLE001
            当前厂家模型 = ""
        if not 模型列表:
            模型列表 = [str((运行时.AI配置 or {}).get("模型") or "deepseek-flash")]
        if 当前厂家模型 and 当前厂家模型 not in 模型列表:
            模型列表.insert(0, 当前厂家模型)
        # 该厂家已经拉过自己的清单 → 就只显示这家的模型，别把上一家的混进来
        try:
            厂家清单 = [str(x) for x in (厂家.get("模型列表") or []) if x]
        except Exception:  # noqa: BLE001
            厂家清单 = []
        if 厂家清单:
            去重 = list(dict.fromkeys(([当前厂家模型] if 当前厂家模型 else []) + 厂家清单))
            模型列表 = 去重
        if not 模型列表:
            模型列表 = [str((运行时.AI配置 or {}).get("模型") or "deepseek-flash")]
        当前 = str((运行时.AI配置 or {}).get("模型") or "")
        self.模型下拉框.blockSignals(True)
        self.模型下拉框.clear()
        for 模型 in 模型列表:
            self.模型下拉框.addItem(模型, 模型)
        idx = self.模型下拉框.findData(当前)
        self.模型下拉框.setCurrentIndex(idx if idx >= 0 else 0)
        try:
            补全 = self.模型下拉框.completer()
            if 补全 is not None:
                补全.setFilterMode(Qt.MatchFlag.MatchContains)   # 输中间一段也能匹配
                补全.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        except Exception:  # noqa: BLE001
            pass
        self.模型下拉框.blockSignals(False)
        self.模型下拉框.setToolTip(
            f"当前厂家可用模型（共 {self.模型下拉框.count()} 个，可直接打字筛选）。\n"
            "名字是用你的密钥从该厂家接口拉取的，不是内置写死的名单。")

    def _切换模型(self, _索引=None):
        运行时 = self.运行时
        模型 = self.模型下拉框.currentData()
        if 运行时 is None or not 模型:
            return
        try:
            ai = dict(运行时.配置.get("AI") or {})
            if ai.get("模型") == 模型:
                return
            ai["模型"] = 模型
            运行时.配置["AI"] = ai
            运行时.AI配置["模型"] = 模型
            运行时.保存配置()
            self.主窗口.追加日志(f"AI 模型已切换为：{模型}")
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "切换失败", str(e))
        self._刷新价格区()
        self._刷新横幅()

    def _手动刷新价格(self):
        运行时 = self.运行时
        抓取器 = self._抓取器()
        if 运行时 is None or 抓取器 is None:
            QMessageBox.information(self, "提示", "价格抓取器不可用（AI 未启用）")
            return
        self.刷新价格按钮.setEnabled(False)
        self.刷新价格按钮.setText("⏳ 刷新中…")
        QApplication.processEvents()
        try:
            成功 = 抓取器.手动刷新()
            self.主窗口.追加日志(
                "DeepSeek 价格已刷新" if 成功 else "价格刷新失败（沿用缓存）")
        except Exception as e:  # noqa: BLE001
            self.主窗口.追加日志(f"价格刷新异常：{e}")
        finally:
            self.刷新价格按钮.setEnabled(True)
            self.刷新价格按钮.setText("🔄 刷新价格")
        self._刷新价格区()
        self._刷新横幅()

    def _刷新价格表(self):
        抓取器 = self._抓取器()
        运行时 = self.运行时
        if 抓取器 is None or 运行时 is None:
            self.价格表.setRowCount(0)
            self.价格状态标签.setText("AI 未启用或没有价格抓取器")
            return
        try:
            当前时段 = 运行时.获取当前时段类型()
            当前模型 = self.模型下拉框.currentData()
            模型列表 = list(抓取器.获取所有模型名() or [])
            self.价格表.setRowCount(len(模型列表) * 2)
            行 = 0
            for 模型名 in 模型列表:
                价格 = 抓取器.获取价格(模型名) or {}
                for 时段 in ("空闲", "高峰"):
                    价 = 价格.get(时段, {}) or {}
                    是当前时段 = 时段 == 当前时段
                    是当前模型 = 模型名 == 当前模型
                    图标 = self._时段图标(时段)
                    项 = QTableWidgetItem(模型名 + (" ⭐" if 是当前模型 else ""))
                    self.价格表.setItem(行, 0, 项)
                    self.价格表.setItem(
                        行, 1, QTableWidgetItem(
                            f"{时段} {图标}" if 是当前时段 else 时段))
                    for 列, 键 in enumerate(("缓存命中", "缓存未命中", "输出"), start=2):
                        值 = float(价.get(键, 0) or 0)
                        单元 = QTableWidgetItem(f"¥{值:.3f}")
                        单元.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                        if 键 == "输出":
                            if 值 and 值 < self.便宜阈值:
                                单元.setForeground(QColor("#2ecc71"))
                            elif 值 > self.贵阈值:
                                单元.setForeground(QColor("#e74c3c"))
                        self.价格表.setItem(行, 列, 单元)
                    if 是当前时段 and 是当前模型:
                        标记 = "✅ 使用中"
                    elif 是当前时段:
                        标记 = f"{图标} 当前"
                    else:
                        标记 = ""
                    self.价格表.setItem(行, 5, QTableWidgetItem(标记))
                    行 += 1
            self.价格状态标签.setText(
                f"💡 当前时段：{当前时段} | ⭐ = 选中模型 | "
                "🌿 空闲 / 🔥 高峰（价格来自公开页解析，仅供参考）")
        except Exception as e:  # noqa: BLE001
            self.价格状态标签.setText(f"⚠️ 价格表刷新失败：{e}")

    def _刷新横幅(self):
        运行时 = self.运行时
        if 运行时 is None:
            self.提示横幅.setText("⚠️ AI 层不可用（导入失败），请检查日志")
            return
        抓取器 = self._抓取器()
        当前模型 = (self.模型下拉框.currentData()
                or (运行时.AI配置 or {}).get("模型") or "")
        try:
            时段 = 运行时.获取当前时段类型()
        except Exception:
            时段 = "空闲"
        若可用 = False
        try:
            若可用 = bool(运行时.是否可用())
        except Exception:
            pass
        if not 若可用:
            本地可用 = False
            try:
                本地可用 = bool((运行时.获取本地模型摘要() or {}).get("可用"))
            except Exception:
                pass
            有密钥 = bool(str((运行时.AI配置 or {}).get("api密钥") or "").strip())
            if not 有密钥 and 本地可用:
                self.提示横幅.setText(
                    "🏠 没有云端密钥，但**本地模型可用**：勾选上面的「启用本地模型」"
                    "即可免费用（离线、不上传数据）。")
            elif not 有密钥:
                self.提示横幅.setText(
                    "⚠️ 还没有可用的 DeepSeek API 密钥：AI 只做规则级调度"
                    "（不影响传输）。把密钥粘到上面的「🔑 DeepSeek API 密钥」"
                    "卡片里点保存即可，或改用下面的本地模型（免费·离线）。")
            else:
                self.提示横幅.setText(
                    "⏸ 当前时段不调用 AI（或预算已熔断）：AI 只做规则级调度。"
                    "本地模型可用时不受此限制。")
            self.提示横幅.setStyleSheet(
                "font-size: 13px; font-weight: bold; padding: 10px 14px;"
                "border-radius: 6px; background: #fff3e0; color: #e65100;"
                "border: 1px solid #ffcc80;")
            return
        if 抓取器 is None:
            self.提示横幅.setText("💡 未接入价格抓取器")
            return
        try:
            价 = (抓取器.获取价格(当前模型) or {}).get(时段, {}) or {}
            输出 = float(价.get("输出", 0) or 0)
            if 输出 and 输出 < self.便宜阈值:
                背景, 文字, 边框, 评价 = "#e8f5e9", "#1b5e20", "#a5d6a7", "💚 便宜"
            elif 输出 > self.贵阈值:
                背景, 文字, 边框, 评价 = "#ffebee", "#b71c1c", "#ef9a9a", "💸 昂贵"
            else:
                背景, 文字, 边框, 评价 = "#e3f2fd", "#0d47a1", "#90caf9", "💙 中等"
            # 本地模型在用时，横幅直接标明（免费、不消耗云端额度）
            本地标注 = ""
            try:
                本地 = 运行时.获取本地模型摘要() or {}
                if 本地.get("启用") and 本地.get("可用"):
                    本地标注 = (f"🏠 本地模型优先：{本地.get('模型')}"
                            f"（免费·离线；云端仅兜底）｜ ")
            except Exception:
                pass
            self.提示横幅.setText(
                f"💡 {本地标注}当前使用：{当前模型} · "
                f"{self._时段图标(时段)} {时段} | "
                f"缓存命中 ¥{float(价.get('缓存命中', 0) or 0):.3f}/M · "
                f"未命中 ¥{float(价.get('缓存未命中', 0) or 0):.2f}/M · "
                f"输出 ¥{输出:.2f}/M · {评价}")
            self.提示横幅.setStyleSheet(
                f"font-size: 13px; font-weight: bold; padding: 10px 14px;"
                f"border-radius: 6px; background: {背景}; color: {文字};"
                f"border: 1px solid {边框};")
        except Exception as e:  # noqa: BLE001
            self.提示横幅.setText(f"⚠️ 价格读取失败：{e}")

    # ==================== 状态 ====================

    def _阈值变化(self, 值: float):
        运行时 = self.运行时
        if 运行时 is None:
            return
        try:
            运行时.预算管理器.设置阈值(max(0.0, float(值)))
            预算 = dict((运行时.配置.get("AI") or {}).get("预算") or {})
            预算["低余额阈值"] = float(值)
            运行时.配置.setdefault("AI", {})["预算"] = 预算
            运行时.保存配置()
        except Exception:
            pass

    def _切换AI模式(self):
        运行时 = self.运行时
        if 运行时 is None:
            return
        if getattr(运行时.预算管理器, "是否已熔断", False):
            QMessageBox.warning(self, "提示", "AI 预算已熔断，先「刷新余额」或解除熔断")
            return
        当前 = getattr(运行时.时段管理器, "AI手动覆盖状态", None)
        try:
            运行时.设置手动覆盖(True if 当前 is None
                          else (False if 当前 is True else None))
            运行时.保存配置()
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "切换失败", str(e))
        self.刷新()

    def _切换用AI(self, _=None):
        运行时 = self.运行时
        开启 = bool(self.用AI框.isChecked())
        self.用AI框.setText(f"🧠 传输时用 AI 调整并发：{'开' if 开启 else '关'}")
        if 运行时 is None:
            return
        try:
            ai = dict(运行时.配置.get("AI") or {})
            调度 = dict(ai.get("调度") or {})
            调度["自适应并发"] = 开启
            调度["文件优先级"] = 开启
            调度["失败诊断"] = 开启
            ai["调度"] = 调度
            运行时.配置["AI"] = ai
            运行时.AI配置.setdefault("调度", {}).update(调度)
            运行时.保存配置()
            传输页 = getattr(self.主窗口, "_传输页面", None)
            if 传输页 is not None:
                try:
                    传输页.用AI框.setChecked(开启)
                except Exception:
                    pass
            self.主窗口.追加日志(f"传输时使用 AI 调优：{'开' if 开启 else '关'}")
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "保存失败", str(e))

    def _手动刷新余额(self):
        运行时 = self.运行时
        if 运行时 is None:
            return
        self.刷新余额按钮.setEnabled(False)
        self.刷新余额按钮.setText("⏳ 查询中…")
        QApplication.processEvents()
        try:
            文本 = 运行时.刷新余额(落盘=True)
        except Exception as e:  # noqa: BLE001
            文本 = f"❌ 刷新余额失败：{e}"
        finally:
            self.刷新余额按钮.setEnabled(True)
            self.刷新余额按钮.setText("🔄 刷新余额并记账")
        self.详情框.setPlainText(f"{文本}\n\n{self.详情框.toPlainText()}")
        self.主窗口.追加日志(f"AI 余额：{文本}")
        self.刷新()

    # ==================== 刷新 ====================

    def 刷新(self):
        # 密钥卡片先刷：AI 层就算没起来，也要能看出密钥配没配、能不能填
        try:
            self._刷新密钥区()
        except Exception:  # noqa: BLE001
            pass
        运行时 = self.运行时
        if 运行时 is None:
            self.提示横幅.setText("⚠️ AI 层不可用（导入失败），请检查日志")
            self.详情框.setPlainText(
                getattr(self.主窗口, "_AI不可用", "") or "AI 层不可用")
            return
        预算 = 运行时.预算管理器
        self.预算余额标签.setText(f"¥{float(预算.当前余额):.2f}")
        self.阈值输入框.blockSignals(True)
        self.阈值输入框.setValue(float(getattr(预算, "阈值", 2.0)))
        self.阈值输入框.blockSignals(False)
        self.预算摘要标签.setText(运行时.获取预算摘要())
        self.时段摘要标签.setText(运行时.获取时段摘要())
        调度 = dict((运行时.AI配置 or {}).get("调度") or {})
        开启 = bool(调度.get("自适应并发", True))
        self.用AI框.setChecked(开启)
        self.用AI框.setText(f"🧠 传输时用 AI 调整并发：{'开' if 开启 else '关'}")
        self._填充模型下拉()
        try:
            self.刷新本地模型()
        except Exception as e:  # noqa: BLE001
            self.本地状态标签.setText(f"🏠 本地模型刷新失败：{e}")
        self._刷新价格表()
        self._刷新横幅()
        self._刷新状态UI()
        self._刷新详情()

    def _刷新状态UI(self):
        运行时 = self.运行时
        try:
            时段 = 运行时.获取当前时段类型()
            AI启用 = bool(运行时.时段管理器.是否启用AI())
            熔断 = bool(运行时.预算管理器.是否已熔断)
            手动 = getattr(运行时.时段管理器, "AI手动覆盖状态", None)
        except Exception as e:  # noqa: BLE001
            self.时段标签.setText(f"⚠️ {e}")
            return
        图标 = self._时段图标(时段)
        if 熔断:
            self.时段标签.setText("🚨 AI 已熔断（预算不足）")
            self.时段标签.setStyleSheet(
                "background:#ffcdd2; color:#b71c1c; border:1px solid #ef9a9a;"
                "padding:8px; border-radius:4px; font-weight:bold;")
            self.状态提示标签.setText("💡 预算已用完：刷新余额或调大额度")
            self.AI开关按钮.setEnabled(False)
            return
        色 = ("background:#e8f5e9; color:#2e7d32; border:1px solid #a5d6a7;"
             if 时段 == "空闲" else
             "background:#fff3e0; color:#e65100; border:1px solid #ffcc80;")
        self.时段标签.setText(
            f"{图标} {时段} | {'✅ AI 运行中' if AI启用 else '❌ AI 关闭'}")
        self.时段标签.setStyleSheet(
            f"{色} padding:8px; border-radius:4px; font-weight:bold;")
        if 手动 is None:
            if AI启用:
                后缀 = f"自动模式 · 当前{时段}（空闲时段）→ 会调用 AI"
            else:
                后缀 = (f"自动模式 · 当前{时段}（高峰=价格贵）→ AI 自动让路，"
                      "只做规则调度；点下面的按钮可强制开启")
        elif 手动 is True:
            后缀 = "强制开启（忽略时段，任何时间都调用 AI）"
        else:
            后缀 = "强制关闭（只做规则调度）"
        self.状态提示标签.setText(f"💡 {后缀}")
        self.AI开关按钮.setEnabled(True)

    def _刷新详情(self):
        运行时 = self.运行时
        try:
            统计 = dict(运行时.获取统计() or {})
        except Exception as e:  # noqa: BLE001
            统计 = {"错误": str(e)}
        调度键 = ("缓存命中", "相似复用", "规则降级", "AI调用", "AI失败",
                 "优先级AI", "诊断AI", "调优AI", "缓存条数", "优先级缓存",
                 "近期调用")
        学习键 = ("总记录", "正收益", "负收益", "平均收益", "平均吞吐_MBps")
        行 = ["=== 📊 AI 调度器 ==="]
        行 += [f"{键}: {统计.get(键, 0)}" for 键 in 调度键]
        行 += ["", "=== 📚 学习库 ==="]
        学习统计 = {k: v for k, v in 统计.items() if k in 学习键}
        if 学习统计:
            行 += [f"{k}: {v}" for k, v in 学习统计.items()]
        else:
            行.append("（学习库未启用或还没有记录）")
        行 += ["", "=== 🧩 运行时 ==="]
        try:
            摘要 = 运行时.获取状态摘要()
        except Exception as e:  # noqa: BLE001
            摘要 = f"（读取失败：{e}）"
        if isinstance(摘要, dict):
            行 += [f"{k}: {v}" for k, v in 摘要.items()]
        else:
            行.append(str(摘要))
        self.详情框.setPlainText("\n".join(行))
        self.调度摘要标签.setText(
            " | ".join(f"{键} {统计.get(键, 0)}"
                     for 键 in ("AI调用", "缓存命中", "规则降级", "相似复用")))

    # ==================== 目录 ====================

    def _导入密钥(self):
        """从**用户自己选的** JSON 配置文件里读 deepseek_api_key 写进本项目配置。

        为什么改成选文件：V8_3 是**完全独立**的项目，以前这里硬编码去读
        ``项目根.parent/"网盘管理_V8"/"配置.json"`` —— 那就是对别的项目产生了
        运行时依赖（别的项目删了/改名了，这里就报错）。现在只认用户选的文件，
        默认定位到本项目自己的 配置.json。
        """
        from ..配置 import 保存配置
        默认目录 = str(getattr(self.主窗口, "配置路径", "") or "")
        路径, _ = QFileDialog.getOpenFileName(
            self, "选择要导入密钥的配置文件", 默认目录, "JSON 配置 (*.json);;所有文件 (*)")
        if not 路径:
            return
        配置文件 = Path(路径)
        try:
            数据 = json.loads(配置文件.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "读取失败", str(e))
            return
        密钥 = str(数据.get("deepseek_api_key") or "").strip()
        # 兼容本项目自己的键名（配置里叫 AI.api密钥）
        if not 密钥:
            密钥 = str((数据.get("AI") or {}).get("api密钥") or "").strip()
        if not 密钥:
            QMessageBox.information(
                self, "没有密钥",
                f"这个文件里没有 deepseek_api_key / AI.api密钥：\n{配置文件}")
            return
        if QMessageBox.question(
                self, "确认导入",
                f"把 {配置文件.name} 里的 DeepSeek 密钥导入本项目配置？\n"
                f"（只写入 {getattr(self.主窗口, '配置路径', '配置.json')}，"
                "不会回写来源文件）"
        ) != QMessageBox.Yes:
            return
        try:
            ai = dict(self.主窗口.配置.get("AI") or {})
            ai["api密钥"] = 密钥
            self.主窗口.配置["AI"] = ai
            保存配置(self.主窗口.配置, getattr(self.主窗口, "配置路径", None))
            if self.运行时 is not None:
                self.运行时.AI配置["api密钥"] = 密钥
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "导入失败", str(e))
            return
        self.主窗口.追加日志(f"已从 {配置文件.name} 导入 DeepSeek 密钥（界面只显示是否已配置）")
        # 重建运行时，让新密钥生效（连启动自检预建的那个一起清，否则会被捞回来）
        self.主窗口._AI运行时 = None
        self.主窗口._外部AI运行时 = None
        self.主窗口._AI不可用 = ""
        self.刷新()

    def _打开配置(self):
        from ..配置 import 保存配置
        路径 = Path(getattr(self.主窗口, "配置路径", "") or "")
        try:
            if 路径 and not 路径.is_file():
                保存配置(self.主窗口.配置, 路径)
            if 路径:
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(路径)))
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "打开失败", str(e))
