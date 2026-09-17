"""可动态调整上限的并发控制器。

线程池大小固定会限制自适应调优；这里用“固定大线程池 + 可调许可数”实现：
工作线程先获取许可，再执行文件任务；调优线程只改许可上限。
"""

from __future__ import annotations

import threading


class 并发控制器:
    def __init__(self, 初始: int, 最小: int = 1, 最大: int = 32):
        self.最小 = max(1, int(最小))
        self.最大 = max(self.最小, int(最大))
        self._上限 = max(self.最小, min(self.最大, int(初始)))
        self._使用中 = 0
        self._条件 = threading.Condition()

    @property
    def 上限(self) -> int:
        with self._条件:
            return self._上限

    @property
    def 使用中(self) -> int:
        with self._条件:
            return self._使用中

    def 获取(self) -> None:
        with self._条件:
            while self._使用中 >= self._上限:
                self._条件.wait(timeout=0.5)
            self._使用中 += 1

    def 释放(self) -> None:
        with self._条件:
            if self._使用中 > 0:
                self._使用中 -= 1
            self._条件.notify_all()

    def 调整(self, 新上限: int) -> int:
        新上限 = max(self.最小, min(self.最大, int(新上限)))
        with self._条件:
            if 新上限 == self._上限:
                return self._上限
            self._上限 = 新上限
            self._条件.notify_all()
            return self._上限

    def __enter__(self):
        self.获取()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.释放()
        return False


class 吞吐自适应器:
    """根据吞吐升降并发：失败先降，吞吐持续上升再升。"""

    def __init__(self, 控制器: 并发控制器, 日志=None,
                 最小上调间隔: float = 4.0):
        self.控制器 = 控制器
        self._日志 = 日志
        self._最小间隔 = 最小上调间隔
        self._上次时间 = 0.0
        self._上次字节 = 0
        self._上次失败 = 0
        self._上次吞吐 = 0.0

    def 观测(self, 已完成字节: int, 失败数: int, 队列长度: int) -> None:
        import time
        现在 = time.monotonic()
        if self._上次时间 <= 0:
            self._上次时间, self._上次字节, self._上次失败 = 现在, 已完成字节, 失败数
            return
        间隔 = 现在 - self._上次时间
        if 间隔 < self._最小间隔:
            return
        字节增量 = max(0, 已完成字节 - self._上次字节)
        失败增量 = max(0, 失败数 - self._上次失败)
        吞吐 = 字节增量 / 间隔
        上限 = self.控制器.上限

        if 失败增量 > 0:
            新 = max(1, 上限 - max(1, 失败增量))
            提示 = f"失败 +{失败增量}，并发 {上限} → {新}"
        elif 吞吐 > self._上次吞吐 * 1.15 and 队列长度 > self.控制器.使用中:
            新 = 上限 + 1
            提示 = f"吞吐上升，并发 {上限} → {新}"
        elif 吞吐 < self._上次吞吐 * 0.6 and 上限 > 2:
            新 = 上限 - 1
            提示 = f"吞吐回落，并发 {上限} → {新}"
        else:
            新 = 上限
            提示 = ""
        if 新 != 上限:
            self.控制器.调整(新)
            if self._日志:
                try:
                    self._日志(f"[自适应] {提示}")
                except Exception:
                    pass
        self._上次时间, self._上次字节, self._上次失败 = 现在, 已完成字节, 失败数
        self._上次吞吐 = 吞吐
