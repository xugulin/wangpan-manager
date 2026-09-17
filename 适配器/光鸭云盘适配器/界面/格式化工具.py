# 光鸭云盘适配器/界面/格式化工具.py
"""通用格式化工具

  - 文件大小：字节 → B/KB/MB/GB/TB/PB
  - 时长：秒 → 天/时/分/秒
  - 时间戳：秒/毫秒 → "YYYY-MM-DD HH:MM:SS"
  - 默认新建文件夹名
  - 手机号规范化
"""

import datetime
import time

from 核心.认证.登录服务 import 默认国家码


# 文件夹名非法字符（Windows/云端通用）
非法字符集 = set('\\/:*?"<>|')


def 格式化大小(字节) -> str:
    if 字节 is None:
        return "0 B"
    try:
        数值 = int(字节)
    except (TypeError, ValueError):
        return str(字节)
    符号 = "-" if 数值 < 0 else ""
    值 = abs(数值)
    if 值 < 1024:
        return f"{符号}{值} B"
    if 值 < 1024 ** 2:
        return f"{符号}{值 / 1024:.2f} KB"
    if 值 < 1024 ** 3:
        return f"{符号}{值 / 1024 ** 2:.2f} MB"
    if 值 < 1024 ** 4:
        return f"{符号}{值 / 1024 ** 3:.2f} GB"
    if 值 < 1024 ** 5:
        return f"{符号}{值 / 1024 ** 4:.2f} TB"
    return f"{符号}{值 / 1024 ** 5:.2f} PB"


def 格式化时长(秒) -> str:
    try:
        秒 = int(秒)
    except (TypeError, ValueError):
        return str(秒)
    if 秒 < 0:
        return "已过期"
    天 = 秒 // 86400
    小时 = (秒 % 86400) // 3600
    分钟 = (秒 % 3600) // 60
    部分 = []
    if 天 > 0:
        部分.append(f"{天} 天")
    if 小时 > 0:
        部分.append(f"{小时} 小时")
    if 分钟 > 0 and 天 == 0:
        部分.append(f"{分钟} 分")
    if not 部分:
        部分.append(f"{秒} 秒")
    return " ".join(部分)


def 格式化时间戳(值) -> str:
    try:
        数值 = float(值)
        if 数值 > 1e12:
            数值 /= 1000
        return time.strftime(
            "%Y-%m-%d %H:%M:%S", time.localtime(数值))
    except Exception:
        return str(值)


def 默认新文件夹名() -> str:
    """对齐光鸭的默认命名：新建文件夹-YYYYMMDDHHMMSSmmm"""
    现在 = datetime.datetime.now()
    时间串 = (现在.strftime("%Y%m%d%H%M%S")
             + f"{现在.microsecond // 1000:03d}")
    return f"新建文件夹-{时间串}"


def 规范化手机号(输入: str) -> str:
    """把用户输入的手机号规范化为 '+86 18512828303' 格式"""
    数字 = "".join(ch for ch in (输入 or "") if ch.isdigit())
    if not 数字:
        return ""
    if 数字.startswith("0086"):
        号码 = 数字[4:]
    elif 数字.startswith("86") and len(数字) >= 13:
        号码 = 数字[2:]
    else:
        号码 = 数字
    国家码数字 = 默认国家码.lstrip("+")
    return f"+{国家码数字} {号码}"