# V8_3/界面/主题管理器.py
"""
主题管理器 - 10 套程序员主题（V2 高对比度选中版）

来源：移植自 网盘管理_V8 / UI层/主题管理器.py（只读学习后在本项目内独立实现，
V8_3 不 import 也不修改 V8 的任何文件），并补充了 V8_3 新增的
「左侧网盘导航滚动区」「左下网盘管理面板」「网盘页管理区」样式。

使用：
    from v8_3.界面.主题管理器 import 主题管理器
    app.setStyleSheet(主题管理器.获取样式表("Dracula"))
"""
import os
from pathlib import Path
from typing import Dict, List

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPen


# ============================================================
# 左侧栏按钮高度（QSS 与主窗口共用这一份，避免两套数字打架）
# ============================================================
# 为什么要写进 QSS：主题里的 ``padding`` 会让 Qt 把控件的**最小高度**算成
# 「文字高 + 上下内边距」——60px 会被压成 42px；窗口一矮，布局就顺着这个最小高度
# 把按钮挤扁、两行文字被裁掉。QSS 的 ``min-height`` 管的是**内边距以内**的高度，
# 所以下面要减掉上下 padding（60 - 12×2 = 36），算出来的总高才等于这里的常数。
高度_网盘按钮 = 58
高度_功能按钮 = 60
高度_管理按钮 = 52

#: 下拉框右侧"展开按钮"的宽度，以及里面那个箭头的尺寸（QSS 与生成图片共用）。
宽度_下拉按钮 = 28
宽度_箭头 = 22
高度_箭头 = 14

#: 运行时生成的界面小图标放这儿（**现画现用**，项目不带任何图片资源）
图标目录 = Path(__file__).resolve().parents[2] / "数据" / "图标"

#: 主题的"全局那一层"（每个控件都要沾的：背景色 / 文字色 / 字号）怎么落地。
#:
#: * ``样式表``：QSS 里写一条 ``QWidget {...}``（老写法）；
#: * ``调色板``：背景/文字/选中色走 ``QPalette``、字号走应用字体，QSS 里
#:   **不再有**那条全局规则；
#: * ``无字号``：保留全局规则、只把 ``font-size`` 拿掉（字号交给应用字体）。
#:
#: 为什么要分档：真机 Windows 上"切到播放页 12.3 秒，而清掉整套 QSS 只要 40 毫秒"
#: —— 贵的就是这张全局样式表。一条 ``QWidget{}`` 规则会让**每个**控件都做一遍
#: 样式匹配 + 重新 polish（字号一变还会连锁触发整棵树重算 sizeHint）。
#: 三档用 ``工具/主题测速.py`` 在真机上量过，再改这里的默认值。
全局层 = os.environ.get("V8_3_主题全局层", "样式表")

#: 全局字号（像素）。要跟老写法 ``QWidget { font-size: 13px }`` 一致 ——
#: 换成应用字体之后字号不能变，否则整套界面的高度全变。
全局字号像素 = 13


def 应用全局外观(应用, 主题名: str = "") -> None:
    """把"全局那一层"落到 ``QApplication``（调色板 + 字号）。

    幂等：多调几次没关系。只在 :data:`全局层` 不是 ``样式表`` 时才真的需要用，
    但调色板本身跟 QSS 不冲突，所以调用方不必判断。
    """
    try:
        from PySide6.QtGui import QFont, QPalette
    except Exception:  # noqa: BLE001 - 无 GUI 环境下不致命
        return
    主题 = 主题定义.get(主题名) or 主题定义[主题管理器.默认主题]
    c = 主题["颜色"]
    调色板 = QPalette()
    配 = {
        QPalette.ColorRole.Window: c["背景"],
        QPalette.ColorRole.WindowText: c["文字"],
        QPalette.ColorRole.Base: c["卡片"],
        QPalette.ColorRole.AlternateBase: c.get("悬停") or c["卡片"],
        QPalette.ColorRole.Text: c["文字"],
        QPalette.ColorRole.Button: c["卡片"],
        QPalette.ColorRole.ButtonText: c["文字"],
        QPalette.ColorRole.ToolTipBase: c["卡片"],
        QPalette.ColorRole.ToolTipText: c["文字"],
        QPalette.ColorRole.Highlight: c["选中背景"],
        QPalette.ColorRole.HighlightedText: c["选中文字"],
        QPalette.ColorRole.PlaceholderText: c["次要文字"],
    }
    for 角色, 值 in 配.items():
        try:
            调色板.setColor(角色, QColor(值))
        except Exception:  # noqa: BLE001
            continue
    for 角色 in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text,
                QPalette.ColorRole.ButtonText):
        调色板.setColor(QPalette.ColorGroup.Disabled, 角色, QColor(c["禁用文字"]))
    try:
        应用.setPalette(调色板)
        字体 = QFont(应用.font())
        if 字体.pixelSize() != 全局字号像素:
            字体.setPixelSize(全局字号像素)
            应用.setFont(字体)
    except Exception:  # noqa: BLE001
        pass


def _箭头图片(颜色: str) -> str:
    """现画一张下拉箭头 PNG，返回绝对路径；画不出来就返回空串（调用方退回边框三角）。

    QSS 的 ``::down-arrow`` 只认 ``image: url(...)``，而本项目要求完全自包含、不带
    图片资源；边框拼三角在 Qt 里又会糊成一个方块。所以这里用 QPainter 现画一个折角
    箭头存到项目内的 ``数据/图标/``，文件名带上颜色 —— 同一颜色只生成一次，
    换主题按需再生成。
    """
    try:
        路径 = 图标目录 / f"下拉箭头_{str(颜色).lstrip('#')}.png"
        if 路径.is_file():
            return 路径.as_posix()
        图标目录.mkdir(parents=True, exist_ok=True)
        倍 = 2                                   # 2 倍图，HiDPI 下不糊
        图 = QImage(宽度_箭头 * 倍, 高度_箭头 * 倍,
                  QImage.Format.Format_ARGB32_Premultiplied)
        图.fill(Qt.GlobalColor.transparent)
        笔 = QPainter(图)
        笔.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        笔.setPen(QPen(QColor(str(颜色)), 2.2 * 倍, Qt.PenStyle.SolidLine,
                      Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        笔.setBrush(Qt.BrushStyle.NoBrush)
        中心x = 宽度_箭头 * 倍 / 2
        中心y = 高度_箭头 * 倍 / 2
        半宽 = 宽度_箭头 * 倍 * 0.30
        半高 = 高度_箭头 * 倍 * 0.28
        笔.drawPolyline([QPointF(中心x - 半宽, 中心y - 半高),
                       QPointF(中心x, 中心y + 半高),
                       QPointF(中心x + 半宽, 中心y - 半高)])
        笔.end()
        if not 图.save(str(路径)):
            return ""
        return 路径.as_posix()
    except Exception:  # noqa: BLE001 - 画不出来就走边框三角的兜底
        return ""

#: 最近一次生成的样式表用的颜色（控件样式.py 自绘勾选框/箭头时按它取色）
_最近颜色: Dict[str, str] = {}


def 当前颜色() -> Dict[str, str]:
    """当前主题的颜色表；还没套过主题时返回空字典。"""
    return dict(_最近颜色)


# ============================================================
# 10 套主题定义
# ============================================================
主题定义: Dict[str, dict] = {
    # ==================== 1. Dracula ====================
    "Dracula": {
        "显示名": "🧛 Dracula",
        "类型": "dark",
        "颜色": {
            "背景": "#282a36",
            "卡片": "#21222c",
            "输入": "#282a36",
            "文字": "#f8f8f2",
            "次要文字": "#6272a4",
            "边框": "#44475a",
            # ★ 选中：用 Dracula 紫（强调色暗版本）
            "选中背景": "#7c6fbd",
            "选中文字": "#ffffff",
            # ★ 悬停：与选中拉开差距（更暗更灰）
            "悬停": "#383a4a",
            "表头背景": "#21222c",
            "表头文字": "#f8f8f2",
            "强调": "#bd93f9",
            "强调悬停": "#cba9ff",
            "成功": "#50fa7b",
            "警告": "#f1fa8c",
            "错误": "#ff5555",
            "导航背景": "#1e1f29",
            "导航文字": "#f8f8f2",
            "导航悬停": "#282a36",
            "导航激活": "#bd93f9",
            "禁用背景": "#2d303e",
            "禁用文字": "#6272a4",
        },
    },

    # ==================== 2. One Dark Pro ====================
    "One Dark Pro": {
        "显示名": "🌑 One Dark Pro",
        "类型": "dark",
        "颜色": {
            "背景": "#282c34",
            "卡片": "#21252b",
            "输入": "#282c34",
            "文字": "#abb2bf",
            "次要文字": "#5c6370",
            "边框": "#3e4451",
            # ★ 选中：Atom 蓝
            "选中背景": "#4976c9",
            "选中文字": "#ffffff",
            "悬停": "#2c313a",
            "表头背景": "#21252b",
            "表头文字": "#dcdfe4",
            "强调": "#61afef",
            "强调悬停": "#7dc1ff",
            "成功": "#98c379",
            "警告": "#e5c07b",
            "错误": "#e06c75",
            "导航背景": "#1c1f24",
            "导航文字": "#abb2bf",
            "导航悬停": "#282c34",
            "导航激活": "#61afef",
            "禁用背景": "#2c313a",
            "禁用文字": "#5c6370",
        },
    },

    # ==================== 3. Nord ====================
    "Nord": {
        "显示名": "❄️  Nord",
        "类型": "dark",
        "颜色": {
            "背景": "#2e3440",
            "卡片": "#3b4252",
            "输入": "#3b4252",
            "文字": "#eceff4",
            "次要文字": "#4c566a",
            "边框": "#434c5e",
            # ★ 选中：Nord Frost 蓝
            "选中背景": "#5e81ac",
            "选中文字": "#ffffff",
            "悬停": "#3b4252",
            "表头背景": "#3b4252",
            "表头文字": "#eceff4",
            "强调": "#88c0d0",
            "强调悬停": "#8fbcbb",
            "成功": "#a3be8c",
            "警告": "#ebcb8b",
            "错误": "#bf616a",
            "导航背景": "#242933",
            "导航文字": "#d8dee9",
            "导航悬停": "#2e3440",
            "导航激活": "#88c0d0",
            "禁用背景": "#3b4252",
            "禁用文字": "#4c566a",
        },
    },

    # ==================== 4. Monokai Pro ====================
    "Monokai Pro": {
        "显示名": "🎨 Monokai Pro",
        "类型": "dark",
        "颜色": {
            "背景": "#2d2a2e",
            "卡片": "#221f22",
            "输入": "#2d2a2e",
            "文字": "#fcfcfa",
            "次要文字": "#727072",
            "边框": "#403e41",
            # ★ 选中：Monokai 紫
            "选中背景": "#7150a0",
            "选中文字": "#ffffff",
            "悬停": "#3a373b",
            "表头背景": "#221f22",
            "表头文字": "#fcfcfa",
            "强调": "#ffd866",
            "强调悬停": "#ffdf7e",
            "成功": "#a9dc76",
            "警告": "#ffd866",
            "错误": "#ff6188",
            "导航背景": "#19181a",
            "导航文字": "#fcfcfa",
            "导航悬停": "#2d2a2e",
            "导航激活": "#ffd866",
            "禁用背景": "#3a373b",
            "禁用文字": "#727072",
        },
    },

    # ==================== 5. Tokyo Night ====================
    "Tokyo Night": {
        "显示名": "🌃 Tokyo Night",
        "类型": "dark",
        "颜色": {
            "背景": "#1a1b26",
            "卡片": "#16161e",
            "输入": "#1a1b26",
            "文字": "#c0caf5",
            "次要文字": "#565f89",
            "边框": "#2f334d",
            # ★ 选中：Tokyo Night 蓝
            "选中背景": "#3d59a1",
            "选中文字": "#ffffff",
            "悬停": "#24283b",
            "表头背景": "#16161e",
            "表头文字": "#c0caf5",
            "强调": "#7aa2f7",
            "强调悬停": "#8bb1ff",
            "成功": "#9ece6a",
            "警告": "#e0af68",
            "错误": "#f7768e",
            "导航背景": "#0e0e15",
            "导航文字": "#c0caf5",
            "导航悬停": "#1a1b26",
            "导航激活": "#7aa2f7",
            "禁用背景": "#24283b",
            "禁用文字": "#565f89",
        },
    },

    # ==================== 6. Gruvbox Dark ====================
    "Gruvbox Dark": {
        "显示名": "🌰 Gruvbox Dark",
        "类型": "dark",
        "颜色": {
            "背景": "#282828",
            "卡片": "#1d2021",
            "输入": "#282828",
            "文字": "#ebdbb2",
            "次要文字": "#928374",
            "边框": "#3c3836",
            # ★ 选中：Gruvbox 亮灰褐
            "选中背景": "#8b7355",
            "选中文字": "#fbf1c7",
            "悬停": "#32302f",
            "表头背景": "#1d2021",
            "表头文字": "#ebdbb2",
            "强调": "#d79921",
            "强调悬停": "#e8b339",
            "成功": "#b8bb26",
            "警告": "#fabd2f",
            "错误": "#fb4934",
            "导航背景": "#1d2021",
            "导航文字": "#ebdbb2",
            "导航悬停": "#282828",
            "导航激活": "#d79921",
            "禁用背景": "#32302f",
            "禁用文字": "#928374",
        },
    },

    # ==================== 7. Catppuccin Mocha ====================
    "Catppuccin Mocha": {
        "显示名": "🐱 Catppuccin Mocha",
        "类型": "dark",
        "颜色": {
            "背景": "#1e1e2e",
            "卡片": "#181825",
            "输入": "#1e1e2e",
            "文字": "#cdd6f4",
            "次要文字": "#6c7086",
            "边框": "#313244",
            # ★ 选中：Catppuccin 蓝紫
            "选中背景": "#6f7bc4",
            "选中文字": "#ffffff",
            "悬停": "#313244",
            "表头背景": "#181825",
            "表头文字": "#cdd6f4",
            "强调": "#cba6f7",
            "强调悬停": "#d8b5ff",
            "成功": "#a6e3a1",
            "警告": "#f9e2af",
            "错误": "#f38ba8",
            "导航背景": "#11111b",
            "导航文字": "#cdd6f4",
            "导航悬停": "#1e1e2e",
            "导航激活": "#cba6f7",
            "禁用背景": "#313244",
            "禁用文字": "#6c7086",
        },
    },

    # ==================== 8. Rose Pine ====================
    "Rose Pine": {
        "显示名": "🌹 Rosé Pine",
        "类型": "dark",
        "颜色": {
            "背景": "#191724",
            "卡片": "#1f1d2e",
            "输入": "#191724",
            "文字": "#e0def4",
            "次要文字": "#6e6a86",
            "边框": "#26233a",
            # ★ 选中：Rosé Pine 紫
            "选中背景": "#6b5f8a",
            "选中文字": "#ffffff",
            "悬停": "#26233a",
            "表头背景": "#1f1d2e",
            "表头文字": "#e0def4",
            "强调": "#c4a7e7",
            "强调悬停": "#d8bff2",
            "成功": "#9ccfd8",
            "警告": "#f6c177",
            "错误": "#eb6f92",
            "导航背景": "#16141f",
            "导航文字": "#e0def4",
            "导航悬停": "#191724",
            "导航激活": "#c4a7e7",
            "禁用背景": "#26233a",
            "禁用文字": "#6e6a86",
        },
    },

    # ==================== 9. Solarized Light ====================
    "Solarized Light": {
        "显示名": "☀️  Solarized Light",
        "类型": "light",
        "颜色": {
            "背景": "#fdf6e3",
            "卡片": "#eee8d5",
            "输入": "#fdf6e3",
            "文字": "#073642",
            "次要文字": "#93a1a1",
            "边框": "#ddd6c1",
            # ★ 选中：Solarized 青绿（明显）
            "选中背景": "#7cbfb5",
            "选中文字": "#002b36",
            "悬停": "#e6dfca",
            "表头背景": "#eee8d5",
            "表头文字": "#073642",
            "强调": "#268bd2",
            "强调悬停": "#3a9ce0",
            "成功": "#859900",
            "警告": "#b58900",
            "错误": "#dc322f",
            "导航背景": "#073642",
            "导航文字": "#eee8d5",
            "导航悬停": "#0e4a5c",
            "导航激活": "#268bd2",
            "禁用背景": "#eee8d5",
            "禁用文字": "#93a1a1",
        },
    },

    # ==================== 10. GitHub Light ====================
    "GitHub Light": {
        "显示名": "🐙 GitHub Light",
        "类型": "light",
        "颜色": {
            "背景": "#ffffff",
            "卡片": "#f6f8fa",
            "输入": "#ffffff",
            "文字": "#24292f",
            "次要文字": "#57606a",
            "边框": "#d0d7de",
            # ★ 选中：GitHub 蓝（明显）
            "选中背景": "#79c0ff",
            "选中文字": "#0d1117",
            "悬停": "#eaeef2",
            "表头背景": "#f6f8fa",
            "表头文字": "#24292f",
            "强调": "#0969da",
            "强调悬停": "#218bff",
            "成功": "#1a7f37",
            "警告": "#9a6700",
            "错误": "#cf222e",
            "导航背景": "#24292f",
            "导航文字": "#f6f8fa",
            "导航悬停": "#32383f",
            "导航激活": "#0969da",
            "禁用背景": "#f6f8fa",
            "禁用文字": "#8c959f",
        },
    },
}


# ============================================================
# 主题管理器
# ============================================================
class 主题管理器:
    """主题管理器"""

    默认主题 = "Dracula"

    @classmethod
    def 获取所有主题名(cls) -> List[str]:
        return list(主题定义.keys())

    @classmethod
    def 获取主题显示名(cls, 主题名: str) -> str:
        主题 = 主题定义.get(主题名)
        if 主题:
            return 主题["显示名"]
        return 主题名

    @classmethod
    def 获取默认主题(cls) -> str:
        return cls.默认主题

    @classmethod
    def 获取主题类型(cls, 主题名: str) -> str:
        主题 = 主题定义.get(主题名)
        return 主题["类型"] if 主题 else "dark"

    @classmethod
    def 获取样式表(cls, 主题名: str) -> str:
        """生成指定主题的完整 QSS 样式表"""
        主题 = 主题定义.get(主题名)
        if 主题 is None:
            主题 = 主题定义[cls.默认主题]
        # 记住这一份颜色：控件样式.py 自绘勾选框/箭头时要按主题取色
        global _最近颜色
        _最近颜色 = dict(主题["颜色"])
        return cls._生成样式表(主题["颜色"], 主题["类型"])

    @classmethod
    def 获取样式表_档(cls, 档: str, 主题名: str = "") -> str:
        """按指定"全局层"档位生成样式表（对照实验/工具用，**不改**模块默认值）。

        真实应用只走 :meth:`获取样式表`（读 :data:`全局层`）；这个入口给
        ``工具/主题测速.py`` 在真机上把三档并排量出来用。
        """
        global 全局层
        旧 = 全局层
        全局层 = str(档 or 旧)
        try:
            return cls.获取样式表(主题名 or cls.默认主题)
        finally:
            全局层 = 旧

    @classmethod
    def _生成样式表(cls, c: dict, 类型: str) -> str:
        # 全局那一层（背景/文字色/字号）按 全局层 开关落地：
        #   · 样式表：一条 QWidget{} —— 每个控件都要匹配一次，真机 Windows 上最贵；
        #   · 调色板：这几条挪到 QPalette + 应用字体（见 应用全局外观），QSS 里删掉；
        #   · 无字号：留着全局规则，但把 font-size 拿掉（字号交给应用字体）。
        全局块 = ""
        if 全局层 != "调色板":
            字号 = "    font-size: 13px;\n" if 全局层 != "无字号" else ""
            全局块 = (f"QWidget {{\n"
                    f"    background-color: {c['背景']};\n"
                    f"    color: {c['文字']};\n"
                    f"{字号}}}")
        # 关于下拉箭头：**必须**用 QSS 的 image 画，而且必须写明定位。
        #
        # 实测（离屏渲染 + 统计 drawPrimitive 调用）：
        #   · 只要 QComboBox 写了样式表，Qt 就完全不调用 PE_IndicatorArrowDown
        #     —— 自绘箭头那条路在组合框上是走不通的（调用次数恒为 0）；
        #   · 只写 image: url(...) 而不写 subcontrol-* 定位，箭头会被摆在输入框
        #     中间、右边按钮区还多出一个，也就是"两个箭头"（用户截图反馈的就是这个）；
        #   · 加上 subcontrol-origin/position + right，才是一个、且在按钮区正中。
        箭头 = _箭头图片(c['文字'])
        箭头_激活 = _箭头图片(c['强调'])
        箭头_禁用 = _箭头图片(c['次要文字'])
        定位 = f"""    subcontrol-origin: padding;
    subcontrol-position: center right;
    right: 3px;
    width: {宽度_箭头}px;
    height: {高度_箭头}px;"""
        # 只写**一条**箭头规则。踩过的坑（都靠离屏截图对照确认）：
        #   · 只给 QComboBox 写样式表，Qt 就再也不调用 PE_IndicatorArrowDown，
        #     所以自绘箭头在组合框上走不通（调用次数恒为 0）；
        #   · 写了 image 但不写 subcontrol-* 定位 → 箭头会被摆在输入框中间；
        #   · 再加 :hover/:on/:focus/:disabled 的箭头规则 → Qt 会把基础规则和状态
        #     规则**各画一次**，于是出现"两个箭头"（用户截图反馈的就是这个）。
        # 结论：一条规则 + 明确 subcontrol 定位 = 一个、位置正确的箭头。
        # 悬停反馈由上面的 QComboBox:hover 边框变色承担，不靠换箭头图片。
        if 箭头:
            箭头规则 = f"""QComboBox::down-arrow {{
{定位}
    image: url("{箭头}");
}}"""
        else:
            # 项目目录不可写等情况下退化成边框拼的三角（没有图片也能看见）
            箭头规则 = f"""QComboBox::down-arrow {{
    subcontrol-origin: padding;
    subcontrol-position: center right;
    right: 3px;
    image: none;
    width: 0;
    height: 0;
    border-left: 6px solid transparent;
    border-right: 6px solid transparent;
    border-top: 7px solid {c['文字']};
}}"""
        return f"""
/* ============================================================
 * 主题 · {类型}
 * 由 主题管理器 自动生成
 * ============================================================ */

/* ---------------- 全局 ---------------- */
{全局块}
QMainWindow {{
    background-color: {c['背景']};
}}
QDialog {{
    background-color: {c['背景']};
    color: {c['文字']};
}}

/* ---------------- 标签 ---------------- */
QLabel {{
    color: {c['文字']};
    background: transparent;
}}
QLabel:disabled {{
    color: {c['禁用文字']};
}}

/* ---------------- 表格 ---------------- */
QTableWidget, QTableView {{
    background-color: {c['卡片']};
    alternate-background-color: {c['悬停']};
    gridline-color: {c['边框']};
    selection-background-color: {c['选中背景']};
    selection-color: {c['选中文字']};
    color: {c['文字']};
    border: 1px solid {c['边框']};
    border-radius: 4px;
}}
QTableWidget::item, QTableView::item {{
    color: {c['文字']};
    padding: 4px;
    border: none;
}}
/* ★★★ 选中：高亮背景 + 高对比文字 ★★★ */
QTableWidget::item:selected, QTableView::item:selected {{
    background-color: {c['选中背景']};
    color: {c['选中文字']};
    font-weight: bold;
}}
QTableWidget::item:selected:active, QTableView::item:selected:active {{
    background-color: {c['选中背景']};
    color: {c['选中文字']};
}}
QTableWidget::item:selected:!active, QTableView::item:selected:!active {{
    background-color: {c['选中背景']};
    color: {c['选中文字']};
}}
/* 悬停：颜色比选中浅，但比未选中明显 */
QTableWidget::item:hover, QTableView::item:hover {{
    background-color: {c['悬停']};
    color: {c['文字']};
}}
QTableWidget:disabled, QTableView:disabled {{
    background-color: {c['禁用背景']};
    color: {c['禁用文字']};
}}
QTableWidget QTableCornerButton::section {{
    background: {c['表头背景']};
    border: 1px solid {c['边框']};
}}

/* ---------------- 表头 ---------------- */
QHeaderView {{
    background-color: {c['表头背景']};
}}
QHeaderView::section {{
    background-color: {c['表头背景']};
    color: {c['表头文字']};
    padding: 6px 4px;
    border: none;
    border-right: 1px solid {c['边框']};
    border-bottom: 1px solid {c['边框']};
    font-weight: bold;
}}
QHeaderView::section:hover {{
    background-color: {c['悬停']};
}}
QHeaderView::section:last {{
    border-right: none;
}}

/* ---------------- 列表 / 树 ---------------- */
QListWidget, QTreeWidget {{
    background-color: {c['卡片']};
    color: {c['文字']};
    border: 1px solid {c['边框']};
    border-radius: 4px;
    selection-background-color: {c['选中背景']};
    selection-color: {c['选中文字']};
}}
QListWidget::item, QTreeWidget::item {{
    color: {c['文字']};
    padding: 4px;
}}
QListWidget::item:selected, QTreeWidget::item:selected {{
    background-color: {c['选中背景']};
    color: {c['选中文字']};
    font-weight: bold;
}}
QListWidget::item:hover, QTreeWidget::item:hover {{
    background-color: {c['悬停']};
}}

/* ---------------- 输入框 ---------------- */
QLineEdit, QTextEdit, QPlainTextEdit, QSpinBox, QDoubleSpinBox {{
    background-color: {c['输入']};
    color: {c['文字']};
    border: 1px solid {c['边框']};
    border-radius: 4px;
    padding: 4px 6px;
    selection-background-color: {c['强调']};
    selection-color: {c['背景']};
}}
QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus,
QSpinBox:focus, QDoubleSpinBox:focus {{
    border: 1px solid {c['强调']};
}}
QLineEdit:disabled, QTextEdit:disabled {{
    background-color: {c['禁用背景']};
    color: {c['禁用文字']};
}}
QTextEdit {{
    padding: 6px;
}}

/* ---------------- 下拉框 ---------------- */
QComboBox {{
    background-color: {c['输入']};
    color: {c['文字']};
    border: 1px solid {c['边框']};
    border-radius: 5px;
    padding: 4px {宽度_下拉按钮 + 6}px 4px 10px;   /* 右侧留出下拉按钮，文字不会被压到箭头底下 */
    min-height: 22px;
}}
QComboBox:hover {{
    border: 1px solid {c['强调']};
}}
QComboBox:focus, QComboBox:on {{
    border: 1px solid {c['强调']};
    background-color: {c['卡片']};
}}
QComboBox:disabled {{
    color: {c['次要文字']};
    border: 1px solid {c['边框']};
}}
/* 下拉按钮：单独一块底色 + 左侧分隔线，一眼能看出"点这里展开"。
   箭头本身（三角形）由 控件样式.py 自绘：::down-arrow 一写规则就轮不到它了。 */
QComboBox::drop-down {{
    subcontrol-origin: border;
    subcontrol-position: top right;
    width: {宽度_下拉按钮}px;
    border: none;
    border-left: 1px solid {c['边框']};
    border-top-right-radius: 4px;
    border-bottom-right-radius: 4px;
    background-color: {c['卡片']};
}}
QComboBox::drop-down:hover {{
    background-color: {c['导航悬停']};
}}
QComboBox::drop-down:on {{
    background-color: {c['选中背景']};
}}
/* 展开箭头：一个清清楚楚的折角箭头，比原来大一圈，悬停/展开时变强调色。
   注意：Qt 只要给 QComboBox 写了样式表，就**不会**再让基样式去画这个箭头
   （实测 baseStyle()->drawPrimitive(PE_IndicatorArrowDown) 调用次数为 0），
   所以这里用运行时生成的图片，而不是像勾选框那样自绘。 */
{箭头规则}
/* 展开后的列表：行更高、圆角、悬停与选中都醒目 */
QComboBox QAbstractItemView {{
    background-color: {c['卡片']};
    color: {c['文字']};
    border: 1px solid {c['强调']};
    border-radius: 6px;
    padding: 4px;
    selection-background-color: {c['选中背景']};
    selection-color: {c['选中文字']};
    outline: none;
}}
QComboBox QAbstractItemView::item {{
    min-height: 26px;
    padding: 3px 10px;
    border-radius: 4px;
}}
QComboBox QAbstractItemView::item:hover {{
    background-color: {c['导航悬停']};
    color: {c['文字']};
}}
QComboBox QAbstractItemView::item:selected {{
    background-color: {c['选中背景']};
    color: {c['选中文字']};
}}

/* ---------------- 按钮 ---------------- */
QPushButton {{
    background-color: {c['卡片']};
    color: {c['文字']};
    border: 1px solid {c['边框']};
    border-radius: 4px;
    padding: 6px 14px;
    min-height: 18px;
}}
QPushButton:hover {{
    background-color: {c['悬停']};
    border: 1px solid {c['强调']};
}}
QPushButton:pressed {{
    background-color: {c['选中背景']};
    color: {c['选中文字']};
}}
QPushButton:disabled {{
    background-color: {c['禁用背景']};
    color: {c['禁用文字']};
    border: 1px solid {c['边框']};
}}
QPushButton:checked {{
    background-color: {c['强调']};
    color: {c['背景']};
    border: 1px solid {c['强调']};
}}
QPushButton:default {{
    border: 1px solid {c['强调']};
}}

/* ---------------- 工具按钮 ---------------- */
QToolButton {{
    background-color: transparent;
    color: {c['文字']};
    border: 1px solid transparent;
    border-radius: 4px;
    padding: 4px 8px;
}}
QToolButton:hover {{
    background-color: {c['悬停']};
}}
QToolButton:pressed {{
    background-color: {c['选中背景']};
    color: {c['选中文字']};
}}
QToolButton::menu-indicator {{
    image: none;
}}

/* ---------------- 进度条 ---------------- */
QProgressBar {{
    background-color: {c['卡片']};
    color: {c['文字']};
    border: 1px solid {c['边框']};
    border-radius: 4px;
    text-align: center;
    min-height: 18px;
}}
QProgressBar::chunk {{
    background-color: {c['强调']};
    border-radius: 3px;
}}

/* ---------------- 复选框 / 单选框 ---------------- */
/* 勾选框/单选框的**方框与勾**由 控件样式.py 自绘（QSS 画勾需要图片资源）：
   这里只定文字与间距；一旦写了 ::indicator 规则，Qt 就会自己画那个子控件，
   自绘的"空框 + 勾"（不填充色块）就轮不上了。 */
QCheckBox, QRadioButton {{
    color: {c['文字']};
    spacing: 8px;
}}
QCheckBox:disabled, QRadioButton:disabled {{
    color: {c['次要文字']};
}}

/* ---------------- 分组框 ---------------- */
QGroupBox {{
    background-color: transparent;
    color: {c['文字']};
    font-weight: bold;
    border: 1px solid {c['边框']};
    border-radius: 6px;
    margin-top: 12px;
    padding-top: 8px;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 12px;
    padding: 0 6px;
    color: {c['强调']};
    background-color: {c['背景']};
}}

/* ---------------- 菜单 ---------------- */
QMenu {{
    background-color: {c['卡片']};
    color: {c['文字']};
    border: 1px solid {c['边框']};
    border-radius: 4px;
    padding: 4px;
}}
QMenu::item {{
    background: transparent;
    padding: 6px 24px 6px 12px;
    border-radius: 3px;
}}
QMenu::item:selected {{
    background-color: {c['选中背景']};
    color: {c['选中文字']};
}}
QMenu::separator {{
    height: 1px;
    background: {c['边框']};
    margin: 4px 8px;
}}

/* ---------------- 滚动条 ---------------- */
QScrollBar:vertical {{
    background: {c['背景']};
    width: 12px;
    margin: 0;
    border: none;
}}
QScrollBar::handle:vertical {{
    background: {c['边框']};
    min-height: 30px;
    border-radius: 6px;
    margin: 2px;
}}
QScrollBar::handle:vertical:hover {{
    background: {c['强调']};
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0;
    border: none;
}}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
    background: transparent;
}}
QScrollBar:horizontal {{
    background: {c['背景']};
    height: 12px;
    margin: 0;
    border: none;
}}
QScrollBar::handle:horizontal {{
    background: {c['边框']};
    min-width: 30px;
    border-radius: 6px;
    margin: 2px;
}}
QScrollBar::handle:horizontal:hover {{
    background: {c['强调']};
}}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
    width: 0;
    border: none;
}}
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{
    background: transparent;
}}

/* ---------------- 状态栏 ---------------- */
QStatusBar {{
    background-color: {c['卡片']};
    color: {c['文字']};
    border-top: 1px solid {c['边框']};
}}
QStatusBar::item {{
    border: none;
}}

/* ---------------- 分割线 ---------------- */
QSplitter::handle {{
    background-color: {c['边框']};
}}

/* ---------------- 顶部导航栏（V8_3：从左侧竖排改成顶部横排） ---------------- */
QWidget#TopBar {{
    background-color: {c['导航背景']};
    border-radius: 8px;
}}
QScrollArea#TopBarScroll, QScrollArea#TopBarScroll > QWidget > QWidget {{
    background: transparent;
    border: none;
}}
QWidget#TopBar QLabel#NavTitle {{
    color: {c['导航文字']};
    font-size: 12px;
    font-weight: bold;
    padding: 0 4px;
}}
QWidget#TopBar QWidget#TopBarSep {{
    background-color: {c['边框']};
    border: none;
}}
QWidget#TopBar QPushButton {{
    background: transparent;
    color: {c['导航文字']};
    border: none;
    font-size: 13px;
    padding: 6px 12px;
    border-radius: 6px;
    font-weight: bold;
    text-align: center;
}}
QWidget#TopBar QPushButton:hover {{
    background-color: {c['导航悬停']};
}}
QWidget#TopBar QPushButton:pressed {{
    background-color: {c['选中背景']};
    color: {c['选中文字']};
}}
QWidget#TopBar QPushButton#active {{
    background-color: {c['导航激活']};
    color: {c['背景']};
}}
QWidget#TopBar QPushButton[navCloud="true"] {{
    font-size: 12px;
    padding: 6px 10px;
    min-height: {高度_网盘按钮 - 12}px;
}}
/* 顶部栏的横向滚动条也收窄成细条 */
QScrollArea#TopBarScroll QScrollBar:horizontal,
QScrollArea#NavScroll QScrollBar:horizontal {{
    background: transparent;
    height: 6px;
    margin: 0;
    border: none;
}}
QScrollArea#TopBarScroll QScrollBar::handle:horizontal,
QScrollArea#NavScroll QScrollBar::handle:horizontal {{
    background: {c['边框']};
    min-width: 24px;
    border-radius: 3px;
    margin: 0;
}}
QScrollArea#TopBarScroll QScrollBar::handle:horizontal:hover,
QScrollArea#NavScroll QScrollBar::handle:horizontal:hover {{
    background: {c['导航悬停']};
}}
QScrollArea#TopBarScroll QScrollBar::add-line:horizontal,
QScrollArea#TopBarScroll QScrollBar::sub-line:horizontal,
QScrollArea#TopBarScroll QScrollBar::add-page:horizontal,
QScrollArea#TopBarScroll QScrollBar::sub-page:horizontal,
QScrollArea#NavScroll QScrollBar::add-line:horizontal,
QScrollArea#NavScroll QScrollBar::sub-line:horizontal,
QScrollArea#NavScroll QScrollBar::add-page:horizontal,
QScrollArea#NavScroll QScrollBar::sub-page:horizontal {{
    background: transparent;
    width: 0;
}}

/* ---------------- 左侧功能导航栏（传输/播放/敏感词/AI/日志/设置 竖排） ---------------- */
QWidget#LeftNav {{
    background-color: {c['导航背景']};
    border-radius: 8px;
}}
QWidget#LeftNav QLabel#NavTitle {{
    color: {c['导航文字']};
    font-size: 12px;
    font-weight: bold;
    padding: 0 2px;
}}
QWidget#LeftNav QPushButton {{
    background: transparent;
    color: {c['导航文字']};
    border: none;
    font-size: 12px;
    padding: 8px 4px;
    min-height: {高度_功能按钮 - 16}px;
    border-radius: 6px;
    font-weight: bold;
    text-align: center;
}}
QWidget#LeftNav QPushButton:hover {{
    background-color: {c['导航悬停']};
}}
QWidget#LeftNav QPushButton:pressed {{
    background-color: {c['选中背景']};
    color: {c['选中文字']};
}}
QWidget#LeftNav QPushButton#active {{
    background-color: {c['导航激活']};
    color: {c['背景']};
}}

/* ---------------- AI 页内页签（AI状态 / AI设置 / 模型商店） ---------------- */
QPushButton#AITab {{
    background: transparent;
    color: {c['文字']};
    border: 1px solid {c['边框']};
    border-radius: 6px;
    padding: 5px 14px;
    font-size: 13px;
    font-weight: bold;
}}
QPushButton#AITab:hover {{
    background-color: {c['导航悬停']};
    color: {c['导航文字']};
}}
QPushButton#AITab:checked {{
    background-color: {c['强调']};
    color: {c['背景']};
    border: 1px solid {c['强调']};
}}

/* ---------------- 导航面板（旧版左侧竖排；保留以免旧控件没样式） ---------------- */
QWidget#NavPanel {{
    background-color: {c['导航背景']};
    border-radius: 8px;
}}
QWidget#NavPanel QPushButton {{
    background: transparent;
    color: {c['导航文字']};
    border: none;
    font-size: 14px;
    padding: 12px 6px;
    min-height: {高度_功能按钮 - 24}px;   /* 12px 上下内边距 ×2，总高 = {高度_功能按钮}px */
    border-radius: 6px;
    font-weight: bold;
    text-align: center;
}}
QWidget#NavPanel QPushButton:hover {{
    background-color: {c['导航悬停']};
}}
QWidget#NavPanel QPushButton:pressed {{
    background-color: {c['选中背景']};
    color: {c['选中文字']};
}}
QWidget#NavPanel QPushButton#active {{
    background-color: {c['导航激活']};
    color: {c['背景']};
}}

/* ---------------- 主页路径标签 ---------------- */
QLabel#PathLabel {{
    background-color: {c['卡片']};
    color: {c['文字']};
    border: 1px solid {c['边框']};
    border-radius: 4px;
    padding: 6px 10px;
}}

/* ---------------- 主要按钮 ---------------- */
QPushButton#PrimaryButton {{
    background-color: {c['强调']};
    color: {c['背景']};
    border: 1px solid {c['强调']};
    font-weight: bold;
}}
QPushButton#PrimaryButton:hover {{
    background-color: {c['强调悬停']};
    border: 1px solid {c['强调悬停']};
}}

QPushButton#SuccessButton {{
    background-color: {c['成功']};
    color: {c['背景']};
    border: 1px solid {c['成功']};
    font-weight: bold;
}}

QPushButton#WarningButton {{
    background-color: {c['警告']};
    color: {c['背景']};
    border: 1px solid {c['警告']};
    font-weight: bold;
}}

QPushButton#ErrorButton {{
    background-color: {c['错误']};
    color: {c['背景']};
    border: 1px solid {c['错误']};
    font-weight: bold;
}}

/* ---------------- QToolTip ---------------- */
QToolTip {{
    background-color: {c['卡片']};
    color: {c['文字']};
    border: 1px solid {c['边框']};
    border-radius: 4px;
    padding: 4px 8px;
}}

/* ---------------- V8_3：网盘按钮（顶部横排；名称比 V8 长，字号收小） ---------------- */
QWidget#TopBar QPushButton[navCloud="true"]:hover {{
    background-color: {c['导航悬停']};
}}
QWidget#NavPanel QPushButton[navCloud="true"] {{
    font-size: 11px;
    padding: 6px 2px;
    min-height: {高度_网盘按钮 - 12}px;   /* 6px 上下内边距 ×2，总高 = {高度_网盘按钮}px */
    font-weight: bold;
}}

/* ---------------- V8_3：左侧网盘导航滚动区 ---------------- */
QWidget#NavPanel QScrollArea, QWidget#NavScroll {{
    background: transparent;
    border: none;
}}
QWidget#NavPanel QScrollArea > QWidget > QWidget {{
    background: transparent;
}}
/* 左侧栏只有 74px 宽：滚动条收窄成 6px 半透明细条，别再挤占按钮的位置 */
QScrollArea#NavScroll QScrollBar:vertical {{
    background: transparent;
    width: 6px;
    margin: 0;
    border: none;
}}
QScrollArea#NavScroll QScrollBar::handle:vertical {{
    background: {c['边框']};
    min-height: 24px;
    border-radius: 3px;
    margin: 0;
}}
QScrollArea#NavScroll QScrollBar::handle:vertical:hover {{
    background: {c['导航悬停']};
}}
QScrollArea#NavScroll QScrollBar::add-line:vertical,
QScrollArea#NavScroll QScrollBar::sub-line:vertical,
QScrollArea#NavScroll QScrollBar::add-page:vertical,
QScrollArea#NavScroll QScrollBar::sub-page:vertical {{
    background: transparent;
    height: 0;
}}
QLabel#NavTitle {{
    color: {c['次要文字']};
    font-size: 11px;
    padding: 2px 0;
}}

/* ---------------- V8_3：可滚动页面（AI 页：控件保持高度，装不下就整页滚动） ---------------- */
QScrollArea#PageScroll, QWidget#PageScroll {{
    background: transparent;
    border: none;
}}
QScrollArea#PageScroll > QWidget > QWidget {{
    background: transparent;
}}
QScrollArea#PageScroll QScrollBar:vertical {{
    background: transparent;
    width: 10px;
    margin: 0;
    border: none;
}}
QScrollArea#PageScroll QScrollBar::handle:vertical {{
    background: {c['边框']};
    min-height: 30px;
    border-radius: 5px;
    margin: 2px;
}}
QScrollArea#PageScroll QScrollBar::handle:vertical:hover {{
    background: {c['导航悬停']};
}}
QScrollArea#PageScroll QScrollBar::add-line:vertical,
QScrollArea#PageScroll QScrollBar::sub-line:vertical,
QScrollArea#PageScroll QScrollBar::add-page:vertical,
QScrollArea#PageScroll QScrollBar::sub-page:vertical {{
    background: transparent;
    height: 0;
}}

/* ---------------- V8_3：左下网盘管理面板 ---------------- */
QWidget#ManagePanel {{
    background-color: {c['导航背景']};
    border: 1px solid {c['边框']};
    border-radius: 8px;
}}
QWidget#ManagePanel QPushButton {{
    background-color: {c['卡片']};
    color: {c['导航文字']};
    border: 1px solid {c['边框']};
    border-radius: 6px;
    font-size: 12px;
    font-weight: bold;
    padding: 8px 2px;
    min-height: {高度_管理按钮 - 16}px;   /* 8px 上下内边距 ×2，总高 = {高度_管理按钮}px */
}}
QWidget#ManagePanel QPushButton:hover {{
    background-color: {c['导航悬停']};
    border: 1px solid {c['强调']};
}}
QWidget#ManagePanel QPushButton#DeleteButton {{
    color: {c['错误']};
    border: 1px solid {c['错误']};
}}
QWidget#ManagePanel QPushButton:disabled {{
    background-color: {c['禁用背景']};
    color: {c['禁用文字']};
    border: 1px solid {c['边框']};
}}

/* ---------------- V8_3：网盘页管理区 ---------------- */
QGroupBox#CloudManageBox QPushButton {{
    min-height: 16px;
    padding: 5px 10px;
}}
QLabel#CloudState {{
    color: {c['文字']};
    background-color: {c['卡片']};
    border: 1px solid {c['边框']};
    border-radius: 4px;
    padding: 6px 10px;
}}
"""
