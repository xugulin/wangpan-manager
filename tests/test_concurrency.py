"""测试自适应并发控制器。"""

from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path

项目根 = Path(__file__).resolve().parents[1]
if str(项目根) not in sys.path:
    sys.path.insert(0, str(项目根))

from v8_3.核心.并发 import 并发控制器, 吞吐自适应器


class 并发测试(unittest.TestCase):
    def test_上限与阻塞(self):
        控制器 = 并发控制器(2, 最小=1, 最大=4)
        self.assertEqual(控制器.上限, 2)
        控制器.获取()
        控制器.获取()
        self.assertEqual(控制器.使用中, 2)
        控制器.调整(3)
        self.assertEqual(控制器.上限, 3)
        控制器.获取()
        self.assertEqual(控制器.使用中, 3)
        控制器.释放()
        控制器.释放()
        控制器.释放()
        self.assertEqual(控制器.使用中, 0)

    def test_自适应降并发(self):
        控制器 = 并发控制器(10, 1, 32)
        自适应 = 吞吐自适应器(控制器, 日志=None, 最小上调间隔=0.0)
        自适应.观测(0, 0, 100)
        time.sleep(0.01)
        自适应.观测(1_000_000, 3, 100)  # 失败 +3 → 至少降 3
        self.assertLess(控制器.上限, 10)


if __name__ == "__main__":
    unittest.main()
