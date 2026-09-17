"""JSON 修复单测：本地小模型输出不规矩时的四级兜底。

这些用例全部来自**实测**：本地 deepseek-r1:1.5b 在 CPU 上跑一次 40~50 秒，
交出来的东西却经常是"围栏包着 + 前面一段推理 + 撞 token 上限被截断"。
如果解析不了，那几十秒推理就白烧，用户还看到一句"不是 JSON"。

用例清单
========
* 裸 JSON / ```json 围栏 / 无语言围栏；
* 推理在前（推理里还带花括号）——只从第一个 ``{`` 修会修出废物；
* 全角花括号（中文输入法）；
* **截断**：实测诊断输出停在 ``"起播等待秒":``；
* 纯文字 → 必须返回 ``None``（不能瞎编一个对象出来）；
* 一段话里多个对象 → 按"期望键"挑对的那个。
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

项目根 = Path(__file__).resolve().parents[1]
if str(项目根) not in sys.path:
    sys.path.insert(0, str(项目根))

from v8_3.AI.JSON修复 import 去代码块, 修补截断JSON, 提取JSON对象

真 = ('{"需要调整":true,"动作":["加大网络缓存到 6000ms"],'
     '"新参数":{"网络缓存毫秒":6000,"起播等待秒":2.6}}')

#: 实测日志里那段被截断的诊断输出（撞 token 上限）
实测截断 = ('```{"需要调整":true,"动作":["加大网络缓存到 6000ms",'
         '"起播等待秒":2.6,"新参数":{"网络缓存毫秒":6000,"起播等待秒":')


class 提取JSON对象测试(unittest.TestCase):
    def test_裸JSON(self):
        self.assertEqual(提取JSON对象(真)["新参数"]["网络缓存毫秒"], 6000)

    def test_代码围栏(self):
        for 文本 in (f"```json\n{真}\n```", f"```\n{真}\n```",
                    f"```JSON\n{真}\n```"):
            with self.subTest(文本=文本[:12]):
                得 = 提取JSON对象(文本)
                self.assertIsNotNone(得, "围栏不该让解析失败")
                self.assertEqual(得["需要调整"], True)

    def test_推理在前(self):
        文本 = f"先想一下 {{随便}} 然后给结论：\n{真}"
        得 = 提取JSON对象(文本, ("需要调整",))
        self.assertIsNotNone(得, "前面有带花括号的推理时也要抠出真对象")
        self.assertIn("新参数", 得)

    def test_全角花括号(self):
        得 = 提取JSON对象(真.replace("{", "｛").replace("}", "｝"))
        self.assertIsNotNone(得)
        self.assertEqual(得["需要调整"], True)

    def test_截断实测样本能救回关键字段(self):
        得 = 提取JSON对象(实测截断, ("需要调整",))
        self.assertIsNotNone(得, "撞 token 上限的截断输出必须能救回来")
        self.assertIn("需要调整", 得)
        self.assertIs(得["需要调整"], True)

    def test_纯文字返回None(self):
        for 文本 in ("", "   ", "这句话里没有对象", "[]", "null", None):
            with self.subTest(文本=repr(文本)):
                self.assertIsNone(提取JSON对象(文本))

    def test_多个对象按期望键挑(self):
        文本 = ('{"x":1} 还有 {"需要调整":false,"理由":"ok","动作":[]}')
        得 = 提取JSON对象(文本, ("需要调整",))
        self.assertEqual(得["理由"], "ok")
        # 不给期望键时取字段最多的（一般是真正的参数对象）
        self.assertEqual(提取JSON对象(文本)["理由"], "ok")

    def test_数组对象也能被扫到(self):
        得 = 提取JSON对象('前面的话 {"档位":"1080p","缓存":8000} 后面的话')
        self.assertEqual(得["档位"], "1080p")

    def test_与json模块一致(self):
        """完整 JSON 的结果必须和标准库完全一致（不能修歪）。"""
        得 = 提取JSON对象(真)
        self.assertEqual(得, json.loads(真))


class 修补截断JSON测试(unittest.TestCase):
    def test_补上未闭合的括号(self):
        得 = 修补截断JSON('{"a":1,"b":{"c":2,"d":')
        self.assertEqual(得, {"a": 1})

    def test_丢掉不完整的键值对(self):
        得 = 修补截断JSON('{"摘要":"很好","标签":["悬疑","动作"')
        self.assertIsNotNone(得)
        self.assertEqual(得["摘要"], "很好")

    def test_字符串里有花括号也不乱(self):
        得 = 修补截断JSON('{"文本":"这里有个 { 花括号","数":1,"尾巴":')
        self.assertIsNotNone(得)
        self.assertEqual(得["数"], 1)

    def test_转义引号不打断扫描(self):
        得 = 修补截断JSON('{"文本":"他说 \\"你好\\" 了","数":2,"尾巴":')
        self.assertIsNotNone(得)
        self.assertEqual(得["数"], 2)

    def test_完整对象直接返回(self):
        self.assertEqual(修补截断JSON(真), json.loads(真))

    def test_没有对象返回None(self):
        for 文本 in ("", "没有花括号", "[]"):
            self.assertIsNone(修补截断JSON(文本))

    def test_中英混排标签(self):
        得 = 修补截断JSON('{"标签":["悬疑","科幻"],"评分":9,"缺":')
        self.assertEqual(得["标签"], ["悬疑", "科幻"])


class 去代码块测试(unittest.TestCase):
    def test_剥围栏(self):
        self.assertEqual(去代码块("```json\n{\"a\":1}\n```"), '{"a":1}')

    def test_多个代码块优先给含花括号的(self):
        文本 = "```\n说明文字\n```\n```json\n{\"a\":1}\n```"
        self.assertIn("{", 去代码块(文本))

    def test_没有围栏原样返回(self):
        self.assertEqual(去代码块("{\"a\":1}"), '{"a":1}')

    def test_截断的围栏(self):
        self.assertIn("{", 去代码块("```json\n{\"a\":1,"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
