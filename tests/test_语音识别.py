"""本地语音识别（faster-whisper）单测（V8_3 新增）。

分两层，**默认只跑快的那层**：

============  ==========================================================
快测试（默认）  不加载模型、不起工作进程、不碰网络。即使把 ``运行环境/语音识别/``
              整个删掉也应当全绿 —— 验证的是**接口语义**：
              模块导入、`可用()` 不抛异常且给得出修复步骤、`模型信息()` 字段齐全、
              `识别段落` 数据结构、语言规范、延迟加载、异常类型可捕获。
慢测试         真的跑一遍 Whisper（用项目内 ``运行环境/语音识别/测试音频/`` 里的
              3 秒中文音频）。默认跳过，用环境变量开启：

              .. code-block:: bash

                  V8_3_跑慢测试=1 运行环境/venv/bin/python -m unittest \\
                      tests.test_语音识别 -v
============  ==========================================================

跑法（项目根下）::

    运行环境/venv/bin/python -m unittest tests.test_语音识别 -v
"""

from __future__ import annotations

import os
import sys
import time
import unittest
from pathlib import Path

项目根 = Path(__file__).resolve().parents[1]
if str(项目根) not in sys.path:
    sys.path.insert(0, str(项目根))

from v8_3.播放 import 语音识别 as 语音识别模块
from v8_3.播放.语音识别 import (识别失败, 识别段落, 环境目录, 独立解释器,
                          模型仓库, 规范语言, 语音识别器, 转SRT)

#: 刚 import 完本模块的这一刻，主进程里已经有哪些重库。
#: **必须在导入后立刻快照**，否则慢测试先跑会把 faster_whisper 拉进来，误伤这条断言。
重库名单 = ("faster_whisper", "ctranslate2", "onnxruntime", "torch")
导入瞬间的重库 = tuple(名 for 名 in 重库名单 if 名 in sys.modules)

#: 测试音频目录（在项目内，跟着局部环境一起走）
测试音频目录 = 环境目录 / "测试音频"
三秒音频 = 测试音频目录 / "中文测试_3秒.wav"
完整音频 = 测试音频目录 / "中文测试_完整.wav"
样本源 = 测试音频目录 / "中文语音样本_源.mp3"

#: 慢测试开关：默认关，避免日常跑测试时白白烧几分钟 CPU
慢测试开关 = "V8_3_跑慢测试"


def 慢测试(原因: str = ""):
    """标记"慢测试"：只有显式设了环境变量才真的跑。"""
    return unittest.skipUnless(
        os.environ.get(慢测试开关) == "1",
        原因 or f"慢测试（真要跑模型）：设 {慢测试开关}=1 开启")


def _准备三秒音频() -> tuple[bool, str]:
    """确保 3 秒测试音频存在；缺了就用 ffmpeg 从项目内的样本重新切。"""
    if 三秒音频.is_file() and 三秒音频.stat().st_size > 4096:
        return True, str(三秒音频)
    if not 样本源.is_file():
        return False, f"既没有 {三秒音频}，也没有样本 {样本源}"
    import shutil
    import subprocess
    if not shutil.which("ffmpeg"):
        return False, "系统里没有 ffmpeg，无法重新生成 3 秒测试音频"
    测试音频目录.mkdir(parents=True, exist_ok=True)
    结果 = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-t", "3.0", "-i", str(样本源),
         "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(三秒音频)],
        capture_output=True, text=True)
    if 结果.returncode != 0 or not 三秒音频.is_file():
        return False, "ffmpeg 生成 3 秒测试音频失败：" + (结果.stderr or "")[-300:]
    return True, str(三秒音频)


# ==========================================================================
# 快测试（不依赖模型）
# ==========================================================================

class 模块导入测试(unittest.TestCase):
    """模块能被导入，且导入本身是"干净"的（不拉重库、不起进程）。"""

    def test_导入不引入重库(self):
        self.assertEqual(
            导入瞬间的重库, (),
            f"导入 v8_3.播放.语音识别 时不该把 {导入瞬间的重库} 拉进主进程"
            "（会污染共享 venv 的内存与启动时间）")

    def test_导出符号齐全(self):
        for 名字 in ("识别段落", "识别失败", "语音识别器", "转SRT", "规范语言",
                     "环境目录", "环境Python", "默认模型目录", "默认模型大小", "模型仓库",
                     "候选模型大小", "支持语言", "标记"):
            self.assertTrue(hasattr(语音识别模块, 名字), f"模块缺少导出符号 {名字}")
            self.assertIn(名字, 语音识别模块.__all__)

    def test_必需方法都在(self):
        for 名字 in ("可用", "模型信息", "识别", "卸载"):
            self.assertTrue(callable(getattr(语音识别器, 名字, None)), f"语音识别器 缺少 {名字}()")

    def test_目录常量落在项目内(self):
        self.assertTrue(str(环境目录).startswith(str(项目根)),
                        f"局部环境目录必须在项目内，实际是 {环境目录}")
        self.assertEqual(环境目录, 项目根 / "运行环境" / "语音识别")

    def test_模型仓库表覆盖候选大小(self):
        for 大小 in 语音识别模块.候选模型大小:
            self.assertIn(大小, 模型仓库)


class 语言规范测试(unittest.TestCase):

    def test_常用别名(self):
        用例 = {"中文": "zh", "汉语": "zh", "普通话": "zh", "ZH": "zh", "zh-cn": "zh",
                "英语": "en", "EN": "en", "日语": "ja", "粤语": "yue"}
        for 输入, 期望 in 用例.items():
            self.assertEqual(规范语言(输入), 期望, f"{输入!r} 应规范成 {期望!r}")

    def test_空与自动(self):
        for 输入 in ("", None, "自动", "auto", "AUTO", "自动检测", "  "):
            self.assertEqual(规范语言(输入), "", f"{输入!r} 应表示自动检测")

    def test_未知语言原样透传(self):
        self.assertEqual(规范语言("xx-unknown"), "xx-unknown")


class 段落数据结构测试(unittest.TestCase):

    def test_字段与默认(self):
        段 = 识别段落(开始秒=1.5, 结束秒=3.25, 文本="你好世界")
        self.assertAlmostEqual(段.开始秒, 1.5)
        self.assertAlmostEqual(段.结束秒, 3.25)
        self.assertEqual(段.文本, "你好世界")
        self.assertAlmostEqual(段.时长秒, 1.75)

    def test_时长不为负(self):
        self.assertEqual(识别段落(5.0, 4.0, "倒挂").时长秒, 0.0)

    def test_转字典(self):
        self.assertEqual(识别段落(0.0, 1.0, "啊").转字典(),
                         {"开始秒": 0.0, "结束秒": 1.0, "文本": "啊"})

    def test_可比较可迭代(self):
        甲 = 识别段落(0.0, 1.0, "甲")
        self.assertEqual(甲, 识别段落(0.0, 1.0, "甲"))
        self.assertEqual(len([甲, 识别段落(1.0, 2.0, "乙")]), 2)

    def test_既是dataclass也是dict(self):
        """回归锁：下游按字典取字段，纯 dataclass 会被整条丢掉。"""
        import dataclasses
        import json
        段 = 识别段落(0.37, 3.17, "開放時間早上9點")
        self.assertTrue(dataclasses.is_dataclass(段))
        self.assertEqual([f.name for f in dataclasses.fields(段)],
                         ["开始秒", "结束秒", "文本"])
        self.assertIsInstance(段, dict)                     # ← 关键
        self.assertIn("开始秒", 段)
        self.assertAlmostEqual(段["开始秒"], 0.37)
        self.assertAlmostEqual(段.get("结束秒"), 3.17)
        self.assertEqual(段.get("文本"), "開放時間早上9點")
        self.assertTrue(段)                                  # 空文本段也不能是"假值"
        self.assertEqual(json.loads(json.dumps(段, ensure_ascii=False)),
                         {"开始秒": 0.37, "结束秒": 3.17, "文本": "開放時間早上9點"})

    def test_字段与字典键保持同步(self):
        段 = 识别段落(1.0, 2.0, "原文")
        段.文本 = "改过"
        self.assertEqual(段["文本"], "原文",
                         "直接改属性不会同步字典键——这是已知取舍，"
                         "识别结果本来就是只读的；写这条用例是为了把行为钉住")

    def test_能被真的字幕助手归一化(self):
        """跟 ``v8_3/AI/字幕助手.py`` 真联调一次（它按字典取字段）。"""
        try:
            from v8_3.AI.字幕助手 import 字幕助手
        except Exception as 错误:                            # pragma: no cover
            self.skipTest(f"字幕助手 暂时用不了：{type(错误).__name__}: {错误}")
        条目 = 字幕助手().识别结果转条目([
            识别段落(0.37, 5.37, "開放時間早上9點至下午5點"),
            识别段落(5.37, 6.00, "   "),                     # 空文本应被丢掉
        ])
        self.assertEqual(len(条目), 1, "识别段落 没被字幕助手认出来（会被静默丢光）")
        self.assertAlmostEqual(条目[0].开始秒, 0.37)
        self.assertAlmostEqual(条目[0].结束秒, 5.37)
        self.assertEqual(条目[0].文本, "開放時間早上9點至下午5點")


class SRT转换测试(unittest.TestCase):

    def test_标准格式(self):
        文本 = 转SRT([识别段落(0.37, 3.17, "開放時間早上9點"),
                     识别段落(3.17, 5.37, "至下午5點")])
        行 = 文本.splitlines()
        self.assertEqual(行[0], "1")
        self.assertEqual(行[1], "00:00:00,370 --> 00:00:03,170")
        self.assertEqual(行[2], "開放時間早上9點")
        self.assertEqual(行[4], "2")
        self.assertEqual(行[5], "00:00:03,170 --> 00:00:05,370")

    def test_超过一小时(self):
        文本 = 转SRT([识别段落(3661.5, 3662.0, "一小时零一秒")])
        self.assertIn("01:01:01,500 --> 01:01:02,000", 文本)

    def test_空文本段落被跳过(self):
        文本 = 转SRT([识别段落(0.0, 1.0, "  "), 识别段落(1.0, 2.0, "有内容")])
        self.assertEqual(len(文本.strip().splitlines()), 3)
        self.assertTrue(文本.strip().startswith("1"))

    def test_空列表(self):
        self.assertEqual(转SRT([]), "")


class 可用语义测试(unittest.TestCase):
    """`可用()` 的核心契约：**不抛异常**、返回二元组、不可用时给得出修复步骤。"""

    def test_返回二元组(self):
        可用, 说明 = 语音识别器().可用()
        self.assertIsInstance(可用, bool)
        self.assertIsInstance(说明, str)
        self.assertTrue(说明.strip())

    def test_默认实例可用性自洽(self):
        """模型装没装都要自洽：装了→说明里带"就绪"；没装→必须带修复步骤。"""
        可用, 说明 = 语音识别器().可用()
        if 可用:
            self.assertIn("就绪", 说明)
            self.assertIn("MB", 说明)
        else:
            self.assertIn("修复步骤", 说明)
            self.assertIn("hf-mirror", 说明)

    def test_模型目录不存在时不可用且给修复步骤(self):
        可用, 说明 = 语音识别器(模型目录="/tmp/v8_3_绝对不存在的目录_9f8e7d").可用()
        self.assertFalse(可用)
        self.assertIn("没找到完整可用的 Whisper 模型", 说明)
        self.assertIn("修复步骤", 说明)
        self.assertIn("venv", 说明)
        self.assertIn("hf-mirror.com", 说明)
        # 修复步骤必须指向**项目自带**的独立解释器，不能指向任何外部环境
        self.assertIn(str(独立解释器), 说明)

    def test_模型文件残缺时报没下完(self):
        import tempfile
        临时 = Path(tempfile.mkdtemp(prefix="v8_3_残缺模型_"))
        目录 = 临时 / "faster-whisper-small"
        目录.mkdir(parents=True)
        (目录 / "model.bin").write_bytes(b"\x00" * 128)      # 假的、极小
        (目录 / "config.json").write_text("{}", encoding="utf-8")
        (目录 / "tokenizer.json").write_text("{}", encoding="utf-8")
        (目录 / "vocabulary.txt").write_text("", encoding="utf-8")
        可用, 说明 = 语音识别器(模型目录=str(临时)).可用()
        self.assertFalse(可用)
        self.assertIn("明显没下完", 说明)
        self.assertIn("修复步骤", 说明)

    def test_乱七八糟的参数也不抛异常(self):
        怪 = [
            {"模型目录": "/dev/null/不可能/路径"},
            {"模型目录": "", "模型大小": "../../etc"},
            {"模型大小": "根本不存在的模型名"},
            {"模型目录": "/proc/self/mem"},
            {"模型大小": ""},
            {"设备": "", "计算类型": ""},
        ]
        for 参数 in 怪:
            try:
                可用, 说明 = 语音识别器(**参数).可用()
            except Exception as 错误:            # pragma: no cover
                self.fail(f"可用() 不该抛异常，参数={参数}，却抛了 {type(错误).__name__}: {错误}")
            self.assertIsInstance(可用, bool, f"参数={参数}")
            self.assertIsInstance(说明, str, f"参数={参数}")

    def test_模型信息在不可用时也不抛(self):
        信息 = 语音识别器(模型目录="/tmp/v8_3_不存在_abc").模型信息()
        self.assertIsInstance(信息, dict)
        self.assertFalse(信息["已就绪"])


class 模型信息测试(unittest.TestCase):

    def test_约定字段齐全(self):
        信息 = 语音识别器().模型信息()
        for 键 in ("模型", "目录", "大小MB", "设备", "计算类型", "已就绪"):
            self.assertIn(键, 信息, f"模型信息() 缺少约定字段 {键}")
        self.assertIsInstance(信息["模型"], str)
        self.assertIsInstance(信息["目录"], str)
        self.assertIsInstance(信息["大小MB"], (int, float))
        self.assertIsInstance(信息["已就绪"], bool)
        self.assertEqual(信息["设备"], "cpu")
        self.assertEqual(信息["计算类型"], "int8")

    def test_默认参数(self):
        信息 = 语音识别器().模型信息()
        self.assertEqual(信息["模型"], 语音识别模块.默认模型大小)
        self.assertTrue(信息["目录"].startswith(str(语音识别模块.默认模型目录)))

    def test_体积与就绪自洽(self):
        信息 = 语音识别器().模型信息()
        if 信息["已就绪"]:
            self.assertGreater(信息["大小MB"], 1.0)
            self.assertEqual(信息["缺少文件"], [])
        else:
            self.assertTrue(信息["缺少文件"] or not Path(信息["目录"]).is_dir())

    def test_附加字段(self):
        信息 = 语音识别器().模型信息()
        for 键 in ("运行方式", "环境目录", "环境Python", "当前解释器", "模型已加载",
                   "支持语言数", "缺少文件", "仓库"):
            self.assertIn(键, 信息)
        self.assertIn(信息["运行方式"], ("进程内", "子进程"))
        self.assertGreaterEqual(信息["支持语言数"], 90)

    def test_换模型大小会换目录(self):
        甲 = 语音识别器(模型大小="tiny").模型信息()
        乙 = 语音识别器(模型大小="small").模型信息()
        self.assertIn("tiny", 甲["目录"])
        self.assertIn("small", 乙["目录"])
        self.assertEqual(甲["仓库"], "Systran/faster-whisper-tiny")


class 延迟加载与单例测试(unittest.TestCase):
    """构造必须"不做重活"；模型加载必须"单例缓存"。"""

    def setUp(self):
        语音识别模块.卸载全部()

    def tearDown(self):
        语音识别模块.卸载全部()

    def test_构造很快且不起进程(self):
        起点 = time.perf_counter()
        for _ in range(50):
            语音识别器()
        耗 = time.perf_counter() - 起点
        self.assertLess(耗, 2.0, f"构造 50 次花了 {耗:.2f}s，太重了（应当只记参数）")
        self.assertEqual(语音识别模块._工作进程缓存, {},
                         "构造时不该拉起工作进程（延迟加载）")
        for 大小 in ("tiny", "medium"):
            self.assertFalse(语音识别器(模型大小=大小).模型是否已加载())

    def test_构造不加载模型(self):
        self.assertEqual(语音识别模块._进程内模型缓存, {})
        self.assertFalse(语音识别器().模型是否已加载())

    def test_卸载不抛异常(self):
        识别器 = 语音识别器()
        识别器.卸载()          # 全新实例，什么都没加载，也要安全
        识别器.卸载()          # 幂等
        语音识别模块.卸载全部()
        self.assertEqual(语音识别模块._工作进程缓存, {})

    def test_同配置共用工作进程(self):
        """同一 (解释器, 设备, 计算类型) 只应有一个工作进程 —— 模型单例的地基。"""
        if not 语音识别器().模型信息()["环境Python存在"]:
            self.skipTest("局部环境还没建，跳过")
        try:
            甲 = 语音识别模块._取工作进程("cpu", "int8")
            乙 = 语音识别模块._取工作进程("cpu", "int8")
        except 识别失败 as 错误:
            self.skipTest(f"局部环境拉不起来：{错误}")
        self.assertIs(甲, 乙, "同配置必须复用同一个工作进程")

    def test_同配置的识别器共享环境(self):
        甲 = 语音识别器(设备="cpu", 计算类型="int8")
        乙 = 语音识别器(设备="cpu", 计算类型="int8")
        self.assertEqual(甲.模型信息()["环境Python"], 乙.模型信息()["环境Python"])
        self.assertEqual(甲.模型信息()["目录"], 乙.模型信息()["目录"])


class 异常语义测试(unittest.TestCase):

    def test_识别失败是RuntimeError(self):
        self.assertTrue(issubclass(识别失败, RuntimeError))
        self.assertIsInstance(识别失败("x"), Exception)

    def test_空路径抛识别失败(self):
        for 空 in ("", "   ", None):
            with self.assertRaises(识别失败) as 上下文:
                语音识别器().识别(空)
            self.assertTrue(str(上下文.exception).strip())

    def test_模型缺失时抛的是识别失败且可读(self):
        识别器 = 语音识别器(模型目录="/tmp/v8_3_真没有这个目录_123")
        可用, _ = 识别器.可用()
        if 可用:                                  # pragma: no cover
            self.skipTest("本机默认模型可用，跳过不可用路径")
        with self.assertRaises(识别失败) as 上下文:
            识别器.识别("/tmp/随便什么.wav")
        消息 = str(上下文.exception)
        self.assertIn("不可用", 消息)
        self.assertIn("修复步骤", 消息)

    def test_不存在的媒体文件不会静默成功(self):
        """模型可用时，喂一个不存在的文件应当抛识别失败（而不是返回空列表）。"""
        识别器 = 语音识别器()
        可用, _ = 识别器.可用()
        if not 可用:
            self.skipTest("本机没有可用的局部模型，跳过")
        with self.assertRaises(识别失败):
            识别器.识别("/tmp/v8_3_绝对不存在的媒体文件_zzz.wav")


# ==========================================================================
# 慢测试（真跑模型；默认跳过）
# ==========================================================================

@慢测试()
class 真识别慢测试(unittest.TestCase):
    """真的把 3 秒中文音频跑一遍 Whisper —— 用项目内生成的音频，不联网。"""

    @classmethod
    def setUpClass(cls):
        就绪, 说明 = _准备三秒音频()
        if not 就绪:
            raise unittest.SkipTest(说明)
        cls.音频 = 说明
        cls.识别器 = 语音识别器()
        可用, 原因 = cls.识别器.可用()
        if not 可用:
            raise unittest.SkipTest("局部语音识别环境不可用：\n" + 原因)
        cls.耗 = {}

    def test_01_音频确实有3秒(self):
        import subprocess
        if not __import__("shutil").which("ffprobe"):
            self.skipTest("没有 ffprobe")
        结果 = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                             "-of", "csv=p=0", self.音频], capture_output=True, text=True)
        时长 = float(结果.stdout.strip())
        self.assertGreater(时长, 2.5)
        self.assertLess(时长, 3.6)
        type(self).耗["音频时长"] = 时长

    def test_02_首次识别_含模型加载(self):
        进度: list[tuple[float, float]] = []
        起点 = time.perf_counter()
        段 = self.识别器.识别(self.音频, 语言="中文",
                          进度回调=lambda 百分比, 已处理秒: 进度.append((百分比, 已处理秒)))
        墙钟 = time.perf_counter() - 起点
        信息 = self.识别器.最近识别信息
        type(self).耗.update(信息)
        type(self).耗["首次墙钟"] = 墙钟
        type(self).耗["段"] = 段

        print(f"\n  [慢测试] 音频      : {self.音频} "
              f"({type(self).耗.get('音频时长', 0):.3f}s)")
        print(f"  [慢测试] 模型加载  : {信息.get('加载耗时', 0):.3f}s")
        print(f"  [慢测试] 识别总耗时: {墙钟:.3f}s（模式 {信息.get('模式')}，"
              f"倍速 {type(self).耗.get('音频时长', 0) / 墙钟:.2f}x 实时）")
        print(f"  [慢测试] 检出语言  : {信息.get('语言')} p={信息.get('语言概率')}")
        for 序号, 项 in enumerate(段, 1):
            print(f"  [慢测试] 段落 {序号}    : {项.开始秒:.2f}-{项.结束秒:.2f} {项.文本!r}")

        self.assertIsInstance(段, list)
        self.assertTrue(段, "3 秒中文语音应当至少识别出一个段落")
        for 项 in 段:
            self.assertIsInstance(项, 识别段落)
            self.assertIsInstance(项.文本, str)
            self.assertLessEqual(项.开始秒, 项.结束秒)
            self.assertGreaterEqual(项.开始秒, 0.0)
        self.assertTrue(any(项.文本.strip() for 项 in 段), "识别文本不该全空")
        self.assertTrue(进度, "进度回调应当至少被调用一次")
        self.assertAlmostEqual(进度[-1][0], 100.0, places=3)
        self.assertTrue(all(0.0 <= p <= 100.0 for p, _ in 进度))

    def test_03_二次识别_不再加载模型(self):
        if "首次墙钟" not in type(self).耗:
            self.skipTest("上一用例没跑成")
        起点 = time.perf_counter()
        段 = self.识别器.识别(self.音频, 语言="中文")
        墙钟 = time.perf_counter() - 起点
        加载 = self.识别器.最近识别信息.get("加载耗时", -1)
        print(f"\n  [慢测试] 二次识别  : 墙钟 {墙钟:.3f}s，模型加载 {加载:.6f}s（单例缓存→应≈0，"
              f"真加载要 1~2s）")
        self.assertTrue(段)
        # 注意：命中缓存时这里是个 1e-5 量级的"取缓存"耗时，不可能正好 0.0，
        # 所以判据是"远小于一次真实加载"，而不是"等于 0"
        self.assertLess(加载, 0.05,
                        f"第二次识别花了 {加载:.3f}s 加载模型 —— 单例缓存没生效")

    def test_04_自动检测语言(self):
        段 = self.识别器.识别(self.音频)
        语言 = self.识别器.最近识别信息.get("语言")
        print(f"\n  [慢测试] 自动检测  : 语言={语言} 文本={[项.文本 for 项 in 段]}")
        self.assertEqual(语言, "zh", "这段音频是中文，自动检测应当给出 zh")
        self.assertTrue(段)

    def test_05_生成SRT字幕(self):
        段 = self.识别器.识别(self.音频, 语言="中文")
        srt = 转SRT(段)
        print(f"\n  [慢测试] SRT 字幕  :\n" + "\n".join("    " + 行 for 行 in srt.splitlines()))
        self.assertIn("-->", srt)
        self.assertTrue(srt.strip().startswith("1\n"))

    def test_06_卸载后内存释放(self):
        self.识别器.卸载()
        self.assertFalse(self.识别器.模型是否已加载(), "卸载后不该还挂着模型")
        # 卸载后再识别应当能自动恢复（重新拉起 + 重新加载）
        段 = self.识别器.识别(self.音频, 语言="中文")
        print(f"\n  [慢测试] 卸载后重识别: 加载={self.识别器.最近识别信息.get('加载耗时', 0):.3f}s "
              f"文本={[项.文本 for 项 in 段]}")
        self.assertTrue(段, "卸载后重新识别应当照常工作")


@慢测试()
class 直链识别慢测试(unittest.TestCase):
    """http 直链路径：起一个本机 http 服务，验证"先 ffmpeg 抽音轨再识别"这条路。"""

    def test_直链识别(self):
        import functools
        import threading
        from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

        if not shutil_which("ffmpeg"):
            self.skipTest("没有 ffmpeg，直链抽音轨这条路走不通")
        就绪, 音频 = _准备三秒音频()
        if not 就绪:
            self.skipTest(音频)
        识别器 = 语音识别器()
        可用, 原因 = 识别器.可用()
        if not 可用:
            self.skipTest("局部语音识别环境不可用：\n" + 原因)

        目录 = str(Path(音频).parent)
        处理器 = functools.partial(SimpleHTTPRequestHandler, directory=目录)
        服务 = ThreadingHTTPServer(("127.0.0.1", 0), 处理器)
        端口 = 服务.server_address[1]
        threading.Thread(target=服务.serve_forever, daemon=True).start()
        try:
            直链 = f"http://127.0.0.1:{端口}/{Path(音频).name}"
            起点 = time.perf_counter()
            段 = 识别器.识别(直链, 语言="中文")
            墙钟 = time.perf_counter() - 起点
            print(f"\n  [慢测试] 直链      : {直链}")
            print(f"  [慢测试] 耗时      : {墙钟:.3f}s，段落={[ (round(s.开始秒,2), round(s.结束秒,2), s.文本) for s in 段 ]}")
            self.assertTrue(段, "直链识别应当有结果")
            self.assertTrue(any(s.文本.strip() for s in 段))
        finally:
            服务.shutdown()
            服务.server_close()


def shutil_which(名字: str) -> bool:
    import shutil
    return bool(shutil.which(名字))


if __name__ == "__main__":
    unittest.main(verbosity=2)
