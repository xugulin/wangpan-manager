"""传输任务表格的纯逻辑单测：分类映射、过滤、排序、展示格式。

（UI 交互由 `工具/界面自检.py` 的 [25] 段覆盖；这里只测不依赖 Qt 的部分，
所以跑得飞快，适合每次改动都跑。）
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

项目根 = Path(__file__).resolve().parents[1]
if str(项目根) not in sys.path:
    sys.path.insert(0, str(项目根))

from v8_3.界面.任务表格 import (分类定义, 在分类里, 取分类, 排序键, 统计分类,
                            时长文本, 时间文本, 大小文本)


def 任务(状态: str, **额外):
    return {"任务ID": 状态, "状态": 状态, **额外}


class 分类映射测试(unittest.TestCase):
    def test_引擎中文状态(self):
        对应 = {"下载": "进行中", "上传": "进行中", "枚举": "进行中",
              "等待": "等待", "完成": "已完成", "失败": "失败",
              "跳过": "跳过", "暂停": "暂停", "已取消": "失败"}
        for 状态, 期望 in 对应.items():
            self.assertEqual(取分类(任务(状态)), 期望, 状态)

    def test_英文状态也认(self):
        对应 = {"downloading": "进行中", "uploading": "进行中",
              "pending": "等待", "done": "已完成", "failed": "失败",
              "skipped": "跳过", "paused": "暂停", "cancelled": "失败"}
        for 状态, 期望 in 对应.items():
            self.assertEqual(取分类(任务(状态)), 期望, 状态)

    def test_未知状态按进行中(self):
        self.assertEqual(取分类(任务("莫名其妙")), "进行中")

    def test_八个分类齐全(self):
        self.assertEqual([k for k, _, _ in 分类定义],
                        ["全部", "进行中", "已完成", "等待", "暂停", "跳过",
                         "失败", "未完成"])


class 过滤与计数测试(unittest.TestCase):
    台账 = {
        "a": 任务("进行中", 大小=10, 已传输=5),
        "b": 任务("等待", 大小=10),
        "c": 任务("暂停", 大小=10, 已传输=2),
        "d": 任务("已完成", 大小=10, 已传输=10),
        "e": 任务("失败", 大小=10, 错误="boom"),
        "f": 任务("跳过", 大小=10, 跳过原因="目标已存在"),
    }

    def test_全部(self):
        self.assertEqual(len([t for t in self.台账.values()
                          if 在分类里(t, "全部")]), 6)

    def test_各分类命中数(self):
        for 分类, 期望 in (("进行中", 1), ("等待", 1), ("暂停", 1),
                       ("已完成", 1), ("失败", 1), ("跳过", 1),
                       ("未完成", 3)):
            命中 = [t for t in self.台账.values() if 在分类里(t, 分类)]
            self.assertEqual(len(命中), 期望, 分类)

    def test_失败不算未完成(self):
        # 与分类标签语义一致：未完成 = 跑着 / 排队 / 暂停
        self.assertFalse(在分类里(self.台账["e"], "未完成"))

    def test_统计分类(self):
        计数 = 统计分类(self.台账)
        self.assertEqual(计数["全部"], 6)
        self.assertEqual(计数["未完成"], 3)
        self.assertEqual(计数["已完成"], 1)
        self.assertEqual(sum(v for k, v in 计数.items()
                         if k not in ("全部", "未完成")), 6)

    def test_统计分类也接受列表(self):
        计数 = 统计分类(list(self.台账.values()))
        self.assertEqual(计数["全部"], 6)


class 排序测试(unittest.TestCase):
    def test_进行中排最前_已完成最后(self):
        列表 = [任务("已完成", 结束=1), 任务("失败", 结束=2),
              任务("进行中"), 任务("等待"), 任务("暂停")]
        有序 = [取分类(t) for t in sorted(列表, key=排序键)]
        self.assertEqual(有序[0], "进行中")
        self.assertEqual(有序[-1], "已完成")
        self.assertLess(有序.index("失败"), 有序.index("等待"))

    def test_同分类按结束时间倒序(self):
        新 = 任务("已完成", 结束=200)
        旧 = 任务("已完成", 结束=100)
        self.assertEqual([取分类(t) for t in sorted([旧, 新], key=排序键)],
                        ["已完成", "已完成"])
        self.assertIs(sorted([旧, 新], key=排序键)[0], 新)


class 格式测试(unittest.TestCase):
    def test_时间文本(self):
        self.assertEqual(时间文本(0), "")
        self.assertEqual(时间文本(None), "")
        self.assertTrue(时间文本(1789529760).count(":") == 2)

    def test_时长文本(self):
        self.assertEqual(时长文本(0), "-")
        self.assertEqual(时长文本(2.5), "2.5s")
        self.assertEqual(时长文本(90), "1m30s")
        self.assertEqual(时长文本(3700), "1h01m")

    def test_大小文本(self):
        self.assertEqual(大小文本(0), "0 B")
        self.assertEqual(大小文本(1536), "1.5 KiB")
        self.assertEqual(大小文本(12 * 1024 ** 2), "12.0 MiB")


if __name__ == "__main__":
    unittest.main()
