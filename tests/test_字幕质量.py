"""AI 输出质量过滤单测：本地小模型交出来的"垃圾摘要"必须被挡住。

用例全部来自**真机实测**（本地 deepseek-r1:1.5b，CPU）：
``总结`` 的"摘要"字段出现过两种直接显示给用户的垃圾：

1. **提示词回声**：把格式示例原样抄回来 —— ``整体内容，150 字以内``；
2. **JSON 片段**：把整个对象塞进摘要 —— ``"摘要": "视频讲的是…"``。

这类内容比"没有摘要"更糟：用户以为 AI 认真总结了。所以判定为不可用时，
改用规则摘要（截断正文），章节/标签只要是好的就保留，而不是整次结果丢掉。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

项目根 = Path(__file__).resolve().parents[1]
if str(项目根) not in sys.path:
    sys.path.insert(0, str(项目根))

from v8_3.AI.字幕助手 import _摘要不可用


class 摘要质量测试(unittest.TestCase):
    def test_提示词回声被拒(self):
        for 文本 in ("整体内容，150 字以内",
                    "章节 2~6 个，标签 3~6 个",
                    "只输出 JSON，不要解释、不要 Markdown 代码块",
                    "你是视频内容分析助手。根据带时间戳的字幕"):
            with self.subTest(文本=文本[:16]):
                self.assertTrue(_摘要不可用(文本), f"没挡住提示词回声：{文本}")

    def test_JSON片段被拒(self):
        for 文本 in ('"摘要": "视频讲的是翻译"',
                    '{"摘要":"x","章节":[]}',
                    '关键词": "a", "b": "c',
                    '[{"标题":"开场"}]'):
            with self.subTest(文本=文本[:16]):
                self.assertTrue(_摘要不可用(文本), f"没挡住 JSON 片段：{文本}")

    def test_太短太长被拒(self):
        self.assertTrue(_摘要不可用(""))
        self.assertTrue(_摘要不可用("好"))
        self.assertTrue(_摘要不可用("很好"))
        self.assertTrue(_摘要不可用("这段文字" * 200))

    def test_正常摘要放行(self):
        for 文本 in ("这部片子讲了如何用本地模型翻译字幕，并演示了章节跳转。",
                    "电影《沙丘》讲述保罗的成长与复仇，画面宏大，节奏偏慢。",
                    "视频演示了 4K HEVC 在 CPU 软解下的解码余量与丢帧情况。"):
            with self.subTest(文本=文本[:16]):
                self.assertEqual(_摘要不可用(文本), "", f"误伤了正常摘要：{文本}")

    def test_返回值是可读原因(self):
        self.assertIn("提示词", _摘要不可用("整体内容，150 字以内"))
        self.assertIn("JSON", _摘要不可用('{"摘要":"x"}'))


class 总结降级测试(unittest.TestCase):
    """载荷里摘要不可用时：换规则摘要，但**保留** AI 给的章节与标签。"""

    def _助手(self):
        from v8_3.AI.字幕助手 import 字幕助手
        return 字幕助手(AI助手=None, 日志回调=None, 本地模型=None)

    def _条目(self):
        from v8_3.AI.字幕助手 import 字幕条目
        return [字幕条目(开始秒=0.0, 结束秒=2.0, 文本="开场介绍主角"),
                字幕条目(开始秒=2.0, 结束秒=4.0, 文本="主角踏上旅程"),
                字幕条目(开始秒=4.0, 结束秒=6.0, 文本="结局揭晓真相")]

    def test_坏摘要换成规则摘要且保留章节(self):
        助手 = self._助手()
        载荷 = {"摘要": "整体内容，150 字以内",
                "章节": [{"时间": "00:00:02", "标题": "出发", "要点": "踏上旅程"}],
                "标签": ["冒险", "成长"]}
        结果 = 助手._解析总结载荷(载荷, self._条目())
        self.assertIsNotNone(结果, "章节是好的，不该整条丢掉")
        self.assertNotIn("字以内", 结果["摘要"], "提示词回声必须被换掉")
        self.assertTrue(结果["摘要"], "应换成规则摘要而不是空")
        self.assertEqual([c["标题"] for c in 结果["章节"]], ["出发"])
        self.assertEqual(结果["标签"], ["冒险", "成长"])
        self.assertIn("规则摘要", 结果.get("说明", ""))

    def test_好的摘要原样保留(self):
        助手 = self._助手()
        载荷 = {"摘要": "本片讲述主角从平凡到承担责任的成长故事。",
                "章节": [{"时间": "00:00:02", "标题": "出发", "要点": "踏上旅程"}],
                "标签": ["成长"]}
        结果 = 助手._解析总结载荷(载荷, self._条目())
        self.assertEqual(结果["摘要"], "本片讲述主角从平凡到承担责任的成长故事。")
        self.assertNotIn("说明", 结果)

    def test_摘要与章节都空返回None(self):
        助手 = self._助手()
        self.assertIsNone(助手._解析总结载荷({"摘要": "", "章节": []}, self._条目()))
        self.assertIsNone(助手._解析总结载荷("不是字典", self._条目()))

    def test_超长摘要被截断(self):
        助手 = self._助手()
        长 = "这是一段很长的摘要。" * 100
        结果 = 助手._解析总结载荷({"摘要": 长, "章节": [],
                             "标签": []}, self._条目())
        if 结果 is not None:
            self.assertLessEqual(len(结果["摘要"]), 601)


if __name__ == "__main__":
    unittest.main(verbosity=2)
