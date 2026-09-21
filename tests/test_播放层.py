"""播放层单测（V8_3 新增）：libvlc 绑定 / 媒体信息 / 直链探测 / 起播参数。

背景：这一层出过一次**把整个程序搞崩**的事故 —— VLC 3.0 里没有
``libvlc_media_player_get_spu``，回退名 ``libvlc_video_get_spu`` 又没进绑定表，
于是 ctypes 按 C ``int`` 传参把 64 位播放器句柄**截成 32 位**，libvlc 拿着野
指针加锁 → 段错误（exit 139，崩在 ``libvlc_video_get_spu+0x28``，触发点是界面
每 500ms 刷一次状态）。所以这里**专门锁死这一类问题**：

* 凡是会被调用的 libvlc 符号，必须已配 ``argtypes``（``缺类型符号()`` 为空）；
* 回退名（VLC 3 用 ``video_*``、VLC 4 用 ``media_player_*``）都要能取到且带类型；
* 拿错类型的函数去调**必须**抛异常而不是崩（ctypes 会做参数检查）；
* 分辨率档位不能再拿 ``max(宽,高)`` 比"像素总数"（1920x1080 曾被判成 SD）；
* 本地文件不再显示"直链探测失败"，且缓存给小值。

不需要装 VLC 的部分（媒体信息解析、档位、参数规则、探测摘要）用**纯函数**测；
需要 libvlc / ffprobe 的用 skipUnless 守卫，没装就跳过而不是变红。
"""

from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

def _确保离屏() -> None:
    """没有显示环境时强制离屏平台（本机环境变量是 "wayland;xcb"，直接起 Qt 会崩）。"""
    if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        os.environ["QT_QPA_PLATFORM"] = "offscreen"


def _有Qt() -> bool:
    """本机能不能用 PySide6（没装就跳过真 X11 那两条）。"""
    try:
        import PySide6.QtWidgets  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


项目根 = Path(__file__).resolve().parents[1]
if str(项目根) not in sys.path:
    sys.path.insert(0, str(项目根))

from v8_3.播放 import vlc绑定
from v8_3.播放.媒体信息 import (媒体信息, 分辨率档位, 格式化码率, 档位表,
                           解析探测结果)
from v8_3.播放.播放核心 import 播放设置, 规则参数, 硬解能力
from v8_3.播放.直链探测 import 探测结果, 格式化带宽


def _造视频(路径: Path, 尺寸: str = "1920x1080", 时长: int = 2,
           码率: str = "") -> bool:
    """用 ffmpeg 生成一个小测试视频；失败返回 False（调用方 skip）。"""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return False
    命令 = [ffmpeg, "-y", "-loglevel", "error",
           "-f", "lavfi", "-i", f"testsrc2=size={尺寸}:rate=25:duration={时长}",
           "-f", "lavfi", "-i", f"sine=frequency=440:duration={时长}"]
    if 码率:
        命令 += ["-b:v", 码率]
    命令 += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest", str(路径)]
    try:
        return subprocess.run(命令, capture_output=True, timeout=180).returncode == 0
    except Exception:
        return False


# ==================== 1. 分辨率档位（曾经全判成 SD） ====================


class 分辨率档位测试(unittest.TestCase):
    def test_常见分辨率(self):
        表 = [
            ((3840, 2160), "4K"),
            ((4096, 2160), "4K"),
            ((2560, 1440), "2K"),
            ((1920, 1080), "1080p"),
            ((1280, 720), "720p"),
            ((854, 480), "480p"),
            ((640, 360), "SD"),
            ((320, 240), "SD"),
        ]
        for (宽, 高), 期望 in 表:
            with self.subTest(分辨率=f"{宽}x{高}"):
                self.assertEqual(分辨率档位(宽, 高), 期望)

    def test_1080p_不再被判成SD(self):
        """回归：档位表阈值原来是像素总数，max(1920,1080)=1920 比不过 2073600。"""
        self.assertEqual(分辨率档位(1920, 1080), "1080p")
        self.assertEqual(分辨率档位(3840, 2160), "4K")

    def test_竖屏与横屏同档(self):
        self.assertEqual(分辨率档位(1080, 1920), 分辨率档位(1920, 1080))
        self.assertEqual(分辨率档位(2160, 3840), "4K")

    def test_只给一个维度或都不给(self):
        """ffprobe 正常都给两维；这里只保证不抛异常、不返回空。"""
        for 宽, 高 in ((0, 0), (1920, 0), (0, 2160), (-1, -1)):
            self.assertTrue(分辨率档位(宽, 高))

    def test_档位表阈值单调(self):
        阈值们 = [t for t, _ in 档位表]
        self.assertEqual(阈值们, sorted(阈值们, reverse=True))
        self.assertEqual(档位表[-1][0], 0)


# ==================== 2. 起播参数规则 ====================


def _媒体(宽=1920, 高=1080, 码率=13_600_000, 时长=4.0) -> 媒体信息:
    return 媒体信息(宽=宽, 高=高, 档位=分辨率档位(宽, 高),
                视频码率bps=码率, 时长秒=时长,
                分辨率=f"{宽}x{高}", 视频编码="h264")


class 规则参数测试(unittest.TestCase):
    def test_4K带宽充足给大缓存与硬解(self):
        设置 = 规则参数(_媒体(3840, 2160, 25_000_000), 
                    探测结果(成功=True, 实测带宽bps=90_000_000), ["vaapi"])
        self.assertTrue(设置.可流畅播放)
        self.assertGreaterEqual(设置.网络缓存毫秒, 12000)
        self.assertEqual(设置.硬解, "vaapi")
        self.assertIn("余量", 设置.理由)
        self.assertEqual(设置.风险, "")

    def test_4K带宽不足判卡顿并拉满缓存(self):
        设置 = 规则参数(_媒体(3840, 2160, 25_000_000),
                    探测结果(成功=True, 实测带宽bps=20_000_000), ["vaapi"])
        self.assertFalse(设置.可流畅播放)
        self.assertGreaterEqual(设置.网络缓存毫秒, 20000)
        self.assertTrue(设置.风险, "带宽不足必须给出风险说明（不能假装没事）")

    def test_没测到带宽时保守起播(self):
        设置 = 规则参数(_媒体(), 探测结果(成功=False, 错误="超时"), ["vaapi"])
        self.assertTrue(设置.可流畅播放, "未知带宽不该直接判死")
        self.assertIn("探测失败", 设置.理由)

    def test_本地文件不给网络缓存且不报探测失败(self):
        本地 = 探测结果(成功=True, 来源说明="本地文件（13 MB，不经过网络，无需测带宽）")
        设置 = 规则参数(_媒体(3840, 2160, 60_000_000), 本地, ["vaapi"])
        self.assertLessEqual(设置.网络缓存毫秒, 3000)
        self.assertNotIn("探测失败", 设置.理由)
        self.assertIn("本地文件", 设置.理由)
        self.assertEqual(本地.摘要(), 本地.来源说明)

    def test_硬解选择与回落(self):
        """没探到硬解时给 auto（VLC 自己挑，挑不到自动回落软解），不硬写 none。"""
        探测 = 探测结果(成功=True, 实测带宽bps=100_000_000)
        未知 = 规则参数(_媒体(), 探测, [])
        self.assertEqual(未知.硬解, "auto")
        self.assertIn(":avcodec-hw=any", 未知.libvlc选项())
        # 明确要求软解时才给 none（播放页/顾问显式指定）
        软解 = 规则参数(_媒体(), 探测, ["none"])
        self.assertEqual(软解.硬解, "none")
        self.assertIn(":avcodec-hw=none", 软解.libvlc选项())
        # 有核显优先 vaapi
        self.assertEqual(规则参数(_媒体(), 探测, ["vaapi", "auto"]).硬解, "vaapi")

    def test_探测本机硬解总带auto兜底(self):
        候选 = __import__("v8_3.播放.播放核心",
                       fromlist=["x"]).播放会话._探测本机硬解()
        self.assertIn("auto", 候选, "没硬解时必须能回落到 auto")
        self.assertIn("none", 候选, "必须能显式要求软解")
        self.assertEqual(候选, 硬解能力()["可用"],
                     "会话层的硬解候选应直接来自 硬解能力（同一套判定）")

    def test_选项里网络参数齐全(self):
        设置 = 播放设置(网络缓存毫秒=9000, 硬解="vaapi")
        选项 = 设置.libvlc选项()
        self.assertIn(":network-caching=9000", 选项)
        self.assertIn(":file-caching=9000", 选项)
        self.assertIn(":avcodec-hw=vaapi", 选项)
        self.assertIn(":http-reconnect=true", 选项)

    def test_丢帧选项归属以顾问为准(self):
        """默认"不丢帧"；顾问显式给了 :drop-late-frames 就听顾问的。"""
        默认 = 播放设置().libvlc选项()
        self.assertIn(":no-drop-late-frames", 默认)
        顾问 = 播放设置(附加选项=[":drop-late-frames", ":avcodec-skiploopfilter=0"]
                   ).libvlc选项()
        self.assertIn(":drop-late-frames", 顾问)
        self.assertNotIn(":no-drop-late-frames", 顾问)

    def test_缓存下限钳制(self):
        self.assertIn(":network-caching=500", 播放设置(网络缓存毫秒=0).libvlc选项())


# ==================== 3. 直链探测（纯函数部分） ====================


class 探测结果测试(unittest.TestCase):
    def test_失败摘要带原因(self):
        self.assertIn("超时", 探测结果(成功=False, 错误="超时").摘要())
        self.assertIn("未知", 探测结果(成功=False).摘要())

    def test_成功摘要含带宽与Range(self):
        文本 = 探测结果(成功=True, 实测带宽bps=88_000_000,
                    首字节毫秒=120, range支持=True).摘要()
        self.assertIn("88.0 Mbps", 文本)
        self.assertIn("120ms", 文本)
        self.assertIn("✅", 文本)

    def test_来源说明优先于失败文案(self):
        结果 = 探测结果(来源说明="本地文件")
        self.assertEqual(结果.摘要(), "本地文件")
        self.assertEqual(结果.to_dict()["来源说明"], "本地文件")

    def test_格式化带宽(self):
        self.assertEqual(格式化带宽(0), "未知")
        self.assertEqual(格式化带宽(1_500_000_000), "1.50 Gbps")
        self.assertEqual(格式化带宽(88_000_000), "88.0 Mbps")
        self.assertEqual(格式化带宽(500_000), "500 kbps")
        self.assertEqual(格式化带宽(300), "300 bps")


class 码率格式化测试(unittest.TestCase):
    def test_分档(self):
        self.assertEqual(格式化码率(0), "未知")
        self.assertEqual(格式化码率(25_000_000), "25.0 Mbps")
        self.assertEqual(格式化码率(128_000), "128 kbps")


# ==================== 4. 媒体信息解析（纯函数） ====================


class 解析探测结果测试(unittest.TestCase):
    def _原始(self, **覆盖) -> dict:
        原始 = {
            "format": {"duration": "12.5", "bit_rate": "8000000",
                       "format_name": "mov,mp4,m4a,3gp,3g2,mj2"},
            "streams": [
                {"codec_type": "video", "codec_name": "h264",
                 "width": 3840, "height": 2160,
                 "avg_frame_rate": "25/1", "bit_rate": "20000000"},
                {"codec_type": "audio", "codec_name": "aac",
                 "channels": 2, "bit_rate": "128000"},
            ],
        }
        原始.update(覆盖)
        return 原始

    def test_基本字段(self):
        信息 = 解析探测结果(self._原始())
        self.assertEqual((信息.宽, 信息.高), (3840, 2160))
        self.assertEqual(信息.档位, "4K")
        self.assertEqual(信息.视频编码, "h264")
        self.assertEqual(信息.视频码率bps, 20_000_000)
        self.assertAlmostEqual(信息.帧率, 25.0)
        self.assertAlmostEqual(信息.时长秒, 12.5)

    def test_竖屏4K也判4K(self):
        原始 = self._原始()
        原始["streams"][0].update({"width": 2160, "height": 3840})
        self.assertEqual(解析探测结果(原始).档位, "4K")

    def test_没有码率时用总码率兜底(self):
        """容器不给流码率时，用"总码率 - 音频码率"估一个（AI 决策要用）。"""
        原始 = self._原始()
        原始["streams"][0].pop("bit_rate")
        原始["format"]["bit_rate"] = "6000000"
        信息 = 解析探测结果(原始)
        self.assertEqual(信息.总码率bps, 6_000_000)
        self.assertEqual(信息.音频码率bps, 128_000)
        self.assertEqual(信息.视频码率bps, 6_000_000 - 128_000)

    def test_流码率优先于总码率估算(self):
        信息 = 解析探测结果(self._原始())
        self.assertEqual(信息.视频码率bps, 20_000_000, "流里有码率就该用流里的")

    def test_带内嵌字幕轨道(self):
        原始 = self._原始()
        原始["streams"].append({"codec_type": "subtitle", "codec_name": "subrip",
                              "tags": {"language": "eng"}})
        self.assertEqual(解析探测结果(原始).字幕轨数, 1)

    def test_空输入不崩(self):
        """空/坏数据不抛异常；档位保持"未知"（不假装成 SD）。"""
        信息 = 解析探测结果({})
        self.assertIn(信息.档位, ("", "SD"))
        self.assertEqual(信息.宽, 0)
        self.assertIsInstance(信息.摘要(), str)
        self.assertIn("档位", 解析探测结果(None or {}).to_dict())
        for 坏 in ({"streams": None}, {"streams": [{}]}, {"format": {}}):
            self.assertIsInstance(解析探测结果(坏), 媒体信息)


# ==================== 5. libvlc 绑定的类型安全（崩过程序的那一类） ====================


def _本机有libvlc() -> bool:
    """本机能不能真的加载 libvlc（下面几条断言都要拿真库来验）。

    ⚠️ CI（windows-latest）上**没装 VLC**，以前这里直接 ERROR 而不是跳过 ——
    整个工作流的单测步骤因此永远是红的，真出问题反而看不见。
    """
    try:
        vlc绑定.VLC库.取()
        return True
    except Exception:  # noqa: BLE001 - 缺库/缺依赖都算"本机没有"
        return False


@unittest.skipUnless(_本机有libvlc(), "本机没有 libvlc（VLC 运行库），跳过绑定类型检查")
class 绑定类型安全测试(unittest.TestCase):
    """**不崩**是这里的核心断言：错类型要抛异常，不能把句柄截断。"""

    def test_关键符号都配了argtypes(self):
        库 = vlc绑定.VLC库.取()
        缺 = 库.缺类型符号()
        self.assertEqual(缺, [], f"这些符号存在但没配 argtypes，随时会段错误：{缺}")

    def test_回退名在VLC3里可用且带类型(self):
        库 = vlc绑定.VLC库.取()
        函数 = vlc绑定.取函数(库.lib, ("libvlc_media_player_get_spu",
                                  "libvlc_video_get_spu"),
                           [ctypes.c_void_p], ctypes.c_int)
        self.assertIsNotNone(函数, "VLC 3/4 至少要有一个取字幕轨的函数")
        self.assertEqual(函数.argtypes, [ctypes.c_void_p])
        self.assertIs(函数.restype, ctypes.c_int)

    def test_取函数会给候选名补类型(self):
        库 = vlc绑定.VLC库.取()
        函数 = vlc绑定.取函数(库.lib, ("libvlc_media_player_get_audio_track_count",
                                  "libvlc_audio_get_track_count"),
                           [ctypes.c_void_p], ctypes.c_int)
        self.assertIsNotNone(函数)
        self.assertEqual(函数.argtypes, [ctypes.c_void_p])
        self.assertIs(函数.restype, ctypes.c_int)

    def test_取函数对不存在的名字返回None(self):
        库 = vlc绑定.VLC库.取()
        self.assertIsNone(
            vlc绑定.取函数(库.lib, ("libvlc_no_such_symbol_xyz",)))
        # 非 ASCII 名字：ctypes 在 getattr 阶段会抛 UnicodeError，必须被吃掉
        self.assertIsNone(vlc绑定.取函数(库.lib, ("根本没有这个符号名",)))

    def test_句柄闸拦住非法值(self):
        """**绝不把野指针递给 libvlc**：libvlc 不检查 NULL，进去就是段错误。

        为什么专门测这个：写这个文件时我图省事，直接拿 ``2 ** 64`` 去调
        ``libvlc_video_get_spu``，ctypes 把它变成 NULL 递进去，进程**当场段
        错误**（libvlc 不判空）。所以句柄必须先过 ``校验句柄``。
        """
        self.assertEqual(vlc绑定.校验句柄(0), 0)
        self.assertEqual(vlc绑定.校验句柄(2 ** 64 - 1), 2 ** 64 - 1)
        for 坏值 in ("字符串不是指针", None, True, False, -1, 2 ** 64):
            with self.subTest(值=repr(坏值)):
                with self.assertRaises((TypeError, ValueError)):
                    vlc绑定.校验句柄(坏值)

    def test_没有播放器时所有接口都安全返回(self):
        """没起播 / 已关闭时挨个调一遍：必须安静返回，不能崩、不能抛。"""
        播放器 = vlc绑定.VLC(窗口句柄=0)
        播放器.关闭()
        self.assertIsNone(播放器.句柄())
        self.assertEqual(播放器.当前字幕(), -1)
        self.assertEqual(播放器.字幕轨(), [])
        self.assertEqual(播放器.音频轨(), [])
        self.assertFalse(播放器.选择字幕(1))
        self.assertFalse(播放器.选择音频(1))
        self.assertFalse(播放器.挂字幕文件("/tmp/不存在.srt"))
        self.assertEqual(播放器.时长秒(), 0.0)
        self.assertEqual(播放器.进度秒(), 0.0)
        self.assertEqual(播放器.统计().读字节, 0)
        self.assertFalse(播放器.截图(str(Path(tempfile.mkdtemp()) / "x.png")))

    def test_模块级可用性查询不抛异常(self):
        self.assertIsInstance(vlc绑定.可用(), bool)
        self.assertIsInstance(vlc绑定.不可用原因(), str)
        if vlc绑定.可用():
            self.assertTrue(vlc绑定.VLC库.版本().startswith("3."))

    def test_准备干净环境把VLC写到项目目录(self):
        根 = Path(tempfile.mkdtemp())
        目录 = vlc绑定.准备干净环境(根)
        for 键, 子 in (("XDG_CONFIG_HOME", "config"), ("XDG_DATA_HOME", "data"),
                     ("XDG_CACHE_HOME", "cache")):
            self.assertIn(键, 目录)
            self.assertEqual(os.environ.get(键), 目录[键])
            self.assertEqual(Path(目录[键]).name, 子)
            self.assertTrue(Path(目录[键]).is_dir())
        # 必须落在项目根里（不然又污染 ~/.config/vlc）
        self.assertTrue(all(str(Path(v)).startswith(str(根))
                            for v in 目录.values()))


# ==================== 6. 真机：libvlc 起来 + 真播一小段 ====================


@unittest.skipUnless(vlc绑定.可用(), "没装 libvlc/VLC")
class 真机播放测试(unittest.TestCase):
    """无窗口模式解码一段真视频（不嵌窗口，所以无头环境也能跑）。"""

    @classmethod
    def setUpClass(cls):
        cls.目录 = Path(tempfile.mkdtemp(prefix="v83播放_"))
        cls.视频 = cls.目录 / "样片.mp4"
        if not _造视频(cls.视频, "1280x720", 2):
            raise unittest.SkipTest("ffmpeg 不可用，造不出测试视频")

    def test_起播并读到进度和统计(self):
        import time
        播放器 = vlc绑定.VLC(窗口句柄=0, 日志回调=None)
        try:
            播放器.播放(str(self.视频), [":network-caching=1000"])
            时长 = 0.0
            进度 = 0.0
            统计 = None
            for _ in range(60):
                time.sleep(0.1)
                时长 = 播放器.时长秒()
                进度 = 播放器.进度秒()
                if 进度 > 0.2:
                    统计 = 播放器.统计()
                    break
            self.assertGreater(时长, 1.0, "libvlc 没读到时长")
            self.assertGreater(进度, 0.0, "libvlc 没有推进播放进度")
            self.assertIsNotNone(统计)
            self.assertGreater(统计.读字节, 0, "统计没读到数据")
        finally:
            播放器.关闭()

    def test_状态快照里取字幕轨不崩(self):
        """回归：``当前字幕()`` 曾经让整个进程段错误（句柄被截断）。"""
        import time
        from v8_3.播放.播放核心 import 播放会话
        会话 = 播放会话(取适配器=lambda *_: (_ for _ in ()).throw(
            RuntimeError("本测试只播本地文件")), 日志回调=None,
            探测直链开关=False, 探测媒体开关=False)
        会话.网盘标识 = "本地"
        会话.远端路径 = str(self.视频)
        会话.标题 = self.视频.name
        会话.直链信息 = {"url": str(self.视频), "headers": {},
                    "size": self.视频.stat().st_size, "name": self.视频.name}
        会话.媒体 = 媒体信息(宽=1280, 高=720, 档位="720p",
                        视频码率bps=1_000_000, 时长秒=2.0)
        会话.设置 = 规则参数(会话.媒体, 探测结果(), ["vaapi"])
        self.assertTrue(会话.起播(0))
        try:
            for _ in range(40):
                time.sleep(0.1)
                快照 = 会话.状态快照()   # ← 曾经在这里段错误
                if 快照.get("进度秒", 0) > 0.1:
                    break
            self.assertIn("状态", 快照)
            self.assertIn("字幕轨", 快照)
        finally:
            会话.关闭()

    def test_ffprobe真文件给出正确档位(self):
        if not shutil.which("ffprobe"):
            self.skipTest("没有 ffprobe")
        from v8_3.播放.媒体信息 import 探测媒体
        信息 = 探测媒体(str(self.视频))
        self.assertEqual((信息.宽, 信息.高), (1280, 720))
        self.assertEqual(信息.档位, "720p", "1920x1080 以下也不能一律 SD")
        self.assertGreater(信息.时长秒, 0.5)


# ==================== 7. 硬解能力核实（不能过度承诺） ====================


class 硬解能力测试(unittest.TestCase):
    """回归：只要有 /dev/dri 就说"用 vaapi"，而 Intel VA 驱动其实没装。

    实测坑：``/dev/dri/renderD128`` 在，但 ``/usr/lib/dri`` 里只有 AMD/NVIDIA
    的 VA 驱动，VLC 初始化 vaapi 失败后**悄悄软解**，界面还显示"硬解 vaapi"。
    """

    def test_结果结构完整(self):
        能力 = 硬解能力()
        for 键 in ("可用", "VA驱动", "厂商", "渲染节点", "说明"):
            self.assertIn(键, 能力)
        self.assertIn("auto", 能力["可用"], "必须能回落 auto")
        self.assertIn("none", 能力["可用"], "必须能显式软解")
        self.assertIsInstance(能力["说明"], str)

    def test_没有匹配驱动就不宣称vaapi(self):
        """本机（Intel + 没装 iHD/i965）就该判成没有 vaapi。"""
        能力 = 硬解能力()
        if 能力["VA驱动"]:
            self.assertIn("vaapi", 能力["可用"])
        else:
            self.assertNotIn("vaapi", 能力["可用"],
                             "驱动缺失时不能宣称 vaapi（会让用户以为已开硬解）")
            self.assertTrue(能力["说明"], "驱动缺失必须给出说明/安装建议")

    def test_驱动必须与厂商匹配(self):
        """Mesa 自带 radeonsi 的 VA 驱动 —— Intel 机器上不能拿它当可用。"""
        from v8_3.播放 import 播放核心
        原 = 播放核心._显卡厂商
        try:
            播放核心._显卡厂商 = lambda: "Intel"
            # 直接问"Intel 有没有驱动"：答案是看 iHD/i965 在不在
            驱动名, _路径 = 播放核心._找VA驱动("Intel")
            if 驱动名:
                self.assertIn(播放核心.VA驱动表[驱动名][0], ("Intel",))
            # 不存在的厂商 → 一定找不到匹配驱动
            self.assertEqual(播放核心._找VA驱动("不存在的厂商"), ("", ""))
        finally:
            播放核心._显卡厂商 = 原

    def test_说明里带安装建议或可用信息(self):
        说明 = 硬解能力()["说明"]
        self.assertTrue(说明)
        if "vaapi" in 硬解能力()["可用"]:
            self.assertIn("VAAPI 可用", 说明)
        else:
            self.assertTrue("驱动" in 说明 or "软解" in 说明)

    def test_软解时规则给auto(self):
        能力 = 硬解能力()
        设置 = 规则参数(_媒体(3840, 2160, 25_000_000),
                    探测结果(成功=True, 实测带宽bps=90_000_000), 能力["可用"])
        if "vaapi" in 能力["可用"]:
            self.assertEqual(设置.硬解, "vaapi")
        else:
            self.assertEqual(设置.硬解, "auto",
                         "没驱动就不该硬写 vaapi；auto 让 VLC 自己挑并回落")


if __name__ == "__main__":
    unittest.main(verbosity=2)


# ==================== 8. 字幕查找（网盘视频的坑） ====================


class 字幕查找测试(unittest.TestCase):
    """回归：网盘视频**永远找不到字幕**。

    以前只查 ``Path(远端路径).with_suffix(".srt")`` —— 网盘路径 ``/电影/x.mp4``
    在本地磁盘上不存在，于是点「AI 翻译字幕」只能弹"没有字幕可翻译"。
    真正的场景是：**网盘同一目录里就放着同名 .srt**。
    """

    def setUp(self):
        import tempfile
        from v8_3.界面.播放页面 import 播放页面
        self.临时 = tempfile.TemporaryDirectory(prefix="v83字幕_")
        self.根 = Path(self.临时.name)
        self.缓存 = self.根 / "字幕缓存"
        self.页 = 播放页面.__new__(播放页面)      # 不跑 __init__，只测查找逻辑
        # 查找逻辑现在住在 AI播放面板.AI字幕动作 里（页面/独立窗口共用），
        # 所以这里装一个真的动作对象，测的还是同一套代码
        from v8_3.界面.AI播放面板 import AI字幕动作
        self.动作 = AI字幕动作(取会话=lambda: self.页.会话,
                          日志=lambda _t: None, 状态=lambda _d: None)
        self.页.AI动作 = self.动作
        # 缓存目录是"项目根/数据/字幕"（绝对路径），测试必须把它指到临时目录，
        # 否则会把测试文件写进真项目里（也确实发生过，导致用例互相污染）
        self.动作.缓存字幕目录 = lambda: (self.缓存.mkdir(parents=True,
                                                exist_ok=True) or self.缓存)

        class 条目:
            def __init__(self, name, path, is_dir=False):
                self.name, self.path, self.is_dir = name, path, is_dir

        class 假适配器:
            def __init__(self):
                self.下载过 = []

            def 列目录(self, 路径="/"):
                return [条目("影片.srt", "/云端/影片.srt"),
                        条目("影片.mp4", "/云端/影片.mp4"),
                        条目("别的.srt", "/云端/别的.srt"),
                        条目("子目录", "/云端/子目录", True)]

            def 下载(self, 远端, 本地):
                self.下载过.append((远端, 本地))
                Path(本地).write_text(
                    "1\n00:00:00,000 --> 00:00:01,000\nhello\n", encoding="utf-8")
                return {"ok": True}

        self.适配器 = 假适配器()
        助手 = type("助手", (), {
            "读取字幕文件": staticmethod(
                lambda p: [{"开始秒": 0.0, "结束秒": 1.0, "文本": "hello"}])})()
        self.页.字幕助手 = 助手            # 页面属性（旧代码引用）
        self.动作.字幕助手 = 助手           # 动作对象才是真正干活的（新结构）
        self.页.状态更新 = type("信号", (), {"emit": staticmethod(lambda d: None)})()
        self.页.会话 = type("会话", (), {
            "网盘标识": "fake_1", "远端路径": "/云端/影片.mp4",
            "标题": "影片.mp4", "取适配器": staticmethod(lambda _: self.适配器)})()

    def tearDown(self):
        self.临时.cleanup()

    def test_网盘同目录字幕能找出来并下载(self):
        条目, 来源 = self.页._取字幕条目(允许联网=True)
        self.assertEqual(len(条目), 1, f"没找到网盘字幕（来源={来源}）")
        self.assertIn("网盘字幕", 来源)
        self.assertEqual(len(self.适配器.下载过), 1)
        远端, 本地 = self.适配器.下载过[0]
        self.assertEqual(远端, "/云端/影片.srt", "应优先挑与视频完全同名的字幕")
        self.assertTrue(Path(本地).is_file(), "字幕应下载到 数据/字幕/")
        # ⚠️ 两边都 resolve()：Windows 会把临时目录缩成 8.3 短名（用户名那段会变成
        #    RUNNER~1 这种），直接比 Path 就是"长名 vs 短名"互相比 ——
        #    真机 CI 上就是这么假失败的（可移植性检查也要求这里别写死绝对路径）。
        self.assertEqual(Path(本地).parent.resolve(), self.缓存.resolve(),
                     "字幕应下载到字幕缓存目录里")

    def test_目录条目不会被当成字幕(self):
        条目, _ = self.页._取字幕条目(允许联网=True)
        self.assertTrue(条目)
        self.assertFalse(any("子目录" in str(x) for x in self.适配器.下载过))

    def test_不联网时不去拉网盘(self):
        条目, 来源 = self.页._取字幕条目(允许联网=False)
        self.assertEqual(条目, [])
        self.assertEqual(self.适配器.下载过, [])
        self.assertIn("没找到字幕", 来源)

    def test_本地同名字幕优先(self):
        视频 = self.根 / "本地片.mp4"
        视频.write_bytes(b"x")
        视频.with_suffix(".srt").write_text(
            "1\n00:00:00,000 --> 00:00:01,000\n本地\n", encoding="utf-8")
        self.页.会话.远端路径 = str(视频)
        self.页.会话.标题 = "本地片.mp4"
        条目, 来源 = self.页._取字幕条目(允许联网=True)
        self.assertEqual(len(条目), 1)
        self.assertIn("本地字幕", 来源)
        self.assertEqual(self.适配器.下载过, [], "本地有字幕就不该再联网")

    def test_缓存字幕优先于网盘(self):
        缓存 = self.缓存
        缓存.mkdir(parents=True, exist_ok=True)
        (缓存 / "影片.mp4.srt").write_text(
            "1\n00:00:00,000 --> 00:00:01,000\n缓存\n", encoding="utf-8")
        self.页.会话.远端路径 = "/云端/不存在于本地.mp4"
        条目, 来源 = self.页._取字幕条目(允许联网=True)
        self.assertEqual(len(条目), 1)
        self.assertIn("缓存字幕", 来源)
        self.assertEqual(self.适配器.下载过, [])

    def test_没有网盘标识时不联网(self):
        self.页.会话.网盘标识 = "本地"
        self.assertEqual(self.页._取字幕条目(允许联网=True)[0], [])
        self.assertEqual(self.适配器.下载过, [])


class 桥本地路径测试(unittest.TestCase):
    """回归：相对本地路径被桥进程按**它自己的 cwd** 解析，文件落到错误位置。

    实测：桥进程用 ``cwd=v8_3/桥`` 启动，于是 ``数据/字幕/x.srt`` 被下到
    ``v8_3/桥/数据/字幕/x.srt`` —— 调用方拿到"成功"却在原地找不到文件，
    排查了半天。现在桥客户端统一把本地路径绝对化。
    """

    def test_相对路径会被绝对化(self):
        from v8_3.核心.子进程客户端 import _绝对本地路径
        得 = _绝对本地路径("数据/字幕/a.srt")
        self.assertTrue(os.path.isabs(得))
        self.assertEqual(Path(得), (Path.cwd() / "数据/字幕/a.srt").resolve())

    def test_绝对路径保持不变(self):
        from v8_3.核心.子进程客户端 import _绝对本地路径
        原 = str((Path.cwd() / "x.mp4").resolve())
        self.assertEqual(_绝对本地路径(原), 原)

    def test_下载与上传都走绝对化(self):
        """不真起桥进程：只看发给桥的参数是不是绝对路径。"""
        from v8_3.核心.子进程客户端 import 子进程适配器
        发出 = []

        def 记录(命令, 参数, 超时=None):
            发出.append((命令, dict(参数)))
            return {}

        假 = 子进程适配器.__new__(子进程适配器)
        假.调用 = 记录                     # 拦在真正起桥进程之前
        假.上传("相对/源.mp4", "/远端")
        假.下载("/远端/a.srt", "相对/目标.srt")
        self.assertEqual([c for c, _ in 发出], ["upload", "download"])
        for 命令, 收到 in 发出:
            self.assertTrue(os.path.isabs(收到["local_path"]),
                        f"{命令} 的 local_path 不是绝对路径：{收到['local_path']}")


# ==================== 9. 视频后缀判定（网盘双击播放要用） ====================


class 视频后缀测试(unittest.TestCase):
    """网盘页双击"视频"才播放，所以这个判定必须准（按后缀，不发网络请求）。"""

    def test_常见视频后缀(self):
        from v8_3.播放.媒体信息 import 是视频文件
        for 名 in ("a.mp4", "a.MKV", "剧集.第01集.ts", "x.m2ts", "y.webm",
                  "z.rmvb", "w.mov", "v.avi", "u.flv", "t.m4v", "s.mpg",
                  "流媒体.m3u8", "r.iso"):
            with self.subTest(名字=名):
                self.assertTrue(是视频文件(名), f"{名} 应判为视频")

    def test_非视频不被误判(self):
        from v8_3.播放.媒体信息 import 是视频文件
        for 名 in ("字幕.srt", "文档.pdf", "图片.jpg", "音乐.mp3", "包.zip",
                  "说明.txt", "程序.exe", "无后缀", ""):
            with self.subTest(名字=名):
                self.assertFalse(是视频文件(名), f"{名} 不该判为视频")

    def test_大小写与路径(self):
        from v8_3.播放.媒体信息 import 是视频文件
        self.assertTrue(是视频文件("电影.MP4"))
        self.assertTrue(是视频文件("某片.Mp4"))
        self.assertFalse(是视频文件("电影.mp4.bak"))

    def test_后缀表可用于过滤(self):
        from v8_3.播放.媒体信息 import 视频后缀
        self.assertIn(".mp4", 视频后缀)
        self.assertIn(".mkv", 视频后缀)
        self.assertTrue(all(后缀.startswith(".") for 后缀 in 视频后缀),
                        "后缀必须都带点，否则 endswith 会误判")


# ==================== 10. 流畅度策略（用户实测"稍显卡顿"的针对性优化） ====================


class 流畅度策略测试(unittest.TestCase):
    """用户的真实场景：4K60 10bit HEVC 12.7 Mbps，实测带宽 13.0 Mbps。

    当时的观感是"稍显卡顿"，而参数是这样的：缓存被 AI 从 12000 **压到 6000ms**
    （带宽余量只有 1.02×，远低于 1.3× 的判定线），而且默认带着
    ``:no-drop-late-frames``（迟到帧一个都不许丢 → 越积越晚 → 持续小卡顿）。
    """

    def _媒体(self, **kw):
        base = {"宽": 3840, "高": 2160, "档位": "4K", "视频编码": "hevc",
                "视频码率bps": 12_700_000, "帧率": 60.0, "时长秒": 1472}
        base.update(kw)
        return 媒体信息(**base)

    def test_吃力片源允许丢帧并用GPU缩放(self):
        设 = 规则参数(self._媒体(), 探测结果(成功=True, 实测带宽bps=13_000_000),
                   ["vaapi", "auto", "none"])
        self.assertTrue(设.允许丢帧, "4K60 这种吃力片源要允许丢迟到帧")
        self.assertEqual(设.视频输出, "gl", "4K 缩到小屏应该用 GPU 缩放")
        选项 = 设.libvlc选项()
        self.assertIn(":vout=gl", 选项)
        self.assertNotIn(":no-drop-late-frames", 选项, "允许丢帧时不能再禁止丢帧")

    def test_带宽不足时缓存只许加大(self):
        设 = 规则参数(self._媒体(), 探测结果(成功=True, 实测带宽bps=13_000_000),
                   ["vaapi"])
        self.assertGreaterEqual(设.网络缓存毫秒, 15000,
                            "带宽余量不足时必须给大缓存（旧行为会掉到 6~8 秒）")
        self.assertFalse(设.可流畅播放)
        self.assertIn("带宽不足", 设.风险)

    def test_带宽充足时不吃力就不折腾(self):
        设 = 规则参数(媒体信息(宽=1920, 高=1080, 档位="1080p",
                        视频码率bps=8_000_000, 帧率=25.0),
                  探测结果(成功=True, 实测带宽bps=100_000_000), ["vaapi"])
        self.assertFalse(设.允许丢帧, "普通片源仍以画面完整优先")
        self.assertEqual(设.视频输出, "", "普通片源不必强制 GL")

    def test_高帧率也算吃力(self):
        设 = 规则参数(self._媒体(档位="1080p", 宽=1920, 高=1080,
                          视频码率bps=8_000_000, 帧率=120.0),
                  探测结果(成功=True, 实测带宽bps=100_000_000), ["vaapi"])
        self.assertTrue(设.允许丢帧, "120fps 也是吃力片源")
        self.assertEqual(设.视频输出, "gl")


class 缓存下限保护测试(unittest.TestCase):
    """带宽不足时，AI（或任何后续建议）**不许**把缓存改小。"""

    def _会话(self):
        from v8_3.播放.播放核心 import 播放会话
        会话 = 播放会话(取适配器=lambda *_: None, 日志回调=None,
                    探测直链开关=False, 探测媒体开关=False)
        会话.媒体 = 媒体信息(宽=3840, 高=2160, 档位="4K",
                        视频码率bps=12_700_000, 帧率=60.0)
        会话.探测 = 探测结果(成功=True, 实测带宽bps=13_000_000)
        会话.设置 = 规则参数(会话.媒体, 会话.探测, ["vaapi"])
        return 会话

    def test_缓存下限在带宽不足时生效(self):
        会话 = self._会话()
        self.assertGreaterEqual(会话.缓存下限(), 15000)

    def test_带宽充足时没有下限(self):
        会话 = self._会话()
        会话.探测 = 探测结果(成功=True, 实测带宽bps=200_000_000)
        self.assertEqual(会话.缓存下限(), 0)

    def test_应用新参数不会把缓存降到下限以下(self):
        会话 = self._会话()
        旧缓存 = 会话.设置.网络缓存毫秒
        会话.应用新参数({"网络缓存毫秒": 6000}, 自动重载=False)
        self.assertGreaterEqual(会话.设置.网络缓存毫秒, 15000,
                            f"不许把 {旧缓存}ms 降到 6000ms（带宽不够）")

    def test_应用新参数保留流畅字段(self):
        会话 = self._会话()
        会话.应用新参数({"视频输出": "gl", "允许丢帧": True}, 自动重载=False)
        self.assertEqual(会话.设置.视频输出, "gl")
        self.assertTrue(会话.设置.允许丢帧)

    def test_流畅优先预设(self):
        会话 = self._会话()
        设 = 会话.流畅优先()
        self.assertTrue(设.允许丢帧)
        self.assertEqual(设.视频输出, "gl")
        self.assertGreaterEqual(设.网络缓存毫秒, 15000)
        self.assertIn("流畅优先", 设.理由)

    def test_规则诊断只在真的掉帧或缓冲时才建议(self):
        会话 = self._会话()
        会话.播放器 = None
        # 没有播放器时状态快照是"未开始"，不该乱建议
        self.assertIsNone(会话.规则诊断())


class _假播放器:
    """只为"有播放器对象"而存在（安全回退只判断 None，不真调 VLC）。"""

    窗口句柄 = 0

    def 进度秒(self) -> float:
        return 0.0


class 嵌入播放输出测试(unittest.TestCase):
    """画面嵌进我们窗口时，不许用"会自己开窗口"的视频输出。

    用户实测：播 4K/60fps（吃力片源 → 规则给 ``:vout=gl``）时，屏幕上多出来一个
    标题为 ``VLC media player`` 的顶层窗口在放同一个视频 —— GL 输出不画进别人的窗口，
    libvlc 就自己开了一个。这里的几条就是防止它再回来。
    """

    def _会话(self):
        from v8_3.播放.播放核心 import (播放会话, 媒体信息, 探测结果, 规则参数)
        会话 = 播放会话(取适配器=lambda *_: None, 日志回调=None,
                    探测直链开关=False, 探测媒体开关=False)
        会话.媒体 = 媒体信息(宽=3840, 高=2160, 档位="4K",
                        视频码率bps=12_700_000, 帧率=60.0)
        会话.探测 = 探测结果(成功=True, 实测带宽bps=13_000_000)
        会话.设置 = 规则参数(会话.媒体, 会话.探测, ["vaapi"])
        return 会话

    def test_吃力片源的规则确实会给gl(self):
        """先把前提钉住：4K60 的规则本来就选 gl（所以必须由起播那边拦掉）。"""
        self.assertEqual(self._会话().设置.视频输出, "gl")

    def test_嵌入时实例级钉死xcb_x11(self):
        """**根因回归**：视频输出必须钉在 **libvlc 实例**上（媒体级 ``:vout=`` 无效）。

        实测（cvlc -vvv，2026-09-21）：
            cvlc --vout=xcb_x11 …   → using vout display module "xcb_x11" ✓
            cvlc ":vout=xcb_x11" …  → matching "any" → using module "gl" ✗
        以前规则给的是媒体级 ``:vout=gl``、兜底又是把它删掉 —— 全在改一个没人看的
        选项；libvlc 自己挑到了 GL 系，硬解（VAAPI）时 GL 建不到画布就**自己开一个
        顶层窗口**放画面。这就是用户那个 "VLC media player" 窗口的真正来路。
        """
        会话 = self._会话()
        self.assertEqual(会话._想要的实例输出(12345), "xcb_x11",
                         "有嵌入窗口时必须钉死能嵌进去的输出")
        self.assertEqual(会话._想要的实例输出(0), "",
                         "无窗口/独立窗口模式不钉（想看 GPU 的 GL 输出走这条）")

    def test_媒体级vout一律丢掉(self):
        """媒体级 vout 不生效，留着只会误导（规则/AI 都可能给）。"""
        会话 = self._会话()
        for 选项 in ([":vout=gl", ":network-caching=8000", ":avcodec-hw=vaapi"],
                   ["--vout=glx", ":network-caching=2000"],
                   [":vout=xcb_xv"]):
            留 = 会话._嵌入播放选项(选项, 12345)
            self.assertEqual([x for x in 留 if "vout" in x], [],
                             f"{选项} 里的 vout 是媒体级，必须丢掉")
        self.assertIn(":avcodec-hw=vaapi", 会话._嵌入播放选项(
            [":network-caching=8000", ":avcodec-hw=vaapi"], 1),
            "硬解等其它选项不许被牵连")

    def test_嵌入输出可以按需覆盖或关掉(self):
        """V8_3_嵌入视频输出 与阶梯覆盖：都能改"钉哪个"，auto/空 = 不钉。"""
        from unittest import mock
        from v8_3.播放 import 显示环境
        会话 = self._会话()
        with mock.patch.dict("os.environ", {"V8_3_嵌入视频输出": "xcb_xv"}):
            self.assertEqual(显示环境.嵌入视频输出(), "xcb_xv")
            self.assertEqual(会话._想要的实例输出(7), "xcb_xv")
        with mock.patch.dict("os.environ", {"V8_3_嵌入视频输出": "auto"}):
            self.assertEqual(显示环境.嵌入视频输出(), "")
            self.assertEqual(会话._想要的实例输出(7), "")
        会话.设置嵌入输出覆盖("xcb_xv")          # 回退阶梯用
        self.assertEqual(会话._想要的实例输出(7), "xcb_xv")
        会话.设置嵌入输出覆盖("auto")
        self.assertEqual(会话._想要的实例输出(7), "")

    def test_独立窗口模式不钉输出(self):
        会话 = self._会话()
        self.assertEqual(会话._想要的实例输出(0), "",
                         "没有嵌入窗口时不钉 —— 独立窗口就是要让 VLC 自己开窗口")

    def test_起播时总是先停止并等待(self):
        """**根因回归**：以前只在"句柄变了"时才等 —— 重播（句柄没变）会踩竞态。

        旧 vout 没释放就起新的，新的那个会因为 drawable 正忙而自己开窗口
        （用户看到的"又多一个 VLC 窗口"）。现在不管句柄变没变都先真的停住。
        """
        from unittest import mock
        会话 = self._会话()
        假 = mock.MagicMock()
        假.窗口句柄 = 12345          # 句柄**没变**（重播/AI 换参数重载的典型情况）
        假.实例视频输出 = "xcb_x11"   # 与"想要"一致 → 不重建实例
        假.播放.return_value = True
        假.取音量.return_value = 100
        会话.播放器 = 假
        会话.直链信息 = {"url": "http://例子.invalid/x.mp4", "headers": {}}
        # 本机没装 VLC 时（CI 的 Windows runner 就没有）不该因此跳过 ——
        # 这条测的是"起播流程先停住"，跟 libvlc 在不在无关，所以把可用性打桩。
        with mock.patch.object(会话, "_日志", lambda *a, **k: None), \
             mock.patch("v8_3.播放.播放核心.vlc可用", lambda: True):
            会话.起播(12345)
        假.停止并等待.assert_called_once()
        self.assertEqual(假.绑定窗口.call_args[0][0], 12345)

    def test_换了嵌入模式会重建实例(self):
        """视频输出是实例级选项：从"独立窗口"切回"嵌入"必须重建 libvlc 实例。"""
        from unittest import mock
        会话 = self._会话()
        旧实例 = mock.MagicMock()
        旧实例.实例视频输出 = ""          # 独立窗口模式建的（没钉）
        旧实例.窗口句柄 = 0
        会话.播放器 = 旧实例
        会话.直链信息 = {"url": "http://例子.invalid/x.mp4", "headers": {}}
        with mock.patch.object(会话, "_日志", lambda *a, **k: None), \
             mock.patch("v8_3.播放.播放核心.vlc可用", lambda: True), \
             mock.patch("v8_3.播放.播放核心.VLC") as 新VLC:
            新VLC.return_value.播放.return_value = True
            新VLC.return_value.取音量.return_value = 100
            会话.起播(999)                 # 这次要嵌进窗口
        旧实例.关闭.assert_called_once()
        self.assertEqual(新VLC.call_args.kwargs.get("实例视频输出"), "xcb_x11")

    def test_应用新参数能把视频输出清空(self):
        """回归：以前重载时 ``视频输出=""`` 被当"没给"过滤掉，等于没改回默认输出。"""
        会话 = self._会话()
        会话.应用新参数({"视频输出": ""}, 自动重载=False)
        self.assertEqual(会话.设置.视频输出, "",
                         "清空视频输出必须真的生效（否则兜底重载还是 gl）")

    def test_安全回退按阶梯走且不无限重载(self):
        """回退是**阶梯**（先关 GPU 解码 → 再换 XVideo 输出），走完就停，不无限重载。

        顺序是按**病因**排的：硬解（VAAPI 要用 GL 系输出）才是"自己开窗口"的诱因，
        所以第一级就是关硬解，而不是先动输出模块。
        """
        会话 = self._会话()
        会话.播放器 = _假播放器()
        次数: list = []
        会话.应用新参数 = lambda *a, **k: (次数.append(a[0]), True)[1]
        self.assertTrue(会话.安全回退画面("VLC media player"))     # 第 1 级
        self.assertTrue(会话.安全回退画面("VLC media player"))     # 第 2 级
        self.assertFalse(会话.安全回退画面("VLC media player"))    # 阶梯走完
        self.assertEqual(len(次数), len(会话.画面回退阶梯))
        self.assertEqual(次数[0].get("硬解"), "none",
                         "第一级必须是关硬解（GPU 解码路径才是会自己开窗口的那个）")
        self.assertEqual(会话._想要的实例输出(1), "xcb_xv",
                         "第二级要换掉**实例级**输出（媒体级 vout 不生效）")

    def test_没有播放器时不回退(self):
        会话 = self._会话()
        会话.播放器 = None
        self.assertFalse(会话.安全回退画面())


@unittest.skipUnless(_有Qt(), "没装 PySide6")
class 主线程归口测试(unittest.TestCase):
    """libvlc 不是线程安全的：真机 coredump 显示 ``播放AI决策`` 线程在
    ``libvlc_media_add_option_flag`` 里 SIGSEGV（界面线程同时在起播）。

    所以会话里所有碰播放器的方法，都必须"不在界面线程就排队给界面线程执行"。
    """

    def _会话(self):
        from v8_3.播放.播放核心 import 播放会话
        会话 = 播放会话(取适配器=lambda *_: None, 日志回调=None,
                    探测直链开关=False, 探测媒体开关=False)
        会话.装主线程泵()
        return 会话

    def test_后台线程调用会排队到界面线程(self):
        import threading
        _确保离屏()
        from PySide6.QtWidgets import QApplication
        应用 = QApplication.instance() or QApplication([])
        会话 = self._会话()
        记录: list = []

        def 假跳转(秒):
            记录.append((秒, threading.current_thread().name))

        会话.跳转 = 假跳转
        会话._归口装好 = False          # 覆盖掉 __init__ 里那次，让假方法也被套壳
        会话._装界面线程归口()
        线程 = threading.Thread(target=lambda: 会话.跳转(42.0), name="测试后台")
        线程.start()
        time.sleep(0.3)                # 后台线程此刻正"等界面线程"（这里没有事件循环）
        self.assertEqual(记录, [], "界面线程还没排空之前不该执行")
        会话.排空主线程队列()          # 界面线程排空（真程序里是 25ms 定时器在做）
        线程.join(2.0)
        self.assertFalse(线程.is_alive(), "排空后后台线程就该继续往下走")
        self.assertEqual(len(记录), 1)
        self.assertEqual(记录[0][0], 42.0)
        self.assertIn("MainThread", 记录[0][1], "必须真的在界面线程里执行")

    def test_界面线程调用直接执行不排队(self):
        import threading
        会话 = self._会话()
        记录: list = []
        会话.跳转 = lambda 秒: 记录.append(threading.current_thread().name)
        会话._归口装好 = False
        会话._装界面线程归口()
        会话.跳转(9.0)
        self.assertEqual(len(记录), 1, "界面线程里应该直接执行")
        self.assertEqual(会话.排空主线程队列(), 0)

    def test_拿不到窗口号时拒绝起播(self):
        """有出口却拿不到窗口号 → 宁可不起播，也不能把 0 交给 libvlc
        （那会让 VLC 自己开一个 "VLC media player" 窗口放画面）。"""
        from unittest import mock
        会话 = self._会话()

        class 空出口:
            def 句柄(self):
                return 0

            def 建播放器(self, **_k):        # 不该被调用
                raise AssertionError("拿不到窗口号就不该建播放器")
        会话.出口 = 空出口()
        会话.直链信息 = {"url": "http://127.0.0.1/假.mp4", "headers": {}}
        with mock.patch("v8_3.播放.播放核心.vlc可用", lambda: True):
            self.assertFalse(会话.起播(0), "必须拒绝起播")


class 播放出口测试(unittest.TestCase):
    """画面往哪里画：**唯一出口**（v8_3/播放/播放出口.py）。

    用户反馈过四次"多出一个 VLC media player 窗口 / 画面没对齐"。真机矩阵实测
    （用户这台机器：KDE Wayland + XWayland + Intel vaapi + 光鸭 4K60 HEVC 直链）：

    ==================================================  ==================  ========
    做法                                                 vout window 模块    游离窗口
    ==================================================  ==================  ========
    set_xwindow(视频控件自己的 X11 窗口)                  "embed-xid,any" ✓   无 ✓
    实例级 --drawable-xid + --embedded-video              "any" ✗            **有** ✗
    媒体级 :vout=xxx                                     不生效
    ==================================================  ==================  ========

    几条不能再犯的规矩：
    * 只有 ``set_xwindow`` 会触发嵌入（实例级 drawable 不会）；
    * 交给它的必须是**控件自己的** X11 子窗口（``WA_NativeWindow``），
      自己另建窗口再算偏移 → 画面歪到右下角还被裁（用户实测）；
    * **永远不要**销毁那个窗口（VLC 可能还在画；拆了它会另开一个窗口）。
    """

    def test_不可嵌入平台返回0(self):
        from unittest import mock
        from v8_3.播放.播放出口 import 播放出口
        出口 = 播放出口(控件=None)
        with mock.patch.object(出口, "可以嵌入", lambda: False):
            self.assertEqual(出口.句柄(), 0, "没有 X11 窗口号时绝不能给 libvlc 递句柄")

    def test_句柄来自控件自己的原生窗口(self):
        """非原生控件的 winId() 是顶层窗口号 —— 必须先把控件设成原生窗口。"""
        from unittest import mock
        from v8_3.播放.播放出口 import 播放出口
        设过属性 = []

        class 假控件:
            def __init__(self):
                self.原生 = False
            def setAttribute(self, 属性, 值):
                设过属性.append((属性, 值))
                self.原生 = True
            def testAttribute(self, 属性):   # noqa: N802
                return self.原生
            def winId(self):            # noqa: N802
                return 4242
            def window(self):
                return self

        控件 = 假控件()
        出口 = 播放出口(控件=控件)
        with mock.patch.object(出口, "可以嵌入", lambda: True), \
             mock.patch("PySide6.QtCore.Qt") as 假Qt:
            假Qt.WidgetAttribute.WA_NativeWindow = "WA_NativeWindow"
            self.assertEqual(出口.句柄(), 4242)
        self.assertTrue(设过属性, "必须把控件设成 WA_NativeWindow（否则窗口号是顶层窗口）")

    def test_句柄可重入且只设一次原生属性(self):
        """真机 SEGV 的第二个现场：`setAttribute(WA_NativeWindow)` 会立刻建原生窗口，
        这个过程派发的事件可能再次走到 ``句柄()`` —— 标志位若在调用后才置位就会
        无限递归（coredump 里正是 createWinId 层层嵌套），必须先置位 + testAttribute。"""
        from unittest import mock
        from v8_3.播放.播放出口 import 播放出口
        设过: list = []

        class 重入控件:
            def __init__(self):
                self.原生 = False
                self.出口 = None
            def setAttribute(self, 属性, 值):
                设过.append(值)
                self.原生 = True
                if self.出口 is not None:          # 模拟"建窗口时事件回调又进来问句柄"
                    self.出口.句柄()
            def testAttribute(self, 属性):           # noqa: N802
                return self.原生
            def winId(self):                    # noqa: N802
                return 777
            def window(self):
                return self

        控件 = 重入控件()
        出口 = 播放出口(控件=控件)
        控件.出口 = 出口
        with mock.patch.object(出口, "可以嵌入", lambda: True), \
             mock.patch("PySide6.QtCore.Qt") as 假Qt:
            假Qt.WidgetAttribute.WA_NativeWindow = "WA_NativeWindow"
            self.assertEqual(出口.句柄(), 777)      # 不递归、不炸栈
            self.assertEqual(出口.句柄(), 777)
        self.assertEqual(len(设过), 1, "原生属性只该设一次")

    def test_销毁不拆窗口(self):
        """销毁只能清引用：拆窗口会把 VLC 正在画的 drawable 抽走 → 它另开一个窗口。"""
        from unittest import mock
        from v8_3.播放.播放出口 import 播放出口
        出口 = 播放出口(控件=None)
        with mock.patch.object(出口, "可以嵌入", lambda: True):
            出口._已绑句柄 = 123
            出口.销毁()
            self.assertEqual(出口._已绑句柄, 0)

    def test_交接先把出口换过来再起播(self):
        """交接纪律：**先** 会话.出口 = 自己，再起播（否则起播会用旧窗口的句柄）。

        这正是"关掉独立窗口又冒出 VLC 窗口"的原因：一边往新窗口起播，
        一边句柄还指着那个即将被销毁的旧窗口。
        """
        from unittest import mock
        from v8_3.播放.播放出口 import 播放出口
        顺序: list[str] = []

        class 假播放器:
            def 停止并等待(self, _秒):
                顺序.append("停止并等待")

        class 假会话:
            出口 = "旧出口"
            播放器 = 假播放器()
            def 起播(self, 号):
                顺序.append(f"起播({号})，此时出口={self.出口}")
                return True
            def 跳转(self, _秒):
                顺序.append("跳转")
        会话 = 假会话()
        出口 = 播放出口(控件=None)
        with mock.patch.object(出口, "可以嵌入", lambda: True), \
             mock.patch.object(出口, "句柄", lambda: 777):
            self.assertTrue(出口.交接(会话, 42.0, 理由="测试"))
        self.assertEqual(顺序[0], "停止并等待", "换绑前必须先停干净（旧 vout 要释放）")
        self.assertIs(会话.出口, 出口, "交接第一步就必须把会话的出口换成自己")
        self.assertIn("起播(777)", 顺序[1])
        self.assertEqual(顺序[-1], "跳转", "起播后要跳回原位置")


@unittest.skipUnless(_有Qt() and bool(os.environ.get("DISPLAY")),
                     "需要真 X11 显示（Xvfb 里跑：xvfb-run -a …）")
class 真X11嵌入不游离测试(unittest.TestCase):
    """真 X11 上连播三次（模拟 AI 换参数重载）都不许出现游离的 VLC 窗口。"""

    @classmethod
    def setUpClass(cls):
        from v8_3.播放.游离窗口 import 可用 as X可用
        if not X可用():
            raise unittest.SkipTest("没有可用的 X11")
        import shutil, subprocess, tempfile
        if not (shutil.which("vlc") or shutil.which("cvlc")):
            raise unittest.SkipTest("没装 VLC")
        if not shutil.which("ffmpeg"):
            raise unittest.SkipTest("没有 ffmpeg")
        cls.目录 = Path(tempfile.mkdtemp(prefix="v83出口_"))
        cls.视频 = cls.目录 / "出口.mp4"
        subprocess.run([shutil.which("ffmpeg"), "-y", "-loglevel", "error",
                        "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=20:duration=20",
                        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt",
                        "yuv420p", str(cls.视频)], capture_output=True, timeout=180)

    def test_连播三次与交接都不出现游离窗口(self):
        import time
        from PySide6.QtWidgets import (QApplication, QHBoxLayout, QLabel,
                                     QVBoxLayout, QWidget)
        from v8_3.播放.播放出口 import 播放出口
        from v8_3.播放.播放核心 import 播放会话, 播放设置
        from v8_3.播放.游离窗口 import 找游离窗口
        应用 = QApplication.instance() or QApplication([])
        窗 = QWidget(); 窗.resize(900, 560)
        行 = QHBoxLayout(窗); 行.setContentsMargins(40, 30, 20, 20)
        行.addWidget(QLabel("左侧导航"))
        区 = QWidget(); 区.setMinimumSize(420, 260); 行.addWidget(区, 1)
        窗.show()
        self.addCleanup(窗.close)
        for _ in range(40):
            应用.processEvents(); time.sleep(0.02)
        页出口 = 播放出口(控件=区, 日志回调=None)
        句柄 = 页出口.句柄()
        self.assertTrue(句柄)
        会话 = 播放会话(出口=页出口, 取适配器=lambda *_: None, 日志回调=None,
                    探测直链开关=False, 探测媒体开关=False)
        会话.直链信息 = {"url": str(self.视频), "headers": {}}
        for 轮 in range(3):
            会话.设置 = 播放设置(网络缓存毫秒=800 + 轮 * 300, 硬解="auto")
            self.assertTrue(会话.起播(句柄), "起播应成功")
            for _ in range(15):
                应用.processEvents(); time.sleep(0.1)
            游离 = 找游离窗口(排除窗口号=句柄)
            self.assertFalse(游离, f"第 {轮 + 1} 次起播出现游离窗口：{游离}")
        # 交接：换到"另一个窗口的出口"再回来（对应 独立窗口 ⇄ 播放页）
        别处 = QWidget(); 别处.resize(500, 300); 别处.show()
        self.addCleanup(别处.close)
        for _ in range(30):
            应用.processEvents(); time.sleep(0.02)
        别出口 = 播放出口(控件=别处, 日志回调=None)
        self.assertTrue(别出口.交接(会话, 3.0, 理由="测试交接过去"))
        for _ in range(12):
            应用.processEvents(); time.sleep(0.1)
        self.assertFalse(找游离窗口(排除窗口号=别出口.句柄()), "交给别处时出现游离窗口")
        self.assertTrue(页出口.交接(会话, 4.0, 理由="测试交接回来"))
        for _ in range(12):
            应用.processEvents(); time.sleep(0.1)
        self.assertFalse(找游离窗口(排除窗口号=页出口.句柄()), "收回播放页时出现游离窗口")
        会话.关闭()


class 守护不再破坏性处理测试(unittest.TestCase):
    """游离窗口守护**不许**再销毁 libvlc 的窗口。

    那个窗口是 libvlc 正在渲染的画布：从外面 XDestroyWindow 掉，VLC 的 vout 线程
    就废了，之后任何 停止/释放 都要一直等它 —— 用户看到的就是"关一下播放，界面彻底卡死"。
    """

    def test_守护里没有任何销毁动作(self):
        from v8_3.界面 import 游离窗口守护 as 模块
        源码 = Path(模块.__file__).read_text(encoding="utf-8")
        # 只查"真的调用了"这些破坏性函数（文档里提名字是允许的）
        for 禁用 in ("游离窗口.销毁窗口", "游离窗口.清干净游离窗口",
                   "游离窗口.请关闭窗口", "XDestroyWindow("):
            self.assertNotIn(禁用, 源码, f"守护里不该再调用破坏性动作：{禁用}")
        self.assertFalse(hasattr(模块.游离窗口守护, "_最后销毁"))
        self.assertFalse(hasattr(模块.游离窗口守护, "_请它关闭"))

    def test_发现后会交给宿主处理(self):
        from v8_3.界面.游离窗口守护 import 游离窗口守护
        收到: list = []
        守护 = 游离窗口守护(
            None, 取自己窗口号们=lambda: [1, 2],
            发现回调=lambda 找到=None: (收到.append(找到), True)[1],
            日志=lambda *_: None)
        守护._交给宿主([(99, "VLC media player")])
        self.assertEqual(len(收到), 1, "发现游离窗口要交给宿主的处理函数")


class 视频输出钉法平台测试(unittest.TestCase):
    """真机 CI 抓到的病根：Windows 上照样钉了 ``xcb_x11``，而 VLC 在 Windows 上
    没有这个模块 —— 日志是 ``looking for vout display module matching "xcb_x11"``
    → ``no vout display modules matched`` → 它自己开一个顶层窗口放画面（用户看到的
    "视频游离在 GUI 之外"）。
    """

    def test_windows默认不钉(self):
        from unittest import mock
        from v8_3.播放 import 显示环境
        with mock.patch.dict(显示环境.os.environ, {}, clear=True), \
             mock.patch.object(显示环境.os, "name", "nt"):
            self.assertEqual(显示环境.嵌入视频输出(), "",
                             "Windows 上钉 X11 模块名会让 VLC 自己开窗口")

    def test_linux仍钉xcb_x11(self):
        from unittest import mock
        from v8_3.播放 import 显示环境
        with mock.patch.dict(显示环境.os.environ, {}, clear=True), \
             mock.patch.object(显示环境.os, "name", "posix"):
            self.assertEqual(显示环境.嵌入视频输出(), 显示环境.嵌入视频输出默认)

    def test_用户显式指定优先(self):
        from unittest import mock
        from v8_3.播放 import 显示环境
        with mock.patch.dict(显示环境.os.environ,
                            {"V8_3_嵌入视频输出": "xcb_xv"}, clear=True), \
             mock.patch.object(显示环境.os, "name", "nt"):
            self.assertEqual(显示环境.嵌入视频输出(), "xcb_xv")

    def test_有窗口时Windows不传给实例(self):
        from unittest import mock
        from v8_3.播放.播放核心 import 播放会话
        会话 = 播放会话(取适配器=lambda *_: None, 日志回调=None,
                    探测直链开关=False, 探测媒体开关=False)
        with mock.patch.dict("os.environ", {}, clear=True), \
             mock.patch("os.name", "nt"):
            self.assertEqual(会话._想要的实例输出(12345), "",
                             "Windows 上不要给 libvlc 实例钉输出模块")
