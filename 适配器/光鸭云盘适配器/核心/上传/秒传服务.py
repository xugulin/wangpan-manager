"""
秒传服务
作用：调用上传中心的 token 接口、闪电上传检查接口
对应抓包接口：
  POST /userres/v2/get_res_center_token
  POST /userres/v1/check_can_flash_upload
"""

from dataclasses import dataclass
from typing import Optional

from ..网络.网络客户端 import 网络客户端


@dataclass
class 上传凭证:
    """STS 临时凭证"""
    accessKeyID: str
    secretAccessKey: str
    sessionToken: str
    expiration: str


@dataclass
class 上传令牌:
    """上传中心返回的 token 结构"""
    gcid: str
    provider: int
    taskId: str
    creds: Optional[上传凭证] = None
    endPoint: Optional[str] = None
    bucketName: Optional[str] = None
    objectPath: Optional[str] = None
    region: Optional[str] = None

    @classmethod
    def 从字典(cls, 数据: dict) -> "上传令牌":
        凭证数据 = 数据.get("creds")
        凭证 = None
        if 凭证数据:
            凭证 = 上传凭证(
                accessKeyID=凭证数据["accessKeyID"],
                secretAccessKey=凭证数据["secretAccessKey"],
                sessionToken=凭证数据["sessionToken"],
                expiration=凭证数据["expiration"],
            )
        return cls(
            gcid=数据["gcid"],
            provider=数据.get("provider", 0),
            taskId=数据["taskId"],
            creds=凭证,
            endPoint=数据.get("endPoint"),
            bucketName=数据.get("bucketName"),
            objectPath=数据.get("objectPath"),
            region=数据.get("region"),
        )


@dataclass
class 取令牌结果:
    """取令牌结果"""
    是否秒传: bool
    令牌: 上传令牌


class 秒传服务:
    """秒传服务"""

    def __init__(self, 网络: 网络客户端):
        self.网络 = 网络

    def 取上传令牌(
        self,
        文件名: str,
        文件大小: int,
        MD5值: str,
        父目录ID: str,
        通道数: int = 30,
    ) -> 取令牌结果:
        """
        取上传中心 token
        抓包观察：返回 code=156、msg="上传已完成" 时表示秒传命中
        """
        请求体 = {
            "capacity": 通道数,
            "name": 文件名,
            "res": {"fileSize": 文件大小, "md5": MD5值},
            "parentId": 父目录ID,
        }
        响应数据 = self.网络.请求(
            "POST",
            "/userres/v2/get_res_center_token",
            json数据=请求体,
        )
        是否秒传 = 响应数据.get("code") == 156
        令牌 = 上传令牌.从字典(响应数据["data"])
        return 取令牌结果(是否秒传=是否秒传, 令牌=令牌)

    def 检查是否可闪电上传(
        self,
        taskId: str,
        gcid: str,
        cid: str,
    ) -> bool:
        """
        对应抓包：POST /userres/v1/check_can_flash_upload
        """
        响应数据 = self.网络.请求(
            "POST",
            "/userres/v1/check_can_flash_upload",
            json数据={"taskId": taskId, "gcid": gcid, "cid": cid},
        )
        return bool(响应数据.get("data", {}).get("canFlashUpload"))