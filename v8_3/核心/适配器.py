"""适配器规格与统一接口。

V8_3 不重写三家网盘的协议，而是把工作区里已经 100% 自研的适配器项目
（百度网盘适配器 / 光鸭云盘适配器 / 夸克网盘适配器）当作“驱动后端”使用。

关键隔离手段：
* 每个后端跑在**独立子进程**里，避免三家适配器都使用顶层包名
  ``核心`` / ``界面`` 导致的模块命名冲突；
* V8_3 通过行式 JSON 协议调用它们，适配器源码一行不改；
* 登录仍然使用各家适配器原来的 GUI（V8_3 只负责启动它）。

多实例（同一家网盘挂多个账号）：
* 一个 :class:`适配器规格` 就是一个**网盘实例**，用 ``标识`` 唯一区分
  （如 ``baidu``、``baidu_2``），``类型`` 只决定用哪个桥后端；
* 适配器的凭证落在各自项目根的 ``数据/`` 目录，所以同一家网盘的第二个
  账号要指向一份独立的适配器项目目录（V8_3 可以帮你复制一份）。
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .模型 import 网盘类型, 标识文本, 账号信息, 远端条目


def 默认项目根() -> Path:
    """当前 V8_3 项目根目录。"""
    return Path(__file__).resolve().parents[2]


#: 工作区里三家适配器的默认相对位置（相对于工作区根目录）
默认适配器目录 = {
    网盘类型.百度: "百度网盘适配器",
    网盘类型.光鸭: "光鸭云盘适配器",
    网盘类型.夸克: "夸克网盘适配器",
}

#: 界面上给“新增网盘”用的类型顺序与图标
类型图标 = {
    网盘类型.百度: "🅱",
    网盘类型.光鸭: "🦆",
    网盘类型.夸克: "🅠",
    网盘类型.假: "🧪",
}


def 生成标识(类型: 网盘类型, 已用: "set[str] | list[str] | tuple[str, ...]" = ()) -> str:
    """为一个新实例生成唯一标识：``baidu`` → ``baidu_2`` → ``baidu_3``…"""
    已用集合 = {标识文本(x) for x in 已用}
    基础 = 标识文本(类型) or "云盘"
    if 基础 not in 已用集合:
        return 基础
    序号 = 2
    while f"{基础}_{序号}" in 已用集合:
        序号 += 1
    return f"{基础}_{序号}"


def 规范化标识(文本: str) -> str:
    """把用户输入的标识清洗成安全形式（字母/数字/下划线/短横线）。"""
    文本 = str(文本 or "").strip().lower()
    文本 = re.sub(r"[\s/\\]+", "_", 文本)
    文本 = re.sub(r"[^0-9a-z_\u4e00-\u9fff\-]+", "", 文本)
    return 文本.strip("_-") or ""


@dataclass
class 适配器规格:
    """描述一个可用的后端适配器实例。"""

    类型: 网盘类型
    名称: str = ""
    项目根: str = ""
    Python解释器: str = ""
    线程数: int = 8
    已启用: bool = True
    标识: str = ""
    额外参数: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.类型 = 网盘类型.解析(self.类型)
        if not self.标识:
            self.标识 = self.类型.value
        if not self.名称:
            self.名称 = self.类型.中文名
        if not self.Python解释器:
            # 优先使用当前解释器；V8_3 推荐用项目 venv 启动
            self.Python解释器 = sys.executable

    @property
    def 显示名(self) -> str:
        """导航按钮/日志里用的名字。"""
        return self.名称 or self.类型.中文名

    @property
    def 图标(self) -> str:
        return 类型图标.get(self.类型, "☁")

    @property
    def 按钮标题(self) -> str:
        return f"{self.图标}\n{self.显示名}"

    @property
    def 路径(self) -> Path:
        return Path(self.项目根).expanduser()

    def 启动脚本(self) -> Path:
        """返回适配器原始 GUI 启动脚本（登录管理用）。"""
        return self.路径 / "启动.py"

    @property
    def 数据目录(self) -> Path:
        return self.路径 / "数据"

    @property
    def 凭证文件(self) -> Path:
        """各家的凭证文件名不同。

        ⚠️ 这**不只是提示文案**：网盘页的凭证监视器（"在原 GUI 里退出登录后
        界面要同步"）和「退出登录」判断都用它定位真正的凭证文件。
        假网盘以前落到了兜底分支 `数据目录`（一个目录），于是监视器去
        watch 目录、退出登录时还会对目录 unlink 报 IsADirectoryError。
        """
        return {
            网盘类型.百度: self.数据目录 / "会话.json",
            网盘类型.光鸭: self.数据目录 / "令牌.json",
            网盘类型.夸克: self.数据目录 / "凭证.json",
            网盘类型.假: self.数据目录 / "登录.json",
        }.get(self.类型, self.数据目录)

    def 校验(self) -> None:
        if not self.已启用:
            return
        if not self.路径.is_dir():
            raise FileNotFoundError(f"适配器目录不存在：{self.路径}")
        if not (self.路径 / "核心").is_dir():
            raise FileNotFoundError(f"适配器缺少 核心/ 目录：{self.路径}")

    @staticmethod
    def 校验目录(路径: str | Path) -> tuple[bool, str]:
        """新增/编辑网盘时用的目录校验：返回 (是否可用, 提示文本)。"""
        目录 = Path(路径).expanduser()
        if not str(路径 or "").strip():
            return False, "请填写适配器目录"
        if not 目录.is_dir():
            return False, f"目录不存在：{目录}"
        if not (目录 / "核心").is_dir():
            return False, f"目录里没有 核心/ 包，可能不是适配器项目：{目录}"
        if not (目录 / "启动.py").is_file():
            return False, f"目录里没有 启动.py，无法打开登录界面：{目录}"
        return True, "目录可用"

    def 启动适配器GUI(self):
        import subprocess
        启动 = self.启动脚本()
        if not 启动.is_file():
            raise FileNotFoundError(f"找不到适配器启动脚本：{启动}")
        环境 = os.environ.copy()
        环境.setdefault("PYTHONIOENCODING", "utf-8")
        环境.setdefault("PYTHONUTF8", "1")
        subprocess.Popen(
            [self.Python解释器, str(启动)],
            cwd=str(self.路径),
            env=环境,
        )
        return 环境


class 云盘适配器:
    """统一网盘驱动接口（V8_3 传输引擎只依赖这一层）。

    ``标识`` 是网盘实例标识（同一个类型可以有多个实例）；子类既可以直接赋值，
    也可以像 :class:`子进程适配器` 那样用属性从规格里取。
    """

    类型: 网盘类型
    标识: str = ""
    显示名: str = ""


    def 启动(self) -> None:  # pragma: no cover - 接口默认实现
        pass

    def 关闭(self) -> None:  # pragma: no cover
        pass

    def 账号状态(self) -> 账号信息:  # pragma: no cover
        raise NotImplementedError

    def 列目录(self, 路径: str = "/") -> list[远端条目]:  # pragma: no cover
        raise NotImplementedError

    def 文件信息(self, 路径: str):  # pragma: no cover
        raise NotImplementedError

    def 确保目录(self, 路径: str) -> dict:  # pragma: no cover
        raise NotImplementedError

    def 上传(self, 本地路径: str, 远端目录: str, *,
             名称: Optional[str] = None,
             任务ID: str = "",
             进度=None) -> dict:  # pragma: no cover
        raise NotImplementedError

    def 取播放直链(self, 远端路径: str, *, 超时: float = 60.0) -> dict:  # pragma: no cover
        """取可播放直链（V8_3 播放用）。默认不支持，由桥适配器实现。"""
        raise NotImplementedError

    def 下载(self, 远端路径: str, 本地路径: str, *,
             任务ID: str = "",
             进度=None,
             续传: bool = True,
             保守: bool = False) -> dict:  # pragma: no cover
        """下载到本地。

        :param 续传: 允许断点续传——本地文件已有部分内容时按 Range 继续，
                     而不是从 0 重下（内置桥适配器支持）。
        """
        raise NotImplementedError

    def 删除(self, 路径: str) -> dict:  # pragma: no cover
        raise NotImplementedError
