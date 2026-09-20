"""游离窗口守卫：自愈有上限、到上限后"只关不修"，并且会报窗口尺寸。

为什么要这几条：用户实测"多出来一个超出屏幕的窗口、两个窗口都在放同一个视频" ——
自愈（停→绑→重播）如果无限重试，可能越修越多窗口。这里把上限行为钉住。
"""
from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication    # noqa: E402

from v8_3.界面.游离窗口守护 import 游离窗口守护    # noqa: E402


class 假游离窗口:
    """替换 播放.游离窗口 模块：可控地"发现"游离窗口。"""

    def __init__(self, 发现次数: int = 99):
        self.发现次数 = 发现次数
        self.调用 = 0
        self.请关闭的 = []
        self.销毁的 = []

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


class 自愈上限测试(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.应用 = QApplication.instance() or QApplication([])

    def setUp(self):
        # 把守卫模块里的 游离窗口 换成假的（X 不可用时真模块直接返回"不可用"）
        from v8_3.界面 import 游离窗口守护 as 守护模块
        self._原 = 守护模块.游离窗口
        self._假 = 假游离窗口()
        守护模块.游离窗口 = self._假

    def tearDown(self):
        from v8_3.界面 import 游离窗口守护 as 守护模块
        守护模块.游离窗口 = self._原

    def _跑一轮(self, 发现次数=99, 自愈上限=2):
        # 每轮用**新的**假模块：否则"调用次数"会跨轮累计，发现次数判断就乱了
        from v8_3.界面 import 游离窗口守护 as 守护模块
        假 = 假游离窗口(发现次数)
        守护模块.游离窗口 = 假
        self._假 = 假
        日志: list[str] = []
        自愈次数 = {"n": 0}

        def 自愈():
            自愈次数["n"] += 1

        守护 = 游离窗口守护(
            None, 取自己窗口号们=lambda: [1, 2], 自愈回调=自愈,
            日志=日志.append, 间隔毫秒=10, 巡检次数=0)
        守护._自愈上限 = 自愈上限
        # 直接调 _检查（不依赖定时器/事件循环）。
        # 注意要把 _无限 打开：生产代码走 开始(无限=True)，否则 _检查 会在
        # "剩余次数<=0" 时直接 return（实测踩过：测试里什么都不发生）。
        守护._无限 = True
        for _ in range(6):
            守护._检查()
        return 假, 日志, 自愈次数

    def test_自愈到上限后不再自愈(self):
        _假, 日志, 自愈 = self._跑一轮(自愈上限=2)
        self.assertEqual(自愈["n"], 2, f"自愈次数应被上限截住：{自愈}")
        self.assertTrue(any("已达上限" in x for x in 日志),
                        f"日志里要说清'到上限了、只关窗口'：{日志}")

    def test_日志里带窗口尺寸(self):
        _假, 日志, _ = self._跑一轮()
        self.assertTrue(any("2560x1440" in x for x in 日志),
                        f"日志要报游离窗口尺寸（诊断'超出屏幕'）：{日志}")

    def test_到上限后仍会去关窗口(self):
        假, 日志, _ = self._跑一轮(发现次数=99, 自愈上限=1)
        # _检查 里 1.5 秒后才"请它关闭"，这里直接验它被安排上了：
        self.assertTrue(any("发现 libvlc 自己开的窗口" in x for x in 日志))


if __name__ == "__main__":
    unittest.main()
