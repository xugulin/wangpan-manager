# 光鸭云盘适配器/核心/认证/认证服务.py
"""
认证服务
抓包确认：GET https://account.guangyapan.com/v1/user/me
"""
import logging

from .令牌仓库 import 全局令牌仓库
from ..网络.网络客户端 import 网络客户端, 账号基础地址

logger = logging.getLogger("光鸭云盘.认证")


class 认证服务:
    def __init__(self, 网络: 网络客户端 | None = None):
        self.网络 = 网络 or 网络客户端()

    def 取当前用户(self, 令牌: str | None = None) -> dict:
        """GET /v1/user/me（在账号域名下）"""
        令牌 = 令牌 or 全局令牌仓库.获取访问令牌()
        if not 令牌:
            raise RuntimeError("无有效访问令牌")
        self.网络.设置访问令牌(令牌)
        logger.debug("[认证] 取当前用户")
        结果 = self.网络.账号请求(
            "GET", "/v1/user/me",
            需要认证=True,
        )
        if isinstance(结果, dict) and "data" in 结果:
            return 结果["data"]
        return 结果

    def 关闭(self) -> None:
        self.网络.关闭()