# 光鸭云盘适配器/核心/接口/杂项接口.py
"""
杂项接口
包含：全局配置、首页横幅、未读消息、分享、云收集、回收站
"""

from ..网络.网络客户端 import 网络客户端


class 杂项接口:
    """杂项接口"""

    def __init__(self, 网络: 网络客户端):
        self.网络 = 网络

    def _请求(self, 路径: str, 请求体: dict | None = None) -> dict:
        请求体 = dict(请求体 or {})
        请求体.setdefault("clientId", self.网络.客户端标识)
        响应数据 = self.网络.请求("POST", 路径, json数据=请求体)
        return 响应数据.get("data", 响应数据)

    def 取全局配置(self) -> dict:
        return self._请求("/misc/v1/get_global_config")

    def 取横幅列表(self, 参数: dict | None = None) -> dict:
        return self._请求("/misc/v1/get_banner_list", 参数 or {})

    def 取未读消息数(self) -> dict:
        return self._请求("/misc/v1/get_inapp_msg_unread_count")

    def 取分享列表(self, page: int = 0, pageSize: int = 50) -> dict:
        return self._请求("/userres/v1/get_share_list",
                        {"page": page, "pageSize": pageSize})

    def 取云收集任务列表(self, page: int = 0, pageSize: int = 50) -> dict:
        return self._请求("/cloudcollection/v1/list_task",
                        {"page": page, "pageSize": pageSize})