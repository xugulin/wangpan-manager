# 光鸭云盘适配器/核心/接口/任务接口.py
"""
任务相关接口
参考抓包（2026-09-13）：
  POST /userres/v1/get_task_status
  请求体：{"taskId":"1946179562994634836"}
  响应：{"msg":"success","data":{"status":2}}

status 含义推测（基于观察）：
  0/1 = 处理中
  2   = 已完成
  其他 = 失败/未知
"""
import logging
import time

from ..网络.网络客户端 import 网络客户端

logger = logging.getLogger("光鸭云盘.任务")


class 任务接口:
    """任务接口"""

    def __init__(self, 网络: 网络客户端):
        self.网络 = 网络

    def 取任务状态(self, taskId: str) -> dict:
        """POST /userres/v1/get_task_status"""
        响应数据 = self.网络.请求(
            "POST",
            "/userres/v1/get_task_status",
            json数据={"taskId": taskId},
        )
        return 响应数据.get("data", 响应数据)

    def 等待任务完成(
        self,
        taskId: str,
        最长等待秒: float = 60.0,
        轮询间隔秒: float = 1.0,
        进度回调=None,
    ) -> bool:
        """轮询任务状态直到完成

        :param 进度回调: 形如 回调(status:int, 已等待秒:float)
        :return: True 表示完成，False 表示超时
        """
        开始 = time.time()
        while time.time() - 开始 < 最长等待秒:
            try:
                数据 = self.取任务状态(taskId)
                状态 = 数据.get("status") if isinstance(数据, dict) else None
                if 进度回调:
                    进度回调(状态, time.time() - 开始)
                if 状态 == 2:
                    return True
            except Exception as e:
                logger.debug(f"[任务] 查询状态异常：{e}")
            time.sleep(轮询间隔秒)
        return False