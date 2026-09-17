"""目标同名语义单测（2026-09-16 明确化）。

规则：
  ① 同名 + 大小一致            → 跳过（"目标已存在"）
  ② 同名 + 大小不同（覆盖关）   → 跳过，并说明两边大小（不静默覆盖用户文件）
  ③ 同名 + 大小不同（覆盖开）   → 删旧传新，目标内容与源一致
  ④ 同名是文件夹（覆盖关）      → 跳过，原因写明"目标同名是文件夹"
  ⑤ 同名是文件夹（覆盖开）      → 删掉同名文件夹后上传成功
  ⑥ 源大小未知（0）            → 保守按已存在处理，原因写明
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

项目根 = Path(__file__).resolve().parents[1]
if str(项目根) not in sys.path:
    sys.path.insert(0, str(项目根))

from tests.test_engine import 本地假适配器
from v8_3.核心.传输引擎 import 传输引擎, 传输请求, 任务状态


class 同名语义测试(unittest.TestCase):
    def setUp(self):
        self.根 = Path(tempfile.mkdtemp(prefix="v8_3_同名_"))
        self.源根 = self.根 / "源"
        self.目标根 = self.根 / "目标"
        self.源根.mkdir(parents=True)
        self.目标根.mkdir(parents=True)
        self.源 = 本地假适配器(self.源根)
        self.目标 = 本地假适配器(self.目标根)
        self.引擎 = 传输引擎({"源": self.源, "目标": self.目标})
        (self.源根 / "同名.txt").write_bytes(b"A" * 300)

    def tearDown(self):
        shutil.rmtree(self.根, ignore_errors=True)

    def _传(self, 覆盖: bool = False):
        事件: list[dict] = []
        请求 = 传输请求(源网盘="源", 源路径="/同名.txt",
                    目标网盘="目标", 目标路径="/同名.txt",
                    覆盖=覆盖, 重试次数=0)
        统计 = self.引擎.传输(请求, 事件回调=lambda e: 事件.append(e.to_dict()))
        任务 = None
        for d in 事件:
            if d.get("type") == "任务跳过":
                任务 = d.get("task") or {}
        return 统计, 任务, 事件

    def test_同名大小一致_跳过(self):
        (self.目标根 / "同名.txt").write_bytes(b"A" * 300)
        统计, 任务, _ = self._传()
        self.assertEqual(统计.跳过, 1)
        self.assertEqual(统计.已完成, 0)
        self.assertEqual(任务["skip_reason"], "目标已存在")

    def test_同名大小不同_不静默跳过(self):
        (self.目标根 / "同名.txt").write_bytes(b"A" * 150)
        统计, 任务, _ = self._传()
        self.assertEqual(统计.跳过, 1)
        原因 = 任务["skip_reason"]
        self.assertIn("同名但大小不同", 原因)
        self.assertIn("150", 原因)
        self.assertIn("300", 原因)
        self.assertIn("覆盖", 原因)
        # 关键：没有把用户的文件改掉
        self.assertEqual((self.目标根 / "同名.txt").stat().st_size, 150)

    def test_同名大小不同_覆盖开则替换(self):
        (self.目标根 / "同名.txt").write_bytes(b"A" * 150)
        统计, _, _ = self._传(覆盖=True)
        self.assertEqual(统计.已完成, 1)
        self.assertEqual(统计.跳过, 0)
        self.assertEqual((self.目标根 / "同名.txt").read_bytes(), b"A" * 300)

    def test_同名是文件夹_跳过并说明(self):
        (self.目标根 / "同名.txt").mkdir()
        统计, 任务, _ = self._传()
        self.assertEqual(统计.跳过, 1)
        self.assertEqual(任务["skip_reason"], "目标同名是文件夹")
        self.assertTrue((self.目标根 / "同名.txt").is_dir(), "不应破坏目录")

    def test_同名是文件夹_覆盖开则替换为文件(self):
        (self.目标根 / "同名.txt").mkdir()
        (self.目标根 / "同名.txt" / "里面的东西.txt").write_bytes(b"x")
        统计, _, _ = self._传(覆盖=True)
        self.assertEqual(统计.已完成, 1)
        目标 = self.目标根 / "同名.txt"
        self.assertTrue(目标.is_file())
        self.assertEqual(目标.read_bytes(), b"A" * 300)

    def test_源大小未知时保守跳过(self):
        (self.目标根 / "同名.txt").write_bytes(b"A" * 10)
        事件: list[dict] = []

        class 无大小适配器(本地假适配器):
            """模拟"源端没给出大小"（有些网盘列目录不带 size）。"""

            def 列目录(self, 路径="/"):
                条目 = super().列目录(路径)
                for x in 条目:
                    x.size = 0
                return 条目

            def 文件信息(self, 路径):
                条目 = super().文件信息(路径)
                if 条目 is not None:
                    条目.size = 0
                return 条目

        源 = 无大小适配器(self.源根)
        引擎 = 传输引擎({"源": 源, "目标": self.目标})
        统计 = 引擎.传输(传输请求(源网盘="源", 源路径="/同名.txt",
                              目标网盘="目标", 目标路径="/同名.txt",
                              重试次数=0),
                      事件回调=lambda e: 事件.append(e.to_dict()))
        self.assertEqual(统计.跳过, 1)
        原因 = [ (d.get("task") or {}).get("skip_reason") for d in 事件
                if d.get("type") == "任务跳过"][0]
        self.assertIn("源大小未知", 原因)

    def test_覆盖开关不影响不存在的情况(self):
        统计, _, _ = self._传(覆盖=True)
        self.assertEqual(统计.已完成, 1)
        self.assertEqual((self.目标根 / "同名.txt").read_bytes(), b"A" * 300)


if __name__ == "__main__":
    unittest.main()
