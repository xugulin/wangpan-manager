"""统一异常模型。"""

from __future__ import annotations


class 适配器错误(RuntimeError):
    """适配器/桥接层错误。"""

    def __init__(self, message: str, *, 网盘: str = "", 命令: str = "",
                 原始: object = None):
        super().__init__(message)
        self.网盘 = 网盘
        self.命令 = 命令
        self.原始 = 原始

    def __str__(self) -> str:
        前缀 = f"[{self.网盘}/{self.命令}] " if self.网盘 or self.命令 else ""
        return f"{前缀}{super().__str__()}"


class 认证错误(适配器错误):
    """未登录或登录态失效。"""


class 任务取消(Exception):
    """任务被用户取消。"""


class 任务暂停(Exception):
    """任务被用户暂停（保留已下载的中转分片，继续时可断点续传）。"""
