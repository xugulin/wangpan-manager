# v8_3/AI/运行时.py
"""AI 运行时门面 —— 界面/引擎共用的一个入口（V8_3 自有实现，非 V8 移植）。

``v8_3/界面/主窗口.py`` 里的用法::

    from ..AI.运行时 import AI运行时
    运行时 = AI运行时(self.配置, self.配置路径, 日志回调=self.追加日志)
    self._传输页面.AI调度器 = 运行时.调度器

构造函数签名
============
``AI运行时(配置=None, 配置路径=None, 日志回调=None, *,
          自动刷新价格=False, 初始化联网=None, 禁用网络=False,
          建学习库=None)``

* ``配置``：整份配置 dict（``加载配置()`` 的返回值）；省略则 ``加载配置()``；
* ``配置路径``：``配置.json`` 路径（保存余额时用）；省略用 V8_3 默认路径；
* ``日志回调``：``callable(文本)``，界面拿它往日志页写一行；
* ``自动刷新价格=False``：构造阶段不联网抓价（界面可手动 ``刷新价格()``）；
* AI 未启用 / 无密钥时**全部降级**：``调度器`` 仍可用（只出规则），
  不建学习库、不发网络请求，界面无需额外判断。

属性
====
``配置``、``配置路径``、``AI配置``、``价格抓取器``、``时段管理器``、
``预算管理器``、``学习库``、``助手``、``调度器``、``相似度函数``

方法
====
``是否可用()``、``获取统计()``、``获取状态摘要()``、``获取模型列表()``、
``获取缓存价格描述(模型名=None)``、``获取价格历史(限制=50)``、
``获取当前时段类型()``、``获取时段摘要()``、``获取预算摘要()``、
``刷新价格()``、``刷新余额(落盘=True)``、``刷新模型()``、
``设置手动覆盖(状态)``、``保存配置()``、``关闭()``
"""
import logging
from pathlib import Path
from typing import Optional

from ..配置 import 加载配置, 保存AI配置, 配置文件 as 默认配置文件
from .配置适配 import 取AI配置

logger = logging.getLogger(__name__)


class AI运行时:
    """把 AI 层各部件装到一个对象上，界面只跟它打交道。"""

    def __init__(self, 配置=None, 配置路径=None, 日志回调=None, *,
                 自动刷新价格: bool = False, 初始化联网=None,
                 禁用网络: bool = False, 建学习库=None):
        from . import 装配AI层            # 延迟导入，避免包初始化期循环

        self.配置 = 配置 if isinstance(配置, dict) else 加载配置()
        self.配置路径 = Path(配置路径) if 配置路径 else 默认配置文件
        self._日志回调 = 日志回调

        组件 = 装配AI层(
            self.配置, 自动刷新价格=自动刷新价格,
            初始化联网=初始化联网, 禁用网络=禁用网络,
            建学习库=建学习库)

        self.AI配置 = 组件["配置"]
        self.价格抓取器 = 组件["价格抓取器"]
        self.时段管理器 = 组件["时段管理器"]
        self.预算管理器 = 组件["预算管理器"]
        self.学习库 = 组件["学习库"]
        self.助手 = 组件["AI助手"]
        self.调度器 = 组件["AI调度器"]
        self.相似度函数 = 组件["相似度函数"]

        self._日志(f"[AI] 运行时就绪：模型 {self.AI配置.get('模型')} | "
                 f"{self.时段管理器.获取时段摘要()} | "
                 f"{self.预算管理器.获取预算摘要()}")
        # 本地模型状态：开了就报是否可用，没开也报一句（便于用户发现这台机器能白嫖）
        if self.助手.本地模型配置.启用:
            try:
                状态 = self.获取本地模型状态()
                self._日志(f"[AI] {状态.一行() if 状态 else '本地模型未检测'}")
                if 状态 is not None and not 状态.可用:
                    self._日志(f"[AI] {状态.说明.splitlines()[0]}")
            except Exception as e:  # noqa: BLE001
                self._日志(f"[AI] 本地模型检测异常：{e}")

    # ==================== 日志 ====================

    def _日志(self, 文本: str):
        logger.info("%s", 文本)
        if callable(self._日志回调):
            try:
                self._日志回调(str(文本))
            except Exception:
                pass

    # ==================== 状态 ====================

    def 是否可用(self) -> bool:
        """密钥 / AI 开关 / 时段 / 预算 都满足才 True（不发网络请求）。"""
        try:
            return bool(self.助手.是否可用())
        except Exception:
            return False

    def 获取统计(self) -> dict:
        return self.调度器.获取统计()

    def 获取状态摘要(self) -> dict:
        return {
            "可用": self.是否可用(),
            "模型": self.AI配置.get("模型"),
            "模型列表": list(self.获取模型列表()),
            "接口地址": self.AI配置.get("接口地址"),
            "时段": self.时段管理器.获取当前时段类型(),
            "时段摘要": self.时段管理器.获取时段摘要(),
            "预算摘要": self.预算管理器.获取预算摘要(),
            "余额": self.预算管理器.当前余额,
            "已熔断": self.预算管理器.是否已熔断,
            "价格描述": self.获取缓存价格描述(),
            "价格元信息": self.价格抓取器.获取元信息(),
            "token统计": self.助手.获取token统计(),
            "调度统计": self.获取统计(),
            "学习库": getattr(self.学习库, "数据库路径", ""),
            # ---- 本地 DeepSeek 模型（V8_3 新增）----
            "本地模型": self.获取本地模型摘要(),
        }

    # ==================== 本地模型（V8_3 新增） ====================

    def 获取本地模型状态(self, 重新检测: bool = False):
        """本地模型状态对象（不可用时也返回对象，不抛异常）。"""
        try:
            return self.助手.获取本地模型状态(重新检测=重新检测)
        except Exception:
            return None

    def 获取本地模型摘要(self) -> dict:
        try:
            状态 = self.获取本地模型状态()
            if 状态 is None:
                return {"启用": False, "可用": False, "说明": "未初始化"}
            return {"启用": bool(self.助手.本地模型配置.启用),
                    **状态.to_dict()}
        except Exception as e:  # noqa: BLE001
            return {"启用": False, "可用": False, "说明": f"检测失败：{e}"}

    def 获取本地模型一行(self) -> str:
        """一行摘要（终端/日志/自检用）。"""
        try:
            配置 = self.助手.本地模型配置
            状态 = self.获取本地模型状态()
            if not 配置.启用:
                if 状态 is not None and 状态.可用:
                    return (f"⚪ 本地模型已关闭（检测到可用服务：{状态.地址} · "
                          f"{状态.模型}；开启后免费用）")
                return "⚪ 本地模型已关闭（未启用）"
            return 状态.一行() if 状态 is not None else "⚪ 本地模型未检测"
        except Exception as e:  # noqa: BLE001
            return f"⚪ 本地模型检测失败：{e}"

    def 本地模型测速(self) -> dict:
        try:
            return self.助手.本地模型测速()
        except Exception as e:  # noqa: BLE001
            return {"成功": False, "错误": str(e)}

    def 启动本地服务(self):
        try:
            return self.助手.本地模型.启动服务()
        except Exception as e:  # noqa: BLE001
            return False, str(e)

    def 一键装本地模型(self, 进度回调=None) -> tuple[bool, str]:
        """一键把离线模型装好：检测 → 内置基座缺失就补 → 启动 → 拉模型 → 打开开关。

        基座（官方便携版 ollama）**已经随包内置**在 ``运行环境/本地模型``，
        所以正常情况下这里只下载模型权重；只有内置那份缺失/损坏时才补一份。

        全程在后台线程里跑（AI 页负责起线程），这里只管按步骤做、把进展回调出去。
        """
        def 报(文本: str) -> None:
            if 进度回调 is not None:
                try:
                    进度回调(文本)
                except Exception:
                    pass

        from .本地模型 import (下载便携运行时, 项目内可执行文件, 默认模型)
        报("① 检测本机是否已有可用推理服务…")
        状态 = self.获取本地模型状态(重新检测=True)
        if 状态 is None or not getattr(状态, "可用", False):
            可执行 = 项目内可执行文件()
            if not (可执行.is_file() or __import__("shutil").which("ollama")):
                报("② 内置 ollama 基座缺失，正在补一份官方便携版（约 1.4 GB）…")
                好, 消息 = 下载便携运行时()
                if not 好:
                    return False, f"补内置 ollama 失败：{消息}"
                报(f"   {消息}")
            报("③ 启动本地服务…")
            好, 消息 = self.启动本地服务()
            if not 好:
                return False, f"启动本地服务失败：{消息}"
            报(f"   {消息}")
        模型 = ""
        try:
            模型 = str(self.助手.本地模型配置.模型 or "") or 默认模型
        except Exception:
            模型 = 默认模型
        报(f"④ 拉取模型 {模型}（首次要下 1 GB 左右，之后就不用再下）…")
        好, 消息 = self.拉取本地模型(模型)
        if not 好:
            return False, f"拉取模型失败：{消息}"
        报("⑤ 打开本地模型开关并保存…")
        self.保存本地模型配置(启用=True, 模型=模型)
        状态 = self.获取本地模型状态(重新检测=True)
        可用 = bool(getattr(状态, "可用", False))
        return 可用, (f"完成：{模型} 已就绪"
                   + ("" if 可用 else "（服务还没就绪，稍后点「检测」再看）"))

    def 拉取本地模型(self, 模型: str = ""):
        try:
            return self.助手.本地模型.拉取模型(模型)
        except Exception as e:  # noqa: BLE001
            return False, str(e)

    def 保存本地模型配置(self, **变更) -> None:
        try:
            self.助手.保存本地模型配置(**变更)
        except Exception as e:  # noqa: BLE001
            self._日志(f"[AI] 保存本地模型配置失败：{e}")

    @staticmethod
    def 本地模型安装指引() -> str:
        from .本地模型 import 本地模型客户端
        return 本地模型客户端.安装指引()

    def 获取模型列表(self) -> list:
        try:
            return list(self.助手.获取模型列表())
        except Exception:
            return []

    def 获取缓存价格描述(self, 模型名: str = None) -> str:
        return self.助手.获取缓存价格描述(模型名)

    def 获取价格历史(self, 限制: int = 50) -> list:
        return self.价格抓取器.获取历史(限制)

    def 获取当前时段类型(self) -> str:
        return self.时段管理器.获取当前时段类型()

    def 获取时段摘要(self) -> str:
        return self.时段管理器.获取时段摘要()

    def 获取预算摘要(self) -> str:
        return self.预算管理器.获取预算摘要()

    # ==================== 动作 ====================

    def 设置手动覆盖(self, 状态):
        """``None`` 自动；``True`` 强制开 AI；``False`` 强制关 AI。"""
        self.时段管理器.设置手动覆盖(状态)
        self._日志(f"[AI] 手动覆盖 → {self.时段管理器.获取时段摘要()}")

    def 刷新模型(self) -> list:
        """重新拉一次可用模型列表（无密钥时直接用内置白名单）。"""
        try:
            self.助手._获取并缓存模型列表()
        except Exception as e:
            self._日志(f"[AI] 刷新模型失败：{e}")
        return self.获取模型列表()

    def 刷新价格(self) -> bool:
        """手动抓一次 DeepSeek 官网价格（``禁用网络`` 时直接 False）。"""
        成功 = False
        try:
            成功 = bool(self.价格抓取器.手动刷新())
        except Exception as e:
            self._日志(f"[AI] 刷新价格失败：{e}")
        if 成功:
            try:
                self.助手.注入价格抓取器(self.价格抓取器)
            except Exception:
                pass
            self._日志(f"[AI] 价格已刷新：{self.获取缓存价格描述()}")
        else:
            self._日志("[AI] 价格刷新未成功（离线或已禁用网络），沿用缓存/兜底")
        return 成功

    def 刷新余额(self, 落盘: bool = True) -> str:
        """查一次真实余额；成功且 ``落盘`` 时把余额写回 ``配置.json``。

        无密钥 / 禁用网络时只记一条日志并返回提示，不发请求、不抛异常。
        """
        结果 = self.助手.查询真实余额()
        if not 结果.get("成功"):
            self.预算管理器.标记查询失败()
            提示 = f"⚠️ 余额查询失败: {结果.get('错误', '')}"
            self._日志(f"[AI] {提示}")
            return 提示

        刚熔断 = self.预算管理器.更新余额(结果["余额"])
        提示 = (f"🚨 余额 ¥{结果['余额']:.2f} 已熔断！" if 刚熔断
              else f"✅ 余额 ¥{结果['余额']:.2f}")
        self._日志(f"[AI] {提示}")
        if 落盘:
            try:
                if self.预算管理器.同步到配置(self.配置):
                    self.保存配置()
            except Exception as e:
                self._日志(f"[AI] 余额回写失败：{e}")
        return 提示

    def 保存配置(self) -> bool:
        """把内存里的配置写回 ``配置.json``（含 AI.预算.当前余额）。"""
        try:
            保存AI配置(self.配置, self.配置路径)
            return True
        except Exception as e:
            self._日志(f"[AI] 保存配置失败：{e}")
            return False

    def 关闭(self):
        try:
            if self.学习库 is not None:
                self.学习库.关闭()
        except Exception:
            pass
