# v8_3/播放/直链探测.py
"""起播前实测直链性能：TTFB、可持续带宽、是否支持 Range、是否能拉多连接。

为什么必须"实测"而不是"猜"
==========================
网盘直链的可用带宽波动很大（会员/限速/时段/节点都影响）。决定"能不能流畅播 4K"
的唯一可靠依据是：**现在这条链接到底能跑多少 Mbps**。做法：

1. 用 ``Range: bytes=0-N`` 取一小段（默认 2 MiB），量首字节时间（TTFB）；
2. 再取一段（默认 4 MiB）量稳态吞吐 → 换算成 bps；
3. 顺便确认服务端是否回 206（支持 Range = 能拖动进度、能多段预读）；
4. 全程带超时与清理，失败不影响播放（返回"未知"，参数走保守档）。

注意：探测会消耗一点流量（默认合计约 6 MiB），可以在配置里关掉。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

try:
    import httpx
except Exception:  # pragma: no cover - V8_3 已有 httpx
    httpx = None  # type: ignore

logger = logging.getLogger(__name__)

__all__ = ["探测结果", "探测直链", "格式化带宽"]

#: 探测用默认尺寸
首段字节 = 2 * 1024 * 1024
次段字节 = 4 * 1024 * 1024


@dataclass
class 探测结果:
    成功: bool = False
    首字节毫秒: float = 0.0
    实测带宽bps: float = 0.0
    range支持: bool = False
    状态码: int = 0
    内容类型: str = ""
    内容长度: int = 0          # 服务端声明的总长度（0=未知）
    已读字节: int = 0
    用时秒: float = 0.0
    错误: str = ""
    #: 非空表示这条"探测结果"是**合成**的（例如本地文件不走网络），摘要直接
    #: 显示它，避免界面上出现"直链探测失败"这种误导性提示。
    来源说明: str = ""

    def to_dict(self) -> dict:
        return {
            "成功": self.成功, "首字节毫秒": round(self.首字节毫秒, 1),
            "实测带宽bps": int(self.实测带宽bps), "range支持": self.range支持,
            "状态码": self.状态码, "内容类型": self.内容类型,
            "内容长度": self.内容长度, "已读字节": self.已读字节,
            "用时秒": round(self.用时秒, 2), "错误": self.错误,
            "来源说明": self.来源说明,
        }

    def 摘要(self) -> str:
        if self.来源说明:
            return self.来源说明
        if not self.成功:
            return f"直链探测失败（{self.错误 or '未知'}）"
        return (f"实测 {格式化带宽(self.实测带宽bps)}"
                f" · 首字节 {self.首字节毫秒:.0f}ms"
                f" · Range {'✅' if self.range支持 else '❌'}")


def 格式化带宽(bps: float) -> str:
    值 = float(bps or 0)
    if 值 <= 0:
        return "未知"
    if 值 >= 1_000_000_000:
        return f"{值 / 1_000_000_000:.2f} Gbps"
    if 值 >= 1_000_000:
        return f"{值 / 1_000_000:.1f} Mbps"
    if 值 >= 1_000:
        return f"{值 / 1_000:.0f} kbps"
    return f"{值:.0f} bps"


def _读一段(客户端, 地址: str, 头: dict, 起: int, 数量: int,
          超时秒: float) -> tuple[int, float, int]:
    """读 [起, 起+数量) 字节；返回 (已读字节, 用时秒, TTFB毫秒)。"""
    请求头 = dict(头 or {})
    请求头["Range"] = f"bytes={起}-{起 + 数量 - 1}"
    开始 = time.time()
    首字节 = 0.0
    已读 = 0
    with 客户端.stream("GET", 地址, headers=请求头,
                     timeout=httpx.Timeout(connect=5.0, read=超时秒,
                                           write=10.0, pool=10.0)) as 响应:
        if 响应.status_code not in (200, 206):
            raise RuntimeError(f"HTTP {响应.status_code}")
        for 块 in 响应.iter_bytes(chunk_size=256 * 1024):
            if not 首字节:
                首字节 = (time.time() - 开始) * 1000
            已读 += len(块)
            if 已读 >= 数量:
                break
    用时 = max(1e-6, time.time() - 开始)
    return 已读, 用时, 首字节


def 探测直链(地址: str, 请求头: dict | None = None, *,
           首段: int = 首段字节, 次段: int = 次段字节,
           超时秒: float = 12.0, 日志回调=None) -> 探测结果:
    """实测直链的 TTFB 与带宽。任何异常都收成 ``成功=False``。"""
    结果 = 探测结果()
    if httpx is None:
        结果.错误 = "缺少 httpx"
        return 结果
    if not 地址:
        结果.错误 = "空地址"
        return 结果
    基础头 = dict(请求头 or {})
    客户端 = httpx.Client(follow_redirects=True, http2=False)
    try:
        开始 = time.time()
        已读1, 用时1, 首字节 = _读一段(客户端, 地址, 基础头, 0, 首段, 超时秒)
        结果.已读字节 = 已读1
        结果.首字节毫秒 = 首字节
        结果.成功 = 已读1 > 0
        if 已读1 < 首段:
            # 读不满：可能是短文件或提前结束，按实读算
            结果.用时秒 = time.time() - 开始
            结果.实测带宽bps = 已读1 * 8 / max(1e-6, 用时1)
            return 结果
        # 第二段用来量稳态（避开 TCP 慢启动）
        已读2, 用时2, _ = _读一段(客户端, 地址, 基础头, 首段, 次段,
                                超时秒)
        结果.已读字节 += 已读2
        结果.用时秒 = time.time() - 开始
        if 已读2 > 0:
            结果.实测带宽bps = 已读2 * 8 / max(1e-6, 用时2)
        else:
            结果.实测带宽bps = 已读1 * 8 / max(1e-6, 用时1)
        return 结果
    except Exception as e:  # noqa: BLE001
        结果.错误 = f"{type(e).__name__}: {str(e)[:120]}"
        结果.成功 = 结果.已读字节 > 0
        return 结果
    finally:
        try:
            客户端.close()
        except Exception:
            pass


def 探测头(地址: str, 请求头: dict | None = None,
         超时秒: float = 8.0) -> dict:
    """只发一次带 Range 的小请求，拿状态码/Content-Type/Content-Length。

    用于确认"是否支持 Range（能拖进度）"与"服务端声明的长度"。
    """
    if httpx is None:
        return {}
    请求头 = dict(请求头 or {})
    请求头.setdefault("Range", "bytes=0-1023")
    try:
        with httpx.Client(follow_redirects=True, http2=False) as 客户端:
            响应 = 客户端.get(地址, headers=请求头, timeout=超时秒)
            长度 = 0
            范围 = 响应.headers.get("content-range") or ""
            if "/" in 范围:
                try:
                    长度 = int(范围.rsplit("/", 1)[1])
                except Exception:
                    长度 = 0
            if not 长度:
                try:
                    长度 = int(响应.headers.get("content-length") or 0)
                except Exception:
                    长度 = 0
            return {"状态码": 响应.status_code,
                    "内容类型": 响应.headers.get("content-type") or "",
                    "内容长度": 长度,
                    "range支持": 响应.status_code == 206}
    except Exception as e:  # noqa: BLE001
        return {"错误": f"{type(e).__name__}: {str(e)[:80]}"}
