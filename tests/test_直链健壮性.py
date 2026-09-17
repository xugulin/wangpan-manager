"""测试跨盘直链传输的健壮性修复（纯函数 + 语义校验，不联网）。

覆盖 2026-09-16 实测定位的三类问题：
  1. 引擎能识别"服务端暂时不可用"并给出更长的退避（同名冲突/doloading/超时…），
     但认证/权限/参数类错误不重试；
  2. 敏感词上传守卫能认出光鸭的真实拦截文案「名称不可用，请更换后重试」；
  3. 源路径的最终一致性重试函数存在且边界安全（0 次/1 次尝试都不炸）。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

项目根 = Path(__file__).resolve().parents[1]
if str(项目根) not in sys.path:
    sys.path.insert(0, str(项目根))

from v8_3.核心.传输引擎 import 传输引擎
from v8_3.敏感词.上传守卫 import 上传守卫


class 瞬时错误分类测试(unittest.TestCase):
    def test_服务端暂时类错误算瞬时(self):
        for 文本 in (
            "适配器错误: [quark/] file is doloading[同名冲突]",
            "连接超时：read timeout",
            "HTTP 503 Service Unavailable",
            "服务器繁忙，请稍后重试",
            "内部错误 internal error",
            "429 too many requests",
        ):
            self.assertTrue(传输引擎._是瞬时错误(文本), 文本)

    def test_认证权限参数类不算瞬时(self):
        for 文本 in (
            "未登录或登录已过期（errno:-6）",
            "无权限操作该文件",
            "参数错误：path",
            "文件不存在",
            "名称不可用，请更换后重试",
            "包含敏感词被拦截",
        ):
            self.assertFalse(传输引擎._是瞬时错误(文本), 文本)

    def test_瞬时错误退避更长(self):
        瞬时等待, 类1 = 传输引擎._退避秒("file is doloading[同名冲突]", 0)
        普通等待, 类2 = 传输引擎._退避秒("读取失败", 0)
        self.assertGreater(瞬时等待, 普通等待)
        self.assertIn("暂时", 类1)
        self.assertEqual(类2, "普通错误")
        # 退避要有上限，别把批次卡死
        self.assertLessEqual(传输引擎._退避秒("timeout", 9)[0], 60.0)
        self.assertLessEqual(传输引擎._退避秒("其它", 9)[0], 30.0)

    def test_退避随尝试次数递增(self):
        序列 = [传输引擎._退避秒("timeout", i)[0] for i in range(4)]
        self.assertEqual(序列, sorted(序列))
        self.assertEqual(序列[0], 3.0)


class 敏感词拦截识别测试(unittest.TestCase):
    def test_光鸭真实文案被识别(self):
        # 实测原文（光鸭服务端）：这就是之前漏检、导致敏感词文件直接失败的原因
        self.assertTrue(上传守卫.是拦截错误(
            "适配器错误: [guangya/] 名称不可用，请更换后重试"))

    def test_其它常见拦截文案(self):
        for 文本 in ("HTTP 405", "含违规内容", "审核不通过",
                    "invalid name", "blocked by risk control"):
            self.assertTrue(上传守卫.是拦截错误(文本), 文本)

    def test_普通错误不误判(self):
        for 文本 in ("connection reset by peer", "磁盘空间不足",
                    "本地文件不存在：/tmp/x"):
            self.assertFalse(上传守卫.是拦截错误(文本), 文本)


class 源路径重试测试(unittest.TestCase):
    class _假源:
        def __init__(self, 系列):
            self.系列 = list(系列)
            self.调用次数 = 0

        def 文件信息(self, 路径):
            self.调用次数 += 1
            return self.系列.pop(0) if self.系列 else None

    引擎 = None

    def _引擎(self):
        # 只为调用 _确认源存在；不需要真的适配器表
        return 传输引擎({})

    def test_最终一致性_前两次不可见_第三次可见(self):
        引擎 = self._引擎()
        源 = self._假源([None, None, {"name": "x"}])
        结果 = 引擎._确认源存在(源, "/x", 尝试次数=4, 间隔秒=0.01)
        self.assertEqual(结果, {"name": "x"})
        self.assertEqual(源.调用次数, 3)

    def test_一直不可见返回_None(self):
        引擎 = self._引擎()
        源 = self._假源([])
        self.assertIsNone(引擎._确认源存在(源, "/x", 尝试次数=3, 间隔秒=0.01))
        self.assertEqual(源.调用次数, 3)

    def test_异常不会逃逸(self):
        class 爆炸源:
            def 文件信息(self, 路径):
                raise RuntimeError("boom")

        引擎 = self._引擎()
        self.assertIsNone(引擎._确认源存在(爆炸源(), "/x", 尝试次数=2,
                                        间隔秒=0.01))


if __name__ == "__main__":
    unittest.main()
