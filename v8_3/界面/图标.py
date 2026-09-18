# v8_3/界面/图标.py
"""本地图形标：给"本地小模型市场"的每个模型生成一张小图（离线也能画）。

用户要求"要显示模型的图片"。做法**不抓网图**：

* 不联网 → 断网/内网也能看到图；
* 不引入第三方商标 → 各家模型的官方 logo 有商标与版权问题，
  所以我们画的是**自家风格的"文字标"**：家族色 + 家族首字母 + 参数量角标，
  一眼能区分 DeepSeek / Qwen / Gemma / Llama…；
* 缓存到 ``数据/图标/模型/<键>.png``，第二次直接读盘。

对外只有一个函数：:func:`模型图`（返回 ``QPixmap``，失败返回空 pixmap）。
"""
from __future__ import annotations

from pathlib import Path

__all__ = ["模型图", "家族配色", "图标目录", "清除缓存"]

#: 家族 → (底色, 前景色, 字)
家族配色: dict[str, tuple[str, str, str]] = {
    "deepseek": ("#2f5fd0", "#ffffff", "DS"),
    "deepseek-r1": ("#2f5fd0", "#ffffff", "DS"),
    "qwen": ("#7b4bd6", "#ffffff", "Q"),
    "qwen2.5": ("#7b4bd6", "#ffffff", "Q"),
    "qwen3": ("#6a3fd0", "#ffffff", "Q3"),
    "qwen3.5": ("#5a2fc8", "#ffffff", "Q3.5"),
    "gemma": ("#1a73e8", "#ffffff", "G"),
    "gemma3": ("#1a73e8", "#ffffff", "G3"),
    "llama": ("#0b6b52", "#ffffff", "L"),
    "llama3.2": ("#0b6b52", "#ffffff", "L3"),
    "phi": ("#0f6cbd", "#ffffff", "Φ"),
    "phi4": ("#0f6cbd", "#ffffff", "Φ4"),
    "granite": ("#3b4a5a", "#ffffff", "GR"),
    "nemotron": ("#76b900", "#0b1a00", "NV"),
    "ornith": ("#b45f06", "#ffffff", "OR"),
    "mistral": ("#e07b00", "#ffffff", "M"),
    "gpt-oss": ("#10a37f", "#ffffff", "OSS"),
    "minimax": ("#c2185b", "#ffffff", "MM"),
    "glm": ("#1565c0", "#ffffff", "GLM"),
    "kimi": ("#4a148c", "#ffffff", "K"),
}

默认配色 = ("#546e7a", "#ffffff", "AI")


def 图标目录() -> Path:
    根 = Path(__file__).resolve().parents[2] / "数据" / "图标" / "模型"
    return 根


def _配色(键: str) -> tuple[str, str, str]:
    键 = str(键 or "").strip()
    小写 = 键.lower()
    if 小写 in 家族配色:
        return 家族配色[小写]
    for 名, 色 in 家族配色.items():
        if 小写.startswith(名):
            return 色
    return 默认配色


def 模型图(键: str, 名称: str = "", 尺寸: int = 96, 角标: str = ""):
    """生成/读取一张模型图标。

    :param 键: 家族键（如 ``qwen3.5``、``deepseek-r1``）
    :param 名称: 图里没字时用的兜底名
    :param 尺寸: 像素边长
    :param 角标: 右下角小字（一般放参数量，如 ``4B``）
    """
    try:
        from PySide6.QtCore import QRectF, Qt
        from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPen, QPixmap
    except Exception:  # pragma: no cover - 没有 Qt 时界面本来也用不了
        return None

    底色, 前景, 字 = _配色(键 or 名称)
    if not 字 or 字 == "AI":
        字 = (str(名称 or 键 or "AI").strip()[:2] or "AI").upper()
    缓存 = 图标目录() / f"{键 or '默认'}_{尺寸}_{角标}.png"
    if 缓存.is_file():
        try:
            图 = QPixmap(str(缓存))
            if not 图.isNull():
                return 图
        except Exception:
            pass

    图 = QPixmap(尺寸, 尺寸)
    图.fill(Qt.transparent)
    画 = QPainter(图)
    try:
        画.setRenderHint(QPainter.Antialiasing, True)
        圆角 = 尺寸 * 0.22
        画.setBrush(QBrush(QColor(底色)))
        画.setPen(QPen(QColor(255, 255, 255, 40), max(1.0, 尺寸 * 0.02)))
        画.drawRoundedRect(QRectF(1, 1, 尺寸 - 2, 尺寸 - 2), 圆角, 圆角)
        # 主字
        字号 = int(尺寸 * (0.46 if len(字) <= 2 else 0.34))
        字体 = QFont()
        字体.setBold(True)
        字体.setPixelSize(max(8, 字号))
        画.setFont(字体)
        画.setPen(QColor(前景))
        文字区 = QRectF(0, 0, 尺寸, 尺寸 * (0.86 if 角标 else 1.0))
        画.drawText(文字区, Qt.AlignCenter, 字)
        # 参数量角标
        if 角标:
            小字 = QFont()
            小字.setBold(True)
            小字.setPixelSize(max(7, int(尺寸 * 0.19)))
            画.setFont(小字)
            画.setPen(QColor(前景))
            画.drawText(QRectF(0, 尺寸 * 0.62, 尺寸, 尺寸 * 0.32),
                       Qt.AlignCenter, str(角标))
    finally:
        画.end()
    try:
        缓存.parent.mkdir(parents=True, exist_ok=True)
        图.save(str(缓存), "PNG")
    except Exception:
        pass
    return 图


def 清除缓存() -> None:
    try:
        for 文件 in 图标目录().glob("*.png"):
            文件.unlink(missing_ok=True)
    except Exception:
        pass
