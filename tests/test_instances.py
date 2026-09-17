"""测试网盘多实例（同一家网盘多个账号）：配置 CRUD、标识解析、引擎键。

这些测试不需要网络，也不需要真的适配器项目（用 tests/test_engine 里的
本地假适配器 + 临时目录冒充适配器项目）。
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path

项目根 = Path(__file__).resolve().parents[1]
if str(项目根) not in sys.path:
    sys.path.insert(0, str(项目根))

from tests.test_engine import 本地假适配器
from v8_3.配置 import (
    加载配置, 保存配置, 网盘实例列表, 新增网盘实例, 更新网盘实例,
    删除网盘实例, 解析标识, 查找实例, 生成唯一标识, 复制适配器项目,
    准备假网盘目录, 适配器规格表, 实例名称表,
)
from v8_3.核心.传输引擎 import 传输引擎, 传输请求
from v8_3.核心.模型 import 网盘类型, 缓存配置, 标识文本, 显示名


class 配置多实例测试(unittest.TestCase):
    def setUp(self):
        self.临时 = tempfile.TemporaryDirectory(prefix="v8_3_inst_")
        self.根 = Path(self.临时.name)
        self.配置路径 = self.根 / "配置.json"
        self.配置 = 加载配置(self.配置路径)
        self.配置["适配器"] = [
            {"标识": "baidu", "类型": "baidu", "名称": "百度网盘",
             "路径": str(self._建项目("百度A")), "线程数": 12, "启用": True},
            {"标识": "quark", "类型": "quark", "名称": "夸克网盘",
             "路径": str(self._建项目("夸克A")), "线程数": 12, "启用": True},
        ]

    def tearDown(self):
        self.临时.cleanup()

    def _建项目(self, 名字: str) -> Path:
        目录 = self.根 / 名字
        (目录 / "核心").mkdir(parents=True, exist_ok=True)
        (目录 / "数据").mkdir(parents=True, exist_ok=True)
        (目录 / "启动.py").write_text("# 假适配器\n", encoding="utf-8")
        (目录 / "数据" / "会话.json").write_text("{}", encoding="utf-8")
        (目录 / "__pycache__").mkdir(exist_ok=True)
        (目录 / "__pycache__" / "x.pyc").write_bytes(b"x")
        return 目录

    # ---------------- 列表 / 解析 ----------------

    def test_旧字典格式自动升级成列表(self):
        旧 = {"适配器": {"baidu": {"路径": "百度网盘适配器", "线程数": 6}}}
        路径 = self.根 / "旧.json"
        路径.write_text(json.dumps(旧, ensure_ascii=False), encoding="utf-8")
        配置 = 加载配置(路径)
        实例们 = 网盘实例列表(配置)
        self.assertEqual([x["标识"] for x in 实例们], ["baidu"])
        self.assertEqual(实例们[0]["类型"], "baidu")
        self.assertEqual(实例们[0]["线程数"], 6)

    def test_解析标识支持标识名称类型与中文名(self):
        新增网盘实例(self.配置, 网盘类型.百度, 名称="百度小号",
                    路径=str(self._建项目("百度B")), 标识="baidu_2")
        self.assertEqual(解析标识(self.配置, "baidu_2"), "baidu_2")
        self.assertEqual(解析标识(self.配置, "百度小号"), "baidu_2")
        self.assertEqual(解析标识(self.配置, "夸克"), "quark")
        self.assertEqual(解析标识(self.配置, "百度网盘"), "baidu")
        with self.assertRaises(ValueError):
            解析标识(self.配置, "光鸭")

    def test_唯一标识与查找(self):
        新增网盘实例(self.配置, 网盘类型.百度, 名称="百度二号",
                    路径=str(self._建项目("百度B")))
        新增网盘实例(self.配置, 网盘类型.百度, 名称="百度三号",
                    路径=str(self._建项目("百度C")))
        标识们 = [x["标识"] for x in 网盘实例列表(self.配置)]
        self.assertEqual(标识们, ["baidu", "quark", "baidu_2", "baidu_3"])
        self.assertEqual(生成唯一标识(self.配置, 网盘类型.百度), "baidu_4")
        self.assertEqual(查找实例(self.配置, "百度三号")["标识"], "baidu_3")
        self.assertIsNone(查找实例(self.配置, "光鸭"))

    def test_新增重名标识被拒绝(self):
        with self.assertRaises(ValueError):
            新增网盘实例(self.配置, 网盘类型.百度, 路径=str(self._建项目("X")),
                        标识="baidu")

    # ---------------- 编辑 / 删除 ----------------

    def test_编辑实例(self):
        结果 = 更新网盘实例(self.配置, "baidu", 名称="百度主号", 线程数=3,
                          启用=False)
        self.assertEqual(结果["名称"], "百度主号")
        self.assertEqual(结果["线程数"], 3)
        self.assertFalse(结果["启用"])
        # 停用后不再出现在规格表里，但仍在实例列表里（导航隐藏、编辑可恢复）
        self.assertNotIn("baidu", 适配器规格表(self.配置))
        self.assertIn("baidu", [x["标识"] for x in 网盘实例列表(self.配置)])

    def test_编辑可改标识并查重(self):
        更新网盘实例(self.配置, "quark", 标识="quark_main")
        self.assertEqual(解析标识(self.配置, "quark_main"), "quark_main")
        with self.assertRaises(ValueError):
            更新网盘实例(self.配置, "quark_main", 标识="baidu")

    def test_删除实例(self):
        被删 = 删除网盘实例(self.配置, "百度")
        self.assertEqual(被删["标识"], "baidu")
        self.assertEqual([x["标识"] for x in 网盘实例列表(self.配置)], ["quark"])
        self.assertIsNone(删除网盘实例(self.配置, "baidu_不存在"))

    # ---------------- 规格表 ----------------

    def test_规格表以标识为键并带名称图标(self):
        新增网盘实例(self.配置, 网盘类型.百度, 名称="百度小号",
                    路径=str(self._建项目("百度B")))
        规格表 = 适配器规格表(self.配置)
        self.assertEqual(sorted(规格表), ["baidu", "baidu_2", "quark"])
        self.assertEqual(规格表["baidu_2"].显示名, "百度小号")
        self.assertEqual(规格表["baidu_2"].类型, 网盘类型.百度)
        self.assertTrue(规格表["baidu_2"].按钮标题.endswith("百度小号"))
        self.assertEqual(实例名称表(self.配置)["baidu_2"], "百度小号")

    def test_保存后重新加载保持一致(self):
        新增网盘实例(self.配置, 网盘类型.夸克, 名称="夸克小号",
                    路径=str(self._建项目("夸克B")))
        保存配置(self.配置, self.配置路径)
        重新 = 加载配置(self.配置路径)
        self.assertEqual([x["标识"] for x in 网盘实例列表(重新)],
                         ["baidu", "quark", "quark_2"])
        self.assertEqual(实例名称表(重新)["quark_2"], "夸克小号")

    # ---------------- 复制适配器项目 ----------------

    def test_复制适配器项目排除凭证与缓存(self):
        源 = self._建项目("源项目")
        (源 / "核心" / "认证.py").write_text("x = 1\n", encoding="utf-8")
        目标 = self.根 / "副本"
        结果 = 复制适配器项目(源, 目标)
        self.assertTrue((结果 / "核心" / "认证.py").is_file())
        self.assertTrue((结果 / "启动.py").is_file())
        self.assertTrue((结果 / "数据").is_dir())
        self.assertFalse((结果 / "数据" / "会话.json").exists())   # 凭证不复制
        self.assertFalse((结果 / "__pycache__").exists())          # 缓存不复制

    def test_复制到非空目录默认拒绝(self):
        源 = self._建项目("源项目2")
        目标 = self.根 / "已存在"
        目标.mkdir()
        (目标 / "别动我.txt").write_text("keep", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            复制适配器项目(源, 目标)
        self.assertTrue((目标 / "别动我.txt").is_file())

    def test_准备假网盘目录(self):
        目录 = 准备假网盘目录(self.根 / "假盘")
        self.assertTrue((目录 / "核心").is_dir())
        self.assertTrue((目录 / "启动.py").is_file())


class 引擎多实例测试(unittest.TestCase):
    """同一家网盘（同类型）的两个实例可以互相传文件。"""

    def setUp(self):
        self.临时 = tempfile.TemporaryDirectory(prefix="v8_3_multi_")
        self.根 = Path(self.临时.name)
        (self.根 / "账号A").mkdir()
        (self.根 / "账号B").mkdir()
        (self.根 / "账号A" / "共享.txt").write_text("hello", encoding="utf-8")
        (self.根 / "账号B" / "回传源.txt").write_text("world", encoding="utf-8")
        self.甲 = 本地假适配器(self.根 / "账号A")
        self.乙 = 本地假适配器(self.根 / "账号B")
        self.甲.标识 = "fake_a"
        self.乙.标识 = "fake_b"
        self.引擎 = 传输引擎(
            {"fake_a": self.甲, "fake_b": self.乙},
            数据库路径=self.根 / "任务.db",
            名称表={"fake_a": "假网盘甲", "fake_b": "假网盘乙"})

    def tearDown(self):
        self.引擎.关闭()
        self.临时.cleanup()

    def test_同类型两实例互传(self):
        请求 = 传输请求(
            源网盘="fake_a", 源路径="/共享.txt",
            目标网盘="fake_b", 目标路径="/收到.txt",
            缓存=缓存配置(目录=str(self.根 / "中转")))
        事件 = []
        统计 = self.引擎.传输(请求, 事件回调=事件.append)
        self.assertEqual(统计.失败, 0)
        self.assertEqual(统计.已完成, 1)
        self.assertTrue((self.根 / "账号B" / "收到.txt").is_file())
        self.assertEqual((self.根 / "账号B" / "收到.txt").read_text(encoding="utf-8"),
                         "hello")
        文本 = [e.to_dict() for e in 事件]
        self.assertTrue(any(d.get("task", {}).get("source_cloud_name") == "假网盘甲"
                            for d in 文本 if d.get("task")))

    def test_标识文本与显示名兜底(self):
        self.assertEqual(标识文本(网盘类型.光鸭), "guangya")
        self.assertEqual(标识文本("disk_9"), "disk_9")
        self.assertEqual(显示名("guangya"), "光鸭云盘")
        self.assertEqual(显示名("disk_9", {"disk_9": "第九个盘"}), "第九个盘")
        self.assertEqual(显示名("disk_9"), "disk_9")   # 查不到就原样返回

    def test_引擎按标识取适配器(self):
        请求 = 传输请求(源网盘="fake_b", 源路径="/", 目标网盘="fake_a",
                       目标路径="/回传")
        self.引擎.传输(请求)
        self.assertTrue((self.根 / "账号A" / "回传" / "回传源.txt").is_file())
        请求 = 传输请求(源网盘="不存在", 源路径="/", 目标网盘="fake_a",
                       目标路径="/x")
        with self.assertRaises(KeyError):
            self.引擎.传输(请求)


if __name__ == "__main__":
    unittest.main()
