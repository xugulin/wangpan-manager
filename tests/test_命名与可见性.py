"""目标盘命名规避 + 上传后可见性确认的单测（2026-09-16）。

对应六方向矩阵实测暴露的三个问题：
  ① 百度不允许 `\\ / : * ? " < > |` 与不可见字符，emoji 让接口直接报错 → 上传前规避；
  ② 上传接口返回成功后，文件在列目录里可能 1–3 秒后才出现 → 跨盘传输的
     "扫描源"会漏文件（实测夸克 12MB 文件） → 上传后确认可见；
  ③ 服务端自己也会改名（夸克把引号存成 `&#39;`/`&quot;`） → 回传实际落盘名。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

项目根 = Path(__file__).resolve().parents[1]
if str(项目根) not in sys.path:
    sys.path.insert(0, str(项目根))
if str(项目根 / "v8_3" / "桥") not in sys.path:
    sys.path.insert(0, str(项目根 / "v8_3" / "桥"))

from v8_3.敏感词.命名规避 import 取限制, 需要规避, 规避名称
from 后端_基类 import 名称等价, 确认可见


class 命名规避测试(unittest.TestCase):
    def test_百度非法字符改全角(self):
        新, 说明 = 规避名称("baidu", '引号\'单"双.txt')
        self.assertNotIn('"', 新)
        self.assertIn("＂", 新)
        self.assertIn("不允许的字符", 说明)
        for 坏 in ('a?b.txt', 'a*b.txt', 'a:b.txt', 'a|b.txt', 'a<b>c.txt',
                  "a\\b.txt"):
            需, _ = 需要规避("baidu", 坏)
            self.assertTrue(需, 坏)
            新名, _ = 规避名称("baidu", 坏)
            self.assertFalse(any(ch in '\\/:*?"<>|' for ch in 新名), 新名)

    def test_百度不可见字符(self):
        需, 原因 = 需要规避("baidu", "换行\n名字.txt")
        self.assertTrue(需)
        self.assertIn("不可见", 原因)
        新, _ = 规避名称("baidu", "换行\n名字.txt")
        self.assertNotIn("\n", 新)
        self.assertNotIn("\t", 规避名称("baidu", "制表\t符.txt")[0])

    def test_百度emoji被替换且名字不为空(self):
        新, 说明 = 规避名称("baidu", "emoji🎬🦆.mp4")
        self.assertTrue(all(ord(ch) <= 0xFFFF for ch in 新), 新)
        self.assertIn("emoji", 新)
        self.assertIn("星平面", 说明)
        只有emoji, _ = 规避名称("baidu", "😀.txt")
        self.assertTrue(只有emoji.strip(), "名字不能变成空")
        self.assertTrue(只有emoji.endswith(".txt"), 只有emoji)

    def test_夸克光鸭不动名字(self):
        for 名 in ("emoji🎬🦆.mp4", '引号\'单"双.txt', "换行\n名字.txt"):
            for 盘 in ("quark", "guangya"):
                需, _ = 需要规避(盘, 名)
                self.assertFalse(需, f"{盘} 不该改名：{名}")
                self.assertEqual(规避名称(盘, 名)[0], 名)

    def test_路径里的斜杠不算名字非法(self):
        # 传整路径时只看文件名部分，避免把目录分隔符当非法字符
        需, _ = 需要规避("baidu", "深/层/普通.txt")
        self.assertFalse(需)

    def test_未知网盘不改名(self):
        需, _ = 需要规避("weird_drive", 'a"b.txt')
        self.assertFalse(需)
        self.assertEqual(取限制("weird_drive")["非法字符"], "")


class 可见性确认测试(unittest.TestCase):
    class 条目:
        def __init__(self, name):
            self.name = name

    def test_服务端实体改名也能匹配(self):
        self.assertTrue(名称等价("引号&#39;单&quot;双.txt", '引号\'单"双.txt'))
        self.assertFalse(名称等价("a.txt", "b.txt"))

    def test_前两次不可见_第三次返回实际名(self):
        序列 = [[], [], [self.条目("x.txt")]]
        次数 = {"n": 0}

        def 列():
            次数["n"] += 1
            return 序列.pop(0) if 序列 else []

        实际 = 确认可见(列, "x.txt", 尝试=5, 间隔=0.01)
        self.assertEqual(实际, "x.txt")
        self.assertEqual(次数["n"], 3)

    def test_返回服务端改名后的实际名(self):
        def 列():
            return [self.条目("引号&#39;单.txt")]

        实际 = 确认可见(列, "引号'单.txt", 尝试=3, 间隔=0.01)
        self.assertEqual(实际, "引号&#39;单.txt")

    def test_一直不可见不抛错(self):
        def 列():
            return []

        self.assertIsNone(确认可见(列, "x.txt", 尝试=2, 间隔=0.01))

    def test_列目录抛错不炸(self):
        def 列():
            raise RuntimeError("boom")

        self.assertIsNone(确认可见(列, "x.txt", 尝试=2, 间隔=0.01))


if __name__ == "__main__":
    unittest.main()
