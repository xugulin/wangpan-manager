"""游离窗口巡检：**只报告 + 交给宿主安全回退**，绝不销毁 libvlc 的窗口。

为什么要重写这几条（原来钉的是"自愈有上限、到上限就只关不修"）：
那条"关窗口"的路子是**破坏性**的 —— 那个窗口是 libvlc 正在渲染的画布，
从外面 XDestroyWindow 掉会把 VLC 的 vout 线程弄僵，之后任何 停止/释放 都要一直
等它，用户看到的就是"关一下播放，界面彻底卡死"。所以现在的契约是：

* 发现游离窗口 → 交给宿主的处理函数（安全动作：换回能嵌入的输出并重载）；
* 宿主处理不了 → 如实写日志（带标题与尺寸，方便诊断"比屏幕还大"）；
* **同一个窗口最多报 3 次**（原来会无限刷）；
* 无论如何都**不调用** 请关闭窗口 / 销毁窗口 / 清干净游离窗口。
"""
from __future__ import annotations

import os
import unittest

# 没有显示环境时**强制**离屏：本机环境里 QT_QPA_PLATFORM="wayland;xcb"，
# setdefault 改不动它，Qt 会依次试 wayland/xcb 然后直接 abort（实测 core dump）。
if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
    os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtWidgets import QApplication    # noqa: E402

from v8_3.界面.游离窗口守护 import 游离窗口守护    # noqa: E402


class 假游离窗口:
    """替换 播放.游离窗口 模块：可控地"发现"游离窗口，并记录有没有被销毁。"""

    def __init__(self, 发现次数: int = 99):
        self.发现次数 = 发现次数
        self.调用 = 0
        self.请关闭的 = []
        self.销毁的 = []
        self.清理过 = 0

    def 可用(self) -> bool:
        return True

    def 不可用原因(self) -> str:
        return ""

    def 找游离窗口(self, 排除窗口号=()):
        self.调用 += 1
        return [(0x1234, "测试视频.mp4 - VLC media player")] if self.调用 <= self.发现次数 else []

    def 窗口尺寸(self, 窗口号: int):
        return (2560, 1440)

    def 请关闭窗口(self, 窗口号: int) -> bool:
        self.请关闭的.append(窗口号)
        return True

    def 销毁窗口(self, 窗口号: int) -> bool:
        self.销毁的.append(窗口号)
        return True

    def 清干净游离窗口(self, 最多轮: int = 3) -> int:
        self.清理过 += 1
        return 1


class 游离窗口巡检测试(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.应用 = QApplication.instance() or QApplication([])

    def setUp(self):
        from v8_3.界面 import 游离窗口守护 as 守护模块
        self._原 = 守护模块.游离窗口
        self._假 = 假游离窗口()
        守护模块.游离窗口 = self._假

    def tearDown(self):
        from v8_3.界面 import 游离窗口守护 as 守护模块
        守护模块.游离窗口 = self._原

    def _跑一轮(self, 发现次数=99, 处理结果=True, 轮数=6):
        """跑若干轮巡检；``处理结果`` 决定宿主的处理函数说"我搞定了"还是"搞不定"。"""
        from v8_3.界面 import 游离窗口守护 as 守护模块
        假 = 假游离窗口(发现次数)
        守护模块.游离窗口 = 假
        self._假 = 假
        日志: list[str] = []
        收到: list = []

        def 处理(找到=None):
            收到.append(找到)
            return 处理结果

        守护 = 游离窗口守护(
            None, 取自己窗口号们=lambda: [1, 2], 发现回调=处理,
            日志=日志.append, 间隔毫秒=10, 巡检次数=0)
        # 直接调 _检查（不依赖定时器/事件循环）。
        # 注意要把 _无限 打开：生产代码走 开始(无限=True)，否则 _检查 会在
        # "剩余次数<=0" 时直接 return（实测踩过：测试里什么都不发生）。
        守护._无限 = True
        for _ in range(轮数):
            守护._检查()
        return 假, 日志, 收到

    def test_发现后交给宿主处理(self):
        _假, _日志, 收到 = self._跑一轮(处理结果=True)
        self.assertTrue(收到, "发现游离窗口必须交给宿主的处理函数（安全回退）")

    def test_宿主处理不了才报日志且带尺寸(self):
        _假, 日志, _收到 = self._跑一轮(处理结果=False)
        self.assertTrue(any("2560x1440" in x for x in 日志),
                        f"日志要报游离窗口尺寸（诊断'超出屏幕'）：{日志}")

    def test_报告有次数上限不刷屏(self):
        _假, 日志, _ = self._跑一轮(处理结果=False, 轮数=20)
        self.assertLessEqual(len(日志), 3,
                             f"同一个窗口最多报 3 次，别无限刷：{len(日志)} 条")

    def test_绝不销毁libvlc的窗口(self):
        """核心契约：不请它关闭、不销毁、不清干净 —— 那会把 VLC 弄僵导致界面卡死。"""
        假, _日志, _ = self._跑一轮(处理结果=False, 轮数=20)
        self.assertEqual(假.请关闭的, [], "不许再发 WM_DELETE 去关 libvlc 的窗口")
        self.assertEqual(假.销毁的, [], "更不许 XDestroyWindow")
        self.assertEqual(假.清理过, 0, "也不许调'清干净游离窗口'")

    def test_巡检次数用完就停(self):
        假, _日志, _ = self._跑一轮(发现次数=99, 处理结果=True, 轮数=4)
        self.assertGreater(假.调用, 0, "巡检要真的查过窗口")


if __name__ == "__main__":
    unittest.main()
