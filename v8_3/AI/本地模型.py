# v8_3/AI/本地模型.py
"""本地 DeepSeek 模型接入（V8_3 新增）——**免费、离线、不上传数据**。

支持两类本地推理服务，都能跑 DeepSeek 系列模型：

===============  ==========================================================
Ollama           ``http://127.0.0.1:11434``，原生 API（``/api/tags``、
                 ``/api/chat``）。推荐：``ollama pull deepseek-r1:1.5b``
OpenAI 兼容      llama.cpp 的 ``llama-server``、LM Studio、vLLM、xinference
                 等，走 ``/v1/models`` 与 ``/v1/chat/completions``（不需要密钥）
===============  ==========================================================

设计要点
========
* **自动探测**：不知道用户装的是哪种、端口是多少时，依次试 ``11434`` 与
  常见的 OpenAI 兼容端口（8080/8000/1234/5000/…），谁能通就用谁；
* **零依赖**：只用 ``httpx``（V8_3 已有）；没有装运行时也不报错，
  而是给出**可直接复制的安装/启动命令**（:meth:`本地模型客户端.安装指引`）；
* **真免费**：本地调用不计费、不查价格、不受"高峰时段不调用"限制，
  :class:`对话结果` 里 ``费用`` 恒为 0、``来源`` 标记为 ``本地模型``；
* **可管理**：能拉起/停止 ``ollama serve``（用户目录安装，不需要 root），
  能 ``ollama pull`` 拉模型，能跑一次固定提示测延迟。

本模块不联网（除了访问本机端口），也不 import 任何 V8_1 的代码。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

try:
    import httpx
except Exception:  # pragma: no cover - httpx 是 V8_3 的既有依赖
    httpx = None  # type: ignore

__all__ = [
    "本地模型配置", "本地模型状态", "对话结果", "本地模型客户端",
    "取本地模型配置", "探测到的运行时", "默认模型", "候选端口",
]

#: 默认拉取/使用的模型（1.5B 在纯 CPU 上也能跑动，约 1.1 GB）
默认模型 = "deepseek-r1:1.5b"

#: 探测顺序：先 Ollama，再常见的 OpenAI 兼容端口
候选端口: list[tuple[str, int]] = [
    ("ollama", 11434),
    ("openai兼容", 8080),
    ("openai兼容", 8000),
    ("openai兼容", 1234),      # LM Studio
    ("openai兼容", 5000),
    ("openai兼容", 12345),
]

#: 安装指引（用户可直接复制；全部装在用户目录，不需要 root）
安装命令 = [
    "# 1) 安装 Ollama 到用户目录（不需要 sudo）",
    "mkdir -p ~/.local/ollama && cd ~/.local/ollama",
    "curl -fL -o ollama.tar.zst "
    "https://github.com/ollama/ollama/releases/latest/download/"
    "ollama-linux-amd64.tar.zst",
    "tar --zstd -xf ollama.tar.zst",
    "ln -sf ~/.local/ollama/bin/ollama ~/.local/bin/ollama",
    "# 2) 启动服务（后台常驻）",
    "ollama serve &",
    "# 3) 拉取 DeepSeek 模型（1.5B 约 1.1 GB，纯 CPU 可跑）",
    f"ollama pull {默认模型}",
    "",
    "# 或者用 llama.cpp（OpenAI 兼容端点）：",
    "#   llama-server -m DeepSeek-R1-Distill-Qwen-1.5B-Q4_K_M.gguf --port 8080",
]


def _取整(值, 默认: int) -> int:
    try:
        return int(值)
    except Exception:
        return 默认


def _取浮(值, 默认: float) -> float:
    try:
        return float(值)
    except Exception:
        return 默认


@dataclass
class 本地模型配置:
    """``配置.json`` 里 ``AI.本地模型`` 段的映射。"""

    启用: bool = False
    提供方: str = "自动"              # 自动 / ollama / openai兼容
    地址: str = ""                    # 空 = 自动探测
    模型: str = 默认模型
    超时秒: float = 120.0
    温度: float = 0.2
    #: deepseek-r1 会先"思考"再回答，预算小于 ~512 时答案会被思考挤空（实测 256 → 空）。
    最大tokens: int = 512
    自动启动: bool = True             # 需要时自动拉起 ollama serve
    优先本地: bool = True             # 优先用本地，云端仅在本地失败时兜底
    云端兜底: bool = True             # 本地不可用时是否回落到云端 API
    上下文长度: int = 4096
    #: 用途：``全部`` = 所有 AI 请求都走本地；
    #: ``仅轻量`` = 只有"文件优先级/失败诊断/运行时调优"这类短请求走本地，
    #: 重决策（批次策略）留给云端（CPU 上本地推理慢，实测一次策略约 15s）。
    用途: str = "全部"

    def 归一化(self) -> "本地模型配置":
        提供方 = str(self.提供方 or "自动").strip()
        if 提供方 not in ("自动", "ollama", "openai兼容"):
            提供方 = "自动"
        return 本地模型配置(
            启用=bool(self.启用), 提供方=提供方,
            地址=str(self.地址 or "").strip(), 模型=str(self.模型 or "").strip(),
            超时秒=max(5.0, _取浮(self.超时秒, 120.0)),
            温度=min(2.0, max(0.0, _取浮(self.温度, 0.2))),
            最大tokens=max(64, _取整(self.最大tokens, 512)),
            自动启动=bool(self.自动启动), 优先本地=bool(self.优先本地),
            云端兜底=bool(self.云端兜底),
            上下文长度=max(512, _取整(self.上下文长度, 4096)),
            用途=("仅轻量" if str(self.用途 or "全部").strip() == "仅轻量"
                else "全部"))


def 取本地模型配置(AI配置: dict | None = None) -> 本地模型配置:
    """从 AI 配置 dict 里取 ``本地模型`` 段（缺项补默认值）。"""
    段 = {}
    if isinstance(AI配置, dict):
        值 = AI配置.get("本地模型")
        if isinstance(值, dict):
            段 = 值
    return 本地模型配置(
        启用=bool(段.get("启用", False)),
        提供方=str(段.get("提供方") or "自动"),
        地址=str(段.get("地址") or ""),
        模型=str(段.get("模型") or 默认模型),
        超时秒=_取浮(段.get("超时秒"), 120.0),
        温度=_取浮(段.get("温度"), 0.2),
        最大tokens=_取整(段.get("最大tokens"), 512),
        自动启动=bool(段.get("自动启动", True)),
        优先本地=bool(段.get("优先本地", True)),
        云端兜底=bool(段.get("云端兜底", True)),
        上下文长度=_取整(段.get("上下文长度"), 4096),
        用途=str(段.get("用途") or "全部"),
    ).归一化()


@dataclass
class 本地模型状态:
    """一次检测的结果（给界面/日志/自检看）。"""

    可用: bool = False
    提供方: str = ""
    地址: str = ""
    模型: str = ""
    模型列表: list[str] = field(default_factory=list)
    版本: str = ""
    延迟毫秒: float = 0.0
    说明: str = ""
    错误: str = ""
    已安装运行时: bool = False
    可执行文件: str = ""

    def 一行(self) -> str:
        if not self.可用:
            尾巴 = f"（{self.错误}）" if self.错误 else ""
            return f"⚪ 本地模型不可用{尾巴}"
        延迟 = f" · {self.延迟毫秒:.0f}ms" if self.延迟毫秒 else ""
        版本 = f" · v{self.版本}" if self.版本 else ""
        return (f"🟢 本地模型可用 · {self.提供方} · {self.地址} · "
                f"{self.模型}{版本}{延迟}")

    def to_dict(self) -> dict:
        return {
            "可用": self.可用, "提供方": self.提供方, "地址": self.地址,
            "模型": self.模型, "模型列表": list(self.模型列表),
            "版本": self.版本, "延迟毫秒": self.延迟毫秒,
            "说明": self.说明, "错误": self.错误,
            "已安装运行时": self.已安装运行时,
            "可执行文件": self.可执行文件,
        }


@dataclass
class 对话结果:
    """一次本地推理的结果。"""

    成功: bool = False
    内容: str = ""
    推理内容: str = ""
    模型: str = ""
    提供方: str = ""
    用时秒: float = 0.0
    输入tokens: int = 0
    输出tokens: int = 0
    费用: float = 0.0                 # 本地推理永远 0
    来源: str = "本地模型"
    错误: str = ""

    def to_dict(self) -> dict:
        return {
            "成功": self.成功, "内容": self.内容, "推理内容": self.推理内容,
            "模型": self.模型, "提供方": self.提供方, "用时秒": self.用时秒,
            "输入tokens": self.输入tokens, "输出tokens": self.输出tokens,
            "费用": self.费用, "来源": self.来源, "错误": self.错误,
        }


def _找可执行文件() -> str:
    """找 ollama 可执行文件：PATH → 用户目录安装 → 常见位置。"""
    候选 = shutil.which("ollama")
    if 候选:
        return 候选
    for 路径 in (Path.home() / ".local" / "ollama" / "bin" / "ollama",
                Path.home() / ".local" / "bin" / "ollama",
                Path("/usr/local/bin/ollama"), Path("/usr/bin/ollama")):
        try:
            if 路径.is_file() and os.access(路径, os.X_OK):
                return str(路径)
        except Exception:
            continue
    return ""


def 探测到的运行时(超时秒: float = 1.5) -> list[dict]:
    """扫一遍本机常见端口，返回**正在运行**的本地推理服务。

    每项形如 ``{"提供方": "ollama", "地址": "http://127.0.0.1:11434",
    "模型列表": [...], "版本": "0.34.1"}``。
    """
    if httpx is None:
        return []
    结果: list[dict] = []
    for 提供方, 端口 in 候选端口:
        地址 = f"http://127.0.0.1:{端口}"
        try:
            客户端 = httpx.Client(timeout=超时秒)
            if 提供方 == "ollama":
                响应 = 客户端.get(f"{地址}/api/version")
                if 响应.status_code != 200:
                    continue
                版本 = str((响应.json() or {}).get("version") or "")
                模型列表 = []
                try:
                    标签 = 客户端.get(f"{地址}/api/tags")
                    模型列表 = [str(m.get("name") or "")
                            for m in (标签.json() or {}).get("models", [])]
                except Exception:
                    pass
            else:
                响应 = 客户端.get(f"{地址}/v1/models")
                if 响应.status_code != 200:
                    continue
                版本 = ""
                模型列表 = [str(m.get("id") or "")
                        for m in (响应.json() or {}).get("data", [])]
            结果.append({"提供方": 提供方, "地址": 地址,
                      "模型列表": [m for m in 模型列表 if m],
                      "版本": 版本})
        except Exception:
            continue
        finally:
            try:
                客户端.close()
            except Exception:
                pass
    return 结果


class 本地模型客户端:
    """本地 DeepSeek 模型客户端（Ollama 原生 / OpenAI 兼容）。"""

    def __init__(self, 配置: 本地模型配置 | dict | None = None, *,
                 日志回调=None):
        if isinstance(配置, dict):
            配置 = 取本地模型配置({"本地模型": 配置})
        self.配置 = (配置 or 本地模型配置()).归一化()
        self._日志回调 = 日志回调 or (lambda *_: None)
        self._锁 = threading.RLock()
        self._状态: Optional[本地模型状态] = None
        self._进程: Optional[subprocess.Popen] = None

    # ---------------- 内部工具 ----------------

    def _日志(self, 文本: str) -> None:
        try:
            self._日志回调(文本)
        except Exception:
            pass

    def _客户端(self, 超时秒: float | None = None):
        if httpx is None:
            raise RuntimeError("缺少 httpx（V8_3 依赖之一）")
        return httpx.Client(
            timeout=httpx.Timeout(connect=2.0,
                                read=float(超时秒 or self.配置.超时秒),
                                write=30.0, pool=10.0))

    def _候选地址(self) -> list[tuple[str, str]]:
        """返回 [(提供方, 地址)]：优先用户配置，其次自动探测。"""
        指定 = self.配置.地址.strip().rstrip("/")
        if 指定:
            提供方 = self.配置.提供方
            if 提供方 == "自动":
                提供方 = "ollama" if ":11434" in 指定 or "ollama" in 指定 \
                    else "openai兼容"
            return [(提供方, 指定)]
        if self.配置.提供方 == "ollama":
            return [("ollama", "http://127.0.0.1:11434")]
        if self.配置.提供方 == "openai兼容":
            return [(提供方, f"http://127.0.0.1:{端口}")
                    for 提供方, 端口 in 候选端口 if 提供方 != "ollama"]
        return [(提供方, f"http://127.0.0.1:{端口}")
                for 提供方, 端口 in 候选端口]

    # ---------------- 探测 / 状态 ----------------

    def 检测(self, 候选模型: bool = True) -> 本地模型状态:
        """探测本机是否有可用的本地服务，并尽量选出一个已下载的 DeepSeek 模型。"""
        状态 = 本地模型状态(可执行文件=_找可执行文件(),
                        已安装运行时=bool(_找可执行文件()))
        if httpx is None:
            状态.错误 = "缺少 httpx"
            self._状态 = 状态
            return 状态
        错误们: list[str] = []
        for 提供方, 地址 in self._候选地址():
            开始 = time.time()
            try:
                客户端 = self._客户端(超时秒=min(5.0, self.配置.超时秒))
                if 提供方 == "ollama":
                    响应 = 客户端.get(f"{地址}/api/version")
                    响应.raise_for_status()
                    版本 = str((响应.json() or {}).get("version") or "")
                    模型列表 = self._列模型_ollama(客户端, 地址)
                else:
                    响应 = 客户端.get(f"{地址}/v1/models")
                    响应.raise_for_status()
                    版本 = ""
                    模型列表 = self._列模型_openai(客户端, 地址)
                延迟 = (time.time() - 开始) * 1000
                模型 = self._选模型(模型列表)
                状态.可用 = True
                状态.提供方 = 提供方
                状态.地址 = 地址
                状态.版本 = 版本
                状态.模型列表 = 模型列表
                状态.模型 = 模型
                状态.延迟毫秒 = 延迟
                状态.说明 = (f"已就绪：{len(模型列表)} 个模型可选"
                          if 模型列表 else "服务在跑，但还没拉取任何模型")
                if not 模型列表:
                    状态.说明 += f"（可执行：ollama pull {self.配置.模型 or 默认模型}）"
                self._状态 = 状态
                return 状态
            except Exception as e:  # noqa: BLE001
                错误们.append(f"{地址}: {type(e).__name__}")
            finally:
                try:
                    客户端.close()
                except Exception:
                    pass
        状态.错误 = ("没检测到本地推理服务；"
                  + (f"试过 {len(错误们)} 个端口" if 错误们 else ""))
        状态.说明 = self.安装指引()
        self._状态 = 状态
        return 状态

    @staticmethod
    def _列模型_ollama(客户端, 地址: str) -> list[str]:
        try:
            响应 = 客户端.get(f"{地址}/api/tags")
            响应.raise_for_status()
            return [str(m.get("name") or "")
                    for m in (响应.json() or {}).get("models", [])
                    if m.get("name")]
        except Exception:
            return []

    @staticmethod
    def _列模型_openai(客户端, 地址: str) -> list[str]:
        try:
            响应 = 客户端.get(f"{地址}/v1/models")
            响应.raise_for_status()
            return [str(m.get("id") or "")
                    for m in (响应.json() or {}).get("data", [])
                    if m.get("id")]
        except Exception:
            return []

    def _选模型(self, 模型列表: list[str]) -> str:
        """在"已下载的模型"里挑一个：优先 DeepSeek，其次用户配置的名字。"""
        配置模型 = (self.配置.模型 or "").strip()
        if 配置模型 and 配置模型 in 模型列表:
            return 配置模型
        小写 = {m.lower(): m for m in 模型列表}
        for 关键词 in ("deepseek-r1", "deepseek", "r1"):
            命中 = [原 for 低, 原 in 小写.items() if 关键词 in 低]
            if 命中:
                return sorted(命中)[0]
        return 配置模型 or (模型列表[0] if 模型列表 else 默认模型)

    # ---------------- 推理 ----------------

    def 对话(self, 用户消息: str, 系统提示: str = "", *,
            模型: str = "", 温度: float | None = None,
            最大tokens: int | None = None) -> 对话结果:
        """跑一次本地推理（免费）。失败时返回 ``成功=False`` 的结果，不抛异常。"""
        状态 = self._状态 or self.检测()
        if not 状态.可用:
            return 对话结果(成功=False, 错误=状态.错误 or "本地模型不可用",
                        提供方=状态.提供方, 模型=状态.模型)
        用模型 = 模型 or 状态.模型 or self.配置.模型
        用温度 = self.配置.温度 if 温度 is None else 温度
        用tokens = int(最大tokens or self.配置.最大tokens)
        开始 = time.time()
        try:
            客户端 = self._客户端()
            if 状态.提供方 == "ollama":
                数据 = self._对话_ollama(客户端, 状态.地址, 用模型,
                                    系统提示, 用户消息, 用温度, 用tokens)
            else:
                数据 = self._对话_openai(客户端, 状态.地址, 用模型,
                                    系统提示, 用户消息, 用温度, 用tokens)
        except Exception as e:  # noqa: BLE001
            return 对话结果(成功=False, 错误=f"{type(e).__name__}: {e}",
                        提供方=状态.提供方, 模型=用模型,
                        用时秒=time.time() - 开始)
        finally:
            try:
                客户端.close()
            except Exception:
                pass
        结果 = 对话结果(
            成功=bool(数据.get("内容") or 数据.get("推理内容")),
            内容=str(数据.get("内容") or ""),
            推理内容=str(数据.get("推理内容") or ""),
            模型=用模型, 提供方=状态.提供方,
            用时秒=time.time() - 开始,
            输入tokens=int(数据.get("输入tokens") or 0),
            输出tokens=int(数据.get("输出tokens") or 0),
            费用=0.0, 来源="本地模型",
        )
        if not 结果.成功:
            结果.错误 = str(数据.get("错误") or "本地模型返回空内容")
        return 结果

    def _消息(self, 系统提示: str, 用户消息: str) -> list[dict]:
        消息 = []
        if 系统提示:
            消息.append({"role": "system", "content": 系统提示})
        消息.append({"role": "user", "content": 用户消息})
        return 消息

    def _对话_ollama(self, 客户端, 地址: str, 模型: str, 系统提示: str,
                  用户消息: str, 温度: float, 最大tokens: int) -> dict:
        响应 = 客户端.post(f"{地址}/api/chat", json={
            "model": 模型,
            "messages": self._消息(系统提示, 用户消息),
            "stream": False,
            "think": False,           # deepseek-r1 的思考块不参与业务 JSON 解析
            "options": {
                "temperature": 温度,
                "num_predict": 最大tokens,
                "num_ctx": self.配置.上下文长度,
            },
        })
        响应.raise_for_status()
        数据 = 响应.json() or {}
        消息 = 数据.get("message") or {}
        return {
            "内容": 消息.get("content") or "",
            "推理内容": 消息.get("thinking") or 消息.get("reasoning_content") or "",
            "输入tokens": 数据.get("prompt_eval_count") or 0,
            "输出tokens": 数据.get("eval_count") or 0,
        }

    def _对话_openai(self, 客户端, 地址: str, 模型: str, 系统提示: str,
                  用户消息: str, 温度: float, 最大tokens: int) -> dict:
        响应 = 客户端.post(f"{地址}/v1/chat/completions", json={
            "model": 模型,
            "messages": self._消息(系统提示, 用户消息),
            "temperature": 温度,
            "max_tokens": 最大tokens,
            "stream": False,
        })
        响应.raise_for_status()
        数据 = 响应.json() or {}
        选择 = (数据.get("choices") or [{}])[0]
        消息 = 选择.get("message") or {}
        用量 = 数据.get("usage") or {}
        return {
            "内容": 消息.get("content") or "",
            "推理内容": 消息.get("reasoning_content") or "",
            "输入tokens": 用量.get("prompt_tokens") or 0,
            "输出tokens": 用量.get("completion_tokens") or 0,
        }

    def 测速(self, 提示: str = "只回复两个字：可用") -> dict:
        """跑一次固定短提示，报告往返耗时与速度（界面"检测/测速"按钮用）。"""
        状态 = self.检测()
        if not 状态.可用:
            return {"成功": False, "错误": 状态.错误, **状态.to_dict()}
        # deepseek-r1 会先"思考"再回答，预算太小会把答案挤没，所以给 128
        结果 = self.对话(提示, "你是测试助手，直接给答案，不要思考。",
                      最大tokens=128)
        速度 = (结果.输出tokens / 结果.用时秒) if 结果.用时秒 > 0 else 0.0
        return {
            "成功": 结果.成功, "错误": 结果.错误, "模型": 结果.模型,
            "提供方": 结果.提供方, "用时秒": round(结果.用时秒, 2),
            "输出tokens": 结果.输出tokens,
            "每秒tokens": round(速度, 1),
            "回答": (结果.内容 or "").strip()[:60],
            **状态.to_dict(),
        }

    # ---------------- 服务管理 ----------------

    def 启动服务(self, 等待秒: float = 40.0) -> tuple[bool, str]:
        """按需拉起 ``ollama serve``（用户目录安装，不需要 root）。"""
        状态 = self.检测()
        if 状态.可用:
            return True, f"本地服务已在运行：{状态.地址}"
        可执行 = _找可执行文件()
        if not 可执行:
            return False, "没找到 ollama 可执行文件：\n" + self.安装指引()
        日志文件 = Path.home() / ".local" / "ollama" / "serve.log"
        try:
            日志文件.parent.mkdir(parents=True, exist_ok=True)
            句柄 = 日志文件.open("a", encoding="utf-8")
            环境 = os.environ.copy()
            环境.setdefault("OLLAMA_HOST", "127.0.0.1:11434")
            self._进程 = subprocess.Popen(
                [可执行, "serve"], stdout=句柄, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, env=环境,
                start_new_session=True)
        except Exception as e:  # noqa: BLE001
            return False, f"启动失败：{type(e).__name__}: {e}"
        截止 = time.time() + max(3.0, 等待秒)
        while time.time() < 截止:
            time.sleep(1.0)
            新状态 = self.检测()
            if 新状态.可用:
                return True, f"已启动 ollama serve（{新状态.地址}）"
        return False, f"启动了但 {等待秒:.0f} 秒内没就绪，日志：{日志文件}"

    def 拉取模型(self, 模型: str = "", *, 超时秒: float = 3600.0) -> tuple[bool, str]:
        """``ollama pull <模型>``（阻塞；界面里请放到后台线程跑）。"""
        模型 = (模型 or self.配置.模型 or 默认模型).strip()
        可执行 = _找可执行文件()
        if not 可执行:
            return False, "没找到 ollama 可执行文件：\n" + self.安装指引()
        try:
            进程 = subprocess.run([可执行, "pull", 模型],
                                capture_output=True, text=True,
                                timeout=超时秒, encoding="utf-8",
                                errors="replace")
        except Exception as e:  # noqa: BLE001
            return False, f"拉取失败：{type(e).__name__}: {e}"
        if 进程.returncode != 0:
            return False, (进程.stderr or 进程.stdout or "")[-400:]
        self._状态 = None                 # 下次检测重新读模型列表
        return True, f"已拉取 {模型}"

    @staticmethod
    def 安装指引() -> str:
        return "本地模型还没就绪，按下面几步装（都在用户目录，不需要 sudo）：\n" \
            + "\n".join(安装命令)
