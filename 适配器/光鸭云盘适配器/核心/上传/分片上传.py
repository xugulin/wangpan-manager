"""
分片上传
作用：使用 STS 临时凭证直传阿里云 OSS，完成后调用完成分片接口
对应抓包：
  PUT  https://{bucketName}.{endPoint}/{objectPath}?partNumber=N
  POST /userres/v1/multipart/complete
  POST /userres/v1/file/get_info_by_task_id
"""

import concurrent.futures
import threading
from pathlib import Path

import oss2
from oss2 import SizedFileAdapter, determine_part_size
from oss2.models import PartInfo

from .秒传服务 import 上传令牌
from ..网络.网络客户端 import 网络客户端


class 分片上传器:
    """分片上传器"""

    def __init__(self, 网络: 网络客户端):
        self.网络 = 网络

    def 上传(
        self,
        文件路径: str | Path,
        令牌: 上传令牌,
        分片大小: int = 1024 * 1024,
        并发数: int = 4,
        进度回调=None,
    ) -> None:
        """
        使用 STS 临时凭证直传阿里云 OSS
        :param 文件路径: 本地文件路径
        :param 令牌: 从秒传服务拿到的上传 token
        :param 分片大小: 每个分片大小，默认 1MB
        :param 并发数: 分片并发数
        :param 进度回调: 可选，形如 回调(已上传字节, 总字节)
        """
        if not 令牌.creds or not 令牌.endPoint or not 令牌.bucketName:
            raise ValueError("上传令牌缺少 STS 凭证或 OSS 地址")

        路径 = Path(文件路径)
        总大小 = 路径.stat().st_size

        # 使用 oss2 的临时凭证
        认证 = oss2.StsAuth(
            令牌.creds.accessKeyID,
            令牌.creds.secretAccessKey,
            令牌.creds.sessionToken,
        )
        端点 = 令牌.endPoint.replace("https://", "").replace("http://", "")
        存储桶 = oss2.Bucket(认证, 端点, 令牌.bucketName)

        # 初始化分片上传
        上传ID = 存储桶.init_multipart_upload(令牌.objectPath).upload_id
        分片总数 = (总大小 + 分片大小 - 1) // 分片大小

        已上传字节 = 0
        进度锁 = threading.Lock()
        分片结果列表: list[PartInfo] = []

        def 上传单个分片(分片序号: int) -> PartInfo:
            """上传第 N 片（从 1 开始）"""
            nonlocal 已上传字节
            起点 = (分片序号 - 1) * 分片大小
            终点 = min(起点 + 分片大小, 总大小)
            长度 = 终点 - 起点

            with 路径.open("rb") as 文件对象:
                文件对象.seek(起点)
                数据流 = SizedFileAdapter(文件对象, 长度)
                结果 = 存储桶.upload_part(
                    令牌.objectPath,
                    上传ID,
                    分片序号,
                    数据流,
                )

            with 进度锁:
                已上传字节 += 长度
                if 进度回调:
                    进度回调(已上传字节, 总大小)

            return PartInfo(分片序号, 结果.etag)

        # 用线程池并发上传
        with concurrent.futures.ThreadPoolExecutor(max_workers=并发数) as 线程池:
            任务列表 = [
                线程池.submit(上传单个分片, 序号)
                for 序号 in range(1, 分片总数 + 1)
            ]
            for 任务 in concurrent.futures.as_completed(任务列表):
                分片结果列表.append(任务.result())

        # 按分片序号排序
        分片结果列表.sort(key=lambda 项: 项.part_number)

        # 完成分片上传
        存储桶.complete_multipart_upload(令牌.objectPath, 上传ID, 分片结果列表)

        # 通知业务服务端
        self.网络.请求(
            "POST",
            "/userres/v1/multipart/complete",
            json数据={
                "taskId": 令牌.taskId,
                "gcid": 令牌.gcid,
                "parts": [
                    {"number": 项.part_number, "etag": 项.etag}
                    for 项 in 分片结果列表
                ],
            },
        )

    def 按任务ID取文件信息(self, taskId: str) -> dict:
        """
        对应抓包：POST /userres/v1/file/get_info_by_task_id
        """
        响应数据 = self.网络.请求(
            "POST",
            "/userres/v1/file/get_info_by_task_id",
            json数据={"taskId": taskId},
        )
        return 响应数据["data"]