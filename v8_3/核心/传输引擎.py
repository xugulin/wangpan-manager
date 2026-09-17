"""V8_3 跨网盘传输引擎（v2：持久化 + 自适应并发）。

目标（针对旧 V8/Alist 的已知性能问题）：
* 不再经过 Alist/WebDAV，直接调用三家网盘的原生直链接口与原生上传接口；
* 下载/上传全程流式，不在内存里堆整个文件；
* 小文件高并发、大文件低并发，且并发会按吞吐/失败率自适应；
* 目标预检查按目录聚合，消除旧版“每文件一次元信息”的 N+1；
* 任务进度落 SQLite，宿主重启后可 `恢复批次` 继续传；
* 本地只做一枚临时中转文件（优先 /dev/shm）。
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from .适配器 import 云盘适配器
from .并发 import 并发控制器, 吞吐自适应器
from .错误 import 任务取消, 任务暂停
from .任务数据库 import 任务数据库
from .模型 import (
    传输任务, 传输统计, 任务状态, 缓存配置,
    规范路径, 拼路径, 取父目录, 标识文本, 显示名, 格式化大小,
)


@dataclass
class 传输请求:
    """一次跨网盘传输的输入参数。

    ``源网盘`` / ``目标网盘`` 用**实例标识**（``baidu`` / ``baidu_2``…）；
    也接受 :class:`网盘类型` 枚举，构造时会自动规范化成字符串。
    """

    源网盘: Any
    源路径: str
    目标网盘: Any
    目标路径: str
    覆盖: bool = False
    递归: bool = True
    并发: int = 0              # 0 = 自动
    小文件阈值: int = 8 * 1024 * 1024
    小文件并发: int = 16
    大文件并发: int = 3
    重试次数: int = 3
    自适应并发: bool = True
    断点续传: bool = True          # 中途断流时保留分片，重试从断点继续
    失败兜底: bool = True          # 常规重试都失败后，改用"源→本地→目标"保守模式
    兜底串行: int = 1              # 兜底任务同时跑几个（默认 1 = 最省磁盘）
    缓存: 缓存配置 = field(default_factory=缓存配置)
    批次ID: str = ""

    def __post_init__(self):
        self.源网盘 = 标识文本(self.源网盘)
        self.目标网盘 = 标识文本(self.目标网盘)
        self.源路径 = 规范路径(self.源路径)
        self.目标路径 = 规范路径(self.目标路径)
        if not self.批次ID:
            self.批次ID = f"batch_{int(time.time())}_{uuid.uuid4().hex[:6]}"

    def to_config(self) -> dict:
        return {
            "source_cloud": 标识文本(self.源网盘),
            "source_path": self.源路径,
            "target_cloud": 标识文本(self.目标网盘),
            "target_path": self.目标路径,
            "覆盖": bool(self.覆盖),
            "递归": bool(self.递归),
            "并发": int(self.并发),
            "小文件阈值": int(self.小文件阈值),
            "小文件并发": int(self.小文件并发),
            "大文件并发": int(self.大文件并发),
            "重试次数": int(self.重试次数),
            "自适应并发": bool(self.自适应并发),
            "缓存": {
                "目录": self.缓存.目录,
                "保留失败文件": bool(self.缓存.保留失败文件),
                "单个文件上限字节": int(self.缓存.单个文件上限字节),
            },
            "批次ID": self.批次ID,
        }

    @classmethod
    def from_config(cls, 配置: dict, 批次ID: str | None = None) -> "传输请求":
        缓存 = 配置.get("缓存") or {}
        return cls(
            源网盘=标识文本(配置.get("source_cloud")),
            源路径=配置.get("source_path") or "/",
            目标网盘=标识文本(配置.get("target_cloud")),
            目标路径=配置.get("target_path") or "/",
            覆盖=bool(配置.get("覆盖")),
            递归=bool(配置.get("递归", True)),
            并发=int(配置.get("并发") or 0),
            小文件阈值=int(配置.get("小文件阈值") or 8 * 1024 * 1024),
            小文件并发=int(配置.get("小文件并发") or 16),
            大文件并发=int(配置.get("大文件并发") or 3),
            重试次数=int(配置.get("重试次数") or 3),
            自适应并发=bool(配置.get("自适应并发", True)),
            缓存=缓存配置(
                目录=str(缓存.get("目录") or ""),
                保留失败文件=bool(缓存.get("保留失败文件")),
                单个文件上限字节=int(缓存.get("单个文件上限字节") or 0),
            ),
            批次ID=批次ID or str(配置.get("批次ID") or ""),
        )


@dataclass
class 引擎事件:
    类型: str
    任务: Optional[传输任务] = None
    统计: Optional[传输统计] = None
    消息: str = ""
    数据: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        数据 = {"type": self.类型, "message": self.消息}
        if self.任务 is not None:
            数据["task"] = self.任务.to_dict()
        if self.统计 is not None:
            数据["stats"] = self.统计.to_dict()
        if self.数据:
            数据["data"] = dict(self.数据)
        return 数据


事件回调类型 = Callable[[引擎事件], None]


class 传输引擎:
    """跨网盘传输调度器（线程池 + 自适应并发 + 重试 + SQLite 持久化）。

    2026-09-16 起还负责两件事：

    * **磁盘暂存预算**：同一时刻落盘的字节数有上限（``缓存.暂存上限字节``，
      默认自动 = 剩余空间的 1/4、最多 4 GiB），并永远给系统留出
      ``缓存.磁盘余量字节``（默认 1 GiB）；任务一结束（成功/失败/取消）
      立刻归还额度并删掉中转文件 —— 这是"尽量减少磁盘占用"的核心。
    * **失败兜底**：常规重试（含直链多段/续传）都失败后，改用
      "源 → 本地 → 目标"的**保守模式**再试一次：清干净中转、跳过直链、
      直接用适配器原生下载，串行执行（``兜底串行``，默认 1）。
    """

    最大线程 = 32

    #: 类级共享：预算按进程算，多个引擎实例（界面/CLI）不会各算各的。
    #: ⚠️ 用可变账本（dict）而不是 `类变量 += `——后者会悄悄变成实例属性，
    #: 导致预算永远看起来是 0（实测踩到）。
    _暂存锁 = threading.Condition()
    _暂存账 = {"已用": 0}

    def __init__(self, 适配器表: dict[str, 云盘适配器],
                 日志回调: Optional[Callable[[str], None]] = None,
                 数据库路径: str | Path | None = None,
                 名称表: Optional[dict] = None,
                 守卫表: Optional[dict] = None):
        # 键是实例标识；旧的 网盘类型 枚举键与同名字符串哈希一致，可直接混用
        self.适配器表 = {标识文本(k): v for k, v in dict(适配器表).items()}
        self.名称表 = {标识文本(k): str(v) for k, v in (名称表 or {}).items()}
        # 敏感词上传守卫（按实例标识）：上传前预检改名、被拦后探测式绕过
        self.守卫表 = {标识文本(k): v for k, v in (守卫表 or {}).items()}
        # AI 调度器（可选，由界面/CLI 注入）：策略 / 优先级 / 调优 / 诊断 / 回填
        self.AI调度器 = None
        self._AI策略: dict = {}
        self._AI并发建议 = 0
        self._AI批次标记 = ""
        self._AI批次开始 = 0.0
        self._日志回调 = 日志回调 or (lambda msg: None)
        self._目录锁 = threading.RLock()
        self._已建目录: set[tuple[str, str, str]] = set()
        # 兜底闸门：默认只放 1 个兜底任务跑（最省磁盘），由请求.兜底串行决定
        self._兜底闸门 = threading.Semaphore(1)
        # ---------------- 暂停/继续（2026-09-16 新增） ----------------
        #   * 全局暂停：排队中的任务不再开跑（已经跑的不打断）；
        #   * 单任务暂停：排队中的直接不出队；**正在跑的**会在下一次进度回调时
        #     抛 任务暂停 → 保留中转分片 → 状态=暂停，继续时按断点续传接着跑。
        self._暂停锁 = threading.RLock()
        self._全局暂停 = threading.Event()
        self._暂停任务集: set[str] = set()
        self._运行中: dict[str, 传输任务] = {}
        self._数据库: Optional[任务数据库] = None
        if 数据库路径:
            try:
                self._数据库 = 任务数据库(数据库路径)
            except Exception as e:
                self._日志(f"[引擎] 任务数据库不可用，退化为非持久化：{e}")
                self._数据库 = None

    # ==================== 对外主入口 ====================

    def 传输(self, 请求: 传输请求,
            事件回调: Optional[事件回调类型] = None,
            取消事件: Optional[threading.Event] = None) -> 传输统计:
        # 批次开始：清掉上一轮可能残留的"安全停止"标记
        self._清除停止标记()
        # 兜底闸门按本次批次重建：兜底任务同时跑几个（默认 1，最省磁盘）
        try:
            self._兜底闸门 = threading.Semaphore(max(1, int(请求.兜底串行 or 1)))
        except Exception:
            self._兜底闸门 = threading.Semaphore(1)
        # 顺手清理"没跑完就没了"的历史中转残留（默认缓存在 /dev/shm，是内存）
        try:
            from ..配置 import 清理旧中转
            个数, 字节 = 清理旧中转(6.0, 保留批次=请求.批次ID)
            if 个数:
                self._日志(f"[引擎] 已清理 {个数} 个过期中转批次，"
                         f"释放 {字节 / 1048576:.1f} MiB")
        except Exception:
            pass
        源 = self._取适配器(请求.源网盘)
        目标 = self._取适配器(请求.目标网盘)
        取消事件 = cancel_event = 取消事件 or threading.Event()
        统计 = 传输统计(批次ID=请求.批次ID, 开始时间=time.time())
        统计锁 = threading.Lock()
        self._抛出(事件回调, 引擎事件(
            "批次开始", 统计=统计,
            消息=f"{self._名(请求.源网盘)} → {self._名(请求.目标网盘)}"))

        self._日志(f"[引擎] 扫描源 {self._名(请求.源网盘)}:{请求.源路径}")
        条目 = self._扫描源(源, 请求, 取消事件)
        if 取消事件.is_set():
            return self._结束批次(统计, 事件回调, 取消=True)
        if not 条目:
            self._日志("[引擎] 源路径没有可传输的文件")
            return self._结束批次(统计, 事件回调, 消息="没有文件")

        任务列表: list[传输任务] = []
        for i, 项 in enumerate(条目):
            目标路径 = self._计算目标路径(请求, 项)
            任务列表.append(传输任务(
                任务ID=f"{请求.批次ID}_{i:06d}",
                源网盘=请求.源网盘,
                目标网盘=请求.目标网盘,
                源路径=项["source_path"],
                目标路径=目标路径,
                是否目录=False,
                大小=int(项.get("size") or 0),
                批次ID=请求.批次ID,
                源网盘名称=self._名(请求.源网盘),
                目标网盘名称=self._名(请求.目标网盘),
            ))
        self._请求AI策略(请求, 任务列表, 事件回调)
        self._统计初始化(统计, 任务列表)
        self._保存批次(请求, 状态="running")
        self._保存任务们(任务列表)

        return self._调度(请求, 任务列表, 统计, 统计锁,
                        事件回调, 取消事件)

    def 恢复批次(self, 批次ID: str,
                事件回调: Optional[事件回调类型] = None,
                取消事件: Optional[threading.Event] = None) -> 传输统计:
        """恢复一个未完成/失败的批次；已完成的文件不会重传。"""
        if self._数据库 is None:
            raise RuntimeError("引擎未启用任务数据库，无法恢复批次")
        记录 = self._数据库.读取批次(批次ID)
        if 记录 is None:
            raise KeyError(f"批次不存在：{批次ID}")
        请求 = 传输请求.from_config(记录.get("config") or {}, 批次ID=批次ID)
        源 = self._取适配器(请求.源网盘)
        目标 = self._取适配器(请求.目标网盘)
        取消事件 = 取消事件 or threading.Event()
        任务列表 = self._数据库.读取任务(批次ID)
        for 任务 in 任务列表:
            任务.源网盘 = 请求.源网盘
            任务.目标网盘 = 请求.目标网盘
            任务.源网盘名称 = self._名(请求.源网盘)
            任务.目标网盘名称 = self._名(请求.目标网盘)
            if 任务.状态 in (任务状态.等待, 任务状态.枚举中,
                            任务状态.下载中, 任务状态.上传中,
                            任务状态.失败, 任务状态.已取消):
                任务.状态 = 任务状态.等待
                任务.错误 = ""
        统计 = 传输统计(批次ID=批次ID, 开始时间=time.time())
        统计锁 = threading.Lock()
        self._统计初始化(统计, 任务列表)
        self._保存批次(请求, 状态="running")
        self._日志(f"[引擎] 恢复批次 {批次ID}："
                   f"总 {统计.总任务数}，待续 {len([t for t in 任务列表 if t.状态 == 任务状态.等待])}")
        self._抛出(事件回调, 引擎事件(
            "批次开始", 统计=统计, 消息=f"恢复批次 {批次ID}"))
        return self._调度(请求, 任务列表, 统计, 统计锁,
                        事件回调, 取消事件)

    # ==================== 调度核心 ====================

    def _调度(self, 请求: 传输请求, 任务列表: list[传输任务],
              统计: 传输统计, 统计锁: threading.Lock,
              事件回调, 取消事件: threading.Event) -> 传输统计:
        目标 = self._取适配器(请求.目标网盘)
        源 = self._取适配器(请求.源网盘)

        待传 = [t for t in 任务列表 if t.状态 == 任务状态.等待]
        if not 待传:
            return self._结束批次(统计, 事件回调, 消息="没有待传文件")
        待传 = self._按AI优先级排序(待传)

        # 目标预检查（按父目录聚合，只对等待中的文件生效）
        self._预检查目标(请求, 目标, 待传, 统计, 统计锁, 事件回调)
        self._保存任务们(任务列表)
        待传 = [t for t in 任务列表 if t.状态 == 任务状态.等待]
        if not 待传:
            return self._结束批次(统计, 事件回调, 消息="目标均已存在")

        # 串行确保目标目录存在，避免 ID 型网盘并发创建同名目录
        目录集 = sorted({取父目录(t.目标路径) for t in 待传},
                       key=lambda p: p.count("/"))
        for 目录 in 目录集:
            if 取消事件.is_set():
                break
            self._确保目录(目标, 目录)

        初始并发 = self._计算并发(请求, 待传)
        if self._AI并发建议 and 请求.并发 <= 0:
            初始并发 = max(1, min(self.最大线程, self._AI并发建议))
            self._日志(f"[AI] 采用策略并发：{初始并发}")
        控制器 = 并发控制器(初始并发, 最小=1, 最大=self.最大线程)
        自适应器 = (吞吐自适应器(控制器, self._日志)
                 if 请求.自适应并发 else None)
        self._日志(f"[引擎] 待传 {len(待传)} 个文件，"
                   f"初始并发 {初始并发}（自适应={'开' if 自适应器 else '关'}），"
                   f"缓存目录 {请求.缓存.解析目录()}")

        停止调优 = threading.Event()
        调优线程 = None
        if 自适应器 is not None:
            调优线程 = threading.Thread(
                target=self._调优循环,
                args=(统计, 统计锁, 自适应器, 停止调优, len(待传),
                      控制器, 事件回调),
                name="传输调优", daemon=True)
            调优线程.start()

        try:
            with ThreadPoolExecutor(max_workers=self.最大线程,
                                    thread_name_prefix="传输") as 池:
                未来表 = {}
                for 任务 in 待传:
                    if 取消事件.is_set():
                        任务.状态 = 任务状态.已取消
                        with 统计锁:
                            self._结账(统计, 任务)
                        self._保存任务(任务)
                        continue
                    未来 = 池.submit(self._执行文件任务, 请求, 源, 目标,
                                    任务, 事件回调, 取消事件, 统计,
                                    统计锁, 控制器)
                    未来表[未来] = 任务
                for 未来 in as_completed(未来表):
                    任务 = 未来表[未来]
                    try:
                        未来.result()
                    except Exception as e:
                        任务.状态 = 任务状态.失败
                        任务.错误 = f"调度器异常：{e!r}"
                        with 统计锁:
                            self._结账(统计, 任务)
                        self._保存任务(任务)
                        self._抛出事件(事件回调, "任务失败", 任务, 统计)
        finally:
            停止调优.set()
            if 调优线程 is not None:
                调优线程.join(timeout=2.0)
        return self._结束批次(统计, 事件回调)

    def _调优循环(self, 统计: 传输统计, 统计锁: threading.Lock,
                  自适应器: 吞吐自适应器, 停止: threading.Event,
                  初始任务数: int,
                  控制器: 并发控制器 | None = None,
                  事件回调=None) -> None:
        AI上次 = 0.0
        while not 停止.wait(3.0):
            with 统计锁:
                快照 = (统计.已完成字节, 统计.失败,
                       统计.已完成 + 统计.跳过 + 统计.失败 + 统计.已取消)
            队列 = max(0, 初始任务数 - 快照[2])
            自适应器.观测(快照[0], 快照[1], 队列)
            if self.AI调度器 is not None and 控制器 is not None:
                现在 = time.time()
                if 现在 - AI上次 >= 10.0:
                    AI上次 = 现在
                    self._AI运行时调优(统计, 统计锁, 控制器, 初始任务数,
                                  事件回调)

    def _结束批次(self, 统计: 传输统计, 事件回调,
                  取消: bool = False, 消息: str = "") -> 传输统计:
        统计.结束时间 = time.time()
        统计.刷新速率()
        self._AI回填(统计)
        if 统计.失败:
            批次状态 = "failed"
        elif 取消:
            批次状态 = "cancelled"
        else:
            批次状态 = "done"
        if self._数据库 is not None:
            try:
                self._数据库.更新批次状态(统计.批次ID, 批次状态)
            except Exception:
                pass
        self._抛出(事件回调, 引擎事件(
            "批次完成", 统计=统计,
            消息=消息 or self._统计摘要(统计)))
        return 统计

    # ==================== 源扫描 ====================

    def _扫描源(self, 源: 云盘适配器, 请求: 传输请求,
                取消事件: threading.Event) -> list[dict]:
        """并行 BFS 扫描源目录（每层目录并发列，限制 8 并发）。"""
        根路径 = 请求.源路径
        根信息 = self._确认源存在(源, 根路径)
        if 根信息 is None:
            raise FileNotFoundError(f"源路径不存在：{根路径}")
        if not 根信息.is_dir:
            return [{"source_path": 根路径,
                     "relative": self._相对路径(根路径, 根路径),
                     "size": 根信息.size}]
        if not 请求.递归:
            raise ValueError(f"源路径是目录，但请求未开启递归：{根路径}")

        from concurrent.futures import ThreadPoolExecutor, as_completed
        输出: list[dict] = []
        当前层 = [根路径]
        with ThreadPoolExecutor(max_workers=8,
                                thread_name_prefix="源扫描") as 池:
            while 当前层 and not 取消事件.is_set():
                下一层: list[str] = []
                未来表 = {池.submit(源.列目录, 目录): 目录
                         for 目录 in 当前层}
                for 未来 in as_completed(未来表):
                    if 取消事件.is_set():
                        break
                    条目 = 未来.result()
                    for 子项 in 条目:
                        if 子项.is_dir:
                            下一层.append(子项.path)
                        else:
                            输出.append({
                                "source_path": 子项.path,
                                "relative": self._相对路径(根路径,
                                                       子项.path),
                                "size": 子项.size,
                            })
                当前层 = 下一层
        输出.sort(key=lambda x: x["source_path"])
        return 输出

    #: 服务端"暂时"类错误：退避要更长，别用 1.5s 那种短退避把重试次数耗光
    瞬时错误关键词 = (
        "doloading", "同名冲突", "正在处理", "处理中", "稍后", "重试",
        "try again", "timeout", "超时", "timed out", "connection",
        "reset", "网络", "busy", "系统繁忙", "internal", "内部错误",
        "502", "503", "504", "429", "too many", "rate limit", "限流",
        "temporarily", "暂时", "不可用",
        # 断流类：下载中途断开（HTTP 层表现为 peer closed / incomplete read）
        "中断", "断开", "断流", "incomplete", "peer closed",
        "remotedisconnected", "incompleteread", "protocolerror",
    )

    @classmethod
    def _是瞬时错误(cls, 错误文本: str) -> bool:
        # 认证/权限/参数类错误不算瞬时，重试没意义
        for 词 in ("未登录", "认证", "权限", "无权限", "参数", "不存在",
                  "名称不可用", "敏感"):
            if 词 in str(错误文本 or ""):
                return False
        文本 = str(错误文本 or "").lower()
        return any(词.lower() in 文本 for 词 in cls.瞬时错误关键词)

    @classmethod
    def _退避秒(cls, 错误文本: str, 尝试: int) -> tuple[float, str]:
        """返回 (等待秒数, 类别)：瞬时错误 3/6/12/24s（上限 60s），
        其它错误维持 1.5/2.25/3.4s（上限 30s）。"""
        if cls._是瞬时错误(错误文本):
            return min(60.0, 3.0 * (2 ** 尝试)), "服务端暂时不可用"
        return min(30.0, 1.5 ** 尝试), "普通错误"

    def _确认源存在(self, 源: 云盘适配器, 根路径: str,
                    尝试次数: int = 4, 间隔秒: float = 1.5):
        """确认源路径存在；不存在时重试几次再放弃。

        为什么要重试：ID 型网盘（夸克/光鸭）与部分对象存储对**刚上传的文件**存在
        1–3 秒的可见性延迟（最终一致性）。用户"上传完立刻开始跨盘传输"时，
        源扫描可能刚好落在延迟窗口里，报"源路径不存在"——实测可复现，
        也是"直链传输有时会失败"的一个来源。
        """
        信息 = None
        for i in range(max(1, 尝试次数)):
            try:
                信息 = 源.文件信息(根路径)
            except FileNotFoundError:
                信息 = None
            except Exception as e:  # noqa: BLE001
                self._日志(f"[引擎] 源路径查询异常（第 {i + 1} 次）：{e}")
                信息 = None
            if 信息 is not None:
                if i:
                    self._日志(f"[引擎] 源路径第 {i + 1} 次查询才可见：{根路径}")
                return 信息
            if i + 1 < 尝试次数:
                time.sleep(间隔秒)
        return None

    @staticmethod
    def _相对路径(根路径: str, 当前路径: str) -> str:
        根 = 规范路径(根路径)
        当前 = 规范路径(当前路径)
        if 根 == "/":
            return 当前.lstrip("/")
        if 当前 == 根:
            return ""
        前缀 = 根.rstrip("/") + "/"
        if 当前.startswith(前缀):
            return 当前[len(前缀):]
        return 当前.lstrip("/")

    @staticmethod
    def _计算目标路径(请求: 传输请求, 项: dict) -> str:
        源相对 = str(项.get("relative") or "")
        源路径 = 规范路径(项["source_path"])
        目标根 = 请求.目标路径
        if not 源相对:
            if 目标根 in ("", "/"):
                return 拼路径("/", 源路径.rsplit("/", 1)[-1])
            return 目标根
        return 拼路径(目标根, 源相对)

    def 重跑单任务(self, 请求: 传输请求, 任务: 传输任务,
                  事件回调=None, 取消事件: threading.Event | None = None,
                  统计: 传输统计 | None = None,
                  统计锁: threading.Lock | None = None,
                  后台: bool = True) -> threading.Thread | None:
        """把某个任务重新跑一遍（继续/重试/重新传输都用它）。

        界面点"继续/重试"时调用：任务状态先回到等待，然后走与批次里完全相同的
        执行路径（含断点续传、兜底、敏感词守卫、磁盘预算）。
        """
        源 = self._取适配器(请求.源网盘)
        目标 = self._取适配器(请求.目标网盘)
        取消事件 = 取消事件 or threading.Event()
        统计 = 统计 if 统计 is not None else 传输统计(
            批次ID=请求.批次ID, 开始时间=time.time())
        统计锁 = 统计锁 or threading.Lock()
        任务.状态 = 任务状态.等待
        任务.错误 = ""
        任务.重试次数 = 0
        任务.结束时间 = 0.0
        self._保存任务(任务)
        with self._暂停锁:
            self._暂停任务集.discard(任务.任务ID)
        self._清除停止标记(任务.任务ID)   # 之前暂停留在桥里的标记要清掉

        def 跑():
            try:
                self._执行文件任务_持锁(请求, 源, 目标, 任务, 事件回调,
                                  取消事件, 统计, 统计锁)
            except Exception as e:  # noqa: BLE001
                self._日志(f"[引擎] 单任务重跑异常：{任务.源路径}：{e}")

        if not 后台:
            跑()
            return None
        线程 = threading.Thread(target=跑, name=f"重跑-{任务.任务ID}",
                            daemon=True)
        线程.start()
        return 线程

    # ==================== 暂停 / 继续 ====================

    def 全部暂停(self) -> None:
        """暂停整个批次：排队任务不再开跑；正在跑的会在下一次进度回调停下。"""
        self._全局暂停.set()
        with self._暂停锁:
            运行中 = list(self._运行中)
        for 任务ID in 运行中:
            self.暂停任务(任务ID)
        self._日志(f"[引擎] 已暂停：排队任务不再开始，"
                 f"运行中的 {len(运行中)} 个任务会在安全点停下")

    def 全部继续(self) -> None:
        with self._暂停锁:
            self._暂停任务集.clear()
        self._全局暂停.clear()
        self._清除停止标记()          # 桥进程里的停止标记也必须清，否则重跑立刻又被停
        self._日志("[引擎] 已继续")

    def 暂停任务(self, 任务ID: str) -> None:
        任务ID = str(任务ID)
        with self._暂停锁:
            self._暂停任务集.add(任务ID)
            运行中 = self._运行中.get(任务ID)
        # 正在跑的：必须让**桥进程内部**在下一个进度回调处停下
        # （引擎侧回调抛异常会被客户端读线程吞掉，实测无效）
        if 运行中 is not None:
            for 方向 in ("源网盘", "目标网盘"):
                标识 = getattr(运行中, 方向, "")
                try:
                    适配器 = self.适配器表.get(标识文本(标识))
                    if 适配器 is not None and hasattr(适配器, "暂停任务"):
                        适配器.暂停任务(任务ID)
                except Exception:
                    pass
        self._日志(f"[引擎] 单任务暂停：{任务ID}")

    def 继续任务(self, 任务ID: str) -> None:
        任务ID = str(任务ID)
        with self._暂停锁:
            self._暂停任务集.discard(任务ID)
        try:
            任务 = self._运行中.get(任务ID)
            标识们 = ([getattr(任务, "源网盘", ""), getattr(任务, "目标网盘", "")]
                    if 任务 is not None
                    else list(self.适配器表))
            for 标识 in 标识们:
                适配器 = self.适配器表.get(标识文本(标识))
                if 适配器 is not None and hasattr(适配器, "继续任务"):
                    适配器.继续任务(任务ID)
        except Exception:
            pass
        self._日志(f"[引擎] 单任务继续：{任务ID}")

    def 是否暂停(self, 任务ID: str = "") -> bool:
        if self._全局暂停.is_set():
            return True
        if not 任务ID:
            return False
        with self._暂停锁:
            return str(任务ID) in self._暂停任务集

    def _清除停止标记(self, 任务ID: str = "") -> None:
        """让桥进程清掉"安全停止"标记（不传任务 ID = 全部清除）。"""
        for 适配器 in self.适配器表.values():
            if hasattr(适配器, "继续任务"):
                try:
                    适配器.继续任务(任务ID)
                except Exception:
                    pass

    def 等待可运行(self, 任务ID: str, 取消事件: threading.Event | None) -> None:
        """排队任务在开跑前等这里：被暂停就一直等（可被取消）。"""
        while self.是否暂停(任务ID):
            if 取消事件 is not None and 取消事件.is_set():
                raise 任务取消()
            time.sleep(0.2)

    def _检查暂停(self, 任务: 传输任务) -> None:
        """运行中任务的"安全暂停点"：在进度回调里被调用。"""
        if self.是否暂停(任务.任务ID):
            raise 任务暂停()

    # ==================== 磁盘暂存预算 ====================

    def _暂存可用(self, 请求: 传输请求) -> int:
        """当前还允许新增多少暂存字节（同时考虑配置上限与磁盘实际剩余）。"""
        import shutil as _sh
        上限 = 请求.缓存.计算暂存上限(请求.缓存.解析目录())
        try:
            余量 = int(请求.缓存.磁盘余量字节 or 0)
            剩余 = int(_sh.disk_usage(请求.缓存.解析目录()).free) - 余量
        except Exception:
            剩余 = 上限
        # 上限与"磁盘实际能给的量"取小，再减去已占用
        return max(0, min(上限, max(0, 剩余)) - 传输引擎._暂存账["已用"])

    def _预留暂存(self, 请求: 传输请求, 需要字节: int,
                取消事件: threading.Event | None = None,
                超时秒: float = 600.0, 日志=None) -> int:
        """为一次下载预留磁盘额度；返回实际预留字节（0 表示不需要预留）。

        规则：
          * 单文件比上限还大 → 放行（文件不能切块上传），但打日志提醒；
          * 额度不够 → 等待其它任务释放（默认最多等 10 分钟），
            期间每 5 秒打一次进度日志；
          * 等超时 → 抛错，任务带明确原因失败（而不是把磁盘写满）。
        """
        需要 = max(0, int(需要字节 or 0))
        if 需要 <= 0:
            return 0
        with self._暂存锁:
            上限 = 请求.缓存.计算暂存上限(请求.缓存.解析目录())
            if 需要 > 上限:
                传输引擎._暂存账["已用"] += 需要
                if 日志:
                    日志(f"[引擎] 单文件 {需要 / 1048576:.1f} MiB 超过暂存上限 "
                      f"{上限 / 1048576:.1f} MiB，本次放行（不切块）")
                self._暂存锁.notify_all()
                return 需要
            开始 = time.time()
            上次提示 = 0.0
            while True:
                if 取消事件 is not None and 取消事件.is_set():
                    raise 任务取消()
                if self._暂存可用(请求) >= 需要:
                    传输引擎._暂存账["已用"] += 需要
                    self._暂存锁.notify_all()
                    return 需要
                if time.time() - 开始 > 超时秒:
                    raise RuntimeError(
                        f"本地暂存空间不足：需要 {需要 / 1048576:.1f} MiB，"
                        f"当前仅剩 {self._暂存可用(请求) / 1048576:.1f} MiB"
                        f"（暂存上限 {上限 / 1048576:.1f} MiB，可调大 "
                        f"传输.暂存上限GB 或清理缓存目录）")
                if 日志 and time.time() - 上次提示 > 5.0:
                    上次提示 = time.time()
                    日志(f"[引擎] 暂存额度等待中：需要 "
                      f"{需要 / 1048576:.1f} MiB，当前可用 "
                      f"{self._暂存可用(请求) / 1048576:.1f} MiB")
                self._暂存锁.wait(0.5)

    def _释放暂存(self, 任务: 传输任务) -> None:
        """归还该任务占用的暂存额度（幂等）。"""
        占用 = int(getattr(任务, "暂存预留", 0) or 0)
        if 占用 <= 0:
            return
        任务.暂存预留 = 0
        with self._暂存锁:
            传输引擎._暂存账["已用"] = max(
                0, 传输引擎._暂存账["已用"] - 占用)
            self._暂存锁.notify_all()

    # ==================== 目标预检查 ====================

    def _预检查目标(self, 请求: 传输请求, 目标: 云盘适配器,
                    任务列表: list[传输任务], 统计: 传输统计,
                    统计锁: threading.Lock, 事件回调) -> None:
        """按目标父目录分组列一次目录，并并行拉取（避免 N+1 且不串行等目录）。"""
        父目录集 = sorted({取父目录(t.目标路径) for t in 任务列表})
        目录缓存: dict[str, dict[str, tuple[bool, int]]] = {}
        if 父目录集:
            from concurrent.futures import ThreadPoolExecutor

            def 列一次(目录: str) -> dict[str, tuple[bool, int]]:
                try:
                    return {条目.name: (bool(条目.is_dir), int(条目.size or 0))
                            for 条目 in 目标.列目录(目录)}
                except Exception:
                    return {}

            并发 = min(8, max(1, len(父目录集)))
            with ThreadPoolExecutor(max_workers=并发,
                                    thread_name_prefix="目标预检") as 池:
                for 目录, 已有 in zip(父目录集,
                                      池.map(列一次, 父目录集)):
                    目录缓存[目录] = 已有
        for 任务 in 任务列表:
            父目录 = 取父目录(任务.目标路径)
            名称 = 任务.目标路径.rsplit("/", 1)[-1]
            已有 = 目录缓存.get(父目录, {}).get(名称)
            if 已有 is None:
                continue
            是目录, 已大小 = 已有
            # 同名语义（2026-09-16 明确化）：
            #   * 覆盖=开  → 不在这里跳过，交给 _执行一次 先删后传；
            #   * 同名是文件夹 → 跳过并说明（直接上传会得到含义不明的冲突错误）；
            #   * 同名且大小一致 → 跳过（真的已经传过了）；
            #   * 同名但大小不同 → **不静默跳过**，说明差异（勾选「覆盖」可替换）；
            #   * 源大小未知（0）→ 保守按已存在处理，并在原因里写明。
            if 请求.覆盖:
                continue
            if 是目录:
                原因 = "目标同名是文件夹"
            elif not 任务.大小:
                原因 = "目标同名（源大小未知，按已存在处理）"
            elif int(任务.大小) != 已大小:
                原因 = (f"同名但大小不同（目标 {格式化大小(已大小)} / "
                       f"源 {格式化大小(int(任务.大小))}）：勾选「覆盖」可替换")
            else:
                原因 = "目标已存在"
            任务.状态 = 任务状态.跳过
            任务.跳过原因 = 原因
            self._日志(f"[引擎] 跳过 {任务.目标路径}：{原因}")
            with 统计锁:
                self._结账(统计, 任务)
            self._保存任务(任务)
            self._抛出事件(事件回调, "任务跳过", 任务, 统计)

    # ==================== 单文件执行 ====================

    def _执行文件任务(self, 请求: 传输请求, 源: 云盘适配器,
                      目标: 云盘适配器, 任务: 传输任务,
                      事件回调, 取消事件: threading.Event,
                      统计: 传输统计, 统计锁: threading.Lock,
                      控制器: 并发控制器) -> None:
        控制器.获取()
        try:
            # 排队任务：被暂停就一直等在这里（不占并发额度之外的资源）
            try:
                self.等待可运行(任务.任务ID, 取消事件)
            except 任务取消:
                任务.状态 = 任务状态.已取消
                任务.结束时间 = time.time()
                with 统计锁:
                    self._结账(统计, 任务)
                self._保存任务(任务)
                self._抛出事件(事件回调, "任务取消", 任务, 统计)
                return
            self._运行中[任务.任务ID] = 任务
            self._执行文件任务_持锁(
                请求, 源, 目标, 任务, 事件回调, 取消事件,
                统计, 统计锁)
        finally:
            self._运行中.pop(任务.任务ID, None)
            控制器.释放()

    def _执行文件任务_持锁(self, 请求: 传输请求, 源: 云盘适配器,
                          目标: 云盘适配器, 任务: 传输任务,
                          事件回调, 取消事件: threading.Event,
                          统计: 传输统计,
                          统计锁: threading.Lock) -> None:
        try:
            self._执行文件任务_主流程(请求, 源, 目标, 任务, 事件回调,
                                取消事件, 统计, 统计锁)
        finally:
            # 无论成功/失败/取消，立刻归还磁盘额度（配合 _清理中转 删文件）
            self._释放暂存(任务)

    def _执行文件任务_主流程(self, 请求: 传输请求, 源: 云盘适配器,
                          目标: 云盘适配器, 任务: 传输任务,
                          事件回调, 取消事件: threading.Event,
                          统计: 传输统计,
                          统计锁: threading.Lock) -> None:
        if 取消事件.is_set():
            任务.状态 = 任务状态.已取消
            任务.结束时间 = time.time()
            with 统计锁:
                self._结账(统计, 任务)
            self._保存任务(任务)
            self._抛出事件(事件回调, "任务取消", 任务, 统计)
            return

        任务.开始时间 = 任务.开始时间 or time.time()
        最后错误 = ""
        for 尝试 in range(max(1, 请求.重试次数 + 1)):
            任务.重试次数 = 尝试
            try:
                self._执行一次(请求, 源, 目标, 任务, 事件回调, 取消事件)
                任务.状态 = 任务状态.完成
                任务.结束时间 = time.time()
                with 统计锁:
                    self._结账(统计, 任务)
                self._保存任务(任务)
                self._抛出事件(事件回调, "任务完成", 任务, 统计)
                return
            except 任务暂停:
                任务.状态 = 任务状态.已暂停
                任务.结束时间 = 0.0
                任务.阶段详情 = "暂停（已保留断点，继续时可续传）"
                with 统计锁:
                    统计.已暂停 += 1
                self._保存任务(任务)
                self._日志(f"[引擎] 任务已暂停：{任务.源路径}")
                self._抛出事件(事件回调, "任务暂停", 任务, 统计)
                return
            except 任务取消:
                任务.状态 = 任务状态.已取消
                任务.结束时间 = time.time()
                with 统计锁:
                    self._结账(统计, 任务)
                self._保存任务(任务)
                self._抛出事件(事件回调, "任务取消", 任务, 统计)
                return
            except Exception as e:
                # 暂停竞态兜底：桥是按"下一个进度回调"停下的，回来的错误信息
                # 可能是"任务已被用户暂停/取消"。此刻只要暂停标记还在，就按暂停处理
                # （保留中转分片，继续时可续传），不要记成失败。
                if self.是否暂停(任务.任务ID):
                    任务.状态 = 任务状态.已暂停
                    任务.结束时间 = 0.0
                    任务.阶段详情 = "暂停（已保留断点，继续时可续传）"
                    with 统计锁:
                        统计.已暂停 += 1
                    self._保存任务(任务)
                    self._日志(f"[引擎] 任务已暂停：{任务.源路径}")
                    self._抛出事件(事件回调, "任务暂停", 任务, 统计)
                    return
                最后错误 = f"{type(e).__name__}: {e}"
                任务.错误 = 最后错误
                self._日志(f"[引擎] 任务失败（第 {尝试 + 1} 次）："
                          f"{任务.源路径}：{最后错误}")
                if 尝试 < 请求.重试次数:
                    if 请求.断点续传:
                        # 保留分片，下一次尝试从断点继续（大文件在弱网下的关键）
                        留 = self._残留字节(Path(任务.本地缓存)) if 任务.本地缓存 else 0
                        if 留:
                            self._日志(f"[引擎] 保留断点 {留 / 1048576:.1f} MiB，"
                                     f"下次续传：{任务.源路径}")
                    else:
                        self._清理中转(任务)
                    等待, 类别 = self._退避秒(最后错误, 尝试)
                    self._日志(f"[引擎] {等待:.1f}s 后重试（{类别}）"
                             f"第 {尝试 + 2} 次：{任务.源路径}")
                    time.sleep(等待)
                    self._保存任务(任务)
                    continue
                任务.状态 = 任务状态.失败
                任务.结束时间 = time.time()
                if not 请求.缓存.保留失败文件:
                    self._清理中转(任务)
                # 兜底：常规重试（直链多段/续传）都失败后，切换"源→本地→目标"的
                # 保守模式再试一次；成功就按完成结账。
                if 请求.失败兜底 and not 取消事件.is_set() and \
                        self._兜底本地中转(请求, 源, 目标, 任务, 事件回调,
                                     取消事件, 统计, 统计锁, 最后错误):
                    return
                with 统计锁:
                    self._结账(统计, 任务)
                self._保存任务(任务)
                self._抛出事件(事件回调, "任务失败", 任务, 统计)
                self._AI诊断(任务, 事件回调)
                return

    # ==================== 失败兜底：源→本地→目标 ====================

    def _兜底本地中转(self, 请求: 传输请求, 源: 云盘适配器,
                   目标: 云盘适配器, 任务: 传输任务,
                   事件回调, 取消事件: threading.Event,
                   统计: 传输统计, 统计锁: threading.Lock,
                   上次错误: str = "") -> bool:
        """保守模式兜底：完全放弃直链，走"适配器原生下载 → 本地 → 上传"。

        与常规重试的区别（也是它更容易成功的原因）：
          * **清干净中转**：此前留下的分片/续传状态/半个文件全部丢弃；
          * **跳过直链**：直连签名 URL 不通时，改用适配器自带的下载实现
            （不同服务器、不同鉴权路径，实测能救回直链失败的任务）；
          * **串行执行**（``兜底串行``，默认 1）：同一时刻只有一个兜底文件落盘，
            磁盘占用最小；批量失败时也不会同时铺开一堆大文件；
          * 仍然尊重磁盘预算与取消。

        返回 True 表示兜底成功（任务已按"完成"结账）。
        """
        if 取消事件.is_set():
            return False
        self._日志(f"[引擎] 兜底：{任务.源路径} 常规重试失败"
                 f"（{str(上次错误)[:80]}），改用「源→本地→目标」保守模式")
        # 串行闸门：默认 1（最省磁盘），由 请求.兜底串行 决定
        try:
            闸门 = getattr(self, "_兜底闸门", None)
            名额 = max(1, int(请求.兜底串行 or 1))
            if 闸门 is None:
                self._兜底闸门 = threading.Semaphore(名额)
                闸门 = self._兜底闸门
            if 闸门.acquire(timeout=1800):
                持有 = True
            else:
                self._日志("[引擎] 兜底排队超时，本次跳过兜底")
                return False
        except Exception:
            持有, 闸门 = False, None
        try:
            with 统计锁:
                统计.兜底次数 += 1
            任务.重试次数 = max(0, int(任务.重试次数)) + 1
            任务.阶段详情 = "兜底：本地中转"
            任务.错误 = ""
            self._清理中转(任务)          # 丢弃残留，重新来
            任务.状态 = 任务状态.下载中
            self._保存任务(任务)
            self._抛出事件(事件回调, "任务进度", 任务, None)
            try:
                self._执行一次(请求, 源, 目标, 任务, 事件回调, 取消事件,
                            保守=True)
            except 任务取消:
                return False
            except Exception as e:  # noqa: BLE001
                任务.状态 = 任务状态.失败
                任务.错误 = f"兜底失败：{type(e).__name__}: {e}"
                任务.阶段详情 = "兜底失败"
                self._日志(f"[引擎] 兜底也失败：{任务.源路径}：{任务.错误}")
                return False
            任务.状态 = 任务状态.完成
            任务.错误 = ""
            任务.阶段详情 = "完成（兜底：本地中转）"
            任务.结束时间 = time.time()
            with 统计锁:
                统计.兜底成功 += 1
                self._结账(统计, 任务)
            self._保存任务(任务)
            self._日志(f"[引擎] 兜底成功：{任务.源路径} → {任务.目标路径}")
            self._抛出事件(事件回调, "任务完成", 任务, 统计)
            return True
        finally:
            try:
                if 持有 and 闸门 is not None:
                    闸门.release()
            except Exception:
                pass

    def _执行一次(self, 请求: 传输请求, 源: 云盘适配器,
                  目标: 云盘适配器, 任务: 传输任务,
                  事件回调, 取消事件: threading.Event,
                  保守: bool = False) -> None:
        if 取消事件.is_set():
            raise 任务取消()

        目标目录 = 取父目录(任务.目标路径)
        目标名称 = 任务.目标路径.rsplit("/", 1)[-1]

        # 覆盖模式下先删掉旧目标，避免各家网盘生成 “文件(1)” 之类的副本
        if 请求.覆盖:
            try:
                信息 = 目标.文件信息(任务.目标路径)
                if 信息 is not None:
                    self._日志(f"[引擎] 覆盖：删除已存在目标 {任务.目标路径}")
                    目标.删除(任务.目标路径)
            except Exception as e:
                self._日志(f"[引擎] 覆盖删除失败（继续尝试上传）：{e}")

        任务.状态 = 任务状态.下载中
        任务.阶段详情 = "下载"
        self._保存任务(任务)
        self._抛出事件(事件回调, "任务开始", 任务, None)
        缓存路径 = self._缓存路径(请求, 任务)
        任务.本地缓存 = str(缓存路径)
        Path(缓存路径).parent.mkdir(parents=True, exist_ok=True)
        # 磁盘预算：先拿到额度再落盘（批量大文件时不会把磁盘写满）。
        # 多段下载的真实占用是 **2 倍**：分片文件（N 个 .v8_3part）合并时还会
        # 再写一份完整副本，然后原子替换——所以大文件按 2× 预留，
        # 预算才是真的"不会超过"（实测踩到：12 MiB 文件峰值 24 MiB）。
        if not getattr(任务, "暂存预留", 0):
            需要 = int(任务.大小 or 0)
            if 需要 >= 8 * 1024 * 1024:      # 与三家后端的"多段阈值"一致
                需要 *= 2
            任务.暂存预留 = self._预留暂存(
                请求, 需要, 取消事件, 日志=self._日志)
        # 断点续传开启时**保留**上次中断的分片，让后端按 Range 接着下；
        # 关闭时清空重来（旧行为）。
        if 请求.断点续传:
            残留 = self._残留字节(缓存路径)
            if 残留:
                self._日志(f"[引擎] 断点续传：{任务.源路径} 复用已下载 "
                         f"{残留 / 1048576:.1f} MiB")
        else:
            Path(缓存路径).unlink(missing_ok=True)

        def 源进度(阶段: str, 当前: int, 总量: int) -> None:
            self._更新进度(任务, "download", 阶段, 当前, 总量)
            self._抛出事件(事件回调, "任务进度", 任务, None)

        self._调用下载(源, 任务, 缓存路径, 源进度, 请求, 保守=保守)
        本地大小 = Path(缓存路径).stat().st_size
        if 任务.大小 and 本地大小 != 任务.大小:
            self._日志(f"[引擎] 警告：下载大小 {本地大小} != 记录 {任务.大小}")
        任务.下载字节 = 本地大小
        任务.已传输 = max(任务.已传输, 本地大小)
        self._保存任务(任务)

        if 取消事件.is_set():
            raise 任务取消()

        self._确保目录(目标, 目标目录)

        任务.状态 = 任务状态.上传中
        任务.阶段详情 = "上传"
        self._保存任务(任务)
        self._抛出事件(事件回调, "任务开始", 任务, None)

        def 目标进度(阶段: str, 当前: int, 总量: int) -> None:
            self._更新进度(任务, "upload", 阶段, 当前, 总量)
            self._抛出事件(事件回调, "任务进度", 任务, None)

        守卫 = self.守卫表.get(标识文本(任务.目标网盘))
        if 守卫 is not None and getattr(守卫, "生效", False):
            结果 = 守卫.上传(str(缓存路径), 目标目录, 名称=目标名称,
                          任务ID=任务.任务ID, 进度=目标进度)
        else:
            结果 = 目标.上传(str(缓存路径), 目标目录, 名称=目标名称,
                          任务ID=任务.任务ID, 进度=目标进度)
        # 目标盘可能做了命名规避（如百度不允许引号/emoji）：把实际落盘名记进任务；
        # 事件里的 target_path 仍保留请求路径，便于对照与排查。
        try:
            改名 = (结果 or {}).get("改名信息") if isinstance(结果, dict) else None
            if isinstance(改名, dict) and 改名.get("新名"):
                任务.实际目标名 = str(改名["新名"])
                self._日志(f"[引擎] 目标盘命名规避：{改名.get('说明')}")
        except Exception:
            pass
        任务.已传输 = 任务.大小 or 本地大小
        任务.上传字节 = 任务.已传输
        任务.阶段详情 = "完成"
        self._抛出事件(事件回调, "任务进度", 任务, None)

        if not 请求.缓存.保留失败文件:
            self._清理中转(任务)

    def _更新进度(self, 任务: 传输任务, 方向: str, 阶段: str,
                  当前: int, 总量: int) -> None:
        # 暂停检查点：运行中的任务在这里安全停下（保留中转分片，可断点续传）
        self._检查暂停(任务)
        阶段文本 = str(阶段 or "")
        if 方向 == "download":
            if (总量 or 0) > 1:
                任务.下载字节 = max(任务.下载字节, int(当前 or 0))
                任务.已传输 = 任务.下载字节
            任务.阶段详情 = 阶段文本 or "下载"
            return
        if (总量 or 0) > 1:
            任务.上传字节 = max(任务.上传字节, int(当前 or 0))
            任务.已传输 = max(任务.已传输, 任务.上传字节)
        任务.阶段详情 = 阶段文本 or "上传"

    #: 缓存"某个适配器类的 下载() 是否支持 续传 参数"，避免每次反射
    _续传支持缓存: dict[type, bool] = {}

    def _调用下载(self, 源, 任务, 缓存路径, 源进度, 请求,
                保守: bool = False) -> None:
        """调用适配器的下载。

        `续传` 是本项目的扩展参数：内置的桥适配器都支持，但**第三方/自定义
        适配器**（以及测试替身）可能没有这个形参——那就按老签名调用，
        保证向后兼容而不是抛 TypeError。
        """
        参数 = {"任务ID": 任务.任务ID, "进度": 源进度}
        类 = type(源)
        形参集 = self._续传支持缓存.get(类)
        if 形参集 is None:
            try:
                import inspect
                形参集 = set(inspect.signature(源.下载).parameters)
                if any(p.kind == p.VAR_KEYWORD
                       for p in inspect.signature(源.下载).parameters.values()):
                    形参集.add("**")
            except Exception:
                形参集 = set()
            self._续传支持缓存[类] = 形参集
        if "续传" in 形参集 or "**" in 形参集:
            参数["续传"] = bool(请求.断点续传)
        if 保守 and ("保守" in 形参集 or "**" in 形参集):
            参数["保守"] = True
        源.下载(任务.源路径, str(缓存路径), **参数)

    @staticmethod
    def _残留字节(缓存路径) -> int:
        """中转文件 + 分片文件一共保留了多少字节（用于续传日志）。"""
        from pathlib import Path as _P
        目标 = _P(缓存路径)
        总 = 0
        try:
            if 目标.exists():
                总 += 目标.stat().st_size
        except Exception:
            pass
        try:
            for p in 目标.parent.glob(目标.name + ".v8_3part*"):
                try:
                    总 += p.stat().st_size
                except Exception:
                    pass
        except Exception:
            pass
        return 总

    @staticmethod
    def _清理中转(任务: 传输任务) -> None:
        """删掉中转文件、分片文件与续传状态（任务结束/失败清理时调用）。"""
        路径 = 任务.本地缓存
        if not 路径:
            return
        目标 = Path(路径)
        候选 = [目标]
        try:
            候选 += list(目标.parent.glob(目标.name + ".v8_3part*"))
            候选.append(目标.with_name(目标.name + ".v8_3resume.json"))
            候选.append(目标.with_name(目标.name + ".v8_3merge"))
        except Exception:
            pass
        for p in 候选:
            try:
                p.unlink(missing_ok=True)
            except Exception:
                pass
        任务.本地缓存 = ""

    def _缓存路径(self, 请求: 传输请求, 任务: 传输任务) -> Path:
        缓存根 = Path(请求.缓存.解析目录()) / 请求.批次ID
        名称 = 任务.任务ID
        源名 = 任务.源路径.rsplit("/", 1)[-1]
        后缀 = Path(源名).suffix
        return 缓存根 / f"{名称}{后缀}"

    def _确保目录(self, 目标: 云盘适配器, 目录: str) -> None:
        目录 = 规范路径(目录)
        键 = (标识文本(getattr(目标, "标识", None) or 目标.类型), str(id(目标)), 目录)
        with self._目录锁:
            if 键 in self._已建目录:
                return
        目标.确保目录(目录)
        with self._目录锁:
            self._已建目录.add(键)

    # ==================== AI 调度（可选） ====================

    @property
    def AI可用(self) -> bool:
        return self.AI调度器 is not None

    def _请求AI策略(self, 请求: 传输请求, 任务列表: list[传输任务],
                   事件回调) -> dict:
        """扫描完源目录后：把队列特征交给 AI，拿回并发/优先级等策略。"""
        self._AI并发建议 = 0
        self._AI策略 = {}
        self._AI批次标记 = ""
        if self.AI调度器 is None:
            if 任务列表:
                self._抛出(事件回调, 引擎事件(
                    "AI策略", 消息="AI 调度未启用，使用规则基线",
                    数据={"来源": "规则基线", "并发": 0,
                         "调度原因": "未启用 AI 调度（或界面里关掉了）",
                         "优先级规则": [], "分批策略": ""}))
            return {}
        if not 任务列表:
            return {}
        try:
            from ..AI.队列特征分析 import 队列特征分析器
        except Exception as e:  # noqa: BLE001 - AI 层缺失就直接降级
            self._日志(f"[AI] 特征分析模块不可用，本次按规则跑：{e}")
            self.AI调度器 = None
            return {}
        try:
            文件列表 = [(t.源路径, t.目标路径, int(t.大小 or 0))
                      for t in 任务列表]
            特征 = 队列特征分析器.分析(文件列表)
            self._AI批次标记 = f"batch_{int(time.time() * 1000)}"
            self._AI批次开始 = time.time()
            策略 = self.AI调度器.请求策略(
                特征, 决策来源标记=self._AI批次标记) or {}
            self._AI策略 = dict(策略)
            并发 = int(策略.get("并发数") or 0)
            if 并发 > 0:
                self._AI并发建议 = 并发
            来源 = str(策略.get("来源") or "规则")
            原因 = str(策略.get("调度原因") or "")
            置信度 = 策略.get("置信度")
            文本 = (f"AI 策略（{来源}"
                  + (f"，置信度 {float(置信度):.2f}" if 置信度 else "")
                  + f"）：并发 {并发 or '默认'}；{原因}")
            规则 = 策略.get("优先级规则") or []
            if isinstance(规则, str):
                规则 = [规则]
            决策 = {
                "来源": 来源,
                "并发": 并发,
                "置信度": float(置信度) if 置信度 else 0.0,
                "调度原因": 原因,
                "优先级规则": list(规则)[:4],
                "分批策略": str(策略.get("分批策略") or ""),
                "预检行为": str(策略.get("预检行为") or ""),
                "预期效果": str(策略.get("预期效果") or ""),
            }
            self._日志(f"[AI] {文本}")
            self._抛出(事件回调, 引擎事件("AI策略", 消息=文本, 数据=决策))
            return dict(策略)
        except Exception as e:  # noqa: BLE001
            self._日志(f"[AI] 请求策略失败，按规则跑：{e}")
            return {}

    def _按AI优先级排序(self, 待传: list[传输任务]) -> list[传输任务]:
        """用 AI 的优先级映射重排待传任务（分数高的先传）。"""
        if self.AI调度器 is None or not self._AI策略 or len(待传) < 2:
            return 待传
        try:
            文件列表 = [{"路径": t.源路径, "名称": t.文件名,
                       "大小": int(t.大小 or 0)} for t in 待传]
            映射 = self.AI调度器.请求文件优先级(文件列表) or {}
        except Exception as e:  # noqa: BLE001
            self._日志(f"[AI] 优先级请求失败：{e}")
            return 待传
        if not 映射:
            return 待传

        def 分数(任务: 传输任务) -> int:
            try:
                return int(映射.get(任务.源路径,
                                   映射.get(任务.目标路径, 0)) or 0)
            except (TypeError, ValueError):
                return 0

        排序后 = sorted(待传, key=分数, reverse=True)
        if 排序后 != 待传:
            self._日志(f"[AI] 已按优先级重排 {len(排序后)} 个任务")
        return 排序后

    def _AI运行时调优(self, 统计: 传输统计, 统计锁: threading.Lock,
                      控制器: 并发控制器, 初始任务数: int,
                      事件回调=None) -> None:
        try:
            with 统计锁:
                完成 = (统计.已完成 + 统计.跳过 + 统计.失败 + 统计.已取消)
                失败数 = 统计.失败
                字节 = 统计.已完成字节
                总数 = 统计.总任务数
                开始 = 统计.开始时间 or time.time()
            队列长度 = max(0, 初始任务数 - 完成)
            if 队列长度 < 5:
                return
            耗时 = max(0.001, time.time() - 开始)
            结果 = self.AI调度器.请求运行时调优(
                当前并发=控制器.上限,
                吞吐_MBps=字节 / 1048576.0 / 耗时,
                失败率=失败数 / max(1, 总数),
                队列长度=队列长度, 已完成=统计.已完成, 总数=总数) or {}
            新并发 = int(结果.get("并发数") or 0)
            if 新并发 > 0 and 新并发 != 控制器.上限:
                控制器.调整(max(1, min(self.最大线程, 新并发)))
                理由 = str(结果.get("理由") or 结果.get("动作") or "")
                self._日志(f"[AI] 运行时调优 → 并发 {控制器.上限}（{理由}）")
                self._抛出(事件回调, 引擎事件(
                    "AI调优", 消息=f"运行时调优：并发 {控制器.上限}",
                    数据={"来源": "AI 运行时调优", "并发": 控制器.上限,
                         "调度原因": 理由,
                         "吞吐_MBps": round(float(结果.get("吞吐_MBps") or 0), 2)}))
        except Exception as e:  # noqa: BLE001
            self._日志(f"[AI] 运行时调优失败：{e}")

    def _AI回填(self, 统计: 传输统计) -> None:
        """批次结束把真实吞吐/失败率回填给学习库（AI 才会越用越准）。"""
        标记 = self._AI批次标记
        if self.AI调度器 is None or not 标记:
            return
        self._AI批次标记 = ""
        try:
            耗时 = max(0.001, (统计.结束时间 or time.time())
                     - (self._AI批次开始 or time.time()))
            吞吐 = 统计.已完成字节 / 1048576.0 / 耗时
            失败率 = 统计.失败 / max(1, 统计.总任务数)
            self.AI调度器.回填效果(标记, 吞吐, 失败率, 耗时)
            self._日志(f"[AI] 已回填效果：吞吐 {吞吐:.2f} MiB/s，"
                     f"失败率 {失败率:.0%}，耗时 {耗时:.1f}s")
        except Exception as e:  # noqa: BLE001
            self._日志(f"[AI] 回填失败：{e}")
        finally:
            self._AI批次开始 = 0.0

    def _AI诊断(self, 任务: 传输任务, 事件回调=None) -> None:
        """任务最终失败时问一次 AI 怎么处理（放后台线程，绝不阻塞传输）。"""
        if self.AI调度器 is None:
            return

        def 跑():
            try:
                结果 = self.AI调度器.请求失败诊断(
                    任务.错误, 任务.文件名, 任务.重试次数) or {}
                动作 = str(结果.get("动作") or 结果.get("建议") or "")
                理由 = str(结果.get("理由") or 结果.get("诊断") or "")
                if not (动作 or 理由):
                    return
                文本 = f"AI 诊断（{任务.文件名}）：{动作}；{理由}"
                self._日志(f"[AI] {文本}")
                self._抛出(事件回调, 引擎事件("AI诊断", 消息=文本))
            except Exception as e:  # noqa: BLE001
                self._日志(f"[AI] 失败诊断异常：{e}")

        threading.Thread(target=跑, name="AI诊断", daemon=True).start()

    # ==================== 持久化 ====================

    def _保存批次(self, 请求: 传输请求, 状态: str) -> None:
        if self._数据库 is None:
            return
        try:
            self._数据库.保存批次(
                请求.批次ID, 请求.源网盘, 请求.源路径,
                请求.目标网盘, 请求.目标路径,
                请求.to_config(), 状态)
        except Exception as e:
            self._日志(f"[引擎] 保存批次失败：{e}")

    def _保存任务(self, 任务: 传输任务) -> None:
        if self._数据库 is None:
            return
        try:
            self._数据库.保存任务(任务)
        except Exception as e:
            self._日志(f"[引擎] 保存任务失败：{e}")

    def _保存任务们(self, 任务列表: list[传输任务]) -> None:
        if self._数据库 is None:
            return
        try:
            self._数据库.批量保存任务(任务列表)
        except Exception as e:
            self._日志(f"[引擎] 批量保存任务失败：{e}")

    def 批次列表(self, 限制: int = 50) -> list[dict]:
        if self._数据库 is None:
            return []
        return self._数据库.未完成批次(限制)

    def 关闭(self) -> None:
        if self._数据库 is not None:
            try:
                self._数据库.关闭()
            except Exception:
                pass
            self._数据库 = None

    # ==================== 工具 ====================

    def _取适配器(self, 网盘) -> 云盘适配器:
        标识 = 标识文本(网盘)
        适配器 = self.适配器表.get(标识)
        if 适配器 is None:
            raise KeyError(f"没有配置 {self._名(标识)} 适配器")
        return 适配器

    def _名(self, 网盘) -> str:
        return 显示名(网盘, self.名称表)

    @staticmethod
    def _统计初始化(统计: 传输统计, 任务列表: list[传输任务]) -> None:
        统计.总任务数 = len(任务列表)
        统计.总字节 = sum(int(t.大小 or 0) for t in 任务列表)
        统计.已完成 = 统计.跳过 = 统计.失败 = 统计.已取消 = 0
        统计.已完成字节 = 0
        for 任务 in 任务列表:
            if 任务.状态 == 任务状态.完成:
                统计.已完成 += 1
                统计.已完成字节 += int(任务.大小 or 任务.已传输 or 0)
            elif 任务.状态 == 任务状态.跳过:
                统计.跳过 += 1
            elif 任务.状态 == 任务状态.失败:
                统计.失败 += 1
            elif 任务.状态 == 任务状态.已取消:
                统计.已取消 += 1
        统计.刷新速率()

    @staticmethod
    def _计算并发(请求: 传输请求, 任务列表: list[传输任务]) -> int:
        if 请求.并发 > 0:
            return max(1, min(32, 请求.并发))
        小 = sum(1 for t in 任务列表 if t.大小 <= 请求.小文件阈值)
        大 = len(任务列表) - 小
        if 大 == 0:
            return max(1, min(32, 请求.小文件并发))
        if 小 == 0:
            return max(1, min(32, 请求.大文件并发))
        return max(1, min(32, 请求.大文件并发 + max(1, 小 // 4)))

    @staticmethod
    def _结账(统计: Optional[传输统计], 任务: 传输任务) -> None:
        if 统计 is None:
            return
        if 任务.状态 == 任务状态.完成:
            统计.已完成 += 1
            统计.已完成字节 += 任务.大小 or 任务.已传输
        elif 任务.状态 == 任务状态.跳过:
            统计.跳过 += 1
        elif 任务.状态 == 任务状态.失败:
            统计.失败 += 1
        elif 任务.状态 == 任务状态.已取消:
            统计.已取消 += 1
        统计.刷新速率()

    def _抛出事件(self, 回调, 类型: str, 任务: 传输任务,
                  统计: Optional[传输统计]) -> None:
        self._抛出(回调, 引擎事件(类型, 任务=任务, 统计=统计))

    @staticmethod
    def _抛出(回调, 事件: 引擎事件) -> None:
        if 回调 is None:
            return
        try:
            回调(事件)
        except Exception:
            pass

    def _日志(self, 消息: str) -> None:
        try:
            self._日志回调(消息)
        except Exception:
            pass

    @staticmethod
    def _统计摘要(统计: 传输统计) -> str:
        统计.刷新速率()
        return (f"完成 {统计.已完成}，跳过 {统计.跳过}，"
                f"失败 {统计.失败}，平均 {统计.平均速度:.2f} MiB/s")
