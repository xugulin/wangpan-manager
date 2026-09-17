"""
追踪上下文
作用：生成符合 W3C Trace Context 标准的 traceparent 字符串
格式：00-{32位追踪ID}-{16位跨度ID}-01
对应抓包中每个请求都不同的 traceparent
"""

import os
import re
import secrets


def 生成追踪ID() -> str:
    """生成 32 位十六进制追踪ID"""
    return secrets.token_hex(16)


def 生成跨度ID() -> str:
    """生成 16 位十六进制跨度ID"""
    return secrets.token_hex(8)


def 生成追踪头(追踪ID: str | None = None) -> str:
    """
    生成完整的 traceparent 字符串
    :param 追踪ID: 可选，不传则自动生成
    """
    最终追踪ID = 追踪ID or 生成追踪ID()
    跨度ID = 生成跨度ID()
    return f"00-{最终追踪ID}-{跨度ID}-01"


_追踪头模式 = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-[0-9a-f]{2}$")


def 解析追踪头(追踪头: str) -> dict | None:
    """
    解析 traceparent 字符串
    :return: 形如 {"追踪ID": "...", "跨度ID": "..."}，解析失败返回 None
    """
    匹配 = _追踪头模式.match(追踪头)
    if not 匹配:
        return None
    return {"追踪ID": 匹配.group(1), "跨度ID": 匹配.group(2)}