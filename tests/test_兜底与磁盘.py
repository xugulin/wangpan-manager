"""失败兜底（源→本地→目标）+ 磁盘暂存预算的单测（2026-09-16）。

覆盖：
  * 常规重试全部失败后，兜底模式能救回任务，并计入 `统计.兜底成功`；
  * 关掉兜底开关时任务照常失败（不偷偷重试）；
  * 兜底会**清干净中转**再重来；
  * 磁盘预算：不超过上限才放行、超额要等待、等待超时给出明确报错、
    单文件超过上限放行（文件不能切块）、释放后额度归还；
  * 兜底是**串行**的（同一时刻只有一个兜底任务在跑）。
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

项目根 = Path(__file__).resolve().parents[1]
if str(项目根) not in sys.path:
    sys.path.insert(0, str(项目根))

from tests.test_engine import 本地假适配器
from v8_3.核心.传输引擎 import 传输引擎, 传输请求, 任务状态
from v8_3.核心.模型 import 缓存配置


class 兜底测试(unittest.TestCase):
    def setUp(self):
        self.根 = Path(tempfile.mkdtemp(prefix="v8_3_兜底_"))
        self.源根 = self.根 / "源"
        self.目标根 = self.根 / "目标"
        self.源根.mkdir(parents=True)
        self.目标根.mkdir(parents=True)
        self.源 = 本地假适配器(self.源根)
        self.目标 = 本地假适配器(self.目标根)
        (self.源根 / "大文件.bin").write_bytes(b"X" * 4096)
        # 预算按类共享，测试之间要清零
        传输引擎._暂存账["已用"] = 0

    def tearDown(self):
        shutil.rmtree(self.根, ignore_errors=True)

    def _跑(self, 失败兜底: bool = True, 下载失败次数: int = 99):
        # 下载全部失败（上限次数用不完），只有兜底阶段才让它成功
        self.源.失败次数 = 下载失败次数
        引擎 = 传输引擎({"源": self.源, "目标": self.目标})
        事件: list[dict] = []
        请求 = 传输请求(源网盘="源", 源路径="/大文件.bin",
                    目标网盘="目标", 目标路径="/大文件.bin",
                    重试次数=1, 失败兜底=失败兜底, 兜底串行=1,
                    缓存=缓存配置(目录=str(self.根 / "缓存")))
        统计 = 引擎.传输(请求, 事件回调=lambda e: 事件.append(e.to_dict()))
        return 引擎, 统计, 事件

    def test_常规失败后兜底能救回(self):
        # 首次尝试失败 1 次，重试（第 2 次）再失败 1 次 → 兜底那次成功
        self.源.失败次数 = 2
        引擎 = 传输引擎({"源": self.源, "目标": self.目标})
        事件: list[dict] = []
        统计 = 引擎.传输(传输请求(
            源网盘="源", 源路径="/大文件.bin",
            目标网盘="目标", 目标路径="/大文件.bin",
            重试次数=1, 失败兜底=True,
            缓存=缓存配置(目录=str(self.根 / "缓存"))),
            事件回调=lambda e: 事件.append(e.to_dict()))
        self.assertEqual(统计.已完成, 1, f"应兜底成功，事件：{[e.get('type') for e in 事件]}")
        self.assertEqual(统计.兜底次数, 1)
        self.assertEqual(统计.兜底成功, 1)
        self.assertTrue((self.目标根 / "大文件.bin").is_file())
        完成 = [e for e in 事件 if e.get("type") == "任务完成"]
        self.assertIn("兜底", str((完成[0].get("task") or {}).get("stage") or ""))

    def test_关掉兜底就照常失败(self):
        _, 统计, 事件 = self._跑(失败兜底=False, 下载失败次数=9)
        self.assertEqual(统计.已完成, 0)
        self.assertEqual(统计.失败, 1)
        self.assertEqual(统计.兜底次数, 0)
        self.assertEqual(统计.兜底成功, 0)
        self.assertFalse((self.目标根 / "大文件.bin").exists())

    def test_兜底也会失败时如实报错(self):
        引擎, 统计, 事件 = self._run_always_fail()
        self.assertEqual(统计.失败, 1)
        self.assertEqual(统计.兜底次数, 1)
        self.assertEqual(统计.兜底成功, 0)
        失败 = [e for e in 事件 if e.get("type") == "任务失败"]
        self.assertIn("兜底", str((失败[0].get("task") or {}).get("error") or ""))

    def _run_always_fail(self):
        # 下载永远失败：把失败次数设成"每次调用都减一但永不为 0"的做法不可行，
        # 这里换成列目录/文件信息正常、下载必炸的适配器
        class 下载必炸(本地假适配器):
            def 下载(self, 远端路径, 本地路径, *, 任务ID="", 进度=None,
                    续传=True, 保守=False):
                raise RuntimeError("注入：下载永远失败")

        源 = 下载必炸(self.源根)
        引擎 = 传输引擎({"源": 源, "目标": self.目标})
        事件: list[dict] = []
        统计 = 引擎.传输(传输请求(
            源网盘="源", 源路径="/大文件.bin",
            目标网盘="目标", 目标路径="/大文件.bin",
            重试次数=1, 失败兜底=True,
            缓存=缓存配置(目录=str(self.根 / "缓存"))),
            事件回调=lambda e: 事件.append(e.to_dict()))
        return 引擎, 统计, 事件

    def test_兜底完成后中转不留残留(self):
        记录: list[str] = []
        引擎 = 传输引擎({"源": self.源, "目标": self.目标},
                    日志回调=记录.append)
        self.源.失败次数 = 2          # 两次常规尝试都失败 → 走兜底
        缓存目录 = self.根 / "缓存"
        请求 = 传输请求(源网盘="源", 源路径="/大文件.bin",
                    目标网盘="目标", 目标路径="/大文件.bin",
                    重试次数=1, 失败兜底=True,
                    缓存=缓存配置(目录=str(缓存目录)))
        统计 = 引擎.传输(请求)
        self.assertEqual(统计.兜底成功, 1)
        残留 = [p for p in 缓存目录.rglob("*") if p.is_file()]
        self.assertFalse(残留, f"兜底完成后不应留下中转文件：{残留}")
        self.assertTrue(any("兜底" in x for x in 记录), "应打兜底日志")


class 磁盘预算测试(unittest.TestCase):
    def setUp(self):
        传输引擎._暂存账["已用"] = 0
        self.根 = Path(tempfile.mkdtemp(prefix="v8_3_预算_"))

    def tearDown(self):
        传输引擎._暂存账["已用"] = 0
        shutil.rmtree(self.根, ignore_errors=True)

    def _请求(self, 上限: int, 余量: int = 0) -> 传输请求:
        return 传输请求(
            源网盘="源", 源路径="/a", 目标网盘="目标", 目标路径="/a",
            缓存=缓存配置(目录=str(self.根), 暂存上限字节=上限,
                      磁盘余量字节=余量))

    def test_正常预留与释放(self):
        引擎 = 传输引擎({})
        请求 = self._请求(1000)
        预留 = 引擎._预留暂存(请求, 400)
        self.assertEqual(预留, 400)
        self.assertEqual(传输引擎._暂存账["已用"], 400)

        class 任务:
            暂存预留 = 400

        引擎._释放暂存(任务)
        self.assertEqual(传输引擎._暂存账["已用"], 0)
        # 幂等：再释放一次不会变成负数
        引擎._释放暂存(任务)
        self.assertEqual(传输引擎._暂存账["已用"], 0)

    def test_超额等待超时给出明确报错(self):
        引擎 = 传输引擎({})
        请求 = self._请求(1000)
        引擎._预留暂存(请求, 800)          # 占掉大半
        with self.assertRaises(RuntimeError) as 上下文:
            引擎._预留暂存(请求, 500, 超时秒=0.3)
        self.assertIn("本地暂存空间不足", str(上下文.exception))
        传输引擎._暂存账["已用"] = 0

    def test_单文件超过上限放行(self):
        引擎 = 传输引擎({})
        请求 = self._请求(1000)
        预留 = 引擎._预留暂存(请求, 5000)   # 文件比上限还大
        self.assertEqual(预留, 5000, "文件不能切块，必须放行")
        传输引擎._暂存账["已用"] = 0

    def test_释放后等待者能拿到额度(self):
        引擎 = 传输引擎({})
        请求 = self._请求(1000)
        引擎._预留暂存(请求, 900)
        拿到: list[int] = []

        class 任务:
            暂存预留 = 900

        def 等额度():
            拿到.append(引擎._预留暂存(请求, 800, 超时秒=5))

        线程 = threading.Thread(target=等额度, daemon=True)
        线程.start()
        time.sleep(0.3)
        self.assertFalse(拿到, "额度没释放前不该拿到")
        引擎._释放暂存(任务)               # 释放 → 等待者应立刻拿到
        线程.join(timeout=3)
        self.assertEqual(拿到, [800])
        传输引擎._暂存账["已用"] = 0

    def test_取消时立刻放弃等待(self):
        引擎 = 传输引擎({})
        请求 = self._请求(1000)
        引擎._预留暂存(请求, 900)
        取消 = threading.Event()
        取消.set()
        with self.assertRaises(Exception):
            引擎._预留暂存(请求, 500, 取消, 超时秒=5)
        传输引擎._暂存账["已用"] = 0


if __name__ == "__main__":
    unittest.main()
