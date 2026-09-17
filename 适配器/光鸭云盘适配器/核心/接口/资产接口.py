# 光鸭云盘适配器/核心/接口/资产接口.py
"""
资产 / 权益 / 下载记录相关接口

参考已验证项目（2026-09-13）：
  POST /assets/v1/get_assets                 资产信息
  POST /nd.bizassets.s/v1/get_user_rights    用户权益

说明：
  光鸭没有独立的 "流量统计" 接口。
  直链流量字段（totalDirectLinkTraffic / freeDirectLinkTraffic 等）
  直接在 资产信息（get_assets）的返回中，因此不单独请求。

  同理，也没有独立的 /assets/v1/get_download_records 接口，
  下载记录属于「用户行为」范畴，通过 /userres/v1/get_user_action
  过滤 fileTypes 获取（已在 文件接口 中实现）。
"""

import logging

from ..网络.网络客户端 import 网络客户端

logger = logging.getLogger("光鸭云盘.资产")


class 资产接口:
    """资产接口"""

    def __init__(self, 网络: 网络客户端):
        self.网络 = 网络

    def _带客户端标识(self, 请求体: dict | None = None) -> dict:
        请求体 = dict(请求体 or {})
        请求体.setdefault("clientId", self.网络.客户端标识)
        return 请求体

    # ==================== 资产信息 ====================

    def 取资产信息(self) -> dict:
        """容量、会员、直链流量信息

        POST /assets/v1/get_assets
        返回字段包括：
          totalSpaceSize / usedSpaceSize / freeSpaceSize
          totalDirectLinkTraffic / freeDirectLinkTraffic
          vipStatus / vipLeftTime / vipExpireTime
          svipStatus / svipLeftTime / svipExpireTime
        """
        响应数据 = self.网络.请求(
            "POST",
            "/assets/v1/get_assets",
            json数据=self._带客户端标识(),
        )
        return 响应数据.get("data", 响应数据)

    # ==================== 用户权益 ====================

    def 取用户权益(self) -> dict:
        """POST /nd.bizassets.s/v1/get_user_rights"""
        响应数据 = self.网络.请求(
            "POST",
            "/nd.bizassets.s/v1/get_user_rights",
            json数据=self._带客户端标识(),
        )
        return 响应数据.get("data", 响应数据)

    # ==================== 便捷：从资产信息提取流量字段 ====================

    def 取流量统计(self) -> dict:
        """从资产信息里提取流量相关字段

        光鸭没有独立的流量统计接口。
        所有直链流量 / 分享访客流量字段都在 资产信息 里，
        这里做一层封装，方便主界面单独显示。
        """
        资产 = self.取资产信息()
        if not isinstance(资产, dict):
            return {}
        流量字段 = [
            "totalDirectLinkTraffic",
            "freeDirectLinkTraffic",
            "usedDirectLinkTraffic",
            "totalShareGuestTraffic",
            "freeShareGuestTraffic",
            "usedShareGuestTraffic",
        ]
        结果 = {k: 资产[k] for k in 流量字段 if k in 资产}
        return 结果 if 结果 else 资产