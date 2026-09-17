# v8_3/播放/语音识别.py
"""本地 Whisper 语音识别（V8_3 新增）——给**无字幕视频**自动生成字幕。

引擎用 `faster-whisper <https://github.com/SYSTRAN/faster-whisper>`_（CTranslate2
后端，纯 CPU int8 就能跑，不需要 torch、不需要显卡、不需要联网、不上传任何数据）。
模型与依赖全部落在**项目内**的 ``运行环境/语音识别/``，不碰系统包、不碰共享 venv。

目录布局（全部可删，删掉即彻底卸载）::

    网盘管理_V8_3/运行环境/语音识别/
    ├── venv/                       局部 Python 环境（faster-whisper 1.2.1）
    ├── 模型/faster-whisper-small/  CTranslate2 量化模型（约 461 MB）
    └── 测试音频/                    自检用中文样本

为什么用"子进程 + 常驻工作进程"
================================
主程序跑在**项目内**的主环境（``运行环境/venv``）里，那里**故意不装**
faster-whisper：它是重库（ctranslate2/onnxruntime/PyAV 加起来几百 MB），
和主程序的依赖互不相干，装在一起只会拖慢启动、还容易版本打架。所以本模块默认这样做：

* 主程序只做轻量的编排（路径解析、ffprobe 取时长、转发进度），
  **不 import** 任何重库；
* 真正跑模型的是项目内局部 venv 的解释器，以**常驻工作进程**方式拉起，
  通过 stdin/stdout 传 JSON 行；
* 工作进程活着 → 模型只加载一次（**单例缓存**），第二次识别不再付加载开销；
* :meth:`语音识别器.卸载` 直接结束工作进程，内存**彻底**还给系统。

如果本模块**本身**就跑在局部 venv 里（``faster_whisper`` 在当前解释器可直接
import），则自动切换成**进程内**模式，省掉一层 IPC。

媒体路径
========
``识别()`` 的 ``媒体路径`` 既可以是本地文件，也可以是 ``http(s)`` 直链：

* 本地文件 → 直接交给 PyAV 解码（不落临时文件）；
* ``http(s)`` 直链 → **先用 ffmpeg 抽音轨**成 16 kHz 单声道 wav 临时文件再识别，
  识别完立即删除（网络流用 PyAV 直接解容易断，抽一次更稳，还能提前拿到总时长）；
* 本地文件解码失败时 → 也会自动用 ffmpeg 抽一次音轨再重试。

``ffmpeg``/``ffprobe`` 缺失只会影响"直链"和"怪格式"，纯本地常见格式（mp4/mkv/
mp3/wav/flv/ts…）不受影响。

本模块不联网（除用户自己给 http 直链）；构造 :class:`语音识别器` 时**不做任何重活**。
"""
from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

__all__ = [
    "识别段落", "识别失败", "语音识别器", "转SRT", "规范语言",
    "环境目录", "环境Python", "独立解释器", "默认模型目录", "默认模型大小", "模型仓库",
    "候选模型大小", "支持语言", "标记",
]

# --------------------------------------------------------------------------
# 路径常量（纯计算，不碰磁盘、不 import 重库）
# --------------------------------------------------------------------------

#: 项目根 ``网盘管理_V8_3/``（本文件位于 ``v8_3/播放/`` 下）
项目根: Path = Path(__file__).resolve().parents[2]

from ..自举 import 环境解释器      # 解释器路径按平台算（Windows 在 Scripts/）

#: 局部环境根目录（**全部可删**，删掉即彻底卸载，见 docs/语音识别.md）
环境目录: Path = 项目根 / "运行环境" / "语音识别"

#: 局部环境的实际目录：Linux/macOS 是 venv，Windows 绿色版是自带的独立 Python
语音环境: Path = 环境目录 / ("python" if os.name == "nt" else "venv")

#: 局部环境里的解释器（装了 faster-whisper 的那个）
环境Python: Path = 环境解释器(语音环境)

#: 项目自带的独立 Python（连解释器和标准库都在项目里，不依赖系统 python、
#: 也不依赖任何别的项目）。Linux/macOS 的局部环境就是用**它**建的：
#: ``<项目根>/运行环境/python/bin/python3.14 -m venv 运行环境/语音识别/venv``
独立解释器: Path = 项目根 / "运行环境" / "python" / (
    "python.exe" if os.name == "nt" else "bin/python3.14")

#: 模型根目录
默认模型目录: Path = 环境目录 / "模型"

#: 默认模型：small 在中文上明显强于 base，体积仍只有 ~461 MB
默认模型大小: str = "small"

#: 允许的模型大小（faster-whisper 的 CT2 转换仓库名）
候选模型大小: tuple[str, ...] = ("tiny", "base", "small", "medium", "large-v3", "large-v2")

#: HuggingFace 仓库模板。**必须**配 ``HF_ENDPOINT=https://hf-mirror.com``
#: （huggingface.co 在目标机器上不通，hf-mirror.com 通）
模型仓库: dict[str, str] = {
    "tiny": "Systran/faster-whisper-tiny",
    "base": "Systran/faster-whisper-base",
    "small": "Systran/faster-whisper-small",
    "medium": "Systran/faster-whisper-medium",
    "large-v2": "Systran/faster-whisper-large-v2",
    "large-v3": "Systran/faster-whisper-large-v3",
}

#: 一个可用的 CT2 模型目录**必须**同时具备这些文件
必需模型文件: tuple[str, ...] = ("config.json", "model.bin", "tokenizer.json", "vocabulary.txt")

#: 子进程协议行前缀（避免和第三方库往 stdout 打的杂音混淆）
标记: str = "@@ASR@@"

#: 常用语言别名 → Whisper 语言码。``""``/``"自动"`` → 自动检测
支持语言: dict[str, str] = {
    "zh": "zh", "中文": "zh", "汉语": "zh", "普通话": "zh", "chs": "zh", "zh-cn": "zh",
    "en": "en", "英语": "en", "英文": "en", "eng": "en",
    "ja": "ja", "日语": "ja", "日文": "ja", "jp": "ja",
    "ko": "ko", "韩语": "ko", "朝鲜语": "ko",
    "yue": "yue", "粤语": "yue", "广东话": "yue",
    "fr": "fr", "法语": "fr", "de": "de", "德语": "de",
    "es": "es", "西班牙语": "es", "ru": "ru", "俄语": "ru",
    "pt": "pt", "葡萄牙语": "pt", "it": "it", "意大利语": "it",
    "ar": "ar", "阿拉伯语": "ar", "th": "th", "泰语": "th",
    "vi": "vi", "越南语": "vi", "id": "id", "印尼语": "id",
}

#: Whisper 支持的语言码（官方 99 种，这里另收了 ceb/haw 等几个常见补充），供上层做下拉框。
#: 用 ``dict.fromkeys`` 去重，保证 ``len()`` 就是真实条数。
Whisper语言全集: tuple[str, ...] = tuple(dict.fromkeys((
    "zh", "en", "yue", "ja", "ko", "fr", "de", "es", "ru", "pt", "it", "nl", "pl",
    "tr", "ar", "fa", "he", "hi", "bn", "ta", "te", "ml", "ur", "th", "vi", "id",
    "ms", "tl", "my", "km", "lo", "si", "ne", "mr", "gu", "kn", "pa", "or", "as",
    "sd", "ps", "ckb", "az", "kk", "ky", "uz", "tg", "tk", "mn", "bo", "ug", "ka",
    "hy", "be", "uk", "bg", "sr", "hr", "sl", "sk", "cs", "hu", "ro", "el", "sv",
    "da", "no", "fi", "et", "lv", "lt", "sq", "mk", "bs", "ca", "gl", "eu", "cy",
    "ga", "is", "mt", "lb", "af", "sw", "am", "so", "yo", "ha", "ig", "zu", "xh",
    "sn", "ny", "mg", "la", "br", "fo", "haw", "mi", "sm", "to", "tt", "ba", "su",
    "jv", "ceb", "oc", "ast", "yue",
)))


def 规范语言(语言: str) -> str:
    """把用户写的语言（``"中文"``/``"zh"``/``"ZH"``/``""``）规范成 Whisper 语言码。

    ``""``、``"自动"``、``"auto"`` 一律返回 ``""``，表示**让 Whisper 自动检测**。
    """
    if not 语言:
        return ""
    文本 = str(语言).strip().lower()
    if 文本 in ("", "自动", "auto", "detect", "自动检测"):
        return ""
    return 支持语言.get(文本, 文本)


@dataclass
class 识别段落(dict):
    """一段识别结果（一行字幕）。

    它**既是 dataclass，也是 dict**：``段.开始秒`` 和 ``段["开始秒"]`` 都能取到值。
    这么做不是为了好玩 —— V8_3 的下游（``v8_3/AI/字幕助手.py`` 的
    ``识别结果转条目``）是按"字段名当字典键"来归一化各家 ASR 输出的
    （``isinstance(段落, dict)`` + ``段落.get("开始秒")``），
    纯 dataclass 会被它整条丢掉。继承 ``dict`` 之后两边都满意，
    顺带还白拿一个好处：``json.dumps(段落)`` 直接可用。
    """

    开始秒: float
    结束秒: float
    文本: str

    def __post_init__(self) -> None:
        # 字段与字典键**始终同步**，避免出现"属性对、字典空"的半吊子状态
        self.开始秒 = float(self.开始秒)
        self.结束秒 = float(self.结束秒)
        self.文本 = str(self.文本)
        dict.__init__(self)
        for 键, 值 in (("开始秒", self.开始秒), ("结束秒", self.结束秒),
                      ("文本", self.文本)):
            dict.__setitem__(self, 键, 值)

    @property
    def 时长秒(self) -> float:
        return max(0.0, float(self.结束秒) - float(self.开始秒))

    def 转字典(self) -> dict:
        return {"开始秒": self.开始秒, "结束秒": self.结束秒, "文本": self.文本}


class 识别失败(RuntimeError):
    """识别过程中的可捕获异常——消息一定是可以直接读给人看的。"""


def _秒转SRT时间(秒: float) -> str:
    总毫秒 = max(0, int(round(float(秒) * 1000)))
    时, 总毫秒 = divmod(总毫秒, 3_600_000)
    分, 总毫秒 = divmod(总毫秒, 60_000)
    秒数, 毫秒 = divmod(总毫秒, 1000)
    return f"{时:02d}:{分:02d}:{秒数:02d},{毫秒:03d}"


def 转SRT(段落列表: list[识别段落]) -> str:
    """把识别结果拼成 SRT 字幕文本（给"无字幕视频生成字幕"直接用）。

    空白段落会被丢弃，并且**序号连续**（不会因为跳过了空段而出现 1、3、4）。
    """
    块: list[str] = []
    序号 = 0
    for 段 in 段落列表:
        文本 = (段.文本 or "").strip()
        if not 文本:
            continue
        序号 += 1
        块.append(str(序号))
        块.append(f"{_秒转SRT时间(段.开始秒)} --> {_秒转SRT时间(段.结束秒)}")
        块.append(文本)
        块.append("")
    return "\n".join(块)


# --------------------------------------------------------------------------
# 局部环境探测（纯文件检查，毫秒级，不 import 重库、不抛异常）
# --------------------------------------------------------------------------

def _局部环境站点包() -> Optional[Path]:
    """返回局部 venv 的 site-packages 目录（不存在则 None）。"""
    try:
        候选 = sorted((环境Python.parent.parent / "lib").glob("python*/site-packages"))
        return 候选[0] if 候选 else None
    except Exception:
        return None


def _环境依赖就绪() -> tuple[bool, str]:
    """检查局部 venv 里 faster-whisper / ctranslate2 是否都在。"""
    站点 = _局部环境站点包()
    if 站点 is None:
        return False, f"局部环境不存在：{环境Python.parent.parent}"
    缺 = [名 for 名 in ("faster_whisper", "ctranslate2", "tokenizers", "onnxruntime")
          if not (站点 / 名).exists()]
    if 缺:
        return False, "局部环境缺少依赖：" + "、".join(缺)
    return True, f"依赖齐全（{站点}）"


def _在进程内可用() -> bool:
    """当前解释器能不能直接 import faster_whisper（即：我们就跑在局部 venv 里）。"""
    if "faster_whisper" in sys.modules:
        return True
    try:
        import importlib.util
        return importlib.util.find_spec("faster_whisper") is not None
    except Exception:
        return False


def _修复步骤(缺什么: str) -> str:
    """给出一份"照着敲就能修好"的步骤——`可用()` 返回的说明里用它。"""
    return (
        f"{缺什么}\n"
        "修复步骤（在项目根目录下执行即可，全程在项目内，不需要 sudo，不碰系统包）：\n"
        f'  0) cd "{项目根}"\n'
        f'  1) 建局部 Python 环境（用项目自带的独立解释器，不碰系统 python）：\n'
        f'     "{独立解释器}" -m venv "{环境目录}/venv"\n'
        f'  2) 装推理依赖（约 110 MB，走阿里云 PyPI 镜像）：\n'
        f'     "{环境Python}" -m pip install -i https://mirrors.aliyun.com/pypi/simple/ faster-whisper==1.2.1\n'
        f'  3) 下模型（约 461 MB，**必须**走 hf-mirror 镜像，huggingface.co 不通）：\n'
        f'     HF_ENDPOINT=https://hf-mirror.com "{环境Python}" -m pip install huggingface_hub\n'
        f'     HF_ENDPOINT=https://hf-mirror.com "{环境目录}/venv/bin/hf" download '
        f'{模型仓库.get(默认模型大小, "Systran/faster-whisper-small")} '
        f'--local-dir "{默认模型目录}/faster-whisper-{默认模型大小}"\n'
        f'     （或直接 curl：curl -fL -o "<模型目录>/model.bin" '
        f'https://hf-mirror.com/{模型仓库.get(默认模型大小, "Systran/faster-whisper-small")}/resolve/main/model.bin）\n'
        f'  4) 重新自检：\n'
        f'     "{环境Python}" -c "import sys; sys.path.insert(0, r\'{项目根}\'); '
        f'from v8_3.播放.语音识别 import 语音识别器; print(语音识别器().可用())"\n'
        f"  细节见 docs/语音识别.md"
    )


# --------------------------------------------------------------------------
# 常驻工作进程（真正跑模型的那个）
# --------------------------------------------------------------------------

#: 工作进程源码。跑在**局部 venv** 里，从 stdin 收 JSON 行命令、往 stdout 回 JSON 行。
#: 放在字符串里是为了让整个引擎只有 ``语音识别.py`` 一个文件，便于审查与搬移。
_工作进程源码 = r'''
import json, os, subprocess, sys, tempfile, time

标记 = "@@ASR@@"
模型表 = {}


def 发(数据):
    sys.stdout.write(标记 + json.dumps(数据, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def 取时长(路径):
    try:
        结果 = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", 路径],
            capture_output=True, text=True, timeout=120)
        return float((结果.stdout or "").strip() or 0.0)
    except Exception:
        return 0.0


def 抽音轨(路径):
    """用 ffmpeg 把任意媒体/直链抽成 16kHz 单声道 wav，返回临时文件路径。"""
    句柄, 临时 = tempfile.mkstemp(suffix=".wav", prefix="v8_3_asr_")
    os.close(句柄)
    if not shutil_which("ffmpeg"):
        raise RuntimeError("系统里找不到 ffmpeg，无法对直链/怪格式抽音轨")
    结果 = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", 路径,
         "-vn", "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", "-f", "wav", 临时],
        capture_output=True, text=True)
    if 结果.returncode != 0 or not os.path.exists(临时) or os.path.getsize(临时) < 2048:
        try:
            os.unlink(临时)
        except Exception:
            pass
        raise RuntimeError("ffmpeg 抽音轨失败：" + (结果.stderr or "")[-500:])
    return 临时


def shutil_which(名字):
    from shutil import which
    return which(名字)


def 取模型(路径, 设备, 计算类型, 线程数):
    """单例：同一个 (路径, 设备, 计算类型, 线程数) 只加载一次。"""
    from faster_whisper import WhisperModel
    键 = (路径, 设备, 计算类型, int(线程数 or 0))
    模型 = 模型表.get(键)
    if 模型 is None:
        模型 = WhisperModel(路径, device=设备, compute_type=计算类型, cpu_threads=int(线程数 or 0))
        模型表[键] = 模型
    return 模型


def 处理识别(命令):
    媒体 = 命令["媒体"]
    语言 = 命令.get("语言") or None
    束搜索 = int(命令.get("束搜索", 5))
    VAD = bool(命令.get("VAD", True))
    临时 = None
    起点 = time.perf_counter()
    try:
        输入 = 媒体
        直链 = 媒体.startswith("http://") or 媒体.startswith("https://")
        if 直链:
            输入 = 抽音轨(媒体)
            临时 = 输入
        总秒 = float(命令.get("总秒") or 0.0) or 取时长(输入)
        加载起点 = time.perf_counter()
        模型 = 取模型(命令["模型路径"], 命令.get("设备", "cpu"),
                      命令.get("计算类型", "int8"), 命令.get("线程数", 0))
        加载耗时 = time.perf_counter() - 加载起点
        发({"类型": "已加载", "加载耗时": 加载耗时, "总秒": 总秒})

        def 起(路径):
            return 模型.transcribe(路径, language=语言, beam_size=束搜索, vad_filter=VAD)

        try:
            段, 信息 = 起(输入)
        except Exception:
            if 临时 is not None:
                raise
            临时 = 抽音轨(媒体)
            输入 = 临时
            总秒 = 取时长(输入)
            段, 信息 = 起(输入)

        段数 = 0
        for 段项 in 段:
            段数 += 1
            结束秒 = float(段项.end)
            发({"类型": "段落", "开始秒": round(float(段项.start), 3),
                "结束秒": round(结束秒, 3), "文本": (段项.text or "").strip()})

        发({"类型": "完成", "段数": 段数, "总秒": 总秒,
            "耗时": time.perf_counter() - 起点, "加载耗时": 加载耗时,
            "语言": getattr(信息, "language", None),
            "语言概率": getattr(信息, "language_probability", None),
            "音频时长": getattr(信息, "duration", None),
            "VAD后时长": getattr(信息, "duration_after_vad", None)})
    finally:
        if 临时:
            try:
                os.unlink(临时)
            except Exception:
                pass


def 处理信息(命令):
    from faster_whisper import WhisperModel
    已加载 = [{"路径": 键[0], "设备": 键[1], "计算类型": 键[2], "线程数": 键[3]}
              for 键 in 模型表]
    发({"类型": "信息结果", "已加载模型": 已加载, "进程内模型数": len(模型表)})


def 主循环():
    发({"类型": "工作进程就绪", "Python": sys.version.split()[0], "PID": os.getpid()})
    for 行 in sys.stdin:
        行 = 行.strip()
        if not 行:
            continue
        try:
            命令 = json.loads(行)
        except Exception as 错误:
            发({"类型": "错误", "消息": "无法解析命令 JSON：%s" % 错误})
            continue
        动作 = 命令.get("命令")
        try:
            if 动作 == "自检":
                import faster_whisper, ctranslate2, onnxruntime
                发({"类型": "自检结果", "可用": True,
                    "说明": "faster-whisper %s / ctranslate2 %s / onnxruntime %s"
                            % (faster_whisper.__version__, ctranslate2.__version__,
                               onnxruntime.__version__),
                    "faster_whisper": faster_whisper.__version__,
                    "ctranslate2": ctranslate2.__version__,
                    "onnxruntime": onnxruntime.__version__,
                    "CUDA设备数": ctranslate2.get_cuda_device_count()})
            elif 动作 == "信息":
                处理信息(命令)
            elif 动作 == "识别":
                处理识别(命令)
            elif 动作 == "卸载":
                模型表.clear()
                import gc
                gc.collect()
                发({"类型": "已卸载"})
            elif 动作 == "退出":
                发({"类型": "再见"})
                return
            else:
                发({"类型": "错误", "消息": "未知命令：%r" % (动作,)})
        except Exception as 错误:
            发({"类型": "错误", "消息": "%s: %s" % (type(错误).__name__, 错误),
                "类型名": type(错误).__name__})


主循环()
'''


class _工作进程:
    """常驻工作进程的薄封装：JSON 行进、JSON 行出，stdout/stderr 各一条读线程。"""

    def __init__(self, 解释器: Path, 环境: dict, 日志回调: Optional[Callable] = None):
        self.解释器 = str(解释器)
        self.环境 = dict(环境)
        self.日志回调 = 日志回调
        self.进程: Optional[subprocess.Popen] = None
        self._队列: "queue.Queue[Optional[str]]" = queue.Queue()
        self._错误行: list[str] = []
        self._启动()

    # -- 生命周期 ---------------------------------------------------------
    def _启动(self) -> None:
        环境 = dict(os.environ)
        环境.update(self.环境)
        环境.setdefault("PYTHONIOENCODING", "utf-8")
        环境.setdefault("PYTHONUNBUFFERED", "1")
        try:
            self.进程 = subprocess.Popen(
                [self.解释器, "-u", "-c", _工作进程源码],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace", env=环境,
                cwd=str(项目根), bufsize=1)
        except Exception as 错误:
            raise 识别失败(
                f"拉不起局部环境解释器：{self.解释器}（{错误}）\n" + _修复步骤("局部环境不可用。")
            ) from 错误
        threading.Thread(target=self._读标准输出, daemon=True).start()
        threading.Thread(target=self._读标准错误, daemon=True).start()
        就绪 = self._收一条(超时=60)
        if 就绪 is None or 就绪.get("类型") != "工作进程就绪":
            self.关闭()
            raise 识别失败(
                "局部环境里的工作进程启动失败。\n" + self.错误尾巴() + "\n"
                + _修复步骤("工作进程没能正常启动。"))
        self._日志(f"语音识别工作进程已就绪（Python {就绪.get('Python')}，PID {就绪.get('PID')}）")

    def _读标准输出(self) -> None:
        流 = self.进程.stdout if self.进程 else None
        if 流 is None:
            self._队列.put(None)
            return
        try:
            for 行 in 流:
                self._队列.put(行.rstrip("\n"))
        except Exception:
            pass
        finally:
            self._队列.put(None)

    def _读标准错误(self) -> None:
        流 = self.进程.stderr if self.进程 else None
        if 流 is None:
            return
        try:
            for 行 in 流:
                行 = 行.rstrip()
                if 行:
                    self._错误行.append(行)
                    del self._错误行[:-40]
        except Exception:
            pass

    def 错误尾巴(self, 行数: int = 12) -> str:
        if not self._错误行:
            return "（工作进程没有输出错误信息）"
        return "工作进程 stderr（末尾 %d 行）：\n" % min(行数, len(self._错误行)) + \
               "\n".join(self._错误行[-行数:])

    def _日志(self, 消息: str) -> None:
        if self.日志回调:
            try:
                self.日志回调(消息)
            except Exception:
                pass

    @property
    def 活着(self) -> bool:
        return self.进程 is not None and self.进程.poll() is None

    # -- 收发 -------------------------------------------------------------
    def 发送(self, 命令: dict) -> None:
        if not self.活着 or self.进程 is None or self.进程.stdin is None:
            raise 识别失败("语音识别工作进程已经不在了。\n" + self.错误尾巴())
        try:
            self.进程.stdin.write(json.dumps(命令, ensure_ascii=False) + "\n")
            self.进程.stdin.flush()
        except Exception as 错误:
            raise 识别失败(f"往工作进程写命令失败：{错误}\n" + self.错误尾巴()) from 错误

    def _收一条(self, 超时: Optional[float]) -> Optional[dict]:
        """收一条**协议**消息；非协议输出（第三方库的杂音）转日志后丢弃。"""
        截止 = None if 超时 is None else time.monotonic() + 超时
        while True:
            剩余 = None if 截止 is None else max(0.05, 截止 - time.monotonic())
            try:
                行 = self._队列.get(timeout=剩余)
            except queue.Empty:
                return None
            if 行 is None:
                return None
            if not 行.startswith(标记):
                if 行.strip():
                    self._日志("[工作进程] " + 行.strip())
                continue
            try:
                return json.loads(行[len(标记):])
            except Exception:
                continue

    def 收一条(self, 超时: Optional[float] = None) -> Optional[dict]:
        return self._收一条(超时)

    def 关闭(self) -> None:
        进程 = self.进程
        self.进程 = None
        if 进程 is None:
            return
        try:
            if 进程.poll() is None and 进程.stdin:
                进程.stdin.write(json.dumps({"命令": "退出"}) + "\n")
                进程.stdin.flush()
        except Exception:
            pass
        try:
            进程.wait(timeout=3)
        except Exception:
            try:
                进程.kill()
            except Exception:
                pass
        for 流 in (进程.stdin, 进程.stdout, 进程.stderr):
            try:
                if 流:
                    流.close()
            except Exception:
                pass


# 模块级单例：同一进程内，同一套配置只保留一个工作进程（→ 模型只加载一次）
_工作进程缓存: dict[tuple, _工作进程] = {}
_工作进程锁 = threading.RLock()

# 模块级单例：进程内模式下的模型缓存
_进程内模型缓存: dict[tuple, Any] = {}


def _取工作进程(设备: str, 计算类型: str, 日志回调=None) -> _工作进程:
    键 = (str(环境Python), 设备, 计算类型)
    with _工作进程锁:
        进程 = _工作进程缓存.get(键)
        if 进程 is not None and 进程.活着:
            return 进程
        环境 = {
            # huggingface.co 不通，强制走镜像；缓存也锁在项目内
            "HF_ENDPOINT": os.environ.get("HF_ENDPOINT", "https://hf-mirror.com"),
            "HF_HOME": os.environ.get("HF_HOME", str(环境目录 / "缓存")),
            "HF_HUB_DISABLE_TELEMETRY": "1",
        }
        进程 = _工作进程(环境Python, 环境, 日志回调=日志回调)
        _工作进程缓存[键] = 进程
        return 进程


def _关闭全部工作进程() -> None:
    with _工作进程锁:
        for 进程 in list(_工作进程缓存.values()):
            try:
                进程.关闭()
            except Exception:
                pass
        _工作进程缓存.clear()


def _取进程内模型(路径: str, 设备: str, 计算类型: str, 线程数: int):
    from faster_whisper import WhisperModel  # 只有本进程跑在局部 venv 里时才会走到这
    键 = (路径, 设备, 计算类型, int(线程数 or 0))
    with _工作进程锁:
        模型 = _进程内模型缓存.get(键)
        if 模型 is None:
            模型 = WhisperModel(路径, device=设备, compute_type=计算类型,
                                cpu_threads=int(线程数 or 0))
            _进程内模型缓存[键] = 模型
        return 模型


# --------------------------------------------------------------------------
# 媒体工具（主进程侧用：取时长；抽音轨只在进程内模式 + 直链时用）
# --------------------------------------------------------------------------

def _取时长(路径: str) -> float:
    if not shutil.which("ffprobe"):
        return 0.0
    try:
        结果 = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", 路径],
            capture_output=True, text=True, timeout=120)
        return float((结果.stdout or "").strip() or 0.0)
    except Exception:
        return 0.0


def _抽音轨(路径: str) -> str:
    import tempfile
    if not shutil.which("ffmpeg"):
        raise 识别失败("系统里找不到 ffmpeg，无法对 http 直链抽音轨。请先装 ffmpeg。")
    句柄, 临时 = tempfile.mkstemp(suffix=".wav", prefix="v8_3_asr_")
    os.close(句柄)
    结果 = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", 路径,
         "-vn", "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", "-f", "wav", 临时],
        capture_output=True, text=True)
    if 结果.returncode != 0 or not os.path.exists(临时) or os.path.getsize(临时) < 2048:
        try:
            os.unlink(临时)
        except Exception:
            pass
        raise 识别失败("ffmpeg 抽音轨失败：" + (结果.stderr or "")[-500:])
    return 临时


def _是直链(路径: str) -> bool:
    文本 = (路径 or "").strip().lower()
    return 文本.startswith("http://") or 文本.startswith("https://")


# --------------------------------------------------------------------------
# 对外主类
# --------------------------------------------------------------------------

class 语音识别器:
    """本地 Whisper 语音识别器（延迟加载 + 模型单例）。

    参数
    ====
    模型大小    ``"tiny"/"base"/"small"/"medium"/"large-v3"``，默认 ``"small"``
    模型目录    默认 ``<项目根>/运行环境/语音识别/模型``。会在里面找
                ``faster-whisper-<模型大小>/``；若该目录本身就是一个模型
                （含 ``model.bin``）也接受
    日志回调    ``回调(消息: str)``，用于把"拉工作进程/加载模型"等过程报给界面
    设备        ``"cpu"``（默认）或 ``"cuda"``；本机无 N 卡，用默认即可
    计算类型    ``"int8"``（CPU 默认，最快最省内存）/``"int8_float32"``/``"float32"``

    用法::

        from v8_3.播放.语音识别 import 语音识别器
        识别器 = 语音识别器()                       # 构造极快，不加载模型
        print(识别器.可用())                        # (True/False, 说明)
        段落 = 识别器.识别("电影.mkv", 语言="中文",
                           进度回调=lambda 百分比, 已处理秒: print(百分比))
        识别器.卸载()                               # 结束工作进程，内存还给系统
    """

    def __init__(self, 模型大小: str = 默认模型大小, 模型目录: str = "", 日志回调=None,
                 设备: str = "cpu", 计算类型: str = "int8",
                 线程数: int = 0, 束搜索: int = 5, VAD: bool = True):
        # —— 构造时只记参数，绝不做重活（不建进程、不加载模型、不查网络）——
        self.模型大小 = str(模型大小 or 默认模型大小).strip() or 默认模型大小
        self.模型目录 = str(模型目录 or 默认模型目录)
        self.日志回调 = 日志回调
        self.设备 = str(设备 or "cpu").strip() or "cpu"
        self.计算类型 = str(计算类型 or "int8").strip() or "int8"
        self.线程数 = int(线程数 or 0)
        self.束搜索 = int(束搜索 or 5)
        self.VAD = bool(VAD)
        self.最近识别信息: dict = {}
        self._模型路径缓存: Optional[str] = None
        self._路径已解析 = False
        self._调用锁 = threading.RLock()

    # -- 内部：路径与自检 -------------------------------------------------
    def _日志(self, 消息: str) -> None:
        if self.日志回调:
            try:
                self.日志回调(消息)
            except Exception:
                pass

    def _候选模型路径(self) -> list[Path]:
        基 = Path(self.模型目录).expanduser()
        候选: list[Path] = []
        if not self.模型目录:
            return 候选
        if 基.is_file() and 基.name == "model.bin":
            候选.append(基.parent)
        else:
            候选.append(基 / f"faster-whisper-{self.模型大小}")
            候选.append(基 / self.模型大小)
            候选.append(基)
        # 模型大小 直接写成绝对路径的情况
        if os.sep in self.模型大小:
            直接 = Path(self.模型大小).expanduser()
            if 直接.is_dir():
                候选.insert(0, 直接)
        去重: list[Path] = []
        for 项 in 候选:
            if 项 not in 去重:
                去重.append(项)
        return 去重

    @staticmethod
    def _模型是否完整(目录: Path) -> tuple[bool, list[str]]:
        if not 目录.is_dir():
            return False, list(必需模型文件)
        缺 = [名 for 名 in 必需模型文件 if not (目录 / 名).is_file()]
        return (not 缺), 缺

    def _模型路径(self) -> Optional[str]:
        """解析出真正可用的模型目录；解析不到返回 ``None``（不抛异常）。"""
        if self._路径已解析:
            return self._模型路径缓存
        self._路径已解析 = True
        self._模型路径缓存 = None
        for 候选 in self._候选模型路径():
            完整, _缺 = self._模型是否完整(候选)
            if 完整:
                self._模型路径缓存 = str(候选)
                break
        return self._模型路径缓存

    def _模型体积字节(self) -> int:
        路径 = self._模型路径()
        根 = Path(路径) if 路径 else None
        if 根 is None:
            for 候选 in self._候选模型路径():
                if 候选.is_dir():
                    根 = 候选
                    break
        if 根 is None or not 根.is_dir():
            return 0
        合计 = 0
        for 名 in 必需模型文件:
            文件 = 根 / 名
            try:
                if 文件.is_file():
                    合计 += 文件.stat().st_size
            except Exception:
                pass
        return 合计

    def _运行方式(self) -> str:
        return "进程内" if _在进程内可用() else "子进程"

    def 可用(self, 深度检查: bool = False) -> tuple[bool, str]:
        """``(是否可用, 说明)``——**任何情况下都不抛异常**。

        默认只做毫秒级的文件级检查（适合界面轮询）；``深度检查=True`` 会真的
        在局部环境里 import 一次 faster-whisper 并报出真实版本号（约 1~2 秒）。
        不可用时，说明里一定带**可以照着敲的修复步骤**。
        """
        try:
            路径 = self._模型路径()
            if 路径 is None:
                期望 = self._候选模型路径()
                期望文本 = str(期望[0]) if 期望 else str(默认模型目录)
                return False, _修复步骤(
                    f"没找到完整可用的 Whisper 模型（期望目录：{期望文本}，"
                    f"需要 {'、'.join(必需模型文件)}）。")
            体积 = self._模型体积字节()
            if 体积 < 1_000_000:
                return False, _修复步骤(
                    f"模型目录 {路径} 里的 model.bin 只有 {体积} 字节，明显没下完。")
            if not _在进程内可用():
                if not 环境Python.is_file():
                    return False, _修复步骤(f"局部环境解释器不存在：{环境Python}")
                if not os.access(环境Python, os.X_OK):
                    return False, _修复步骤(f"局部环境解释器不可执行：{环境Python}（可能被挂载成 noexec）")
                依赖就绪, 依赖说明 = _环境依赖就绪()
                if not 依赖就绪:
                    return False, _修复步骤(f"{依赖说明}。")
            if 深度检查:
                if _在进程内可用():
                    import faster_whisper
                    import ctranslate2
                    说明 = (f"就绪（进程内）：faster-whisper {faster_whisper.__version__} / "
                            f"ctranslate2 {ctranslate2.__version__}")
                else:
                    进程 = _取工作进程(self.设备, self.计算类型, self.日志回调)
                    进程.发送({"命令": "自检"})
                    结果 = 进程.收一条(超时=120)
                    if not 结果 or 结果.get("类型") != "自检结果":
                        尾巴 = (结果 or {}).get("消息") or 进程.错误尾巴()
                        return False, _修复步骤(f"局部环境自检失败：{尾巴}")
                    说明 = f"就绪（子进程）：{结果.get('说明')}"
            else:
                说明 = f"就绪（{self._运行方式()}，未做深度检查）"
            return True, (
                f"{说明}；模型 {self.模型大小}（{体积 / 1048576:.1f} MB）在 {路径}；"
                f"设备 {self.设备} / 计算类型 {self.计算类型}"
            )
        except Exception as 错误:  # 可用() 绝不抛
            try:
                return False, _修复步骤(f"自检时出现意外错误：{type(错误).__name__}: {错误}")
            except Exception:
                return False, "自检失败，且拼接修复步骤时又出错；请手动检查 运行环境/语音识别/。"

    def 模型信息(self) -> dict:
        """模型与环境的静态信息（不加载模型、不建进程）。"""
        路径 = self._模型路径()
        字节 = self._模型体积字节()
        候选 = self._候选模型路径()
        期望 = str(候选[0]) if 候选 else str(默认模型目录)
        信息 = {
            # —— 约定字段 ——
            "模型": self.模型大小,
            "目录": 路径 or 期望,
            "大小MB": round(字节 / 1048576, 1),
            "设备": self.设备,
            "计算类型": self.计算类型,
            "已就绪": 路径 is not None and 字节 >= 1_000_000,
            # —— 附加信息（方便界面/日志展示）——
            "期望目录": 期望,
            "缺少文件": [] if 路径 else self._模型是否完整(Path(期望))[1],
            "运行方式": self._运行方式(),
            "环境目录": str(环境目录),
            "环境Python": str(环境Python),
            "环境Python存在": 环境Python.is_file(),
            "当前解释器": sys.executable,
            "线程数": self.线程数,
            "束搜索": self.束搜索,
            "VAD": self.VAD,
            "模型已加载": self.模型是否已加载(),
            "支持语言数": len(Whisper语言全集),
            "仓库": 模型仓库.get(self.模型大小, ""),
        }
        return 信息

    def 模型是否已加载(self) -> bool:
        """模型当前是否已经在内存里（进程内缓存或工作进程里有）。不抛异常。"""
        try:
            if _在进程内可用():
                路径 = self._模型路径()
                return any(键[0] == 路径 for 键 in _进程内模型缓存)
            进程 = _工作进程缓存.get((str(环境Python), self.设备, self.计算类型))
            if 进程 is None or not 进程.活着:
                return False
            进程.发送({"命令": "信息"})
            结果 = 进程.收一条(超时=30)
            return bool(结果 and 结果.get("进程内模型数"))
        except Exception:
            return False

    # -- 识别 -------------------------------------------------------------
    def 识别(self, 媒体路径: str, 语言: str = "", 进度回调=None) -> list[识别段落]:
        """识别一段媒体，返回 :class:`识别段落` 列表。

        参数
        ====
        媒体路径    本地文件路径，或 ``http(s)`` 直链
        语言        ``"中文"``/``"zh"``/``""``（空=自动检测）
        进度回调    ``回调(百分比: float, 已处理秒: float)``

        异常
        ====
        失败时抛 :class:`识别失败`（``RuntimeError`` 子类），消息可直接展示给用户。
        """
        媒体 = str(媒体路径 or "").strip()
        if not 媒体:
            raise 识别失败("媒体路径是空的。")
        语言码 = 规范语言(语言)

        with self._调用锁:
            可用, 说明 = self.可用()
            if not 可用:
                raise 识别失败("语音识别当前不可用。\n" + 说明)

            if _在进程内可用():
                return self._进程内识别(媒体, 语言码, 进度回调)
            return self._子进程识别(媒体, 语言码, 进度回调)

    # -- 识别：子进程模式（默认，主程序跑在共享 venv 时走这条）------------
    def _子进程识别(self, 媒体: str, 语言码: str, 进度回调) -> list[识别段落]:
        路径 = self._模型路径()
        self._日志("正在准备语音识别工作进程…")
        进程 = _取工作进程(self.设备, self.计算类型, self.日志回调)
        总秒 = _取时长(媒体) if shutil.which("ffprobe") else 0.0
        起点 = time.perf_counter()
        进程.发送({
            "命令": "识别", "媒体": 媒体, "语言": 语言码,
            "模型路径": 路径, "设备": self.设备, "计算类型": self.计算类型,
            "线程数": self.线程数, "束搜索": self.束搜索, "VAD": self.VAD, "总秒": 总秒,
        })
        段落: list[识别段落] = []
        加载已报 = False
        while True:
            消息 = 进程.收一条(超时=None)
            if 消息 is None:
                raise 识别失败(
                    "语音识别工作进程中途退出了。\n" + 进程.错误尾巴() + "\n"
                    f"（媒体：{媒体}）")
            类型 = 消息.get("类型")
            if 类型 == "段落":
                段 = 识别段落(float(消息.get("开始秒", 0.0)),
                               float(消息.get("结束秒", 0.0)),
                               str(消息.get("文本", "")))
                段落.append(段)
                self._报进度(进度回调, 段.结束秒, 消息.get("百分比"), 总秒)
            elif 类型 == "进度":
                self._报进度(进度回调, float(消息.get("已处理秒", 0.0)),
                            消息.get("百分比"), 总秒)
            elif 类型 == "已加载":
                if not 加载已报:
                    加载已报 = True
                    self._日志(f"模型已就绪（加载 {消息.get('加载耗时', 0.0):.2f}s），开始识别…")
            elif 类型 == "完成":
                self.最近识别信息 = dict(消息)
                self.最近识别信息["总耗时"] = time.perf_counter() - 起点
                self.最近识别信息["模式"] = "子进程"
                self._日志(f"识别完成：{消息.get('段数')} 段，"
                           f"耗时 {self.最近识别信息['总耗时']:.2f}s")
                self._报进度(进度回调, float(消息.get("总秒") or 总秒), 100.0, 总秒, 最终=True)
                return 段落
            elif 类型 == "错误":
                raise 识别失败("识别失败：" + str(消息.get("消息")) + "\n" + 进程.错误尾巴())
            else:
                continue

    # -- 识别：进程内模式（本模块自己就跑在局部 venv 里时走这条）----------
    def _进程内识别(self, 媒体: str, 语言码: str, 进度回调) -> list[识别段落]:
        路径 = self._模型路径()
        临时: Optional[str] = None
        起点 = time.perf_counter()
        加载起点 = time.perf_counter()
        try:
            输入 = 媒体
            if _是直链(媒体):
                输入 = _抽音轨(媒体)
                临时 = 输入
            总秒 = _取时长(输入)
            模型 = _取进程内模型(路径, self.设备, self.计算类型, self.线程数)
            加载耗时 = time.perf_counter() - 加载起点
            try:
                段迭代器, 信息 = 模型.transcribe(输入, language=语言码 or None,
                                                 beam_size=self.束搜索, vad_filter=self.VAD)
            except Exception:
                if 临时 is not None:
                    raise
                临时 = _抽音轨(媒体)
                输入 = 临时
                总秒 = _取时长(输入)
                段迭代器, 信息 = 模型.transcribe(输入, language=语言码 or None,
                                                 beam_size=self.束搜索, vad_filter=self.VAD)
            段落: list[识别段落] = []
            for 段项 in 段迭代器:
                段 = 识别段落(float(段项.start), float(段项.end), (段项.text or "").strip())
                段落.append(段)
                百分比 = (段.结束秒 / 总秒 * 100.0) if 总秒 > 0 else 0.0
                self._报进度(进度回调, 段.结束秒, 百分比, 总秒)
            self.最近识别信息 = {
                "段数": len(段落), "总秒": 总秒, "耗时": time.perf_counter() - 起点,
                "加载耗时": 加载耗时, "语言": getattr(信息, "language", None),
                "语言概率": getattr(信息, "language_probability", None),
                "音频时长": getattr(信息, "duration", None),
                "VAD后时长": getattr(信息, "duration_after_vad", None),
                "总耗时": time.perf_counter() - 起点, "模式": "进程内",
            }
            self._报进度(进度回调, 总秒, 100.0, 总秒, 最终=True)
            return 段落
        except 识别失败:
            raise
        except Exception as 错误:
            raise 识别失败(
                f"识别失败（{type(错误).__name__}）：{错误}\n媒体：{媒体}") from 错误
        finally:
            if 临时:
                try:
                    os.unlink(临时)
                except Exception:
                    pass

    @staticmethod
    def _报进度(进度回调, 已处理秒: float, 百分比, 总秒: float, 最终: bool = False) -> None:
        if not 进度回调:
            return
        try:
            值 = float(百分比) if 百分比 is not None else (
                (已处理秒 / 总秒 * 100.0) if 总秒 else 0.0)
        except Exception:
            值 = 0.0
        值 = max(0.0, min(100.0, 值))
        if not 最终:
            值 = min(值, 99.5)   # 中间进度不抢先报 100，100 只由"完成"给
        try:
            进度回调(值, float(已处理秒))
        except Exception:
            pass  # 回调自己的锅，不能拖垮识别

    # -- 收尾 -------------------------------------------------------------
    def 卸载(self) -> None:
        """卸载模型：结束工作进程 + 清空进程内缓存，**内存彻底还给系统**。

        不抛异常。下次 :meth:`识别` 会自动重新拉起（模型会重新加载一次）。
        """
        键 = (str(环境Python), self.设备, self.计算类型)
        try:
            with _工作进程锁:
                进程 = _工作进程缓存.pop(键, None)
            if 进程 is not None:
                进程.关闭()
        except Exception:
            pass
        try:
            with _工作进程锁:
                _进程内模型缓存.clear()
        except Exception:
            pass
        try:
            import gc
            gc.collect()
        except Exception:
            pass
        self.最近识别信息 = {}
        self._日志("语音识别模型已卸载。")


def 卸载全部() -> None:
    """把本进程拉起过的所有工作进程/模型都清掉（进程退出前调一次即可）。"""
    _关闭全部工作进程()
    try:
        _进程内模型缓存.clear()
    except Exception:
        pass
