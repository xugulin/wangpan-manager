#!/usr/bin/env python3
"""真机（Windows / Linux）播放冒烟测试：**画面到底在不在我们的窗口里**。

为什么要有它（用户实测的 Windows 问题）
========================================
Windows 版能播了，但"视频游离在 GUI 之外播放" —— 也就是 libvlc 自己开了一个
顶层窗口放画面，我们的播放页一片黑。这类问题**只有真机跑真播放**才能发现，
所以这个脚本在 **GitHub Actions 的 Windows runner** 上用发布包里的真软件跑一遍：

1. 用应用**自己的**代码路径建窗口（``播放出口`` + ``播放会话``，跟播放页一模一样）；
2. 播一段随包附带的小视频（``工具/测试素材/样片.mp4``，不需要网络）；
3. 等它出画面，然后检查三件事：
   * ``libvlc_media_player_has_vout()`` —— 到底有没有视频输出；
   * ``游离窗口.画面在我们窗口里(句柄)`` —— 画面是不是画在**我们给的那个窗口**里
     （VLC 嵌入成功会在里面建子窗口；自己开窗口时我们那个窗口是空的）；
   * ``游离窗口.找游离窗口(排除我们的窗口)`` —— 有没有多出来的 VLC 顶层窗口；
4. 打印 VLC 自己的日志（``--file-logging``，看它挑了哪个 vout 模块）。

判定：**有画面但不在我们窗口里**（或冒出游离窗口）→ 退出码 1，CI 直接红。
环境本身出不了画面（比如无 GPU 的 CI 会话）→ 只提示、不算失败。

用法（项目根下）::

    运行环境\\python\\pythonw.exe 工具\\测试播放嵌入.py     # Windows
    运行环境/venv/bin/python 工具/测试播放嵌入.py           # Linux
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

项目根 = Path(__file__).resolve().parents[1]
if str(项目根) not in sys.path:
    sys.path.insert(0, str(项目根))

for 流 in (sys.stdout, sys.stderr):
    try:
        流.reconfigure(encoding="utf-8", errors="replace")   # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass

#: 等画面出现最多多少秒
等待秒 = 12.0


def 说(文本: str) -> None:
    print(f"[播放测试] {文本}", flush=True)


def main() -> int:
    if not os.environ.get("DISPLAY") and os.name != "nt":
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    素材 = 项目根 / "工具" / "测试素材" / "样片.mp4"
    if not 素材.is_file():
        (项目根 / "工具" / "测试素材").mkdir(parents=True, exist_ok=True)
        raise SystemExit(f"测试素材不存在：{素材}（发布包应自带）")

    from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget

    from v8_3.播放.播放出口 import 播放出口
    from v8_3.播放.播放核心 import 播放会话
    from v8_3.播放.游离窗口 import (可用 as 窗口枚举可用, 平台说明, 找游离窗口,
                              画面在我们窗口里, 窗口已映射, 窗口尺寸)
    from v8_3.界面.窗口就绪 import 窗口就绪

    日志文件 = Path(tempfile.gettempdir()) / "v8_3_播放测试_vlc.log"
    日志文件.unlink(missing_ok=True)
    # 让 libvlc 把自己的日志写文件：我们才能看到它挑了哪个 vout 模块、
    # 有没有报"embed 失败"。这一步是这次排查的关键证据来源。
    os.environ["V8_3_VLC日志文件"] = str(日志文件)

    应用 = QApplication.instance() or QApplication([])
    说(f"平台 = {应用.platformName()}｜窗口枚举：{平台说明()}｜可用 = {窗口枚举可用()}")
    窗 = QWidget()
    窗.setWindowTitle("播放嵌入测试")
    窗.resize(960, 600)
    布局 = QVBoxLayout(窗)
    区 = QWidget()
    布局.addWidget(区)
    窗.show()

    def 泵(秒: float = 0.2) -> None:
        截止 = time.time() + 秒
        while time.time() < 截止:
            应用.processEvents()
            time.sleep(0.02)

    泵(0.8)
    出口 = 播放出口(控件=区, 日志回调=说)
    句柄 = 出口.句柄()
    就绪 = 窗口就绪(区, 重试上限=40)
    说(f"窗口句柄 = {句柄}｜Qt 句柄 = {int(区.winId())}｜已映射 = "
      f"{窗口已映射(句柄)}｜窗口就绪 = {就绪}｜尺寸 = {窗口尺寸(句柄)}")
    if not 句柄:
        说("✗ 没拿到窗口句柄（这个平台不能嵌入）")
        return 1

    会话 = 播放会话(出口=出口, 取适配器=lambda *_: None, 日志回调=说,
                探测直链开关=False, 探测媒体开关=False, 顾问=None,
                AI决策后台=False)
    会话.直链信息 = {"url": str(素材), "headers": {}}
    if not 会话.起播(句柄):
        说("✗ 起播失败")
        return 1

    播放器 = 会话.播放器
    出画面 = False
    截止 = time.time() + 等待秒
    while time.time() < 截止:
        泵(0.3)
        try:
            if 播放器 and 播放器.是否在播() and 播放器.有画面():
                出画面 = True
                break
        except Exception:  # noqa: BLE001
            pass
    泵(1.0)

    在窗口里 = 画面在我们窗口里(句柄, 排除窗口号=(int(窗.winId()),))
    游离 = 找游离窗口(排除窗口号=(句柄, int(窗.winId())))
    进度 = 0.0
    try:
        进度 = float(播放器.进度秒())
    except Exception:  # noqa: BLE001
        进度 = 0.0
    说(f"播放中：进度 {进度:.1f}s｜出画面 = {出画面}｜画面在我们窗口里 = {在窗口里}"
      f"｜游离窗口 = {游离 or '无'}")
    if 日志文件.is_file():
        关键 = [行.strip() for 行 in 日志文件.read_text(encoding="utf-8",
                                               errors="replace").splitlines()
               if "vout" in 行.lower() or "embed" in 行.lower()]
        说("VLC 日志（vout/embed 相关，最多 12 行）：")
        for 行 in 关键[-12:]:
            说("   " + 行[-160:])
    else:
        说("（没有拿到 VLC 日志文件）")

    会话.关闭()
    窗.close()
    泵(0.3)

    if 游离:
        说("✗ 出现了游离窗口：画面跑到 VLC 自己开的窗口里了")
        return 1
    if not 出画面:
        说("⚠️ 这台机器没能出画面（可能没有可用的视频输出/GPU）—— 不算失败，"
          "但要人工看一眼")
        return 0
    if not 在窗口里:
        # "有画面但不在我们窗口里"正是用户报的那个 bug：必须有画面才算数
        if not 窗口枚举可用():
            说("⚠️ 这台机器不能枚举窗口（无 X11/非 Windows），只能靠 has_vout 判断")
            return 0
        说("✗ 有画面，但不在我们的窗口里（视频游离在 GUI 之外）")
        return 1
    说("✅ 画面确实画在我们的窗口里（没有游离窗口）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
