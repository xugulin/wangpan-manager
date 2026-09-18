"""本地小模型市场（v8_3/AI/模型市场.py）的离线单测。

用户要求：模型目录要能动态拉取、要按"针对本项目的推荐指数"排序、
前三名用不同 emoji 标记、每个模型有图片/多行详情/一句核心总结、
以及一键安装·卸载·更新 + 下载链接 + 官网。

这些用例**全部离线**（`V8_3_不联网=1`），只验证"数据 + 打分 + 缓存"逻辑；
真正的注册表核对与 ollama 装/卸在实机验收里跑。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

项目根 = Path(__file__).resolve().parents[1]
if str(项目根) not in sys.path:
    sys.path.insert(0, str(项目根))

os.environ["V8_3_不联网"] = "1"      # 单测一律不联网

from v8_3.AI import 模型市场 as 市场  # noqa: E402


class 目录与打分测试(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.目录 = 市场.构建目录(联网=True)      # 总闸已开 → 自动降级为策展目录

    def test_离线也有足够多的策展模型(self):
        self.assertGreaterEqual(len(self.目录), 15,
                            f"策展目录太小：{len(self.目录)}")

    def test_目录按推荐指数倒序(self):
        分数 = [x.分数 for x in self.目录]
        self.assertEqual(分数, sorted(分数, reverse=True),
                        "目录必须按推荐指数从高到低排")

    def test_前三名有不同emoji标记(self):
        self.assertTrue(市场.名次标记(1).startswith("🥇"))
        self.assertTrue(市场.名次标记(2).startswith("🥈"))
        self.assertTrue(市场.名次标记(3).startswith("🥉"))
        self.assertEqual(市场.名次标记(4), "")
        self.assertEqual(len({市场.名次标记(i) for i in (1, 2, 3)}), 3,
                        "三个名次的 emoji 不能重复")

    def test_每个模型都有总结理由与详情(self):
        for 条目 in self.目录:
            self.assertTrue(条目.总结 and len(条目.总结) >= 8,
                            f"{条目.名字} 缺核心总结")
            self.assertTrue(条目.推荐理由, f"{条目.名字} 缺推荐理由")
            self.assertGreaterEqual(len(条目.详情行()), 10,
                                f"{条目.名字} 的详细信息行太少")
            self.assertTrue(条目.官方页.startswith("https://ollama.com/"),
                            f"{条目.名字} 缺官网")
            self.assertTrue(条目.下载页.startswith("https://ollama.com/"),
                            f"{条目.名字} 缺下载链接")
            self.assertIn(市场.推荐等级(条目.分数)[:1], "🟢🟡🟠⚪")

    def test_打分落在0到100(self):
        for 条目 in self.目录:
            self.assertGreaterEqual(条目.分数, 0.0)
            self.assertLessEqual(条目.分数, 100.0)

    def test_权重能改变排序(self):
        偏轻量 = 市场.推荐权重(任务适配=0.05, 中文能力=0.05, 推理能力=0.05,
                          轻量=0.8, 新鲜度=0.05)
        重排 = 市场.排序并打分(市场.策展目录(), 偏轻量)
        self.assertLessEqual(重排[0].参数B, 2.0,
                            f"轻量权重拉满后应当推荐小模型：{重排[0].名字}")

    def test_权重归一化与零权重兜底(self):
        全零 = 市场.推荐权重(0, 0, 0, 0, 0).归一()
        self.assertAlmostEqual(全零.任务适配, 0.35, places=6,
                            msg="全零权重应回落到默认权重")
        权重 = 市场.推荐权重(2, 2, 2, 2, 2).归一()
        self.assertAlmostEqual(权重.任务适配 + 权重.中文能力 + 权重.推理能力
                            + 权重.轻量 + 权重.新鲜度, 1.0, places=6)

    def test_不存在的模型会被重罚(self):
        条目 = 市场.策展目录()[0]
        原分, _ = 市场.打分(条目)
        条目.核对状态 = "不存在"
        罚分, _ = 市场.打分(条目)
        self.assertLess(罚分, 原分 * 0.5,
                        f"不存在的模型要被重罚：{原分} → {罚分}")

    def test_缺字段按中性参与打分(self):
        空 = 市场.模型条目(名字="x:1b", 中文名="测试")
        分, 理由 = 市场.打分(空)
        self.assertGreater(分, 0)
        self.assertTrue(理由)
        self.assertAlmostEqual(分, 50.0, delta=3.0,
                            msg="全中性分项应当接近 50 分")


class 已装状态测试(unittest.TestCase):
    def test_合并已装与可更新(self):
        条目们 = 市场.策展目录()
        目标 = 条目们[0]
        目标.体积字节 = 3_000_000_000
        市场.合并已装状态(条目们, {目标.名字: {"大小": 1_000_000_000}})
        self.assertTrue(目标.已安装)
        self.assertTrue(目标.可更新, "本机体积远小于官方体积 → 应标记可更新")

    def test_本机体积一致时不算可更新(self):
        条目们 = 市场.策展目录()
        目标 = 条目们[0]
        目标.体积字节 = 3_000_000_000
        市场.合并已装状态(条目们, {目标.名字: {"大小": 2_999_000_000}})
        self.assertTrue(目标.已安装)
        self.assertFalse(目标.可更新)

    def test_没装的不会标已安装(self):
        条目们 = 市场.策展目录()
        市场.合并已装状态(条目们, {})
        self.assertFalse(any(getattr(x, "已安装", False) for x in 条目们))


class 缓存测试(unittest.TestCase):
    def setUp(self):
        self.临时 = tempfile.TemporaryDirectory(prefix="v8_3_market_")
        self.原路径 = 市场.缓存路径
        市场.缓存路径 = lambda: Path(self.临时.name) / "模型市场.json"

    def tearDown(self):
        市场.缓存路径 = self.原路径
        self.临时.cleanup()

    def test_写入读回(self):
        目录 = 市场.策展目录()
        市场.写入缓存(目录)
        self.assertGreater(市场.缓存时间(), 0)
        读回 = 市场.读取缓存()
        self.assertEqual(len(读回), len(目录))
        self.assertEqual(读回[0].名字, 目录[0].名字)
        self.assertAlmostEqual(读回[0].分数, 目录[0].分数, places=3)

    def test_过期缓存不返回(self):
        市场.写入缓存(市场.策展目录())
        self.assertEqual(市场.读取缓存(最长年龄=-1), [],
                         "负数年龄 = 强制视为过期")

    def test_坏缓存被丢弃(self):
        路径 = 市场.缓存路径()
        路径.parent.mkdir(parents=True, exist_ok=True)
        路径.write_text('{"时间": 99999999999, "模型": [{"名字": "x"}]}',
                      encoding="utf-8")
        self.assertEqual(市场.读取缓存(), [],
                       "缺字段的坏缓存必须作废（否则界面会出现空卡片）")

    def test_清除缓存(self):
        市场.写入缓存(市场.策展目录())
        市场.清除缓存()
        self.assertEqual(市场.读取缓存(), [])


class 动态拉取约束测试(unittest.TestCase):
    def test_不联网总闸生效(self):
        self.assertTrue(市场.联网被禁用())
        目录 = 市场.构建目录(联网=True)
        self.assertTrue(目录)
        self.assertTrue(all(x.核对状态 == "未核对" for x in 目录),
                        "不联网时不该有任何模型被标成「已核对」")

    def test_大模型不会被动态收进来(self):
        """参数量超过上限的（700B 那种）不能进本项目的小模型目录。"""
        self.assertGreaterEqual(市场.小模型上限B, 7.0)
        self.assertLessEqual(市场.小模型上限B, 30.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
