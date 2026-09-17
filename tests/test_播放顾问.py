"""AI 播放顾问单测（V8_3 新增）。

全部**离线**：假 AI 只覆盖三种状态（正常 JSON / 坏 JSON / 直接抛异常），
不连真实 Ollama、不发任何外部请求。真机验证（deepseek-r1:1.5b）在交付报告里，
不放进单测，避免 CI 上因为没有本地模型而红。

覆盖范围
========
* 规则基线各分支：带宽够 / 不够 / 4K / 无硬解 / 没测到带宽 / 没给码率 /
  首字节慢 / Range 不支持 / 缓存目录快满 / 短视频；
* AI 成功时覆盖规则（并做区间钳制），AI 给的硬解必须是本机有的；
* AI 失败三态（坏 JSON、抛异常、返回空）逐一回落到规则，并且**日志里有说明**；
* 本地模型挂了会转云端助手，云端也没有才回规则；
* 学习库命中优先于 AI，且能感知"硬件变了 / 带宽更紧了"；
* 诊断卡顿各分支：缓冲不足、硬解没生效（有替代/无替代/信息缺失）、
  丢帧且码率超带宽、丢帧但缓冲充足、CPU 高、一切正常；
* SQLite 落盘：记录效果 → 查得到 → 统计对得上；库不可用时不抛异常。

**不改 tests/ 里任何别人的文件**，也不依赖它们的辅助函数。
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

项目根 = Path(__file__).resolve().parents[1]
if str(项目根) not in sys.path:
    sys.path.insert(0, str(项目根))

from v8_3.AI.播放顾问 import (播放顾问, 判定卡顿, 分辨率档, 生成学习键,
                          默认学习库路径, 解析JSON对象)


# ==================== 测试替身（假 AI） ====================


class 假本地模型:
    """替身：三种状态 —— 正常 / 坏 JSON / 抛异常。"""

    def __init__(self, 内容: str = "", 异常: Exception = None,
                 成功: bool = True, 错误: str = "", 推理内容: str = ""):
        self.内容 = 内容
        self.推理内容 = 推理内容
        self.异常 = 异常
        self.成功 = 成功
        self.错误 = 错误
        self.调用次数 = 0
        self.收到的系统提示 = ""
        self.收到的用户消息 = ""
        self.收到的最大tokens = None

    def 对话(self, 用户消息, 系统提示="", *, 模型="", 温度=None, 最大tokens=None):
        self.调用次数 += 1
        self.收到的系统提示 = 系统提示
        self.收到的用户消息 = 用户消息
        self.收到的最大tokens = 最大tokens
        if self.异常 is not None:
            raise self.异常
        return _假对话结果(self.成功, self.内容, self.错误, self.推理内容)


class _假对话结果:
    """模仿 ``v8_3.AI.本地模型.对话结果`` 的字段（只放本模块用到的）。"""

    def __init__(self, 成功=True, 内容="", 错误="", 推理内容=""):
        self.成功 = 成功
        self.内容 = 内容
        self.推理内容 = 推理内容
        self.错误 = 错误


class 假本地模型_无tokens参数:
    """更简陋的替身：连 ``最大tokens`` 关键字都不接受。"""

    def __init__(self, 内容=""):
        self.内容 = 内容
        self.调用次数 = 0

    def 对话(self, 用户消息, 系统提示=""):
        self.调用次数 += 1
        return self.内容


class 假云端助手:
    def __init__(self, 返回: dict = None, 异常: Exception = None):
        self.返回 = 返回 if 返回 is not None else {}
        self.异常 = 异常
        self.调用次数 = 0

    def 请求结构化策略(self, 系统提示: str, 用户消息: str, 强制模型: str = None) -> dict:
        self.调用次数 += 1
        if self.异常 is not None:
            raise self.异常
        return self.返回


# ==================== 公共数据 ====================


def 造探测(**覆盖) -> dict:
    """一个典型的 4K/25Mbps、实测 90Mbps 的夸克直链探测结果。"""
    探测 = {
        "网盘": "quark", "文件名": "xxx.mp4",
        "大小字节": 8_000_000_000,
        "分辨率": "3840x2160", "视频码率bps": 25_000_000, "音频码率bps": 192_000,
        "时长秒": 5400.0, "容器": "mp4", "视频编码": "hevc",
        "首字节毫秒": 180.0,
        "实测带宽bps": 90_000_000,
        "range支持": True,
        "本机硬解": ["vaapi", "none"],
        "缓存目录剩余字节": 40_000_000_000,
    }
    探测.update(覆盖)
    return 探测


def 造采样(**覆盖) -> dict:
    采样 = {"丢帧": 0, "解码丢帧": 0, "缓冲等待次数": 0, "已播秒": 60.0,
          "平均码率bps": 25_000_000, "当前缓冲秒": 8.0,
          "硬解生效": True, "CPU占用": 0.2}
    采样.update(覆盖)
    return 采样


class 顾问基类(unittest.TestCase):
    """每个用例一个独立临时目录的学习库，互不干扰、也不碰项目 数据/。"""

    def setUp(self):
        self._临时 = tempfile.TemporaryDirectory(prefix="播放顾问测试_")
        self.库路径 = os.path.join(self._临时.name, "AI播放学习.db")
        self.日志: list[str] = []

    def tearDown(self):
        self._临时.cleanup()

    def 造顾问(self, AI助手=None, 本地模型=None, 启用AI=True) -> 播放顾问:
        顾问 = 播放顾问(AI助手=AI助手, 本地模型=本地模型,
                     日志回调=self.日志.append,
                     学习库路径=self.库路径, 启用AI=启用AI)
        self.addCleanup(顾问.关闭)
        return 顾问

    def 连接(self) -> sqlite3.Connection:
        连接 = sqlite3.connect(self.库路径)
        连接.row_factory = sqlite3.Row
        return 连接


# ==================== 一、纯工具函数 ====================


class 测试工具函数(unittest.TestCase):

    def test_分辨率档各种写法(self):
        for 文本, 期望 in (("3840x2160", "2160p"), ("2160p", "2160p"),
                        ("4K", "2160p"), ("1920x1080", "1080p"),
                        ("1920×1080", "1080p"), ("1080p", "1080p"),
                        ("1280x720", "720p"), ("640x480", "480p"),
                        ("1440p", "1440p"), ("7680x4320", "4320p"),
                        ("320x240", "低清"), ("", "未知"), ("不知道", "未知")):
            with self.subTest(分辨率=文本):
                self.assertEqual(分辨率档(文本), 期望)
        # 竖屏视频按短边判断：解码压力等同 1080p
        self.assertEqual(分辨率档("1080x1920"), "1080p")

    def test_生成学习键带别名与退化键(self):
        self.assertEqual(生成学习键("Quark", "3840x2160", "h265"),
                         "quark|2160p|hevc")
        self.assertEqual(生成学习键("quark", "3840x2160"), "quark|2160p")
        # 网盘大小写/空白都归一，避免学出两套键
        self.assertEqual(生成学习键(" QUARK ", "1080p", "H.264"),
                         "quark|1080p|h264")

    def test_解析JSON对象容错(self):
        干净 = '{"网络缓存毫秒": 8000, "起播等待秒": 2}'
        self.assertEqual(解析JSON对象(干净)["网络缓存毫秒"], 8000)
        # markdown 代码块
        包裹 = "```json\n" + 干净 + "\n```"
        self.assertEqual(解析JSON对象(包裹)["起播等待秒"], 2)
        # 前后一堆解释文字（r1 常见）
        啰嗦 = "好的，我分析了一下：\n" + 干净 + "\n以上就是建议，希望有帮助。"
        self.assertEqual(解析JSON对象(啰嗦)["网络缓存毫秒"], 8000)
        # 尾逗号
        self.assertEqual(解析JSON对象('{"a": 1, "b": [1,2,],}')["a"], 1)
        # 思考块里带花括号也不能把配对搞乱：给定期望键就能挑出真正的参数对象
        嵌套 = '推理{"x": {"y": 1}}结束 {"网络缓存毫秒": 9000}'
        self.assertEqual(解析JSON对象(嵌套, ("网络缓存毫秒",))["网络缓存毫秒"], 9000)
        # 不给期望键时按"第一个能解析的对象"返回（保持通用工具的语义）
        self.assertEqual(解析JSON对象(嵌套), {"x": {"y": 1}})
        # 彻底不是 JSON
        for 坏 in ("", "我觉得应该加大缓存", "[1, 2, 3]", None):
            self.assertIsNone(解析JSON对象(坏))

    def test_解析被token上限截断的JSON(self):
        # 真机实测：deepseek-r1:1.5b 在 512 tokens 处被硬截断，就是这个样子
        真机截断 = ('```json\n{   "网络缓存毫秒": 40000,   "起播等待秒": 2,   '
                 '"硬解": "vaapi",')
        解析 = 解析JSON对象(真机截断, ("网络缓存毫秒",))
        self.assertIsNotNone(解析)
        self.assertEqual(解析["网络缓存毫秒"], 40000)
        self.assertEqual(解析["起播等待秒"], 2)
        self.assertEqual(解析["硬解"], "vaapi")
        # 值写到一半（半截数字）必须丢掉：40 很可能是 4000/40000，不能拿它当答案
        self.assertEqual(解析JSON对象('{"网络缓存毫秒": 40'), None)
        丢了半截 = 解析JSON对象('{"网络缓存毫秒": 40000, "起播等待秒": 2')
        self.assertEqual(丢了半截, {"网络缓存毫秒": 40000})   # 保住完整字段
        # 半截字符串：补上引号和括号，至少前面的字段可用
        半截串 = 解析JSON对象('{"硬解": "vaapi", "风险": "带宽不')
        self.assertEqual(半截串["硬解"], "vaapi")
        # 数组/嵌套也能补
        嵌套 = 解析JSON对象('{"附加选项": [":clock-jitter=0"')
        self.assertEqual(嵌套["附加选项"], [":clock-jitter=0"])

    def test_判定卡顿显式优先与自动推断(self):
        self.assertTrue(判定卡顿({"是否卡顿": "true"}))
        self.assertFalse(判定卡顿({"是否卡顿": "否"}))
        # 没给"是否卡顿"就按缓冲等待/丢帧推断
        self.assertTrue(判定卡顿({"缓冲等待次数": 2}))
        self.assertTrue(判定卡顿({"丢帧": 30}))
        self.assertFalse(判定卡顿({"丢帧": 1, "缓冲等待次数": 0}))
        # 显式说流畅时，不被丢帧推断覆盖（用户/核心最清楚）
        self.assertFalse(判定卡顿({"是否卡顿": False, "丢帧": 99}))

    def test_默认学习库路径在项目数据目录(self):
        路径 = Path(默认学习库路径())
        self.assertEqual(路径.name, "AI播放学习.db")
        self.assertEqual(路径.parent.name, "数据")
        self.assertEqual(路径.parent.parent, 项目根)


# ==================== 二、规则基线（AI 不可用也能用） ====================


class 测试规则基线(顾问基类):

    def test_带宽充足的4K给合理参数(self):
        顾问 = self.造顾问(启用AI=False)
        建议 = 顾问.建议起播参数(造探测())
        self.assertEqual(set(建议), {"网络缓存毫秒", "起播等待秒", "硬解", "附加选项",
                                 "可流畅播放", "风险", "理由", "来源"})
        self.assertEqual(建议["来源"], "规则")
        self.assertTrue(建议["可流畅播放"])
        self.assertEqual(建议["硬解"], "vaapi")
        self.assertEqual(建议["网络缓存毫秒"], 6000)     # 3.57 倍 + 4K 抬到 6000
        self.assertEqual(建议["起播等待秒"], 2.0)       # 4K 且带宽充裕
        self.assertIn(":clock-jitter=0", 建议["附加选项"])
        self.assertIn(":avcodec-hw=vaapi", 建议["附加选项"])
        self.assertIn(":no-drop-late-frames", 建议["附加选项"])
        # 选项里的缓存必须和 网络缓存毫秒 一致
        self.assertIn(f":network-caching={建议['网络缓存毫秒']}", 建议["附加选项"])
        self.assertIn("带宽够", 建议["风险"])
        self.assertIn("90.0Mbps", 建议["风险"])
        self.assertIn("3.6", 建议["风险"])
        self.assertEqual(顾问.获取统计()["规则建议次数"], 1)

    def test_带宽不足时判不能流畅并拉满缓存(self):
        顾问 = self.造顾问(启用AI=False)
        建议 = 顾问.建议起播参数(造探测(实测带宽bps=20_000_000))
        self.assertFalse(建议["可流畅播放"])
        self.assertEqual(建议["网络缓存毫秒"], 20000)     # 拉满
        self.assertEqual(建议["起播等待秒"], 10.0)
        self.assertIn(":drop-late-frames", 建议["附加选项"])
        self.assertIn("带宽不够", 建议["风险"])
        self.assertIn("1080p", 建议["风险"])             # 给出可执行的降级建议
        self.assertIn("32.7Mbps", 建议["风险"])           # 需要多少带宽

    def test_带宽刚好卡在1点3倍边界(self):
        顾问 = self.造顾问(启用AI=False)
        # 25.192M × 1.3 = 32.7496M：刚好给到就应算够（规则是 >=）
        够 = 顾问.建议起播参数(造探测(实测带宽bps=32_750_000))
        self.assertTrue(够["可流畅播放"])
        # 差一点点就不够
        不够 = 顾问.建议起播参数(造探测(实测带宽bps=32_000_000))
        self.assertFalse(不够["可流畅播放"])
        self.assertGreaterEqual(不够["网络缓存毫秒"], 16000)   # 余量很小 → 厚缓冲
        self.assertEqual(不够["起播等待秒"], 8.0)

    def test_无硬解时回退软解并提示(self):
        顾问 = self.造顾问(启用AI=False)
        建议 = 顾问.建议起播参数(造探测(本机硬解=["none"]))
        self.assertEqual(建议["硬解"], "none")
        self.assertIn(":avcodec-hw=none", 建议["附加选项"])
        self.assertIn("只能软解", 建议["风险"])
        # 本机硬解没给（列表为空）也一样安全
        空的 = 顾问.建议起播参数(造探测(本机硬解=[]))
        self.assertEqual(空的["硬解"], "none")

    def test_硬解优先级vaapi最高(self):
        顾问 = self.造顾问(启用AI=False)
        建议 = 顾问.建议起播参数(造探测(本机硬解=["cuda", "vaapi", "none"]))
        self.assertEqual(建议["硬解"], "vaapi")
        # 没有 vaapi 时按列表给的顺序（顺序即优先级）
        次选 = 顾问.建议起播参数(造探测(本机硬解=["cuda", "dxva2"]))
        self.assertEqual(次选["硬解"], "cuda")

    def test_auto要翻译成libvlc认的any(self):
        # 播放核心把"让 VLC 自己挑"叫 auto，libvlc 只认 any
        顾问 = self.造顾问(启用AI=False)
        建议 = 顾问.建议起播参数(造探测(本机硬解=["auto", "none"]))
        self.assertEqual(建议["硬解"], "auto")             # 字段保留上层叫法
        self.assertIn(":avcodec-hw=any", 建议["附加选项"])  # 选项用 libvlc 的叫法
        self.assertNotIn(":avcodec-hw=auto", 建议["附加选项"])

    def test_没测到带宽时保守并说明(self):
        顾问 = self.造顾问(启用AI=False)
        建议 = 顾问.建议起播参数(造探测(实测带宽bps=0))
        self.assertFalse(建议["可流畅播放"])
        self.assertEqual(建议["网络缓存毫秒"], 12000)
        self.assertEqual(建议["起播等待秒"], 4.0)
        self.assertIn("没测到实测带宽", 建议["风险"])

    def test_没给码率时按大小估(self):
        顾问 = self.造顾问(启用AI=False)
        # 8GB / 5400s ≈ 11.85Mbps；带宽 90Mbps 依然够
        建议 = 顾问.建议起播参数(造探测(视频码率bps=0, 音频码率bps=0))
        self.assertTrue(建议["可流畅播放"])
        self.assertIn("按大小估算", 建议["风险"])
        self.assertIn("估算", 建议["理由"])

    def test_既没码率也没大小时按未知处理(self):
        顾问 = self.造顾问(启用AI=False)
        建议 = 顾问.建议起播参数(造探测(视频码率bps=0, 音频码率bps=0,
                                 大小字节=0, 时长秒=0))
        self.assertFalse(建议["可流畅播放"])
        self.assertEqual(建议["网络缓存毫秒"], 12000)
        self.assertIn("没给码率", 建议["风险"])

    def test_首字节慢会加大缓存(self):
        顾问 = self.造顾问(启用AI=False)
        正常 = 顾问.建议起播参数(造探测())
        慢 = 顾问.建议起播参数(造探测(首字节毫秒=2500))
        self.assertGreater(慢["网络缓存毫秒"], 正常["网络缓存毫秒"])
        self.assertIn("首字节 2500ms", 慢["理由"])

    def test_不支持Range时加http_reconnect(self):
        顾问 = self.造顾问(启用AI=False)
        建议 = 顾问.建议起播参数(造探测(range支持=False))
        self.assertIn(":http-reconnect=true", 建议["附加选项"])
        self.assertIn("Range", 建议["风险"])

    def test_缓存目录快满时提醒(self):
        顾问 = self.造顾问(启用AI=False)
        建议 = 顾问.建议起播参数(造探测(缓存目录剩余字节=120_000_000))
        self.assertIn("缓存目录只剩 120MB", 建议["风险"])
        # 空间充足时不啰嗦
        self.assertNotIn("缓存目录", 顾问.建议起播参数(造探测())["风险"])

    def test_高速网络低分辨率立刻起播(self):
        顾问 = self.造顾问(启用AI=False)
        建议 = 顾问.建议起播参数(造探测(分辨率="1920x1080", 视频码率bps=8_000_000,
                                 音频码率bps=128_000, 实测带宽bps=200_000_000))
        self.assertEqual(建议["起播等待秒"], 0.0)         # 0 = 立刻起播
        self.assertTrue(建议["可流畅播放"])
        self.assertIn("可直接起播", 建议["风险"])

    def test_短视频不会要求等太久(self):
        顾问 = self.造顾问(启用AI=False)
        建议 = 顾问.建议起播参数(造探测(时长秒=9.0, 实测带宽bps=20_000_000))
        self.assertLessEqual(建议["起播等待秒"], 3.0)     # 9/3 = 3 秒上限
        self.assertGreater(建议["起播等待秒"], 0.0)

    def test_空探测结果也不崩(self):
        顾问 = self.造顾问(启用AI=False)
        for 入参 in ({}, None, {"网盘": "quark"}):
            建议 = 顾问.建议起播参数(入参)
            self.assertEqual(set(建议), {"网络缓存毫秒", "起播等待秒", "硬解",
                                     "附加选项", "可流畅播放", "风险", "理由",
                                     "来源"})
            self.assertIsInstance(建议["网络缓存毫秒"], int)
            self.assertGreaterEqual(建议["网络缓存毫秒"], 2000)
            self.assertLessEqual(建议["网络缓存毫秒"], 20000)


# ==================== 三、AI 辅助加载 ====================


class 测试AI建议(顾问基类):

    def test_AI成功时覆盖规则(self):
        AI返回 = {"网络缓存毫秒": 9000, "起播等待秒": 3.5, "硬解": "vaapi",
                "附加选项": [":clock-jitter=0", "network-caching=9000"],
                "可流畅播放": True, "风险": "AI 认为没问题", "理由": "AI 的理由"}
        模型 = 假本地模型(json.dumps(AI返回, ensure_ascii=False))
        顾问 = self.造顾问(本地模型=模型, AI助手=假云端助手({"不应该被调用": 1}))
        建议 = 顾问.建议起播参数(造探测())
        self.assertEqual(建议["来源"], "本地模型")
        self.assertEqual(建议["网络缓存毫秒"], 9000)      # 覆盖了规则的 6000
        self.assertEqual(建议["起播等待秒"], 3.5)
        self.assertEqual(建议["风险"], "AI 认为没问题")
        self.assertEqual(建议["理由"], "AI 的理由")
        # 选项做了归一化：补冒号、去重、缓存值强制对齐
        self.assertIn(":network-caching=9000", 建议["附加选项"])
        self.assertEqual(len(建议["附加选项"]),
                       len(set(建议["附加选项"])))
        self.assertEqual(模型.调用次数, 1)
        self.assertEqual(模型.收到的最大tokens, 512)      # 按约定传 512
        self.assertIn("JSON", 模型.收到的系统提示)         # 提示词要求只输出 JSON
        self.assertIn("探测结果", 模型.收到的用户消息)
        self.assertEqual(顾问.获取统计()["本地模型建议次数"], 1)

    def test_AI的非法数值被钳制(self):
        模型 = 假本地模型(json.dumps({"网络缓存毫秒": 999999, "起播等待秒": 500,
                                 "硬解": "vaapi", "附加选项": "不是列表",
                                 "可流畅播放": "yes"}, ensure_ascii=False))
        顾问 = self.造顾问(本地模型=模型)
        建议 = 顾问.建议起播参数(造探测())
        self.assertEqual(建议["网络缓存毫秒"], 20000)     # 上限
        self.assertEqual(建议["起播等待秒"], 20.0)        # 上限
        self.assertIsInstance(建议["附加选项"], list)     # 非法类型 → 退回规则选项
        self.assertIn(":clock-jitter=0", 建议["附加选项"])

    def test_AI不能编造本机没有的硬解(self):
        模型 = 假本地模型(json.dumps({"硬解": "别的方式"}, ensure_ascii=False))
        顾问 = self.造顾问(本地模型=模型)
        建议 = 顾问.建议起播参数(造探测(本机硬解=["vaapi", "none"]))
        self.assertEqual(建议["硬解"], "vaapi")
        self.assertTrue(any("不在本机可用列表" in x for x in self.日志))

    def test_AI与实测结论矛盾时整条作废(self):
        # 真机实测：1.5B 模型把 90Mbps÷25Mbps（3.57 倍）算成"带宽不够"，
        # 于是既报假警又把缓存从 6 秒抬到 20 秒 —— 这种 AI 建议必须整条丢掉
        模型 = 假本地模型(json.dumps({"可流畅播放": False, "网络缓存毫秒": 20000},
                                 ensure_ascii=False))
        顾问 = self.造顾问(本地模型=模型)
        建议 = 顾问.建议起播参数(造探测())
        self.assertEqual(建议["来源"], "规则")
        self.assertTrue(建议["可流畅播放"])
        self.assertEqual(建议["网络缓存毫秒"], 6000)      # 用规则的 6 秒
        self.assertIn("矛盾", 建议["理由"])
        self.assertTrue(any("与实测矛盾" in x for x in self.日志))
        self.assertEqual(顾问.获取统计()["AI结论矛盾次数"], 1)

    def test_AI说能播也不能推翻带宽事实(self):
        # 20Mbps 跑 25Mbps 的 4K：AI 说 true 也必须保持 false
        模型 = 假本地模型(json.dumps({"可流畅播放": True, "网络缓存毫秒": 3000},
                                 ensure_ascii=False))
        顾问 = self.造顾问(本地模型=模型)
        建议 = 顾问.建议起播参数(造探测(实测带宽bps=20_000_000))
        self.assertFalse(建议["可流畅播放"])
        self.assertEqual(建议["来源"], "规则")            # 结论矛盾 → 整条作废
        self.assertEqual(建议["网络缓存毫秒"], 20000)     # 规则拉满，不是 AI 的 3000

    def test_AI文案不是字符串时用规则文案(self):
        # 真机实测：模型把 风险 写成嵌套 dict，str() 出来是给用户看的 Python repr
        模型 = 假本地模型(json.dumps(
            {"网络缓存毫秒": 7000, "可流畅播放": True,
             "风险": {"带宽": "25000000bps", "分辨率": "3840x2160"},
             "理由": ["带宽够", "缓存取 7 秒"]}, ensure_ascii=False))
        顾问 = self.造顾问(本地模型=模型)
        建议 = 顾问.建议起播参数(造探测())
        self.assertEqual(建议["来源"], "本地模型")
        self.assertEqual(建议["网络缓存毫秒"], 7000)
        self.assertNotIn("{", 建议["风险"])               # 不能出现 dict 的 repr
        self.assertNotIn("25000000bps", 建议["风险"])
        self.assertIn("带宽够", 建议["风险"])             # 用的是规则文案
        self.assertTrue(any("不是字符串" in x for x in self.日志))

    def test_坏JSON回落到规则且日志有说明(self):
        模型 = 假本地模型("我觉得这个视频应该加一点缓存，大概八秒左右吧")
        顾问 = self.造顾问(本地模型=模型)
        建议 = 顾问.建议起播参数(造探测())
        self.assertEqual(建议["来源"], "规则")
        self.assertEqual(建议["网络缓存毫秒"], 6000)
        self.assertIn("AI 未参与", 建议["理由"])
        self.assertTrue(any("不是 JSON" in x for x in self.日志))
        统计 = 顾问.获取统计()
        self.assertEqual(统计["规则回退次数"], 1)
        self.assertEqual(统计["AI解析失败次数"], 1)

    def test_AI抛异常被吞掉并回落(self):
        模型 = 假本地模型(异常=RuntimeError("连接被拒绝"))
        顾问 = self.造顾问(本地模型=模型)
        建议 = 顾问.建议起播参数(造探测())          # 不能抛
        self.assertEqual(建议["来源"], "规则")
        self.assertTrue(any("RuntimeError" in x and "连接被拒绝" in x
                          for x in self.日志))
        统计 = 顾问.获取统计()
        self.assertEqual(统计["AI异常次数"], 1)
        self.assertEqual(统计["规则回退次数"], 1)

    def test_本地模型返回失败结果也回落(self):
        模型 = 假本地模型(内容="", 成功=False, 错误="模型没加载")
        顾问 = self.造顾问(本地模型=模型)
        建议 = 顾问.建议起播参数(造探测())
        self.assertEqual(建议["来源"], "规则")
        self.assertIn("模型没加载", 建议["理由"])

    def test_本地模型挂了转云端(self):
        模型 = 假本地模型(异常=TimeoutError("本地超时"))
        云端 = 假云端助手({"网络缓存毫秒": 11000, "理由": "云端判断",
                      "附加选项": [":clock-jitter=0"]})
        顾问 = self.造顾问(本地模型=模型, AI助手=云端)
        建议 = 顾问.建议起播参数(造探测())
        self.assertEqual(建议["来源"], "云端")
        self.assertEqual(建议["网络缓存毫秒"], 11000)
        self.assertEqual(云端.调用次数, 1)
        self.assertEqual(顾问.获取统计()["云端建议次数"], 1)

    def test_云端抛异常回规则(self):
        云端 = 假云端助手(异常=ValueError("没密钥"))
        顾问 = self.造顾问(AI助手=云端)
        建议 = 顾问.建议起播参数(造探测())
        self.assertEqual(建议["来源"], "规则")
        self.assertTrue(any("ValueError" in x for x in self.日志))

    def test_没有AI时日志说明未配置(self):
        顾问 = self.造顾问()
        建议 = 顾问.建议起播参数(造探测())
        self.assertEqual(建议["来源"], "规则")
        self.assertTrue(any("没有可用的 AI" in x for x in self.日志))

    def test_关闭AI时一次也不问模型(self):
        模型 = 假本地模型("我不会被调用")
        顾问 = self.造顾问(本地模型=模型, 启用AI=False)
        建议 = 顾问.建议起播参数(造探测())
        self.assertEqual(建议["来源"], "规则")
        self.assertEqual(模型.调用次数, 0)

    def test_简陋替身不接受最大tokens也能用(self):
        模型 = 假本地模型_无tokens参数(json.dumps({"网络缓存毫秒": 7000},
                                          ensure_ascii=False))
        顾问 = self.造顾问(本地模型=模型)
        建议 = 顾问.建议起播参数(造探测())
        self.assertEqual(建议["来源"], "本地模型")
        self.assertEqual(建议["网络缓存毫秒"], 7000)

    def test_纯字符串返回也认(self):
        模型 = 假本地模型("```json\n{\"硬解\": \"none\"}\n```")
        顾问 = self.造顾问(本地模型=模型)
        建议 = 顾问.建议起播参数(造探测())
        self.assertEqual(建议["来源"], "本地模型")
        self.assertEqual(建议["硬解"], "none")

    def test_真机式截断输出仍算AI成功(self):
        # 与真机 deepseek-r1:1.5b 输出同形：JSON 被截断在 512 tokens
        截断 = ('```json\n{   "网络缓存毫秒": 40000,   "起播等待秒": 2,   '
              '"硬解": "vaapi",   "附加选项": [":clock-jitter=0"],   '
              '"可流畅播放": true,   "风险": "4K 25Mbps，实测 90Mbps，够')
        模型 = 假本地模型(截断)
        顾问 = self.造顾问(本地模型=模型)
        建议 = 顾问.建议起播参数(造探测())
        self.assertEqual(建议["来源"], "本地模型")          # 救回来就当 AI 成功
        self.assertEqual(建议["网络缓存毫秒"], 20000)       # 40000 被钳到上限
        self.assertEqual(建议["起播等待秒"], 2.0)
        self.assertEqual(建议["硬解"], "vaapi")
        self.assertIn(":network-caching=20000", 建议["附加选项"])
        self.assertEqual(模型.调用次数, 1)

    def test_正文被截断时改用思考通道的完整草稿(self):
        # 真机实测形态：内容通道被 512 tokens 截断，thinking 里却有一份完整 JSON
        模型 = 假本地模型(
            内容='```json\n{\n  "网络缓存毫秒": 6000,\n  "起播等待秒": 2,\n'
               '  "硬解": "vaapi",\n  "附加',
            推理内容='分析过程略。最终 JSON：\n{\n  "网络缓存毫秒": 9000,\n'
                '  "起播等待秒": 3,\n  "硬解": "vaapi",\n'
                '  "附加选项": [":clock-jitter=0"],\n'
                '  "可流畅播放": true,\n  "风险": "90Mbps 够用",\n'
                '  "理由": "带宽是码率的 3.6 倍"\n}')
        顾问 = self.造顾问(本地模型=模型)
        建议 = 顾问.建议起播参数(造探测())
        self.assertEqual(建议["来源"], "本地模型")
        self.assertEqual(建议["网络缓存毫秒"], 9000)      # 取字段更全的那份
        self.assertEqual(建议["起播等待秒"], 3.0)
        self.assertEqual(建议["风险"], "90Mbps 够用")
        self.assertEqual(建议["理由"], "带宽是码率的 3.6 倍")

    def test_正文完整时优先用正文(self):
        模型 = 假本地模型(内容='{"网络缓存毫秒": 7000, "风险": "正文的风险"}',
                     推理内容='{"网络缓存毫秒": 3000, "风险": "思考里的风险"}')
        顾问 = self.造顾问(本地模型=模型)
        建议 = 顾问.建议起播参数(造探测())
        self.assertEqual(建议["网络缓存毫秒"], 7000)

    def test_提示词不喂现成文案也不含占位语(self):
        # 教训来自真机：提示词里写 "风险": "一句人话" 时，1.5B 模型会原样抄回来
        模型 = 假本地模型('{"网络缓存毫秒": 6000}')
        顾问 = self.造顾问(本地模型=模型)
        建议 = 顾问.建议起播参数(造探测())
        提示 = 模型.收到的系统提示 + 模型.收到的用户消息
        self.assertNotIn("一句人话", 提示)
        # 规则基线的风险/理由文案不喂给模型（模型只拿数值，文案自己写）
        self.assertNotIn(建议["风险"], 提示)
        self.assertIn("网络缓存毫秒", 提示)                # 但数值要给
        self.assertIn("带宽是码率的几倍", 提示)            # 关键比值替模型算好
        self.assertIn("实测带宽Mbps", 提示)                # bps 换 Mbps，小模型少算错
        self.assertNotIn("实测带宽bps", 提示)
        self.assertIn("不要照抄", 提示)


# ==================== 四、学习库 ====================


class 测试学习库(顾问基类):

    def test_命中学习库优先于AI(self):
        顾问 = self.造顾问(启用AI=False)
        探测 = 造探测()
        建议 = 顾问.建议起播参数(探测)
        # 记两次好结果（样本数门槛是 2）
        for _ in range(2):
            顾问.记录效果("quark|2160p|hevc", dict(建议, 网络缓存毫秒=16000,
                                          起播等待秒=5.0), {"是否卡顿": False,
                                                      "平均码率bps": 24_000_000})
        模型 = 假本地模型(json.dumps({"网络缓存毫秒": 3000}, ensure_ascii=False))
        顾问2 = self.造顾问(本地模型=模型)
        复用 = 顾问2.建议起播参数(探测)
        self.assertEqual(复用["来源"], "学习库")
        self.assertEqual(复用["网络缓存毫秒"], 16000)     # 用历史参数，不是 AI 的 3000
        self.assertEqual(复用["起播等待秒"], 5.0)
        self.assertEqual(模型.调用次数, 0)                # 命中就不问模型
        self.assertIn("学习库命中", 复用["理由"])
        self.assertIn("卡顿率 0%", 复用["理由"])

    def test_学习库按网盘加分辨率档隔离(self):
        顾问 = self.造顾问(启用AI=False)
        建议 = 顾问.建议起播参数(造探测())
        for _ in range(2):
            顾问.记录效果("quark|2160p", 建议, {"是否卡顿": False})
        # 同网盘不同档、同档不同网盘都不该命中
        self.assertEqual(顾问.建议起播参数(造探测(分辨率="1280x720"))["来源"], "规则")
        self.assertEqual(顾问.建议起播参数(造探测(网盘="baidu"))["来源"], "规则")

    def test_样本不足不复用(self):
        顾问 = self.造顾问(启用AI=False)
        建议 = 顾问.建议起播参数(造探测())
        顾问.记录效果("quark|2160p|hevc", dict(建议, 网络缓存毫秒=16000),
                   {"是否卡顿": False})
        # 只记了 1 次 → 不够门槛，仍走规则
        self.assertEqual(顾问.建议起播参数(造探测())["来源"], "规则")

    def test_卡顿率太高不复用(self):
        顾问 = self.造顾问(启用AI=False)
        建议 = 顾问.建议起播参数(造探测())
        顾问.记录效果("quark|2160p|hevc", 建议, {"是否卡顿": True})
        顾问.记录效果("quark|2160p|hevc", 建议, {"是否卡顿": True})
        顾问.记录效果("quark|2160p|hevc", 建议, {"是否卡顿": False})
        self.assertEqual(顾问.建议起播参数(造探测())["来源"], "规则")

    def test_精确键没命中时退化到网盘加档(self):
        顾问 = self.造顾问(启用AI=False)
        建议 = 顾问.建议起播参数(造探测())
        for _ in range(2):
            顾问.记录效果("quark|2160p", dict(建议, 网络缓存毫秒=12000),
                     {"是否卡顿": False})
        # 探测带 hevc（精确键 quark|2160p|hevc 无记录）→ 退化键命中
        复用 = 顾问.建议起播参数(造探测())
        self.assertEqual(复用["来源"], "学习库")
        self.assertEqual(复用["网络缓存毫秒"], 12000)

    def test_学习参数遇到硬件变化要丢弃硬解(self):
        顾问 = self.造顾问(启用AI=False)
        建议 = 顾问.建议起播参数(造探测())
        for _ in range(2):
            顾问.记录效果("quark|2160p|hevc", 建议, {"是否卡顿": False})
        # 换了一台没有 vaapi 的机器
        换机 = 顾问.建议起播参数(造探测(本机硬解=["none"]))
        self.assertEqual(换机["来源"], "学习库")
        self.assertEqual(换机["硬解"], "none")            # 不能照抄 vaapi
        self.assertIn(":avcodec-hw=none", 换机["附加选项"])

    def test_学习参数遇到带宽更紧时缓存不能更小(self):
        顾问 = self.造顾问(启用AI=False)
        建议 = 顾问.建议起播参数(造探测())
        for _ in range(2):
            顾问.记录效果("quark|2160p|hevc", dict(建议, 网络缓存毫秒=4000),
                     {"是否卡顿": False})
        复用 = 顾问.建议起播参数(造探测(实测带宽bps=10_000_000))
        self.assertEqual(复用["来源"], "学习库")
        self.assertEqual(复用["网络缓存毫秒"], 20000)     # 取规则要求的更大值
        self.assertFalse(复用["可流畅播放"])              # 流畅与否永远按当前实测
        self.assertIn("带宽不够", 复用["风险"])

    def test_学习库命中后仍记新结果(self):
        顾问 = self.造顾问(启用AI=False)
        建议 = 顾问.建议起播参数(造探测())
        for _ in range(2):
            顾问.记录效果("quark|2160p|hevc", 建议, {"是否卡顿": False})
        命中 = 顾问.建议起播参数(造探测())
        顾问.记录效果("quark|2160p|hevc", 命中, {"是否卡顿": True, "丢帧": 40})
        self.assertEqual(顾问.获取统计()["总记录数"], 3)
        self.assertEqual(顾问.获取统计()["卡顿记录数"], 1)

    def test_记录效果落盘并读得回来(self):
        顾问 = self.造顾问(启用AI=False)
        建议 = 顾问.建议起播参数(造探测())
        顾问.记录效果("quark|2160p|hevc", 建议,
                   {"是否卡顿": False, "平均码率bps": 24_000_000,
                    "已播秒": 300, "缓冲等待次数": 0, "丢帧": 0})
        self.assertTrue(os.path.exists(self.库路径))       # 真的落盘了
        with self.连接() as 连接:
            行 = 连接.execute("SELECT 键, 网盘, 分辨率档, 是否卡顿, 平均码率bps,"
                            " 创建时间 FROM 播放学习").fetchall()
        self.assertEqual(len(行), 1)
        self.assertEqual(行[0]["键"], "quark|2160p|hevc")
        self.assertEqual(行[0]["网盘"], "quark")
        self.assertEqual(行[0]["分辨率档"], "2160p")
        self.assertEqual(行[0]["是否卡顿"], 0)
        self.assertAlmostEqual(行[0]["平均码率bps"], 24_000_000)
        self.assertIn("年", 行[0]["创建时间"])

        记录 = 顾问.获取学习记录()
        self.assertEqual(len(记录), 1)
        self.assertEqual(记录[0]["参数"]["网络缓存毫秒"], 建议["网络缓存毫秒"])
        self.assertEqual(记录[0]["指标"]["已播秒"], 300)
        self.assertIs(记录[0]["是否卡顿"], False)
        # 键过滤：不存在的键查不到，空键查全部
        self.assertEqual(顾问.获取学习记录("baidu|1080p"), [])
        self.assertEqual(len(顾问.获取学习记录("")), 1)

    def test_统计汇总(self):
        顾问 = self.造顾问(启用AI=False)
        建议 = 顾问.建议起播参数(造探测())
        顾问.记录效果("quark|2160p|hevc", 建议,
                   {"是否卡顿": False, "平均码率bps": 20_000_000})
        顾问.记录效果("quark|1080p|h264", 建议,
                   {"是否卡顿": True, "平均码率bps": 8_000_000, "丢帧": 50})
        顾问.建议起播参数(造探测())
        统计 = 顾问.获取统计()
        self.assertTrue(统计["库可用"])
        self.assertEqual(统计["总记录数"], 2)
        self.assertEqual(统计["不同键数"], 2)
        self.assertEqual(统计["卡顿记录数"], 1)
        self.assertAlmostEqual(统计["卡顿率"], 0.5)
        self.assertAlmostEqual(统计["平均码率bps"], 14_000_000)
        self.assertIn("quark", 统计["按网盘"])
        self.assertEqual(统计["按网盘"]["quark"]["记录数"], 2)
        self.assertAlmostEqual(统计["按网盘"]["quark"]["卡顿率"], 0.5)
        self.assertEqual(统计["按分辨率档"]["2160p"]["记录数"], 1)
        self.assertEqual(统计["记录卡顿条数"], 1)
        self.assertEqual(统计["记录流畅条数"], 1)
        self.assertEqual(统计["建议次数"], 2)
        self.assertNotEqual(统计["最近记录时间"], "")

    def test_指标没写是否卡顿时自动推断(self):
        顾问 = self.造顾问(启用AI=False)
        顾问.记录效果("quark|2160p", {"网络缓存毫秒": 8000},
                   {"缓冲等待次数": 3, "丢帧": 30})
        self.assertTrue(顾问.获取学习记录()[0]["是否卡顿"])

    def test_空键不记录(self):
        顾问 = self.造顾问(启用AI=False)
        顾问.记录效果("", {}, {"是否卡顿": False})
        顾问.记录效果("   ", {}, {"是否卡顿": False})
        self.assertEqual(顾问.获取统计()["总记录数"], 0)
        self.assertTrue(any("失败" in x for x in self.日志))

    def test_学习库坏了也不影响播放(self):
        # 把一个**文件**当成目录用 → 建库必然失败
        占位 = os.path.join(self._临时.name, "占位文件")
        Path(占位).write_text("not a dir", encoding="utf-8")
        坏路径 = os.path.join(占位, "学习.db")
        顾问 = 播放顾问(日志回调=self.日志.append, 学习库路径=坏路径,
                    启用AI=False)
        self.addCleanup(顾问.关闭)
        建议 = 顾问.建议起播参数(造探测())          # 不抛异常
        self.assertEqual(建议["来源"], "规则")
        self.assertEqual(顾问.获取学习记录(), [])
        self.assertFalse(顾问.获取统计()["库可用"])
        顾问.记录效果("quark|2160p", 建议, {"是否卡顿": False})   # 也不抛
        self.assertTrue(any("学习库不可用" in x for x in self.日志))

    def test_学习库保留上限(self):
        顾问 = self.造顾问(启用AI=False)
        顾问._库.最多保留 = 3                            # 白盒调小，免得写 5000 次
        for i in range(5):
            顾问.记录效果("quark|2160p", {"网络缓存毫秒": 8000 + i},
                     {"是否卡顿": False})
        记录 = 顾问.获取学习记录()
        self.assertEqual(len(记录), 3)
        self.assertEqual(记录[0]["参数"]["网络缓存毫秒"], 8004)   # 留最新 3 条


# ==================== 五、AI 优化播放（诊断卡顿） ====================


class 测试诊断卡顿(顾问基类):

    def 建议(self, **覆盖):
        """拿一份规则建议参数当"当前参数"。"""
        return self.造顾问(启用AI=False).建议起播参数(造探测(**覆盖))

    def test_缓冲不足加大缓存与预缓冲(self):
        顾问 = self.造顾问(启用AI=False)
        当前 = self.建议()
        诊断 = 顾问.诊断卡顿(造采样(丢帧=12, 缓冲等待次数=3, 当前缓冲秒=1.2,
                              硬解生效=True, CPU占用=0.2), 当前,
                        {"分辨率": "3840x2160", "本机硬解": ["vaapi", "none"]})
        self.assertTrue(诊断["需要调整"])
        self.assertIn("加大网络缓存到 10000ms", 诊断["动作"])
        self.assertIn("起播预缓冲提高到 4 秒", 诊断["动作"])
        self.assertEqual(诊断["新参数"]["网络缓存毫秒"], 10000)
        self.assertEqual(诊断["新参数"]["起播等待秒"], 4.0)
        self.assertIn("缓冲等待 3 次", 诊断["风险"])
        self.assertEqual(诊断["来源"], "规则")

    def test_缓存已经拉满时不再往上加(self):
        顾问 = self.造顾问(启用AI=False)
        当前 = dict(self.建议(实测带宽bps=20_000_000),
                  网络缓存毫秒=20000, 起播等待秒=4.0)
        诊断 = 顾问.诊断卡顿(造采样(缓冲等待次数=4, 当前缓冲秒=0.3), 当前, {})
        self.assertEqual(诊断["新参数"]["网络缓存毫秒"], 20000)
        self.assertNotIn("加大网络缓存到 20000ms", 诊断["动作"])
        # 缓存已经拉满 → 只能建议从码率/片源上想办法
        self.assertTrue(any("已拉满" in x and "1080p" in x for x in 诊断["动作"]))
        self.assertIn("起播预缓冲提高到 6 秒", 诊断["动作"])
        self.assertEqual(诊断["新参数"]["起播等待秒"], 6.0)

    def test_硬解没生效且有替代方案(self):
        顾问 = self.造顾问(启用AI=False)
        当前 = dict(self.建议(), 硬解="vaapi")
        诊断 = 顾问.诊断卡顿(造采样(硬解生效=False, CPU占用=0.7), 当前,
                        {"本机硬解": ["vaapi", "cuda", "none"]})
        self.assertIn("强制硬解 cuda", 诊断["动作"])
        self.assertEqual(诊断["新参数"]["硬解"], "cuda")
        self.assertIn(":avcodec-hw=cuda", 诊断["新参数"]["附加选项"])

    def test_当前是软解时强制用上hvaapi(self):
        顾问 = self.造顾问(启用AI=False)
        当前 = dict(self.建议(), 硬解="none")
        诊断 = 顾问.诊断卡顿(造采样(硬解生效=False, CPU占用=0.8), 当前,
                        {"本机硬解": ["vaapi", "none"]})
        self.assertIn("强制硬解 vaapi", 诊断["动作"])
        self.assertEqual(诊断["新参数"]["硬解"], "vaapi")
        self.assertIn("关闭反交错", 诊断["动作"])          # CPU 0.8 ≥ 0.7
        self.assertIn(":deinterlace=-1", 诊断["新参数"]["附加选项"])

    def test_硬解没生效但本机确实没有硬解(self):
        顾问 = self.造顾问(启用AI=False)
        当前 = dict(self.建议(本机硬解=["none"]), 硬解="none")
        诊断 = 顾问.诊断卡顿(造采样(硬解生效=False, CPU占用=0.9), 当前,
                        {"本机硬解": ["none"]})
        self.assertIn("本机没有可用硬解，只能软解", 诊断["动作"])
        self.assertIn("没有硬解可用", 诊断["风险"])

    def test_硬解信息缺失时不瞎猜后端(self):
        顾问 = self.造顾问(启用AI=False)
        当前 = dict(self.建议(本机硬解=[]), 硬解="none")
        诊断 = 顾问.诊断卡顿(造采样(硬解生效=False), 当前, {})
        self.assertNotIn("强制硬解 none", 诊断["动作"])
        self.assertTrue(any("请检查播放器硬解配置" in x for x in 诊断["动作"]))
        self.assertEqual(诊断["新参数"]["硬解"], "none")

    def test_唯一硬解没生效时显式指定重试(self):
        顾问 = self.造顾问(启用AI=False)
        当前 = dict(self.建议(本机硬解=["vaapi", "none"]), 硬解="vaapi")
        诊断 = 顾问.诊断卡顿(造采样(硬解生效=False), 当前,
                        {"本机硬解": ["vaapi", "none"]})
        self.assertIn("硬解 vaapi 未生效，显式指定 :avcodec-hw=vaapi 重试",
                   诊断["动作"])
        self.assertIn(":avcodec-hw=vaapi", 诊断["新参数"]["附加选项"])

    def test_丢帧但缓冲充足且码率超带宽建议降画质(self):
        顾问 = self.造顾问(启用AI=False)
        当前 = self.建议()
        诊断 = 顾问.诊断卡顿(造采样(丢帧=60, 当前缓冲秒=6.0, 缓冲等待次数=0,
                              平均码率bps=30_000_000),
                        当前, {"分辨率": "3840x2160", "实测带宽bps": 25_000_000,
                             "本机硬解": ["vaapi", "none"]})
        self.assertTrue(any("建议切 1080p" in x for x in 诊断["动作"]))
        self.assertIn("带宽", 诊断["风险"])
        # 网络没问题，缓存不该乱动
        self.assertEqual(诊断["新参数"]["网络缓存毫秒"], 当前["网络缓存毫秒"])

    def test_丢帧但缓冲充足且带宽够判解码跟不上(self):
        顾问 = self.造顾问(启用AI=False)
        当前 = self.建议()
        诊断 = 顾问.诊断卡顿(造采样(丢帧=60, 当前缓冲秒=6.0, 缓冲等待次数=0,
                              平均码率bps=25_000_000),
                        当前, {"分辨率": "3840x2160", "实测带宽bps": 90_000_000,
                             "本机硬解": ["vaapi", "none"]})
        self.assertTrue(any("解码能力不足" in x for x in 诊断["动作"]))
        self.assertIn("瓶颈在解码", 诊断["风险"])

    def test_解码丢帧多时提示换硬解方式(self):
        顾问 = self.造顾问(启用AI=False)
        当前 = self.建议()
        诊断 = 顾问.诊断卡顿(造采样(丢帧=0, 解码丢帧=30, 平均码率bps=10_000_000),
                        当前, {"本机硬解": ["vaapi", "none"]})
        self.assertTrue(any("解码丢帧 30 个" in x for x in 诊断["动作"]))

    def test_CPU极高时提示关后台(self):
        顾问 = self.造顾问(启用AI=False)
        当前 = self.建议()
        诊断 = 顾问.诊断卡顿(造采样(CPU占用=0.95), 当前, {})
        self.assertTrue(any("CPU 占用 95%" in x for x in 诊断["动作"]))
        self.assertIn("关闭反交错", 诊断["动作"])

    def test_一切正常时不用调整(self):
        顾问 = self.造顾问(启用AI=False)
        当前 = self.建议()
        诊断 = 顾问.诊断卡顿(造采样(), 当前, {"本机硬解": ["vaapi", "none"]})
        self.assertFalse(诊断["需要调整"])
        self.assertEqual(诊断["动作"], [])
        self.assertEqual(诊断["新参数"]["网络缓存毫秒"], 当前["网络缓存毫秒"])
        self.assertEqual(诊断["新参数"]["硬解"], 当前["硬解"])
        self.assertIn("正常范围", 诊断["理由"])
        self.assertIn("暂无明显风险", 诊断["风险"])
        self.assertEqual(set(诊断), {"需要调整", "动作", "新参数", "理由",
                                 "风险", "来源"})
        self.assertEqual(set(诊断["新参数"]), {"网络缓存毫秒", "起播等待秒",
                                          "硬解", "附加选项", "可流畅播放",
                                          "风险", "理由", "来源"})

    def test_当前正在缓冲就该加大缓存(self):
        # 播放核心是"此刻在缓冲就给 1"（累计值很小），所以状态字段也算信号
        顾问 = self.造顾问(启用AI=False)
        当前 = self.建议()
        诊断 = 顾问.诊断卡顿(造采样(缓冲等待次数=1, 当前缓冲秒=4.0,
                              状态="缓冲中"),
                        当前, {"本机硬解": ["vaapi", "none"]})
        self.assertTrue(诊断["需要调整"])
        self.assertIn("加大网络缓存到 10000ms", 诊断["动作"])
        # 同样只有一个等待次数但没有"正在缓冲"信号 → 不动参数（累计 1 次不值得重载）
        平静 = 顾问.诊断卡顿(造采样(缓冲等待次数=1, 当前缓冲秒=4.0,
                             状态="播放中"), 当前,
                        {"本机硬解": ["vaapi", "none"]})
        self.assertFalse(平静["需要调整"])

    def test_当前参数残缺也能诊断(self):
        顾问 = self.造顾问(启用AI=False)
        诊断 = 顾问.诊断卡顿({"丢帧": 12, "已播秒": 60.0}, {"网络缓存毫秒": 4000},
                        None)
        self.assertEqual(诊断["新参数"]["网络缓存毫秒"], 4000)  # 没触发缓冲分支
        self.assertEqual(set(诊断["新参数"]), {"网络缓存毫秒", "起播等待秒",
                                          "硬解", "附加选项", "可流畅播放",
                                          "风险", "理由", "来源"})
        # 刚刚起播（已播 2 秒）时不按速率放大丢帧结论
        刚起播 = 顾问.诊断卡顿({"丢帧": 12, "已播秒": 2.0, "当前缓冲秒": 8.0},
                          {"网络缓存毫秒": 8000}, None)
        self.assertFalse(刚起播["需要调整"])

    def test_AI成功覆盖诊断(self):
        AI返回 = {"需要调整": True, "动作": ["把缓存提到 15000ms", "重启解码器"],
                "新参数": {"网络缓存毫秒": 15000, "硬解": "cuda",
                         "附加选项": [":clock-jitter=0"]},
                "理由": "AI 诊断理由", "风险": "AI 诊断风险"}
        顾问 = self.造顾问(本地模型=假本地模型(json.dumps(AI返回, ensure_ascii=False)))
        当前 = self.建议()
        诊断 = 顾问.诊断卡顿(造采样(), 当前,
                        {"本机硬解": ["vaapi", "cuda", "none"]})
        self.assertEqual(诊断["来源"], "本地模型")
        self.assertTrue(诊断["需要调整"])
        self.assertEqual(诊断["动作"], ["把缓存提到 15000ms", "重启解码器"])
        self.assertEqual(诊断["新参数"]["网络缓存毫秒"], 15000)
        self.assertEqual(诊断["新参数"]["硬解"], "cuda")
        self.assertEqual(诊断["理由"], "AI 诊断理由")
        self.assertEqual(诊断["风险"], "AI 诊断风险")
        self.assertIn(":network-caching=15000", 诊断["新参数"]["附加选项"])

    def test_AI诊断编造硬解与非法值被丢弃(self):
        AI返回 = {"需要调整": True, "动作": [],
                "新参数": {"硬解": "魔法硬解", "网络缓存毫秒": "abc",
                         "可流畅播放": False}}
        顾问 = self.造顾问(本地模型=假本地模型(json.dumps(AI返回, ensure_ascii=False)))
        当前 = self.建议()
        诊断 = 顾问.诊断卡顿(造采样(), 当前, {"本机硬解": ["vaapi", "none"]})
        self.assertEqual(诊断["新参数"]["硬解"], 当前["硬解"])
        self.assertEqual(诊断["新参数"]["网络缓存毫秒"], 当前["网络缓存毫秒"])
        # 实测结论同样不许 AI 改写（新参数里也要保持一致）
        self.assertEqual(诊断["新参数"]["可流畅播放"], 当前["可流畅播放"])
        # AI 说"需要调整"却什么都没改 → 判定不用调整
        self.assertFalse(诊断["需要调整"])
        self.assertEqual(诊断["动作"], [])

    def test_AI诊断只改参数会自动补人话动作(self):
        AI返回 = {"需要调整": True, "新参数": {"网络缓存毫秒": 12000}}
        顾问 = self.造顾问(本地模型=假本地模型(json.dumps(AI返回, ensure_ascii=False)))
        当前 = self.建议()
        诊断 = 顾问.诊断卡顿(造采样(), 当前, {"本机硬解": ["vaapi", "none"]})
        self.assertTrue(诊断["需要调整"])
        self.assertTrue(any("网络缓存" in x and "12000ms" in x
                          for x in 诊断["动作"]))

    def test_AI不许在缓冲不足时把缓存改小(self):
        AI返回 = {"需要调整": True, "动作": [], "新参数": {"网络缓存毫秒": 2000}}
        顾问 = self.造顾问(本地模型=假本地模型(json.dumps(AI返回, ensure_ascii=False)))
        当前 = self.建议()
        诊断 = 顾问.诊断卡顿(造采样(缓冲等待次数=3, 当前缓冲秒=0.5), 当前,
                        {"本机硬解": ["vaapi", "none"]})
        # 规则基线要 10000ms，AI 想降到 2000 必须被拦下
        self.assertEqual(诊断["新参数"]["网络缓存毫秒"], 10000)
        self.assertTrue(any("至少保持 10000ms" in x for x in 诊断["动作"]))

    def test_AI诊断坏JSON回落规则(self):
        顾问 = self.造顾问(本地模型=假本地模型("建议把缓存调大一点，谢谢"))
        当前 = self.建议()
        诊断 = 顾问.诊断卡顿(造采样(缓冲等待次数=3, 当前缓冲秒=1.0), 当前,
                        {"本机硬解": ["vaapi", "none"]})
        self.assertEqual(诊断["来源"], "规则")
        self.assertIn("加大网络缓存到 10000ms", 诊断["动作"])
        self.assertIn("AI 未参与", 诊断["理由"])
        self.assertTrue(any("不是 JSON" in x for x in self.日志))

    def test_AI诊断抛异常回落规则(self):
        顾问 = self.造顾问(本地模型=假本地模型(异常=OSError("socket 挂了")))
        当前 = self.建议()
        诊断 = 顾问.诊断卡顿(造采样(), 当前, {})
        self.assertEqual(诊断["来源"], "规则")
        self.assertFalse(诊断["需要调整"])
        # 降到"只报决策"之后：不需要调整时**不写日志**（这是刻意的降噪），
        # 但统计里必须仍然记下这次 AI 异常
        self.assertFalse(any("OSError" in x for x in self.日志),
                         "不需要调整时不该刷日志（用户要求降噪）")
        self.assertEqual(顾问.获取统计()["AI异常次数"], 1)

    def test_启用AI为假时只走规则(self):
        模型 = 假本地模型("不该被调用")
        顾问 = self.造顾问(本地模型=模型, 启用AI=False)
        诊断 = 顾问.诊断卡顿(造采样(), self.建议(), {})
        self.assertEqual(诊断["来源"], "规则")
        self.assertEqual(模型.调用次数, 0)
        统计 = 顾问.获取统计()
        self.assertEqual(统计["诊断次数"], 1)
        self.assertEqual(统计["规则诊断次数"], 1)

    def test_诊断后可以记录并形成闭环(self):
        顾问 = self.造顾问(启用AI=False)
        当前 = self.建议()
        诊断 = 顾问.诊断卡顿(造采样(丢帧=12, 缓冲等待次数=3, 当前缓冲秒=1.2),
                        当前, {"本机硬解": ["vaapi", "none"]})
        顾问.记录效果("quark|2160p|hevc", 诊断["新参数"],
                   {"是否卡顿": False, "平均码率bps": 24_000_000})
        # 再记一次同类好结果，下次起播就该命中学习库
        顾问.记录效果("quark|2160p|hevc", 诊断["新参数"],
                   {"是否卡顿": False, "平均码率bps": 24_000_000})
        复用 = 顾问.建议起播参数(造探测())
        self.assertEqual(复用["来源"], "学习库")
        self.assertEqual(复用["网络缓存毫秒"], 诊断["新参数"]["网络缓存毫秒"])
        self.assertEqual(顾问.获取统计()["诊断需调整次数"], 1)
        self.assertEqual(顾问.获取统计()["学习库命中次数"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
