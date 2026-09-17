# 百度网盘适配器/核心/接口/任务接口.py
"""
任务接口
作用：轮询百度网盘的异步任务状态

端点（HAR 实测，见 PLAN.md §6.2 / §6.4）
--------------------------------------
    GET /share/taskquery?taskid=<taskid>&channel=chunlei&web=1&app_id=250528
        &bdstoken=...&logid=...&clienttype=0&dp-logid=...

    → 进行中: { errno:0, task_errno:0, status:"running", progress:10, request_id }
    → 已完成: { errno:0, task_errno:0, status:"success", list:[...], total:1 }

实测行为：
  - 删除 / 移动 / 复制 / 转存 都返回 `taskid`（或转存的 `task_id`），随后轮询此端点
  - 轮询间隔约 2.6~5 秒，**通常 2 次即 success**
  - ⚠️ 转存接口返回的字段名是 `task_id`（字符串），而轮询参数名是 `taskid`
  - ⚠️ `task_errno` 是任务自身的错误码，与响应 `errno` 不同，两者都要检查
"""

from __future__ import annotations

import logging
import time
from typing import Callable

from ..网络.网络客户端 import 网络客户端, 接口错误

logger = logging.getLogger("百度网盘.任务")

# 状态取值（实测）
状态_进行中 = "running"
状态_成功 = "success"
状态_失败 = "failed"
状态_等待 = "pending"

默认轮询间隔 = 5.0
默认超时秒 = 300.0

成功状态集 = {状态_成功}
终态集 = {状态_成功, 状态_失败}


class 任务接口:
    """异步任务查询。"""

    def __init__(self, 网络: 网络客户端):
        self.网络 = 网络

    # ---------------- 单次查询 ----------------

    def 查询任务(self, taskid: str | int) -> dict:
        """查询一次任务状态，返回原始响应 dict。"""
        if taskid in (None, "", 0, "0"):
            raise ValueError(f"taskid 非法：{taskid!r}")
        return self.网络.请求(
            "GET", "/share/taskquery",
            params={"taskid": str(taskid)},
            带渠道=True,
        )

    # ---------------- 轮询 ----------------

    def 等待任务(
        self,
        taskid: str | int,
        间隔秒: float = 默认轮询间隔,
        超时秒: float = 默认超时秒,
        进度回调: Callable[[str, int], None] | None = None,
    ) -> dict:
        """轮询直到任务进入终态。

        :param 进度回调: `回调(status, progress)`
        :return: 最后一次的响应 dict
        :raises TimeoutError: 超时
        :raises 接口错误: 任务失败（`task_errno != 0`）
        """
        开始 = time.monotonic()
        次数 = 0
        while True:
            次数 += 1
            数据 = self.查询任务(taskid)
            status = 数据.get("status")
            progress = 数据.get("progress") or 0
            logger.debug(f"[任务] {taskid} 第 {次数} 次：status={status} "
                         f"progress={progress}")
            if 进度回调:
                try:
                    进度回调(status, int(progress))
                except Exception:
                    pass

            # 任务自身报错
            任务错误 = 数据.get("task_errno")
            if 任务错误 not in (0, None):
                raise 接口错误(
                    f"任务失败（task_errno={任务错误}）：{数据.get('show_msg') or ''}",
                    errno=任务错误, 响应数据=数据)

            if status in 终态集:
                logger.info(f"[任务] {taskid} → {status}（轮询 {次数} 次）")
                return 数据

            if time.monotonic() - 开始 >= 超时秒:
                raise TimeoutError(
                    f"任务 {taskid} 等待超时（{超时秒}s，已轮询 {次数} 次，"
                    f"最后状态 {status}）")
            time.sleep(间隔秒)

    # ---------------- 便捷包装 ----------------

    def 等待完成(self, taskid: str | int,
                间隔秒: float = 默认轮询间隔,
                超时秒: float = 默认超时秒) -> list:
        """等待并返回完成后的 `list`（多数任务的成功结果在此）。"""
        数据 = self.等待任务(taskid, 间隔秒=间隔秒, 超时秒=超时秒)
        return 数据.get("list") or []
