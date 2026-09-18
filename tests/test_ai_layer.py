"""AI 层（v8_3.AI）离线自测：只跑纯逻辑与离线降级路径，全程不联网。

    cd 网盘管理_V8_3 && python -m unittest tests.test_ai_layer -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from unittest import mock

项目根 = Path(__file__).resolve().parents[1]
if str(项目根) not in sys.path:
    sys.path.insert(0, str(项目根))

import httpx

from v8_3.AI import (
    AI学习库, AI智能助手, AI调度器, DeepSeek价格抓取器,
    队列特征分析器, 时段管理器, 预算管理器,
)
from v8_3.AI.配置适配 import 取AI配置
from v8_3.配置 import AI配置, AI学习库路径


# ==================== 测试小工具 ====================


def _当前分钟() -> int:
    try:
        from zoneinfo import ZoneInfo
        现在 = datetime.now(ZoneInfo("Asia/Shanghai"))
    except Exception:                        # pragma: no cover - 平台相关
        现在 = datetime.now()
    return 现在.hour * 60 + 现在.minute


def _分钟文本(分钟: int) -> str:
    分钟 %= 1440
    return f"{分钟 // 60:02d}:{分钟 % 60:02d}"


class 假价格抓取器:
    """只有 ``获取高峰时段()`` 的最小 stub（V8 的时段管理器只用这一个方法）。"""

    def __init__(self, 时段配置):
        self.时段配置 = dict(时段配置)

    def 获取高峰时段(self):
        return dict(self.时段配置)


def _时段窗口(开始分钟: int, 结束分钟: int) -> dict:
    """V8 形态的时段配置（工作日全周，单窗口）。"""
    return {
        "工作日": [0, 1, 2, 3, 4, 5, 6],
        "时段": [{"start": _分钟文本(开始分钟),
                 "end": _分钟文本(结束分钟)}],
        "时区": "Asia/Shanghai",
    }


def _覆盖当前时间() -> dict:
    return _时段窗口(_当前分钟() - 10, _当前分钟() + 10)


def _避开当前时间() -> dict:
    return _时段窗口(_当前分钟() + 30, _当前分钟() - 30)


@contextmanager
def 禁止联网():
    """把 httpx 的所有出网入口 mock 掉，退出时（含异常路径）断言一次都没被调用。"""
    with mock.patch.object(httpx, "get") as 取, \
            mock.patch.object(httpx, "post") as 发, \
            mock.patch.object(httpx, "request") as 请求, \
            mock.patch.object(httpx, "Client") as 客户端, \
            mock.patch.object(httpx, "AsyncClient") as 异步客户端:
        try:
            yield
        finally:
            for 桩 in (取, 发, 请求, 客户端, 异步客户端):
                桩.assert_not_called()


假HTML = """
<html><body>
<h2>模型细节</h2>
<table>
  <tr><th>模型</th><th>deepseek-flash</th><th>deepseek-v4-pro</th></tr>
  <tr><td>价格（元/百万tokens）</td>
      <td>0.02 元</td><td>0.04 元</td><td>0.15 元</td><td>0.30 元</td></tr>
  <tr><td>输入（缓存未命中）</td>
      <td>1.00 元</td><td>2.00 元</td><td>4.50 元</td><td>9.00 元</td></tr>
  <tr><td>输出</td>
      <td>4.00 元</td><td>8.00 元</td><td>13.50 元</td><td>27.00 元</td></tr>
</table>
<p>高峰时段：北京时间 09:00-12:00、14:00-18:00</p>
</body></html>
"""


class AI层测试基类(unittest.TestCase):
    """统一的临时目录 + 强制空密钥环境（避免误用真实配置里的密钥）。"""

    def setUp(self):
        self.临时 = tempfile.TemporaryDirectory(prefix="v8_3_ai_")
        self.根 = Path(self.临时.name)
        环境 = mock.patch.dict(os.environ, {"V8_3_AI_KEY": ""})
        环境.start()
        self.addCleanup(环境.stop)
        self.addCleanup(self.临时.cleanup)

    def 离线配置(self, 启用: bool = False, api密钥: str = "") -> dict:
        """整份配置 dict（含 "AI" 段），所有落盘路径都在临时目录里。

        注意用 ``AI配置({})`` 取**纯默认值**：不带参数会走 加载配置()，
        把开发机/用户机器上真实的 配置.json 读进来 —— 那样"默认阈值 2.0"
        这类断言会随用户改过的设置而失败（踩过：本机把熔断阈值改成 3.0 后就红）。
        """
        AI段 = AI配置({})
        AI段["启用"] = 启用
        AI段["api密钥"] = api密钥
        AI段["价格抓取"] = dict(
            AI段["价格抓取"],
            缓存路径=str(self.根 / "DeepSeek价格.json"),
            历史路径=str(self.根 / "DeepSeek价格历史.jsonl"),
        )
        AI段["学习库路径"] = str(self.根 / "AI学习库.db")
        # 夹具必须显式关掉本地模型：否则这些"无密钥/禁止联网就降级"的断言
        # 会被开发机上真实运行的本地模型影响（V8_3 新增能力，见 docs/本地模型.md）
        AI段["本地模型"] = {"启用": False, "地址": "http://127.0.0.1:9"}
        return {"AI": AI段}


# ==================== 队列特征分析 ====================


class 队列特征分析测试(AI层测试基类):

    def test_空列表返回空特征(self):
        特征 = 队列特征分析器.分析([])
        self.assertIsInstance(特征, dict)
        self.assertEqual(特征["总任务数"], 0)
        self.assertEqual(特征["总字节"], 0)
        self.assertEqual(特征["大小分布"]["小(<1MB)"], 0)
        self.assertEqual(特征["类型分布"]["其他"], 0)

    def test_分析聚合统计(self):
        文件列表 = [
            ("/源/电影/阿凡达.mp4", "/目标/电影/阿凡达.mp4", 200 * 1024 * 1024),
            ("/源/电影/readme.txt", "/目标/电影/readme.txt", 1024),
            ("/源/游戏/rom.gba", "/目标/游戏/rom.gba", 5 * 1024 * 1024),
            ("/源/代码/main.py", "/目标/代码/main.py", 50 * 1024 * 1024),
        ]
        特征 = 队列特征分析器.分析(文件列表)
        self.assertEqual(特征["总任务数"], 4)
        self.assertEqual(特征["总字节"], sum(f[2] for f in 文件列表))
        self.assertEqual(特征["大小分布"]["小(<1MB)"], 1)
        self.assertEqual(特征["大小分布"]["巨大(>100MB)"], 1)
        self.assertEqual(特征["类型分布"]["媒体"], 1)
        self.assertEqual(特征["类型分布"]["游戏ROM"], 1)
        self.assertEqual(特征["类型分布"]["文档"], 1)
        self.assertEqual(特征["类型分布"]["代码"], 1)
        self.assertEqual(特征["含中文文件名数"], 1)      # 阿凡达.mp4
        self.assertEqual(特征["敏感扩展名数"], 1)         # readme.txt
        self.assertEqual(特征["源目录数"], 3)
        self.assertEqual(特征["目标目录数"], 3)
        self.assertAlmostEqual(特征["最大文件_MB"], 200.0, places=1)

    def test_计算指纹稳定且可区分(self):
        甲 = 队列特征分析器.分析(
            [("/a/x.mp4", "/b/x.mp4", 3 * 1024 * 1024)] * 120)
        乙 = 队列特征分析器.分析(
            [("/a/x.mp4", "/b/x.mp4", 3 * 1024 * 1024)] * 120)
        丙 = 队列特征分析器.分析(
            [("/a/y.txt", "/b/y.txt", 900 * 1024 * 1024)] * 120)
        self.assertEqual(队列特征分析器.计算指纹(甲),
                         队列特征分析器.计算指纹(乙))
        self.assertNotEqual(队列特征分析器.计算指纹(甲),
                            队列特征分析器.计算指纹(丙))
        self.assertEqual(len(队列特征分析器.计算指纹(甲)), 32)

    def test_相似度自比与差异(self):
        甲 = 队列特征分析器.分析(
            [("/a/x.mp4", "/b/x.mp4", 3 * 1024 * 1024)] * 120)
        近 = 队列特征分析器.分析(
            [("/a/x.mp4", "/b/x.mp4", 3 * 1024 * 1024)] * 118)
        远 = 队列特征分析器.分析(
            [("/a/y.txt", "/b/y.txt", 900 * 1024 * 1024)] * 5)
        self.assertAlmostEqual(队列特征分析器.相似度(甲, 甲), 1.0, places=6)
        self.assertGreater(队列特征分析器.相似度(甲, 近), 0.95)
        self.assertLess(队列特征分析器.相似度(甲, 远), 0.9)
        self.assertAlmostEqual(队列特征分析器.相似度(甲, 近),
                               队列特征分析器.相似度(近, 甲), places=6)
        self.assertEqual(队列特征分析器.相似度({}, 甲), 0.0)


# ==================== AI 学习库 ====================


class AI学习库测试(AI层测试基类):

    def setUp(self):
        super().setUp()
        self.库路径 = str(self.根 / "学习库.db")
        self.库 = AI学习库(self.库路径)
        self.addCleanup(self.库.关闭)
        self.特征 = 队列特征分析器.分析(
            [("/a/x.mp4", "/b/x.mp4", 3 * 1024 * 1024)] * 150)
        self.指纹 = 队列特征分析器.计算指纹(self.特征)
        self.策略 = {"并发数": 24, "分批策略": "顺序处理"}

    def test_默认路径来自配置(self):
        库 = AI学习库(配置=self.离线配置())
        try:
            self.assertEqual(库.数据库路径, AI学习库路径(self.离线配置()))
            self.assertTrue(库.数据库路径.endswith("AI学习库.db"))
            self.assertIn(str(self.根), 库.数据库路径)
        finally:
            库.关闭()

    def test_记录_更新_查询_统计往返(self):
        记录 = self.库.记录策略(self.指纹, self.特征, self.策略,
                              决策来源="ai")
        self.assertEqual(记录["特征指纹"], self.指纹)
        self.assertEqual(记录["使用次数"], 1)

        拿到 = self.库.获取记录(self.指纹)
        self.assertEqual(拿到["策略"], self.策略)
        self.assertEqual(拿到["特征"]["总任务数"], 150)
        self.assertEqual(拿到["收益评分"], 0)

        self.库.更新效果(self.指纹, 吞吐_MBps=12.5, 失败率=0.0123,
                        总耗时_秒=31.4, 收益评分=1)
        拿到 = self.库.获取记录(self.指纹)
        self.assertAlmostEqual(拿到["吞吐_MBps"], 12.5)
        self.assertAlmostEqual(拿到["失败率"], 0.0123)
        self.assertAlmostEqual(拿到["总耗时_秒"], 31.4)
        self.assertEqual(拿到["收益评分"], 1)

        命中 = self.库.查询相似高收益策略(
            self.特征, 队列特征分析器.相似度, 阈值=0.85)
        self.assertIsNotNone(命中)
        self.assertEqual(命中["记录"]["特征指纹"], self.指纹)
        self.assertGreaterEqual(命中["相似度"], 0.85)

        # 不相似的队列 + 高阈值 → 查不到
        self.assertIsNone(self.库.查询相似高收益策略(
            队列特征分析器.分析([("/a/y.txt", "/b/y.txt", 1)]),
            队列特征分析器.相似度, 阈值=0.99))

        统计 = self.库.获取统计()
        self.assertEqual(统计["总记录"], 1)
        self.assertEqual(统计["正收益"], 1)
        self.assertEqual(统计["负收益"], 0)

    def test_同指纹重复记录累加使用次数(self):
        第一次 = self.库.记录策略(self.指纹, self.特征, self.策略,
                               决策来源="ai")
        第二次 = self.库.记录策略(self.指纹, self.特征,
                                {"并发数": 8}, 决策来源="相似复用")
        self.assertEqual(第二次["使用次数"], 2)
        self.assertEqual(第二次["策略"], {"并发数": 8})
        self.assertEqual(第二次["决策来源"], "相似复用")
        self.assertEqual(第二次["创建时间"], 第一次["创建时间"])

    def test_未记录时读取与更新都安全(self):
        self.assertIsNone(self.库.获取记录("不存在的指纹"))
        self.assertIsNone(self.库.更新效果("不存在的指纹", 收益评分=1))
        self.assertEqual(self.库.获取统计()["总记录"], 0)

    def test_重开库能读到落盘记录(self):
        self.库.记录策略(self.指纹, self.特征, self.策略, 决策来源="ai")
        self.库.更新效果(self.指纹, 吞吐_MBps=9.5, 收益评分=1)
        self.库.关闭()

        第二个库 = AI学习库(self.库路径)
        try:
            self.assertEqual(第二个库.获取统计()["总记录"], 1)
            拿到 = 第二个库.获取记录(self.指纹)
            self.assertEqual(拿到["策略"], self.策略)
            self.assertAlmostEqual(拿到["吞吐_MBps"], 9.5)
        finally:
            第二个库.关闭()


# ==================== 时段管理器 ====================


class 时段管理器测试(AI层测试基类):

    def test_假价格抓取器_高峰时段(self):
        抓取器 = 假价格抓取器(_覆盖当前时间())
        时段 = 时段管理器(价格抓取器=抓取器, 配置=self.离线配置(),
                        打印启动状态=False)
        self.assertEqual(时段.获取当前时段类型(), "高峰")
        self.assertFalse(时段.是否启用AI())
        self.assertIn("高峰", 时段.获取时段摘要())

    def test_假价格抓取器_空闲时段(self):
        抓取器 = 假价格抓取器(_避开当前时间())
        时段 = 时段管理器(价格抓取器=抓取器, 配置=self.离线配置(启用=True),
                        打印启动状态=False)
        self.assertEqual(时段.获取当前时段类型(), "空闲")
        self.assertTrue(时段.是否空闲时段())
        self.assertTrue(时段.是否启用AI())
        self.assertIn("空闲", 时段.获取时段摘要())

    def test_假价格抓取器_直给类型(self):
        时段 = 时段管理器(价格抓取器=假价格抓取器({"类型": "低谷"}),
                        配置=self.离线配置(启用=True),
                        打印启动状态=False)
        self.assertEqual(时段.获取当前时段类型(), "空闲")
        self.assertTrue(时段.是否启用AI())

    def test_设置手动覆盖(self):
        时段 = 时段管理器(价格抓取器=假价格抓取器(_覆盖当前时间()),
                        配置=self.离线配置(启用=True),
                        打印启动状态=False)
        self.assertFalse(时段.是否启用AI())            # 高峰 → AI 停
        时段.设置手动覆盖(True)
        self.assertTrue(时段.是否启用AI())
        时段.设置手动覆盖(False)
        self.assertFalse(时段.是否启用AI())
        时段.设置手动覆盖(None)
        self.assertFalse(时段.是否启用AI())            # 回到自动判断

    def test_配置驱动时段(self):
        分钟 = _当前分钟()
        配置 = self.离线配置(启用=True)
        配置["AI"]["高峰时段"] = {
            "启用": True,
            "开始": _分钟文本(分钟 - 30),
            "结束": _分钟文本(分钟 + 30),
            "时区": "北京时间",
        }
        时段 = 时段管理器(配置=配置, 打印启动状态=False)
        self.assertEqual(时段.获取当前时段类型(), "高峰")
        self.assertFalse(时段.是否启用AI())

    def test_全局关闭时是否启用AI为假(self):
        配置 = self.离线配置(启用=False)
        配置["AI"]["高峰时段"] = {"启用": False, "开始": "00:30",
                               "结束": "08:30", "时区": "北京时间"}
        时段 = 时段管理器(配置=配置, 打印启动状态=False)
        self.assertEqual(时段.获取当前时段类型(), "空闲")     # 时段功能关了 → 全是低谷
        self.assertFalse(时段.是否启用AI())                  # 但 AI 总开关是关的

    def test_默认构造可用(self):
        时段 = 时段管理器(打印启动状态=False)
        self.assertIn(时段.获取当前时段类型(), ("高峰", "空闲"))
        self.assertIsInstance(时段.获取时段摘要(), str)
        self.assertIsInstance(时段.获取当前时间(), datetime)

    def test_兜底时段不抛异常(self):
        时段 = 时段管理器(价格抓取器=假价格抓取器({}),
                        配置={}, 打印启动状态=False)
        self.assertIn(时段.获取当前时段类型(), ("高峰", "空闲"))


# ==================== 预算管理器 ====================


class 预算管理器测试(AI层测试基类):

    def test_余额与摘要(self):
        预算 = 预算管理器(初始充值金额=10.0, 低余额阈值=2.0)
        self.assertAlmostEqual(预算.当前余额, 10.0)
        self.assertFalse(预算.是否已熔断)
        self.assertIn("¥10.00", 预算.获取预算摘要())
        self.assertIn("¥2.00", 预算.获取预算摘要())

    def test_低余额熔断与恢复(self):
        预算 = 预算管理器(初始充值金额=10.0, 低余额阈值=2.0)
        self.assertTrue(预算.更新余额(1.5))          # 刚熔断 → True
        self.assertTrue(预算.是否已熔断)
        self.assertFalse(预算.更新余额(1.0))         # 已熔断，不再重复报
        self.assertTrue(预算.是否已熔断)
        self.assertFalse(预算.更新余额(8.0))         # 回血 → 解除
        self.assertFalse(预算.是否已熔断)

    def test_查询失败三次熔断与解除(self):
        预算 = 预算管理器(初始充值金额=10.0, 低余额阈值=2.0)
        预算.标记查询失败()
        预算.标记查询失败()
        self.assertFalse(预算.是否已熔断)
        self.assertEqual(预算.查询失败次数, 2)
        预算.标记查询失败()
        self.assertTrue(预算.是否已熔断)
        预算.解除熔断()
        self.assertFalse(预算.是否已熔断)
        self.assertEqual(预算.查询失败次数, 0)

    def test_配置驱动余额(self):
        配置 = self.离线配置(启用=True)
        配置["AI"]["预算"] = {"初始充值金额": 20.0, "低余额阈值": 5.0,
                           "当前余额": 3.0}
        预算 = 预算管理器(配置=配置)
        self.assertAlmostEqual(预算.当前余额, 3.0)
        self.assertTrue(预算.是否已熔断)              # 3 < 5
        self.assertIn("🚨", 预算.获取预算摘要())

    def test_同步回写到整份配置(self):
        配置 = self.离线配置(启用=True)
        预算 = 预算管理器(配置=配置)
        预算.更新余额(7.25)
        self.assertFalse(预算.是否已熔断)
        self.assertTrue(预算.同步到配置())
        self.assertAlmostEqual(配置["AI"]["预算"]["当前余额"], 7.25)
        self.assertAlmostEqual(配置["AI"]["预算"]["初始充值金额"], 50.0)
        self.assertAlmostEqual(配置["AI"]["预算"]["低余额阈值"], 2.0)

    def test_自动同步开关(self):
        配置 = self.离线配置(启用=True)
        预算 = 预算管理器(配置=配置, 自动同步=True)
        预算.更新余额(9.5)
        self.assertAlmostEqual(配置["AI"]["预算"]["当前余额"], 9.5)

    def test_无配置时同步安全(self):
        预算 = 预算管理器(初始充值金额=10.0, 低余额阈值=2.0)
        预算.更新余额(1.0)
        self.assertFalse(预算.同步到配置())          # 没有可写配置 → False，不抛异常


# ==================== AI 调度器（离线降级） ====================


class AI调度器离线测试(AI层测试基类):

    def 大特征(self) -> dict:
        return 队列特征分析器.分析(
            [("/a/x.mp4", "/b/中文名.mp4", 3 * 1024 * 1024)] * 150)

    def test_AI关闭时全部降级且不联网(self):
        with 禁止联网():
            调度器 = AI调度器(配置=self.离线配置(启用=False))
            self.assertFalse(调度器._AI可用())

            策略 = 调度器.请求策略(self.大特征())
            self.assertEqual(策略["来源"], "规则")
            self.assertEqual(策略["并发数"], 16)
            self.assertTrue(策略["调度原因"].startswith("规则基线"))

            优先级 = 调度器.请求文件优先级(
                [{"路径": "/b/小文件.txt", "大小": 1024},
                 {"路径": "/b/大文件.mp4", "大小": 500 * 1024 * 1024}])
            self.assertEqual(len(优先级), 2)
            self.assertGreater(优先级["/b/小文件.txt"],
                               优先级["/b/大文件.mp4"])

            诊断 = 调度器.请求失败诊断("HTTP 500 服务器错误", "a.txt", 1)
            self.assertEqual(诊断["动作"], "重试")
            self.assertTrue(诊断["理由"].startswith("规则"))

            兜底诊断 = 调度器.请求失败诊断("说不清的错误", "a.txt", 0)
            self.assertEqual(兜底诊断["动作"], "重试")
            self.assertIn("AI未启用", 兜底诊断["理由"])

            调优 = 调度器.请求运行时调优(16, 5.0, 0.8, 10, 3, 100)
            self.assertEqual(调优["动作"], "继续")
            self.assertEqual(调优["并发数"], 8)      # 失败率 80% → 规则降并发

            统计 = 调度器.获取统计()
            self.assertIsInstance(统计, dict)
            self.assertFalse(统计["AI可用"])
            self.assertEqual(统计["AI调用"], 0)

    def test_无密钥时不联网也不建学习库(self):
        with 禁止联网():
            配置 = self.离线配置(启用=True, api密钥="")
            调度器 = AI调度器(配置=配置)
            self.assertFalse(调度器._AI可用())
            self.assertIsNone(调度器.学习库)          # 无密钥 → 不建库
            self.assertEqual(调度器.请求策略(self.大特征())["来源"], "规则")
            self.assertIsInstance(调度器.获取统计(), dict)
        self.assertFalse((self.根 / "AI学习库.db").exists())

    def test_禁用网络时不联网(self):
        with 禁止联网():
            调度器 = AI调度器(配置=self.离线配置(启用=True, api密钥="sk-测试"),
                           禁用网络=True, 初始化联网=False)
            self.assertFalse(调度器._AI可用())
            self.assertEqual(调度器.请求策略(self.大特征())["来源"], "规则")
            self.assertEqual(
                调度器.请求文件优先级([{"路径": "/a.txt", "大小": 1}])["/a.txt"],
                40)

    def test_相似度函数默认值(self):
        调度器 = AI调度器(配置=self.离线配置())
        self.assertIs(调度器.相似度函数, 队列特征分析器.相似度)

    def test_空列表与空特征安全(self):
        调度器 = AI调度器(配置=self.离线配置())
        self.assertEqual(调度器.请求文件优先级([]), {})
        self.assertEqual(调度器.请求策略({})["来源"], "规则")

    def test_V8风格手工装配也能用(self):
        助手 = AI智能助手(配置=self.离线配置(启用=False))
        时段 = 时段管理器(配置=self.离线配置(启用=False),
                        打印启动状态=False)
        预算 = 预算管理器(初始充值金额=5.0, 低余额阈值=1.0)
        库 = AI学习库(str(self.根 / "手工.db"))
        try:
            调度器 = AI调度器(AI助手=助手, 时段管理器=时段,
                            预算管理器=预算, 学习库=库,
                            相似度函数=队列特征分析器.相似度)
            self.assertIs(调度器.学习库, 库)
            self.assertEqual(调度器.请求策略(self.大特征())["来源"], "规则")
        finally:
            库.关闭()


# ==================== AI 智能助手（离线降级） ====================


class AI智能助手离线测试(AI层测试基类):

    def test_无密钥时请求全部降级(self):
        with 禁止联网():
            助手 = AI智能助手(配置=self.离线配置(启用=True, api密钥=""),
                            数据目录路径=str(self.根))
            self.assertFalse(助手.是否可用())
            self.assertEqual(助手.api密钥, "")
            self.assertEqual(助手.请求结构化策略("系统", "用户"), {})
            self.assertEqual(助手.获取模型列表(),
                             助手.官方模型白名单)
            优先级 = 助手.请求文件优先级([{"路径": "/a/中文.txt",
                                     "大小": 2048}])
            self.assertEqual(优先级["理由"], "规则回退")
            self.assertEqual(优先级["优先级映射"]["/a/中文.txt"], 20)
            诊断 = 助手.请求失败诊断("405 Method Not Allowed", "a.txt", 0)
            self.assertEqual(诊断["动作"], "改名")
            self.assertTrue(诊断["理由"].startswith("规则"))
            调优 = 助手.请求运行时调优(16, 1.0, 0.9, 5, 1, 10)
            self.assertEqual(调优["并发数"], 8)

    def test_无密钥时查余额与刷新预算不联网(self):
        with 禁止联网():
            助手 = AI智能助手(配置=self.离线配置(启用=True, api密钥=""),
                            数据目录路径=str(self.根))
            结果 = 助手.查询真实余额()
            self.assertFalse(结果["成功"])
            self.assertIn("密钥", 结果["错误"])
            提示 = 助手.检查预算并刷新余额()
            self.assertIn("余额查询失败", 提示)
            self.assertEqual(助手.预算管理器.查询失败次数, 1)
            self.assertFalse((self.根 / "AI_token自适应.json").exists())

    def test_禁用网络时不联网(self):
        with 禁止联网():
            助手 = AI智能助手(配置=self.离线配置(启用=True,
                                            api密钥="sk-测试"),
                            禁用网络=True, 数据目录路径=str(self.根))
            self.assertFalse(助手.是否可用())
            self.assertEqual(助手.查询真实余额()["成功"], False)
            self.assertEqual(助手.请求结构化策略("s", "u"), {})

    def test_价格描述与token统计可用(self):
        助手 = AI智能助手(配置=self.离线配置(启用=False),
                        数据目录路径=str(self.根))
        self.assertIn("缓存命中", 助手.获取缓存价格描述())
        self.assertEqual(助手.获取token统计()["总尝试"], 0)
        self.assertEqual(助手.获取建议max_tokens("deepseek-flash"), 2048)
        self.assertEqual(助手.获取自适应表(), {})


# ==================== DeepSeek 价格抓取器（纯解析） ====================


class DeepSeek价格抓取器测试(AI层测试基类):

    def _抓取器(self) -> DeepSeek价格抓取器:
        return DeepSeek价格抓取器(
            配置=self.离线配置(启用=False), 自动刷新=False,
            禁用网络=True, 打印状态=False,
        )

    def test_解析内嵌HTML能拿到价格(self):
        抓取器 = self._抓取器()
        结果 = 抓取器._解析HTML(假HTML)
        self.assertIsNotNone(结果)
        self.assertIn("deepseek-flash", 结果["模型"])
        self.assertIn("deepseek-v4-pro", 结果["模型"])
        便宜 = 结果["模型"]["deepseek-flash"]
        self.assertAlmostEqual(便宜["空闲"]["缓存命中"], 0.02)
        self.assertAlmostEqual(便宜["空闲"]["输出"], 4.0)
        self.assertAlmostEqual(便宜["高峰"]["缓存命中"], 0.04)
        self.assertAlmostEqual(便宜["高峰"]["输出"], 8.0)
        贵 = 结果["模型"]["deepseek-v4-pro"]
        self.assertAlmostEqual(贵["空闲"]["输出"], 13.5)
        self.assertAlmostEqual(贵["高峰"]["输出"], 27.0)

    def test_从文本提取价格(self):
        抓取器 = self._抓取器()
        结果 = {"模型": {
            "deepseek-flash": {"空闲": {}, "高峰": {}},
            "deepseek-v4-pro": {"空闲": {}, "高峰": {}},
        }}
        文本 = "价格 " + " 元 ".join(
            ["0.02", "0.04", "0.15", "0.30",
             "1.00", "2.00", "4.50", "9.00",
             "4.00", "8.00", "13.50", "27.00"]) + " 元"
        抓取器._从文本提取价格(文本, 结果)
        self.assertAlmostEqual(
            结果["模型"]["deepseek-flash"]["空闲"]["缓存未命中"], 1.0)
        self.assertAlmostEqual(
            结果["模型"]["deepseek-v4-pro"]["高峰"]["输出"], 27.0)

    def test_解析高峰时段(self):
        抓取器 = self._抓取器()
        时段 = 抓取器._解析高峰时段(
            "<p>高峰时段：每天 00:30-08:30</p>")
        self.assertEqual(时段["时段"],
                         [{"start": "00:30", "end": "08:30"}])
        self.assertEqual(时段["工作日"], [0, 1, 2, 3, 4, 5, 6])
        默认 = 抓取器._解析高峰时段("<p>没有相关字样</p>")
        self.assertTrue(默认["时段"])

    def test_禁用网络时不联网不落盘(self):
        缓存路径 = self.根 / "价格.json"
        历史路径 = self.根 / "价格历史.jsonl"
        with 禁止联网():
            抓取器 = DeepSeek价格抓取器(
                配置=self.离线配置(启用=False), 自动刷新=False,
                禁用网络=True, 打印状态=False,
                缓存路径=str(缓存路径), 历史路径=str(历史路径))
            self.assertTrue(抓取器.获取所有模型名())
            价格 = 抓取器.获取价格("deepseek-flash")
            self.assertAlmostEqual(价格["空闲"]["缓存命中"], 0.02)
            元信息 = 抓取器.获取元信息()
            self.assertEqual(元信息["来源"], "builtin")
            self.assertTrue(元信息["禁用网络"])
            self.assertEqual(抓取器.获取历史(), [])
            self.assertFalse(抓取器.手动刷新())
        self.assertFalse(缓存路径.exists())
        self.assertFalse(历史路径.exists())

    def test_配置里带来的高峰时段会覆盖抓取结果(self):
        配置 = self.离线配置(启用=False)
        配置["AI"]["高峰时段"] = {"启用": True, "开始": "01:00",
                               "结束": "07:00", "时区": "北京时间"}
        抓取器 = DeepSeek价格抓取器(配置=配置, 自动刷新=False,
                                 禁用网络=True, 打印状态=False)
        时段 = 抓取器.获取高峰时段()
        self.assertEqual(时段["开始"], "01:00")
        self.assertEqual(抓取器.获取元信息()["时段来源"], "配置")
        管理器 = 时段管理器(价格抓取器=抓取器, 配置=配置,
                         打印启动状态=False)
        self.assertIn(管理器.获取当前时段类型(), ("高峰", "空闲"))


# ==================== V8 老写法兼容 ====================


class 假配置加载器:
    """模拟 V8 的 ``配置加载器``：只有 ``获取(键, 默认)``，支持 ``"a.b"`` 取值。"""

    def __init__(self, 数据):
        self.数据 = dict(数据)

    def 获取(self, 键名, 默认=None):
        if "." in 键名:
            当前层 = self.数据
            for k in 键名.split("."):
                if isinstance(当前层, dict) and k in 当前层:
                    当前层 = 当前层[k]
                else:
                    return 默认
            return 当前层
        return self.数据.get(键名, 默认)


class V8兼容测试(AI层测试基类):

    def _V8配置(self, 时段开始: str = "00:00",
              时段结束: str = "23:59") -> 假配置加载器:
        return 假配置加载器({
            "deepseek_api_key": "",
            "ai_settings": {"model": "deepseek-flash"},
            "价格抓取": {
                "刷新间隔小时": 12, "历史最多条数": 20,
                "缓存路径": str(self.根 / "v8价格.json"),
                "历史路径": str(self.根 / "v8历史.jsonl"),
                "禁用网络": True,
            },
            "budget": {"initial_amount": 30.0,
                       "low_balance_threshold": 3.0},
            "database": {"AI学习库": str(self.根 / "v8学习库.db")},
            "ai_schedule": {"工作日": [0, 1, 2, 3, 4, 5, 6],
                            "时段": [{"start": 时段开始,
                                    "end": 时段结束}],
                            "时区": "Asia/Shanghai"},
        })

    def test_配置对象被翻译成AI段(self):
        段 = 取AI配置(self._V8配置())
        self.assertEqual(段["模型"], "deepseek-flash")       # V8 的 model → 模型
        self.assertEqual(段["预算"]["初始充值金额"], 30.0)
        self.assertEqual(段["预算"]["低余额阈值"], 3.0)
        self.assertEqual(段["价格抓取"]["刷新间隔小时"], 12)
        self.assertTrue(段["价格抓取"]["禁用网络"])
        self.assertEqual(段["高峰时段"]["时段"],
                         [{"start": "00:00", "end": "23:59"}])
        # dict 方式的 ai_settings 也认
        self.assertEqual(取AI配置({"model": "deepseek-flash"})["模型"],
                         "deepseek-flash")

    def test_V8的ai_schedule优先于V8_3默认时段(self):
        分钟 = _当前分钟()
        开始, 结束 = _分钟文本(分钟 - 30), _分钟文本(分钟 + 30)
        抓取器 = DeepSeek价格抓取器(配置=self._V8配置(开始, 结束),
                                 自动刷新=False, 打印状态=False)
        时段 = 时段管理器(价格抓取器=抓取器, 打印启动状态=False)
        self.assertEqual(时段._获取时段配置()["时段"],
                         [{"start": 开始, "end": 结束}])
        self.assertEqual(时段.获取当前时段类型(), "高峰")

    def test_V8关键字链路整条能用(self):
        配置对象 = self._V8配置()
        抓取器 = DeepSeek价格抓取器(配置=配置对象, 自动刷新=False,
                                 打印状态=False)
        时段 = 时段管理器(价格抓取器=抓取器)
        预算 = 预算管理器(
            初始充值金额=配置对象.获取("budget", {}).get(
                "initial_amount", 50.0),
            低余额阈值=配置对象.获取("budget", {}).get(
                "low_balance_threshold", 2.0))
        库 = AI学习库(配置对象.获取(
            "database.AI学习库", str(self.根 / "兜底.db")))
        try:
            助手 = AI智能助手(
                api密钥=配置对象.获取("deepseek_api_key", ""),
                时段管理器=时段, 预算管理器=预算,
                ai配置=配置对象.获取("ai_settings", {}),
                初始化联网=False, 数据目录路径=str(self.根))
            助手.注入价格抓取器(抓取器)
            调度器 = AI调度器(
                AI助手=助手, 时段管理器=时段, 预算管理器=预算,
                学习库=库, 相似度函数=队列特征分析器.相似度)

            self.assertAlmostEqual(预算.当前余额, 30.0)
            self.assertEqual(助手.api密钥, "")
            self.assertFalse(助手.是否可用())              # 无密钥
            self.assertIs(调度器.学习库, 库)
            特征 = 队列特征分析器.分析(
                [("/a/x.mp4", "/b/中.mp4", 3 * 1024 * 1024)] * 150)
            策略 = 调度器.请求策略(特征)
            self.assertEqual(策略["来源"], "规则")
            self.assertIsInstance(调度器.获取统计(), dict)
        finally:
            库.关闭()


# ==================== 运行时门面（界面用） ====================


class AI运行时测试(AI层测试基类):

    def test_离线构造与状态摘要(self):
        from v8_3.AI.运行时 import AI运行时
        日志 = []
        配置 = self.离线配置(启用=False)
        配置路径 = self.根 / "配置.json"
        with 禁止联网():
            运行时 = AI运行时(配置, 配置路径, 日志回调=日志.append,
                            自动刷新价格=False, 禁用网络=True)
            try:
                self.assertIs(运行时.调度器.时段管理器, 运行时.时段管理器)
                self.assertFalse(运行时.是否可用())
                self.assertIsNone(运行时.学习库)
                摘要 = 运行时.获取状态摘要()
                for 键 in ("可用", "模型", "时段", "预算摘要", "价格描述",
                          "调度统计", "价格元信息"):
                    self.assertIn(键, 摘要)
                特征 = 队列特征分析器.分析(
                    [("/a/x.mp4", "/b/x.mp4", 1024)] * 150)
                self.assertEqual(运行时.调度器.请求策略(特征)["来源"], "规则")
                self.assertEqual(运行时.刷新价格(), False)
                self.assertIn("余额查询失败", 运行时.刷新余额())
                self.assertIn(运行时.获取当前时段类型(), ("高峰", "空闲"))
                self.assertTrue(运行时.保存配置())
            finally:
                运行时.关闭()
        self.assertTrue(日志)                      # 日志回调被调用过
        self.assertTrue(配置路径.exists())          # 配置写到了临时路径
        self.assertFalse((self.根 / "AI学习库.db").exists())

    def test_保存配置不碰真实配置(self):
        from v8_3.AI.运行时 import AI运行时
        配置 = self.离线配置(启用=False)
        配置路径 = self.根 / "副本.json"
        运行时 = AI运行时(配置, 配置路径)
        try:
            self.assertTrue(运行时.保存配置())
        finally:
            运行时.关闭()
        文本 = 配置路径.read_text(encoding="utf-8")
        self.assertIn('"AI"', 文本)
        self.assertNotEqual(配置路径, 项目根 / "配置.json")


if __name__ == "__main__":
    unittest.main()