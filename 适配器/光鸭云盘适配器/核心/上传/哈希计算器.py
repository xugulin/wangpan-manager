"""
哈希计算器
作用：计算文件 MD5（用于秒传判断）
抓包中上传前会带上 fileSize 和 md5，服务端据此判断是否已有该文件
"""

import hashlib
from pathlib import Path


def 计算文件MD5(文件路径: str | Path, 进度回调=None) -> str:
    """
    分块计算文件 MD5，避免一次性读取大文件撑爆内存
    :param 文件路径: 文件路径
    :param 进度回调: 可选，形如 回调(已读字节, 总字节)
    :return: MD5 十六进制字符串（小写）
    """
    路径 = Path(文件路径)
    总大小 = 路径.stat().st_size
    哈希 = hashlib.md5()
    已读 = 0
    分块大小 = 2 * 1024 * 1024  # 2MB

    with 路径.open("rb") as 文件对象:
        while True:
            数据块 = 文件对象.read(分块大小)
            if not 数据块:
                break
            哈希.update(数据块)
            已读 += len(数据块)
            if 进度回调:
                进度回调(已读, 总大小)

    return 哈希.hexdigest()