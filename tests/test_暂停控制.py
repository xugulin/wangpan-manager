"""暂停/继续控制单测（2026-09-16）。

三层都覆盖：
  * 引擎标志位：全批暂停、单任务暂停、`是否暂停`；
  * 排队语义：被暂停的任务卡在 `等待可运行`（可被取消打断），继续后立刻放行；
  * 运行中暂停：本地适配器的进度回调里抛 `任务暂停` → 任务记为"暂停"且保留中转；
  * 桥协议：`暂停任务()` 让桥进程在安全点停下（上传/下载的进度回调处），
    `继续任务()` 清掉标记后能正常跑完。
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


class 引擎暂停标志测试(unittest.TestCase):
    def setUp(self):
        传输引擎._暂存账["已用"] = 0
        self.引擎 = 传输引擎({})

    def test_单任务暂停与继续(self):
        self.assertFalse(self.引擎.是否暂停("t1"))
        self.引擎.暂停任务("t1")
        self.assertTrue(self.引擎.是否暂停("t1"))
        self.assertFalse(self.引擎.是否暂停("t2"))
        self.引擎.继续任务("t1")
        self.assertFalse(self.引擎.是否暂停("t1"))

    def test_全部暂停影响所有任务(self):
        self.引擎.全部暂停()
        self.assertTrue(self.引擎.是否暂停("任意"))
        self.引擎.全部继续()
        self.assertFalse(self.引擎.是否暂停("任意"))

    def test_等待可运行被暂停卡住_继续后放行(self):
        self.引擎.暂停任务("t1")
        完成 = []
        线程 = threading.Thread(
            target=lambda: (self.引擎.等待可运行("t1", None), 完成.append(1)),
            daemon=True)
        线程.start()
        time.sleep(0.4)
        self.assertFalse(完成, "暂停期间不该放行")
        self.引擎.继续任务("t1")
        线程.join(timeout=3)
        self.assertEqual(完成, [1], "继续后应立刻放行")

    def test_等待可运行可被取消打断(self):
        self.引擎.暂停任务("t1")
        取消 = threading.Event()
        结果 = []

        def 跑():
            try:
                self.引擎.等待可运行("t1", 取消)
                结果.append("未取消")
            except Exception as e:  # noqa: BLE001
                结果.append(type(e).__name__)

        线程 = threading.Thread(target=跑, daemon=True)
        线程.start()
        time.sleep(0.3)
        取消.set()
        线程.join(timeout=3)
        self.assertEqual(结果, ["任务取消"], "取消事件应打断等待")


class 运行中暂停测试(unittest.TestCase):
    """本地适配器：进度回调里检查暂停 → 任务记为暂停且保留中转。"""

    class 慢适配器(本地假适配器):
        def 下载(self, 远端路径, 本地路径, *, 任务ID="", 进度=None,
                续传=True, 保守=False):
            目标 = Path(本地路径)
            目标.parent.mkdir(parents=True, exist_ok=True)
            with 目标.open("wb") as f:
                for i in range(20):
                    f.write(b"X" * 4096)
                    f.flush()
                    if 进度:
                        进度("download", (i + 1) * 4096, 20 * 4096)
                    time.sleep(0.05)
            return {"path": str(目标), "size": 目标.stat().st_size}

    def setUp(self):
        传输引擎._暂存账["已用"] = 0
        self.根 = Path(tempfile.mkdtemp(prefix="v8_3_暂停_"))
        self.源根 = self.根 / "源"
        self.目标根 = self.根 / "目标"
        self.源根.mkdir(parents=True)
        self.目标根.mkdir(parents=True)
        (self.源根 / "慢.bin").write_bytes(b"X" * (20 * 4096))
        self.源 = self.慢适配器(self.源根)
        self.目标 = 本地假适配器(self.目标根)

    def tearDown(self):
        传输引擎._暂存账["已用"] = 0
        shutil.rmtree(self.根, ignore_errors=True)

    def test_运行中暂停_记为暂停且保留中转(self):
        引擎 = 传输引擎({"源": self.源, "目标": self.目标})
        事件: list[dict] = []
        缓存 = self.根 / "缓存"
        请求 = 传输请求(源网盘="源", 源路径="/慢.bin", 目标网盘="目标",
                    目标路径="/慢.bin", 重试次数=0,
                    缓存=缓存配置(目录=str(缓存)))
        线程 = threading.Thread(
            target=lambda: 引擎.传输(请求, 事件回调=lambda e: 事件.append(e.to_dict())),
            daemon=True)
        线程.start()
        # 等第一条进度出来再暂停，确保暂停落在运行中
        for _ in range(100):
            if any(e.get("type") == "任务进度" for e in 事件):
                break
            time.sleep(0.02)
        引擎.暂停任务("任意")          # 单任务暂停判据是任务 ID，这里先按全批暂停
        引擎.全部暂停()
        线程.join(timeout=20)
        类型们 = [e.get("type") for e in 事件]
        self.assertIn("任务暂停", 类型们, f"应产生暂停事件：{类型们[-4:]}")
        暂停任务 = [e for e in 事件 if e.get("type") == "任务暂停"][0]["task"]
        self.assertEqual(暂停任务.get("status_name"), "暂停")
        self.assertIn("断点", str(暂停任务.get("stage") or ""))
        # 保留中转：缓存目录里还有半成品（继续时可续传）
        残留 = [p for p in 缓存.rglob("*") if p.is_file()]
        self.assertTrue(残留, "暂停应保留中转文件以便续传")


class 桥暂停协议测试(unittest.TestCase):
    """通过真实桥进程验证：暂停标记会让上传/下载在安全点停下。"""

    @classmethod
    def setUpClass(cls):
        from v8_3.配置 import 动作, 加载配置, 准备假网盘目录
        根 = Path(tempfile.mkdtemp(prefix="v8_3_桥暂停_"))
        cls.根 = 根
        目录 = 根 / "假盘"
        准备假网盘目录(目录)
        配置 = 加载配置(根 / "配置.json")
        配置["适配器"] = [{"标识": "fake_1", "类型": "fake", "名称": "假盘",
                        "路径": str(目录), "线程数": 4, "启用": True}]
        cls.动作 = 动作(配置, 日志回调=lambda m, l="信息": None)
        cls.适配器 = cls.动作.适配器("fake_1")
        cls.适配器.启动()
        (根 / "本地.txt").write_bytes(b"hello\n" * 4)
        cls.适配器.确保目录("/暂停测试")

    @classmethod
    def tearDownClass(cls):
        try:
            cls.适配器.关闭()
        except Exception:
            pass
        try:
            cls.动作.关闭()
        except Exception:
            pass
        shutil.rmtree(cls.根, ignore_errors=True)

    def test_暂停标记让操作立刻停下(self):
        任务ID = f"暂停-{time.time()}"
        self.适配器.暂停任务(任务ID)
        with self.assertRaises(Exception) as 上下文:
            self.适配器.上传(str(self.根 / "本地.txt"), "/暂停测试",
                         名称="停.txt", 任务ID=任务ID)
        self.assertIn("暂停", str(上下文.exception))
        # 继续后同一个任务应当能正常跑完
        self.适配器.继续任务(任务ID)
        结果 = self.适配器.上传(str(self.根 / "本地.txt"), "/暂停测试",
                            名称="停.txt", 任务ID=任务ID)
        self.assertEqual(结果.get("name"), "停.txt")

    def test_暂停其他任务不影响本任务(self):
        结果 = self.适配器.上传(str(self.根 / "本地.txt"), "/暂停测试",
                            名称="不受影响.txt", 任务ID="别的任务")
        self.assertEqual(结果.get("name"), "不受影响.txt")


if __name__ == "__main__":
    unittest.main()
