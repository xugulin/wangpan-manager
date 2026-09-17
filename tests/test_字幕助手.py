# tests/test_字幕助手.py
"""AI 字幕助手单测（V8_3 新增，**全离线**）。

不联网、不起真实服务：本地模型 / 云端 AI / 语音识别器全部是假对象，
只写 tempfile 临时目录，因此可以在没网、没有 Ollama 的机器上跑。

覆盖：
  * SRT 解析容错（CRLF/BOM/缺序号/点或逗号/多行/空行混乱/坏时间行）；
  * 生成与往返一致、重排序号、UTF-8(BOM)/GBK 读写；
  * 翻译：时间戳零漂移、分批、重试、坏 JSON/条数不符/空返回/异常 → 保留原文；
  * 翻译：本地优先、云端兜底、规则降级、并发不乱序、进度回调、不动入参；
  * 总结：AI 成功（章节排序/时间自洽/夹在片长内）与规则降级；
  * 语音识别结果转条目、生成字幕（鸭子类型 + 最小签名 + 异常兜底）；
  * 工具：纯文本、合并短句、无字幕判定；模块不依赖 Qt。
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

项目根 = Path(__file__).resolve().parents[1]
if str(项目根) not in sys.path:
    sys.path.insert(0, str(项目根))

from v8_3.AI.本地模型 import 对话结果          # 复用真实结果类型，证明鸭子类型兼容
from v8_3.AI.字幕助手 import (字幕助手, 字幕条目, 解析SRT, 生成SRT,
                          读取字幕文件, 写出字幕文件, 时间戳转秒, 秒转时间戳,
                          时钟文本)


# ============================================================ 测试替身

class 假状态:
    """冒充 ``本地模型状态``（只需要 ``可用`` / ``错误``）。"""

    def __init__(self, 可用: bool, 错误: str = ""):
        self.可用 = 可用
        self.错误 = 错误
        self.说明 = 错误


def _取应答(应答, 序号: int, 用户消息: str, 系统提示: str):
    """从"应答脚本"里取第 ``序号`` 次调用的返回值。

    应答可以是：字符串 / dict / list / BaseException / callable / 它们的列表
    （列表按调用顺序取，用完后一直用最后一个）——一个机制覆盖
    "正常 / 坏 JSON / 条数不符 / 空 / 抛异常 / 先失败后成功" 全部场景。
    """
    if isinstance(应答, (list, tuple)):
        if not 应答:
            return None
        项 = 应答[min(序号, len(应答) - 1)]
    else:
        项 = 应答
    if callable(项):
        项 = 项(用户消息, 系统提示)
    return 项


class 假本地模型:
    """鸭子类型的本地模型客户端（``检测()`` + ``对话()``）。"""

    def __init__(self, 应答=None, 可用: bool = True, 延迟: float = 0.0,
                 带检测: bool = True, 配置=None):
        self.应答 = 应答
        self.可用 = 可用
        self.延迟 = 延迟
        self.带检测 = 带检测
        if 配置 is not None:
            self.配置 = 配置
        self.调用: list[dict] = []

    def 检测(self):
        if self.延迟:
            pass
        return 假状态(bool(self.可用), "" if self.可用 else "没检测到本地推理服务")

    # 签名与 v8_3.AI.本地模型.本地模型客户端.对话 一致
    def 对话(self, 用户消息, 系统提示="", *, 模型="", 温度=None, 最大tokens=None):
        self.调用.append({"用户消息": 用户消息, "系统提示": 系统提示,
                       "最大tokens": 最大tokens})
        if self.延迟:
            time.sleep(self.延迟)
        项 = _取应答(self.应答, len(self.调用) - 1, 用户消息, 系统提示)
        if isinstance(项, BaseException):
            raise 项
        if 项 is None:
            return 对话结果(成功=False, 错误="本地模型返回空内容")
        if isinstance(项, 对话结果):
            return 项
        if isinstance(项, (dict, list)):
            项 = json.dumps(项, ensure_ascii=False)
        return 对话结果(成功=True, 内容=str(项), 模型="deepseek-r1:1.5b",
                    提供方="ollama", 用时秒=0.01, 输出tokens=32,
                    费用=0.0, 来源="本地模型")


class 假AI助手:
    """鸭子类型的云端 AI 助手（``是否可用()`` + ``请求结构化策略()``）。"""

    def __init__(self, 应答=None, 可用: bool = True, 延迟: float = 0.0):
        self.应答 = 应答
        self.可用 = 可用
        self.延迟 = 延迟
        self.调用: list[dict] = []

    def 是否可用(self) -> bool:
        return bool(self.可用)

    def 请求结构化策略(self, 系统提示: str, 用户消息: str,
                 强制模型=None) -> dict:
        self.调用.append({"系统提示": 系统提示, "用户消息": 用户消息,
                       "强制模型": 强制模型})
        if self.延迟:
            time.sleep(self.延迟)
        项 = _取应答(self.应答, len(self.调用) - 1, 用户消息, 系统提示)
        if isinstance(项, BaseException):
            raise 项
        return 项


class 假识别器:
    """鸭子类型的语音识别器。"""

    def __init__(self, 段落=None, 可用=(True, ""), 异常=None,
                 最小签名: bool = False):
        self.段落 = 段落 if 段落 is not None else []
        self._可用 = 可用
        self.异常 = 异常
        self.最小签名 = 最小签名
        self.调用: list[dict] = []

    def 可用(self):
        if isinstance(self._可用, tuple):
            return self._可用
        return self._可用

    def _跑(self):
        if self.异常 is not None:
            raise self.异常
        return self.段落

    def 识别(self, 媒体来源, 语言="", 进度回调=None):
        self.调用.append({"媒体来源": 媒体来源, "语言": 语言,
                       "进度回调": 进度回调})
        return self._跑()


class 最小签名识别器(假识别器):
    """只实现 ``识别(媒体来源)`` 的老识别器（模拟签名不匹配的第三方实现）。"""

    def 识别(self, 媒体来源):
        self.调用.append({"媒体来源": 媒体来源})
        return self._跑()


# ============================================================ 小工具

def 造条目(文本们, 起始: float = 0.0, 步长: float = 2.0) -> list[字幕条目]:
    条目们 = []
    for 序, 文本 in enumerate(文本们, start=1):
        开始 = 起始 + (序 - 1) * 步长
        条目们.append(字幕条目(序号=序, 开始秒=开始, 结束秒=开始 + 步长 - 0.5,
                            文本=文本))
    return 条目们


def 时间点们(条目们) -> list[tuple[float, float]]:
    return [(e.开始秒, e.结束秒) for e in 条目们]


def 应答翻译(用户消息, 系统提示):
    """假云端翻译：按输入顺序逐条加前缀，条数严格对齐。"""
    数据 = json.loads(用户消息)
    return {"译文": [f"【译】{t}" for t in 数据["字幕"]]}


def 应答本地翻译(用户消息, 系统提示):
    数据 = json.loads(用户消息)
    return json.dumps({"译文": [f"【本】{t}" for t in 数据["字幕"]]},
                      ensure_ascii=False)


样本SRT = """1
00:00:12,000 --> 00:00:15,500
Hello world
second line

2
00:00:16.0 --> 00:00:18,250
Bye
"""


# ============================================================ 数据模型

class 字幕条目测试(unittest.TestCase):
    def test_追加与复制(self):
        条 = 字幕条目(序号=3, 开始秒=1.5, 结束秒=2.5, 文本="第一行")
        条.追加("第二行")
        条.追加("")
        条.追加("   ")
        self.assertEqual(条.文本, "第一行\n第二行")
        副本 = 条.复制()
        self.assertIsNot(副本, 条)
        副本.追加("第三行")
        self.assertEqual(条.文本, "第一行\n第二行", "复制必须是深拷贝")
        self.assertEqual((副本.序号, 副本.开始秒, 副本.结束秒),
                      (3, 1.5, 2.5))
        self.assertEqual(条.时长秒(), 1.0)
        self.assertFalse(条.是否为空())
        self.assertTrue(字幕条目(文本="  ").是否为空())


# ============================================================ SRT 解析

class SRT解析测试(unittest.TestCase):
    def test_标准解析(self):
        条目们 = 解析SRT(样本SRT)
        self.assertEqual(len(条目们), 2)
        self.assertEqual(条目们[0].序号, 1)
        self.assertEqual(条目们[0].开始秒, 12.0)
        self.assertEqual(条目们[0].结束秒, 15.5)
        self.assertEqual(条目们[0].文本, "Hello world\nsecond line")
        self.assertEqual(条目们[1].开始秒, 16.0)
        self.assertEqual(条目们[1].结束秒, 18.25)

    def test_CRLF与BOM(self):
        文本 = "\ufeff" + 样本SRT.replace("\n", "\r\n")
        条目们 = 解析SRT(文本)
        self.assertEqual(len(条目们), 2)
        self.assertEqual(条目们[0].文本, "Hello world\nsecond line")

    def test_缺序号(self):
        文本 = ("00:00:01,000 --> 00:00:02,000\n甲\n\n"
                "00:00:03,000 --> 00:00:04,000\n乙\n")
        条目们 = 解析SRT(文本)
        self.assertEqual([e.序号 for e in 条目们], [1, 2])
        self.assertEqual([e.文本 for e in 条目们], ["甲", "乙"])

    def test_序号保留但缺号自动补(self):
        文本 = ("7\n00:00:01,000 --> 00:00:02,000\n甲\n\n"
                "00:00:03,000 --> 00:00:04,000\n乙\n")
        条目们 = 解析SRT(文本)
        self.assertEqual([e.序号 for e in 条目们], [7, 8])

    def test_逗号点号与无毫秒(self):
        self.assertEqual(时间戳转秒("00:00:12,500"), 12.5)
        self.assertEqual(时间戳转秒("00:00:12.500"), 12.5)
        self.assertEqual(时间戳转秒("00:00:12"), 12.0)
        self.assertEqual(时间戳转秒("01:02:03,004"), 3723.004)
        self.assertEqual(时间戳转秒("12:34,500"), 754.5)      # 只有 MM:SS,mmm
        self.assertEqual(时间戳转秒("不是时间"), -1.0)
        self.assertEqual(秒转时间戳(3723.004), "01:02:03,004")
        self.assertEqual(秒转时间戳(-5), "00:00:00,000")
        self.assertEqual(时钟文本(754.0), "00:12:34")

    def test_空行混乱与多行文本(self):
        文本 = ("\n\n1\n00:00:01,000 --> 00:00:02,000\n第一行\n\n第二行\n\n\n"
                "2\n00:00:03,000 --> 00:00:04,000\n第三行\n\n\n")
        条目们 = 解析SRT(文本)
        self.assertEqual(len(条目们), 2)
        self.assertEqual(条目们[0].文本, "第一行\n第二行", "正文里的空行是噪声，不是块边界")
        self.assertEqual(条目们[1].文本, "第三行")

    def test_块之间没有空行(self):
        文本 = ("1\n00:00:01,000 --> 00:00:02,000\n甲\n"
                "2\n00:00:03,000 --> 00:00:04,000\n乙\n")
        条目们 = 解析SRT(文本)
        self.assertEqual([e.文本 for e in 条目们], ["甲", "乙"],
                      "紧挨着的下一块序号不能被当成正文")

    def test_坏时间行被跳过(self):
        文本 = ("1\n00:00:01,000 --> 乱码\n坏块\n\n"
                "2\n00:00:03,000 --> 00:00:04,000\n好块\n")
        条目们 = 解析SRT(文本)
        self.assertEqual([e.文本 for e in 条目们], ["好块"])

    def test_结束时间缺失或倒挂(self):
        文本 = ("1\n00:00:05,000 --> \n只有开始\n\n"
                "2\n00:00:09,000 --> 00:00:08,000\n倒挂\n")
        条目们 = 解析SRT(文本)
        self.assertEqual(条目们[0].结束秒, 5.5)
        self.assertEqual(条目们[1].结束秒, 9.5)

    def test_时间行尾部带样式参数(self):
        文本 = "1\n00:00:01,000 --> 00:00:02,000 X1:100 X2:200\n带样式\n"
        条目们 = 解析SRT(文本)
        self.assertEqual((条目们[0].开始秒, 条目们[0].结束秒), (1.0, 2.0))

    def test_空输入(self):
        self.assertEqual(解析SRT(""), [])
        self.assertEqual(解析SRT(None), [])


class SRT生成测试(unittest.TestCase):
    def test_生成格式(self):
        文本 = 生成SRT(造条目(["甲", "乙"]))
        self.assertTrue(文本.startswith("1\n00:00:00,000 --> 00:00:01,500\n甲\n\n"))
        self.assertTrue(文本.endswith("2\n00:00:02,000 --> 00:00:03,500\n乙\n"))
        self.assertEqual(生成SRT([]), "")

    def test_往返一致(self):
        条目们 = 造条目(["甲", "多行\n文本", "丙"])
        回来 = 解析SRT(生成SRT(条目们))
        self.assertEqual(回来, 条目们)

    def test_重排序号(self):
        条目们 = [字幕条目(序号=9, 开始秒=0.0, 结束秒=1.0, 文本="甲"),
                 字幕条目(序号=3, 开始秒=2.0, 结束秒=3.0, 文本="乙")]
        文本 = 生成SRT(条目们)
        self.assertIn("\n\n2\n", 文本)
        self.assertTrue(文本.startswith("1\n"))
        self.assertEqual([e.序号 for e in 解析SRT(文本)], [1, 2])


class 字幕文件测试(unittest.TestCase):
    def test_读写三种编码(self):
        条目们 = 造条目(["中文甲乙", "第二行"])
        内容 = 生成SRT(条目们)
        with tempfile.TemporaryDirectory() as 目录:
            用例 = [("utf8.srt", 内容.encode("utf-8")),
                   ("bom.srt", 内容.encode("utf-8-sig")),
                   ("gbk.srt", 内容.encode("gbk"))]
            for 名, 字节 in 用例:
                路径 = str(Path(目录) / 名)
                Path(路径).write_bytes(字节)
                读回 = 读取字幕文件(路径)
                self.assertEqual([e.文本 for e in 读回], ["中文甲乙", "第二行"], 名)
                self.assertEqual(时间点们(读回), 时间点们(条目们), 名)

    def test_读取不存在或空路径(self):
        self.assertEqual(读取字幕文件("/tmp/绝对不存在的字幕文件.srt"), [])
        self.assertEqual(读取字幕文件(""), [])

    def test_写出自动建目录并返回路径(self):
        条目们 = 造条目(["甲"])
        with tempfile.TemporaryDirectory() as 目录:
            目标 = str(Path(目录) / "深层" / "目录" / "out.srt")
            返回 = 写出字幕文件(目标, 条目们)
            self.assertEqual(返回, 目标)
            self.assertTrue(Path(目标).is_file())
            self.assertEqual([e.文本 for e in 读取字幕文件(目标)], ["甲"])
            self.assertFalse(Path(目标).read_bytes().startswith(b"\xef\xbb\xbf"),
                          "写出用无 BOM 的 UTF-8")

    def test_写出空路径抛错(self):
        with self.assertRaises(ValueError):
            写出字幕文件("", 造条目(["甲"]))


# ============================================================ 翻译

class 翻译测试(unittest.TestCase):
    def test_时间戳零漂移(self):
        # 故意用"不整齐"的浮点：只要翻译链路上动过一次时间，断言就会挂
        条目们 = [字幕条目(序号=1, 开始秒=0.1 + 0.2, 结束秒=12.345, 文本="a"),
                 字幕条目(序号=2, 开始秒=754.0000001, 结束秒=1000.0, 文本="b"),
                 字幕条目(序号=3, 开始秒=1000.5, 结束秒=1000.9, 文本="c")]
        助手 = 字幕助手(AI助手=假AI助手(应答=应答翻译))
        译文, 统计 = 助手.翻译(条目们, "中文")
        self.assertEqual(时间点们(译文), 时间点们(条目们))
        for 新, 原 in zip(译文, 条目们):
            self.assertEqual(新.开始秒, 原.开始秒)
            self.assertEqual(新.结束秒, 原.结束秒)
        self.assertEqual([e.文本 for e in 译文], ["【译】a", "【译】b", "【译】c"])
        self.assertEqual(统计["成功批"], 1)

    def test_不动入参且序号保持(self):
        条目们 = 造条目(["甲", "乙"])
        助手 = 字幕助手(AI助手=假AI助手(应答=应答翻译))
        译文, _ = 助手.翻译(条目们)
        self.assertEqual([e.文本 for e in 条目们], ["甲", "乙"], "原始条目必须原封不动")
        self.assertEqual([e.序号 for e in 译文], [1, 2])
        self.assertIsNot(译文[0], 条目们[0])

    def test_统计字段(self):
        条目们 = 造条目(["甲", "乙", "丙", "丁", "戊"])
        助手 = 字幕助手(AI助手=假AI助手(应答=应答翻译), 分批行数=2)
        _, 统计 = 助手.翻译(条目们)
        for 键 in ("批次", "成功批", "失败批", "用时秒", "来源"):
            self.assertIn(键, 统计)
        self.assertEqual((统计["批次"], 统计["成功批"], 统计["失败批"]),
                      (3, 3, 0))
        self.assertEqual(统计["来源"], "云端")
        self.assertIsInstance(统计["用时秒"], float)
        self.assertEqual(统计["原文保留行数"], 0)

    def test_提示里带目标语言与条数(self):
        云端 = 假AI助手(应答=应答翻译)
        助手 = 字幕助手(AI助手=云端, 分批行数=2)
        助手.翻译(造条目(["甲", "乙", "丙"]), "英文")
        第一条 = 云端.调用[0]
        self.assertIn("英文", 第一条["系统提示"])
        self.assertIn("2 条", 第一条["系统提示"])
        数据 = json.loads(第一条["用户消息"])
        self.assertEqual(数据["字幕"], ["甲", "乙"])
        self.assertEqual(数据["目标语言"], "英文")

    def test_重试后成功(self):
        调用次数 = {"n": 0}

        def 先坏后好(用户消息, 系统提示):
            调用次数["n"] += 1
            if 调用次数["n"] == 1:
                return "这不是 JSON"
            return 应答翻译(用户消息, 系统提示)

        云端 = 假AI助手(应答=先坏后好)
        助手 = 字幕助手(AI助手=云端, 每批重试=2)
        译文, 统计 = 助手.翻译(造条目(["甲", "乙"]))
        self.assertEqual([e.文本 for e in 译文], ["【译】甲", "【译】乙"])
        self.assertEqual(统计["成功批"], 1)
        self.assertEqual(统计["失败批"], 0)
        self.assertEqual(len(云端.调用), 2, "第一次坏 JSON 应触发重试")

    def test_坏JSON保留原文(self):
        云端 = 假AI助手(应答="抱歉，我无法完成这个请求。")
        助手 = 字幕助手(AI助手=云端, 每批重试=1)
        条目们 = 造条目(["甲", "乙"])
        译文, 统计 = 助手.翻译(条目们)
        self.assertEqual([e.文本 for e in 译文], ["甲", "乙"])
        self.assertEqual(时间点们(译文), 时间点们(条目们))
        self.assertEqual((统计["成功批"], 统计["失败批"]), (0, 1))
        self.assertEqual(统计["来源"], "规则")
        self.assertEqual(统计["原文保留行数"], 2)

    def test_条数不符保留原文(self):
        云端 = 假AI助手(应答={"译文": ["只给一条"]})
        助手 = 字幕助手(AI助手=云端, 每批重试=0)
        条目们 = 造条目(["甲", "乙", "丙"])
        译文, 统计 = 助手.翻译(条目们)
        self.assertEqual([e.文本 for e in 译文], ["甲", "乙", "丙"])
        self.assertEqual(统计["失败批"], 1)
        self.assertEqual(len(云端.调用), 1, "每批重试=0 时只尝试一次")

    def test_多出来的译文被截断(self):
        云端 = 假AI助手(应答={"译文": ["一", "二", "三多出来的"]})
        助手 = 字幕助手(AI助手=云端)
        译文, 统计 = 助手.翻译(造条目(["甲", "乙"]))
        self.assertEqual([e.文本 for e in 译文], ["一", "二"])
        self.assertEqual(统计["成功批"], 1)

    def test_空返回保留原文(self):
        for 空 in (None, {}, [], "", {"译文": []}):
            with self.subTest(空=空):
                云端 = 假AI助手(应答=空)
                助手 = 字幕助手(AI助手=云端, 每批重试=0)
                条目们 = 造条目(["甲", "乙"])
                译文, 统计 = 助手.翻译(条目们)
                self.assertEqual([e.文本 for e in 译文], ["甲", "乙"])
                self.assertEqual(统计["失败批"], 1)

    def test_单行空译文保留原文(self):
        云端 = 假AI助手(应答={"译文": ["译好的", "   ", "也译好了"]})
        助手 = 字幕助手(AI助手=云端)
        译文, 统计 = 助手.翻译(造条目(["甲", "乙", "丙"]))
        self.assertEqual([e.文本 for e in 译文], ["译好的", "乙", "也译好了"])
        self.assertEqual(统计["成功批"], 1)
        self.assertEqual(统计["原文保留行数"], 1)

    def test_抛异常与超时不冒泡(self):
        用例 = [RuntimeError("模型炸了"), TimeoutError("请求超时"),
              ConnectionError("断连")]
        for 异常 in 用例:
            with self.subTest(异常=type(异常).__name__):
                云端 = 假AI助手(应答=异常)
                助手 = 字幕助手(AI助手=云端, 每批重试=1)
                条目们 = 造条目(["甲", "乙"])
                译文, 统计 = 助手.翻译(条目们)      # 不许抛
                self.assertEqual([e.文本 for e in 译文], ["甲", "乙"])
                self.assertEqual(时间点们(译文), 时间点们(条目们))
                self.assertEqual(统计["失败批"], 1)
                self.assertEqual(len(云端.调用), 2, "重试 1 次 = 共 2 次尝试")

    def test_本地优先云端不参与(self):
        本地 = 假本地模型(应答=应答本地翻译)
        云端 = 假AI助手(应答=应答翻译)
        助手 = 字幕助手(AI助手=云端, 本地模型=本地)
        译文, 统计 = 助手.翻译(造条目(["甲", "乙"]))
        self.assertEqual([e.文本 for e in 译文], ["【本】甲", "【本】乙"])
        self.assertEqual(统计["来源"], "本地模型")
        self.assertEqual(len(本地.调用), 1)
        self.assertEqual(len(云端.调用), 0, "本地成功时不该走云端（省钱、内容不出本机）")

    def test_本地不可用走云端(self):
        本地 = 假本地模型(应答=应答本地翻译, 可用=False)
        云端 = 假AI助手(应答=应答翻译)
        助手 = 字幕助手(AI助手=云端, 本地模型=本地)
        译文, 统计 = 助手.翻译(造条目(["甲"]))
        self.assertEqual([e.文本 for e in 译文], ["【译】甲"])
        self.assertEqual(统计["来源"], "云端")

    def test_本地回答被截断时加大预算重试(self):
        # 真机实测：1.5B 的总结 JSON 会顶到 max_tokens（默认 512）被切断
        截断的 = ('```json\n{"摘要": "本地总结", "章节": [{"时间": "00:00:01", '
               '"标题": "开头"')
        完整的 = json.dumps({"摘要": "本地总结",
                         "章节": [{"时间": "00:00:01", "标题": "开头",
                                 "要点": "本地"}],
                         "标签": ["本地"]}, ensure_ascii=False)
        配置 = type("配置", (), {"启用": True, "最大tokens": 512})()
        本地 = 假本地模型(应答=[截断的, 完整的], 配置=配置)
        结果 = 字幕助手(本地模型=本地).总结(造条目(["甲。", "乙。"]))
        self.assertTrue(结果["成功"])
        self.assertEqual(结果["来源"], "本地模型")
        self.assertEqual(结果["摘要"], "本地总结")
        self.assertEqual(len(本地.调用), 2, "截断 → 预算翻倍重试一次")
        self.assertEqual(本地.调用[0]["最大tokens"], 512)
        self.assertEqual(本地.调用[1]["最大tokens"], 1024)

    def test_回答不像截断就不重试(self):
        本地 = 假本地模型(应答="抱歉，这个请求我做不到。")
        云端 = 假AI助手(应答=应答翻译)
        助手 = 字幕助手(AI助手=云端, 本地模型=本地)
        译文, 统计 = 助手.翻译(造条目(["甲", "乙"]))
        self.assertEqual([e.文本 for e in 译文], ["【译】甲", "【译】乙"])
        self.assertEqual(统计["来源"], "云端")
        self.assertEqual(len(本地.调用), 1, "完整但不可解析的回答不该白等一次翻倍重试")

    def test_预算到顶就不再重试(self):
        截断的 = '{"摘要": "x", "章节": ['
        配置 = type("配置", (), {"启用": True, "最大tokens": 2048})()
        本地 = 假本地模型(应答=截断的, 配置=配置)
        结果 = 字幕助手(本地模型=本地).总结(造条目(["甲。", "乙。"]))
        self.assertEqual(len(本地.调用), 1)
        self.assertEqual(本地.调用[0]["最大tokens"], 2048)
        self.assertEqual(结果["来源"], "规则", "到顶后只能规则降级")

    def test_本地坏掉立刻熔断并云端兜底(self):
        本地 = 假本地模型(应答=ConnectionError("connection refused"))
        云端 = 假AI助手(应答=应答翻译)
        助手 = 字幕助手(AI助手=云端, 本地模型=本地, 分批行数=1, 每批重试=0)
        译文, 统计 = 助手.翻译(造条目(["甲", "乙", "丙"]))
        self.assertEqual([e.文本 for e in 译文], ["【译】甲", "【译】乙", "【译】丙"])
        self.assertEqual(统计["来源"], "云端")
        self.assertEqual(len(本地.调用), 1, "本地失败后必须熔断，不能每批都撞一次")

    def test_本地未启用不调用(self):
        # 配置.启用=False 的语义：用户关掉了本地模型，就不许偷偷用（哪怕端口是通的）
        本地 = 假本地模型(应答=应答本地翻译, 可用=True,
                     配置=type("配置", (), {"启用": False})())
        云端 = 假AI助手(应答=应答翻译)
        助手 = 字幕助手(AI助手=云端, 本地模型=本地)
        _, 统计 = 助手.翻译(造条目(["甲"]))
        self.assertEqual(统计["来源"], "云端")
        self.assertEqual(len(本地.调用), 0)

    def test_无AI来源时规则降级(self):
        助手 = 字幕助手()          # 既没有本地模型，也没有 AI助手
        条目们 = 造条目(["甲", "乙", "丙"])
        译文, 统计 = 助手.翻译(条目们)
        self.assertEqual([e.文本 for e in 译文], ["甲", "乙", "丙"])
        self.assertEqual(时间点们(译文), 时间点们(条目们))
        self.assertEqual(统计["来源"], "规则")
        self.assertEqual((统计["成功批"], 统计["失败批"]), (0, 1))

    def test_空输入不调AI(self):
        云端 = 假AI助手(应答=应答翻译)
        助手 = 字幕助手(AI助手=云端)
        译文, 统计 = 助手.翻译([])
        self.assertEqual(译文, [])
        self.assertEqual(统计["批次"], 0)
        self.assertEqual(len(云端.调用), 0)

    def test_并发不乱序(self):
        def 反向延迟(用户消息, 系统提示):
            数据 = json.loads(用户消息)
            序 = int(str(数据["字幕"][0]).split("-")[0])
            time.sleep(0.05 * (4 - 序))       # 越靠前的批越慢 → 完成顺序与批顺序相反
            return {"译文": [f"【译】{t}" for t in 数据["字幕"]]}

        云端 = 假AI助手(应答=反向延迟)
        助手 = 字幕助手(AI助手=云端, 分批行数=1, 最大并发=3)
        条目们 = 造条目(["1-甲", "2-乙", "3-丙"])
        译文, 统计 = 助手.翻译(条目们)
        self.assertEqual([e.文本 for e in 译文],
                      ["【译】1-甲", "【译】2-乙", "【译】3-丙"],
                      "并发只改变完成顺序，回填必须按原顺序")
        self.assertEqual(时间点们(译文), 时间点们(条目们))
        self.assertEqual(统计["成功批"], 3)

    def test_进度回调三种签名(self):
        条目们 = 造条目(["甲", "乙", "丙", "丁", "戊"])
        助手 = 字幕助手(AI助手=假AI助手(应答=应答翻译), 分批行数=2)
        三段 = []
        助手.翻译(条目们, 进度回调=lambda 阶段, 已完成, 总数: 三段.append(
            (阶段, 已完成, 总数)))
        self.assertEqual(三段, [("翻译", 2, 5), ("翻译", 4, 5), ("翻译", 5, 5)])
        比例 = []
        助手.翻译(条目们, 进度回调=lambda 比值: 比例.append(比值))
        self.assertEqual(比例, [0.4, 0.8, 1.0])
        两段 = []
        助手.翻译(条目们, 进度回调=lambda 已完成, 总数: 两段.append(
            (已完成, 总数)))
        self.assertEqual(两段[-1], (5, 5))

    def test_进度回调抛错不影响翻译(self):
        def 坏回调(*_):
            raise RuntimeError("进度条炸了")

        助手 = 字幕助手(AI助手=假AI助手(应答=应答翻译))
        译文, 统计 = 助手.翻译(造条目(["甲"]), 进度回调=坏回调)
        self.assertEqual([e.文本 for e in 译文], ["【译】甲"])
        self.assertEqual(统计["成功批"], 1)

    def test_文本越界时按字数分批(self):
        长 = "啊" * 700
        条目们 = 造条目([长, 长, 长])
        云端 = 假AI助手(应答=应答翻译)
        助手 = 字幕助手(AI助手=云端, 分批行数=40)
        _, 统计 = 助手.翻译(条目们)
        self.assertEqual(统计["批次"], 3, "每批最大字数 1200 → 700 字的条目只能一条一批")


class 翻译文件测试(unittest.TestCase):
    def test_默认输出路径与内容(self):
        条目们 = 造条目(["Hello", "World"])
        with tempfile.TemporaryDirectory() as 目录:
            源 = str(Path(目录) / "movie.srt")
            写出字幕文件(源, 条目们)
            助手 = 字幕助手(AI助手=假AI助手(应答=应答翻译))
            结果 = 助手.翻译文件(源)
            self.assertTrue(结果["成功"], 结果["错误"])
            self.assertEqual(结果["输出路径"], str(Path(目录) / "movie.中文.srt"))
            self.assertEqual(结果["条目数"], 2)
            self.assertEqual(结果["统计"]["来源"], "云端")
            读回 = 读取字幕文件(结果["输出路径"])
            self.assertEqual([e.文本 for e in 读回], ["【译】Hello", "【译】World"])
            self.assertEqual(时间点们(读回), 时间点们(条目们))

    def test_指定目标路径(self):
        with tempfile.TemporaryDirectory() as 目录:
            源 = str(Path(目录) / "a.srt")
            写出字幕文件(源, 造条目(["甲"]))
            目标 = str(Path(目录) / "b.srt")
            结果 = 字幕助手(AI助手=假AI助手(应答=应答翻译)).翻译文件(
                源, 目标, "日文")
            self.assertTrue(结果["成功"], 结果["错误"])
            self.assertEqual(结果["输出路径"], 目标)

    def test_源文件不存在不抛(self):
        助手 = 字幕助手(AI助手=假AI助手(应答=应答翻译))
        结果 = 助手.翻译文件("/tmp/没有这个文件.srt")
        self.assertFalse(结果["成功"])
        self.assertEqual(结果["输出路径"], "")
        self.assertTrue(结果["错误"])
        self.assertEqual(结果["条目数"], 0)


# ============================================================ 总结

class 总结测试(unittest.TestCase):
    def test_AI总结成功且章节排序(self):
        应答 = {"摘要": "讲了三件事",
              "章节": [{"时间": "00:01:00", "标题": "第二段", "要点": "要点二"},
                      {"时间": "00:00:10", "标题": "第一段", "要点": "要点一"}],
              "标签": ["甲", "乙", "甲", "  "]}
        云端 = 假AI助手(应答=应答)
        助手 = 字幕助手(AI助手=云端)
        # 步长 100 秒 → 片长近 300 秒，10s / 60s 两个章节都在片长内
        结果 = 助手.总结(造条目(["甲", "乙", "丙"], 步长=100.0), 标题="测试视频")
        self.assertTrue(结果["成功"], 结果["错误"])
        self.assertEqual(结果["摘要"], "讲了三件事")
        self.assertEqual([章["秒"] for 章 in 结果["章节"]], [10.0, 60.0])
        self.assertEqual([章["时间"] for 章 in 结果["章节"]],
                      ["00:00:10", "00:01:00"])
        self.assertEqual(结果["标签"], ["甲", "乙"])
        self.assertEqual(结果["来源"], "云端")
        self.assertEqual(结果["错误"], "")
        for 章 in 结果["章节"]:
            self.assertIsInstance(章["秒"], float)
            self.assertEqual(章["时间"], 时钟文本(章["秒"]), "时间与秒必须自洽")
        self.assertIn("测试视频", 云端.调用[0]["用户消息"])
        self.assertIn("[00:00:", 云端.调用[0]["用户消息"])

    def test_章节时间夹在片长内(self):
        应答 = {"摘要": "x",
              "章节": [{"时间": "00:05:00", "标题": "越界", "要点": ""},
                      {"时间": "00:00:05", "标题": "正常", "要点": ""}]}
        条目们 = 造条目(["甲", "乙"], 起始=0.0, 步长=2.0)   # 片长 3.5s
        结果 = 字幕助手(AI助手=假AI助手(应答=应答)).总结(条目们)
        self.assertTrue(结果["成功"])
        秒们 = [章["秒"] for 章 in 结果["章节"]]
        self.assertLessEqual(max(秒们), 3.5, "越界的时间点在播放器里跳不动，必须夹回片长")
        self.assertEqual(秒们, sorted(秒们))
        self.assertIn(3.5, 秒们)

    def test_章节秒是字符串或一行式(self):
        应答 = {"摘要": "x",
              "章节": [{"秒": "120.0", "标题": "字符串秒", "要点": ""},
                      "00:04:00 一行式章节"]}
        条目们 = 造条目(["甲", "乙", "丙"], 步长=200.0)     # 片长 599.5s
        结果 = 字幕助手(AI助手=假AI助手(应答=应答)).总结(条目们)
        self.assertTrue(结果["成功"])
        self.assertEqual([章["秒"] for 章 in 结果["章节"]], [120.0, 240.0])
        self.assertEqual([章["标题"] for 章 in 结果["章节"]],
                      ["字符串秒", "一行式章节"])

    def test_AI失败降级规则(self):
        助手 = 字幕助手()      # 没有任何 AI
        条目们 = 造条目(["第一句话。", "第二句话。", "第三句话。",
                      "第四句话。", "第五句话。"])
        结果 = 助手.总结(条目们)
        self.assertTrue(结果["成功"])
        self.assertEqual(结果["来源"], "规则")
        self.assertTrue(结果["摘要"].startswith("第一句话"))
        self.assertGreaterEqual(len(结果["章节"]), 2)
        秒们 = [章["秒"] for 章 in 结果["章节"]]
        self.assertEqual(秒们, sorted(秒们), "章节必须按时间升序")
        for 章 in 结果["章节"]:
            self.assertIsInstance(章["秒"], float)
            self.assertEqual(章["时间"], 时钟文本(章["秒"]))
            self.assertTrue(章["标题"])
        self.assertIn("说明", 结果)

    def test_坏载荷降级规则(self):
        for 坏 in ("完全不是 JSON", {"无关字段": 1}, [1, 2, 3], 42):
            with self.subTest(坏=坏):
                助手 = 字幕助手(AI助手=假AI助手(应答=坏))
                结果 = 助手.总结(造条目(["甲。", "乙。"]))
                self.assertTrue(结果["成功"])
                self.assertEqual(结果["来源"], "规则")
                self.assertTrue(结果["章节"])

    def test_只有摘要时用规则章节补齐(self):
        助手 = 字幕助手(AI助手=假AI助手(应答={"摘要": "只有摘要"}))
        结果 = 助手.总结(造条目(["甲。", "乙。", "丙。"]))
        self.assertTrue(结果["成功"])
        self.assertEqual(结果["摘要"], "只有摘要")
        self.assertEqual(结果["来源"], "云端")
        self.assertTrue(结果["章节"], "章节不能空着，否则播放器没有跳转点")

    def test_本地模型总结(self):
        本地 = 假本地模型(应答=json.dumps(
            {"摘要": "本地总结", "章节": [{"时间": "00:00:01", "标题": "开头",
                                    "要点": "本地"}], "标签": ["本地"]},
            ensure_ascii=False))
        结果 = 字幕助手(本地模型=本地).总结(造条目(["甲。", "乙。"]))
        self.assertTrue(结果["成功"])
        self.assertEqual(结果["来源"], "本地模型")
        self.assertEqual(结果["摘要"], "本地总结")

    def test_空字幕不调AI(self):
        云端 = 假AI助手(应答={"摘要": "不该被用到"})
        助手 = 字幕助手(AI助手=云端)
        for 条目们 in ([], [字幕条目(文本="")], [字幕条目(文本="[音乐]")],
                     [字幕条目(文本="♪♪")]):
            with self.subTest(条目们=条目们):
                结果 = 助手.总结(条目们)
                self.assertFalse(结果["成功"])
                self.assertTrue(结果["错误"])
                self.assertEqual(结果["章节"], [])
        self.assertEqual(len(云端.调用), 0, "没有字幕就不该花 AI 调用")

    def test_长字幕走抽样而不是截断(self):
        条目们 = 造条目([f"第{i}句话，内容挺长的用来撑爆上限。" for i in range(1, 501)],
                    步长=1.0)
        助手 = 字幕助手(AI助手=假AI助手(应答=应答翻译))
        _, 用户消息 = 助手._总结提示(条目们, "")
        self.assertLessEqual(len(用户消息), 助手.总结文本上限 + 200)
        self.assertIn("[00:00:00]", 用户消息, "抽样必须保留开头")
        self.assertIn("[00:08:19]", 用户消息, "抽样必须覆盖到结尾")


# ============================================================ 语音识别

class 识别结果转条目测试(unittest.TestCase):
    def test_标准段落(self):
        段落们 = [{"开始秒": 12.3, "结束秒": 15.0, "文本": "你好"},
                 {"开始秒": 15.2, "结束秒": 17.0, "文本": "世界"}]
        条目们 = 字幕助手().识别结果转条目(段落们)
        self.assertEqual([e.序号 for e in 条目们], [1, 2])
        self.assertEqual((条目们[0].开始秒, 条目们[0].结束秒), (12.3, 15.0))
        self.assertEqual([e.文本 for e in 条目们], ["你好", "世界"])

    def test_字段名与毫秒与字符串时间戳(self):
        段落们 = [{"start": 1.0, "end": 2.0, "text": "英文键"},
                 {"开始毫秒": 3000, "结束毫秒": 4500, "内容": "毫秒"},
                 {"开始": "00:00:06,000", "结束": "00:00:08,500", "文本": "字符串"},
                 {"begin": 9, "stop": 10, "sentence": ["分词", "数组"]}]
        条目们 = 字幕助手().识别结果转条目(段落们)
        self.assertEqual([e.文本 for e in 条目们],
                      ["英文键", "毫秒", "字符串", "分词 数组"])
        self.assertEqual((条目们[1].开始秒, 条目们[1].结束秒), (3.0, 4.5))
        self.assertEqual((条目们[2].开始秒, 条目们[2].结束秒), (6.0, 8.5))

    def test_乱序倒挂与空文本(self):
        段落们 = [{"开始秒": 10.0, "结束秒": 11.0, "文本": "后面"},
                 {"开始秒": 1.0, "结束秒": 0.5, "文本": "倒挂"},
                 {"开始秒": 5.0, "结束秒": 6.0, "文本": "   "},
                 {"开始秒": 3.0, "结束秒": 4.0, "文本": "中间"}]
        条目们 = 字幕助手().识别结果转条目(段落们)
        self.assertEqual([e.文本 for e in 条目们], ["倒挂", "中间", "后面"])
        self.assertEqual([e.序号 for e in 条目们], [1, 2, 3])
        self.assertEqual(条目们[0].结束秒, 1.5, "倒挂/零时长必须有兜底时长")
        self.assertEqual(条目们[0].开始秒, 1.0)

    def test_元组形式与脏数据(self):
        段落们 = [(1.0, 2.0, "元组"), (2.5, 3.0, "第二个"), "垃圾", {}, None,
                 {"开始秒": -5, "结束秒": -4, "文本": "负数"}]
        条目们 = 字幕助手().识别结果转条目(段落们)
        self.assertEqual([e.文本 for e in 条目们], ["负数", "元组", "第二个"])
        self.assertEqual(条目们[0].开始秒, 0.0)
        self.assertEqual(字幕助手().识别结果转条目(None), [])

    def test_数字以字符串给出(self):
        # AI / ASR 的 JSON 里时间经常是字符串，只认 float 会把整条丢掉
        段落们 = [{"开始秒": "12.3", "结束秒": "15", "文本": "字符串数字"},
                 {"开始秒": "00:00:20,500", "结束秒": "00:00:22,000", "文本": "时间戳"}]
        条目们 = 字幕助手().识别结果转条目(段落们)
        self.assertEqual((条目们[0].开始秒, 条目们[0].结束秒), (12.3, 15.0))
        self.assertEqual((条目们[1].开始秒, 条目们[1].结束秒), (20.5, 22.0))

    def test_转出来的条目能直接生成SRT(self):
        段落们 = [{"开始秒": 1.0, "结束秒": 2.0, "文本": "甲"},
                 {"开始秒": 2.0, "结束秒": 3.0, "文本": "乙"}]
        条目们 = 字幕助手().识别结果转条目(段落们)
        文本 = 生成SRT(条目们)
        self.assertTrue(文本.startswith("1\n00:00:01,000 --> 00:00:02,000\n甲\n"))
        self.assertEqual(解析SRT(文本), 条目们)


class 生成字幕测试(unittest.TestCase):
    段落 = [{"开始秒": 0.0, "结束秒": 2.0, "文本": "第一句"},
           {"开始秒": 2.2, "结束秒": 4.0, "文本": "第二句"}]

    def test_成功(self):
        识别器 = 假识别器(段落=self.段落)
        进度 = []
        结果 = 字幕助手().生成字幕("/video/a.mp4", 识别器, "en",
                               进度回调=lambda *a: 进度.append(a))
        self.assertTrue(结果["成功"], 结果["错误"])
        self.assertEqual([e.文本 for e in 结果["条目们"]], ["第一句", "第二句"])
        self.assertEqual(结果["统计"]["段落数"], 2)
        self.assertEqual(结果["统计"]["条目数"], 2)
        self.assertEqual(结果["统计"]["语言"], "en")
        self.assertEqual(结果["统计"]["时长秒"], 4.0)
        self.assertEqual(len(识别器.调用), 1)
        self.assertEqual(进度[-1], ("识别", 2, 2))

    def test_不可用(self):
        识别器 = 假识别器(段落=self.段落, 可用=(False, "没装 whisper"))
        结果 = 字幕助手().生成字幕("/video/a.mp4", 识别器)
        self.assertFalse(结果["成功"])
        self.assertEqual(结果["错误"], "没装 whisper")
        self.assertEqual(识别器.调用, [])

    def test_识别抛异常不冒泡(self):
        识别器 = 假识别器(异常=RuntimeError("ffmpeg 挂了"))
        结果 = 字幕助手().生成字幕("/video/a.mp4", 识别器)
        self.assertFalse(结果["成功"])
        self.assertTrue(结果["错误"])
        self.assertEqual(结果["条目们"], [])

    def test_返回空或非列表(self):
        for 段落 in ([], None, "乱数据", {"段落": []}):
            with self.subTest(段落=段落):
                结果 = 字幕助手().生成字幕("/v.mp4", 假识别器(段落=段落))
                self.assertFalse(结果["成功"])

    def test_识别器返回字典包段落(self):
        结果 = 字幕助手().生成字幕("/v.mp4", 假识别器(段落={"段落": self.段落}))
        self.assertTrue(结果["成功"], 结果["错误"])
        self.assertEqual(结果["统计"]["条目数"], 2)

    def test_最小签名识别器(self):
        识别器 = 最小签名识别器(段落=self.段落)
        结果 = 字幕助手().生成字幕("/v.mp4", 识别器, "zh")
        self.assertTrue(结果["成功"], 结果["错误"])
        self.assertEqual(结果["统计"]["条目数"], 2)

    def test_没有识别器或空来源(self):
        助手 = 字幕助手()
        self.assertFalse(助手.生成字幕("/v.mp4", None)["成功"])
        self.assertFalse(助手.生成字幕("", 假识别器(段落=self.段落))["成功"])
        self.assertFalse(助手.生成字幕("   ", 假识别器(段落=self.段落))["成功"])

    def test_可用返回裸布尔(self):
        识别器 = 假识别器(段落=self.段落, 可用=True)
        结果 = 字幕助手().生成字幕("/v.mp4", 识别器)
        self.assertTrue(结果["成功"])


# ============================================================ 工具

class 工具测试(unittest.TestCase):
    def test_纯文本带时间戳(self):
        条目们 = [字幕条目(1, 12.0, 15.0, "第一行\n第二行"),
                 字幕条目(2, 65.5, 70.0, "  空白折叠  "),
                 字幕条目(3, 70.0, 71.0, "   ")]
        文本 = 字幕助手().纯文本(条目们)
        self.assertEqual(文本, "[00:00:12] 第一行 第二行\n[00:01:05] 空白折叠")
        self.assertEqual(字幕助手().纯文本([]), "")

    def test_合并短句(self):
        条目们 = [字幕条目(1, 0.0, 0.6, "我"),
                 字幕条目(2, 0.62, 1.2, "觉得"),
                 字幕条目(3, 1.22, 1.9, "这个不错。")]
        合并后 = 字幕助手().合并短句(条目们)
        self.assertEqual(len(合并后), 1)
        self.assertEqual(合并后[0].文本, "我觉得这个不错。")
        self.assertEqual((合并后[0].开始秒, 合并后[0].结束秒), (0.0, 1.9),
                      "合并只扩不缩：起点取首条、终点取末条")
        self.assertEqual(合并后[0].序号, 1)
        self.assertEqual([e.文本 for e in 条目们], ["我", "觉得", "这个不错。"],
                      "不能改入参")

    def test_合并受最大字数限制(self):
        条目们 = [字幕条目(1, 0.0, 0.6, "我"),
                 字幕条目(2, 0.62, 1.2, "觉得"),
                 字幕条目(3, 1.22, 1.9, "这个不错。")]
        合并后 = 字幕助手().合并短句(条目们, 最大字数=4)
        self.assertEqual([e.文本 for e in 合并后], ["我觉得", "这个不错。"])
        self.assertEqual([e.序号 for e in 合并后], [1, 2])

    def test_跨大间隙不合并(self):
        条目们 = [字幕条目(1, 0.0, 0.6, "嗯"),
                 字幕条目(2, 5.0, 5.6, "然后")]
        合并后 = 字幕助手().合并短句(条目们)
        self.assertEqual(len(合并后), 2, "隔了 4.4 秒多半是换镜头，不能粘")

    def test_完整长句不合并(self):
        条目们 = [字幕条目(1, 0.0, 2.0, "你好。"),
                 字幕条目(2, 2.05, 3.5, "再见。")]
        合并后 = 字幕助手().合并短句(条目们)
        self.assertEqual(len(合并后), 2)

    def test_英文合并补空格(self):
        条目们 = [字幕条目(1, 0.0, 0.5, "hello"),
                 字幕条目(2, 0.55, 1.0, "world")]
        合并后 = 字幕助手().合并短句(条目们)
        self.assertEqual(合并后[0].文本, "hello world")

    def test_无字幕判定(self):
        助手 = 字幕助手()
        self.assertTrue(助手.无字幕判定([]))
        self.assertTrue(助手.无字幕判定([字幕条目(文本="")]))
        self.assertTrue(助手.无字幕判定([字幕条目(文本="   ")]))
        self.assertTrue(助手.无字幕判定([字幕条目(文本="[音乐]"),
                                  字幕条目(文本="♪♪")]))
        self.assertTrue(助手.无字幕判定([字幕条目(文本="—— ……")]))
        self.assertFalse(助手.无字幕判定([字幕条目(文本="你好")]))
        self.assertFalse(助手.无字幕判定([字幕条目(文本="ok")]))
        self.assertFalse(助手.无字幕判定([字幕条目(文本="[音乐]"),
                                   字幕条目(文本="真正的内容")]))

    def test_本地状态可以重置(self):
        本地 = 假本地模型(应答=应答本地翻译, 可用=False)
        助手 = 字幕助手(本地模型=本地)
        self.assertFalse(助手.本地是否可用())
        本地.可用 = True
        self.assertTrue(助手.重置本地状态(), "重置后要重新探测（界面启动本地服务后用）")
        self.assertTrue(助手.本地是否可用())


class 模块约束测试(unittest.TestCase):
    def test_不依赖Qt(self):
        代码 = (
            "import sys; sys.path.insert(0, %r);\n"
            "import v8_3.AI.字幕助手 as 模块;\n"
            "坏 = [m for m in sys.modules if 'PySide' in m or 'PyQt' in m];\n"
            "assert not 坏, 坏;\n"
            "print('ok')\n" % str(项目根))
        结果 = subprocess.run([sys.executable, "-c", 代码],
                            capture_output=True, text=True, timeout=60)
        self.assertEqual(结果.returncode, 0, 结果.stderr)
        self.assertIn("ok", 结果.stdout)

    def test_导出契约(self):
        import v8_3.AI.字幕助手 as 模块
        for 名字 in ("字幕条目", "字幕助手", "解析SRT", "生成SRT",
                   "读取字幕文件", "写出字幕文件", "时间戳转秒", "秒转时间戳"):
            self.assertTrue(hasattr(模块, 名字), 名字)
        助手 = 字幕助手()
        for 方法 in ("解析SRT", "生成SRT", "读取字幕文件", "写出字幕文件", "翻译",
                   "翻译文件", "总结", "识别结果转条目", "生成字幕", "纯文本",
                   "合并短句", "无字幕判定"):
            self.assertTrue(callable(getattr(助手, 方法, None)), 方法)
        条 = 字幕条目(1, 0.0, 1.0, "x")
        self.assertTrue(callable(条.追加))
        self.assertIsInstance(条.复制(), 字幕条目)


if __name__ == "__main__":
    unittest.main()
