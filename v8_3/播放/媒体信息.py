# v8_3/播放/媒体信息.py
"""用 ffprobe 探测远端媒体信息（分辨率/码率/编码/音字幕轨）。

为什么要单独探测
================
"能不能流畅播 4K"这件事，取决于**实测带宽 vs 视频码率**。分辨率只是表象：
同样是 4K，H.264 可能 40 Mbps，HEVC 可能 15 Mbps。所以起播前必须拿到：
分辨率、视频码率、音频码率、时长、编码、音轨/字幕轨数量，才能：
  * 判断实测带宽够不够（AI 播放顾问的输入）；
  * 决定网络缓存窗口与预缓冲时长（4K 高码率必须给更大缓存）；
  * 告诉用户"这条视频有没有内嵌字幕"（决定要不要 AI 生成/翻译字幕）。

实现要点
========
* 用系统 **ffprobe**（本机已有；VLC 也自带 libavformat，但直接调 ffprobe 最省事）；
* 网盘直链需要 Referer/UA（有的还要 Cookie）→ 用 ``-headers`` 传进去；
* ffprobe 走网络，可能很慢或失败 → **一律带超时**、失败返回"未知"而不是抛异常，
  让上层可以"信息不详也能播"（只是参数保守一点）。
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

__all__ = ["媒体信息", "探测媒体", "格式化码率", "分辨率档位",
           "ffprobe路径", "视频后缀", "是视频文件"]

#: 能被播放器直接打开的常见视频后缀（网盘页双击播放、选片过滤都用它）
视频后缀 = (".mp4", ".mkv", ".avi", ".mov", ".flv", ".ts", ".m2ts", ".webm",
         ".wmv", ".m4v", ".mpg", ".mpeg", ".rmvb", ".rm", ".3gp", ".ogv",
         ".mpv", ".m2ts", ".vob", ".iso", ".m3u8", ".mpd", ".strm")


def 是视频文件(名字: str) -> bool:
    """名字看着像视频吗（只按后缀判断，不发网络请求）。"""
    return str(名字 or "").lower().endswith(视频后缀)


#: 分辨率 → 档位（用于 AI 决策与学习库分档）
#: 阈值按**短边**（竖屏 1080x1920 的短边也是 1080），这样横竖屏同档。
档位表 = [
    (2160, "4K"),
    (1440, "2K"),
    (1080, "1080p"),
    (720, "720p"),
    (480, "480p"),
    (0, "SD"),
]


def ffprobe路径() -> str:
    return shutil.which("ffprobe") or ""


@dataclass
class 媒体信息:
    """一次探测的结果；未知字段留空/0，绝不因为缺字段而失败。"""

    分辨率: str = ""
    宽: int = 0
    高: int = 0
    档位: str = ""
    视频编码: str = ""
    视频码率bps: int = 0
    音频编码: str = ""
    音频码率bps: int = 0
    时长秒: float = 0.0
    容器: str = ""
    音轨数: int = 0
    字幕轨数: int = 0
    字幕语言: list[str] = field(default_factory=list)
    帧率: float = 0.0
    总码率bps: int = 0
    原始: dict = field(default_factory=dict)
    错误: str = ""

    @property
    def 已知(self) -> bool:
        return bool(self.分辨率 or self.时长秒 or self.视频编码)

    def to_dict(self) -> dict:
        return {
            "分辨率": self.分辨率, "宽": self.宽, "高": self.高,
            "档位": self.档位, "视频编码": self.视频编码,
            "视频码率bps": self.视频码率bps, "音频编码": self.音频编码,
            "音频码率bps": self.音频码率bps, "时长秒": self.时长秒,
            "容器": self.容器, "音轨数": self.音轨数,
            "字幕轨数": self.字幕轨数, "字幕语言": list(self.字幕语言),
            "帧率": self.帧率, "总码率bps": self.总码率bps,
            "已知": self.已知, "错误": self.错误,
        }

    def 摘要(self) -> str:
        if not self.已知:
            return f"媒体信息未知（{self.错误 or '未探测'}）"
        片段 = [self.分辨率 or "?", self.档位 or ""]
        if self.视频编码:
            片段.append(self.视频编码.upper())
        if self.视频码率bps:
            片段.append(格式化码率(self.视频码率bps))
        if self.时长秒:
            片段.append(f"{int(self.时长秒 // 60)}分{int(self.时长秒 % 60):02d}秒")
        if self.字幕轨数:
            片段.append(f"内嵌字幕 {self.字幕轨数}")
        return " · ".join(x for x in 片段 if x)


def 格式化码率(bps: float) -> str:
    值 = float(bps or 0)
    if 值 <= 0:
        return "未知"
    if 值 >= 1_000_000:
        return f"{值 / 1_000_000:.1f} Mbps"
    if 值 >= 1_000:
        return f"{值 / 1_000:.0f} kbps"
    return f"{值:.0f} bps"


def 分辨率档位(宽: int, 高: int) -> str:
    """按**短边**判档位（4K/2K/1080p/720p/480p/SD）。

    ⚠️ 这里以前拿 ``max(宽, 高)`` 去比 ``3840*2160`` 这种**像素总数**，
    1920 < 2073600 于是 **1920x1080 也会被判成 "SD"**——所有视频全落最低档，
    AI 决策和学习库分档跟着一起错。现在用短边比短边阈值（档位表），
    横屏 3840x2160 与竖屏 2160x3840 都是 4K。
    只给了一个维度时按那一边粗判，避免返回空。
    """
    宽 = max(int(宽 or 0), 0)
    高 = max(int(高 or 0), 0)
    短边 = min(宽, 高) if (宽 and 高) else max(宽, 高)
    if not 短边:
        return "SD"
    for 阈值, 名 in 档位表:
        if 阈值 and 短边 >= 阈值:
            return 名
    return "SD"


def _解析分数(文本: str) -> float:
    try:
        if "/" in str(文本):
            分子, 分母 = str(文本).split("/", 1)
            return float(分子) / float(分母 or 1)
        return float(文本)
    except Exception:
        return 0.0


def _码率(值) -> int:
    try:
        return int(float(值 or 0))
    except Exception:
        return 0


def 探测媒体(地址: str, 请求头: dict | None = None,
            超时秒: float = 25.0) -> 媒体信息:
    """探测 URL（或本地路径）的媒体信息；失败返回带 ``错误`` 的空对象。"""
    ffprobe = ffprobe路径()
    if not ffprobe:
        return 媒体信息(错误="找不到 ffprobe（请安装 ffmpeg）")
    # 请求头 → ffprobe -headers 需要 CRLF 分隔
    头文本 = ""
    if 请求头:
        行 = [f"{k}: {v}" for k, v in 请求头.items() if v]
        头文本 = "\r\n".join(行) + "\r\n"
    命令 = [ffprobe, "-v", "error", "-print_format", "json",
           "-show_format", "-show_streams", "-analyzeduration", "6000000",
           "-probesize", "6000000"]
    if 头文本:
        命令 += ["-headers", 头文本]
    命令 += [str(地址)]
    try:
        进程 = subprocess.run(命令, capture_output=True, text=True,
                            timeout=max(5.0, 超时秒), encoding="utf-8",
                            errors="replace")
    except subprocess.TimeoutExpired:
        return 媒体信息(错误=f"探测超时（>{超时秒:.0f}s）")
    except Exception as e:  # noqa: BLE001
        return 媒体信息(错误=f"{type(e).__name__}: {e}")
    if 进程.returncode != 0:
        尾巴 = (进程.stderr or "").strip().splitlines()
        return 媒体信息(错误=(尾巴[-1][:160] if 尾巴 else "ffprobe 失败"))
    try:
        数据 = json.loads(进程.stdout or "{}")
    except Exception as e:  # noqa: BLE001
        return 媒体信息(错误=f"ffprobe 输出不是 JSON：{e}")
    return 解析探测结果(数据)


def 解析探测结果(数据: dict) -> 媒体信息:
    """把 ffprobe 的 JSON 转成 :class:`媒体信息`（纯函数，方便单测）。"""
    信息 = 媒体信息(原始=数据 if isinstance(数据, dict) else {})
    流们 = (数据 or {}).get("streams") or []
    格式 = (数据 or {}).get("format") or {}
    信息.容器 = str(格式.get("format_name") or "").split(",")[0]
    信息.时长秒 = float(格式.get("duration") or 0)
    信息.总码率bps = _码率(格式.get("bit_rate"))
    for 流 in 流们:
        类型 = str(流.get("codec_type") or "")
        if 类型 == "video" and not 信息.视频编码:
            信息.视频编码 = str(流.get("codec_name") or "")
            信息.宽 = int(流.get("width") or 0)
            信息.高 = int(流.get("height") or 0)
            信息.分辨率 = f"{信息.宽}x{信息.高}" if 信息.宽 else ""
            信息.档位 = 分辨率档位(信息.宽, 信息.高)
            信息.视频码率bps = _码率(流.get("bit_rate"))
            信息.帧率 = _解析分数(流.get("avg_frame_rate")
                             or 流.get("r_frame_rate") or "0")
            if not 信息.时长秒 and 流.get("duration"):
                信息.时长秒 = float(流.get("duration") or 0)
        elif 类型 == "audio":
            信息.音轨数 += 1
            if not 信息.音频编码:
                信息.音频编码 = str(流.get("codec_name") or "")
                信息.音频码率bps = _码率(流.get("bit_rate"))
        elif 类型 == "subtitle":
            信息.字幕轨数 += 1
            语言 = str((流.get("tags") or {}).get("language") or "").strip()
            标题 = str((流.get("tags") or {}).get("title") or "").strip()
            信息.字幕语言.append(语言 or 标题 or f"字幕{信息.字幕轨数}")
    # 容器有时不给流码率：用总码率 - 音频码率兜一下，至少量级对
    if not 信息.视频码率bps and 信息.总码率bps:
        信息.视频码率bps = max(0, 信息.总码率bps - 信息.音频码率bps)
    if not 信息.时长秒 and 信息.总码率bps and 格式.get("size"):
        try:
            信息.时长秒 = float(格式["size"]) * 8 / 信息.总码率bps
        except Exception:
            pass
    return 信息
