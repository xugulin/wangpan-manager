# v8_3/AI/播放顾问.py
"""AI 播放顾问（V8_3 新增）——**AI 辅助加载** + **AI 优化播放**。

播放核心（libvlc 内嵌播放网盘直链）只问这一个模块两件事：

* :meth:`播放顾问.建议起播参数` —— 起播前：算网络缓存 / 预缓冲 / 硬解 / libvlc 选项；
* :meth:`播放顾问.诊断卡顿`     —— 播放中：看丢帧、缓冲等待、CPU，决定要不要换参数。

``诊断卡顿`` 的 ``采样`` 除约定字段外，还认两个"此刻正在缓冲"的信号
（``缓冲中`` 为真、``状态`` 里含"缓冲"）：累计的 ``缓冲等待次数`` 只有 1 次时
不值得动参数，但"现在就在缓冲"必须立刻处理 —— 真实播放核心正是这么采样的。

设计原则（每条都解释"为什么"）
==============================

1. **规则永远算得出来**：AI 只是"优化项"而不是"必需品"。没有本地模型、没有云端
   助手、断网、模型吐出一堆废话时，本模块依旧用"带宽 ÷ 码率"算出合理参数
   （``来源="规则"``），绝不抛异常、绝不返回缺键的 dict —— 播放不能被 AI 拖死。
2. **物理事实压过 AI 判断**：带宽 < 码率×1.3 时 ``可流畅播放`` 恒为 ``False``，
   AI 说 ``true`` 也翻不过来（见 :meth:`播放顾问._归一化参数`）；学习库里记的硬解
   方式如果本机已经不可用，也必须换掉（硬件换了，老经验就失效了）。
3. **先查学习库，再问 AI**：同一个"网盘 + 分辨率档"历史上参数表现好（样本≥2、
   卡顿率≤1/3）就直接复用，``来源="学习库"`` —— 比问模型更快、更稳、零成本，
   而且经验来自真实播放而不是模型的想象。
4. **两种 AI 来源，逐级兜底**：优先本地模型（免费离线）
   ``本地模型.对话(用户消息, 系统提示, 最大tokens=512)``，失败/没配就转云端
   ``AI助手.请求结构化策略(系统提示, 用户消息)``，再失败回规则。任何异常都被
   吞掉并写一行日志（``日志回调``），播放流程感知不到。
5. **纯逻辑、无 Qt**：只依赖标准库，可以被工作线程随便调；也**不 import
   ``v8_3.配置``**（那会把整个核心/传输引擎拖进来），默认库路径自己按
   ``项目根/数据/`` 算，需要改就传 ``学习库路径``。

用法::

    from v8_3.AI.播放顾问 import 播放顾问

    顾问 = 播放顾问(本地模型=本地模型客户端(), 日志回调=界面.追加日志)
    建议 = 顾问.建议起播参数(探测结果)            # 起播前
    ...
    诊断 = 顾问.诊断卡顿(采样, 建议, 媒体信息)     # 卡了
    顾问.记录效果("quark|2160p|hevc", 建议,
                  {"是否卡顿": False, "平均码率bps": 24_000_000})
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

__all__ = [
    "播放顾问", "分辨率档", "生成学习键", "默认学习库路径", "解析JSON对象",
    "判定卡顿",
]

# ==================== 可调常量（规则基线的"物理量"） ====================

#: libvlc ``--network-caching`` 的合理区间（毫秒）。低于 2 秒对网盘直链没意义，
#: 高于 20 秒会让换台/拖动后的等待长到用户以为卡死。
缓存下限毫秒 = 2000
缓存上限毫秒 = 20000

#: 判定"带宽够"的冗余系数：实测带宽至少要是码率的 1.3 倍，
#: 因为 TCP 重传、服务器抖动、同网段抢带宽都会吃掉瞬时余量。
带宽冗余系数 = 1.3

#: 学习库复用门槛：至少看过 2 次、卡顿率不超过 1/3 才敢照抄历史参数。
最少样本 = 2
卡顿率上限 = 0.34

#: 诊断阈值（都留成常量，方便按机器性能微调）。
CPU高阈值 = 0.7            # 70% 以上：反交错这类"白吃 CPU"的开关就该关掉
CPU极高阈值 = 0.9          # 90% 以上：软解基本到极限，只能降画质/关后台
缓冲低秒 = 2.0             # 当前缓冲不足 2 秒就算"随时会卡"
缓冲等待阈值 = 2           # 一次播放里缓冲等待 ≥2 次，说明网络供给不稳
丢帧阈值 = 10              # 丢帧绝对值门槛
丢帧速率阈值 = 6.0         # 每分钟丢帧数门槛（换算成速率才不受播放时长影响）
解码丢帧阈值 = 5           # 解码丢帧绝对值门槛

#: 硬解方式的优先级：**vaapi 优先**（Linux 上最常见、最稳），其余按经验排。
硬解优先级 = ("vaapi", "nvdec", "cuda", "d3d11va", "dxva2",
            "videotoolbox", "qsv", "vdpau", "amf")

#: ``建议起播参数`` / ``诊断卡顿.新参数`` 必须齐全的键（播放核心按这些键取值）。
参数键 = ("网络缓存毫秒", "起播等待秒", "硬解", "附加选项",
        "可流畅播放", "风险", "理由", "来源")

#: 学习库表结构：一行 = 一次"某键 + 某参数 + 某结果"，追加写，查询时聚合。
_建表语句 = """
CREATE TABLE IF NOT EXISTS 播放学习 (
    行号          INTEGER PRIMARY KEY AUTOINCREMENT,
    键            TEXT NOT NULL,
    网盘          TEXT DEFAULT '',
    分辨率档      TEXT DEFAULT '',
    参数JSON      TEXT NOT NULL,
    指标JSON      TEXT NOT NULL,
    是否卡顿      INTEGER DEFAULT 0,
    平均码率bps   REAL DEFAULT 0,
    缓冲等待次数  INTEGER DEFAULT 0,
    丢帧          INTEGER DEFAULT 0,
    已播秒        REAL DEFAULT 0,
    来源          TEXT DEFAULT '',
    创建时间      TEXT NOT NULL
)
"""


# ==================== 小工具（容错取值 + 时间 + JSON 解析） ====================


def _文本(值, 默认: str = "") -> str:
    """把任意值变成**去掉首尾空白的 str**；None/异常一律给默认值。"""
    if 值 is None:
        return 默认
    try:
        if isinstance(值, str):
            return 值.strip()
        return str(值).strip()
    except Exception:  # noqa: BLE001 - 打印一个奇怪对象失败也不该影响播放
        return 默认


def _浮点(值, 默认: float = 0.0) -> float:
    try:
        if isinstance(值, bool):
            return float(int(值))
        if 值 is None or (isinstance(值, str) and not 值.strip()):
            return float(默认)
        return float(值)
    except Exception:  # noqa: BLE001
        return float(默认)


def _可选浮点(值) -> Optional[float]:
    """缺省/不可解析 → ``None``（用来区分"真的是 0"和"没给"）。"""
    if 值 is None or (isinstance(值, str) and not 值.strip()):
        return None
    try:
        return float(值)
    except Exception:  # noqa: BLE001
        return None


def _整数(值, 默认: int = 0, 最小: int = None, 最大: int = None) -> int:
    try:
        if isinstance(值, bool):
            结果 = int(值)
        elif 值 is None or (isinstance(值, str) and not 值.strip()):
            结果 = int(默认)
        else:
            结果 = int(round(float(值)))
    except Exception:  # noqa: BLE001
        结果 = int(默认)
    if 最小 is not None:
        结果 = max(int(最小), 结果)
    if 最大 is not None:
        结果 = min(int(最大), 结果)
    return 结果


def _布尔(值, 默认: bool = False) -> bool:
    """认 ``true/false``、``是/否``、``1/0``、``卡/不卡`` 这些常见写法。"""
    if isinstance(值, bool):
        return 值
    if 值 is None:
        return 默认
    if isinstance(值, (int, float)):
        return bool(值)
    文本 = _文本(值).lower()
    if not 文本:
        return 默认
    if 文本 in ("true", "1", "yes", "y", "on", "是", "真", "卡", "卡顿", "需要"):
        return True
    if 文本 in ("false", "0", "no", "n", "off", "否", "假", "不卡", "流畅", "不需要"):
        return False
    return 默认


def _现在() -> str:
    """与 ``AI学习库`` 一致的可读时间戳（带毫秒，方便看清"哪次播放"）。"""
    dt = datetime.fromtimestamp(time.time())
    return (f"{dt.year}年{dt.month}月{dt.day}日 "
            f"{dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}."
            f"{dt.microsecond // 1000:03d}")


def _截断(文本: str, 上限: int = 80) -> str:
    文本 = _文本(文本).replace("\n", " ")
    return 文本 if len(文本) <= 上限 else 文本[:上限] + "…"


def 默认学习库路径() -> str:
    """默认 ``项目根/数据/AI播放学习.db``。

    刻意不 import ``v8_3.配置``：播放顾问会被播放线程频繁调用，而配置模块会把
    传输引擎等一大堆东西拖进来；这里只按文件位置推算项目根，零副作用。
    """
    return str(Path(__file__).resolve().parents[2] / "数据" / "AI播放学习.db")


def 分辨率档(分辨率) -> str:
    """把各种写法的分辨率归成学习库用的"档"。

    ``"3840x2160"`` / ``"2160p"`` / ``"4K"`` → ``"2160p"``；
    认不出来（空、``"未知"``）→ ``"未知"``。取**短边**做判断，
    因为 1920x1080 和 1080x1920 对解码压力的意义是一样的。
    """
    if isinstance(分辨率, bool) or 分辨率 is None:
        return "未知"
    if isinstance(分辨率, (int, float)):
        高 = int(分辨率)
    else:
        文本 = _文本(分辨率).lower().replace("×", "x").replace("*", "x").replace(" ", "")
        if not 文本:
            return "未知"
        高 = 0
        for 词, 值 in (("8k", 4320), ("4k", 2160), ("2k", 1440), ("1080p", 1080),
                      ("720p", 720), ("576p", 576), ("480p", 480), ("360p", 360)):
            if 词 in 文本:
                高 = 值
                break
        else:
            数字 = re.findall(r"\d+", 文本)
            if len(数字) >= 2:
                高 = min(int(数字[0]), int(数字[1]))
            elif len(数字) == 1:
                高 = int(数字[0])
    if 高 >= 4320:
        return "4320p"
    if 高 >= 2160:
        return "2160p"
    if 高 >= 1440:
        return "1440p"
    if 高 >= 1080:
        return "1080p"
    if 高 >= 720:
        return "720p"
    if 高 >= 480:
        return "480p"
    if 高 > 0:
        return "低清"
    return "未知"


def _编码别名(视频编码) -> str:
    """h265/x265 → hevc 之类统一别名，避免同一台机器学出两套键。"""
    文本 = _文本(视频编码).lower().replace("-", "").replace("_", "").replace(" ", "")
    if not 文本:
        return ""
    if 文本 in ("h265", "x265", "hevc", "hvc1", "hev1", "h.265"):
        return "hevc"
    if 文本 in ("h264", "x264", "avc", "avc1", "h.264"):
        return "h264"
    if 文本 in ("av01", "av1"):
        return "av1"
    if 文本 in ("vp09", "vp9"):
        return "vp9"
    return 文本


def 生成学习键(网盘="", 分辨率="", 视频编码="") -> str:
    """学习库的键：``网盘|分辨率档``（有编码就再加一段 ``|编码``）。

    为什么带编码：HEVC 4K 和 H.264 4K 的解码压力差一大截，硬解是否生效也不同。
    查库时会先试精确键，再退化成 ``网盘|分辨率档``（见 :meth:`播放顾问._候选键`），
    所以老记录不会因为新加了编码字段就全部作废。
    """
    网盘文本 = (_文本(网盘).lower() or "未知")
    档 = 分辨率档(分辨率)
    编码 = _编码别名(视频编码)
    return f"{网盘文本}|{档}|{编码}" if 编码 else f"{网盘文本}|{档}"


def _拆键(键: str) -> tuple[str, str]:
    """``"quark|2160p|hevc"`` → ``("quark", "2160p")``（统计按网盘/档分组用）。"""
    段 = [_文本(x) for x in _文本(键).split("|")]
    return (段[0] if 段 and 段[0] else "未知",
            段[1] if len(段) > 1 and 段[1] else "未知")


def _去代码块(文本: str) -> str:
    """剥掉 ```` ```json ... ``` ```` 包裹 —— 模型最常见的"多此一举"。"""
    匹配 = re.search(r"```(?:json|JSON)?\s*(.+?)```", 文本, re.S)
    return 匹配.group(1).strip() if 匹配 else ""


def _扫描对象(文本: str) -> list[str]:
    """按花括号配对扫出所有"看起来像 JSON 对象"的片段（字符串内的括号不算）。"""
    片段: list[str] = []
    深度 = 0
    起点 = -1
    在串里 = False
    转义 = False
    for 位置, 字符 in enumerate(文本):
        if 在串里:
            if 转义:
                转义 = False
            elif 字符 == "\\":
                转义 = True
            elif 字符 == '"':
                在串里 = False
            continue
        if 字符 == '"':
            在串里 = True
        elif 字符 in "{｛":
            if 深度 == 0:
                起点 = 位置
            深度 += 1
        elif 字符 in "}｝":
            if 深度 > 0:
                深度 -= 1
                if 深度 == 0 and 起点 >= 0:
                    片段.append(文本[起点:位置 + 1])
                    起点 = -1
    return 片段


def _尝试JSON(文本: str) -> Any:
    """尽量解析：先原样，再容忍尾逗号 —— 小模型很爱在最后一个字段后加逗号。"""
    for 候选 in (文本, re.sub(r",\s*([}\]])", r"\1", 文本)):
        try:
            return json.loads(候选)
        except Exception:  # noqa: BLE001
            continue
    return None


def _未闭合(文本: str) -> Optional[list[str]]:
    """扫一遍，返回"把这段被截断的 JSON 补全"还需要追加的字符（顺序即追加顺序）。

    没遇到 ``{`` 或括号不匹配（不是"截断"而是"写坏了"）→ 返回 ``None``。
    """
    栈: list[str] = []
    在串里 = False
    转义 = False
    见过对象 = False
    for 字符 in 文本:
        if 在串里:
            if 转义:
                转义 = False
            elif 字符 == "\\":
                转义 = True
            elif 字符 == '"':
                在串里 = False
            continue
        if 字符 == '"':
            在串里 = True
        elif 字符 in "{｛":
            栈.append("}")
            见过对象 = True
        elif 字符 == "[":
            栈.append("]")
        elif 字符 in "}｝]":
            if not 栈:
                return None
            期望 = 栈.pop()
            if (期望 == "}" and 字符 == "]") or (期望 == "]" and 字符 in "}｝"):
                return None
    if not 见过对象:
        return None
    补齐: list[str] = []
    if 在串里:
        补齐.append('"')          # 字符串被截断：先把引号闭上
    补齐.extend(reversed(栈))
    return 补齐


def _修补截断JSON(文本: str) -> Optional[dict]:
    """救回"被 token 上限截断"的 JSON 对象。

    为什么需要：本地小模型（deepseek-r1:1.5b 这类）在 512 tokens 处被硬截断是
    常态，实测输出经常停在 ``{"网络缓存毫秒": 40000, "起播等待秒": 2, "硬解":
    "vaapi",`` 就没了。直接判失败会让 AI 白跑一趟；这里先补括号，补不上就砍掉
    最后那个半截字段再补。

    **只信完整的字段**：结尾是半个数字（``"网络缓存毫秒": 40``）这种一律丢掉，
    因为 40 很可能本来是 4000/40000，用它比用规则基线还糟。
    """
    原文 = _文本(文本)
    起点 = 原文.find("{")
    if 起点 < 0:
        return None
    片段 = 原文[起点:]
    for _ in range(40):
        片段 = 片段.rstrip()
        if 片段 and 片段[-1] not in ',}]"':
            # 结尾是"可能被截断的值"：退到上一个完整分隔符（保留分隔符本身）
            切点 = max(片段.rfind(","), 片段.rfind("{"), 片段.rfind("["))
            if 切点 <= 0:
                return None
            片段 = 片段[:切点 + 1]
            continue
        补齐 = _未闭合(片段)
        if 补齐 is None:
            return None
        数据 = _尝试JSON(片段 + "".join(补齐))
        if isinstance(数据, dict) and 数据:
            return 数据
        # 补全也解析不了 → 再砍一段（可能是写坏的键值对）
        切点 = max(片段.rfind(","), 片段.rfind("{"), 片段.rfind("["))
        if 切点 <= 0:
            return None
        片段 = 片段[:切点 + 1]
    return None


def 解析JSON对象(文本, 期望键: tuple = ()) -> Optional[dict]:
    """从模型输出里抠出一个 JSON 对象；抠不到返回 ``None``（调用方回落规则）。

    依次尝试：直接解析 → 剥代码块 → 全文替换全角括号 → 花括号配对扫片段 →
    补齐被截断的对象。deepseek-r1 有时把 JSON 写在思考块里、超过 token 上限被
    砍断、或前后带一堆解释，这几步都能救回来。

    ``期望键`` 用来在"一段文字里有好几个 JSON 对象"时挑对的那个：
    模型经常先写一坨推理（里面也有花括号），最后才是真正的参数对象。
    给定期望键后，优先返回**含其中任一键**的对象。
    """
    原文 = _文本(文本)
    if not 原文:
        return None
    候选: list[str] = []
    for 项 in (原文, _去代码块(原文),
              原文.replace("｛", "{").replace("｝", "}")):
        if 项 and 项 not in 候选:
            候选.append(项)
    找到: list[dict] = []
    for 项 in 候选:
        数据 = _尝试JSON(项)
        if isinstance(数据, dict) and 数据 and 数据 not in 找到:
            找到.append(数据)
    for 片段 in _扫描对象(原文):
        数据 = _尝试JSON(片段)
        if isinstance(数据, dict) and 数据 and 数据 not in 找到:
            找到.append(数据)
    修补 = _修补截断JSON(原文)
    if 修补 and 修补 not in 找到:
        找到.append(修补)
    if not 找到:
        return None
    if 期望键:
        for 数据 in 找到:
            if any(键 in 数据 for 键 in 期望键):
                return 数据
    return 找到[0]


def _归一化选项(原始) -> list[str]:
    """把 AI 给的选项整成 libvlc 认的 ``:option`` 列表（去空、去重、补冒号）。"""
    if isinstance(原始, str):
        原始 = [原始]
    if not isinstance(原始, (list, tuple)):
        return []
    结果: list[str] = []
    for 项 in 原始:
        文本 = _文本(项)
        if not 文本:
            continue
        if not 文本.startswith(":"):
            文本 = ":" + 文本.lstrip("-")
        if 文本 not in 结果:
            结果.append(文本)
    return 结果


def _硬解选项值(硬解: str) -> str:
    """把硬解方式翻译成 libvlc ``--avcodec-hw`` 认得的值。

    libvlc 只认 ``any`` / ``none`` / 具体后端名（vaapi、cuda…）；上层（播放核心）
    习惯把"让 VLC 自己挑"叫 ``auto``，直接把 auto 塞进 ``:avcodec-hw=`` 会让
    libvlc 找不到同名模块，反而可能退回软解。
    """
    值 = _文本(硬解).lower() or "none"
    return "any" if 值 in ("auto", "any", "自动", "默认") else 值


def _同步缓存选项(选项: list[str], 缓存毫秒: int) -> list[str]:
    """保证 ``:network-caching`` 与 ``网络缓存毫秒`` 一致（AI 常常只改其中一个）。"""
    其他 = [x for x in _归一化选项(选项) if not x.startswith(":network-caching")]
    return [f":network-caching={int(缓存毫秒)}"] + 其他


def _同步硬解选项(选项: list[str], 硬解: str) -> list[str]:
    """保证 ``:avcodec-hw`` 与 ``硬解`` 一致。

    为什么必须"替换"而不是"追加"：学习库/AI 给的旧选项里可能写着
    ``:avcodec-hw=vaapi``，而这台机器已经没有 vaapi 了；两条冲突的选项
    谁生效取决于 libvlc 内部顺序，等于埋雷。
    """
    其他 = [x for x in _归一化选项(选项) if not x.startswith(":avcodec-hw")]
    return 其他 + [f":avcodec-hw={_硬解选项值(硬解)}"]


def _加入选项(选项: list[str], 新项: str) -> list[str]:
    结果 = list(_归一化选项(选项))
    if 新项 not in 结果:
        结果.append(新项)
    return 结果


# ==================== 规则基线（AI 不在也能用） ====================


def _总码率bps(探测: dict) -> tuple[float, str]:
    """总码率 = 视频 + 音频；没给就用 ``大小 ÷ 时长`` 估；再不行给 0（未知）。"""
    视频 = _浮点(探测.get("视频码率bps"))
    音频 = max(0.0, _浮点(探测.get("音频码率bps")))
    if 视频 > 0:
        return 视频 + 音频, "实测"
    大小 = _浮点(探测.get("大小字节"))
    时长 = _浮点(探测.get("时长秒"))
    if 大小 > 0 and 时长 > 0:
        return 大小 * 8.0 / 时长, "按大小估算"
    return 0.0, "未知"


def _选硬解(候选列表) -> tuple[str, list[str]]:
    """从"本机硬解"里挑一个：**vaapi 优先**，其次其它非 none，最后才 none。

    返回 ``(选中方式, 备选列表)``；备选给 :meth:`播放顾问.诊断卡顿` 换方案用。
    """
    if isinstance(候选列表, (list, tuple)):
        列表 = [_文本(x).lower() for x in 候选列表]
    elif 候选列表:
        列表 = [_文本(候选列表).lower()]
    else:
        列表 = []
    列表 = [x for x in 列表 if x]
    可用 = [x for x in 列表 if x != "none"]
    # 去重但保持原顺序（保持"顺序即优先级"的语义）
    去重: list[str] = []
    for 项 in 可用:
        if 项 not in 去重:
            去重.append(项)
    if not 去重:
        return "none", []
    for 首选 in 硬解优先级:
        if 首选 in 去重:
            return 首选, [x for x in 去重 if x != 首选]
    return 去重[0], 去重[1:]


def _校验硬解(值, 可用列表) -> str:
    """只接受"本机确实有"的硬解方式（含 none），否则返回空串表示"别改"。"""
    文本 = _文本(值).lower()
    if not 文本:
        return ""
    可用 = {_文本(x).lower() for x in (可用列表 or []) if _文本(x)}
    可用.add("none")           # 软解永远可选
    return 文本 if 文本 in 可用 else ""


def _基础缓存毫秒(比值: float, 码率: float, 带宽: float, 档: str,
            TTFB毫秒: float) -> int:
    """按"带宽/码率比值"给缓存窗口：余量越小、缓冲越厚。

    比值大（局域网/高速网盘）→ 4 秒就够，起播快；
    比值接近 1 甚至小于 1 → 直接拉满 20 秒，靠缓冲把波动抹平。
    """
    if 码率 <= 0 or 带宽 <= 0:
        缓存 = 12000                    # 数据不全：取中间偏保守值
    elif 比值 >= 4:
        缓存 = 4000
    elif 比值 >= 3:
        缓存 = 5000
    elif 比值 >= 2:
        缓存 = 7000
    elif 比值 >= 1.5:
        缓存 = 9000
    elif 比值 >= 1.3:
        缓存 = 12000
    elif 比值 >= 1.0:
        缓存 = 16000
    else:
        缓存 = 20000
    if 档 == "2160p":
        if 码率 > 0 and 带宽 > 0 and 比值 < 2.5:
            缓存 += 3000                # 4K 单帧大，同样的网络抖动更容易卡
        缓存 = max(缓存, 6000)
    elif 档 == "4320p":
        if 码率 > 0 and 带宽 > 0 and 比值 < 3:
            缓存 += 4000
        缓存 = max(缓存, 8000)
    elif 档 == "1440p":
        缓存 = max(缓存, 5000)
    if TTFB毫秒 >= 2000:
        缓存 += 4000                    # 首字节都这么久，说明链路差，多攒点
    elif TTFB毫秒 >= 800:
        缓存 += 2000
    缓存 = int(round(缓存 / 500.0) * 500)          # 整成 500ms 的倍数，日志好读
    return int(min(缓存上限毫秒, max(缓存下限毫秒, 缓存)))


def _起播等待秒(比值: float, 码率: float, 带宽: float, 档: str) -> float:
    """预缓冲多少秒再开播：带宽越紧、分辨率越高，越值得先攒一点。

    注意 0 是合法值（立刻起播）：带宽 ≥5 倍且不是 4K 时没必要让用户等。
    """
    if 码率 <= 0 or 带宽 <= 0:
        return 4.0
    if 比值 >= 5 and 档 not in ("2160p", "4320p"):
        return 0.0
    if 比值 >= 3:
        return 2.0 if 档 in ("2160p", "4320p") else 0.5
    if 比值 >= 2:
        return 3.0 if 档 in ("2160p", "4320p") else 1.5
    if 比值 >= 1.5:
        return 4.0
    if 比值 >= 1.3:
        return 6.0
    if 比值 >= 1.0:
        return 8.0
    return 10.0


def _档人话(档: str) -> str:
    """给用户看的档位名：2160p 说成 4K 更好懂。"""
    if 档 == "未知":
        return "未知画质"
    return {"2160p": "4K", "4320p": "8K", "1440p": "2K"}.get(档, 档)


def _降一档(档: str) -> str:
    """丢帧且带宽不够时建议切哪一档。"""
    return {"4320p": "2160p", "2160p": "1080p", "1440p": "1080p",
            "1080p": "720p", "720p": "480p", "480p": "360p"}.get(档, "1080p")


def _构建选项(缓存毫秒: int, 硬解: str, 够流畅: bool, 档: str,
          range支持: bool) -> list[str]:
    """拼 libvlc 的 ``:option`` 列表。每条都写清"为什么加"。

    * ``:network-caching`` —— 即使核心自己也会设，这里再给一份，保证核心
      直接把 ``附加选项`` 丢给 libvlc 时也生效；
    * ``:clock-jitter=0`` / ``:clock-synchro=0`` —— 网络流最常见的卡顿来自
      时钟抖动：VLC 反复校正时钟导致画面顿一下；直链播放没有录制时钟可对齐，
      干脆关掉抖动容忍；
    * ``:avcodec-hw=`` —— 明确指定硬解方式；``none`` 也要显式给，
      免得 libvlc 自己乱挑一个不支持的方式再回退（回退过程会花屏/卡一下）；
    * 带宽不够时 ``:drop-late-frames``（宁可丢帧也别停住）；
      带宽充裕的 4K 反而 ``:no-drop-late-frames``（大画面丢一帧很显眼，保画质）；
    * 不支持 Range 的直链，拖动进度会让服务器重新开始推流，开 ``:http-reconnect``
      让 VLC 自己重连，而不是直接报错退出。
    """
    选项 = [f":network-caching={int(缓存毫秒)}", ":clock-jitter=0", ":clock-synchro=0"]
    选项.append(f":avcodec-hw={_硬解选项值(硬解)}")
    if not 够流畅:
        选项.append(":drop-late-frames")
    elif 档 in ("2160p", "4320p"):
        选项.append(":no-drop-late-frames")
    if not range支持:
        选项.append(":http-reconnect=true")
    return 选项


def _人话风险(档: str, 码率: float, 码率来源: str, 带宽: float,
          比值: float, 够流畅: bool, 起播秒: float,
          硬解: str, range支持: bool, 剩余字节: float) -> str:
    """把参数翻译成用户能看懂的一句话（界面直接显示这一行）。"""
    件: list[str] = []
    if 码率 > 0 and 带宽 > 0:
        开头 = f"{_档人话(档)} " if 档 != "未知" else ""
        件.append(f"{开头}{码率 / 1e6:.1f}Mbps 码率，实测带宽 {带宽 / 1e6:.1f}Mbps"
                 f"（{比值:.1f} 倍）")
        if 够流畅:
            件.append(f"带宽够（需 ≥{码率 * 带宽冗余系数 / 1e6:.1f}Mbps）")
        else:
            件.append(f"带宽不够（需 ≥{码率 * 带宽冗余系数 / 1e6:.1f}Mbps），会卡；"
                     f"建议切 {_降一档(档)} 或换低码率片源")
    elif 带宽 <= 0:
        件.append("没测到实测带宽，先按保守参数起播，边播边看")
    else:
        件.append("片源没给码率，按保守参数起播")
    if 码率来源 == "按大小估算":
        件.append("码率是按大小估算的（大小÷时长）")
    if 起播秒 > 0:
        件.append(f"起播前缓冲 {起播秒:g} 秒")
    else:
        件.append("可直接起播")
    件.append(f"硬解 {硬解}" if 硬解 != "none" else "本机无可用硬解，只能软解（CPU 可能吃满）")
    if not range支持:
        件.append("该直链不支持 Range，拖动进度可能要重新加载")
    if 0 <= 剩余字节 < 500_000_000:
        件.append(f"缓存目录只剩 {剩余字节 / 1e6:.0f}MB，别开磁盘缓存")
    return "；".join(件) + "。"


def _规则理由(档: str, 码率: float, 码率来源: str, 带宽: float, 比值: float,
          够流畅: bool, 缓存毫秒: int, 起播秒: float, 硬解: str,
          TTFB毫秒: float, 选项: list[str]) -> str:
    """说明"这些数字是怎么算出来的"，方便用户/开发者复核规则。"""
    步: list[str] = []
    if 码率 > 0 and 带宽 > 0:
        步.append(f"总码率 {码率 / 1e6:.1f}Mbps（{码率来源}），"
                 f"带宽/码率 = {比值:.2f}")
        步.append(f"{'带宽够（≥1.3 倍）' if 够流畅 else '带宽不足 1.3 倍，缓存拉满保守起播'}")
    else:
        步.append("码率或带宽未知 → 取保守的 12 秒缓存")
    步.append(f"网络缓存 {缓存毫秒}ms")
    步.append(f"预缓冲 {起播秒:g} 秒" if 起播秒 else "预缓冲 0 秒（立刻起播）")
    if 档 in ("2160p", "4320p"):
        步.append(f"{_档人话(档)} 单帧大，缓存与预缓冲都已上调")
    步.append(f"硬解取 本机硬解 里优先级最高的 {硬解}")
    if TTFB毫秒 > 0:
        步.append(f"首字节 {TTFB毫秒:.0f}ms")
    if 选项:
        步.append("附加选项：" + " ".join(选项))
    return "；".join(步) + "。"


# ==================== 学习库（小 SQLite，坏了也不能影响播放） ====================


class _播放学习库:
    """播放效果学习库：一行一次播放，聚合出"这个键该怎么播"。

    为什么用 SQLite 而不是 JSON：播放线程会频繁写（每次起播/卡顿都要记），
    SQLite 天生并发安全、断电不坏文件；单文件也可以随时删掉重学。
    """

    最多保留 = 5000          # 无限长下去必查越慢；只留最近的 5000 条
    表 = "播放学习"

    def __init__(self, 路径: str, 日志: Callable[[str], None] = None):
        self.路径 = str(路径 or "")
        self._日志 = 日志 or (lambda 文本: None)
        self._锁 = threading.RLock()
        self._连接: Optional[sqlite3.Connection] = None
        self.可用 = False
        self._初始化()

    # ---------- 连接 ----------

    def _连接对象(self) -> sqlite3.Connection:
        with self._锁:
            if self._连接 is None:
                连接 = sqlite3.connect(self.路径, timeout=10.0,
                                    check_same_thread=False)
                连接.row_factory = sqlite3.Row
                连接.execute("PRAGMA journal_mode=WAL")
                连接.execute("PRAGMA synchronous=NORMAL")
                self._连接 = 连接
            return self._连接

    def _初始化(self) -> None:
        if not self.路径:
            self._日志("[播放顾问] 学习库路径为空，本次只走规则/AI")
            return
        try:
            目录 = os.path.dirname(os.path.abspath(self.路径))
            if 目录:
                os.makedirs(目录, exist_ok=True)
            连接 = self._连接对象()
            连接.execute(_建表语句)
            连接.execute("CREATE INDEX IF NOT EXISTS idx_播放学习_键 "
                       "ON 播放学习(键)")
            连接.commit()
            self.可用 = True
        except Exception as e:  # noqa: BLE001 - 磁盘满了/只读目录都不该拦住播放
            self.可用 = False
            self._日志(f"[播放顾问] 学习库不可用（{type(e).__name__}: {e}），"
                     f"本次只走规则/AI")

    def 关闭(self) -> None:
        with self._锁:
            if self._连接 is not None:
                try:
                    self._连接.close()
                except Exception:  # noqa: BLE001
                    pass
                self._连接 = None

    # ---------- 写 ----------

    def 记录(self, 键: str, 参数: dict, 指标: dict) -> bool:
        if not self.可用:
            return False
        键 = _文本(键)
        if not 键:
            return False
        参数 = 参数 if isinstance(参数, dict) else {}
        指标 = 指标 if isinstance(指标, dict) else {}
        网盘, 档 = _拆键(键)
        卡顿 = 判定卡顿(指标)
        码率 = _浮点(指标.get("平均码率bps")) or _浮点(参数.get("平均码率bps"))
        try:
            with self._锁:
                连接 = self._连接对象()
                连接.execute(
                    f"INSERT INTO {self.表}(键, 网盘, 分辨率档, 参数JSON, 指标JSON,"
                    f" 是否卡顿, 平均码率bps, 缓冲等待次数, 丢帧, 已播秒, 来源, 创建时间)"
                    f" VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (键, 网盘, 档,
                     json.dumps(参数, ensure_ascii=False),
                     json.dumps(指标, ensure_ascii=False),
                     1 if 卡顿 else 0, 码率,
                     _整数(指标.get("缓冲等待次数")),
                     _整数(指标.get("丢帧")),
                     _浮点(指标.get("已播秒")),
                     _文本(参数.get("来源")),
                     _现在()))
                连接.commit()
                self._清理(连接)
            return True
        except Exception as e:  # noqa: BLE001
            self._日志(f"[播放顾问] 写学习库失败（{type(e).__name__}: {e}）")
            return False

    def _清理(self, 连接) -> None:
        总数 = 连接.execute(f"SELECT COUNT(*) FROM {self.表}").fetchone()[0] or 0
        if 总数 > self.最多保留:
            连接.execute(
                f"DELETE FROM {self.表} WHERE 行号 NOT IN "
                f"(SELECT 行号 FROM {self.表} ORDER BY 行号 DESC LIMIT ?)",
                (self.最多保留,))
            连接.commit()

    # ---------- 读 ----------

    @staticmethod
    def _行转dict(行) -> dict:
        try:
            参数 = json.loads(行["参数JSON"])
        except Exception:  # noqa: BLE001
            参数 = {}
        try:
            指标 = json.loads(行["指标JSON"])
        except Exception:  # noqa: BLE001
            指标 = {}
        return {
            "键": 行["键"], "网盘": 行["网盘"] or "", "分辨率档": 行["分辨率档"] or "",
            "参数": 参数 if isinstance(参数, dict) else {},
            "指标": 指标 if isinstance(指标, dict) else {},
            "是否卡顿": bool(行["是否卡顿"]),
            "平均码率bps": _浮点(行["平均码率bps"]),
            "缓冲等待次数": _整数(行["缓冲等待次数"]),
            "丢帧": _整数(行["丢帧"]),
            "已播秒": _浮点(行["已播秒"]),
            "来源": 行["来源"] or "",
            "创建时间": 行["创建时间"] or "",
        }

    def 查询(self, 键: str = "", 上限: int = 200) -> list[dict]:
        if not self.可用:
            return []
        上限 = _整数(上限, 200, 1, 2000)
        键 = _文本(键)
        try:
            with self._锁:
                连接 = self._连接对象()
                if 键:
                    行集 = 连接.execute(
                        f"SELECT * FROM {self.表} WHERE 键=? ORDER BY 行号 DESC LIMIT ?",
                        (键, 上限)).fetchall()
                else:
                    行集 = 连接.execute(
                        f"SELECT * FROM {self.表} ORDER BY 行号 DESC LIMIT ?",
                        (上限,)).fetchall()
            return [self._行转dict(行) for 行 in 行集]
        except Exception as e:  # noqa: BLE001
            self._日志(f"[播放顾问] 读学习库失败（{type(e).__name__}: {e}）")
            return []

    def 最佳(self, 候选键: list[str], 最少样本数: int = 最少样本,
           卡顿率上限值: float = 卡顿率上限) -> Optional[dict]:
        """在候选键里找"表现够好"的历史参数；找不到返回 ``None``。

        判定"好"：样本 ≥2 且卡顿率 ≤1/3。取**最近一次不卡顿**的参数，
        因为那才是"这台机器 + 这个网盘"真正验证过的配置。
        """
        if not self.可用:
            return None
        for 键 in 候选键:
            键 = _文本(键)
            if not 键:
                continue
            try:
                with self._锁:
                    连接 = self._连接对象()
                    汇总 = 连接.execute(
                        f"SELECT COUNT(*) AS 次数, COALESCE(SUM(是否卡顿),0) AS 卡顿次数,"
                        f" COALESCE(AVG(平均码率bps),0) AS 平均码率"
                        f" FROM {self.表} WHERE 键=?", (键,)).fetchone()
                    次数 = _整数(汇总["次数"] if 汇总 else 0)
                    if 次数 < 最少样本数:
                        continue
                    卡顿次数 = _整数(汇总["卡顿次数"] if 汇总 else 0)
                    卡顿率 = 卡顿次数 / 次数 if 次数 else 1.0
                    if 卡顿率 > 卡顿率上限值:
                        continue
                    行 = 连接.execute(
                        f"SELECT * FROM {self.表} WHERE 键=? AND 是否卡顿=0"
                        f" ORDER BY 行号 DESC LIMIT 1", (键,)).fetchone()
                    if 行 is None:
                        行 = 连接.execute(
                            f"SELECT * FROM {self.表} WHERE 键=?"
                            f" ORDER BY 行号 DESC LIMIT 1", (键,)).fetchone()
                if 行 is None:
                    continue
                记录 = self._行转dict(行)
                记录.update({"样本数": 次数, "卡顿次数": 卡顿次数, "卡顿率": 卡顿率,
                          "平均码率bps": _浮点(汇总["平均码率"] if 汇总 else 0)})
                return 记录
            except Exception as e:  # noqa: BLE001
                self._日志(f"[播放顾问] 查询学习库失败（{type(e).__name__}: {e}）")
                return None
        return None

    def 统计(self) -> dict:
        if not self.可用:
            return {"库可用": False, "库路径": self.路径, "总记录数": 0,
                    "不同键数": 0, "卡顿记录数": 0, "卡顿率": 0.0,
                    "平均码率bps": 0.0, "最近记录时间": "",
                    "按网盘": {}, "按分辨率档": {}}
        try:
            with self._锁:
                连接 = self._连接对象()
                行 = 连接.execute(
                    f"SELECT COUNT(*) AS 总数, COUNT(DISTINCT 键) AS 键数,"
                    f" COALESCE(SUM(是否卡顿),0) AS 卡顿数,"
                    f" COALESCE(AVG(平均码率bps),0) AS 码率,"
                    f" COALESCE(MAX(创建时间),'') AS 最近"
                    f" FROM {self.表}").fetchone()
                按网盘 = self._分组(连接, "网盘")
                按档 = self._分组(连接, "分辨率档")
            总数 = _整数(行["总数"] if 行 else 0)
            卡顿数 = _整数(行["卡顿数"] if 行 else 0)
            return {
                "库可用": True, "库路径": self.路径,
                "总记录数": 总数,
                "不同键数": _整数(行["键数"] if 行 else 0),
                "卡顿记录数": 卡顿数,
                "卡顿率": (卡顿数 / 总数) if 总数 else 0.0,
                "平均码率bps": _浮点(行["码率"] if 行 else 0),
                "最近记录时间": _文本(行["最近"] if 行 else ""),
                "按网盘": 按网盘, "按分辨率档": 按档,
            }
        except Exception as e:  # noqa: BLE001
            self._日志(f"[播放顾问] 统计学习库失败（{type(e).__name__}: {e}）")
            return {"库可用": False, "库路径": self.路径, "总记录数": 0,
                    "不同键数": 0, "卡顿记录数": 0, "卡顿率": 0.0,
                    "平均码率bps": 0.0, "最近记录时间": "",
                    "按网盘": {}, "按分辨率档": {}}

    def _分组(self, 连接, 列: str) -> dict:
        行集 = 连接.execute(
            f"SELECT {列} AS 名, COUNT(*) AS 次数, COALESCE(SUM(是否卡顿),0) AS 卡顿数,"
            f" COALESCE(AVG(平均码率bps),0) AS 码率"
            f" FROM {self.表} GROUP BY {列}").fetchall()
        结果 = {}
        for 行 in 行集:
            次数 = _整数(行["次数"])
            结果[_文本(行["名"]) or "未知"] = {
                "记录数": 次数,
                "卡顿率": (_整数(行["卡顿数"]) / 次数) if 次数 else 0.0,
                "平均码率bps": _浮点(行["码率"]),
            }
        return 结果


def _正在缓冲(采样: dict) -> bool:
    """采样里有没有"此刻正在缓冲"的明确信号。

    为什么要单独看它：``缓冲等待次数`` 是**累计**值，一次两小时的电影中途卡一下
    并不值得动参数；但"现在就在缓冲"是硬伤，必须立刻处理。播放核心给的
    ``状态``/``缓冲中`` 字段正好表达这个意思（真实调用点见 v8_3/播放/播放核心.py）。
    """
    if not isinstance(采样, dict):
        return False
    if _布尔(采样.get("缓冲中"), False):
        return True
    return "缓冲" in _文本(采样.get("状态"))


def 判定卡顿(指标: dict) -> bool:
    """从指标里判"这次卡不卡"：显式给了就用；没给就按丢帧/缓冲等待推断。"""
    指标 = 指标 if isinstance(指标, dict) else {}
    for 键 in ("是否卡顿", "卡顿"):
        if 键 in 指标 and 指标.get(键) is not None and _文本(指标.get(键)) != "":
            return _布尔(指标.get(键), False)
    if _整数(指标.get("缓冲等待次数")) >= 缓冲等待阈值:
        return True
    if _整数(指标.get("丢帧")) >= 丢帧阈值:
        return True
    return False


# ==================== AI 提示词 ====================

_建议系统提示 = """你是网盘播放器的起播参数顾问。只输出 JSON，不要 markdown，不要解释，不要思考过程。
格式示例（数值和文字都要按本次数据自己写，不要照抄）：
{"网络缓存毫秒":6000,"起播等待秒":2,"硬解":"vaapi","附加选项":[":clock-jitter=0"],
"可流畅播放":true,"风险":"4K 25Mbps、带宽 90Mbps 够用，起播缓冲 2 秒",
"理由":"带宽是码率的 3.6 倍，4K 单帧大，缓存取 6000ms"}
规则：带宽÷码率<1.3 时可流畅播放=false 且 网络缓存毫秒=20000；比值越小缓存越大、起播等待越长（0=立刻起播）；4K 更保守；硬解只能选"本机硬解"列表里的，优先 vaapi；风险/理由必须带本次的具体数字。
只输出 JSON。"""

_诊断系统提示 = """你是网盘播放器的卡顿诊断专家。只输出 JSON，不要 markdown，不要解释，不要思考过程。
格式示例（数值和文字都要按本次数据自己写，不要照抄）：
{"需要调整":true,"动作":["加大网络缓存到 12000ms","强制硬解 vaapi"],
"新参数":{"网络缓存毫秒":12000,"起播等待秒":4,"硬解":"vaapi",
"附加选项":[":clock-jitter=0"],"可流畅播放":true,
"风险":"缓冲等待 3 次、当前缓冲 1.2 秒，网络供给不足","理由":"缓冲跟不上，加大缓存"},
"风险":"缓冲等待 3 次、当前缓冲 1.2 秒，网络供给不足","理由":"缓冲跟不上，加大缓存"}
规则：缓冲等待多或当前缓冲低 → 加大网络缓存与预缓冲；硬解生效=false → 强制硬解（只能选"本机硬解"里的）；丢帧多但缓冲充足 → 码率超带宽就建议降画质，否则是解码跟不上；CPU 高 → 动作加"关闭反交错"、附加选项加 :deinterlace=-1。动作/风险/理由要带本次的具体数字。
只输出 JSON。"""

#: 判断"模型这段文字里哪个对象才是答案"用的特征键（见 :func:`解析JSON对象`）。
_建议期望键 = ("网络缓存毫秒", "起播等待秒", "硬解", "附加选项", "可流畅播放")
_诊断期望键 = ("需要调整", "动作", "新参数")


# ==================== 播放顾问 ====================


class 播放顾问:
    """AI 辅助加载 + AI 优化播放（规则兜底，绝不抛异常）。

    参数
    ====
    * ``AI助手``：可选，需有 ``请求结构化策略(系统提示, 用户消息) -> dict``；
    * ``本地模型``：可选，需有 ``对话(用户消息, 系统提示, 最大tokens=512)``
      （``v8_3.AI.本地模型.本地模型客户端`` 正好符合），优先于 ``AI助手``；
    * ``日志回调``：可选 ``callable(文本)``，界面拿它往日志页写一行；
    * ``学习库路径``：SQLite 路径，默认 ``项目根/数据/AI播放学习.db``；
    * ``启用AI``：``False`` 时完全不问模型（学习库照用）。
    """

    VERSION = 1
    最少样本 = 最少样本
    卡顿率上限 = 卡顿率上限

    def __init__(self, AI助手=None, 本地模型=None, 日志回调=None, *,
                 学习库路径: str = "", 启用AI: bool = True):
        self.AI助手 = AI助手
        self.本地模型 = 本地模型
        self.启用AI = bool(启用AI)
        self._日志回调 = 日志回调
        self._计数锁 = threading.RLock()
        self._计数表: dict[str, int] = {}
        self._库 = _播放学习库(学习库路径 or 默认学习库路径(), self._日志)

    # ---------------- 基础设施 ----------------

    def _日志(self, 文本: str) -> None:
        """日志只是"顺带"，绝不能因为它把播放流程带崩。"""
        try:
            logger.debug("%s", 文本)
        except Exception:  # noqa: BLE001
            pass
        if callable(self._日志回调):
            try:
                self._日志回调(str(文本))
            except Exception:  # noqa: BLE001
                pass

    def _计数(self, 名称: str, 增量: int = 1) -> None:
        with self._计数锁:
            self._计数表[名称] = int(self._计数表.get(名称, 0)) + int(增量)

    @property
    def 学习库路径(self) -> str:
        return self._库.路径

    def 关闭(self) -> None:
        """释放 SQLite 连接（界面关闭/换库时调用；不调也不影响正确性）。"""
        self._库.关闭()

    # ==================== 一、AI 辅助加载 ====================

    def 建议起播参数(self, 探测结果: dict) -> dict:
        """起播前算参数。优先级：学习库 → AI（本地模型 → 云端）→ 规则。

        返回的 dict **一定**含 :data:`参数键` 里的全部键（缺键会让播放核心取到
        ``None`` 而崩），且数值都经过区间钳制。
        """
        探测 = dict(探测结果) if isinstance(探测结果, dict) else {}
        self._计数("建议次数")
        规则 = self._规则建议(探测)

        候选键 = self._候选键(探测)
        学习 = self._库.最佳(候选键) if 候选键 else None
        if 学习:
            self._计数("学习库命中")
            self._日志(f"[播放顾问] 学习库命中 {学习['键']}"
                     f"（样本 {学习['样本数']} 次、卡顿率 {学习['卡顿率']:.0%}），"
                     f"沿用历史有效参数")
            return self._套用学习(规则, 学习, 探测)

        if not self.启用AI:
            self._计数("规则建议")
            return 规则

        AI建议, 来源, 失败 = self._请求AI(_建议系统提示,
                                    self._建议用户消息(探测, 规则), "起播建议",
                                    _建议期望键)
        if AI建议:
            矛盾 = self._结论矛盾(AI建议, 规则)
            if 矛盾:
                # AI 连"带宽够不够"都看错了 → 它的数字也不可信，整条作废
                self._计数("AI结论矛盾")
                self._计数("规则回退")
                self._日志(f"[播放顾问] {来源}建议与实测矛盾（{矛盾}），"
                         f"整条作废，回落规则基线")
                规则["理由"] = f"{规则['理由'][:-1]}（{来源}结论与实测矛盾：{矛盾}，已按规则执行）。"
                return 规则
            结果 = self._归一化参数(AI建议, 规则, 探测.get("本机硬解"))
            结果["来源"] = 来源
            self._计数("本地模型建议" if 来源 == "本地模型" else "云端建议")
            self._日志(f"[播放顾问] {来源}给出起播参数：缓存 {结果['网络缓存毫秒']}ms、"
                     f"预缓冲 {结果['起播等待秒']:g} 秒、硬解 {结果['硬解']}")
            return 结果

        self._计数("规则回退")
        规则["理由"] = f"{规则['理由'][:-1]}（AI 未参与：{失败}）。"
        self._日志(f"[播放顾问] 回落规则基线：{失败 or '未配置 AI'}")
        return 规则

    # ---------------- 规则基线 ----------------

    def _规则建议(self, 探测: dict) -> dict:
        """纯规则算参数：**任何 AI 都不可用时，播放也必须能开**。"""
        档 = 分辨率档(探测.get("分辨率"))
        码率, 码率来源 = _总码率bps(探测)
        带宽 = _浮点(探测.get("实测带宽bps"))
        TTFB = _浮点(探测.get("首字节毫秒"))
        剩余字节 = _浮点(探测.get("缓存目录剩余字节"), -1.0)
        range支持 = _布尔(探测.get("range支持"), True)
        时长秒 = _浮点(探测.get("时长秒"))
        硬解, _备选 = _选硬解(探测.get("本机硬解"))

        比值 = (带宽 / 码率) if (码率 > 0 and 带宽 > 0) else 0.0
        # "够不够"是物理结论：带宽必须 ≥ 码率×1.3
        够流畅 = bool(码率 > 0 and 带宽 > 0 and 带宽 >= 码率 * 带宽冗余系数)

        缓存 = _基础缓存毫秒(比值, 码率, 带宽, 档, TTFB)
        起播 = _起播等待秒(比值, 码率, 带宽, 档)
        if 时长秒 > 0:
            # 短视频不值得为了缓冲等太久：等三分之一时长已经是上限
            起播 = round(min(起播, max(0.0, 时长秒 / 3.0)), 1)
        选项 = _构建选项(缓存, 硬解, 够流畅, 档, range支持)
        return {
            "网络缓存毫秒": int(缓存),
            "起播等待秒": float(起播),
            "硬解": 硬解,
            "附加选项": 选项,
            "可流畅播放": 够流畅,
            "风险": _人话风险(档, 码率, 码率来源, 带宽, 比值, 够流畅,
                         起播, 硬解, range支持, 剩余字节),
            "理由": _规则理由(档, 码率, 码率来源, 带宽, 比值, 够流畅, 缓存,
                         起播, 硬解, TTFB, 选项),
            "来源": "规则",
        }

    # ---------------- 学习库 ----------------

    def _候选键(self, 探测: dict) -> list[str]:
        """先精确键（带编码），再退化键（只有网盘+档），最后用调用方显式给的键。"""
        显式 = _文本(探测.get("键"))
        精确 = 生成学习键(探测.get("网盘"), 探测.get("分辨率"), 探测.get("视频编码"))
        粗 = 生成学习键(探测.get("网盘"), 探测.get("分辨率"))
        结果: list[str] = []
        for 键 in (显式, 精确, 粗):
            if 键 and 键 not in 结果:
                结果.append(键)
        return 结果

    def _套用学习(self, 规则: dict, 学习: dict, 探测: dict) -> dict:
        """把学习库的历史参数套到当前建议上。

        两处**必须**用当前实测修正，不能无脑照抄：
        * 带宽现在更紧张（规则判定不够流畅）时，缓存取两者较大值；
        * 学习记录里的硬解方式在本机列表里已经不存在时，丢弃它（换机器/换驱动了）。
        """
        结果 = dict(规则)
        学参 = 学习.get("参数") or {}
        缓存 = _整数(学参.get("网络缓存毫秒"), 0, 0, 缓存上限毫秒)
        if 缓存 > 0:
            if not 规则["可流畅播放"]:
                缓存 = max(缓存, _整数(规则["网络缓存毫秒"]))
            结果["网络缓存毫秒"] = _整数(缓存, 规则["网络缓存毫秒"],
                                 缓存下限毫秒, 缓存上限毫秒)
        起播 = _可选浮点(学参.get("起播等待秒"))
        if 起播 is not None:
            结果["起播等待秒"] = round(min(20.0, max(0.0, 起播)), 1)
        硬解 = _校验硬解(学参.get("硬解"), 探测.get("本机硬解"))
        if 硬解:
            结果["硬解"] = 硬解
        选项 = 学参.get("附加选项")
        if isinstance(选项, (list, tuple)):
            选项 = _归一化选项(选项)
            if 选项:
                结果["附加选项"] = 选项
        结果["附加选项"] = _同步硬解选项(结果["附加选项"], 结果["硬解"])
        结果["附加选项"] = _同步缓存选项(结果["附加选项"], 结果["网络缓存毫秒"])
        结果["来源"] = "学习库"
        结果["风险"] = 规则["风险"]      # 风险按"当前实测"重新说，不抄旧结论
        结果["理由"] = (f"学习库命中 {学习['键']}（样本 {学习['样本数']} 次、"
                     f"卡顿率 {学习['卡顿率']:.0%}、平均码率 "
                     f"{_浮点(学习.get('平均码率bps')) / 1e6:.1f}Mbps），"
                     f"沿用历史上验证过的参数；" + 规则["理由"])
        return 结果

    # ---------------- AI 接入 ----------------

    def _建议用户消息(self, 探测: dict, 规则: dict) -> str:
        """给模型看：探测数据（精简过）+ 规则基线数值。

        两个刻意的取舍：
        * 只给"数值型"的规则基线（``_参数要旨``），**不给** 风险/理由 文案 ——
          小模型会把现成句子原样抄回来，让它自己按数字写反而更准；
        * 码率/带宽换成 bps 整数（别带千分位/科学计数法），1.5B 模型对长数字
          更容易算错，所以额外给出"带宽÷码率"这个关键比值。
        """
        码率, _来源 = _总码率bps(探测)
        带宽 = _浮点(探测.get("实测带宽bps"))
        # 刻意把 bps 换成 Mbps：1.5B 级别的小模型对着 25000000 这种长数字
        # 很容易算错（真机上它把"码率÷音频码率"当成帧率算），位数越少越靠谱。
        精简 = {
            "网盘": _文本(探测.get("网盘")) or "未知",
            "文件名": _文本(探测.get("文件名"))[:60],
            "分辨率": _文本(探测.get("分辨率")),
            "视频编码": _文本(探测.get("视频编码")),
            "容器": _文本(探测.get("容器")),
            "大小MB": round(_浮点(探测.get("大小字节")) / 1e6, 1),
            "时长秒": round(_浮点(探测.get("时长秒")), 1),
            "视频码率Mbps": round(_浮点(探测.get("视频码率bps")) / 1e6, 2),
            "音频码率Mbps": round(_浮点(探测.get("音频码率bps")) / 1e6, 3),
            "首字节毫秒": _整数(探测.get("首字节毫秒")),
            "实测带宽Mbps": round(带宽 / 1e6, 2),
            "range支持": _布尔(探测.get("range支持"), True),
            "本机硬解": list(探测.get("本机硬解") or []),
            "缓存目录剩余MB": round(_浮点(探测.get("缓存目录剩余字节")) / 1e6, 1),
        }
        关键 = {
            "总码率Mbps": round(码率 / 1e6, 2),
            "带宽是码率的几倍": round(带宽 / 码率, 2) if (码率 > 0 and 带宽 > 0) else 0.0,
            "带宽是否够_要求大于等于1.3倍": bool(
                码率 > 0 and 带宽 > 0 and 带宽 >= 码率 * 带宽冗余系数),
            "分辨率档": 分辨率档(探测.get("分辨率")),
            "规则基线数值": _参数要旨(规则),
        }
        return ("【探测结果】" + json.dumps(精简, ensure_ascii=False)
                + "\n【关键指标与规则基线】" + json.dumps(关键, ensure_ascii=False)
                + "\n请按本次数据输出参数 JSON。")

    def _请求AI(self, 系统提示: str, 用户消息: str, 用途: str,
              期望键: tuple = ()) -> tuple[dict, str, str]:
        """逐级问 AI，返回 ``(建议dict, 来源, 失败原因)``。

        本地模型优先（免费离线）；它没配/报错/吐不出 JSON 就问云端；
        两边都不行返回空 dict + 失败原因，调用方回落规则。**异常全部吞掉**。
        """
        if not self.启用AI:
            return {}, "", "AI 已关闭"
        失败: list[str] = []

        if self.本地模型 is not None:
            if callable(getattr(self.本地模型, "对话", None)):
                文本列表, 错误 = self._问本地模型(系统提示, 用户消息)
                最佳, 最佳分, 最佳文本 = None, -1, ""
                for 文本 in 文本列表:
                    解析 = 解析JSON对象(文本, 期望键)
                    if not 解析:
                        continue
                    # 两个通道（正文/思考）都可能带 JSON，取"字段更全"的那份：
                    # 被截断的正文往往只剩前两个字段，思考里的草稿反而是完整的
                    分 = (sum(1 for 键 in 期望键 if 键 in 解析)
                         if 期望键 else len(解析))
                    if 分 > 最佳分:
                        最佳, 最佳分, 最佳文本 = 解析, 分, 文本
                if 最佳:
                    return 最佳, "本地模型", ""
                self._计数("AI解析失败")
                失败.append(f"本地模型输出解析不出 JSON（{_截断(最佳文本 or (文本列表[0] if 文本列表 else 错误))}）")
                self._日志(f"[播放顾问] {用途}：本地模型输出不是 JSON，"
                         f"尝试其它来源")
            else:
                失败.append("本地模型对象没有 对话() 方法")

        if self.AI助手 is not None:
            if callable(getattr(self.AI助手, "请求结构化策略", None)):
                try:
                    数据 = self.AI助手.请求结构化策略(系统提示, 用户消息)
                except Exception as e:  # noqa: BLE001 - 云端挂了也不能影响播放
                    self._计数("AI异常")
                    失败.append(f"云端助手异常 {type(e).__name__}: {e}")
                else:
                    if isinstance(数据, dict) and 数据:
                        return 数据, "云端", ""
                    失败.append("云端助手没返回有效 JSON")
            else:
                失败.append("AI助手对象没有 请求结构化策略() 方法")

        if not 失败:
            失败.append("没有可用的 AI（既没有本地模型也没有 AI 助手）")
        return {}, "", "；".join(失败)

    def _问本地模型(self, 系统提示: str, 用户消息: str) -> tuple[list[str], str]:
        """调本地模型，返回 ``(候选输出文本列表, 错误)``。

        为什么要返回**列表**：真机实测（deepseek-r1:1.5b + Ollama）会发现，模型
        512 tokens 的预算经常先被"思考"吃掉一半，于是
        ``内容`` 通道里只剩一段**被截断**的 JSON，而 ``推理内容``（thinking）
        通道里反而躺着一份**完整草稿**。只认正文会白白丢掉可用的结果，
        所以两个通道都给出去，让调用方挑字段更全的那份。

        兼容三种返回形态：``对话结果`` dataclass / dict / 纯字符串。
        """
        对话 = getattr(self.本地模型, "对话", None)
        try:
            结果 = 对话(用户消息, 系统提示, 最大tokens=512)
        except TypeError:
            # 有些替身/旧实现不接受 最大tokens，退一步再试，别让小差异丢掉 AI
            try:
                结果 = 对话(用户消息, 系统提示)
            except Exception as e:  # noqa: BLE001
                self._计数("AI异常")
                return [], f"本地模型异常 {type(e).__name__}: {e}"
        except Exception as e:  # noqa: BLE001
            self._计数("AI异常")
            return [], f"本地模型异常 {type(e).__name__}: {e}"

        if isinstance(结果, str):
            文本 = 结果.strip()
            return ([文本] if 文本 else []), ("" if 文本 else "本地模型返回空内容")
        内容 = _文本(getattr(结果, "内容", "") or
                    (结果.get("内容") if isinstance(结果, dict) else ""))
        推理 = _文本(getattr(结果, "推理内容", "") or
                    (结果.get("推理内容") if isinstance(结果, dict) else ""))
        错误 = _文本(getattr(结果, "错误", "") or
                    (结果.get("错误") if isinstance(结果, dict) else ""))
        候选 = [x for x in (内容, 推理) if x]
        if 候选:
            return 候选, ""
        return [], 错误 or "本地模型返回空内容"

    # ---------------- AI 结果归一化 ----------------

    def _结论矛盾(self, 原始: dict, 基线: dict) -> str:
        """AI 的"可流畅播放"与实测结论不一致时，返回人话说明；一致/没给返回 ``""``。

        为什么这算"整条作废"的理由：``可流畅播放`` 不是偏好，而是"带宽 ÷ 码率"
        的实测结论。真机实测过 deepseek-r1:1.5b 把 90Mbps ÷ 25Mbps（3.57 倍）
        算成"带宽不够"，于是既报了假警，又把缓存从 6 秒抬到 20 秒 —— 连它给的
        数字一起信，只会比规则更差。所以一旦它对事实判断错了，本次 AI 建议全部
        丢弃，回落规则（宁可用稳的）。
        """
        if not isinstance(原始, dict):
            return ""
        想流畅 = 原始.get("可流畅播放")
        if 想流畅 is None or _文本(想流畅) == "":
            return ""
        实测 = bool(基线.get("可流畅播放"))
        AI结论 = _布尔(想流畅, 实测)
        if AI结论 == 实测:
            return ""
        return (f"AI 说{'能' if AI结论 else '不能'}流畅播放，"
                f"实测结论是{'够' if 实测 else '不够'}"
                f"（带宽与码率是实测的，以实测为准）")

    def _归一化参数(self, 原始: dict, 基线: dict,
                可用硬解) -> dict:
        """把 AI 给的参数并到规则基线上：非法值一律丢弃，实测结论不容改写。

        * 数值超范围就钳到 :data:`缓存下限毫秒` ~ :data:`缓存上限毫秒`；
        * 硬解只能从本机列表里选，AI 编一个不存在的后端只会让 libvlc 报错；
        * ``可流畅播放`` **永远**采用实测结论（调用方还会在矛盾时整条作废）；
        * ``风险``/``理由`` 只接受字符串 —— 真机上见过模型把这两个字段写成嵌套
          dict，直接 str() 出来会变成 ``{'带宽': '25000000bps', ...}`` 这种
          给用户看的 Python repr，比规则文案差得多。
        """
        结果 = {键: 基线.get(键) for 键 in 参数键}
        if not isinstance(原始, dict):
            return 结果
        缓存 = _整数(原始.get("网络缓存毫秒"), -1)
        if 缓存 > 0:
            结果["网络缓存毫秒"] = _整数(缓存, 基线["网络缓存毫秒"],
                                 缓存下限毫秒, 缓存上限毫秒)
        起播 = _可选浮点(原始.get("起播等待秒"))
        if 起播 is not None:
            结果["起播等待秒"] = round(min(20.0, max(0.0, 起播)), 1)
        硬解 = _校验硬解(原始.get("硬解"), 可用硬解)
        if 硬解:
            结果["硬解"] = 硬解
        elif _文本(原始.get("硬解")):
            self._日志(f"[播放顾问] AI 给的硬解 {_文本(原始.get('硬解'))} "
                     f"不在本机可用列表，按规则用 {结果['硬解']}")
        选项 = 原始.get("附加选项")
        # 只认列表：附加选项是"自由发挥"的口子，模型给一段散文时宁可不用，
        # 因为 libvlc 遇到可疑选项的表现不可预期，而规则基线一定是对的。
        if isinstance(选项, (list, tuple)):
            选项 = _归一化选项(选项)
            if 选项:
                结果["附加选项"] = 选项
        结果["可流畅播放"] = bool(基线["可流畅播放"])     # 实测结论，AI 改不了
        for 键 in ("风险", "理由"):
            值 = 原始.get(键)
            if isinstance(值, str) and 值.strip():
                结果[键] = 值.strip()
            elif 值:
                self._日志(f"[播放顾问] AI 给的 {键} 不是字符串"
                         f"（{type(值).__name__}），改用规则文案")
        # 选项里的缓存值/硬解必须和最终字段一致（AI 经常只改一个）
        结果["附加选项"] = _同步硬解选项(结果["附加选项"], 结果["硬解"])
        结果["附加选项"] = _同步缓存选项(结果["附加选项"], 结果["网络缓存毫秒"])
        return 结果

    # ==================== 二、AI 优化播放 ====================

    def 诊断卡顿(self, 采样: dict, 当前参数: dict,
             媒体信息: dict = None) -> dict:
        """播放中诊断：返回 ``{需要调整, 动作, 新参数, 理由, 风险, 来源}``。

        规则先算一版；AI 可用时在规则基线上做增删（AI 编不出来的硬解、
        非法数值都会被丢掉）。``采样`` 里的物理事实同样压过 AI：
        已经"缓冲跟不上"时，不允许把网络缓存改小。
        """
        采样 = dict(采样) if isinstance(采样, dict) else {}
        当前 = dict(当前参数) if isinstance(当前参数, dict) else {}
        媒体 = dict(媒体信息) if isinstance(媒体信息, dict) else {}
        self._计数("诊断次数")
        基线 = self._规则诊断(采样, 当前, 媒体)

        if not self.启用AI:
            return self._收尾诊断(基线)

        AI结果, 来源, 失败 = self._请求AI(
            _诊断系统提示, self._诊断用户消息(采样, 当前, 媒体, 基线), "卡顿诊断",
            _诊断期望键)
        if not AI结果:
            基线["理由"] = f"{基线['理由'][:-1]}（AI 未参与：{失败}）。"
            # 只在**规则也认为要调整**时留痕，避免"AI 没参与"这种过程噪音
            # 每次诊断都刷一条（用户实测：每秒一条，完全没法看）
            if 基线.get("需要调整"):
                self._日志(f"[播放顾问] 诊断回落规则：{失败 or '未配置 AI'}")
            return self._收尾诊断(基线)
        结果 = self._合并诊断(AI结果, 基线, 采样, 当前, 媒体)
        结果["来源"] = 来源
        self._计数("本地模型诊断" if 来源 == "本地模型" else "云端诊断")
        if 结果["需要调整"]:
            self._计数("诊断需调整")
        # 只有"真给出调整动作"才算重要决策：需要调整=False 完全不写
        if 结果["需要调整"]:
            self._日志(f"[AI 决策] {来源}诊断 → 调整动作："
                     f"{'；'.join(str(x) for x in 结果['动作'])}")
        return 结果

    def _收尾诊断(self, 结果: dict) -> dict:
        """规则路（无 AI / AI 失败）统一计数与留痕。"""
        self._计数("规则诊断")
        if 结果.get("需要调整"):
            self._计数("诊断需调整")
        if 结果.get("需要调整"):
            self._日志(f"[AI 决策] 规则诊断 → 调整动作："
                     f"{'；'.join(str(x) for x in (结果.get('动作') or []))}")
        return 结果

    # ---------------- 规则诊断 ----------------

    def _规则诊断(self, 采样: dict, 当前: dict, 媒体: dict) -> dict:
        """规则版诊断：丢帧/缓冲/CPU 三条线各自判断，动作写成人话。"""
        丢帧 = _整数(采样.get("丢帧"))
        解码丢帧 = _整数(采样.get("解码丢帧"))
        等待 = _整数(采样.get("缓冲等待次数"))
        已播 = _浮点(采样.get("已播秒"))
        缓冲秒 = _可选浮点(采样.get("当前缓冲秒"))
        硬解生效 = _布尔(采样.get("硬解生效"), True)
        CPU = _浮点(采样.get("CPU占用"))
        码率 = _浮点(采样.get("平均码率bps")) or _浮点(媒体.get("视频码率bps"))
        带宽 = _浮点(媒体.get("实测带宽bps"))
        档 = 分辨率档(媒体.get("分辨率") or 当前.get("分辨率"))

        分钟 = max(已播, 1.0) / 60.0
        丢帧速率 = 丢帧 / 分钟
        if 已播 >= 5:
            丢帧多 = 丢帧 >= 丢帧阈值 and 丢帧速率 >= 丢帧速率阈值
        else:
            # 刚起播的几秒样本太短，按速率算会放大成百上千，放宽成只看绝对值
            丢帧多 = 丢帧 >= 丢帧阈值 * 3
        缓冲紧张 = bool(等待 >= 缓冲等待阈值 or _正在缓冲(采样)
                    or (缓冲秒 is not None and 缓冲秒 < 缓冲低秒))

        动作: list[str] = []
        风险件: list[str] = []
        参数 = self._补全参数(当前)

        # ---- 1) 缓冲：网络供给不稳 → 加大缓存 + 多攒一点再开播 ----
        if 缓冲紧张:
            旧缓存 = _整数(参数.get("网络缓存毫秒"), 8000, 缓存下限毫秒, 缓存上限毫秒)
            新缓存 = min(缓存上限毫秒, max(旧缓存 + 4000, int(旧缓存 * 1.5)))
            新缓存 = _整数(round(新缓存 / 1000.0) * 1000, 旧缓存,
                        缓存下限毫秒, 缓存上限毫秒)
            if 新缓存 > 旧缓存:
                参数["网络缓存毫秒"] = 新缓存
                动作.append(f"加大网络缓存到 {新缓存}ms")
                缓存说明 = f"已把网络缓存加到 {新缓存}ms"
            else:
                # 已经拉满还卡 → 再加缓存没意义，只能从码率/片源上想办法
                动作.append(f"网络缓存已拉满 {旧缓存}ms，建议切 {_降一档(档)} "
                          f"或换低码率片源")
                缓存说明 = f"网络缓存已是上限 {旧缓存}ms 仍追不上供给"
            旧起播 = _浮点(参数.get("起播等待秒"))
            新起播 = round(min(10.0, 旧起播 + 2.0), 1)
            if 新起播 > 旧起播:
                参数["起播等待秒"] = 新起播
                动作.append(f"起播预缓冲提高到 {新起播:g} 秒")
            明细 = []
            if 等待:
                明细.append(f"缓冲等待 {等待} 次")
            if 缓冲秒 is not None:
                明细.append(f"当前缓冲仅 {缓冲秒:g} 秒")
            风险件.append("网络供给跟不上瞬时码率（" + "、".join(明细) +
                       f"），{缓存说明}")

        # ---- 2) 解码：硬解没生效 / CPU 吃紧 ----
        可用硬解 = self._可用硬解(媒体, 参数)
        if not 硬解生效:
            原硬解 = _文本(当前.get("硬解")) or "none"
            目标 = self._下一硬解(原硬解, 可用硬解)
            if 目标:
                参数["硬解"] = 目标
                if 目标 != 原硬解:
                    动作.append(f"强制硬解 {目标}")
                else:
                    动作.append(f"硬解 {目标} 未生效，显式指定 :avcodec-hw={目标} 重试")
                参数["附加选项"] = _同步缓存选项(
                    _同步硬解选项(参数["附加选项"], 目标),
                    参数["网络缓存毫秒"])
                风险件.append(f"硬解没生效（当前 {原硬解}），实际在软解，CPU 容易吃满")
            elif 可用硬解:
                # 列表明确只有 none：这台机器确实没有硬解可换
                动作.append("本机没有可用硬解，只能软解")
                风险件.append("没有硬解可用，高码率/4K 只能靠 CPU 硬扛，"
                           "建议降低画质或换片源")
            else:
                # 连列表都没给（媒体信息不全）：只能提醒，不能瞎猜一个后端
                动作.append("硬解未生效，请检查播放器硬解配置")
                风险件.append("硬解没生效，实际在软解，CPU 容易吃满")
        elif CPU >= CPU极高阈值:
            动作.append(f"CPU 占用 {CPU:.0%} 偏高，建议关闭其它程序或降低画质")
            风险件.append(f"硬解已生效但 CPU 仍占 {CPU:.0%}，"
                       f"可能是分辨率/码率超出这台机器的能力")

        if CPU >= CPU高阈值:
            # 反交错对逐行片源毫无收益却实打实吃 CPU，CPU 高时一律关掉
            动作.append("关闭反交错")
            参数["附加选项"] = _同步缓存选项(
                _加入选项(参数["附加选项"], ":deinterlace=-1"),
                参数["网络缓存毫秒"])

        # ---- 3) 丢帧：缓冲够还丢，就是码率超带宽或解码跟不上 ----
        if 丢帧多:
            if 缓冲紧张:
                风险件.append(f"已丢帧 {丢帧} 个，主因还是网络供给不足")
            elif 码率 > 0 and 带宽 > 0 and 码率 > 带宽 * 0.75:
                下一档 = _降一档(档)
                动作.append(f"码率 {码率 / 1e6:.1f}Mbps 接近/超过带宽 "
                          f"{带宽 / 1e6:.1f}Mbps，建议切 {下一档} 播放")
                风险件.append(f"带宽不足会持续丢帧，切到 {下一档} 后码率约降一半")
            else:
                动作.append("丢帧但缓冲充足：解码能力不足，建议开启硬解或降低画质")
                风险件.append(f"缓冲够却丢了 {丢帧} 帧（{丢帧速率:.0f} 帧/分钟），"
                           f"瓶颈在解码而不是网络")
        if 解码丢帧 >= 解码丢帧阈值 and 硬解生效:
            动作.append(f"解码丢帧 {解码丢帧} 个：解码器跟不上，"
                      f"可换其它硬解方式或降低画质")
            风险件.append("解码丢帧说明解码器输出跟不上，换硬解方式可能有效")

        新参数 = dict(参数)
        新参数["来源"] = "规则"
        新参数["可流畅播放"] = _布尔(参数.get("可流畅播放"), True)
        新参数["附加选项"] = _同步缓存选项(新参数["附加选项"],
                                  _整数(新参数.get("网络缓存毫秒"), 8000))
        风险 = ("；".join(风险件) + "。") if 风险件 else "暂无明显风险（保持当前参数观察）"
        新参数["风险"] = 风险 if 风险件 else "暂无明显风险"
        需要调整 = bool(动作)
        if 需要调整:
            改动说明 = "；".join(动作)
            理由 = (self._采样摘要(采样, 档) + " → 采取 " + str(len(动作)) +
                   " 项调整：" + "、".join(动作))
        else:
            改动说明 = "各项指标正常，维持当前参数"
            理由 = self._采样摘要(采样, 档) + " → 各项指标都在正常范围，保持当前参数"
        新参数["理由"] = self._采样摘要(采样, 档) + " → " + 改动说明
        return {
            "需要调整": 需要调整,
            "动作": 动作,
            "新参数": 新参数,
            "理由": 理由,
            "风险": 风险,
            "来源": "规则",
        }

    @staticmethod
    def _采样摘要(采样: dict, 档: str) -> str:
        """把采样拼成一行，用于理由/日志（用户看不懂 0.7 是 CPU 还是别的）。"""
        件 = []
        if _采样有(采样, "丢帧"):
            件.append(f"丢帧 {_整数(采样.get('丢帧'))}")
        if _采样有(采样, "解码丢帧"):
            件.append(f"解码丢帧 {_整数(采样.get('解码丢帧'))}")
        if _采样有(采样, "缓冲等待次数"):
            件.append(f"缓冲等待 {_整数(采样.get('缓冲等待次数'))} 次")
        if _采样有(采样, "当前缓冲秒"):
            件.append(f"当前缓冲 {_浮点(采样.get('当前缓冲秒')):g} 秒")
        if _采样有(采样, "已播秒"):
            件.append(f"已播 {_浮点(采样.get('已播秒')):g} 秒")
        if _采样有(采样, "平均码率bps"):
            件.append(f"平均码率 {_浮点(采样.get('平均码率bps')) / 1e6:.1f}Mbps")
        if _采样有(采样, "CPU占用"):
            件.append(f"CPU {_浮点(采样.get('CPU占用')):.0%}")
        if _采样有(采样, "硬解生效"):
            件.append("硬解" + ("生效" if _布尔(采样.get("硬解生效")) else "未生效"))
        if 档 != "未知":
            件.append(f"分辨率档 {档}")
        return "采样：" + "、".join(件) if 件 else "采样：无数据"

    @staticmethod
    def _可用硬解(媒体: dict, 参数: dict) -> list[str]:
        """本机可用硬解：优先媒体信息；没有就把当前参数里的硬件后端也算上。

        注意**不**把 ``none`` 当"可用硬件"塞进来：``none`` 只是"没有硬解"的
        代号，混进候选会让诊断误以为还有得换。
        """
        列表 = 媒体.get("本机硬解")
        if isinstance(列表, (list, tuple)) and 列表:
            return [_文本(x).lower() for x in 列表 if _文本(x)]
        当前 = _文本(参数.get("硬解")).lower()
        return [当前] if 当前 and 当前 != "none" else []

    @staticmethod
    def _下一硬解(当前值, 可用列表) -> str:
        """挑一个"和现在不同"的硬解方式；没有别的就重试当前的（至少显式指定）。"""
        当前 = _文本(当前值).lower() or "none"
        可用 = [_文本(x).lower() for x in (可用列表 or []) if _文本(x)]
        硬件 = [x for x in 可用 if x != "none"]
        if not 硬件:
            return ""
        for 项 in 硬件:
            if 项 != 当前:
                return 项
        # 只有当前这一种硬件：保留它，但调用方会显式指定 :avcodec-hw 重试
        return 当前 if 当前 in 硬件 else 硬件[0]

    # ---------------- AI 诊断合并 ----------------

    def _诊断用户消息(self, 采样: dict, 当前: dict, 媒体: dict,
                 基线: dict) -> str:
        """给模型看：采样 + 当前参数数值 + 媒体信息 + 规则结论。

        同样只给数值、不给现成文案（理由见 :meth:`_建议用户消息`）。
        """
        规则要旨 = {
            "需要调整": 基线.get("需要调整"),
            "动作": list(基线.get("动作") or []),
            "新参数": _参数要旨(基线.get("新参数") or {}),
        }
        return ("【采样】" + json.dumps(采样, ensure_ascii=False)
                + "\n【当前参数】" + json.dumps(_参数要旨(当前), ensure_ascii=False)
                + "\n【媒体信息】" + json.dumps(媒体, ensure_ascii=False)
                + "\n【规则基线】" + json.dumps(规则要旨, ensure_ascii=False)
                + "\n请按本次数据输出诊断 JSON。")

    def _合并诊断(self, AI结果: dict, 基线: dict, 采样: dict, 当前: dict,
              媒体: dict) -> dict:
        """AI 诊断并到规则诊断上；动作与参数改动必须自洽（改了什么就说什么）。"""
        结果 = dict(基线)
        可用硬解 = self._可用硬解(媒体, self._补全参数(当前))

        动作 = [_文本(x) for x in (AI结果.get("动作") or []) if _文本(x)] \
            if isinstance(AI结果.get("动作"), (list, tuple)) else []
        新参 = AI结果.get("新参数")
        合并 = 基线["新参数"]
        if isinstance(新参, dict) and 新参:
            合并 = self._归一化参数(新参, 基线["新参数"], 可用硬解)
            # 物理安全：缓冲已经不够了，不许把缓存改小
            if self._缓冲紧张(采样):
                底线 = _整数(基线["新参数"].get("网络缓存毫秒"), 缓存下限毫秒,
                          缓存下限毫秒, 缓存上限毫秒)
                if _整数(合并.get("网络缓存毫秒")) < 底线:
                    合并["网络缓存毫秒"] = 底线
                    合并["附加选项"] = _同步缓存选项(合并["附加选项"], 底线)
                    动作.append(f"网络缓存至少保持 {底线}ms（缓冲已不足）")
        差异 = _参数差异(合并, self._补全参数(当前))
        if not 动作:
            动作 = _合成动作(差异, self._补全参数(当前), 合并)
        结果["动作"] = 动作
        结果["新参数"] = 合并
        # 需要调整 = 有动作 or 参数真的变了（AI 说 true 却什么都没给 → 视为不用调）
        结果["需要调整"] = bool(动作) or bool(差异)
        理由 = _文本(AI结果.get("理由"))
        结果["理由"] = 理由 or 基线["理由"]
        风险 = _文本(AI结果.get("风险"))
        结果["风险"] = 风险 or 基线["风险"]
        return 结果

    @staticmethod
    def _缓冲紧张(采样: dict) -> bool:
        if not isinstance(采样, dict):
            return False
        缓冲秒 = _可选浮点(采样.get("当前缓冲秒"))
        等待 = _整数(采样.get("缓冲等待次数"))
        return bool(等待 >= 缓冲等待阈值 or _正在缓冲(采样)
                    or (缓冲秒 is not None and 缓冲秒 < 缓冲低秒))

    # ==================== 三、学习库读写 ====================

    def 记录效果(self, 键: str, 参数: dict, 指标: dict) -> None:
        """记一次播放结果：``参数`` 用了什么、``指标`` 结果如何。

        ``指标`` 里建议给 ``是否卡顿``；没给就按 ``缓冲等待次数/丢帧`` 自动判定。
        写库失败只写日志（磁盘满、库被占用都不能影响正在播的视频）。
        """
        参数字典 = 参数 if isinstance(参数, dict) else {}
        指标字典 = 指标 if isinstance(指标, dict) else {}
        卡顿 = 判定卡顿(指标字典)
        成功 = self._库.记录(键, 参数字典, 指标字典)
        self._计数("记录尝试")
        if 成功:
            self._计数("记录条数")
            self._计数("记录卡顿" if 卡顿 else "记录流畅")
            self._日志(f"[播放顾问] 已记录学习：{_文本(键)} → "
                     f"{'卡顿' if 卡顿 else '流畅'}"
                     f"（缓存 {_整数(参数字典.get('网络缓存毫秒'))}ms、"
                     f"硬解 {_文本(参数字典.get('硬解')) or '未知'}）")
        else:
            self._计数("记录失败")
            self._日志(f"[播放顾问] 记录学习失败（键={_文本(键) or '空'}）")

    def 获取学习记录(self, 键: str = "") -> list[dict]:
        """读学习记录（新→旧）；``键`` 为空返回全部。"""
        return self._库.查询(键, 上限=200)

    def 获取统计(self) -> dict:
        """学习库统计 + 本次运行的建议/诊断计数（界面"AI 播放"页直接展示）。"""
        统计 = dict(self._库.统计())
        with self._计数锁:
            计数 = dict(self._计数表)
        统计.update({
            "库路径": self._库.路径,
            "建议次数": 计数.get("建议次数", 0),
            "学习库命中次数": 计数.get("学习库命中", 0),
            "本地模型建议次数": 计数.get("本地模型建议", 0),
            "云端建议次数": 计数.get("云端建议", 0),
            "规则建议次数": 计数.get("规则建议", 0),
            "规则回退次数": 计数.get("规则回退", 0),
            "AI异常次数": 计数.get("AI异常", 0),
            "AI解析失败次数": 计数.get("AI解析失败", 0),
            "AI结论矛盾次数": 计数.get("AI结论矛盾", 0),
            "诊断次数": 计数.get("诊断次数", 0),
            "规则诊断次数": 计数.get("规则诊断", 0),
            "本地模型诊断次数": 计数.get("本地模型诊断", 0),
            "云端诊断次数": 计数.get("云端诊断", 0),
            "诊断需调整次数": 计数.get("诊断需调整", 0),
            "记录条数": 计数.get("记录条数", 0),
            "记录卡顿条数": 计数.get("记录卡顿", 0),
            "记录流畅条数": 计数.get("记录流畅", 0),
        })
        return 统计

    # ---------------- 参数补全/比较 ----------------

    @staticmethod
    def _补全参数(参数: dict) -> dict:
        """把"只改了部分键"的参数补成全量（播放核心按全量取值）。"""
        参数 = dict(参数) if isinstance(参数, dict) else {}
        基线 = {
            "网络缓存毫秒": 8000, "起播等待秒": 0.0, "硬解": "none",
            "附加选项": [], "可流畅播放": True, "风险": "", "理由": "",
            "来源": "规则",
        }
        结果 = dict(参数)
        结果["网络缓存毫秒"] = _整数(参数.get("网络缓存毫秒"),
                            基线["网络缓存毫秒"], 缓存下限毫秒, 缓存上限毫秒)
        结果["起播等待秒"] = round(min(20.0, max(0.0, _浮点(
            参数.get("起播等待秒"), 基线["起播等待秒"]))), 1)
        结果["硬解"] = _文本(参数.get("硬解")) or 基线["硬解"]
        # 选项是"当前参数"的一部分：硬解、缓存变了就必须显式跟过去，
        # 否则播放核心可能同时拿到两条互相打架的 :option
        结果["附加选项"] = _同步硬解选项(_归一化选项(参数.get("附加选项")),
                                结果["硬解"])
        结果["可流畅播放"] = _布尔(参数.get("可流畅播放"), True)
        结果["风险"] = _文本(参数.get("风险"))
        结果["理由"] = _文本(参数.get("理由"))
        结果["来源"] = _文本(参数.get("来源")) or 基线["来源"]
        return 结果


def _参数要旨(参数: dict) -> dict:
    """只留"会影响播放"的键（给模型看、给日志看）。

    省 token 是次要的，主要目的是**别把现成的风险/理由文案喂给模型**：
    小模型看到整句就会原样抄回来，抄回来的句子往往和本次数据对不上。
    """
    参数 = 参数 if isinstance(参数, dict) else {}
    return {键: 参数[键] for 键 in ("网络缓存毫秒", "起播等待秒", "硬解",
                                "附加选项", "可流畅播放") if 键 in 参数}


def _采样有(采样: dict, 键: str) -> bool:
    """采样里到底有没有这个键（0 和"没给"必须区分）。"""
    return isinstance(采样, dict) and 键 in 采样 and _文本(采样.get(键)) != ""


def _参数差异(新: dict, 旧: dict) -> list[str]:
    """只比"会影响播放"的四个键；风险/理由这类文案不算改动。"""
    差异 = []
    for 键 in ("网络缓存毫秒", "起播等待秒", "硬解", "附加选项"):
        if 新.get(键) != 旧.get(键):
            差异.append(键)
    return 差异


def _合成动作(差异: list[str], 旧: dict, 新: dict) -> list[str]:
    """AI 只改了参数没写动作时，替它把人话动作补齐（界面得有话说）。"""
    动作 = []
    for 键 in 差异:
        if 键 == "网络缓存毫秒":
            动作.append(f"网络缓存 {旧.get('网络缓存毫秒')}ms → {新.get('网络缓存毫秒')}ms")
        elif 键 == "起播等待秒":
            动作.append(f"起播预缓冲 {旧.get('起播等待秒'):g} 秒 → {新.get('起播等待秒'):g} 秒")
        elif 键 == "硬解":
            动作.append(f"硬解 {_文本(旧.get('硬解')) or 'none'} → "
                      f"{_文本(新.get('硬解')) or 'none'}")
        elif 键 == "附加选项":
            动作.append("更新 libvlc 附加选项")
    return 动作
