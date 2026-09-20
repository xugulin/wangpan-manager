"""窗口就绪判断：X11 下必须"真的在屏幕上"才算就绪（这是游离 VLC 窗口的根因）。

没有 X11（离屏/无头）时，判断要直接放行 —— 否则会把起播卡死。
"""
from __future__ import annotations

import os
import unittest

# 没有显示环境时**强制**离屏：本机环境里 QT_QPA_PLATFORM="wayland;xcb"，
# setdefault 改不动它，Qt 会依次试 wayland/xcb 然后直接 abort（实测 core dump）。
if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
    os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtWidgets import QApplication, QWidget    # noqa: E402

from v8_3.界面 import 窗口就绪 as 就绪模块                # noqa: E402


class 窗口就绪测试(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.应用 = QApplication.instance() or QApplication([])

    def test_非X11平台直接放行(self):
        """非 X11（离屏/无头/Wayland）没有"嵌入"这回事，必须返回 True。

        在 X11 会话里跑这条就换一个"没有原生窗口"的假部件来验同一件事
        （否则真窗口没 show 本来就不该就绪，断言会跟另一条打架）。
        """
        if 就绪模块.可嵌入(None):
            class 无原生窗口:
                def isVisible(self):
                    return True

                def winId(self):
                    raise RuntimeError("没有原生窗口")

                def windowHandle(self):
                    return None

            假 = 无原生窗口()
        else:
            假 = QWidget()
        self.assertTrue(就绪模块.窗口就绪(假, 重试上限=0),
                        "非 X11 平台应直接判定就绪")

    def test_没show的窗口不算就绪(self):
        if not 就绪模块.可嵌入(None):
            self.skipTest("当前没有 X11（离屏），这条只在真显示下有意义")
        假 = QWidget()
        假.resize(300, 200)
        self.assertFalse(就绪模块.窗口就绪(假, 重试上限=0),
                         "还没 show 的窗口不该算就绪")

    def test_show之后算就绪(self):
        if not 就绪模块.可嵌入(None):
            self.skipTest("当前没有 X11（离屏），这条只在真显示下有意义")
        假 = QWidget()
        假.resize(300, 200)
        假.show()
        self.assertTrue(就绪模块.窗口就绪(假, 重试上限=20, 每次毫秒=20),
                        "show 之后（X 已映射）应判定就绪")
        假.close()

    def test_拿不到窗口号时放行(self):
        """部件拿不到 X 窗口号（比如还没创建原生窗口）时不能卡住。"""
        class 怪:
            def isVisible(self):
                return True

            def winId(self):
                raise RuntimeError("没有原生窗口")

            def windowHandle(self):
                return None

        self.assertTrue(就绪模块.窗口就绪(怪(), 重试上限=1, 每次毫秒=1))


if __name__ == "__main__":
    unittest.main()
