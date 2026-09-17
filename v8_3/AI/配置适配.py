# v8_3/AI/配置适配.py
"""V8_3 AI 层的配置适配小工具（内部模块，不在 ``v8_3.AI`` 里导出）。

背景
====
V8 的 AI 层依赖它自己的配置加载器（``配置.获取("a.b", 默认)``）。
V8_3 不引入这个加载器：配置就是普通 ``dict``，AI 段统一由
``v8_3.配置.AI配置()`` 产出。为了让移植过来的类既能吃 V8_3 的 dict，
也能吃 V8 风格的“带 ``.获取()`` 的配置对象”，这里只做一层薄适配：

* :func:`取AI配置` —— 任意形态 → 与 ``AI配置()`` 同构的 dict（缺项补默认）；
* :func:`配置取值` —— 从 dict / 配置对象里读一个键；
* :func:`取模型名` —— 兼容 V8 的 ``model`` 与 V8_3 的 ``模型`` 键名；
* :func:`深合并` —— 递归合并 dict（不修改入参）。

本模块不联网、不落盘，也不 import 任何 V8 模块。
"""
from __future__ import annotations

from typing import Any, Optional

from ..配置 import AI配置

__all__ = ["取AI配置", "配置取值", "取模型名", "深合并"]

#: V8_3 ``AI配置()`` 里的标量键（用于兼容 V8 配置对象）
_标量键 = ("启用", "api密钥", "接口地址", "模型", "温度", "max_tokens")
#: V8_3 ``AI配置()`` 里的子段键
_段键 = ("预算", "价格抓取", "高峰时段", "调度", "本地模型")


def 深合并(基底: dict, 覆盖: Optional[dict]) -> dict:
    """递归合并两个 dict，返回新 dict（``覆盖`` 优先）。"""
    结果 = dict(基底 or {})
    for 键, 值 in (覆盖 or {}).items():
        if isinstance(值, dict) and isinstance(结果.get(键), dict):
            结果[键] = 深合并(结果[键], 值)
        else:
            结果[键] = 值
    return 结果


def 配置取值(配置, 键: str, 默认=None):
    """从普通 dict 或 V8 风格配置对象（有 ``获取(键, 默认)``）里读一个键。"""
    if 配置 is None:
        return 默认
    if isinstance(配置, dict):
        return 配置.get(键, 默认)
    获取 = getattr(配置, "获取", None)
    if callable(获取):
        try:
            return 获取(键, 默认)
        except Exception:
            return 默认
    return 默认


def 取AI配置(配置=None) -> dict:
    """把任意形态的配置统一成 ``AI配置()`` 同构的 dict。

    支持：

    * ``None`` → ``AI配置()``（读 V8_3 自己的 ``配置.json``）；
    * 整份配置 dict（含 ``"AI"`` 段）→ ``AI配置(整份)``；
    * AI 段 dict（含 ``高峰时段`` / ``预算`` / ``价格抓取`` / ``调度`` 之一，
      或直接就是 ``AI配置()`` 的返回值）→ 与默认值深合并，缺项补默认；
    * V8 风格对象（有 ``.获取()``，例如 V8 的 ``配置加载器``）→ 尽量按
      V8 的键名读出来（含 ``ai_settings`` / ``budget`` / ``ai_schedule``）。

    注意：本函数**总是**返回一个新 dict，调用方改它不会影响入参配置；
    但内部子段在“整份配置 dict”分支下是深合并产生的新 dict，
    因此需要写回时请把**原始配置对象**交给 :class:`预算管理器` 之类的类。

    V8_3 特别说明：``本地模型`` 是 V8_3 新增段。**只要调用方给了配置**，
    就不从磁盘上的 ``配置.json`` 回填这一段（否则"传了空配置做离线测试"会被
    开发机上真实运行的本地模型影响）——给配置时默认"本地模型关闭"。
    """
    基底 = AI配置()
    if 配置 is None:
        return 基底
    if isinstance(配置, dict):
        AI段 = 配置.get("AI")
        片段 = dict(AI段) if isinstance(AI段, dict) else dict(配置)
        # V8 的 ai_settings 用的是 "model"，这里补一份中文键名
        if "模型" not in 片段 and isinstance(片段.get("model"), str):
            片段["模型"] = 片段["model"]
        结果 = 深合并(基底, 片段)
        if "本地模型" not in 片段:
            # 调用方没提本地模型 → 视为关闭（别把磁盘配置里的开启状态带进来）
            结果["本地模型"] = {**dict(基底.get("本地模型") or {}), "启用": False}
        return 结果
    if hasattr(配置, "获取"):
        片段: dict[str, Any] = {}
        for 键 in _标量键:
            值 = 配置取值(配置, 键, None)
            if 值 is not None:
                片段[键] = 值
        # V8 的键名（ai_settings 里放的是 model 等）
        V8片段 = 配置取值(配置, "ai_settings", None)
        if isinstance(V8片段, dict):
            for 键 in ("模型", "温度", "max_tokens", "接口地址"):
                值 = V8片段.get(键, V8片段.get("model"
                                            if 键 == "模型" else 键))
                if 值 is not None:
                    片段[键] = 值
        for 键 in _段键:
            值 = 配置取值(配置, 键, None)
            if isinstance(值, dict):
                片段[键] = 值
        预算 = 配置取值(配置, "budget", None)
        if isinstance(预算, dict):
            片段["预算"] = {
                "初始充值金额": 预算.get("初始充值金额",
                                 预算.get("initial_amount",
                                        基底["预算"]["初始充值金额"])),
                "低余额阈值": 预算.get("低余额阈值",
                                预算.get("low_balance_threshold",
                                       基底["预算"]["低余额阈值"])),
                "当前余额": 预算.get("当前余额",
                               预算.get("current_balance",
                                      基底["预算"]["当前余额"])),
            }
        时段 = 配置取值(配置, "ai_schedule", None)
        if isinstance(时段, dict) and 时段.get("时段"):
            片段["高峰时段"] = 时段
        结果 = 深合并(基底, 片段)
        段 = 配置取值(配置, "本地模型", None)
        if isinstance(段, dict):
            结果["本地模型"] = 深合并(结果.get("本地模型") or {}, 段)
        else:
            结果["本地模型"] = {**dict(结果.get("本地模型") or {}), "启用": False}
        return 结果
    return 基底


def 取模型名(AI段: Optional[dict], 默认: str = "deepseek-flash") -> str:
    """读模型名：优先 V8_3 的 ``模型``，其次 V8 的 ``model``。"""
    AI段 = AI段 or {}
    return str(AI段.get("模型") or AI段.get("model") or 默认)
