"""AI 播放助手：面板控件 + 字幕/总结/诊断动作（播放页与独立窗口共用）。

为什么要抽出来
==============
需求是"播放器界面和功能跟原生 VLC 基本一样，区别就是多了 AI"。那 AI 这块就必须是
**一层可插拔的东西**，而不是把播放页的私有方法复制一份到独立窗口里（复制必然改漏）。
所以拆成两部分：

* :class:`AI播放面板` —— 纯控件（按钮 + 自动调优勾选 + 带时间戳的输出框），
  只发信号，不认识播放会话；
* :class:`AI字幕动作` —— 真正的活儿：找字幕（本地同名 → 缓存 → **网盘同目录**）、
  翻译、语音识别生成、总结、卡顿诊断。它通过"取会话"回调拿到当前该操作的会话，
  所以播放页和独立窗口可以各拿一个实例、指向各自的会话，而共享同一套
  ``字幕助手``/``语音识别器`` 客户端。

线程模型：所有耗时动作都扔到后台线程，结果通过 ``状态更新`` 信号回界面线程
（Qt 信号跨线程安全）。日志一律走 :meth:`AI字幕动作.日志` 回调，由宿主决定怎么显示
（播放页显示在页面面板里，独立窗口显示在窗口面板里）。
"""

from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (QCheckBox, QGridLayout, QHBoxLayout, QLabel,
                               QPlainTextEdit, QPushButton, QVBoxLayout, QWidget)

__all__ = ["AI播放面板", "AI字幕动作"]

#: 字幕扩展名（网盘里常见的就这几种）
字幕后缀 = (".srt", ".ass", ".ssa", ".vtt")


class AI播放面板(QWidget):
    """AI 面板：自动调优勾选 + 四个动作按钮 + 带时间戳的输出框。"""

    翻译 = Signal()
    生成字幕 = Signal()
    总结 = Signal()
    诊断 = Signal()

    def __init__(self, 父=None, *, 自动调优: bool = True, 紧凑: bool = False):
        super().__init__(父)
        # 紧凑模式（独立窗口右栏只有 ~380px）：标题与勾选一行，四个按钮 2×2，
        # 并且用短标签 —— 否则按钮文字会被截成"AI 播放""卡顿自""翻译"这种（实测）
        行 = QHBoxLayout()
        行.setSpacing(6)
        标题 = QLabel("🤖 AI 助手" if 紧凑 else "🤖 AI 播放助手")
        标题.setStyleSheet("font-weight: bold;")
        行.addWidget(标题)

        self.自动调优框 = QCheckBox("卡顿自动换参数重载")
        self.自动调优框.setChecked(bool(自动调优))
        self.自动调优框.setToolTip(
            "播放中每 2 秒采样一次（丢帧/缓冲）；判断卡顿就带新参数从当前位置重开。\n"
            "libvlc 的缓存/硬解只能在起播前生效，所以重开是唯一的真优化手段。")
        行.addWidget(self.自动调优框)

        self.翻译按钮 = QPushButton("🌐 翻译字幕" if 紧凑 else "🌐 AI 翻译字幕")
        self.翻译按钮.setToolTip("把当前视频的字幕翻成中文（本地模型优先，免费）")
        self.翻译按钮.clicked.connect(self.翻译.emit)
        行.addWidget(self.翻译按钮)

        self.生字幕按钮 = QPushButton("🎙 生成字幕" if 紧凑 else "🎙 AI 生成字幕")
        self.生字幕按钮.setToolTip("视频没有字幕时，用本地 Whisper 识别语音生成字幕")
        self.生字幕按钮.clicked.connect(self.生成字幕.emit)
        行.addWidget(self.生字幕按钮)

        self.总结按钮 = QPushButton("📝 总结" if 紧凑 else "📝 AI 总结")
        self.总结按钮.setToolTip("总结内容、章节、标签；点章节可跳转")
        self.总结按钮.clicked.connect(self.总结.emit)
        行.addWidget(self.总结按钮)

        self.诊断按钮 = QPushButton("🩺 诊断" if 紧凑 else "🩺 播放诊断")
        self.诊断按钮.setToolTip("采样丢帧/缓冲，让 AI 顾问判断要不要换参数")
        self.诊断按钮.clicked.connect(self.诊断.emit)
        行.addWidget(self.诊断按钮)

        self.AI输出 = QPlainTextEdit()
        self.AI输出.setReadOnly(True)
        self.AI输出.setPlaceholderText(
            "AI 结果会显示在这里：字幕翻译 / 生成字幕 / 内容总结 / 卡顿诊断。\n"
            "没有配置云端密钥时自动用本地模型（免费离线），本地模型也没有就走规则。")
        self.AI输出.setMaximumBlockCount(2000)     # 别让长时间播放把内存吃满

        布局 = QVBoxLayout(self)
        布局.setContentsMargins(0, 0, 0, 0)
        布局.setSpacing(6)
        布局.addLayout(行)
        if 紧凑:
            # 四个按钮排两行，窄面板里也能把字显示全
            格子 = QGridLayout()
            格子.setSpacing(6)
            格子.addWidget(self.翻译按钮, 0, 0)
            格子.addWidget(self.生字幕按钮, 0, 1)
            格子.addWidget(self.总结按钮, 1, 0)
            格子.addWidget(self.诊断按钮, 1, 1)
            布局.addLayout(格子)
        else:
            按钮行 = QHBoxLayout()
            按钮行.setSpacing(6)
            for 按钮 in (self.翻译按钮, self.生字幕按钮, self.总结按钮,
                       self.诊断按钮):
                按钮行.addWidget(按钮)
            布局.addLayout(按钮行)
        布局.addWidget(self.AI输出, 1)

    # ---------------- 显示 ----------------

    #: 这些是"过程噪音"，不是决策：默认折叠（只在重复到一定次数时提示一行）
    _噪音关键词 = (
        "本地模型输出不是 JSON", "不是 JSON，尝试其它来源",
        "需要调整=False", "需要调整 = False", "动作=[]", "动作 = []",
        "诊断回落规则", "回落规则：未配置 AI", "云端诊断：需要调整",
        "规则诊断：需要调整", "本地模型失败", "云端助手没返回有效 JSON",
        "查询学习库", "读学习库", "写学习库",
    )

    def _是噪音(self, 文本: str) -> bool:
        return any(词 in 文本 for 词 in self._噪音关键词)

    def 追加(self, 文本: str) -> None:
        """追加一行带时间戳的日志（**任何线程调用都安全**）。

        用户反馈：AI 助手每秒重复输出「本地模型输出不是 JSON」「云端诊断：
        需要调整=False」这类过程信息，观影时完全没法看。所以：

        * **同类过程信息折叠**：不显示，只在重复到 2/5/10/20/50… 次时提示一行；
        * **连续重复行折叠**：同上（按内容相同判断）；
        * 只有"AI 真的做了决策"（[AI 决策]/[自动调优]/[重要] 这类）才逐条显示。
        """
        文本 = str(文本 or "")
        try:
            噪音 = self._是噪音(文本)
            重复 = 文本 == getattr(self, "_上次行", None) and 文本 != ""
            if 噪音 or 重复:
                次数 = getattr(self, "_折叠次数", {}).get(文本, 0) + 1
                self._折叠次数 = getattr(self, "_折叠次数", {})
                self._折叠次数[文本] = 次数
                self._上次行 = 文本
                # 只在 2/5/10/20/50/100… 这些节点提示一次，其余时间保持安静
                if not (次数 in (10, 100) or 次数 % 1000 == 0):
                    return
                提示 = f"（同类调试信息已折叠，累计 {次数} 次）"
                self.AI输出.appendPlainText(
                    f"[{datetime.now():%H:%M:%S}] {文本[:60]}… {提示}")
            else:
                self._上次行 = 文本
                self.AI输出.appendPlainText(f"[{datetime.now():%H:%M:%S}] {文本}")
            滚动 = self.AI输出.verticalScrollBar()
            滚动.setValue(滚动.maximum())
        except Exception:  # noqa: BLE001
            pass

    def 文本(self) -> str:
        try:
            return self.AI输出.toPlainText()
        except Exception:  # noqa: BLE001
            return ""

    def 清空(self) -> None:
        try:
            self.AI输出.clear()
        except Exception:  # noqa: BLE001
            pass

    def 设置忙碌(self, 忙: bool) -> None:
        for 按钮 in (self.翻译按钮, self.生字幕按钮, self.总结按钮, self.诊断按钮):
            按钮.setEnabled(not 忙)


class AI字幕动作:
    """把 AI 观影的四件事做掉：翻译 / 生成字幕 / 总结 / 诊断。

    :param 取会话: ``() -> 播放会话``（页面和独立窗口各给自己那一个）
    :param 字幕助手: :class:`v8_3.AI.字幕助手.字幕助手`
    :param 语音识别器: :class:`v8_3.播放.语音识别.语音识别器` 或 None
    :param 日志: ``(文本) -> None``（界面任意线程可调）
    :param 状态: ``(dict) -> None``，形如 ``{"类型": "翻译完成", ...}``，
        宿主据此更新按钮/状态栏（与页面原有约定一致）
    """

    def __init__(self, 取会话: Callable, 字幕助手=None, 语音识别器=None,
                 日志: Optional[Callable] = None,
                 状态: Optional[Callable] = None):
        self._取会话 = 取会话
        self.字幕助手 = 字幕助手
        self.语音识别器 = 语音识别器
        self.日志 = 日志 or (lambda _t: None)
        self.状态 = 状态 or (lambda _d: None)
        self._线程: list[threading.Thread] = []

    # ---------------- 工具 ----------------

    def _会话(self):
        try:
            return self._取会话()
        except Exception:  # noqa: BLE001
            return None

    def _后台(self, 目标: Callable, 名字: str):
        线程 = threading.Thread(target=目标, name=名字, daemon=True)
        线程.start()
        self._线程.append(线程)

    def 缓存字幕目录(self) -> Path:
        """字幕缓存目录（固定项目根下，不随启动时的 cwd 漂移）。"""
        from ..配置 import 项目根
        目录 = 项目根 / "数据" / "字幕"
        目录.mkdir(parents=True, exist_ok=True)
        return 目录

    def _本地同名字幕(self):
        会话 = self._会话()
        路径 = Path(会话.远端路径) if 会话 is not None and 会话.远端路径 else None
        if 路径 is None or not 路径.is_file():
            return None            # 网盘视频在本地没有实体文件，交给网盘查找
        for 后缀 in 字幕后缀:
            候选 = 路径.with_suffix(后缀)
            if 候选.is_file():
                return 候选
        return None

    def _缓存字幕(self):
        会话 = self._会话()
        if 会话 is None:
            return None
        基名 = Path(会话.标题).stem
        for 后缀 in 字幕后缀:
            直接 = self.缓存字幕目录() / f"{基名}{后缀}"
            if 直接.is_file():
                return 直接
        for 文件 in sorted(self.缓存字幕目录().glob(f"{基名}*")):
            if 文件.suffix.lower() in 字幕后缀:
                return 文件
        return None

    def _网盘同名字幕(self):
        """在**网盘同一目录**里找同名字幕并下载到缓存（会联网，只在后台线程调）。"""
        会话 = self._会话()
        if 会话 is None or not 会话.网盘标识 or 会话.网盘标识 == "本地":
            return None
        远端 = str(会话.远端路径 or "")
        if "/" not in 远端:
            return None
        目录, 文件名 = 远端.rsplit("/", 1)
        基名 = Path(文件名).stem
        适配器 = 会话.取适配器(会话.网盘标识)
        try:
            条目们 = 适配器.列目录(目录 or "/")
        except Exception as e:  # noqa: BLE001
            self.状态({"类型": "字幕提示", "说明": f"列网盘目录失败：{e}"})
            return None
        候选: list[tuple[str, str]] = []
        for 项 in 条目们 or []:
            if isinstance(项, dict):
                名 = str(项.get("name") or "")
                远端路径 = str(项.get("path") or "")
                是目录 = bool(项.get("is_dir"))
            else:
                名 = str(getattr(项, "name", "") or "")
                远端路径 = str(getattr(项, "path", "") or "")
                是目录 = bool(getattr(项, "is_dir", False))
            if 是目录 or not 名:
                continue
            if Path(名).suffix.lower() not in 字幕后缀:
                continue
            远端路径 = 远端路径 or f"{目录}/{名}"
            if Path(名).stem == 基名:
                候选.insert(0, (名, 远端路径))
            elif Path(名).stem.startswith(基名):
                候选.append((名, 远端路径))
        if not 候选:
            return None
        本地名, 远端路径 = 候选[0]
        目标 = (self.缓存字幕目录() / 本地名).resolve()
        适配器.下载(远端路径, str(目标))
        return 目标 if 目标.is_file() else None

    def 取字幕条目(self, 允许联网: bool = False):
        """拿字幕条目：本地同名 → 缓存 →（后台线程里）网盘同名。"""
        助手 = self.字幕助手
        if 助手 is None:
            return [], "AI 字幕助手不可用"
        会话 = self._会话()
        if 会话 is None or not str(getattr(会话, "远端路径", "") or ""):
            return [], "还没开始播放：先播一个视频，再翻译/总结字幕"
        本地 = self._本地同名字幕()
        if 本地 is not None:
            return 助手.读取字幕文件(str(本地)), f"本地字幕 {本地.name}"
        缓存 = self._缓存字幕()
        if 缓存 is not None:
            return 助手.读取字幕文件(str(缓存)), f"缓存字幕 {缓存.name}"
        if 允许联网:
            网盘 = self._网盘同名字幕()
            if 网盘 is not None:
                return 助手.读取字幕文件(str(网盘)), f"网盘字幕 {网盘.name}"
        return [], ("没找到字幕：网盘同目录/本地同名/数据/字幕 里都没有 .srt"
                 "（可用「🎙 AI 生成字幕」从语音生成）")

    # ---------------- 四个动作 ----------------

    def 翻译字幕(self, 目标语言: str = "中文") -> None:
        助手 = self.字幕助手
        if 助手 is None:
            self.日志("[字幕] AI 字幕助手不可用（没有可用 AI）")
            self.状态({"类型": "字幕提示", "说明": "AI 字幕助手不可用"})
            return
        self.日志("[字幕] 查找字幕（本地同名 → 数据/字幕 → 网盘同目录）…")

        def 干活():
            try:
                条目, 来源 = self.取字幕条目(允许联网=True)
                if not 条目:
                    self.状态({"类型": "字幕提示", "说明": 来源})
                    return
                self.状态({"类型": "字幕提示",
                        "说明": f"用{来源}，开始翻译 {len(条目)} 条…"})
                翻译后, 统计 = 助手.翻译(条目, 目标语言=目标语言,
                                    进度回调=lambda i, n: None)
                基名 = Path(self._会话().标题).stem if self._会话() else "字幕"
                输出 = self.缓存字幕目录() / f"{基名}.zh.srt"
                助手.写出字幕文件(str(输出), 翻译后)
                self.状态({"类型": "翻译完成", "路径": str(输出), "统计": 统计})
            except Exception as e:  # noqa: BLE001
                self.状态({"类型": "翻译失败", "错误": str(e)})

        self._后台(干活, "字幕翻译")

    def 生成字幕(self) -> None:
        if self.语音识别器 is None:
            self.状态({"类型": "字幕提示",
                    "说明": "本机还没装好本地 Whisper（见 docs/语音识别.md）"})
            return
        会话 = self._会话()
        来源 = str(会话.直链信息.get("url") or "") if 会话 else ""
        if not 来源:
            self.状态({"类型": "字幕提示", "说明": "先播放一个视频再生成字幕"})
            return
        self.日志("[字幕] 开始本地语音识别（可能需要几分钟）…")

        def 干活():
            try:
                可用, 说明 = self.语音识别器.可用()
                if not 可用:
                    raise RuntimeError(说明)
                段落 = self.语音识别器.识别(来源, 进度回调=lambda p, s: None)
                助手 = self.字幕助手
                条目 = 助手.识别结果转条目(段落) if 助手 is not None else []
                基名 = Path(self._会话().标题).stem if self._会话() else "字幕"
                输出 = self.缓存字幕目录() / f"{基名}.识别.srt"
                if 助手 is not None:
                    助手.写出字幕文件(str(输出), 条目)
                else:
                    from ..播放.语音识别 import 转SRT
                    输出.write_text(转SRT(段落), encoding="utf-8")
                self.状态({"类型": "生成字幕完成", "路径": str(输出),
                        "条数": len(条目)})
            except Exception as e:  # noqa: BLE001
                self.状态({"类型": "生成字幕失败", "错误": str(e)})

        self._后台(干活, "语音识别")

    def 总结(self) -> None:
        助手 = self.字幕助手
        if 助手 is None:
            self.状态({"类型": "字幕提示", "说明": "AI 字幕助手不可用"})
            return
        self.日志("[总结] 查找素材（本地同名 → 数据/字幕 → 网盘同目录）…")

        def 干活():
            try:
                条目, 来源 = self.取字幕条目(允许联网=True)
                if not 条目:
                    self.状态({"类型": "字幕提示",
                            "说明": "总结需要字幕文本：" + 来源})
                    return
                self.状态({"类型": "字幕提示",
                        "说明": f"用{来源}，开始总结 {len(条目)} 条…"})
                会话 = self._会话()
                结果 = 助手.总结(条目, 标题=会话.标题 if 会话 else "")
                self.状态({"类型": "总结完成", "结果": 结果})
            except Exception as e:  # noqa: BLE001
                self.状态({"类型": "总结失败", "错误": str(e)})

        self._后台(干活, "AI总结")

    def 诊断(self) -> None:
        会话 = self._会话()
        if 会话 is None:
            return
        self.日志("[诊断] 采样中…")

        def 干活():
            try:
                建议 = 会话.采样并诊断()
            except Exception as e:  # noqa: BLE001
                建议 = {"需要调整": False, "理由": f"诊断失败：{e}"}
            if 建议:
                self.状态({"类型": "调优建议", "建议": 建议})

        self._后台(干活, "播放诊断")
