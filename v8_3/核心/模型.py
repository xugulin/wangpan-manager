"""V8_3 核心数据模型。

设计约定：
* 所有网盘路径都使用“统一逻辑路径”：以 ``/`` 开头的 POSIX 风格字符串，
  根目录是 ``/``。适配器桥接层负责把它翻译成各网盘的路径/ID。
* 所有数据类都可安全地转成 JSON（``raw`` 字段除外时由调用方裁剪），
  因为子进程桥、日志、界面都依赖这一约定。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any


class 网盘类型(str, Enum):
    百度 = "baidu"
    光鸭 = "guangya"
    夸克 = "quark"
    假 = "fake"

    @property
    def 中文名(self) -> str:
        return {
            "baidu": "百度网盘",
            "guangya": "光鸭云盘",
            "quark": "夸克网盘",
            "fake": "本地假网盘",
        }[self.value]

    @classmethod
    def 解析(cls, 值: Any) -> "网盘类型":
        if isinstance(值, cls):
            return 值
        文本 = str(值 or "").strip().lower()
        映射 = {
            "baidu": cls.百度, "百度": cls.百度, "bd": cls.百度,
            "guangya": cls.光鸭, "光鸭": cls.光鸭, "gy": cls.光鸭,
            "quark": cls.夸克, "夸克": cls.夸克, "qk": cls.夸克,
            "fake": cls.假, "假": cls.假, "local": cls.假,
        }
        if 文本 not in 映射:
            raise ValueError(f"未知网盘类型：{值!r}")
        return 映射[文本]


def 标识文本(值: Any) -> str:
    """把网盘类型枚举 / 实例标识统一成字符串标识。

    V8_3 支持同一家网盘挂多个账号（多实例），因此引擎、任务数据库、
    子进程桥、界面一律以**实例标识**（如 ``baidu``、``baidu_2``）为键；
    网盘类型（``网盘类型``）只用来决定用哪个桥后端。
    """
    if isinstance(值, 网盘类型):
        return 值.value
    return str(值 or "").strip()


def 显示名(标识: Any, 名称表: dict | None = None) -> str:
    """标识 → 便于阅读的名称；查不到时退回类型中文名，再退回标识本身。"""
    文本 = 标识文本(标识)
    if 名称表 and 文本 in 名称表:
        return str(名称表[文本])
    try:
        return 网盘类型.解析(文本).中文名
    except ValueError:
        return 文本 or "未知网盘"


def 规范路径(路径: str | None) -> str:
    """统一逻辑路径：以 / 开头、折叠重复斜杠、去掉末尾斜杠（根除外）。"""
    文本 = str(路径 or "").replace("\\", "/").strip()
    if not 文本:
        return "/"
    if not 文本.startswith("/"):
        文本 = "/" + 文本
    段 = [s for s in 文本.split("/") if s and s != "."]
    # .. 不允许向上逃逸，直接忽略
    结果 = []
    for s in 段:
        if s == "..":
            if 结果:
                结果.pop()
            continue
        结果.append(s)
    return "/" + "/".join(结果) if 结果 else "/"


def 拼路径(目录: str, 名称: str) -> str:
    目录 = 规范路径(目录)
    名称 = str(名称 or "").strip().strip("/")
    if not 名称:
        return 目录
    if 目录 == "/":
        return "/" + 名称
    return f"{目录}/{名称}"


def 格式化大小(字节: int) -> str:
    """人类可读大小（用于跳过原因/日志）。"""
    数值 = float(max(0, int(字节 or 0)))
    for 单位 in ("B", "KiB", "MiB", "GiB", "TiB"):
        if 数值 < 1024 or 单位 == "TiB":
            return f"{数值:.0f} {单位}" if 单位 == "B" else f"{数值:.1f} {单位}"
        数值 /= 1024
    return f"{数值:.1f} TiB"


def 取父目录(路径: str) -> str:
    路径 = 规范路径(路径)
    if 路径 == "/":
        return "/"
    return 路径.rsplit("/", 1)[0] or "/"


@dataclass
class 远端条目:
    """跨适配器统一的目录/文件条目。"""

    name: str
    path: str
    is_dir: bool
    size: int = 0
    modified: str = ""
    id: str = ""
    raw: dict = field(default_factory=dict)

    def to_dict(self, 含raw: bool = False) -> dict:
        数据 = {
            "name": self.name,
            "path": self.path,
            "is_dir": bool(self.is_dir),
            "size": int(self.size or 0),
            "modified": self.modified or "",
            "id": self.id or "",
        }
        if 含raw:
            数据["raw"] = self.raw
        return 数据

    @classmethod
    def from_dict(cls, 数据: dict) -> "远端条目":
        return cls(
            name=str(数据.get("name") or ""),
            path=规范路径(数据.get("path") or "/"),
            is_dir=bool(数据.get("is_dir")),
            size=int(数据.get("size") or 0),
            modified=str(数据.get("modified") or ""),
            id=str(数据.get("id") or ""),
            raw=dict(数据.get("raw") or {}),
        )


@dataclass
class 账号信息:
    网盘: 网盘类型
    已登录: bool
    用户名: str = ""
    数据目录: str = ""
    详情: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "cloud": self.网盘.value,
            "cloud_name": self.网盘.中文名,
            "logged_in": bool(self.已登录),
            "user": self.用户名,
            "data_dir": self.数据目录,
            "detail": self.详情,
        }


class 任务状态(str, Enum):
    等待 = "pending"
    枚举中 = "scanning"
    下载中 = "downloading"
    上传中 = "uploading"
    完成 = "done"
    跳过 = "skipped"
    失败 = "failed"
    已取消 = "cancelled"
    已暂停 = "paused"

    @property
    def 中文名(self) -> str:
        return {
            "pending": "等待",
            "scanning": "枚举",
            "downloading": "下载",
            "uploading": "上传",
            "done": "完成",
            "skipped": "跳过",
            "failed": "失败",
            "cancelled": "已取消",
            "paused": "暂停",
        }[self.value]


@dataclass
class 传输任务:
    """一个文件（或目录展开后的一个文件）的传输任务。

    ``源网盘`` / ``目标网盘`` 保存的是**实例标识**字符串；为了兼容旧代码，
    也接受 :class:`网盘类型` 枚举（两者哈希一致，可直接当字典键混用）。
    """

    任务ID: str
    源网盘: Any
    目标网盘: Any
    源路径: str
    目标路径: str
    是否目录: bool = False
    大小: int = 0
    状态: 任务状态 = 任务状态.等待
    错误: str = ""
    重试次数: int = 0
    已传输: int = 0
    下载字节: int = 0
    上传字节: int = 0
    本地缓存: str = ""
    开始时间: float = 0.0
    结束时间: float = 0.0
    批次ID: str = ""
    阶段详情: str = ""
    跳过原因: str = ""
    实际目标名: str = ""      # 目标盘命名规避后的实际落盘名（无规避时为空）
    源网盘名称: str = ""
    目标网盘名称: str = ""

    @property
    def 文件名(self) -> str:
        return self.源路径.rstrip("/").rsplit("/", 1)[-1] or self.源路径

    def to_dict(self) -> dict:
        数据 = {
            "task_id": self.任务ID,
            "source_cloud": 标识文本(self.源网盘),
            "source_cloud_name": self.源网盘名称 or 显示名(self.源网盘),
            "target_cloud": 标识文本(self.目标网盘),
            "target_cloud_name": self.目标网盘名称 or 显示名(self.目标网盘),
            "source_path": self.源路径,
            "target_path": self.目标路径,
            "is_dir": bool(self.是否目录),
            "size": int(self.大小 or 0),
            "status": self.状态.value,
            "status_name": self.状态.中文名,
            "error": self.错误,
            "retries": int(self.重试次数),
            "done_bytes": int(self.已传输),
            "download_bytes": int(self.下载字节),
            "upload_bytes": int(self.上传字节),
            "local_cache": self.本地缓存,
            "started_at": self.开始时间,
            "finished_at": self.结束时间,
            "batch_id": self.批次ID,
            "stage": self.阶段详情,
            "skip_reason": self.跳过原因,
            # 目标盘命名规避后的实际落盘名（无规避时为空）
            "actual_target_name": self.实际目标名,
        }
        return 数据

    @classmethod
    def from_dict(cls, 数据: dict) -> "传输任务":
        return cls(
            任务ID=str(数据.get("task_id")),
            源网盘=标识文本(数据.get("source_cloud")),
            目标网盘=标识文本(数据.get("target_cloud")),
            源路径=规范路径(数据.get("source_path")),
            目标路径=规范路径(数据.get("target_path")),
            是否目录=bool(数据.get("is_dir")),
            大小=int(数据.get("size") or 0),
            状态=任务状态(数据.get("status") or 任务状态.等待.value),
            错误=str(数据.get("error") or ""),
            重试次数=int(数据.get("retries") or 0),
            已传输=int(数据.get("done_bytes") or 0),
            下载字节=int(数据.get("download_bytes") or 0),
            上传字节=int(数据.get("upload_bytes") or 0),
            本地缓存=str(数据.get("local_cache") or ""),
            开始时间=float(数据.get("started_at") or 0),
            结束时间=float(数据.get("finished_at") or 0),
            批次ID=str(数据.get("batch_id") or ""),
            阶段详情=str(数据.get("stage") or ""),
            跳过原因=str(数据.get("skip_reason") or ""),
            实际目标名=str(数据.get("actual_target_name") or ""),
            源网盘名称=str(数据.get("source_cloud_name") or ""),
            目标网盘名称=str(数据.get("target_cloud_name") or ""),
        )


@dataclass
class 传输统计:
    批次ID: str = ""
    总任务数: int = 0
    已完成: int = 0
    跳过: int = 0
    失败: int = 0
    已取消: int = 0
    总字节: int = 0
    已完成字节: int = 0
    开始时间: float = 0.0
    结束时间: float = 0.0
    平均速度: float = 0.0  # MiB/s
    已暂停: int = 0         # 被暂停的任务数（继续后重新计为完成/失败）
    兜底次数: int = 0       # 进入"本地中转"兜底的任务数
    兜底成功: int = 0       # 兜底后传成功的任务数

    def 刷新速率(self) -> None:
        if not self.开始时间:
            self.平均速度 = 0.0
            return
        结束 = self.结束时间 or time.time()
        耗时 = max(0.001, 结束 - self.开始时间)
        self.平均速度 = (self.已完成字节 / 1048576.0) / 耗时

    def to_dict(self) -> dict:
        self.刷新速率()
        return asdict(self)


@dataclass
class 缓存配置:
    """本地中转缓存配置。

    ``目录`` 为空时自动选择：优先 /dev/shm（内存盘），否则用户缓存目录。
    """

    目录: str = ""
    保留失败文件: bool = False
    单个文件上限字节: int = 0  # 0 表示不限制
    #: 同时暂存在磁盘上的**总字节上限**（0 = 自动：剩余空间的 1/4，最多 4 GiB）。
    #: 大文件批量传输时用它兜住磁盘占用，避免把磁盘写满。
    暂存上限字节: int = 0
    #: 无论如何都要给系统留出的剩余空间（默认 1 GiB）。
    磁盘余量字节: int = 0

    def 计算暂存上限(self, 缓存根: str = "") -> int:
        """本次允许的暂存上限（字节）。"""
        if self.暂存上限字节 > 0:
            return int(self.暂存上限字节)
        try:
            import shutil as _sh
            用量 = _sh.disk_usage(缓存根 or self.解析目录())
            可用 = max(0, int(用量.free) - int(self.磁盘余量字节 or 0))
        except Exception:
            可用 = 2 * 1024 ** 3
        return max(256 * 1024 ** 2, min(4 * 1024 ** 3, 可用 // 4))

    def 解析目录(self) -> str:
        import os
        import tempfile
        if self.目录:
            os.makedirs(self.目录, exist_ok=True)
            return self.目录
        候选 = [
            "/dev/shm/v8_3_中转",
            os.path.join(tempfile.gettempdir(), "v8_3_中转"),
        ]
        for 路径 in 候选:
            try:
                os.makedirs(路径, exist_ok=True)
                测试 = os.path.join(路径, ".写权限测试")
                with open(测试, "w") as f:
                    f.write("ok")
                os.unlink(测试)
                return 路径
            except Exception:
                continue
        return tempfile.mkdtemp(prefix="v8_3_中转_")

    def to_dict(self) -> dict:
        return asdict(self)


def 转json(对象: Any) -> str:
    return json.dumps(对象, ensure_ascii=False, default=str)
