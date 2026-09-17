# 夸克网盘适配器/核心/接口/任务接口.py
"""
任务接口（阶段二 2.8）—— 通用异步任务轮询器

夸克的异步操作（删除、移动、创建分享、转存…）都会先返回一个 `task_id`，
再通过统一的 `GET task` 查询进度：

    GET /1/clouddrive/task?task_id=<id>&retry_index=<n>

响应 data.status 语义（对齐 QuarkPan `share_service.py:141-147`）：

    0 = 排队中 (PENDING)
    1 = 进行中 (RUNNING)
    2 = 已完成 (DONE)
    3 = 失败   (FAILED)

data 里还会按操作类型附带结果字段，例如：
    分享创建 → share_id
    移动     → 无
    转存     → 无
以及 message（失败原因）、tq_gap（建议的下次轮询间隔毫秒）。
"""
import logging
import time
from typing import Any, Callable, Optional

from ..网络.网络客户端 import 网络客户端, 接口错误

logger = logging.getLogger("夸克网盘.任务")

# 任务状态
状态_排队 = 0
状态_进行中 = 1
状态_完成 = 2
状态_失败 = 3

状态名 = {0: "排队中", 1: "进行中", 2: "已完成", 3: "失败"}

默认最长等待秒 = 60.0
默认轮询间隔秒 = 1.0


class 任务错误(接口错误):
    """任务失败时抛出"""


class 任务接口:
    """通用任务轮询器"""

    def __init__(self, 网络: 网络客户端):
        self.网络 = 网络

    def 取任务状态(self, taskId: str, retryIndex: int = 0) -> dict:
        """GET task —— 查询一次任务状态

        :return: data 字典（含 status / message / 以及各操作的结果字段）
        """
        if not taskId:
            raise 接口错误("task_id 为空")
        响应 = self.网络.请求(
            "GET", "task",
            params={"task_id": str(taskId), "retry_index": int(retryIndex)})
        数据 = 响应.get("data") if isinstance(响应, dict) else None
        return 数据 if isinstance(数据, dict) else {}

    def 等待任务完成(
        self,
        taskId: str,
        最长等待秒: float = 默认最长等待秒,
        轮询间隔秒: float = 默认轮询间隔秒,
        进度回调: Optional[Callable[[int, dict], None]] = None,
        描述: str = "任务",
        不可重试关键字: tuple[str, ...] = (),
    ) -> dict:
        """轮询直到任务完成

        :param 进度回调: `回调(第几次, 最新data)`
        :param 不可重试关键字: 命中即立刻抛错（如容量不足），不再等超时
        :return: 完成时的 data 字典
        :raises 任务错误: 任务失败 / 超时
        """
        开始 = time.time()
        次数 = 0
        while time.time() - 开始 < 最长等待秒:
            数据 = self.取任务状态(taskId, 次数)
            次数 += 1
            if 进度回调:
                try:
                    进度回调(次数, 数据)
                except Exception:
                    pass

            状态 = 数据.get("status")
            消息 = str(数据.get("message") or "")

            if 状态 == 状态_完成:
                logger.debug(f"[任务] {描述} {taskId} 完成（第 {次数} 次）")
                return 数据
            if 状态 == 状态_失败:
                raise 任务错误(f"{描述}失败：{消息 or data_摘要(数据)}")

            if 消息:
                小写 = 消息.lower()
                if any(k in 消息 for k in 不可重试关键字) or \
                        "capacity limit" in 小写 or "容量不足" in 消息:
                    raise 任务错误(f"{描述}失败（不可重试）：{消息}")

            # 服务端建议的轮询间隔（毫秒），存在则采用
            建议 = 数据.get("tq_gap")
            间隔 = 轮询间隔秒
            try:
                if 建议:
                    间隔 = max(0.2, min(5.0, int(建议) / 1000.0))
            except (TypeError, ValueError):
                pass
            time.sleep(间隔)

        raise 任务错误(
            f"{描述}超时（{int(最长等待秒)} 秒，轮询 {次数} 次，task_id={taskId}）")


def data_摘要(数据: dict) -> str:
    """给日志/异常用的简短摘要"""
    if not isinstance(数据, dict):
        return str(数据)[:120]
    关注 = ("status", "message", "share_id", "fid", "task_id")
    片段 = {k: 数据.get(k) for k in 关注 if 数据.get(k) is not None}
    return str(片段 or 数据)[:200]
