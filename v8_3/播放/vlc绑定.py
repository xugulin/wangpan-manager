# v8_3/播放/vlc绑定.py
"""libvlc 的 ctypes 绑定（V8_3 自带，不依赖 python-vlc，也不装任何系统包）。

为什么自己写绑定
================
* 参考项目 VLC 的官方 Python 绑定（``vlc.py``）需要额外下载一个模块，而且它把
  几百个 API 全塞在一起；我们要的只是"把网盘直链视频播起来"这一小撮能力；
* 自己绑定后**只用系统已有的 libvlc.so.5**（本机 VLC 3.0.23 已带），V8_3 不引入
  新的系统依赖，也不用 pip 装东西 —— 满足"封装在项目内、不污染系统环境"。

线程与生命周期注意
==================
* ``libvlc_new`` 必须在 **Qt 主线程**调用（libvlc 内部会检查线程），
  所有播放控制也从主线程发起（本项目里由 Qt 定时器/按钮驱动）；
* 对象释放顺序：media_player → media → instance，全部用 ``释放()`` 收口，
  避免 VLC 在进程退出时抱怨。

只暴露播放需要的部分，不做通用封装。
"""
from __future__ import annotations

import ctypes
import ctypes.util
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

__all__ = [
    "VLC库", "库不可用", "播放状态", "媒体统计", "音视频轨", "VLC",
    "可用", "不可用原因",
]


class 库不可用(RuntimeError):
    """libvlc 找不到或初始化失败。"""


class 播放状态:
    """与 libvlc_state_t 对齐（只用得到这几个）。"""

    无 = 0
    打开中 = 1
    缓冲中 = 2
    播放中 = 3
    暂停 = 4
    已停止 = 5
    已结束 = 6
    出错 = 7

    中文 = {0: "空闲", 1: "打开中", 2: "缓冲中", 3: "播放中", 4: "已暂停",
          5: "已停止", 6: "已结束", 7: "出错"}

    @classmethod
    def 名称(cls, 值: int) -> str:
        return cls.中文.get(int(值 or 0), f"未知({值})")


@dataclass
class 媒体统计:
    """``libvlc_media_stats_t`` 的可读映射（VLC 3.x 字段布局）。"""

    读字节: int = 0
    输入码率bps: float = 0.0
    解复用字节: int = 0
    解复码率bps: float = 0.0
    解复损坏: int = 0
    解复中断: int = 0
    已解码视频: int = 0
    已解码音频: int = 0
    已显示帧: int = 0
    丢帧: int = 0
    已播音频缓冲: int = 0
    丢音频缓冲: int = 0
    发送包: int = 0
    发送字节: int = 0
    发送码率bps: float = 0.0
    采样时间: float = 0.0

    def to_dict(self) -> dict:
        return {
            "读字节": self.读字节, "输入码率bps": round(self.输入码率bps, 1),
            "解复码率bps": round(self.解复码率bps, 1),
            "解复损坏": self.解复损坏, "解复中断": self.解复中断,
            "已解码视频": self.已解码视频, "已解码音频": self.已解码音频,
            "已显示帧": self.已显示帧, "丢帧": self.丢帧,
            "丢音频缓冲": self.丢音频缓冲, "采样时间": self.采样时间,
        }


@dataclass
class 音视频轨:
    编号: int
    名称: str


class _轨道描述(ctypes.Structure):
    pass


_轨道描述._fields_ = [
    ("i_id", ctypes.c_int),
    ("psz_name", ctypes.c_char_p),
    ("p_next", ctypes.POINTER(_轨道描述)),
]


class _媒体统计结构(ctypes.Structure):
    """与 vlc/libvlc_media.h 的 libvlc_media_stats_t 一一对应。"""

    _fields_ = [
        ("i_read_bytes", ctypes.c_int),
        ("f_input_bitrate", ctypes.c_float),
        ("i_demux_read_bytes", ctypes.c_int),
        ("f_demux_bitrate", ctypes.c_float),
        ("i_demux_corrupted", ctypes.c_int),
        ("i_demux_discontinuity", ctypes.c_int),
        ("i_decoded_video", ctypes.c_int),
        ("i_decoded_audio", ctypes.c_int),
        ("i_displayed_pictures", ctypes.c_int),
        ("i_lost_pictures", ctypes.c_int),
        ("i_played_abuffers", ctypes.c_int),
        ("i_lost_abuffers", ctypes.c_int),
        ("i_sent_packets", ctypes.c_int),
        ("i_sent_bytes", ctypes.c_int),
        ("f_send_bitrate", ctypes.c_float),
    ]


#: 包内自带 VLC 的位置（发布包里就有，用户不必自己装）
#: Windows 版**必须内置**：那边不像桌面 Linux 自带 VLC，用户报过
#: "libvlc 不可用：找不到 libvlc（VLC 的运行库）" —— 播放直接不能用。
自带VLC目录名 = "vlc"
自带VLC候选 = (
    Path("运行环境") / 自带VLC目录名,       # 发布包里的位置
    Path(自带VLC目录名),                   # 源码树里手放一份也能用
)


def _项目根() -> Path:
    """项目根（发布包里就是解压出来的那个目录）。"""
    return Path(__file__).resolve().parents[2]


def 自带库目录() -> Optional[Path]:
    """包内自带的 VLC 目录（没有就返回 None）。"""
    for 相对 in 自带VLC候选:
        目录 = _项目根() / 相对
        名字 = "libvlc.dll" if os.name == "nt" else "libvlc.so.5"
        if (目录 / 名字).is_file():
            return 目录
    return None


def _用自带库(目录: Path) -> str:
    """把包内自带的 libvlc 挂上：加 DLL 搜索目录 + 告诉 VLC 插件在哪。

    两个坑（Windows 上必须都做）：
    * ``libvlc.dll`` 还要找 ``libvlccore.dll`` —— Windows 的 DLL 搜索**不含**
      被加载 DLL 自己的目录，所以先把该目录加进搜索路径（Python 3.8+ 用
      ``os.add_dll_directory``，老办法 PATH 前置也一起做上，双保险）；
    * VLC 起来之后要按 ``VLC_PLUGIN_PATH`` 找解码/HTTP/输出等插件，
      不设就是"能加载、不能播"。
    """
    目录 = Path(目录)
    插件 = 目录 / "plugins"
    try:
        if os.name == "nt" and hasattr(os, "add_dll_directory"):
            os.add_dll_directory(str(目录))            # noqa: S301 - 自家目录
        os.environ["PATH"] = str(目录) + os.pathsep + os.environ.get("PATH", "")
    except Exception:  # noqa: BLE001
        pass
    if 插件.is_dir():
        # 用户自己设过就别抢（方便排查问题时指向系统 VLC）
        os.environ.setdefault("VLC_PLUGIN_PATH", str(插件))
    return str(目录 / ("libvlc.dll" if os.name == "nt" else "libvlc.so.5"))


def _找库() -> str:
    """按优先级找 libvlc：环境变量 → **包内自带** → ctypes 查找 → 常见 soname。

    自带优先于系统：包里的版本是我们测过的（Windows 上更是唯一能用的来源 ——
    系统里通常没装 VLC）。
    """
    指定 = os.environ.get("V8_3_LIBVLC") or ""
    if 指定 and Path(指定).exists():
        return 指定
    自带 = 自带库目录()
    if 自带 is not None:
        路径 = _用自带库(自带)
        try:
            ctypes.CDLL(路径)
            return 路径
        except OSError:
            pass                       # 自带这份坏了就继续往下找系统的
    候选 = [ctypes.util.find_library("vlc"),
          "libvlc.so.5", "libvlc.so"]
    for 名 in 候选:
        if not 名:
            continue
        try:
            ctypes.CDLL(名)
            return 名
        except OSError:
            continue
    return ""


# 已经配好 restype/argtypes 的符号名（模块级共享，因为 CDLL 对象是共享的）
_已配类型: set[str] = set()

# 这些名字一被取到就会立刻调用，必须保证它们**已经配好 argtypes**：没配的话
# ctypes 会把 64 位播放器句柄按 C int 传（截断）→ libvlc 拿着野指针加锁 →
# 段错误。实测踩坑点：VLC 3.0 没有 libvlc_media_player_get_spu，回退到
# libvlc_video_get_spu 时崩在 libvlc_video_get_spu+0x28。
_关键符号 = (
    "libvlc_media_player_get_spu_description",
    "libvlc_video_get_spu_description",
    "libvlc_media_player_get_audio_track_description",
    "libvlc_audio_get_track_description",
    "libvlc_video_get_track_description",
    "libvlc_media_player_set_spu", "libvlc_video_set_spu",
    "libvlc_media_player_set_audio_track", "libvlc_audio_set_track",
    "libvlc_media_player_get_spu", "libvlc_video_get_spu",
    "libvlc_audio_get_track", "libvlc_track_description_list_release",
    "libvlc_video_get_aspect_ratio", "libvlc_video_set_aspect_ratio",
    "libvlc_video_get_scale", "libvlc_video_set_scale",
    "libvlc_media_player_get_chapter_count", "libvlc_media_player_get_chapter",
    "libvlc_media_player_set_chapter", "libvlc_media_player_next_chapter",
    "libvlc_media_player_previous_chapter", "libvlc_media_player_next_frame",
)


def 缺类型符号(库) -> list[str]:
    """返回**存在但没配 argtypes** 的关键符号名（自检用；正常应为空）。"""
    return [名 for 名 in _关键符号
            if getattr(库, 名, None) is not None and 名 not in _已配类型]


def 取函数(库, 候选名们, 参数=(), 返回=None):
    """按候选名取一个**参数类型齐全**的 libvlc 函数。

    为什么必须走这里（而不是裸 ``getattr(库, 名字)``）：
    ``getattr`` 对 CDLL 永远成功，但**没设 argtypes** 时 ctypes 会把 Python
    整数按 C ``int``（32 位）传参，64 位播放器句柄被**截断**，libvlc 拿着野
    指针去 ``pthread_mutex_lock`` → **整个进程段错误**（实测 exit 139，崩在
    ``libvlc_video_get_spu+0x28``）。VLC 3.0 里 ``libvlc_media_player_get_spu``
    并不存在，回退名 ``libvlc_video_get_spu`` 又没进过绑定表，于是踩中这个坑。
    所以：**凡是要调用的符号，一律从这里取**。
    """
    参数 = list(参数) or [ctypes.c_void_p]
    for 名 in 候选名们:
        try:
            函数 = getattr(库, 名, None)
        except Exception:  # noqa: BLE001 - 非 ASCII 名字 ctypes 会抛 UnicodeError
            continue
        if 函数 is None:
            continue
        if 名 not in _已配类型:
            try:
                函数.restype = 返回
                函数.argtypes = list(参数)
                _已配类型.add(名)
            except Exception:  # noqa: BLE001 - 个别符号类型设不上就跳过
                continue
        return 函数
    return None


def 校验句柄(值) -> int:
    """把播放器/实例句柄规范成非负 int；**非法值直接报错**。

    为什么要有这道闸：libvlc 的 ``libvlc_video_get_spu`` 之类函数**不检查
    NULL**，直接把野指针（或 None→NULL）交进去就是段错误，连异常都来不及抛。
    所以凡是把句柄递给 libvlc 之前，一律先过这里。
    """
    if isinstance(值, bool) or 值 is None:
        raise TypeError(f"句柄必须是整数，收到 {值!r}")
    数 = int(值)
    if 数 < 0 or 数 > 2 ** 64 - 1:
        raise ValueError(f"句柄超出指针范围：{数}")
    return 数


class VLC库:
    """libvlc 的函数签名集合（只绑我们要用的）。"""

    _实例: Optional["VLC库"] = None
    _加载错误 = ""

    def __init__(self) -> None:
        路径 = _找库()
        if not 路径:
            raise 库不可用(
                "找不到 libvlc（VLC 的运行库）。\n"
                "  发布包本应自带一份（运行环境/vlc），若缺失请重新解压完整包；\n"
                "  源码运行的话，自行安装 VLC 即可：\n"
                "  Arch/Tea Linux: sudo pacman -S vlc\n"
                "  Debian/Ubuntu:  sudo apt install libvlc5 vlc-plugin-base\n"
                "或者用环境变量 V8_3_LIBVLC 指向 libvlc.so 的绝对路径。")
        try:
            self.lib = ctypes.CDLL(路径)
        except OSError as e:  # pragma: no cover - 环境问题
            raise 库不可用(f"加载 libvlc 失败：{e}") from e
        self.路径 = 路径
        self._绑定()

    # ---------------- 签名 ----------------

    def _绑定(self) -> None:
        L = self.lib
        L.libvlc_new.restype = ctypes.c_void_p
        L.libvlc_new.argtypes = [ctypes.c_int,
                              ctypes.POINTER(ctypes.c_char_p)]
        L.libvlc_release.argtypes = [ctypes.c_void_p]
        L.libvlc_get_version.restype = ctypes.c_char_p

        L.libvlc_media_new_location.restype = ctypes.c_void_p
        L.libvlc_media_new_location.argtypes = [ctypes.c_void_p,
                                              ctypes.c_char_p]
        # 本地文件必须用 new_path（new_location 只认带 scheme 的 MRL）
        L.libvlc_media_new_path.restype = ctypes.c_void_p
        L.libvlc_media_new_path.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        L.libvlc_media_release.argtypes = [ctypes.c_void_p]
        L.libvlc_media_add_option.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        L.libvlc_media_get_stats.restype = ctypes.c_int
        L.libvlc_media_get_stats.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(_媒体统计结构)]

        L.libvlc_media_player_new.restype = ctypes.c_void_p
        L.libvlc_media_player_new.argtypes = [ctypes.c_void_p]
        L.libvlc_media_player_release.argtypes = [ctypes.c_void_p]
        L.libvlc_media_player_set_media.argtypes = [ctypes.c_void_p,
                                                 ctypes.c_void_p]
        L.libvlc_media_player_get_media.restype = ctypes.c_void_p
        L.libvlc_media_player_get_media.argtypes = [ctypes.c_void_p]
        L.libvlc_media_player_set_xwindow.argtypes = [ctypes.c_void_p,
                                                   ctypes.c_uint32]
        L.libvlc_media_player_play.restype = ctypes.c_int
        L.libvlc_media_player_play.argtypes = [ctypes.c_void_p]
        L.libvlc_media_player_pause.argtypes = [ctypes.c_void_p]
        L.libvlc_media_player_stop.argtypes = [ctypes.c_void_p]
        L.libvlc_media_player_is_playing.restype = ctypes.c_int
        L.libvlc_media_player_is_playing.argtypes = [ctypes.c_void_p]
        L.libvlc_media_player_get_state.restype = ctypes.c_int
        L.libvlc_media_player_get_state.argtypes = [ctypes.c_void_p]
        L.libvlc_media_player_get_length.restype = ctypes.c_longlong
        L.libvlc_media_player_get_length.argtypes = [ctypes.c_void_p]
        L.libvlc_media_player_get_time.restype = ctypes.c_longlong
        L.libvlc_media_player_get_time.argtypes = [ctypes.c_void_p]
        L.libvlc_media_player_set_time.argtypes = [ctypes.c_void_p,
                                                ctypes.c_longlong]
        L.libvlc_media_player_get_position.restype = ctypes.c_float
        L.libvlc_media_player_get_position.argtypes = [ctypes.c_void_p]
        L.libvlc_media_player_set_position.argtypes = [ctypes.c_void_p,
                                                    ctypes.c_float]
        L.libvlc_media_player_set_rate.argtypes = [ctypes.c_void_p,
                                                ctypes.c_float]
        L.libvlc_media_player_get_rate.restype = ctypes.c_float
        L.libvlc_media_player_get_rate.argtypes = [ctypes.c_void_p]
        L.libvlc_media_player_get_fps.restype = ctypes.c_float
        L.libvlc_media_player_get_fps.argtypes = [ctypes.c_void_p]
        L.libvlc_media_player_has_vout.restype = ctypes.c_int
        L.libvlc_media_player_has_vout.argtypes = [ctypes.c_void_p]
        L.libvlc_media_player_set_pause.argtypes = [ctypes.c_void_p,
                                                 ctypes.c_int]

        L.libvlc_audio_set_volume.restype = ctypes.c_int
        L.libvlc_audio_set_volume.argtypes = [ctypes.c_void_p, ctypes.c_int]
        L.libvlc_audio_get_volume.restype = ctypes.c_int
        L.libvlc_audio_get_volume.argtypes = [ctypes.c_void_p]
        L.libvlc_audio_set_mute.argtypes = [ctypes.c_void_p, ctypes.c_int]
        L.libvlc_audio_get_mute.restype = ctypes.c_int
        L.libvlc_audio_get_mute.argtypes = [ctypes.c_void_p]

        L.libvlc_video_take_snapshot.restype = ctypes.c_int
        L.libvlc_video_take_snapshot.argtypes = [
            ctypes.c_void_p, ctypes.c_uint, ctypes.c_char_p,
            ctypes.c_uint, ctypes.c_uint]

        # ⚠️ 不同 VLC 版本的符号名不完全一样（3.0 用 video_* 而不是
        # media_player_* 取字幕/音轨），所以这里**只绑存在的**，缺的记下来，
        # 调用点再按 self.有(名) 判断，避免"一个符号缺失整个播放器起不来"。
        self.缺失符号: list[str] = []

        def 绑(名: str, 返回, 参数) -> bool:
            函数 = getattr(L, 名, None)
            if 函数 is None:
                self.缺失符号.append(名)
                return False
            函数.restype = 返回
            函数.argtypes = 参数
            _已配类型.add(名)
            return True

        for 组 in (("libvlc_media_player_get_spu_count",
                  "libvlc_video_get_spu_count",
                  "libvlc_media_player_get_audio_track_count",
                  "libvlc_audio_get_track_count",
                  "libvlc_media_player_get_chapter_count"),):
            for 名 in 组:
                绑(名, ctypes.c_int, [ctypes.c_void_p])
        for 名 in ("libvlc_media_player_get_spu_description",
                  "libvlc_video_get_spu_description",
                  "libvlc_media_player_get_audio_track_description",
                  "libvlc_audio_get_track_description",
                  "libvlc_video_get_track_description"):
            绑(名, ctypes.POINTER(_轨道描述), [ctypes.c_void_p])
        for 名 in ("libvlc_media_player_set_spu", "libvlc_video_set_spu",
                  "libvlc_media_player_set_audio_track",
                  "libvlc_audio_set_track"):
            绑(名, ctypes.c_int, [ctypes.c_void_p, ctypes.c_int])
        # ---- VLC 风格功能要用的：宽高比 / 缩放 / 章节 / 逐帧 ----
        绑("libvlc_video_get_aspect_ratio", ctypes.c_char_p, [ctypes.c_void_p])
        绑("libvlc_video_set_aspect_ratio", None,
          [ctypes.c_void_p, ctypes.c_char_p])
        绑("libvlc_video_get_scale", ctypes.c_float, [ctypes.c_void_p])
        绑("libvlc_video_set_scale", None, [ctypes.c_void_p, ctypes.c_float])
        绑("libvlc_media_player_get_chapter_count", ctypes.c_int,
          [ctypes.c_void_p])
        绑("libvlc_media_player_get_chapter", ctypes.c_int, [ctypes.c_void_p])
        绑("libvlc_media_player_set_chapter", None,
          [ctypes.c_void_p, ctypes.c_int])
        绑("libvlc_media_player_next_chapter", None, [ctypes.c_void_p])
        绑("libvlc_media_player_previous_chapter", None, [ctypes.c_void_p])
        绑("libvlc_media_player_next_frame", None, [ctypes.c_void_p])
        绑("libvlc_media_player_set_pause", None,
          [ctypes.c_void_p, ctypes.c_int])
        绑("libvlc_media_player_has_vout", ctypes.c_int, [ctypes.c_void_p])
        绑("libvlc_audio_get_mute", ctypes.c_int, [ctypes.c_void_p])
        绑("libvlc_audio_set_mute", None, [ctypes.c_void_p, ctypes.c_int])
        绑("libvlc_audio_get_volume", ctypes.c_int, [ctypes.c_void_p])
        绑("libvlc_audio_set_volume", ctypes.c_int,
          [ctypes.c_void_p, ctypes.c_int])
        绑("libvlc_media_player_get_state", ctypes.c_int, [ctypes.c_void_p])
        绑("libvlc_media_player_get_fps", ctypes.c_float, [ctypes.c_void_p])
        绑("libvlc_media_player_is_playing", ctypes.c_int, [ctypes.c_void_p])
        绑("libvlc_media_player_get_time", ctypes.c_longlong, [ctypes.c_void_p])
        绑("libvlc_media_player_set_time", None,
          [ctypes.c_void_p, ctypes.c_longlong])
        绑("libvlc_media_player_get_length", ctypes.c_longlong,
          [ctypes.c_void_p])
        绑("libvlc_media_player_get_position", ctypes.c_float, [ctypes.c_void_p])
        绑("libvlc_media_player_set_position", None,
          [ctypes.c_void_p, ctypes.c_float])
        绑("libvlc_media_player_set_rate", None, [ctypes.c_void_p, ctypes.c_float])
        绑("libvlc_media_player_get_rate", ctypes.c_float, [ctypes.c_void_p])
        绑("libvlc_media_player_get_spu", ctypes.c_int, [ctypes.c_void_p])
        # VLC 3.0 只有 video_* 这一版名字（media_player_* 是 VLC 4 的），
        # 两个都绑上，调用点才能安全回退。
        绑("libvlc_video_get_spu", ctypes.c_int, [ctypes.c_void_p])
        绑("libvlc_audio_get_track", ctypes.c_int, [ctypes.c_void_p])
        绑("libvlc_track_description_list_release", None,
          [ctypes.POINTER(_轨道描述)])
        绑("libvlc_media_player_add_slave", ctypes.c_int,
          [ctypes.c_void_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_int])

    def 缺类型符号(self) -> list[str]:
        """本库里**存在但没配 argtypes** 的关键符号（正常为空）。"""
        return 缺类型符号(self.lib)

    # ---------------- 单例 ----------------

    @classmethod
    def 取(cls) -> "VLC库":
        if cls._实例 is not None:
            return cls._实例
        try:
            cls._实例 = cls()
            cls._加载错误 = ""
        except 库不可用 as e:
            cls._加载错误 = str(e)
            raise
        return cls._实例

    @classmethod
    def 版本(cls) -> str:
        try:
            库 = cls.取()
            return (库.lib.libvlc_get_version() or b"").decode(
                "utf-8", "replace")
        except Exception:
            return ""


class VLC:
    """一个 libvlc 实例 + 一个 media player 的轻封装。

    用法::

        播放器 = VLC(窗口句柄=widget.winId(), 日志回调=print)
        播放器.播放("https://…/video.mp4", 选项=[":network-caching=6000"])
        播放器.暂停(); 播放器.跳转(120.0); 播放器.关闭()
    """

    #: libvlc 实例参数：不显示标题、别写用户目录里的插件缓存、安静
    实例参数 = [
        "--intf=dummy",              # 不要任何界面（我们用 Qt 控制）
        "--no-video-title-show",
        "--no-snapshot-preview",
        "--no-plugins-cache",        # 不写 ~/.cache/vlc
        "--no-metadata-network-access",   # 别为了封面去联网
        "--quiet",
        # ⚠️ 不要加 --no-stats：卡顿诊断（丢帧/码率）就靠它
    ]

    def __init__(self, 窗口句柄: int = 0, 日志回调: Optional[Callable] = None,
                 基础选项: list[str] | None = None,
                 静音: bool = False,
                 实例视频输出: str = "",
                 嵌入窗口号: int = 0):
        self._日志 = 日志回调 or (lambda *_: None)
        库 = VLC库.取()
        self._lib = 库.lib
        self.路径 = 库.路径
        参数 = list(self.实例参数) + list(基础选项 or [])
        # ⚠️ 视频输出**必须**是 libvlc 实例级选项（``--vout=``）。
        #    实测（cvlc -vvv，2026-09-21）：
        #      ``cvlc --vout=xcb_x11 …``   → using vout display module "xcb_x11" ✓
        #      ``cvlc ":vout=xcb_x11" …``  → looking for vout display module matching "any"
        #                                     → using vout display module "gl" ✗
        #    也就是说**媒体级**的 ``:vout=`` 根本不生效（VLC 在 media scope 之外
        #    就把输出模块定下来了）。这正是"多出一个 VLC media player 窗口"
        #    反复治不好的原因：以前不管是规则给的 ``:vout=gl`` 还是兜底把它删掉，
        #    都在改一个**没人看**的选项。
        实例视频输出 = str(实例视频输出 or "").strip()
        if 实例视频输出:
            参数.append(f"--vout={实例视频输出}")
        # ⚠️ 嵌入**只有一条路**：``libvlc_media_player_set_xwindow(窗口号)``。
        #    真机矩阵实测（用户这台机器，光鸭 4K60 HEVC 直链 + vaapi，2026-09-21）：
        #
        #    ==================================================  ==================  ========
        #    做法                                                vout window 模块    游离窗口
        #    ==================================================  ==================  ========
        #    set_xwindow(窗口)                                   "embed-xid,any" ✓   无 ✓
        #    set_xwindow(窗口) + 软件解码                          "embed-xid,any" ✓   无 ✓
        #    set_xwindow(窗口) + 不钉 vout（VLC 自选 gl+vaapi）     "embed-xid,any" ✓   无 ✓
        #    实例级 --drawable-xid + --embedded-video（不调上面那个） "any" ✗           **有** ✗
        #    ==================================================  ==================  ========
        #
        #    结论：实例级 ``--drawable-xid`` **不会**触发嵌入（VLC 会自己开一个顶层窗口，
        #    也就是用户反复看到的 "VLC media player"）；而 ``set_xwindow`` 一定能嵌。
        #    V1.0.9 我一度改成"实例级 drawable"，那正好把嵌入关掉了 —— 用户反馈的"更严重"
        #    就是这个。现在起播前**一定**走 set_xwindow。
        #
        #    另外：set_xwindow 会把实例级 ``--vout`` 重置回 "any"，所以"钉死视频输出"在
        #    嵌入路径上是**无效**的（VLC 自己挑 gl + glconv_vaapi_x11，实测能正常嵌住）。
        #    真正需要钉 vout 的只有"没有 drawable、VLC 自己开窗口"的场景。
        嵌入窗口号 = int(嵌入窗口号 or 0)
        self.用实例drawable = False
        if 静音:
            参数 += ["--no-audio"]
        文本 = [a.encode("utf-8") for a in 参数]
        数组 = (ctypes.c_char_p * len(文本))(*文本)
        #: 这个实例在 ``libvlc_new`` 时钉死的视频输出模块（"" = 没钉）
        self.实例视频输出 = str(实例视频输出 or "")
        self._实例 = self._lib.libvlc_new(len(文本), 数组)
        if not self._实例:
            raise 库不可用("libvlc_new 失败（参数不合法或资源不足）")
        self._播放器 = self._lib.libvlc_media_player_new(self._实例)
        if not self._播放器:
            self._lib.libvlc_release(self._实例)
            self._实例 = None
            raise 库不可用("libvlc_media_player_new 失败")
        self._媒体 = None
        #: 视频要画到哪个窗口号（X11 上是实例级 --drawable-xid 生效；Windows 走 set_hwnd）
        self.窗口句柄 = int(嵌入窗口号 or 窗口句柄 or 0)
        self.当前地址 = ""
        self.已用选项: list[str] = []
        self._已关闭 = False
        self.起播时间 = 0.0
        if self.窗口句柄 and not self.用实例drawable:
            self.绑定窗口(self.窗口句柄)

    # ---------------- 基础 ----------------

    def 绑定窗口(self, 句柄: int) -> None:
        """把视频输出挂到已有窗口（X11 的 window id）。

        ⚠️ 这个方法会**抹掉实例级 ``--vout`` 钉死**（VLC 内部把 vout 重置成 "any"），
        所以嵌入播放走的是实例级 ``--drawable-xid``（见 :meth:`__init__`），
        不调这里。句柄真的变了要重新起播（``播放会话.起播`` 会重建实例）。
        """
        句柄 = int(句柄 or 0)
        if self.用实例drawable and 句柄 == self.窗口句柄:
            return                      # 已经由实例级 drawable 绑好了，别再抹掉钉死
        if self.用实例drawable and 句柄 != self.窗口句柄:
            self._日志("[播放] 嵌入窗口变了：实例级 --drawable-xid 是建实例时定下的，"
                     "这里只能退回 set_xwindow —— 视频输出的钉死会被 VLC 重置，"
                     "建议重新起播（会自动重建实例）")
        self.窗口句柄 = 句柄
        if self.窗口句柄 and self._播放器:
            # VLC 3.x：X11 用 set_xwindow；失败也不致命（可能还没起 vout）
            try:
                self._lib.libvlc_media_player_set_xwindow(self._播放器,
                                                       self.窗口句柄)
            except Exception as e:  # pragma: no cover
                self._日志(f"[播放] 绑定窗口失败：{e}")

    def _媒体选项(self, 选项: list[str]) -> None:
        for 项 in 选项 or []:
            文本 = 项 if 项.startswith(":") else f":{项}"
            try:
                self._lib.libvlc_media_add_option(self._媒体,
                                                文本.encode("utf-8"))
                self.已用选项.append(文本)
            except Exception as e:  # pragma: no cover
                self._日志(f"[播放] 选项被忽略 {文本}：{e}")

    def 播放(self, 地址: str, 选项: list[str] | None = None,
            请求头: dict | None = None) -> bool:
        """开始播放一个 URL（含网盘直链需要的请求头）。

        ⚠️ 真起播前**一定先"停止并等待"**（不是只发一个 stop 就算了）：
        旧的 vout 还活着就起新的，libvlc 会因为 drawable 正忙而改成
        **自己开一个顶层窗口**放画面 —— 用户看到的就是"又多出来一个 VLC 窗口"。
        放在这里（而不是各个调用方）是**结构性**保证：不管谁调 ``播放()``
        （首次起播、AI 换参数重载、换片、接管、画面自愈）都不会踩这个竞态。
        首次起播时它没有任何东西要停，几乎不花时间。
        """
        if self._已关闭:
            return False
        self.停止并等待(3.0)
        文本地址 = str(地址)
        # 带 scheme（http/https/rtsp/…）走 new_location；本地路径走 new_path
        有协议 = "://" in 文本地址 or 文本地址.startswith(("rtsp:", "rtmp:"))
        if 有协议:
            媒体 = self._lib.libvlc_media_new_location(
                self._实例, 文本地址.encode("utf-8"))
        else:
            媒体 = self._lib.libvlc_media_new_path(
                self._实例, 文本地址.encode("utf-8"))
        if not 媒体:
            self._日志(f"[播放] 创建媒体失败：{地址[:80]}")
            return False
        self._媒体 = 媒体
        self.当前地址 = str(地址)
        self.已用选项 = []
        # 请求头：网盘直链基本都要 Referer/UA（有的还要 Cookie）
        头选项 = []
        if 请求头:
            for 键, 值 in 请求头.items():
                if not 值:
                    continue
                低 = str(键).lower()
                if 低 == "referer":
                    头选项.append(f":http-referrer={值}")
                elif 低 == "user-agent":
                    头选项.append(f":http-user-agent={值}")
                elif 低 == "cookie":
                    头选项.append(f":http-cookies={值}")
                elif 低 == "origin":
                    头选项.append(f":http-origin={值}")
                else:
                    头选项.append(f":http-{低}={值}")
        self._媒体选项(头选项 + list(选项 or []))
        self._lib.libvlc_media_player_set_media(self._播放器, 媒体)
        if self.窗口句柄:
            self.绑定窗口(self.窗口句柄)
        self.起播时间 = time.time()
        结果 = self._lib.libvlc_media_player_play(self._播放器)
        if 结果 != 0:
            self._日志(f"[播放] 起播返回非 0：{结果}")
            return False
        return True

    def 暂停(self) -> None:
        if self._播放器:
            self._lib.libvlc_media_player_pause(self._播放器)

    def 设置暂停(self, 暂停: bool) -> None:
        if self._播放器:
            self._lib.libvlc_media_player_set_pause(self._播放器,
                                                 1 if 暂停 else 0)

    def 停止(self) -> None:
        if self._播放器:
            try:
                self._lib.libvlc_media_player_stop(self._播放器)
            except Exception:
                pass
        if self._媒体:
            try:
                self._lib.libvlc_media_release(self._媒体)
            except Exception:
                pass
            self._媒体 = None

    def 等vout消失(self, 超时秒: float = 3.0) -> bool:
        """等视频输出（vout）真的没了；返回是否等到。

        为什么必须等这个：``停止`` 只是请求，vout 是**异步**释放的。
        如果这时就把旧的 drawable（比如要关掉的独立窗口）销毁掉，
        VLC 的 vout 线程会踩到已销毁的窗口 —— 实测：整个进程卡死在事件循环里，
        或者 VLC 干脆另开一个顶层窗口把画面丢进去（用户反复看到的
        "关掉独立窗口又冒出 VLC media player"）。
        ``libvlc_media_player_has_vout()`` 是 libvlc 的公开 API，拿它轮询最可靠。
        """
        if not self._播放器:
            return True
        截止 = time.time() + max(0.1, float(超时秒))
        while time.time() < 截止:
            try:
                if not self._lib.libvlc_media_player_has_vout(self._播放器):
                    return True
            except Exception:  # noqa: BLE001
                return True
            time.sleep(0.05)
        return False

    def 停止并等待(self, 超时秒: float = 3.0) -> bool:
        """停止播放并**等它真的停下来**（vout 释放是异步的）。

        为什么必须等：播放中直接 `set_xwindow` 换绑不会搬走已有的视频输出，
        紧接着再 `播放()` 就会在新句柄上**又开一个 vout** —— 用户看到的
        "多出来一个超大窗口、两个窗口都在放同一个视频"就是这么来的。
        等到状态变成 Stopped（或超时）再换绑/重播，才只可能有一个窗口。
        """
        self.停止()
        if not self._播放器:
            return True
        截止 = time.time() + max(0.1, float(超时秒))
        while time.time() < 截止:
            try:
                if int(self.状态) in (播放状态.已停止, 播放状态.已结束,
                                    播放状态.出错):
                    return True
            except Exception:
                return True
            time.sleep(0.05)
        return False

    # ---------------- 状态 / 进度 ----------------

    @property
    def 状态(self) -> int:
        if not self._播放器:
            return 播放状态.已停止
        return int(self._lib.libvlc_media_player_get_state(self._播放器))

    def 是否在播(self) -> bool:
        return bool(self._播放器 and
                    self._lib.libvlc_media_player_is_playing(self._播放器))

    def 时长秒(self) -> float:
        if not self._播放器:
            return 0.0
        return max(0.0, self._lib.libvlc_media_player_get_length(
            self._播放器) / 1000.0)

    def 进度秒(self) -> float:
        if not self._播放器:
            return 0.0
        return max(0.0, self._lib.libvlc_media_player_get_time(
            self._播放器) / 1000.0)

    def 跳转(self, 秒: float) -> None:
        if self._播放器:
            self._lib.libvlc_media_player_set_time(
                self._播放器, int(max(0.0, float(秒)) * 1000))

    def 按比例跳转(self, 比例: float) -> None:
        if self._播放器:
            self._lib.libvlc_media_player_set_position(
                self._播放器, ctypes.c_float(max(0.0, min(1.0, 比例))))

    def 有画面(self) -> bool:
        return bool(self._播放器 and
                    self._lib.libvlc_media_player_has_vout(self._播放器))

    def 帧率(self) -> float:
        if not self._播放器:
            return 0.0
        return float(self._lib.libvlc_media_player_get_fps(self._播放器) or 0)

    # ---------------- 音量 / 速率 ----------------

    def 设置音量(self, 音量: int) -> None:
        if self._播放器:
            self._lib.libvlc_audio_set_volume(self._播放器,
                                           int(max(0, min(200, 音量))))

    def 取音量(self) -> int:
        if not self._播放器:
            return 0
        return int(self._lib.libvlc_audio_get_volume(self._播放器) or 0)

    def 设置静音(self, 静音: bool) -> None:
        if self._播放器:
            self._lib.libvlc_audio_set_mute(self._播放器, 1 if 静音 else 0)

    def 是否静音(self) -> bool:
        if not self._播放器:
            return False
        return bool(self._lib.libvlc_audio_get_mute(self._播放器))

    def 设置速率(self, 倍速: float) -> None:
        if self._播放器:
            self._lib.libvlc_media_player_set_rate(
                self._播放器, ctypes.c_float(max(0.25, min(4.0, 倍速))))

    def 取速率(self) -> float:
        if not self._播放器:
            return 1.0
        return float(self._lib.libvlc_media_player_get_rate(self._播放器) or 1)

    # ---------------- 字幕 / 音轨 ----------------

    def 播放器可用(self) -> bool:
        """播放器句柄是否可用（过校验闸）。"""
        return self.句柄() is not None

    def _有(self, 名: str) -> bool:
        return getattr(self._lib, 名, None) is not None

    def 缺类型符号(self) -> list[str]:
        """返回**存在但没配 argtypes** 的符号名（自检用；正常应为空）。

        非空就意味着随时可能踩到"句柄被截断 → 段错误"，必须马上补绑定表。
        """
        return 缺类型符号(self._lib)

    def _列轨道(self, 候选名们: tuple[str, ...]) -> list[音视频轨]:
        结果: list[音视频轨] = []
        if not self._播放器:
            return 结果
        函数 = self.取函数(候选名们, 返回=ctypes.POINTER(_轨道描述))
        if 函数 is None:
            return 结果
        try:
            头 = 函数(self._播放器)
        except Exception:
            return 结果
        指针 = 头
        保护 = []
        while 指针:
            项 = 指针.contents
            保护.append(项)
            名称 = (项.psz_name or b"").decode("utf-8", "replace")
            结果.append(音视频轨(int(项.i_id), 名称))
            指针 = 项.p_next
        try:
            释放 = self.取函数(
                ("libvlc_track_description_list_release",),
                参数=[ctypes.POINTER(_轨道描述)])
            if 释放 is not None:
                释放(头)
        except Exception:
            pass
        return 结果

    # ---------------- 通用取函数（必须设置 argtypes，见 取函数 的说明）----

    def 取函数(self, 候选名们, 参数=(), 返回=None):
        """带类型安全地取 libvlc 函数；返回 None 表示这些名字都没有。"""
        参数 = list(参数) or [ctypes.c_void_p]
        return 取函数(self._lib, tuple(候选名们), 参数, 返回)

    def 句柄(self):
        """当前播放器句柄（已过 校验句柄 闸）；不可用时返回 None。"""
        if not self._播放器:
            return None
        try:
            return 校验句柄(self._播放器)
        except Exception:  # noqa: BLE001 - 句柄可疑就当作不可用，绝不硬调
            return None

    def 字幕轨(self) -> list[音视频轨]:
        if not self._播放器:
            return []
        return self._列轨道(("libvlc_media_player_get_spu_description",
                          "libvlc_video_get_spu_description"))

    def 选择字幕(self, 编号: int) -> bool:
        if not self._播放器:
            return False
        函数 = self.取函数(("libvlc_media_player_set_spu", "libvlc_video_set_spu"),
                        参数=[ctypes.c_void_p, ctypes.c_int], 返回=ctypes.c_int)
        if 函数 is not None:
            return 函数(self._播放器, int(编号)) == 0
        return False

    def 当前字幕(self) -> int:
        if not self._播放器:
            return -1
        函数 = self.取函数(("libvlc_media_player_get_spu", "libvlc_video_get_spu"),
                        参数=[ctypes.c_void_p], 返回=ctypes.c_int)
        if 函数 is None:
            return -1
        try:
            return int(函数(self._播放器))
        except Exception:
            return -1

    def 音频轨(self) -> list[音视频轨]:
        if not self._播放器:
            return []
        return self._列轨道(("libvlc_media_player_get_audio_track_description",
                          "libvlc_audio_get_track_description",
                          "libvlc_video_get_track_description"))

    def 选择音频(self, 编号: int) -> bool:
        if not self._播放器:
            return False
        函数 = self.取函数(
            ("libvlc_media_player_set_audio_track", "libvlc_audio_set_track"),
            参数=[ctypes.c_void_p, ctypes.c_int], 返回=ctypes.c_int)
        if 函数 is not None:
            return 函数(self._播放器, int(编号)) == 0
        return False

    def 支持挂字幕(self) -> bool:
        return self._有("libvlc_media_player_add_slave")

    def 挂字幕文件(self, 路径: str, 选中: bool = True) -> bool:
        """把外部字幕（含 AI 翻译/生成出来的 .srt）挂上并选中。"""
        地址 = str(路径)
        if not 地址.lower().startswith(("http://", "https://", "file://")):
            地址 = Path(地址).resolve().as_uri()
        if not self._播放器:
            return False
        # add_slave(类型=0 字幕, uri, 选中)
        return self._lib.libvlc_media_player_add_slave(
            self._播放器, 0, 地址.encode("utf-8"), 1 if 选中 else 0) == 0

    def 切换字幕编号(self) -> int:
        """在可用字幕轨里循环切换（无 → 1 → 2 → … → 无），返回新编号。"""
        轨道 = self.字幕轨()
        if not 轨道:
            return -1
        编号们 = [t.编号 for t in 轨道]
        当前 = self.当前字幕()
        if 当前 not in 编号们:
            下一个 = 编号们[0]
        else:
            位置 = 编号们.index(当前)
            下一个 = -1 if 位置 + 1 >= len(编号们) else 编号们[位置 + 1]
        self.选择字幕(下一个)
        return 下一个

    # ---------------- 宽高比 / 缩放 / 章节 / 逐帧（VLC 风格功能）----------------

    #: 常见宽高比（VLC「视频 → 宽高比」那一栏）
    宽高比表 = (("默认", ""), ("16:9", "16:9"), ("4:3", "4:3"), ("1:1", "1:1"),
             ("21:9", "21:9"), ("2.35:1", "2.35:1"), ("5:4", "5:4"),
             ("16:10", "16:10"))

    def 宽高比(self) -> str:
        函数 = self.取函数(("libvlc_video_get_aspect_ratio",),
                        参数=[ctypes.c_void_p], 返回=ctypes.c_char_p)
        if 函数 is None or not self.播放器可用():
            return ""
        try:
            return (函数(self._播放器) or b"").decode("utf-8", "replace")
        except Exception:
            return ""

    def 设置宽高比(self, 比例: str) -> bool:
        """设置宽高比（""=回到默认/原始比例）。"""
        函数 = self.取函数(("libvlc_video_set_aspect_ratio",),
                        参数=[ctypes.c_void_p, ctypes.c_char_p])
        if 函数 is None or not self.播放器可用():
            return False
        try:
            函数(self._播放器, str(比例 or "").encode("utf-8"))
            return True
        except Exception:
            return False

    def 缩放(self) -> float:
        函数 = self.取函数(("libvlc_video_get_scale",),
                        参数=[ctypes.c_void_p], 返回=ctypes.c_float)
        if 函数 is None or not self.播放器可用():
            return 0.0
        try:
            return float(函数(self._播放器) or 0.0)
        except Exception:
            return 0.0

    def 设置缩放(self, 倍率: float) -> bool:
        """缩放画面（0=自动适应窗口）。"""
        函数 = self.取函数(("libvlc_video_set_scale",),
                        参数=[ctypes.c_void_p, ctypes.c_float])
        if 函数 is None or not self.播放器可用():
            return False
        try:
            函数(self._播放器, ctypes.c_float(float(倍率 or 0.0)))
            return True
        except Exception:
            return False

    def 章节数(self) -> int:
        函数 = self.取函数(("libvlc_media_player_get_chapter_count",),
                        参数=[ctypes.c_void_p], 返回=ctypes.c_int)
        if 函数 is None or not self.播放器可用():
            return 0
        try:
            return max(0, int(函数(self._播放器) or 0))
        except Exception:
            return 0

    def 当前章节(self) -> int:
        函数 = self.取函数(("libvlc_media_player_get_chapter",),
                        参数=[ctypes.c_void_p], 返回=ctypes.c_int)
        if 函数 is None or not self.播放器可用():
            return -1
        try:
            return int(函数(self._播放器))
        except Exception:
            return -1

    def 跳章节(self, 编号: int) -> bool:
        函数 = self.取函数(("libvlc_media_player_set_chapter",),
                        参数=[ctypes.c_void_p, ctypes.c_int])
        if 函数 is None or not self.播放器可用():
            return False
        try:
            函数(self._播放器, int(编号))
            return True
        except Exception:
            return False

    def 下一章(self) -> None:
        函数 = self.取函数(("libvlc_media_player_next_chapter",),
                        参数=[ctypes.c_void_p])
        if 函数 is not None and self.播放器可用():
            try:
                函数(self._播放器)
            except Exception:
                pass

    def 上一章(self) -> None:
        函数 = self.取函数(("libvlc_media_player_previous_chapter",),
                        参数=[ctypes.c_void_p])
        if 函数 is not None and self.播放器可用():
            try:
                函数(self._播放器)
            except Exception:
                pass

    def 下一帧(self) -> None:
        """逐帧步进（需要先暂停，VLC 同款行为）。"""
        函数 = self.取函数(("libvlc_media_player_next_frame",),
                        参数=[ctypes.c_void_p])
        if 函数 is not None and self.播放器可用():
            try:
                函数(self._播放器)
            except Exception:
                pass

    # ---------------- 统计 / 截图 ----------------

    def 统计(self) -> 媒体统计:
        空 = 媒体统计(采样时间=time.time())
        if not self._播放器:
            return 空
        媒体 = self._lib.libvlc_media_player_get_media(self._播放器)
        if not 媒体:
            return 空
        结构 = _媒体统计结构()
        成功 = self._lib.libvlc_media_get_stats(媒体, ctypes.byref(结构))
        if not 成功:
            return 空
        return 媒体统计(
            读字节=int(结构.i_read_bytes),
            输入码率bps=float(结构.f_input_bitrate),
            解复用字节=int(结构.i_demux_read_bytes),
            解复码率bps=float(结构.f_demux_bitrate),
            解复损坏=int(结构.i_demux_corrupted),
            解复中断=int(结构.i_demux_discontinuity),
            已解码视频=int(结构.i_decoded_video),
            已解码音频=int(结构.i_decoded_audio),
            已显示帧=int(结构.i_displayed_pictures),
            丢帧=int(结构.i_lost_pictures),
            已播音频缓冲=int(结构.i_played_abuffers),
            丢音频缓冲=int(结构.i_lost_abuffers),
            发送包=int(结构.i_sent_packets),
            发送字节=int(结构.i_sent_bytes),
            发送码率bps=float(结构.f_send_bitrate),
            采样时间=time.time(),
        )

    def 截图(self, 保存路径: str, 宽: int = 0, 高: int = 0) -> bool:
        if not self._播放器:
            return False
        路径 = Path(保存路径)
        路径.parent.mkdir(parents=True, exist_ok=True)
        # 0 = 原始分辨率
        return self._lib.libvlc_video_take_snapshot(
            self._播放器, 0, str(路径).encode("utf-8"),
            int(宽 or 0), int(高 or 0)) == 0

    # ---------------- 收尾 ----------------

    def 关闭(self) -> None:
        """停止播放并释放 libvlc 资源。**绝不阻塞界面线程**。

        为什么要把释放丢到后台线程：``libvlc_media_player_release`` 会等播放器
        内部线程收工，而播网盘视频时 VLC 正卡在 HTTP 流上读数据，release 会一直
        等下去 —— 用户实测"播放网盘视频 → 关闭播放窗口 → 界面卡死，点哪都没反应"，
        根因就是这里在 GUI 线程里同步 release。现在：先请求停止（不阻塞），
        再把释放交给后台线程，界面立刻恢复。
        """
        if self._已关闭:
            return
        self._已关闭 = True
        try:
            self.停止()
        except Exception:
            pass
        播放器, 实例, 库 = self._播放器, self._实例, self._lib
        self._播放器 = None
        self._实例 = None
        if not 播放器 and not 实例:
            return

        def _收尾() -> None:
            try:
                if 播放器:
                    库.libvlc_media_player_release(播放器)
            except Exception:
                pass
            try:
                if 实例:
                    库.libvlc_release(实例)
            except Exception:
                pass

        import threading
        threading.Thread(target=_收尾, name="vlc-释放", daemon=True).start()


def 准备干净环境(项目根: str | Path) -> dict:
    """把 VLC（以及 Qt）要写的用户目录**搬进项目内**，避免污染系统。

    VLC 默认会写 ``~/.config/vlc``（vlcrc）、``~/.local/share/vlc``（播放列表/
    媒体库）、``~/.cache/vlc``（插件缓存）。V8_3 要求"不污染系统环境"，
    所以在 **libvlc_new 之前**把 XDG 三个目录指到项目里。

    必须在创建 Qt/任何 VLC 对象**之前**调用（环境变量只对之后的初始化生效）。
    """
    import os
    根 = Path(项目根)
    目录表 = {
        "XDG_CONFIG_HOME": 根 / "数据" / "运行环境" / "config",
        "XDG_DATA_HOME": 根 / "数据" / "运行环境" / "data",
        "XDG_CACHE_HOME": 根 / "数据" / "运行环境" / "cache",
    }
    结果 = {}
    for 名, 路径 in 目录表.items():
        try:
            路径.mkdir(parents=True, exist_ok=True)
            os.environ[名] = str(路径)
            结果[名] = str(路径)
        except Exception:
            pass
    # libvlc 的缩略图缓存会在第一次播放时自己 mkdir，但**只会建一级**；
    # 目录不存在时它每个缩略图尺寸都刷一条 ERROR 到日志（实测启动后满屏
    # "failed to create directory, this error can be expected on first run"）。
    # 这里替它把目录先建好，日志就干净了。
    try:
        缓存 = 目录表["XDG_CACHE_HOME"] / "thumbnails"
        for 档 in ("normal", "large", "x-large"):
            (缓存 / 档).mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return 结果


def 可用() -> bool:
    """libvlc 是否可用（不抛异常，供界面显示）。"""
    try:
        VLC库.取()
        return True
    except Exception:
        return False


def 不可用原因() -> str:
    try:
        VLC库.取()
        return ""
    except Exception as e:  # noqa: BLE001
        return str(e)
