"""V8_3 配置：网盘实例（适配器）、Python 解释器、传输并发、缓存。

多实例模型
==========
配置里的 ``适配器`` 是**网盘实例列表**：一份配置可以挂多个网盘账号，
包括同一家网盘的两个账号（各自指向独立的适配器项目目录，凭证互不干扰）。

```json
"适配器": [
  {"标识": "baidu",   "类型": "baidu",   "名称": "百度网盘",   "路径": "百度网盘适配器", "线程数": 12, "启用": true},
  {"标识": "baidu_2", "类型": "baidu",   "名称": "百度小号",   "路径": "适配器实例/baidu_2", "线程数": 12, "启用": true}
]
```

* ``标识``：全局唯一键（引擎 / 任务数据库 / 界面导航都用它）；
* ``类型``：决定用哪个桥后端（baidu / guangya / quark / fake）；
* ``路径``：适配器项目目录，凭证在该目录的 ``数据/`` 下，必须一账号一份。

旧版配置把 ``适配器`` 写成 ``{"baidu": {...}}`` 的形式，加载时会自动升级。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import threading
from pathlib import Path
from typing import Any, Iterable, Optional

from .核心.适配器 import (
    适配器规格, 默认适配器目录, 生成标识, 规范化标识, 类型图标,
)
from .核心.子进程客户端 import 子进程适配器
from .核心.传输引擎 import 传输引擎
from .核心.模型 import 网盘类型, 标识文本, 显示名, 缓存配置
from .敏感词.上传守卫 import 上传守卫
from .敏感词.敏感词数据库 import 敏感词数据库

项目根 = Path(__file__).resolve().parent.parent
#: V8_3 现在**完全自包含**：三家适配器、副本实例、缓存全都在项目内，
#: 不再去看项目外的"工作区"。（旧版本这里是 ``项目根.parent``，
#: 等于要求同级目录里躺着 ``百度网盘适配器/`` 等外部项目。）
工作区根 = 项目根
#: 项目内三家适配器的根目录
适配器根 = 项目根 / "适配器"
配置文件 = 项目根 / "配置.json"
实例目录名 = "适配器实例"

默认配置: dict[str, Any] = {
    "Python解释器": sys.executable,
    "数据库路径": "",
    "界面": {
        "主题": "",
        "上次网盘": "",
        "终端日志": True,          # 像 V8 那样把运行信息打到终端
        "终端日志级别": "信息",     # 调试/信息/警告/错误
    },
    "适配器": [
        {"标识": "baidu", "类型": "baidu", "名称": "百度网盘",
         "路径": "百度网盘适配器", "线程数": 12, "启用": True},
        {"标识": "guangya", "类型": "guangya", "名称": "光鸭云盘",
         "路径": "光鸭云盘适配器", "线程数": 12, "启用": True},
        {"标识": "quark", "类型": "quark", "名称": "夸克网盘",
         "路径": "夸克网盘适配器", "线程数": 12, "启用": True},
    ],
    "传输": {
        "小文件阈值": 8 * 1024 * 1024,
        "小文件并发": 16,
        "大文件并发": 3,
        "重试次数": 3,
        "自适应并发": True,
    },
    "缓存": {
        "目录": "",
        "保留失败文件": False,
    },
    "下载": {
        "分段数": 4,
        "阈值": 32 * 1024 * 1024,
    },
    "AI": {
        "启用": True,
        "api密钥": "",
        "接口地址": "https://api.deepseek.com",
        "模型": "deepseek-flash",
        "温度": 0.7,
        "max_tokens": 1024,
        "预算": {
            "初始充值金额": 50.0,
            "低余额阈值": 2.0,
            "当前余额": None,
        },
        "价格抓取": {
            "刷新间隔小时": 24,
            "历史最多条数": 500,
        },
        # 语义：时间段内 = 高峰 = **不调用 AI**；区间外 = 空闲 = 调 AI。
        # 所以这里填 DeepSeek 的"贵时段"（08:30–次日 00:30），
        # 夜间折扣窗口 00:30–08:30 就自动变成"空闲"，AI 会在那时工作。
        # 官方峰谷分时（2026-08-23 起）：UTC 01:00-04:00、06:00-10:00 为峰时，
        # 即北京时间 09:00-12:00、14:00-18:00；其余为谷时（谷价 = 峰价一半），
        # 且 UTC 周六/周日全天谷期。用"工作日 + 两个窗口"精确表达：
        # 北京 09:00-18:00 与 UTC 同日，所以按北京时间的工作日判断与官方口径一致。
        "高峰时段": {
            "启用": True,
            "工作日": [0, 1, 2, 3, 4],
            "时段": [{"start": "09:00", "end": "12:00"},
                   {"start": "14:00", "end": "18:00"}],
            "时区": "Asia/Shanghai",
        },
        "调度": {
            "每分钟最多调用": 6,
            "缓存条数": 200,
            "自适应并发": True,
            "文件优先级": True,
            "失败诊断": True,
        },
    },
    "敏感词": {
        "启用": True,
        "自动改名上传": True,
        "数据库路径": "",
        "按网盘启用": {},
    },
}


# ==================== 基础读写 ====================


def _合并(基础: dict, 覆盖: dict) -> dict:
    结果 = dict(基础)
    for k, v in (覆盖 or {}).items():
        if isinstance(v, dict) and isinstance(结果.get(k), dict):
            结果[k] = _合并(结果[k], v)
        else:
            结果[k] = v
    return 结果


def _升级旧格式(数据: dict) -> dict:
    """把 ``适配器`` 的旧字典写法升级成实例列表写法。"""
    适配器 = 数据.get("适配器")
    if isinstance(适配器, dict):
        列表 = []
        for 键, 值 in 适配器.items():
            项 = dict(值 or {})
            项.setdefault("标识", 键)
            项.setdefault("类型", 键)
            列表.append(项)
        数据["适配器"] = 列表
    return 数据


def 加载配置(路径: Optional[Path] = None) -> dict:
    路径 = Path(路径) if 路径 else 配置文件
    数据 = json.loads(json.dumps(默认配置, ensure_ascii=False))
    if 路径.is_file():
        try:
            数据 = _合并(数据, _升级旧格式(
                json.loads(路径.read_text(encoding="utf-8"))))
        except Exception:
            pass
    # 环境变量可覆盖解释器，便于在不同 venv 里跑
    数据["Python解释器"] = (os.environ.get("V8_3_PYTHON")
                          or 数据.get("Python解释器") or sys.executable)
    return 数据


def 保存配置(配置: dict, 路径: Optional[Path] = None) -> None:
    路径 = Path(路径) if 路径 else 配置文件
    路径.parent.mkdir(parents=True, exist_ok=True)
    # 「Python解释器」留空 = 就用当前跑着的解释器（加载时会自动补上）。
    # 一旦把这个"等于当前解释器"的绝对路径写进文件，项目换台机器/换个目录就失效了，
    # 别人的用户名也跟着泄进配置里 —— 所以落盘前把它还原成空串。
    待写 = 配置
    值 = str(配置.get("Python解释器") or "").strip()
    if 值 and _同一解释器(值, sys.executable):
        待写 = dict(配置)
        待写["Python解释器"] = ""
    路径.write_text(
        json.dumps(待写, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _同一解释器(甲: str, 乙: str) -> bool:
    """两个解释器路径是不是同一个可执行文件（解不开符号链接就退回字符串比较）。"""
    try:
        return Path(甲).expanduser().resolve() == Path(乙).expanduser().resolve()
    except Exception:  # noqa: BLE001
        return str(甲) == str(乙)


# ==================== 网盘实例（多实例） ====================


def 规范化实例(原始: dict, 标识: str = "") -> dict:
    """把一个实例配置项补齐成标准形式（不抛异常，容错优先）。"""
    原始 = dict(原始 or {})
    类型文本 = str(原始.get("类型") or 原始.get("标识") or "").strip()
    try:
        类型 = 网盘类型.解析(类型文本)
    except ValueError:
        类型 = 网盘类型.假
    标识文本值 = 规范化标识(标识 or 原始.get("标识") or "")
    if not 标识文本值:
        标识文本值 = 规范化标识(类型.value) or 类型.value
    return {
        "标识": 标识文本值,
        "类型": 类型.value,
        "名称": str(原始.get("名称") or 类型.中文名).strip() or 类型.中文名,
        "路径": str(原始.get("路径") or 默认适配器目录.get(类型, "")).strip(),
        "线程数": max(1, int(原始.get("线程数") or 12)),
        "启用": bool(原始.get("启用", True)),
    }


def 网盘实例列表(配置: dict | None = None) -> list[dict]:
    """返回标准化的实例列表（含未启用的，顺序即导航顺序）。"""
    配置 = 配置 if 配置 is not None else 加载配置()
    原始 = 配置.get("适配器")
    if isinstance(原始, dict):
        原始 = [dict(值 or {}, 标识=键, 类型=(值 or {}).get("类型") or 键)
                for 键, 值 in 原始.items()]
    列表: list[dict] = []
    已用: set[str] = set()
    for 项 in (原始 or []):
        实例 = 规范化实例(项)
        if 实例["标识"] in 已用:      # 标识冲突时自动改名，保证唯一
            实例["标识"] = 生成标识(网盘类型.解析(实例["类型"]), 已用)
        已用.add(实例["标识"])
        列表.append(实例)
    return 列表


def 写入实例列表(配置: dict, 列表: Iterable[dict]) -> None:
    配置["适配器"] = [规范化实例(项) for 项 in 列表]


def 实例名称表(配置: dict | None = None) -> dict[str, str]:
    配置 = 配置 if 配置 is not None else 加载配置()
    return {项["标识"]: 项["名称"] for 项 in 网盘实例列表(配置)}


def 查找实例(配置: dict, 文本: str) -> Optional[dict]:
    """按 标识 / 类型 / 类型中文名 / 实例名称 查一个实例。"""
    目标 = 规范化标识(文本) or str(文本 or "").strip().lower()
    原文本 = str(文本 or "").strip()
    候选 = 网盘实例列表(配置)
    for 项 in 候选:                        # 1. 标识精确
        if 项["标识"] == 目标 or 项["标识"] == 原文本:
            return 项
    for 项 in 候选:                        # 2. 名称精确
        if 原文本 and 项["名称"] == 原文本:
            return 项
    for 项 in 候选:                        # 3. 类型 / 类型中文名
        if 项["类型"] == 目标 or 项["类型"] == 原文本:
            return 项
        try:
            if 网盘类型.解析(原文本).value == 项["类型"]:
                return 项
        except ValueError:
            pass
    return None


def 解析标识(配置: dict, 文本: str) -> str:
    """把用户输入的网盘名解析成实例标识，失败时给出可读的错误。"""
    项 = 查找实例(配置, 文本)
    if 项 is None:
        可用 = "、".join(f"{x['标识']}({x['名称']})" for x in 网盘实例列表(配置))
        raise ValueError(f"未知网盘：{文本!r}；已配置：{可用 or '（无）'}")
    return 项["标识"]


def 生成唯一标识(配置: dict, 类型: 网盘类型 | str) -> str:
    类型 = 网盘类型.解析(类型)
    已用 = [项["标识"] for 项 in 网盘实例列表(配置)]
    return 生成标识(类型, 已用)


def 默认适配器路径(类型: 网盘类型 | str) -> Path:
    类型 = 网盘类型.解析(类型)
    return 适配器根 / 默认适配器目录.get(类型, 类型.value)


def _磁盘上的实例标识() -> list[str]:
    """``适配器实例/`` 下已经存在的实例目录名（用来避开被删网盘的旧数据）。"""
    根 = 项目根 / 实例目录名
    try:
        return [p.name for p in 根.iterdir() if p.is_dir()]
    except Exception:
        return []


def 新增网盘实例(配置: dict, 类型: 网盘类型 | str, *,
                 名称: str = "", 路径: str = "", 线程数: int = 12,
                 启用: bool = True, 标识: str = "") -> dict:
    """新增一个网盘实例并写回配置字典（不落盘，由调用方保存）。"""
    类型 = 网盘类型.解析(类型)
    列表 = 网盘实例列表(配置)
    # 已用标识 = 配置里的 + **磁盘上残留的实例目录**。
    # 只算配置里的会踩坑：删掉网盘后它的目录还在（里面有登录凭证和适配器配置），
    # 下次新增又分到同一个 baidu_2 —— 于是"新加的网盘显示上次的内容"（用户反馈过）。
    已用 = [x["标识"] for x in 列表] + _磁盘上的实例标识()
    标识 = 规范化标识(标识) or 生成标识(类型, 已用)
    if any(x["标识"] == 标识 for x in 列表):
        raise ValueError(f"标识已存在：{标识}")
    实例 = 规范化实例({
        "标识": 标识,
        "类型": 类型.value,
        "名称": 名称 or 类型.中文名,
        "路径": 路径 or str(默认适配器路径(类型)),
        "线程数": 线程数,
        "启用": 启用,
    })
    列表.append(实例)
    写入实例列表(配置, 列表)
    return 实例


def 更新网盘实例(配置: dict, 原标识: str, **字段) -> Optional[dict]:
    """更新一个实例；标识本身也可以改（字段里传 ``标识=新值``，自动查重）。"""
    列表 = 网盘实例列表(配置)
    命中 = 查找实例(配置, 原标识)
    if 命中 is None:
        return None
    旧标识 = 命中["标识"]
    新标识 = 规范化标识(字段.get("标识") or 旧标识) or 旧标识
    if 新标识 != 旧标识 and any(x["标识"] == 新标识 for x in 列表):
        raise ValueError(f"标识已存在：{新标识}")
    结果 = None
    for i, 项 in enumerate(列表):
        if 项["标识"] != 旧标识:
            continue
        合并 = dict(项)
        合并.update({k: v for k, v in 字段.items() if v is not None})
        合并["标识"] = 新标识
        结果 = 规范化实例(合并)
        列表[i] = 结果
    写入实例列表(配置, 列表)
    return 结果


def 删除网盘实例(配置: dict, 标识: str) -> Optional[dict]:
    列表 = 网盘实例列表(配置)
    命中 = 查找实例(配置, 标识)
    if 命中 is None:
        return None
    目标 = 命中["标识"]
    被删 = None
    保留 = []
    for 项 in 列表:
        if 项["标识"] == 目标 and 被删 is None:
            被删 = 项
            continue
        保留.append(项)
    写入实例列表(配置, 保留)
    return 被删


def 实例被引用(配置: dict, 标识: str) -> bool:
    """界面上用于删除确认：是否还有别处引用该标识。"""
    目标 = 规范化标识(标识)
    if (配置.get("界面") or {}).get("上次网盘") == 目标:
        return True
    return False


def 复制适配器项目(源: str | Path, 目标: str | Path,
                   覆盖: bool = False) -> Path:
    """复制一份适配器项目，用于给同一家网盘的第二个账号做独立凭证目录。

    * 不复制 ``数据/``（凭证）、``__pycache__``、``.git``、``*.pyc``；
    * 目标已存在且非空时抛错，避免误覆盖用户文件。
    """
    源路径 = Path(源).expanduser().resolve()
    目标路径 = Path(目标).expanduser()
    if not 源路径.is_dir():
        raise FileNotFoundError(f"源适配器目录不存在：{源路径}")
    if 源路径 == 目标路径.resolve():
        raise ValueError("目标目录与源目录相同")
    if 目标路径.exists() and any(目标路径.iterdir()):
        if not 覆盖:
            raise FileExistsError(f"目标目录已存在且非空：{目标路径}")
    跳过目录 = {"__pycache__", "数据", ".git", ".idea", ".venv", "venv"}

    def 忽略(目录: str, 名称: list[str]) -> set[str]:
        return {n for n in 名称
                if n in 跳过目录 or n.endswith(".pyc")
                or n.endswith(".pyo")}

    目标路径.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(源路径, 目标路径, ignore=忽略, dirs_exist_ok=True)
    (目标路径 / "数据").mkdir(parents=True, exist_ok=True)
    return 目标路径


def 准备假网盘目录(路径: str | Path) -> Path:
    """给「本地假网盘」准备一个最小可用的适配器目录（无网络自测用）。"""
    目录 = Path(路径).expanduser()
    (目录 / "核心").mkdir(parents=True, exist_ok=True)
    (目录 / "数据").mkdir(parents=True, exist_ok=True)
    (目录 / "启动.py").touch(exist_ok=True)
    return 目录


def 建议副本目录(配置: dict, 类型: 网盘类型 | str) -> Path:
    """给同一家网盘的第二个账号建议一个副本目录：``适配器实例/<标识>``。"""
    类型 = 网盘类型.解析(类型)
    标识 = 生成唯一标识(配置, 类型)
    return 工作区根 / 实例目录名 / 标识


# ==================== 规格表 ====================


def 解析路径(路径文本: str) -> Path:
    """把配置里的相对路径解析到项目内（``适配器/<名>`` 与 ``<名>`` 两种写法都认）。"""
    路径 = Path(路径文本).expanduser()
    if 路径.is_absolute():
        return 路径
    候选 = 工作区根 / 路径
    备用 = 适配器根 / 路径
    if 候选.is_dir() or not 备用.is_dir():
        return 候选
    return 备用


def 适配器规格表(配置: dict | None = None) -> dict[str, 适配器规格]:
    """实例标识 → 适配器规格（只含已启用实例）。"""
    配置 = 配置 if 配置 is not None else 加载配置()
    解释器 = str(配置.get("Python解释器") or sys.executable)
    结果: dict[str, 适配器规格] = {}
    for 项 in 网盘实例列表(配置):
        if not 项["启用"]:
            continue
        类型 = 网盘类型.解析(项["类型"])
        结果[项["标识"]] = 适配器规格(
            类型=类型,
            名称=项["名称"],
            项目根=str(解析路径(项["路径"])),
            Python解释器=解释器,
            线程数=int(项["线程数"]),
            已启用=True,
            标识=项["标识"],
            额外参数=下载参数(配置),
        )
    return 结果


# ==================== 传输 / 缓存 / 数据库 ====================


def 传输参数(配置: dict | None = None) -> dict:
    配置 = 配置 if 配置 is not None else 加载配置()
    传输 = 配置.get("传输") or {}
    return {
        "小文件阈值": int(传输.get("小文件阈值") or 8 * 1024 * 1024),
        "小文件并发": int(传输.get("小文件并发") or 16),
        "大文件并发": int(传输.get("大文件并发") or 3),
        # 断点续传：直链中途断开时保留已下载分片，重试从断点继续（默认开）
        "断点续传": bool(传输.get("断点续传", True)),
        # 失败兜底：常规重试都失败后，改用"源→本地→目标"的保守模式再试一次
        "失败兜底": bool(传输.get("失败兜底", True)),
        "兜底串行": int(传输.get("兜底串行") or 1),
        # 磁盘占用控制：暂存总量上限（GB，0=自动）与系统保留余量（GB）
        "暂存上限GB": float(传输.get("暂存上限GB") or 0),
        "磁盘余量GB": float(传输.get("磁盘余量GB") or 1.0),
        "重试次数": int(传输.get("重试次数") or 3),
        "自适应并发": bool(传输.get("自适应并发", True)),
    }


def 下载参数(配置: dict | None = None) -> dict:
    配置 = 配置 if 配置 is not None else 加载配置()
    下载 = 配置.get("下载") or {}
    return {
        "下载分段": int(下载.get("分段数") or 0),
        "下载阈值": int(下载.get("阈值") or 0),
    }


def 数据库路径(配置: dict | None = None) -> str:
    配置 = 配置 if 配置 is not None else 加载配置()
    值 = str(配置.get("数据库路径") or "").strip()
    if 值:
        return str(Path(值).expanduser())
    return str(项目根 / "数据" / "传输任务.db")


def 缓存参数(配置: dict | None = None) -> 缓存配置:
    配置 = 配置 if 配置 is not None else 加载配置()
    缓存 = 配置.get("缓存") or {}
    传输 = 配置.get("传输") or {}
    return 缓存配置(
        目录=str(缓存.get("目录") or ""),
        保留失败文件=bool(缓存.get("保留失败文件")),
        暂存上限字节=int(float(传输.get("暂存上限GB") or 0) * 1024 ** 3),
        磁盘余量字节=int(float(传输.get("磁盘余量GB") or 1.0) * 1024 ** 3),
    )


def 清理旧中转(保留小时: float = 6.0, 保留批次: str = "") -> tuple[int, int]:
    """删掉中转缓存里的**过期批次目录**，返回 (删除目录数, 释放字节)。

    为什么需要：默认中转目录是 ``/dev/shm/v8_3_中转``（内存盘），
    进程被强杀/断电时来不及清理，残留会一直占着内存（实测积了 97 MiB）。
    正常结束的批次会被引擎自己删干净，这里兜的是"没跑完就没了"的情况。
    """
    import shutil
    import time as _t
    try:
        根 = Path(缓存参数().解析目录())
    except Exception:
        return (0, 0)
    if not 根.is_dir():
        return (0, 0)
    截止 = _t.time() - max(0.0, float(保留小时)) * 3600.0
    删除数 = 释放 = 0
    try:
        子项 = list(根.iterdir())
    except Exception:
        return (0, 0)
    for 项 in 子项:
        try:
            if not 项.is_dir() or (保留批次 and 项.name == 保留批次):
                continue
            if 项.stat().st_mtime > 截止:
                continue
            大小 = sum(p.stat().st_size for p in 项.rglob("*") if p.is_file())
            shutil.rmtree(项, ignore_errors=True)
            删除数 += 1
            释放 += 大小
        except Exception:
            continue
    return (删除数, 释放)


def 数据目录() -> Path:
    """V8_3 自己的数据目录（任务库/敏感词库/AI学习库/日志都放这里）。"""
    目录 = 项目根 / "数据"
    目录.mkdir(parents=True, exist_ok=True)
    return 目录


def _数据文件(相对或绝对: str, 默认名: str) -> str:
    文本 = str(相对或绝对 or "").strip()
    if 文本:
        路径 = Path(文本).expanduser()
        if not 路径.is_absolute():
            路径 = 数据目录() / 路径
        return str(路径)
    return str(数据目录() / 默认名)


def 敏感词配置(配置: dict | None = None) -> dict:
    配置 = 配置 if 配置 is not None else 加载配置()
    词 = 配置.get("敏感词") or {}
    启用表 = 词.get("按网盘启用") or {}
    return {
        "启用": bool(词.get("启用", True)),
        "自动改名上传": bool(词.get("自动改名上传", True)),
        "数据库路径": _数据文件(词.get("数据库路径"), "敏感词.db"),
        "按网盘启用": {标识文本(k): bool(v) for k, v in dict(启用表).items()},
    }


def 敏感词数据库路径(配置: dict | None = None) -> str:
    return 敏感词配置(配置)["数据库路径"]


#: 官方峰谷窗口（北京时间）：峰时 09:00-12:00、14:00-18:00，周末全天谷期
官方高峰时段 = {
    "启用": True,
    "工作日": [0, 1, 2, 3, 4],
    "时段": [{"start": "09:00", "end": "12:00"},
           {"start": "14:00", "end": "18:00"}],
    "时区": "Asia/Shanghai",
}


def _归一高峰时段(高峰: dict) -> dict:
    """把高峰时段配置统一成官方口径的"工作日 + 多个窗口"。

    老配置写的是 ``{"开始": "08:30", "结束": "00:30"}`` —— 那是一个 16 小时的大窗口，
    既表达不了"午休不算峰时"，也没有"周末全天谷期"，导致界面上把周六周日标成高峰
    （用户反馈：时间显示与模型价格的时段标记都不对）。所以：

    * 没配过 → 直接用官方口径；
    * 老的单窗口写法（只有 开始/结束、没有 时段）→ 迁移到官方口径，
      只保留用户显式关掉的"启用"和自定义时区。
    """
    if not 高峰:
        return dict(官方高峰时段)
    时区 = str(高峰.get("时区") or 官方高峰时段["时区"])
    启用 = bool(高峰.get("启用", True))
    if 高峰.get("时段") or 高峰.get("工作日") is not None:
        return {"启用": 启用,
                "工作日": list(高峰.get("工作日") or 官方高峰时段["工作日"]),
                "时段": [dict(x) for x in (高峰.get("时段") or [])],
                "时区": 时区}
    # 老写法：只有一个窗口，且窗口就是那个 08:30→00:30 —— 迁移
    return {"启用": 启用, "工作日": list(官方高峰时段["工作日"]),
            "时段": [dict(x) for x in 官方高峰时段["时段"]], "时区": 时区}


# ============================================================================
# 在线模型：多厂家（各自 API Key + 直接填接口地址）
# ============================================================================
#: 内置厂家预设：只要能对上接口地址，用户只需要粘一个 API Key 就能用。
#: ``取模型`` 说明怎么拿模型名：``models`` = 标准 ``GET {地址}/models``（大多数网关都支持）；
#: ``预置`` = 该网关不提供模型列表接口，只能用下面这份推荐清单（用户也可以手填）。
在线厂家预设: dict = {
    "deepseek": {
        "名称": "DeepSeek 官方",
        "接口地址": "https://api.deepseek.com/v1",
        "推荐模型": ["deepseek-flash", "deepseek-v4-pro"],
        "取模型": "models",
        "备注": "本项目内置了它的峰谷价与余额查询",
    },
    "bailian": {
        "名称": "阿里云百炼（通义千问）",
        "接口地址": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "推荐模型": ["qwen-plus", "qwen-max", "qwen-turbo", "qwen3-max",
                  "deepseek-v3.2", "kimi-k2-instruct"],
        "取模型": "models",
        "备注": "控制台 → API-KEY 里创建；模型名也可在「模型广场」查",
    },
    "moonshot": {
        "名称": "月之暗面 Kimi",
        "接口地址": "https://api.moonshot.cn/v1",
        "推荐模型": ["kimi-k2-0905-preview", "moonshot-v1-8k", "moonshot-v1-32k"],
        "取模型": "models",
    },
    "zhipu": {
        "名称": "智谱 GLM",
        "接口地址": "https://open.bigmodel.cn/api/paas/v4",
        "推荐模型": ["glm-4.5", "glm-4-plus", "glm-4-flash"],
        "取模型": "models",
    },
    "volces": {
        "名称": "火山方舟（豆包）",
        "接口地址": "https://ark.cn-beijing.volces.com/api/v3",
        "推荐模型": ["doubao-seed-1-6", "doubao-1-5-pro-32k"],
        "取模型": "预置",
        "备注": "模型名通常要填「接入点 ID」（ep- 开头），请按控制台为准",
    },
    "siliconflow": {
        "名称": "硅基流动 SiliconFlow",
        "接口地址": "https://api.siliconflow.cn/v1",
        "推荐模型": ["deepseek-ai/DeepSeek-V3", "Qwen/Qwen2.5-72B-Instruct"],
        "取模型": "models",
    },
    "hunyuan": {
        "名称": "腾讯混元",
        "接口地址": "https://api.hunyuan.cloud.tencent.com/v1",
        "推荐模型": ["hunyuan-turbos-latest", "hunyuan-large"],
        "取模型": "models",
    },
    "qianfan": {
        "名称": "百度千帆",
        "接口地址": "https://qianfan.baidubce.com/v2",
        "推荐模型": ["ernie-4.5-turbo-128k", "ernie-speed-128k"],
        "取模型": "models",
    },
    "自定义": {
        "名称": "自定义（自己填接口地址）",
        "接口地址": "",
        "推荐模型": [],
        "取模型": "models",
        "备注": "任何 OpenAI 兼容网关都行：填它的 /v1 地址 + 密钥",
    },
}


def _规格化厂家(名: str, 项: dict) -> dict:
    预设 = dict(在线厂家预设.get(名) or {})
    合并 = dict(预设)
    合并.update({k: v for k, v in (项 or {}).items() if v is not None})
    合并["名称"] = str(合并.get("名称") or 名)
    合并["接口地址"] = str(合并.get("接口地址") or "").rstrip("/")
    合并["密钥"] = str(合并.get("密钥") or "").strip()
    合并["模型"] = str(合并.get("模型") or "")
    合并["模型列表"] = [str(x) for x in (合并.get("模型列表") or []) if str(x).strip()]
    合并["模型来源"] = str(合并.get("模型来源") or "")
    合并.setdefault("取模型", "models")
    # 默认模型：没选就取推荐里的第一个
    if not 合并["模型"] and 合并.get("推荐模型"):
        合并["模型"] = str(合并["推荐模型"][0])
    return 合并


def 在线模型段(配置: dict | None = None) -> dict:
    """读 ``AI.在线模型``，并把老式单密钥配置迁移进来。

    老配置只有 ``AI.api密钥 / 接口地址 / 模型`` 三个平铺字段（那时只支持 DeepSeek 一家）。
    这里统一看成"deepseek 这个厂家"，用户已有的密钥不会丢。
    """
    配置 = 配置 if 配置 is not None else 加载配置()
    ai = 配置.get("AI") or {}
    段 = dict(ai.get("在线模型") or {})
    厂家表 = {k: _规格化厂家(k, v) for k, v in (段.get("厂家") or {}).items() if isinstance(v, dict)}
    if not 厂家表:
        # 迁移：把老的平铺字段包成 deepseek 厂家
        旧地址 = str(ai.get("接口地址") or "").strip()
        厂家表 = {"deepseek": _规格化厂家("deepseek", {
            "接口地址": 旧地址 or 在线厂家预设["deepseek"]["接口地址"],
            "密钥": str(ai.get("api密钥") or ""),
            "模型": str(ai.get("模型") or ""),
        })}
        段["当前"] = "deepseek"
    当前 = str(段.get("当前") or "").strip()
    if 当前 not in 厂家表:
        当前 = next(iter(厂家表))
    return {"当前": 当前, "厂家": 厂家表}


def 当前厂家(配置: dict | None = None) -> dict:
    """当前在用的在线厂家（含密钥、接口地址、模型）。"""
    段 = 在线模型段(配置)
    return dict(段["厂家"][段["当前"]])


def 写回在线模型(配置: dict, 段: dict) -> None:
    """把 ``在线模型`` 段写回整份配置（调用方负责落盘）。

    同时把"当前厂家"的密钥/地址/模型同步到老的平铺字段，兼容还没升级的代码路径。
    """
    ai = dict(配置.get("AI") or {})
    ai["在线模型"] = 段
    当前 = 段.get("厂家", {}).get(段.get("当前", "")) or {}
    if 当前:
        ai["api密钥"] = 当前.get("密钥", "")
        ai["接口地址"] = 当前.get("接口地址", "")
        if 当前.get("模型"):
            ai["模型"] = 当前["模型"]
    配置["AI"] = ai


def AI配置(配置: dict | None = None) -> dict:
    配置 = 配置 if 配置 is not None else 加载配置()
    ai = 配置.get("AI") or {}
    预算 = dict(ai.get("预算") or {})
    价格 = dict(ai.get("价格抓取") or {})
    高峰 = dict(ai.get("高峰时段") or {})
    调度 = dict(ai.get("调度") or {})
    本地 = dict(ai.get("本地模型") or {})
    本地.setdefault("cache路径", _数据文件(本地.get("cache路径"), "本地模型缓存"))
    return {
        "启用": bool(ai.get("启用", True)),
        "api密钥": str(os.environ.get("V8_3_AI_KEY")
                    or ai.get("api密钥") or "").strip(),
        "接口地址": str(ai.get("接口地址") or "https://api.deepseek.com").rstrip("/"),
        "模型": str(ai.get("模型") or "deepseek-chat"),
        "温度": float(ai.get("温度") if ai.get("温度") is not None else 0.7),
        "max_tokens": int(ai.get("max_tokens") or 1024),
        "预算": {
            "初始充值金额": float(预算.get("初始充值金额") or 50.0),
            "低余额阈值": float(预算.get("低余额阈值") or 2.0),
            "当前余额": (None if 预算.get("当前余额") is None
                      else float(预算.get("当前余额"))),
        },
        "价格抓取": {
            "刷新间隔小时": int(价格.get("刷新间隔小时") or 24),
            "历史最多条数": int(价格.get("历史最多条数") or 500),
            "缓存路径": _数据文件(价格.get("缓存路径"), "DeepSeek价格.json"),
            "历史路径": _数据文件(价格.get("历史路径"), "DeepSeek价格历史.jsonl"),
        },
        "高峰时段": _归一高峰时段(高峰),
        "调度": {
            "每分钟最多调用": int(调度.get("每分钟最多调用") or 6),
            "缓存条数": int(调度.get("缓存条数") or 200),
            "自适应并发": bool(调度.get("自适应并发", True)),
            "文件优先级": bool(调度.get("文件优先级", True)),
            "失败诊断": bool(调度.get("失败诊断", True)),
        },
        # ---- 本地 DeepSeek 模型（V8_3 新增：免费、离线、不上传数据）----
        "本地模型": {
            "启用": bool(本地.get("启用", False)),
            "提供方": str(本地.get("提供方") or "自动"),
            "地址": str(os.environ.get("V8_3_本地模型地址")
                      or 本地.get("地址") or "").strip(),
            "模型": str(本地.get("模型") or "deepseek-r1:1.5b"),
            "超时秒": float(本地.get("超时秒") or 120.0),
            "温度": float(本地.get("温度") if 本地.get("温度") is not None else 0.2),
            "最大tokens": int(本地.get("最大tokens") or 1024),
            "上下文长度": int(本地.get("上下文长度") or 4096),
            "自动启动": bool(本地.get("自动启动", True)),
            "优先本地": bool(本地.get("优先本地", True)),
            "云端兜底": bool(本地.get("云端兜底", True)),
        },
    }


def 设置AI配置(配置: dict, **字段) -> None:
    ai = dict(配置.get("AI") or {})
    ai.update({k: v for k, v in 字段.items() if v is not None})
    配置["AI"] = ai


def 保存AI配置(配置: dict, 路径: Optional[Path] = None) -> None:
    保存配置(配置, 路径)


def AI学习库路径(配置: dict | None = None) -> str:
    配置 = 配置 if 配置 is not None else 加载配置()
    return _数据文件((配置.get("AI") or {}).get("学习库路径"), "AI学习库.db")


def 界面配置(配置: dict | None = None) -> dict:
    配置 = 配置 if 配置 is not None else 加载配置()
    界面 = 配置.get("界面") or {}
    return {
        "主题": str(界面.get("主题") or ""),
        "上次网盘": str(界面.get("上次网盘") or ""),
        "终端日志": bool(界面.get("终端日志", True)),
        "终端日志级别": str(界面.get("终端日志级别") or "信息"),
    }


def 设置界面配置(配置: dict, **字段) -> None:
    界面 = dict(配置.get("界面") or {})
    界面.update({k: v for k, v in 字段.items() if v is not None})
    配置["界面"] = 界面


# ==================== 动作（适配器 + 引擎持有者） ====================


class 动作:
    """持有已启动的适配器与引擎，供 CLI/UI 复用。

    与旧版的差别：适配器按**实例标识**缓存；配置变化后调用 :meth:`重载`
    会关掉旧子进程并重建规格表（新增/编辑/删除网盘时用）。
    """

    def __init__(self, 配置: dict | None = None, 日志回调=None):
        self.配置 = 配置 if 配置 is not None else 加载配置()
        self.日志回调 = 日志回调 or (lambda 消息, 级别="信息": None)
        self._规格表 = 适配器规格表(self.配置)
        self._适配器: dict[str, 子进程适配器] = {}
        self._守卫: dict[str, 上传守卫] = {}
        self._敏感词库: 敏感词数据库 | None = None
        self._敏感词库失败 = False
        self.引擎: 传输引擎 | None = None
        self._锁 = threading.RLock()

    # ---------- 规格 ----------

    @property
    def 规格表(self) -> dict[str, 适配器规格]:
        return dict(self._规格表)

    @property
    def 实例列表(self) -> list[dict]:
        return 网盘实例列表(self.配置)

    @property
    def 名称表(self) -> dict[str, str]:
        return 实例名称表(self.配置)

    def 规格(self, 标识: str) -> Optional[适配器规格]:
        return self._规格表.get(标识文本(标识))

    def 名称(self, 标识) -> str:
        return 显示名(标识, self.名称表)

    def _日志(self, 消息: str, 级别: str = "信息") -> None:
        """统一的日志出口：兼容只收一个参数的回调。"""
        try:
            self.日志回调(消息, 级别)
        except TypeError:
            try:
                self.日志回调(消息)
            except Exception:
                pass
        except Exception:
            pass

    def 重载(self, 配置: dict | None = None) -> None:
        """配置变更后调用：关闭所有子进程，按新配置重建规格表。"""
        if 配置 is not None:
            self.配置 = 配置
        with self._锁:
            适配器们 = list(self._适配器.values())
            self._适配器.clear()
        for 适配器 in 适配器们:
            try:
                适配器.关闭()
            except Exception:
                pass
        if self.引擎 is not None:
            try:
                self.引擎.关闭()
            except Exception:
                pass
            self.引擎 = None
        self._规格表 = 适配器规格表(self.配置)

    def 移除守卫(self, 标识: str) -> None:
        with self._锁:
            self._守卫.pop(标识文本(标识), None)

    def 移除适配器(self, 标识: str) -> None:
        """只关掉某一个实例的子进程（删除/编辑网盘时用）。

        引擎里如果还引用着这个实例，也一并摘掉；下次 :meth:`构建引擎`
        会按需重建调度器（已有子进程保持存活）。
        """
        标识 = 标识文本(标识)
        self.移除守卫(标识)
        with self._锁:
            适配器 = self._适配器.pop(标识, None)
            if self.引擎 is not None:
                self.引擎.适配器表.pop(标识, None)
                self.引擎.名称表.pop(标识, None)
        if 适配器 is not None:
            try:
                适配器.关闭()
            except Exception:
                pass

    def 刷新规格表(self) -> None:
        """配置变更后刷新规格表（不关闭已有子进程，也不打断在跑的批次）。"""
        self._规格表 = 适配器规格表(self.配置)

    # ---------- 敏感词（预检改名 + 失败绕过） ----------

    def 敏感词库(self) -> "敏感词数据库 | None":
        """惰性创建并复用敏感词库；未启用或打不开时返回 None（功能自动降级）。"""
        参数 = 敏感词配置(self.配置)
        if not 参数["启用"]:
            return None
        with self._锁:
            if self._敏感词库 is not None:
                return self._敏感词库
            if self._敏感词库失败:
                return None
            try:
                self._敏感词库 = 敏感词数据库(参数["数据库路径"])
                self._日志(f"[敏感词] 已加载词库：{参数['数据库路径']}")
            except Exception as e:  # noqa: BLE001
                self._敏感词库失败 = True
                self._日志(f"[敏感词] 词库不可用，已降级为不改名：{e}")
                return None
            return self._敏感词库

    def 守卫(self, 标识) -> "上传守卫 | None":
        """取（或建）某个网盘的敏感词上传守卫；敏感词关闭时返回 None。"""
        标识 = 标识文本(标识)
        参数 = 敏感词配置(self.配置)
        if not 参数["启用"]:
            return None
        if not 参数["按网盘启用"].get(标识, True):
            return None
        库 = self.敏感词库()
        if 库 is None:
            return None
        with self._锁:
            已有 = self._守卫.get(标识)
            if 已有 is not None and 已有.适配器 is self._适配器.get(标识):
                return 已有
            try:
                适配器 = self.适配器(标识)
            except Exception as e:  # noqa: BLE001
                self._日志(f"[敏感词] {标识} 适配器不可用，跳过守卫：{e}", "警告")
                return None
            守卫 = 上传守卫(
                适配器, 标识, 库,
                启用=True,
                自动改名=参数["自动改名上传"],
                日志=self.日志回调)
            self._守卫[标识] = 守卫
            return 守卫

    def 守卫表(self, 标识们=None) -> dict[str, 上传守卫]:
        标识们 = list(self._规格表.keys()) if 标识们 is None else list(标识们)
        结果: dict[str, 上传守卫] = {}
        for 标识 in 标识们:
            守卫 = self.守卫(标识)
            if 守卫 is not None and 守卫.生效:
                结果[标识文本(标识)] = 守卫
        return 结果

    # ---------- 适配器 / 引擎 ----------

    def 适配器(self, 网盘) -> 子进程适配器:
        标识 = 标识文本(网盘)
        with self._锁:
            适配器 = self._适配器.get(标识)
            if 适配器 is not None:
                return 适配器
            规格 = self._规格表.get(标识)
            if 规格 is None:
                raise KeyError(f"配置中未启用该网盘：{标识}（{self.名称(标识)}）")
            适配器 = 子进程适配器(
                规格,
                日志回调=lambda 级别, 消息, _n=标识, _s=规格: self._日志(
                    f"[{_s.图标} {self.名称(_n)}] {消息}", 级别),
            )
            适配器.启动()
            self._适配器[标识] = 适配器
            return 适配器

    def 构建引擎(self, 需要: "list | tuple | None" = None) -> 传输引擎:
        if 需要 is None:
            需要 = list(self._规格表.keys())
        标识们 = [标识文本(x) for x in 需要]
        if self.引擎 is not None:
            缺少 = [x for x in 标识们 if x not in self.引擎.适配器表]
            if not 缺少:
                return self.引擎
            # 引擎缺少新加入的实例：只重建调度器，适配器子进程保持存活
            try:
                self.引擎.关闭()
            except Exception:
                pass
            self.引擎 = None
        表: dict[str, 子进程适配器] = {}
        for 标识 in 标识们:
            表[标识] = self.适配器(标识)
        self.引擎 = 传输引擎(
            表, 日志回调=self.日志回调,
            数据库路径=数据库路径(self.配置),
            名称表=self.名称表,
            守卫表=self.守卫表(标识们))
        return self.引擎

    def 关闭(self) -> None:
        with self._锁:
            适配器们 = list(self._适配器.values())
            self._适配器.clear()
        for 适配器 in 适配器们:
            try:
                适配器.关闭()
            except Exception:
                pass
        if self.引擎 is not None:
            try:
                self.引擎.关闭()
            except Exception:
                pass
        self.引擎 = None
        with self._锁:
            库 = self._敏感词库
            self._敏感词库 = None
            self._守卫.clear()
        if 库 is not None:
            try:
                库.关闭()
            except Exception:
                pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.关闭()


__all__ = [
    "项目根", "工作区根", "适配器根", "配置文件", "默认配置", "实例目录名",
    "加载配置", "保存配置", "适配器规格表", "传输参数", "下载参数",
    "数据库路径", "缓存参数", "界面配置", "设置界面配置", "动作",
    "网盘实例列表", "写入实例列表", "规范化实例", "实例名称表",
    "查找实例", "解析标识", "生成唯一标识", "新增网盘实例",
    "准备假网盘目录", "数据目录", "敏感词配置", "敏感词数据库路径",
    "AI配置", "设置AI配置", "保存AI配置", "AI学习库路径",
    "更新网盘实例", "删除网盘实例", "实例被引用", "复制适配器项目",
    "建议副本目录", "默认适配器路径", "解析路径", "类型图标",
]
