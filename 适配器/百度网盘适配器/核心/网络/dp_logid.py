# 百度网盘适配器/核心/网络/dp_logid.py
"""
客户端追踪 ID 生成器
作用：生成百度网盘接口所需的 `dp-logid` 与 `logid` 两个追踪参数

两种 ID 的格式均由 HAR 逆向实测得出（见 PLAN.md §4.4）：

1. `dp-logid` —— 几乎所有 pan.baidu.com 业务接口都带
   格式：<16 位会话前缀><4 位递增序号>
   实测证据：同一页面加载内前 16 位恒定（22 个不同前缀 = 22 次页面加载），
             后 4 位从 0001 起随每次请求递增（观测范围 1..316）。
   样例：2611190077238122 | 0004
         9730890010474785 | 0001

2. `logid` —— 仅 superfile2 分片上传使用
   格式：base64("<15 位数字>.<17 位随机数字>")
   实测证据：MTc4OTMyNTQwNzUyODAuMTY2NjkzMDc5ODkyODU5NDI=
             → "17893254075280.16669307989285942"
   其中第一段是「毫秒时间戳 + 1 位随机数」，第二段是 17 位随机数。

线程安全：`dp-logid` 的序号由 `threading.Lock` 保护，多线程上传时不会重号。
"""
from __future__ import annotations

import base64
import logging
import random
import threading
import time

logger = logging.getLogger("百度网盘.网络.追踪")

# 序号位宽：实测为 4 位十进制（0001..9999）
_序号位宽 = 4
_序号上限 = 10 ** _序号位宽 - 1


class DpLogId生成器:
    """生成 `<16位前缀><4位序号>` 形式的 dp-logid。

    前缀在实例创建时随机生成一次，此后整个会话复用——与浏览器
    「同一页面加载内前缀恒定」的行为一致。
    """

    def __init__(self, 前缀: str | None = None):
        self.前缀 = 前缀 or self._随机前缀()
        self._序号 = 0
        self._锁 = threading.Lock()

    @staticmethod
    def _随机前缀() -> str:
        """16 位纯数字前缀（实测样本均为数字串）。"""
        return "".join(random.choice("0123456789") for _ in range(16))

    def 下一个(self) -> str:
        """返回一个新的 dp-logid，线程安全。"""
        with self._锁:
            self._序号 += 1
            if self._序号 > _序号上限:
                # 溢出后回绕，并换一个新前缀，避免与服务端已见序号冲突
                self.前缀 = self._随机前缀()
                self._序号 = 1
            序号 = self._序号
        return f"{self.前缀}{序号:0{_序号位宽}d}"

    @property
    def 当前序号(self) -> int:
        with self._锁:
            return self._序号


def 生成logid() -> str:
    """生成 superfile2 用的 `logid`：base64("<时间戳><随机位>.<17位随机数>")。

    实测样例解码后为 `17893254075280.16669307989285942`：
    第一段 14 位 ≈ 毫秒时间戳(13位) + 1 位随机；此处按同样形态构造。
    """
    第一段 = f"{int(time.time() * 1000)}{random.randint(0, 9)}"
    第二段 = str(random.randint(10 ** 16, 10 ** 17 - 1))
    原文 = f"{第一段}.{第二段}"
    return base64.b64encode(原文.encode("ascii")).decode("ascii")


# 全局单例：整个进程共用一个前缀与序号序列
全局DpLogId = DpLogId生成器()


def 下一个dp_logid() -> str:
    """便捷函数：取一个全局序列的 dp-logid。"""
    return 全局DpLogId.下一个()
