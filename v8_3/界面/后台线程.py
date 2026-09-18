"""界面后台线程：所有会阻塞的网络操作都放在 QThread 里跑。

约定
====
* 子进程适配器的调用是阻塞的（走行式 JSON 协议），必须在工作线程里执行，
  否则界面会卡住；
* QThread 实例必须被主窗口持有引用（``_活动线程``），否则 Python 3.14
  （GC 已按适配器惯例关闭自动回收）下容易出问题；
* 事件回调里只 emit 信号，绝不直接碰控件。
"""

from __future__ import annotations

import threading
import time
import weakref
from typing import Callable, Optional

from PySide6.QtCore import QThread, Signal

from ..核心.传输引擎 import 传输引擎, 传输请求


# ==========================================================================
# 关窗闸门（按**窗口**记，不是全局开关）
# ==========================================================================
# 现场崩溃（2026-09-19 02:01:33，用户日志）：
#     QThread: Destroyed while thread '' is still running
#     Fatal Python error: Aborted
# 成因：关窗时还有 QThread 卡在适配器调用里（future.result 最长 120 秒），
#       Qt 销毁父控件时把运行中的 QThread 一起删了 —— Qt 遇到这种情况直接
#       abort，**不是 Python 异常，try/except 抓不住**。
#
# 修法：窗口开始关窗时登记自己；此后**属于这个窗口的**界面线程不再发起新的
#       适配器调用（立刻返回"正在关闭"）。关窗流程再去等在飞的调用。
#
# ⚠️ 必须按窗口记：自检里会连开好几个窗口，关掉第一个之后新窗口还得能用；
#    早先写成全局开关时，第一个窗口一关，后面所有窗口的网盘调用都被挡了。
_正在关的窗口: "weakref.WeakSet" = weakref.WeakSet()


def _记一笔(动作: str) -> None:
    """把闸门的每次开关写进 数据/关窗闸门.log（诊断用，出问题时有据可查）。

    ⚠️ 默认**不写**：只有设了 ``V8_3_闸门日志=1`` 时才落盘，
    免得平时白写文件（排查"关窗时还有线程在跑"这类问题时再打开）。
    """
    import os as _os
    if str(_os.environ.get("V8_3_闸门日志") or "").strip() in ("", "0", "false", "False"):
        return
    try:
        import os
        import traceback
        from pathlib import Path
        根 = Path(__file__).resolve().parents[2]
        文件 = Path(os.environ.get("V8_3_闸门日志")
                  or (根 / "数据" / "关窗闸门.log"))
        文件.parent.mkdir(parents=True, exist_ok=True)
        栈 = "".join(traceback.format_stack()[-9:-1])
        with 文件.open("a", encoding="utf-8") as f:
            f.write(f"\n===== {time.strftime('%H:%M:%S')} {动作} =====\n{栈}\n")
    except Exception:
        pass


def 进入关闭态(窗口=None) -> None:
    """某个窗口开始关窗：登记它，之后属于它的界面线程不再发起网盘调用。"""
    if 窗口 is None:
        return
    _正在关的窗口.add(窗口)
    _记一笔(f"关闸门 {type(窗口).__name__} id={id(窗口):#x}")


def 正在关闭(父=None) -> bool:
    """这个线程/控件**所属窗口**是不是正在关？

    没给 `父`（或父已销毁）时，只在"有窗口正在关"且**当前没有别的活窗口**时
    才算关闭 —— 这样自检的多窗口场景不会被上一个窗口的关闭状态误伤。
    """
    if 父 is not None:
        窗口 = None
        try:
            窗口 = 父 if getattr(父, "isWindow", None) and 父.isWindow() else None
            if 窗口 is None:
                try:
                    窗口 = 父.window()
                except Exception:
                    窗口 = None
        except Exception:
            窗口 = None
        if 窗口 is not None:
            try:
                return 窗口 in _正在关的窗口
            except Exception:
                return False
    if not _正在关的窗口:
        return False
    try:
        from PySide6.QtWidgets import QApplication
        活窗口 = [w for w in QApplication.topLevelWidgets()
                 if getattr(w, "isWindow", lambda: False)() and w.isVisible()
                 and hasattr(w, "动作")]
        if not 活窗口:
            return False          # 还有别的窗口活着 → 不挡它
    except Exception:
        return False
    # 没有别的活窗口了：这时"某窗口在关"就等于"整个程序在关"
    return bool(_正在关的窗口)


class _关窗守卫:
    """给界面线程用的守卫：所属窗口在关就直接抛错，别去碰已经关掉的适配器。"""

    def __init__(self, 父=None):
        self._父 = 父

    def 检查(self):
        if 正在关闭(self._父):
            raise RuntimeError("程序正在关闭，已取消这次网盘调用")


class 账号状态线程(QThread):
    """查询某个网盘实例的登录状态。"""

    成功 = Signal(str, dict)
    失败 = Signal(str, str)

    def __init__(self, 标识: str, 取适配器: Callable[[str], object],
                 父=None):
        super().__init__(父)
        self.标识 = 标识
        self._取适配器 = 取适配器

    def run(self):
        try:
            _关窗守卫(self.parent()).检查()
            信息 = self._取适配器(self.标识).账号状态()
            self.成功.emit(self.标识, 信息.to_dict())
        except Exception as e:  # noqa: BLE001 - 界面需要展示任何失败
            self.失败.emit(self.标识, str(e))


class 列目录线程(QThread):
    """列某个网盘实例的一个目录。"""

    成功 = Signal(str, str, list)   # 标识, 路径, 条目
    失败 = Signal(str, str, str)    # 标识, 路径, 错误

    def __init__(self, 标识: str, 适配器, 路径: str, 父=None):
        super().__init__(父)
        self.标识 = 标识
        self.适配器 = 适配器
        self.路径 = 路径

    def run(self):
        try:
            _关窗守卫(self.parent()).检查()
            条目 = self.适配器.列目录(self.路径)
            self.成功.emit(self.标识, self.路径, list(条目))
        except Exception as e:  # noqa: BLE001
            self.失败.emit(self.标识, self.路径, str(e))


class 文件操作线程(QThread):
    """上传 / 下载 / 新建目录 / 删除等单次操作，带进度。"""

    进度 = Signal(str, str, int, int)   # 阶段, 当前, 总量  (第一个参数为描述)
    完成 = Signal(str, dict)
    失败 = Signal(str, str)

    def __init__(self, 描述: str, 动作: Callable[[Callable], dict],
                 父=None):
        super().__init__(父)
        self.描述 = 描述
        self._动作 = 动作

    def _进度回调(self, 阶段: str, 当前: int, 总量: int) -> None:
        self.进度.emit(self.描述, str(阶段), int(当前 or 0), int(总量 or 0))

    def run(self):
        try:
            _关窗守卫(self.parent()).检查()
            结果 = self._动作(self._进度回调)
            self.完成.emit(self.描述, dict(结果 or {}))
        except Exception as e:  # noqa: BLE001
            self.失败.emit(self.描述, str(e))


class 传输线程(QThread):
    """跨网盘批次传输。"""

    事件 = Signal(dict)
    完成 = Signal(dict)

    def __init__(self, 引擎: 传输引擎, 请求: 传输请求,
                 取消: threading.Event, 父=None):
        super().__init__(父)
        self.引擎 = 引擎
        self.请求 = 请求
        self.取消 = 取消

    统计就绪 = Signal(object)      # 把引擎统计对象回传界面，供单任务重跑复用

    def run(self):
        try:
            统计 = self.引擎.传输(
                self.请求,
                事件回调=lambda e: self.事件.emit(e.to_dict()),
                取消事件=self.取消,
            )
            self.统计就绪.emit(统计)
            self.完成.emit(统计.to_dict())
        except Exception as e:  # noqa: BLE001
            self.完成.emit({"error": str(e)})


class 恢复线程(QThread):
    """恢复一个未完成批次。"""

    事件 = Signal(dict)
    完成 = Signal(dict)

    def __init__(self, 引擎: 传输引擎, 批次ID: str,
                 取消: threading.Event, 父=None):
        super().__init__(父)
        self.引擎 = 引擎
        self.批次ID = 批次ID
        self.取消 = 取消

    def run(self):
        try:
            统计 = self.引擎.恢复批次(
                self.批次ID,
                事件回调=lambda e: self.事件.emit(e.to_dict()),
                取消事件=self.取消,
            )
            self.完成.emit(统计.to_dict())
        except Exception as e:  # noqa: BLE001
            self.完成.emit({"error": str(e)})


class 任务线程(QThread):
    """把任意可调用对象丢到后台线程执行，结果用信号送回。

    AI 页的"检测本地模型 / 测速 / 启动服务 / 拉取模型"都是**阻塞**操作：
    探测端口要几秒、加载模型要几十秒、下载模型要几分钟。这些以前直接在按钮回调里
    同步调用，界面就会卡死甚至崩掉（用户反馈：勾"启用本地模型"、点"启动本地服务"、
    切到 AI 页都会卡）。统一走这个线程，界面全程可响应。
    """

    成功 = Signal(object)
    失败 = Signal(str)

    def __init__(self, 工作: Callable, *参数, 父=None, **关键字):
        super().__init__(父)
        self._工作 = 工作
        self._参数 = 参数
        self._关键字 = 关键字

    def run(self):
        try:
            self.成功.emit(self._工作(*self._参数, **self._关键字))
        except Exception as e:  # noqa: BLE001 - 界面要展示任何失败
            self.失败.emit(str(e))


def 线程池管理器(窗口):
    """把线程挂到窗口上并在结束后清理，避免 QThread 被提前回收。"""

    def 登记(线程: QThread, 结束回调: Optional[Callable] = None) -> QThread:
        if not hasattr(窗口, "_活动线程"):
            窗口._活动线程 = []
        窗口._活动线程.append(线程)

        def _清理():
            try:
                窗口._活动线程.remove(线程)
            except ValueError:
                pass
            if 结束回调 is not None:
                try:
                    结束回调()
                except Exception:
                    pass
            线程.deleteLater()

        线程.finished.connect(_清理)
        return 线程

    return 登记
