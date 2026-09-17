"""AI 状态页面（切换页）。

移植 网盘管理_V8 / UI层/AI页面.py 的界面与信息（预算、时段、模型、价格表、
调度器统计），改成跟 V8_3 的 :class:`~v8_3.AI.运行时.AI运行时` 打交道：

* 顶部：价格来源 + 刷新价格；
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

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (
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
        self._构建()
        self.刷新()

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
        self.页面滚动区.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.页面滚动区.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
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
        顶部.addStretch(1)
        self.价格来源标签 = QLabel("💰 价格：加载中…")
        self.价格来源标签.setStyleSheet(
            "font-size: 12px; padding: 4px 10px; border-radius: 4px;"
            "background: #34495e; color: #ecf0f1;")
        顶部.addWidget(self.价格来源标签)
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

        # ---- 云端密钥（V8_3 新增：在页面上直接填，不用再手改 配置.json）----
        布局.addWidget(self._建密钥区())

        # ---- 本地 DeepSeek 模型（V8_3 新增：免费、离线）----
        布局.addWidget(self._建本地模型区())

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
        布局.addLayout(三列, 1)

        底部 = QHBoxLayout()
        self.刷新余额按钮 = QPushButton("🔄 刷新余额并记账")
        self.刷新余额按钮.setObjectName("PrimaryButton")
        self.刷新余额按钮.clicked.connect(self._手动刷新余额)
        底部.addWidget(self.刷新余额按钮)
        self.调度摘要标签 = QLabel("")
        self.调度摘要标签.setWordWrap(True)
        底部.addWidget(self.调度摘要标签, 1)
        布局.addLayout(底部)

        self.页面滚动区.setWidget(self.页面内容)

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

    # ==================== 本地模型（V8_3 新增） ====================

    def _建密钥区(self) -> QWidget:
        """🔑 云端 DeepSeek 密钥卡片：页面上直接填 / 直接测，不用手改 JSON。

        密钥只写进本项目自己的 ``配置.json``（``AI.api密钥``）；
        界面与日志都只显示遮盖后的形态，不打印明文。
        """
        组 = QGroupBox("🔑 DeepSeek API 密钥（云端）")
        外层 = QVBoxLayout(组)

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
        self.密钥输入框.setPlaceholderText("sk-…（粘贴 DeepSeek 控制台里的 API key）")
        self.密钥输入框.setMinimumWidth(320)
        self.密钥输入框.setToolTip(
            "直接粘贴密钥后按「💾 保存密钥」或回车即可生效。\n"
            "保存会写进本项目 配置.json 的 AI.api密钥，并立刻重建 AI 层。")
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
        """当前真正在用的密钥：优先运行时（它才是干活的那个），退回主窗口配置。"""
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

    def _刷新密钥区(self):
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
        检测按钮 = QPushButton("🔍 检测")
        检测按钮.clicked.connect(lambda: self.刷新本地模型(重新检测=True))
        行2.addWidget(检测按钮)
        测速按钮 = QPushButton("⏱ 测速")
        测速按钮.clicked.connect(self._本地模型测速)
        行2.addWidget(测速按钮)
        启动按钮 = QPushButton("▶ 启动服务")
        启动按钮.setToolTip("拉起 ollama serve（用户目录安装，不需要 root）")
        启动按钮.clicked.connect(self._启动本地服务)
        行2.addWidget(启动按钮)
        拉取按钮 = QPushButton("📥 拉取模型")
        拉取按钮.setToolTip("ollama pull <模型>；1.5B 约 1.1 GB")
        拉取按钮.clicked.connect(self._拉取本地模型)
        行2.addWidget(拉取按钮)
        指引按钮 = QPushButton("📖 安装指引")
        指引按钮.clicked.connect(self._显示本地模型指引)
        行2.addWidget(指引按钮)
        行2.addStretch(1)
        外层.addLayout(行2)

        self.本地提示标签 = QLabel(
            "没装运行时也能用：点「📖 安装指引」复制几条命令（全部装在用户目录）。")
        self.本地提示标签.setWordWrap(True)
        self.本地提示标签.setStyleSheet("font-size: 11px; color: #95a5a6;")
        外层.addWidget(self.本地提示标签)
        return 组

    def 刷新本地模型(self, 重新检测: bool = False) -> None:
        运行时 = self.运行时
        if 运行时 is None:
            self.本地状态标签.setText("🏠 AI 运行时未就绪")
            return
        try:
            摘要 = 运行时.获取本地模型摘要() or {}
            if 重新检测:
                运行时.获取本地模型状态(重新检测=True)
                摘要 = 运行时.获取本地模型摘要() or {}
        except Exception as e:  # noqa: BLE001
            self.本地状态标签.setText(f"🏠 本地模型检测失败：{e}")
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
        模型列表 = list(摘要.get("模型列表") or [])
        当前 = str(摘要.get("模型") or "")
        if 模型列表:
            self.本地模型框.blockSignals(True)
            现有 = [self.本地模型框.itemText(i)
                  for i in range(self.本地模型框.count())]
            if 现有 != 模型列表:
                self.本地模型框.clear()
                self.本地模型框.addItems(模型列表)
            idx = self.本地模型框.findText(当前)
            self.本地模型框.setCurrentIndex(max(0, idx))
            self.本地模型框.blockSignals(False)
        if not 摘要.get("可用"):
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
        if self.运行时 is None:
            return
        self.本地状态标签.setText("⏱ 正在测速…（首次会加载模型，可能要十几秒）")
        QApplication.processEvents()
        结果 = self.运行时.本地模型测速()
        if not 结果.get("成功"):
            self.本地状态标签.setText(f"❌ 测速失败：{结果.get('错误')}")
            return
        self.本地状态标签.setText(
            f"✅ {结果.get('模型')} · 用时 {结果.get('用时秒')}s · "
            f"{结果.get('每秒tokens')} tokens/s · 免费")
        self._记日志(f"[本地模型] 测速：{结果.get('用时秒')}s / "
                  f"{结果.get('每秒tokens')} tokens/s")

    def _启动本地服务(self) -> None:
        if self.运行时 is None:
            return
        self.本地状态标签.setText("▶ 正在启动本地服务…")
        QApplication.processEvents()
        成功, 消息 = self.运行时.启动本地服务()
        self.本地状态标签.setText(("✅ " if 成功 else "❌ ") + 消息)
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
        self.本地状态标签.setText(f"📥 正在拉取 {模型}…（可在日志页看进度）")
        QApplication.processEvents()
        成功, 消息 = self.运行时.拉取本地模型(模型)
        self.本地状态标签.setText(("✅ " if 成功 else "❌ ") + 消息)
        self.刷新本地模型(重新检测=True)

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
        运行时 = self.运行时
        if 运行时 is None:
            return
        try:
            运行时.刷新模型()
        except Exception:
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
        if not 模型列表:
            模型列表 = [str((运行时.AI配置 or {}).get("模型") or "deepseek-flash")]
        当前 = str((运行时.AI配置 or {}).get("模型") or "")
        self.模型下拉框.blockSignals(True)
        self.模型下拉框.clear()
        for 模型 in 模型列表:
            self.模型下拉框.addItem(模型, 模型)
        idx = self.模型下拉框.findData(当前)
        self.模型下拉框.setCurrentIndex(idx if idx >= 0 else 0)
        self.模型下拉框.blockSignals(False)

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

    def _刷新价格来源标签(self):
        抓取器 = self._抓取器()
        if 抓取器 is None:
            self.价格来源标签.setText("💰 未接入价格抓取器")
            return
        try:
            信息 = 抓取器.获取元信息() or {}
            来源 = 信息.get("来源", "unknown")
            时间戳 = float(信息.get("时间戳") or 0)
            过期 = time.time() - 时间戳 if 时间戳 else 0
            图标 = {"web": "🌐", "builtin": "📦"}.get(来源, "❓")
            状态 = f"（已过期 {过期/3600:.1f}h）" if 过期 > self._价格过期阈值 else ""
            self.价格来源标签.setText(
                f"💰 {图标} {来源} | {信息.get('抓取时间', '?')} | "
                f"{信息.get('模型数', 0)} 个模型 {状态}")
        except Exception as e:  # noqa: BLE001
            self.价格来源标签.setText(f"💰 状态未知：{e}")

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
        self._刷新价格来源标签()
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
