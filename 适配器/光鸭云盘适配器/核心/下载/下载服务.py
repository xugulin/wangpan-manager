"""
下载服务
作用：调用后端拿签名下载地址，再下载文件到本地
对应抓包：POST /userres/v1/get_res_download_url
"""

from pathlib import Path

import httpx

from ..网络.网络客户端 import 网络客户端


class 下载服务:
    """下载服务"""

    def __init__(self, 网络: 网络客户端):
        self.网络 = 网络

    def 取下载地址(self, fileId: str) -> dict:
        """
        取下载地址
        返回：{ signedURL, urlDuration, speedupSignature, requestId }
        """
        响应数据 = self.网络.请求(
            "POST",
            "/userres/v1/get_res_download_url",
            json数据={"fileId": fileId},
        )
        return 响应数据["data"]

    def 下载到文件(
        self,
        fileId: str,
        保存路径: str | Path,
        进度回调=None,
    ) -> Path:
        """
        下载文件到指定路径
        :param 进度回调: 可选，形如 回调(已下载字节, 总字节)
        """
        信息 = self.取下载地址(fileId)
        地址 = 信息["signedURL"]
        保存 = Path(保存路径)
        保存.parent.mkdir(parents=True, exist_ok=True)

        with httpx.stream("GET", 地址, follow_redirects=True, timeout=60.0) as 响应:
            响应.raise_for_status()
            总大小 = int(响应.headers.get("content-length", 0))
            已下载 = 0
            with 保存.open("wb") as 文件对象:
                for 数据块 in 响应.iter_bytes(chunk_size=64 * 1024):
                    文件对象.write(数据块)
                    已下载 += len(数据块)
                    if 进度回调 and 总大小:
                        进度回调(已下载, 总大小)

        return 保存