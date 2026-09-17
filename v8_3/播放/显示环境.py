"""显示环境策略：**libvlc 只能往 X11 窗口里画**，这里负责把运行环境摆正。

为什么必须专门处理（实测崩溃）
==============================
libvlc 3 嵌入播放只有 ``libvlc_media_player_set_xwindow(窗口号)`` 这一条路，
它要的是 **X11 window id**。而在 Wayland 会话里：

* Qt 默认走 ``wayland`` 平台插件，``widget.winId()`` 拿到的是 **wl_surface**
  相关的东西，**不是 X11 窗口号**；
* 把这个数交给 ``set_xwindow``，VLC 会拿它当 X11 窗口去画 → X 错误 /
  直接 **段错误闪退**（用户实测：网盘里双击 4K 视频开播瞬间整个 GUI 崩掉）。

所以：
1. **启动时**（创建 QApplication 之前）如果发现会话是 Wayland 且存在 XWayland
   （``DISPLAY`` 有值），就把 ``QT_QPA_PLATFORM`` 切成 ``xcb`` —— 这样窗口有真正的
   X11 id，嵌入播放正常，用户无感（XWayland 本来就是 Wayland 桌面的标配）；
2. **兜底闸**：真到了 Wayland 且没有 XWayland 的情况，:func:`可嵌入窗口` 返回假，
   播放器一律用"无窗口模式"（句柄 0）解码：能出声能播，但看不到画面，并且会
   明确告诉用户原因 —— 绝不把无效句柄递给 libvlc。
"""

from __future__ import annotations

import os
from typing import Optional

__all__ = ["可嵌入窗口", "准备嵌入显示", "显示说明", "禁用强制X11",
           "允许VLC自带窗口", "是桌面平台", "无窗口原因"]

#: 这些 Qt 平台插件下 ``winId()`` **不是** X11 窗口号，不能交给 libvlc
不可嵌入平台 = ("wayland", "wayland-egl", "wayland-brcm", "offscreen",
           "minimal", "minimalegl", "vnc", "linuxfb", "eglfs",
           "vkkhrdisplay", "qnx")

#: 无头/离屏平台：这些是**刻意**选的（自检、CI、无人值守），句柄给 0 是对的
无头平台 = ("offscreen", "minimal", "minimalegl", "vnc", "linuxfb", "eglfs",
         "vkkhrdisplay", "qnx")


def 是桌面平台(平台名: str = "") -> bool:
    """是不是"有人在看"的桌面平台（Wayland 桌面也算）。

    为什么要区分：离屏/无头时把句柄给 0 是**正确**做法（无窗口解码）；
    但在 Wayland 桌面上给 0，libvlc 会**自己开一个 VLC 窗口**放视频 ——
    那个窗口不归我们管，可能比屏幕还大（用户实测："窗口过大超出屏幕"，
    标题栏写着 VLC media player）。
    """
    名 = str(平台名 or "").strip().lower().split(":", 1)[0]
    if not 名:
        try:
            from PySide6.QtGui import QGuiApplication
            名 = str(QGuiApplication.platformName() or "").lower()
        except Exception:  # noqa: BLE001
            return False
    return 名 not in 无头平台


def 允许VLC自带窗口() -> bool:
    """用户是否**显式**同意"让 VLC 自己开窗口放"（默认不同意）。"""
    return str(os.environ.get("V8_3_允许VLC自带窗口") or "").strip().lower() in (
        "1", "true", "yes", "on", "是")


def 无窗口原因(平台名: str = "") -> str:
    """不能嵌窗口时，给用户一段"为什么 + 怎么办"。"""
    名 = str(平台名 or "").strip() or "?"
    return (f"当前 Qt 平台是 {名}，它的窗口号不是 X11 窗口号，libvlc 没法把画面嵌进来。\n"
          "直接开播的话，VLC 会自己弹一个**我们控制不了的窗口**（可能比屏幕还大）。\n"
          "解决办法：用 XWayland（X11）启动 —— 设置环境变量 QT_QPA_PLATFORM=xcb 后重启；\n"
          "或者设 V8_3_允许VLC自带窗口=1（明确同意用 VLC 自带窗口，界面里的控件仍可用）。")


def 禁用强制X11() -> bool:
    """用户是否明确要求不要动 ``QT_QPA_PLATFORM``（排查问题时用）。"""
    return bool(os.environ.get("V8_3_不强制X11"))


def 可嵌入窗口(平台名: str = "") -> bool:
    """当前 Qt 平台的 ``winId()`` 能不能当 X11 窗口用。"""
    名 = str(平台名 or "").strip().lower()
    if not 名:
        try:
            from PySide6.QtGui import QGuiApplication
            名 = str(QGuiApplication.platformName() or "").lower()
        except Exception:  # noqa: BLE001
            return False
    # 带参数的平台名（xcb:...）只取冒号前那段
    return 名.split(":", 1)[0] not in 不可嵌入平台


def 准备嵌入显示(日志回调=None) -> dict:
    """创建 QApplication **之前**调用：决定用哪个 Qt 平台插件。

    返回 ``{"平台": 最终值, "改了": bool, "可嵌入": bool, "说明": str}``。
    """
    现在 = str(os.environ.get("QT_QPA_PLATFORM") or "").strip()
    候选们 = [x.strip().lower() for x in 现在.split(";") if x.strip()]
    有X = bool(os.environ.get("DISPLAY"))
    有Wayland = bool(os.environ.get("WAYLAND_DISPLAY"))
    结果 = {"平台": 现在, "改了": False, "可嵌入": True, "说明": ""}

    def _写(说明: str):
        结果["说明"] = 说明
        if 日志回调 is not None:
            try:
                日志回调(说明)
            except Exception:  # noqa: BLE001
                pass

    if 禁用强制X11():
        _写("[显示] 已按 V8_3_不强制X11 保留当前平台设置"
          f"（QT_QPA_PLATFORM={现在 or '未设置'}）")
        结果["可嵌入"] = 可嵌入窗口(候选们[0] if 候选们 else "")
        return 结果

    # 已经指定了单一平台
    if len(候选们) == 1 and 候选们[0] not in ("", "wayland;xcb"):
        目标 = 候选们[0]
        # ⚠️ 关键分支：Wayland 系平台即使被**显式**指定，只要有 XWayland（DISPLAY）
        # 也必须切到 xcb —— 否则 libvlc 会自己开一个我们管不了的窗口
        # （用户实测：标题栏 "VLC media player"、比屏幕还大、嵌不进我们的界面）
        if 目标.startswith("wayland") and 有X:
            改前 = 现在 or 目标
            os.environ["QT_QPA_PLATFORM"] = "xcb"
            结果.update({"平台": "xcb", "改了": True, "可嵌入": True})
            _写(f"[显示] QT_QPA_PLATFORM {改前} → xcb：libvlc 只能往 X11 窗口画。"
              "Wayland 下它会自己弹一个不受控的窗口（可能超出屏幕）。"
              "XWayland 已就绪，切过去用户无感。")
            return 结果
        结果["可嵌入"] = 可嵌入窗口(目标)
        if not 结果["可嵌入"]:
            if 是桌面平台(目标):
                _写(f"[显示] 平台 {目标} 没有 X11 窗口号：桌面环境下直接开播会让"
                  "VLC 自己弹一个不受控的窗口（可能超出屏幕）。"
                  "想让画面嵌进本程序：用 XWayland 启动（QT_QPA_PLATFORM=xcb）。")
            else:
                _写(f"[显示] 平台 {目标} 没有 X11 窗口号：（无头/离屏，"
                  "按无窗口模式解码，正常）")
        else:
            _写(f"[显示] 平台 {目标}：窗口可直接嵌给 libvlc")
        return 结果

    # 需要抉择：只要存在 X（X11 会话，或 Wayland 会话里的 XWayland），就用 xcb
    if 有X:
        改前 = 现在 or "（未设置）"
        os.environ["QT_QPA_PLATFORM"] = "xcb"
        结果.update({"平台": "xcb", "改了": True, "可嵌入": True})
        _写(f"[显示] QT_QPA_PLATFORM {改前} → xcb：libvlc 只能往 X11 窗口画，"
          f"Wayland 的窗口号不能用（否则播放会闪退）。"
          f"当前会话：{'X11' if not 有Wayland else 'Wayland + XWayland(X11)'}")
        return 结果

    # 只有 Wayland、没有 XWayland：没法嵌窗口，如实说明
    结果["可嵌入"] = False
    _写("[显示] 当前只有 Wayland 且没有 XWayland（DISPLAY 为空）："
      "libvlc 无法嵌窗口，播放会用无窗口模式（有声音、没画面）。"
      "解决办法：装/开启 XWayland 后重启本程序。")
    return 结果


def 显示说明() -> str:
    """给界面显示的一行环境说明（播放页/AI 日志用）。"""
    try:
        from PySide6.QtGui import QGuiApplication
        平台 = str(QGuiApplication.platformName() or "")
    except Exception:  # noqa: BLE001
        平台 = os.environ.get("QT_QPA_PLATFORM") or "?"
    if 可嵌入窗口(平台):
        return f"显示环境正常（{平台}）：视频嵌在窗口里播放"
    return (f"⚠️ 当前 Qt 平台是 {平台}，没有 X11 窗口号：播放将无画面。"
          "请用 X11/XWayland（QT_QPA_PLATFORM=xcb）启动。")
