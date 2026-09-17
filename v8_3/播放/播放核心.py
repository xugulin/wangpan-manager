# v8_3/播放/播放核心.py
"""播放会话：把"网盘直链"变成"能播、能拖、能调优"的一件事。

一次播放的完整链路
==================
::

    网盘标识 + 远端路径
        │  ① 桥取直链（签名 URL + 请求头：UA/Referer/Cookie）
        ▼
    直链探测（TTFB / 实测带宽 / Range）  +  ffprobe 媒体信息（分辨率/码率/字幕轨）
        │  ② AI 播放顾问给参数（本地模型优先，规则兜底）
        ▼
    libvlc 起播（:network-caching / :avcodec-hw / :http-* / 预读选项）
        │  ③ 播放中每 2 秒采样统计（丢帧/缓冲）→ 卡顿就给出新参数
        ▼
    自动重载更优参数（保持播放位置）或提示用户；效果写进学习库

为什么"自动重载"是核心优化手段
==============================
libvlc 的**绝大多数选项只能在起播前生效**（网络缓存、硬解、时钟）；
卡顿发生时改不了。所以真正的优化是"**带着新参数从当前位置重开**"——
用户几乎无感（1 秒内回到原位置），但缓存/硬解确实换了。
这正是 VLC 用户手动做的事，我们让 AI 顾问自动做。

本模块**不依赖 Qt**：窗口句柄由界面传进来，方便在没有界面的情况下测试。
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .vlc绑定 import VLC, 播放状态, 可用 as vlc可用, 不可用原因
from .媒体信息 import 媒体信息, 探测媒体, 格式化码率
from .直链探测 import 探测直链, 探测头, 探测结果, 格式化带宽

logger = logging.getLogger(__name__)

__all__ = ["播放设置", "播放会话", "规则参数", "硬解能力"]


# --------------------------------------------------------------------------
# 播放参数
# --------------------------------------------------------------------------


@dataclass
class 播放设置:
    """起播参数（AI 顾问或规则给出）。"""

    网络缓存毫秒: int = 4000
    起播等待秒: float = 2.0
    硬解: str = "auto"            # auto / vaapi / none
    附加选项: list[str] = field(default_factory=list)
    可流畅播放: bool = True
    理由: str = ""
    风险: str = ""
    来源: str = "规则"
    #: 允许丢弃"迟到帧"。**默认 False** = 画面完整优先（VLC 的 no-drop-late-frames）。
    #: 但 4K/60fps/高码率这种吃力的片源，"一个都不许丢"反而会越积越晚 →
    #: 观感就是持续性的小卡顿。这种片源改成允许丢帧（VLC 默认行为）更顺。
    允许丢帧: bool = False
    #: 视频输出模块。""=用 VLC 默认；"gl"=GPU 缩放/色彩转换（4K 缩到屏幕时省 CPU）。
    视频输出: str = ""

    def libvlc选项(self) -> list[str]:
        选项 = [f":network-caching={max(500, int(self.网络缓存毫秒))}",
              f":file-caching={max(500, int(self.网络缓存毫秒))}",
              ":http-reconnect=true",
              # 直播/流式场景下关掉时钟抖动补偿，减少 A/V 不同步
              ":clock-jitter=0", ":clock-synchro=0",
              ":avcodec-threads=0"]
        # 丢帧策略的归属：默认"不丢帧"（画面完整优先）；但 AI 顾问在带宽不足时
        # 会显式给 :drop-late-frames（宁可丢帧也不要卡）——此时以顾问为准。
        附加 = list(self.附加选项 or [])
        顾问要丢帧 = any("drop-late-frames" in str(x) for x in 附加)
        # 丢帧策略：默认"不丢帧"（画面完整）；用户/AI 显式要丢帧、或这段片源
        # 本身吃力（允许丢帧=True）时不加 no-drop —— 让 VLC 按默认丢迟到帧，
        # A/V 不会越走越偏（4K60 实测的"稍显卡顿"多半来自这里）
        if not 顾问要丢帧 and not self.允许丢帧:
            选项 += [":no-drop-late-frames", ":no-skip-frames"]
        if self.视频输出:
            选项.append(f":vout={self.视频输出}")
        硬解 = str(self.硬解 or "auto")
        if 硬解 == "none":
            选项.append(":avcodec-hw=none")
        elif 硬解 in ("auto", ""):
            选项.append(":avcodec-hw=any")
        else:
            选项.append(f":avcodec-hw={硬解}")
        选项 += [x for x in 附加 if x]
        return 选项

    def to_dict(self) -> dict:
        return {
            "网络缓存毫秒": self.网络缓存毫秒, "起播等待秒": self.起播等待秒,
            "硬解": self.硬解, "附加选项": list(self.附加选项),
            "可流畅播放": self.可流畅播放, "理由": self.理由,
            "风险": self.风险, "来源": self.来源,
            "允许丢帧": bool(self.允许丢帧), "视频输出": self.视频输出,
        }


#: VAAPI 驱动文件名 → (适用厂商, 说明)。Mesa 会给 AMD/NVIDIA/virtio 带驱动，
#: 但 **Intel 的 iHD/i965 要单独装包**（intel-media-driver / libva-intel-driver）。
VA驱动表 = {
    "iHD_drv_video.so": ("Intel", "intel-media-driver"),
    "i965_drv_video.so": ("Intel", "libva-intel-driver"),
    "radeonsi_drv_video.so": ("AMD", "mesa（自带）"),
    "nouveau_drv_video.so": ("NVIDIA", "mesa（自带）"),
    "nvidia_drv_video.so": ("NVIDIA", "nvidia-utils"),
    "virtio_gpu_drv_video.so": ("虚拟显卡", "mesa（自带）"),
}

#: 可能的 VA 驱动目录（发行版差异）
_驱动目录们 = ("/usr/lib/dri", "/usr/lib64/dri", "/usr/lib/x86_64-linux-gnu/dri",
           "/usr/lib/i386-linux-gnu/dri")

_厂商名 = {"0x8086": "Intel", "0x1002": "AMD", "0x1022": "AMD",
         "0x10de": "NVIDIA", "0x1af4": "虚拟显卡", "0x1b36": "虚拟显卡"}


def _显卡厂商() -> str:
    """从 /sys 读显卡厂商（Intel/AMD/NVIDIA/…）；读不到返回空串。"""
    try:
        for 卡 in sorted(Path("/sys/class/drm").glob("card[0-9]*")):
            文件 = 卡 / "device" / "vendor"
            if 文件.is_file():
                原始 = 文件.read_text(encoding="utf-8", errors="replace").strip()
                if 原始.lower() in _厂商名:
                    return _厂商名[原始.lower()]
    except Exception:
        pass
    return ""


def _找VA驱动(厂商: str = "") -> tuple[str, str]:
    """找**与本机显卡匹配**的 VAAPI 驱动文件；返回 (文件名, 完整路径)。

    必须按厂商配对：Mesa 一般会装好 radeonsi/nouveau 的 VA 驱动，在 Intel 机器
    上直接"找到第一个就用"会拿到 AMD 的驱动 —— 那是假的可用（实测踩过）。
    厂商读不到时才退化为"任意驱动"，并在说明里讲清楚。
    """
    for 目录 in _驱动目录们:
        根 = Path(目录)
        if not 根.is_dir():
            continue
        for 名, (适用厂商, _包) in VA驱动表.items():
            if 厂商 and 适用厂商 != 厂商:
                continue
            if (根 / 名).is_file():
                return 名, str(根 / 名)
    if 厂商:
        return "", ""
    # 厂商未知：任意驱动兜一下，但调用方会在说明里标注"厂商未识别"
    for 目录 in _驱动目录们:
        根 = Path(目录)
        if not 根.is_dir():
            continue
        for 名 in VA驱动表:
            if (根 / 名).is_file():
                return 名, str(根 / 名)
    return "", ""


def 硬解能力() -> dict:
    """核实本机**真的能用**哪些硬解，并给出人话说明。

    为什么要"核实"：只看 ``/dev/dri`` 存在就宣称用 vaapi，是**过度承诺** ——
    实测这台机器 ``/dev/dri/renderD128`` 在，但 Intel 的 ``iHD/i965`` VA 驱动
    没装，VLC 初始化 vaapi 直接失败（``libva error: vaGetDriverNames() failed``）
    然后悄悄回落到软解。界面却显示"硬解 vaapi"，用户以为开了硬解。
    现在按**显卡厂商 + 驱动文件**一起判，并给出该装什么包。

    返回 ``{"可用": [...], "VA驱动": str, "厂商": str, "渲染节点": bool,
    "说明": str}``；``可用`` 里始终带 ``auto``/``none`` 兜底。
    """
    厂商 = _显卡厂商()
    驱动名, 驱动路径 = _找VA驱动(厂商)
    渲染节点 = False
    try:
        渲染节点 = any(Path("/dev/dri").glob("renderD*"))
    except Exception:
        pass
    可用 = ["auto", "none"]
    说明 = ""
    if 驱动名 and 渲染节点:
        适用厂商, 包名 = VA驱动表[驱动名]
        可用.insert(0, "vaapi")
        匹配 = "（厂商未识别，按驱动试）" if not 厂商 else ""
        说明 = (f"VAAPI 可用：{驱动名}（{适用厂商}{匹配}，{驱动路径}）"
              f" → 4K/8K 解码交给显卡")
    elif not 渲染节点:
        说明 = "没有 /dev/dri/renderD* 渲染节点：只能用 CPU 软解"
    elif not 驱动名:
        该装 = {"Intel": "intel-media-driver", "AMD": "mesa",
              "NVIDIA": "nvidia-utils"}.get(厂商, "")
        命令 = f"（Arch: sudo pacman -S {该装}）" if 该装 else ""
        说明 = (f"⚠️ 有显卡（{厂商 or '未知厂商'}）和渲染节点，但**没装 VAAPI 驱动**"
              f" → 只能 CPU 软解。想开硬解：装 {该装 or '对应的 VAAPI 驱动'}"
              f"{命令}；实测本机软解 4K H.264 约 48 fps / HEVC 约 43 fps"
              f"（够 25fps 片源，但 60fps 或 8K 会吃力）")
    return {"可用": 可用, "VA驱动": 驱动名, "厂商": 厂商,
            "渲染节点": 渲染节点, "说明": 说明}


def 规则参数(媒体: 媒体信息, 探测: 探测结果,
           本机硬解: list[str] | None = None) -> 播放设置:
    """**不依赖 AI** 的保底参数（AI 不可用时也必须有合理结果）。

    规则很朴素但有效：
      * 实测带宽 ≥ 码率 × 1.3 → 判"够"；否则判"不够"并把缓存拉满；
      * 缓存窗口按时长/码率给：4K 高码率给 8~15 秒，普通给 3~6 秒；
      * 硬解优先 vaapi（核实过驱动真的在），否则 auto 让 VLC 自己挑。
    """
    硬解们 = list(本机硬解 or ["auto"])
    码率 = max(1, int(媒体.视频码率bps or 0))
    带宽 = float(探测.实测带宽bps or 0)
    档位 = 媒体.档位 or "未知"
    够 = bool(带宽 and 码率 and 带宽 >= 码率 * 1.3)
    if 探测.来源说明 and not 带宽:
        # 合成的探测结果（本地文件）：没有网络瓶颈，直接按码率给参数
        够, 理由开头 = True, f"本地文件 {档位} {格式化码率(码率)}，直接解码"
    elif not 带宽:
        够, 理由开头 = True, "带宽未知（探测失败），按保守参数起播"
    elif 够:
        理由开头 = (f"{档位} {格式化码率(码率)}，实测 {格式化带宽(带宽)}，"
                  f"余量 {带宽 / 码率:.1f}×")
    else:
        理由开头 = (f"{档位} {格式化码率(码率)} 超过实测 "
                  f"{格式化带宽(带宽)}（需 ≥{码率 * 1.3 / 1e6:.1f} Mbps）")
    # 本地文件没有网络等待，缓存给小值能立刻出画（:network-caching 对本地
    # file 访问本来也不生效，小值只是保险，且不必等缓冲）
    是本地 = bool(探测.来源说明.startswith("本地文件"))
    if 是本地:
        缓存 = 1500
    # 缓存窗口：高码率/带宽紧张 → 更大
    elif 码率 >= 20_000_000 or 档位 == "4K":
        缓存 = 12000 if 够 else 20000
    elif 码率 >= 8_000_000:
        缓存 = 8000 if 够 else 14000
    else:
        缓存 = 4000 if 够 else 9000
    if not 够 and not 是本地:
        缓存 = max(缓存, 15000)
    #: 带宽不足时的缓存下限：任何后续建议（AI/诊断）都不许低于这个值
    缓存下限 = 15000 if (not 够 and not 是本地) else 0
    硬解 = "none"
    for 候选 in ("vaapi", "nvdec", "cuda", "auto"):
        if 候选 in 硬解们:
            硬解 = 候选 if 候选 != "auto" else "auto"
            break
    # ---- 吃力片源的流畅度策略（4K / 高帧率 / 高码率）----
    帧率 = float(getattr(媒体, "帧率", 0.0) or 0.0)
    吃力 = (档位 == "4K" or 码率 >= 20_000_000 or 帧率 >= 45.0)
    允许丢帧 = bool(吃力)
    # 4K 缩到小屏幕时，用 GPU 做缩放/色彩转换（vout=gl）比 CPU 路径省得多；
    # 万一本机没有 GL，起播后会由 _GL兜底检查 自动退回默认 vout。
    视频输出 = "gl" if 吃力 else ""
    if 吃力:
        理由开头 += "；吃力片源：允许丢迟到帧 + 试 GPU 缩放(vout=gl)"
    return 播放设置(
        网络缓存毫秒=缓存,
        起播等待秒=round(min(8.0, 缓存 / 2500.0), 1),
        硬解=硬解,
        附加选项=[],
        允许丢帧=允许丢帧,
        视频输出=视频输出,
        可流畅播放=够,
        理由=理由开头 + f"；缓存 {缓存}ms，硬解 {硬解}",
        风险=("" if 够 else
            "实测带宽不足（余量 <1.3×），可能反复缓冲：缓存已加大到 "
            f"{缓存}ms；仍卡的话建议换低码率版本、避开高峰，"
            "或先在网盘里转存/下载后再看"),
        来源="规则",
    )


# --------------------------------------------------------------------------
# 播放会话
# --------------------------------------------------------------------------


class 播放会话:
    """一次视频播放的会话（可被界面反复调用：准备 → 起播 → 控制 → 关闭）。"""

    def __init__(self, 取适配器: Callable[[str], object] | None = None,
                 日志回调: Callable | None = None, *, 顾问=None,
                 探测直链开关: bool = True, 探测媒体开关: bool = True,
                 自动调优: bool = True, 本地模型=None, 下载分段: int = 0,
                AI决策后台: bool = True):
        self._取适配器 = 取适配器
        self._日志 = 日志回调 or (lambda *_: None)
        self.顾问 = 顾问
        self.探测直链开关 = bool(探测直链开关)
        self.探测媒体开关 = bool(探测媒体开关)
        self.自动调优 = bool(自动调优)
        self._本地模型 = 本地模型
        #: True = 先用**规则参数秒起播**，AI 顾问在后台算；算完若参数更好就自动
        #: 重载到当前位置。为什么默认开：本地 1.5B 模型一次决策要 15~24 秒，
        #: 让用户干等 20 秒才出画面是不能接受的；而 libvlc 的缓存/硬解选项**只能
        #: 起播前生效**，所以"先起播 + 后重载"是唯一既快又能用上 AI 的路子。
        self.AI决策后台 = bool(AI决策后台)
        self.AI决策状态 = ""          # ""：没跑 / 跑着 / 完成说明
        self._子线程: list = []       # 后台线程（AI 决策/跳转），关闭时只做记录

        self.播放器: Optional[VLC] = None
        self.设置 = 播放设置()
        self.媒体 = 媒体信息()
        self.探测 = 探测结果()
        self.直链信息: dict = {}
        self.网盘标识 = ""
        self.远端路径 = ""
        self.标题 = ""
        self.开始时间 = 0.0
        self.上次统计: dict = {}
        self.调优记录: list[dict] = []
        self.累计缓冲次数 = 0
        self._上次是否缓冲 = False
        self.卡顿次数 = 0
        self.平均码率累计 = 0.0
        self.采样次数 = 0
        self._锁 = threading.RLock()

    # ---------------- ① 准备 ----------------

    def 准备(self, 网盘标识: str, 远端路径: str) -> dict:
        """取直链 + 探测 + 媒体信息 + 参数决策；返回全过程摘要（界面直接显示）。"""
        摘要: dict = {"网盘": 网盘标识, "路径": 远端路径}
        if self._取适配器 is None:
            raise RuntimeError("播放会话没有拿到适配器入口")
        适配器 = self._取适配器(网盘标识)
        if 适配器 is None:
            raise RuntimeError(f"网盘不可用：{网盘标识}")
        开始 = time.time()
        信息 = dict(适配器.取播放直链(远端路径) or {})
        self.直链信息 = 信息
        self.网盘标识 = 网盘标识
        self.远端路径 = 远端路径
        self.标题 = str(信息.get("name") or 远端路径.rsplit("/", 1)[-1])
        地址 = str(信息.get("url") or "")
        if not 地址:
            raise RuntimeError("没有拿到播放直链")
        请求头 = dict(信息.get("headers") or {})
        摘要["直链"] = 地址
        摘要["请求头"] = 请求头
        摘要["大小"] = int(信息.get("size") or 0)
        摘要["取直链秒"] = round(time.time() - 开始, 2)
        self._日志(f"[播放] 直链就绪：{self.标题}"
                 f"（{self._日志大小(摘要['大小'])}，{摘要['取直链秒']}s）")

        # 媒体信息（分辨率/码率/字幕轨）——失败也能播，只是参数保守
        if self.探测媒体开关:
            self.媒体 = 探测媒体(地址, 请求头, 超时秒=25.0)
            摘要["媒体信息"] = self.媒体.to_dict()
            摘要["媒体摘要"] = self.媒体.摘要()
            self._日志(f"[播放] 媒体信息：{self.媒体.摘要()}")
        # 实测带宽（只对 http(s) 直链有意义；本地文件直接跳过）
        是网络地址 = 地址.startswith(("http://", "https://"))
        if not 是网络地址:
            # 本地文件：合成一条说明，界面/理由里就不会再出现"探测失败"
            self.探测 = 探测结果(
                成功=True, 实测带宽bps=0.0, range支持=True,
                来源说明=f"本地文件（{self._日志大小(摘要['大小'])}，"
                       f"不经过网络，无需测带宽）")
            self.探测.内容长度 = int(摘要.get("大小") or 0)
            摘要["探测"] = self.探测.to_dict()
            摘要["探测摘要"] = self.探测.摘要()
        elif self.探测直链开关:
            self.探测 = 探测直链(地址, 请求头, 日志回调=self._日志)
            头 = 探测头(地址, 请求头)
            if 头.get("range支持"):
                self.探测.range支持 = True
            if 头.get("内容长度"):
                self.探测.内容长度 = int(头["内容长度"])
            摘要["探测"] = self.探测.to_dict()
            摘要["探测摘要"] = self.探测.摘要()
            self._日志(f"[播放] {self.探测.摘要()}")
        # 参数决策：规则**立刻**给一套（保证秒起播），AI 顾问在后台接着算
        self.设置 = 规则参数(self.媒体, self.探测, self._探测本机硬解())
        摘要["参数"] = self.设置.to_dict()
        self._日志(f"[播放] 起播参数（{self.设置.来源}）："
                 f"缓存 {self.设置.网络缓存毫秒}ms · 硬解 {self.设置.硬解}"
                 f"　{self.设置.理由}")
        if self.顾问 is not None and self.AI决策后台:
            self.AI决策状态 = "AI 正在后台决策（不影响起播）"
            摘要["AI决策"] = self.AI决策状态
            线程 = threading.Thread(target=self._后台问顾问, args=(摘要,),
                                 name="播放AI决策", daemon=True)
            线程.start()
            self._子线程.append(线程)
        elif self.顾问 is not None:
            self.设置 = self._决策参数(摘要)
            摘要["参数"] = self.设置.to_dict()
        return 摘要

    # ---------------- 适配器访问 ----------------

    def 取适配器(self, 网盘标识: str = ""):
        """拿适配器实例（界面/字幕查找要用）。

        为什么要有公开方法：内部字段是 ``_取适配器``，而界面在"网盘同目录找字幕"
        时按直觉写了 ``会话.取适配器(标识)`` —— 结果 AttributeError 被后台线程
        的 except 吞掉，用户只看到"翻译失败"。现在把这个名字补成真接口。
        """
        if self._取适配器 is None:
            return None
        return self._取适配器(网盘标识 or self.网盘标识)

    # ---------------- ①' 后台 AI 决策 ----------------
    def _后台问顾问(self, 摘要: dict) -> None:
        """后台问 AI 顾问要参数；比规则更好就**自动重载**到当前位置。

        只在真的有变化时重载（缓存/硬解/附加选项任一不同），避免白重启一次。
        """
        是本地 = bool(self.探测.来源说明.startswith("本地文件"))
        if 是本地:
            # 本地文件不经过网络：AI 拿不到带宽，只会给"保守大缓存"，对本地是
            # **负优化**（白重启一次还丢掉已缓冲的数据）。实测本地 4K 被建议把
            # 缓存从 1500ms 改成 12000ms。
            self.AI决策状态 = "本地文件不经过网络，跳过 AI 调参（沿用规则参数）"
            self._日志(f"[播放] {self.AI决策状态}")
            return
        try:
            硬解 = self._探测本机硬解()
            探测入参 = {
                "网盘": self.网盘标识, "文件名": self.标题,
                "大小字节": int(摘要.get("大小") or 0),
                **self.媒体.to_dict(), **self.探测.to_dict(),
                "本机硬解": 硬解,
                "缓存目录剩余字节": self._缓存剩余(),
            }
            建议 = dict(self.顾问.建议起播参数(探测入参) or {})
        except Exception as e:  # noqa: BLE001
            self.AI决策状态 = f"AI 决策失败，继续用规则：{type(e).__name__}: {e}"
            self._日志(f"[播放] {self.AI决策状态}")
            return
        if not 建议:
            self.AI决策状态 = "AI 没给建议，继续用规则参数"
            self._日志(f"[播放] {self.AI决策状态}")
            return
        旧 = self.设置
        新 = 播放设置(
            网络缓存毫秒=int(建议.get("网络缓存毫秒") or 旧.网络缓存毫秒),
            起播等待秒=float(建议.get("起播等待秒")
                        if 建议.get("起播等待秒") is not None
                        else 旧.起播等待秒),
            硬解=str(建议.get("硬解") or 旧.硬解),
            附加选项=list(建议.get("附加选项") or []),
            可流畅播放=bool(建议.get("可流畅播放", 旧.可流畅播放)),
            理由=str(建议.get("理由") or 旧.理由),
            风险=str(建议.get("风险") or 旧.风险),
            来源=str(建议.get("来源") or "AI"),
        )
        变了 = (abs(新.网络缓存毫秒 - 旧.网络缓存毫秒) >= 1000
              or 新.硬解 != 旧.硬解
              or sorted(新.附加选项) != sorted(旧.附加选项))
        if not 变了:
            self.AI决策状态 = f"AI 复核后维持原参数（{新.来源}）"
            self._日志(f"[播放] {self.AI决策状态}")
            return
        self.AI决策状态 = (f"AI 建议改参数（{新.来源}）：缓存 "
                      f"{旧.网络缓存毫秒}→{新.网络缓存毫秒}ms，硬解 "
                      f"{旧.硬解}→{新.硬解}")
        self._日志(f"[播放] {self.AI决策状态}；自动重载到当前位置")
        self.应用新参数(新.to_dict(), 自动重载=True)

    def _决策参数(self, 摘要: dict) -> 播放设置:
        硬解 = self._探测本机硬解()
        规则 = 规则参数(self.媒体, self.探测, 硬解)
        if self.顾问 is None:
            return 规则
        try:
            探测入参 = {
                "网盘": self.网盘标识, "文件名": self.标题,
                "大小字节": int(摘要.get("大小") or 0),
                **self.媒体.to_dict(), **self.探测.to_dict(),
                "本机硬解": 硬解,
                "缓存目录剩余字节": self._缓存剩余(),
            }
            建议 = dict(self.顾问.建议起播参数(探测入参) or {})
        except Exception as e:  # noqa: BLE001
            self._日志(f"[播放] AI 顾问不可用，走规则：{type(e).__name__}: {e}")
            return 规则
        if not 建议:
            return 规则
        try:
            return 播放设置(
                网络缓存毫秒=int(建议.get("网络缓存毫秒")
                            or 规则.网络缓存毫秒),
                起播等待秒=float(建议.get("起播等待秒")
                            if 建议.get("起播等待秒") is not None
                            else 规则.起播等待秒),
                硬解=str(建议.get("硬解") or 规则.硬解),
                附加选项=list(建议.get("附加选项") or []),
                可流畅播放=bool(建议.get("可流畅播放", 规则.可流畅播放)),
                理由=str(建议.get("理由") or 规则.理由),
                风险=str(建议.get("风险") or 规则.风险),
                来源=str(建议.get("来源") or "AI"),
            )
        except Exception as e:  # noqa: BLE001
            self._日志(f"[播放] AI 参数不合法，走规则：{e}")
            return 规则

    @staticmethod
    def _探测本机硬解() -> list[str]:
        """本机能用哪些硬解（**核实过驱动真的在**，不只看 /dev/dri）。"""
        return 硬解能力()["可用"]

    def _缓存剩余(self) -> int:
        try:
            import shutil as _sh
            return int(_sh.disk_usage(str(Path.home())).free)
        except Exception:
            return 0

    # ---------------- ② 起播 ----------------

    def 起播(self, 窗口句柄: int = 0) -> bool:
        if not vlc可用():
            raise RuntimeError(不可用原因())
        地址 = str(self.直链信息.get("url") or "")
        if not 地址:
            raise RuntimeError("还没准备直链（先调用 准备()）")
        if self.播放器 is None:
            self.播放器 = VLC(窗口句柄=窗口句柄, 日志回调=self._日志)
        else:
            self.播放器.绑定窗口(窗口句柄)
        选项 = self.设置.libvlc选项()
        self._日志(f"[播放] 起播（缓存 {self.设置.网络缓存毫秒}ms，"
                 f"硬解 {self.设置.硬解}）：{len(选项)} 个选项")
        # 硬解到底能不能用，必须如实告诉用户（不然界面显示 vaapi、实际在软解）
        if not getattr(self, "_已报硬解", False):
            self._已报硬解 = True
            说明 = 硬解能力()["说明"]
            if 说明:
                self._日志(f"[播放] {说明}")
        成功 = self.播放器.播放(地址, 选项,
                           dict(self.直链信息.get("headers") or {}))
        if 成功:
            self.开始时间 = time.time()
            self.播放器.设置音量(max(0, self.播放器.取音量() or 100))
            if str(self.设置.视频输出 or "") == "gl":
                threading.Thread(target=self._GL兜底检查, name="GL兜底",
                                 daemon=True).start()
        return 成功

    def _GL兜底检查(self) -> None:
        """用了 ``:vout=gl`` 却在几秒内没有画面 → 换回默认 vout 重载。

        为什么要兜底：GL/EGL 不是每台机器都能起来（驱动、XWayland、远程桌面
        都可能失败）。失败时如果不管，用户看到的就是"有声没画"。
        """
        for _ in range(12):                 # 最多等 ~3 秒
            time.sleep(0.25)
            try:
                if self.播放器 is None:
                    return
                if self.播放器.有画面():
                    return
                if self.播放器.时长秒() > 0 and self.播放器.进度秒() > 2.0:
                    break                       # 已经在出帧但 has_vout 报假，别误判
            except Exception:  # noqa: BLE001
                return
        位置 = 0.0
        try:
            位置 = float(self.播放器.进度秒()) if self.播放器 else 0.0
        except Exception:  # noqa: BLE001
            位置 = 0.0
        self._日志("[播放] ⚠️ vout=gl 起播后一直没有画面，回退默认视频输出重载")
        self.应用新参数({"视频输出": "", "理由": "vout=gl 无画面，已回退默认 vout",
                    "来源": "规则"}, 自动重载=True)

    # ---------------- ③ 控制 ----------------

    def 暂停(self) -> None:
        if self.播放器:
            self.播放器.暂停()

    def 设置暂停(self, 暂停: bool) -> None:
        if self.播放器:
            self.播放器.设置暂停(暂停)

    def 跳转(self, 秒: float) -> None:
        if self.播放器:
            self.播放器.跳转(秒)

    def 设置音量(self, 值: int) -> None:
        if self.播放器:
            self.播放器.设置音量(值)

    def 设置速率(self, 倍速: float) -> None:
        if self.播放器:
            self.播放器.设置速率(倍速)

    def 挂字幕(self, 路径: str, 选中: bool = True) -> bool:
        if not self.播放器:
            return False
        成功 = self.播放器.挂字幕文件(路径, 选中)
        self._日志(f"[播放] 挂字幕 {'成功' if 成功 else '失败'}：{路径}")
        return 成功

    def 切换字幕(self) -> int:
        return self.播放器.切换字幕编号() if self.播放器 else -1

    def 截图(self, 保存路径: str) -> bool:
        return bool(self.播放器 and self.播放器.截图(保存路径))

    # ---------------- ④ 状态与调优 ----------------

    def 状态快照(self) -> dict:
        if self.播放器 is None:
            return {"状态": "未开始", "状态码": 播放状态.无, "进度秒": 0.0,
                    "时长秒": 0.0, "缓冲中": False, "丢帧": 0,
                    "输入码率bps": 0.0, "有画面": False}
        状态码 = self.播放器.状态
        统计 = self.播放器.统计()
        上次 = self.上次统计
        丢帧增量 = max(0, 统计.丢帧 - int(上次.get("丢帧") or 0))
        self.上次统计 = 统计.to_dict()
        return {
            "状态": 播放状态.名称(状态码), "状态码": 状态码,
            "进度秒": self.播放器.进度秒(),
            "时长秒": self.播放器.时长秒(),
            "比例": (self.播放器.进度秒() / self.播放器.时长秒()
                   if self.播放器.时长秒() else 0.0),
            "缓冲中": 状态码 == 播放状态.缓冲中,
            "有画面": self.播放器.有画面(),
            "丢帧": 统计.丢帧, "丢帧增量": 丢帧增量,
            "输入码率bps": 统计.输入码率bps,
            "解复码率bps": 统计.解复码率bps,
            "读字节": 统计.读字节,
            # 解码/显示帧数：判断"到底有没有真的在解码"（4K 跟不跟得上靠它）
            "已解码视频": 统计.已解码视频, "已显示帧": 统计.已显示帧,
            "帧率": self.播放器.帧率(),
            # VLC 在没有音轨/音频输出没起来时会返回 -1，显示成 -1 很怪 → 夹到 0
            "音量": max(0, int(self.播放器.取音量() or 0)),
            "倍速": self.播放器.取速率(),
            "字幕": self.播放器.当前字幕(),
            "字幕轨": [{"编号": t.编号, "名称": t.名称}
                    for t in self.播放器.字幕轨()],
            "音频轨": [{"编号": t.编号, "名称": t.名称}
                    for t in self.播放器.音频轨()],
            "已播秒": max(0.0, time.time() - self.开始时间) if self.开始时间 else 0.0,
            "参数": self.设置.to_dict(),
        }

    def 流畅优先(self) -> 播放设置:
        """"流畅优先"预设：允许丢帧 + GPU 缩放 + 缓存加大（吃力片源用）。"""
        旧 = self.设置
        return 播放设置(
            网络缓存毫秒=max(int(旧.网络缓存毫秒 or 0), self.缓存下限() or 0,
                       15000),
            起播等待秒=旧.起播等待秒,
            硬解=旧.硬解,
            附加选项=list(旧.附加选项 or []),
            可流畅播放=True,
            理由="流畅优先：允许丢迟到帧 + vout=gl + 缓存加大",
            风险="允许丢帧时画面偶尔会少一帧（换来不卡）",
            来源="规则",
            允许丢帧=True,
            视频输出="gl",
        )

    def 规则诊断(self) -> Optional[dict]:
        """AI 顾问没给建议时的规则兜底：明显在掉帧/缓冲就建议"流畅优先"。

        返回与顾问同构的 ``{需要调整, 理由, 动作, 新参数}``。
        """
        快照 = self.状态快照()
        丢帧增量 = int(快照.get("丢帧增量") or 0)
        缓冲中 = bool(快照.get("缓冲中"))
        if not 丢帧增量 and not 缓冲中:
            return None
        当前 = self.设置
        限 = self.缓存下限()
        缓存 = max(int(当前.网络缓存毫秒 or 0), 限 or 0, 15000)
        需要 = (丢帧增量 >= 3) or 缓冲中
        if not 需要:
            return None
        新 = self.流畅优先()
        新.网络缓存毫秒 = 缓存
        动作 = []
        if not 当前.允许丢帧:
            动作.append("允许丢迟到帧（不再越积越晚）")
        if str(当前.视频输出 or "") != "gl":
            动作.append("改用 GPU 缩放：vout=gl")
        if 缓存 > int(当前.网络缓存毫秒 or 0):
            动作.append(f"缓存 {当前.网络缓存毫秒}→{缓存}ms（实测带宽余量不足）")
        if not 动作:
            return None
        return {"需要调整": True,
                "理由": f"丢帧 +{丢帧增量}" + ("、正在缓冲" if 缓冲中 else ""),
                "动作": 动作, "新参数": 新.to_dict(), "来源": "规则"}

    def 采样并诊断(self) -> Optional[dict]:
        """播放中调用（界面定时器每 2 秒一次）：卡顿就返回诊断建议。

        返回形如 ``{"需要调整": bool, "动作": [...], "新参数": {...},
        "理由": ..., "来源": ...}``；不需要调整时返回 None。
        """
        if self.播放器 is None or not self.自动调优:
            return None
        快照 = self.状态快照()
        if 快照["状态码"] not in (播放状态.播放中, 播放状态.缓冲中):
            return None
        # 累计缓冲次数：从"不在缓冲"进入"缓冲中"才 +1（顾问用它判断抖不抖）
        现在缓冲 = bool(快照.get("缓冲中"))
        if 现在缓冲 and not self._上次是否缓冲:
            self.累计缓冲次数 += 1
        self._上次是否缓冲 = 现在缓冲
        self.采样次数 += 1
        码率 = float(快照.get("输入码率bps") or 0) * 1_000_000
        if 码率 > 0:
            self.平均码率累计 += 码率
        采样 = {
            "丢帧": 快照.get("丢帧增量", 0),
            "缓冲等待次数": self.累计缓冲次数,
            "状态": 快照.get("状态"),
            "已播秒": 快照.get("已播秒", 0.0),
            "当前缓冲秒": self.设置.网络缓存毫秒 / 1000.0,
            "平均码率bps": 快照.get("输入码率bps") or self.媒体.视频码率bps,
            "硬解生效": str(self.设置.硬解).lower() not in ("none", ""),
            "CPU占用": 0.0,
            "状态": 快照.get("状态"),
        }
        建议 = None
        if self.顾问 is not None:
            try:
                建议 = self.顾问.诊断卡顿(采样, self.设置.to_dict(),
                                      self.媒体.to_dict())
            except Exception as e:  # noqa: BLE001
                self._日志(f"[播放] AI 诊断异常：{type(e).__name__}: {e}")
        if not 建议:
            # 规则兜底：缓冲中且缓存没拉满 → 加大缓存
            if 快照.get("缓冲中") and self.设置.网络缓存毫秒 < 20000:
                新 = 播放设置(**{**self.设置.to_dict(),
                              "网络缓存毫秒": min(
                                  20000, int(self.设置.网络缓存毫秒 * 1.6)),
                              "理由": "反复缓冲，缓存窗口加大 60%",
                              "来源": "规则"})
                建议 = {"需要调整": True, "动作": [
                    f"网络缓存 {self.设置.网络缓存毫秒}→{新.网络缓存毫秒}ms"],
                    "新参数": 新.to_dict(), "理由": 新.理由, "来源": "规则",
                    "风险": self.设置.风险}
            elif 快照.get("丢帧增量", 0) > 30:
                建议 = {"需要调整": False, "动作": [], "新参数": {},
                      "理由": f"丢帧 {快照['丢帧增量']} 帧但未在缓冲，"
                            f"多半是解码跟不上（可试软解/降码率版本）",
                      "来源": "规则", "风险": "继续看可能花屏"}
        if 建议 and 建议.get("需要调整"):
            self.调优记录.append({"时间": time.time(), **建议})
            self.卡顿次数 += 1
            self.上报效果(卡顿=True)
            return 建议
        if 建议:
            return 建议
        return None

    def 学习键(self) -> str:
        """学习库的键：网盘|分辨率档[|编码]（由顾问统一生成，避免两边写法不一致）。"""
        try:
            生成 = getattr(self.顾问, "生成学习键", None)
            if callable(生成):
                return str(生成(self.网盘标识 or "本地",
                              self.媒体.档位 or self.媒体.分辨率 or "未知",
                              self.媒体.视频编码 or ""))
        except Exception:
            pass
        return f"{self.网盘标识 or '本地'}|{self.媒体.档位 or '未知'}"

    def 上报效果(self, 卡顿: bool = False) -> None:
        """把这次播放的"参数 → 效果"写进学习库（下次同键优先复用）。

        顾问的规则是"样本≥2 且卡顿率≤1/3 才复用"，所以每次播放都要上报，
        否则学习库永远是空的（这是顾问反复提醒的集成缺口）。
        """
        if self.顾问 is None:
            return
        记录 = getattr(self.顾问, "记录效果", None)
        if not callable(记录):
            return
        try:
            平均码率 = (self.平均码率累计 / self.采样次数
                     if self.采样次数 else float(self.媒体.视频码率bps or 0))
            记录(self.学习键(), self.设置.to_dict(), {
                "是否卡顿": bool(卡顿 or self.卡顿次数 > 0),
                "平均码率bps": int(平均码率),
                "丢帧": int((self.播放器.统计().丢帧 if self.播放器 else 0)),
                "缓冲等待次数": int(self.累计缓冲次数),
                "已播秒": max(0.0, time.time() - self.开始时间)
                       if self.开始时间 else 0.0,
            })
        except Exception as e:  # noqa: BLE001
            self._日志(f"[播放] 上报学习库失败（不影响播放）：{e}")

    def 缓存下限(self) -> int:
        """实测带宽不足时的缓存下限（0 = 带宽够，不限制）。

        用户实测到的问题：带宽 13.0 Mbps、码率 12.7 Mbps（余量仅 1.02×，
        远低于 1.3× 的判定线），AI 却把缓存从 12000ms **压到 6000ms** ——
        结果就是反复缓冲、观感卡顿。所以带宽不够时缓存只许加大。
        """
        try:
            码率 = max(1, int(self.媒体.视频码率bps or 0))
            带宽 = float(self.探测.实测带宽bps or 0)
        except Exception:  # noqa: BLE001
            return 0
        if 带宽 and 码率 and 带宽 < 码率 * 1.3:
            return 15000
        return 0

    def 应用新参数(self, 新参数: dict, 自动重载: bool = True) -> bool:
        """把诊断建议落到播放上：**带新参数从当前位置重开**（libvlc 的选项
        只能在起播前生效，所以重开是唯一的真优化手段）。"""
        if not 新参数:
            return False
        新参数 = dict(新参数)
        下限 = self.缓存下限()
        if 下限:
            原 = int(新参数.get("网络缓存毫秒") or 0)
            if 原 and 原 < 下限:
                self._日志(f"[播放] 实测带宽不足（余量 <1.3×），"
                         f"忽略把缓存降到 {原}ms 的建议，保持 ≥{下限}ms")
                新参数["网络缓存毫秒"] = 下限
        位置 = self.播放器.进度秒() if self.播放器 else 0.0
        旧 = self.设置.to_dict()
        合并 = {**旧, **{k: v for k, v in 新参数.items() if v not in (None, "")}}
        try:
            self.设置 = 播放设置(
                网络缓存毫秒=int(合并.get("网络缓存毫秒") or 8000),
                起播等待秒=float(合并.get("起播等待秒") or 1.0),
                硬解=str(合并.get("硬解") or "auto"),
                附加选项=list(合并.get("附加选项") or []),
                可流畅播放=bool(合并.get("可流畅播放", True)),
                理由=str(合并.get("理由") or "调优后重载"),
                风险=str(合并.get("风险") or ""),
                来源=str(合并.get("来源") or "AI"),
                允许丢帧=bool(合并.get("允许丢帧", False)),
                视频输出=str(合并.get("视频输出") or ""),
            )
        except Exception as e:  # noqa: BLE001
            self._日志(f"[播放] 新参数不合法，忽略：{e}")
            return False
        if not 自动重载:
            return True
        句柄 = self.播放器.窗口句柄 if self.播放器 else 0
        self._日志(f"[播放] 应用新参数并从 {位置:.1f}s 重开"
                 f"（缓存 {self.设置.网络缓存毫秒}ms，硬解 {self.设置.硬解}）")
        if not self.起播(句柄):
            return False
        # 等一小会儿再跳回原位置（刚起播时 seek 可能被忽略）
        def _跳():
            for _ in range(20):
                time.sleep(0.25)
                try:
                    if self.播放器 and self.播放器.时长秒() > 0:
                        self.播放器.跳转(位置)
                        if self.播放器.是否静音() is False:
                            pass
                        return
                except Exception:
                    return
        threading.Thread(target=_跳, daemon=True).start()
        return True

    # ---------------- 收尾 ----------------

    def 关闭(self) -> None:
        # 收尾上报：把这次播放的参数与效果写进学习库（下次这键就能直接复用）
        try:
            if self.播放器 is not None and self.开始时间:
                self.上报效果(卡顿=self.卡顿次数 > 0)
        except Exception:
            pass
        try:
            if self.播放器:
                self.播放器.关闭()
        except Exception:
            pass
        self.播放器 = None

    @staticmethod
    def _日志大小(字节) -> str:
        try:
            值 = float(字节 or 0)
        except Exception:
            return "未知"
        for 单位 in ("B", "KiB", "MiB", "GiB", "TiB"):
            if 值 < 1024 or 单位 == "TiB":
                return f"{值:.0f} {单位}" if 单位 == "B" else f"{值:.1f} {单位}"
            值 /= 1024
        return f"{值:.1f} TiB"
