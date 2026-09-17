# 光鸭云盘适配器/核心/接口/埋点签名.py
"""
埋点签名
"""
import hashlib


def 计算埋点签名(data_list: str, 时间戳: int,
                 appid: int, 密钥: str) -> str:
    """推测算法：sha1(data_list + t + appid + 密钥)"""
    待签名 = f"{data_list}{时间戳}{appid}{密钥}"
    return hashlib.sha1(待签名.encode("utf-8")).hexdigest()