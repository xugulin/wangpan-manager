# 光鸭云盘适配器/核心/埋点/埋点上报.py
"""
埋点上报
接口：POST https://analysis-rcv.guangyapan.com/sync_data

请求格式：
  Content-Type: application/x-www-form-urlencoded

表单字段：
  data_list  Gzip 压缩后再 Base64 编码的埋点数据（H4sI... 开头）
  gzip       固定 "1"，表示 data_list 启用了 gzip 压缩
  appid      固定 "1"
  t          毫秒级时间戳
  sign       签名 = sha1(data_list + t + appid + 密钥)

注意：
  此接口是单向埋点上报，服务器返回 200 OK 即视为成功，
  不返回有业务含义的数据。
"""
import base64
import gzip
import json
import logging
import time
from typing import Dict, List

import httpx

from ..接口.埋点签名 import 计算埋点签名

logger = logging.getLogger("光鸭云盘.埋点")

埋点地址 = "https://analysis-rcv.guangyapan.com/sync_data"
默认应用ID = 1
默认密钥 = ""   # 光鸭埋点密钥需逆向获取；为空时上报通常被服务端忽略


class 埋点上报:
    def __init__(self, 应用ID: int = 默认应用ID, 密钥: str = 默认密钥,
                 上报地址: str = 埋点地址):
        self.应用ID = 应用ID
        self.密钥 = 密钥
        self.上报地址 = 上报地址
        self.客户端 = httpx.Client(timeout=15.0, http2=False)

    @staticmethod
    def _压缩并编码(事件列表: List[Dict]) -> str:
        """JSON → gzip → base64（字符串）"""
        JSON字节 = json.dumps(事件列表, ensure_ascii=False).encode("utf-8")
        压缩字节 = gzip.compress(JSON字节)
        return base64.b64encode(压缩字节).decode("utf-8")

    def 上报(self, 事件列表: List[Dict]) -> Dict:
        """上报一批事件

        :param 事件列表: 形如 [{"event": "xxx", "props": {...}}, ...]
        """
        数据列表 = self._压缩并编码(事件列表)
        时间戳 = int(time.time() * 1000)
        签名 = 计算埋点签名(
            数据列表, 时间戳, self.应用ID, self.密钥)

        表单 = {
            "data_list": 数据列表,
            "gzip": "1",
            "appid": str(self.应用ID),
            "t": str(时间戳),
            "sign": 签名,
        }
        logger.debug(
            f"[埋点] 事件数={len(事件列表)} "
            f"时间戳={时间戳} sign={签名}")

        响应 = self.客户端.post(
            self.上报地址,
            data=表单,
            headers={
                "Content-Type":
                    "application/x-www-form-urlencoded",
                "referer": "https://www.guangyapan.com/",
                "user-agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/151.0.0.0 Safari/537.36"),
            },
        )
        响应.raise_for_status()
        logger.info(
            f"[埋点] 上报成功，状态码={响应.status_code}")
        return {"状态码": 响应.status_code}

    def 关闭(self) -> None:
        self.客户端.close()