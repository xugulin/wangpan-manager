# v8_3/AI/AI智能助手.py
"""AI 智能助手 - DeepSeek 对话 + 结构化策略请求。

移植自 V8 ``AI层/AI智能助手.py``，改动：

* ``requests`` → ``httpx``（``timeout`` / ``headers`` / ``json`` 语义一致，
  异常按 ``httpx.HTTPError`` 处理，状态码走 ``raise_for_status()``）；
* 不再依赖 V8 的 ``配置加载器``：``api密钥`` / ``模型`` / 温度 等一律来自
  ``AI配置()`` 的普通 dict（或用 ``ai配置`` 传 V8 风格的小 dict）；
* 构造函数同时支持 V8 与 V8_3 两种写法（见下）；
* **无密钥 / AI 关闭 / ``禁用网络=True`` 时全程降级**：不发任何网络请求、
  不抛异常，直接用内置模型白名单与内置价目表，策略相关请求返回空 dict，
  由调用方（``AI调度器``）回退到规则；
* 自适应 token 表默认落在 V8_3 的 ``数据/AI_token自适应.json``；
* 日志走 ``logging``，且**从不打印 api密钥**。

构造函数签名
============
``AI智能助手(api密钥=None, 时段管理器=None, 预算管理器=None, ai配置=None,
            *, 配置=None, 初始化联网=None, 禁用网络=False,
            数据目录路径=None, 持久化路径=None, 价格抓取器=None)``

四种等价写法：

```python
AI智能助手(AI配置())                       # V8_3：一个 dict 全搞定
AI智能助手(配置=整份配置)                    # V8_3：整份配置（含 "AI" 段）
AI智能助手(api密钥="sk-xxx", 配置=整份配置)   # V8_3：密钥与配置都显式给
AI智能助手(api密钥=key, 时段管理器=时段, 预算管理器=预算,
          ai配置={"model": "deepseek-flash"})   # V8 老写法（关键字同名）
```

* ``初始化联网=None``：默认“有密钥且 AI 启用时才在构造阶段拉模型列表/价格”；
  显式 ``False`` 可以做到构造阶段零联网（UI 想自己控制时机时用）；
* ``禁用网络=True``：彻底禁止本实例发任何 HTTP 请求（离线/测试用）。

公开方法
========
``注入价格抓取器(抓取器)``、``是否可用()``、``获取模型列表()``、
``获取原始模型ID(标准名)``、``获取建议max_tokens(模型名)``、``获取自适应表()``、
``获取token统计()``、``获取缓存价格描述(模型名=None)``、``查询真实余额()``、
``检查预算并刷新余额()``、``请求结构化策略(系统提示, 用户消息, 强制模型=None)``、
``请求文件优先级(文件列表, 最大样本=30)``、``请求失败诊断(错误信息, 文件名, 已重试次数)``、
``请求运行时调优(当前并发, 吞吐_MBps, 失败率, 队列长度, 已完成, 总数)``
"""
import json
import os
import re
import time
import logging
import threading

import httpx

from ..配置 import 数据目录
from .配置适配 import 取AI配置, 取模型名
from .本地模型 import (
    本地模型客户端, 取本地模型配置,
)
from .时段预算 import (
    时段管理器 as 时段管理器类, 预算管理器 as 预算管理器类,
)

logger = logging.getLogger(__name__)


class AI智能助手:
    #: 默认（DeepSeek 官方）地址；实际用哪个厂家由 ``self.厂家`` 决定
    余额查询地址 = "https://api.deepseek.com/user/balance"
    模型列表地址 = "https://api.deepseek.com/models"
    对话接口地址 = "https://api.deepseek.com/chat/completions"

    @staticmethod
    def _拼地址(基地址: str, 路径: str) -> str:
        """按厂家的接口地址拼出具体端点（自动处理结尾的 / 与 /v1）。

        用户要求"直接填链接就行"，所以这里不认厂家品牌，只认地址：
        任何 OpenAI 兼容网关（百炼 / Kimi / 智谱 / 火山 / 硅基流动 / 自建…）都适用。
        """
        基 = str(基地址 or "").strip().rstrip("/")
        if not 基:
            return ""
        路径 = 路径.lstrip("/")
        return f"{基}/{路径}"

    #: 非对话类模型的关键词：这些排到列表后面（不是不能用，是不适合当聊天模型）
    _非对话关键词 = ("embedding", "rerank", "tts", "asr", "speech", "voice",
                 "cosyvoice", "sambert", "image", "video", "wan", "ocr",
                 "moderation", "translat")

    @classmethod
    def _模型门类(cls, 名: str) -> int:
        """0 = 像对话模型（排前面），1 = 其它（向量/语音/图像…排后面）。"""
        小写 = str(名 or "").lower()
        return 1 if any(k in 小写 for k in cls._非对话关键词) else 0

    def _取厂家(self) -> dict:
        """当前在用的厂家（含接口地址、密钥、模型）。

        厂家信息挂在整份配置的 ``AI.在线模型`` 上；助手这边持有 ``_原始配置``，
        拿不到就退回一条"只含当前密钥/地址"的兜底，保证老调用方式也能跑。
        """
        try:
            from ..配置 import 当前厂家
            整份 = getattr(self, "_原始配置", None)
            if isinstance(整份, dict) and 整份.get("AI"):
                return dict(当前厂家(整份))
        except Exception:      # noqa: BLE001
            pass
        return {"接口地址": str(self.AI配置.get("接口地址") or ""),
                "密钥": self.api密钥,
                "模型": str(self.AI配置.get("模型") or ""),
                "名称": "当前厂家"}

    def _地址(self, 路径: str, 兜底: str) -> str:
        厂家 = self._取厂家()
        地址 = self._拼地址(厂家.get("接口地址", ""), 路径)
        return 地址 or 兜底

    @property
    def 是DeepSeek(self) -> bool:
        """是不是 DeepSeek 官方（余额/峰谷价/模型别名这些只对它有意义）。"""
        厂家 = self._取厂家()
        地址 = str(厂家.get("接口地址") or "").lower()
        return (not 地址) or ("deepseek" in 地址) or (厂家.get("名称", "").startswith("DeepSeek"))

    官方模型白名单 = ["deepseek-flash", "deepseek-v4-pro"]

    模型别名 = {
        "deepseek-v4-flash": "deepseek-flash",
        "deepseek-v4-flash-vision-exp": "deepseek-flash",
        "deepseek-flash-vision-exp": "deepseek-flash",
    }

    废弃模型黑名单 = [
        "deepseek-chat", "deepseek-reasoner", "deepseek-coder",
        "deepseek-v3", "deepseek-v3.1", "deepseek-v3.2",
        "deepseek-r1", "deepseek-v4.1-flash",
    ]

    _token下界 = 512
    _token上界 = 8192
    _token起步 = 2048
    _安全系数 = 1.5
    _最小保存间隔 = 5.0

    #: V8 里写死 ``持久化路径 = "数据库/AI_token自适应.json"``；
    #: V8_3 改成落在自己的 ``数据/`` 目录，构造时可覆盖。
    持久化文件名 = "AI_token自适应.json"

    def __init__(self, api密钥=None, 时段管理器=None, 预算管理器=None,
                 ai配置=None, *, 配置=None, 初始化联网=None,
                 禁用网络: bool = False, 数据目录路径=None,
                 持久化路径=None, 价格抓取器=None):
        # ---- 兼容 V8_3 写法：第一个位置参数可能就是配置 dict ----
        if isinstance(api密钥, dict) and 配置 is None:
            配置, api密钥 = api密钥, None
        if isinstance(时段管理器, dict) and 配置 is None:
            配置, 时段管理器 = 时段管理器, None
        if isinstance(预算管理器, dict) and 配置 is None:
            配置, 预算管理器 = 预算管理器, None
        if isinstance(ai配置, dict) and 配置 is None:
            配置, ai配置 = ai配置, None

        self._原始配置 = 配置 if isinstance(配置, dict) else None
        self.AI配置 = 取AI配置(配置 if 配置 is not None else ai配置)
        self.ai配置 = self.AI配置              # V8 兼容别名
        # ---- 本地 DeepSeek 模型（V8_3 新增）----
        # 免费、离线、不上传数据；启用后优先用本地，云端仅在本地失败时兜底。
        self.本地模型配置 = 取本地模型配置(self.AI配置)
        self.本地模型 = 本地模型客户端(self.本地模型配置)
        self.本地状态缓存 = None               # 最近一次检测结果
        self.本地调用次数 = 0
        self.云端调用次数 = 0
        # 只有"没传"（None）才回落到配置里的密钥；
        # 显式传空串表示"本次不要密钥"，避免悄悄用上项目配置里的密钥。
        self.api密钥 = str(
            (self.AI配置.get("api密钥") if api密钥 is None else api密钥)
            or "").strip()
        self.禁用网络 = bool(禁用网络)

        self.时段管理器 = 时段管理器 if 时段管理器 is not None else \
            时段管理器类(配置=self._原始配置 or self.AI配置,
                      打印启动状态=False)
        self.预算管理器 = 预算管理器 if 预算管理器 is not None else \
            预算管理器类(配置=self._原始配置 or self.AI配置)

        self.持久化路径 = str(
            持久化路径
            or os.path.join(str(数据目录路径 or 数据目录()),
                            self.持久化文件名))

        self.累计消耗 = 0.0
        self.可用模型列表 = []
        self._原始模型映射 = {}
        self.模型价格信息 = {}
        self._价格抓取器 = 价格抓取器

        self._自适应表 = {}
        self._自适应锁 = threading.Lock()
        self._上次保存时间 = 0.0

        self._token统计 = {
            "总尝试": 0, "总成功": 0,
            "总失败": 0, "总费用": 0.0,
        }

        self.自动联网 = (bool(self.api密钥)
                      and bool(self.AI配置.get("启用", True))
                      and not self.禁用网络) \
            if 初始化联网 is None else (bool(初始化联网)
                                    and not self.禁用网络)

        self._初始化()

    # ==================== 初始化 ====================

    def _初始化(self):
        logger.info("=" * 50)
        logger.info("🤖 AI 助手初始化中... (密钥: %s)",
                    "已配置" if self.api密钥 else "未配置")
        self._从磁盘加载()
        if self.自动联网:
            self._获取并缓存模型列表()
        else:
            self._使用内置模型列表()
        self._获取并缓存价格信息()
        logger.info("=" * 50)

    def _使用内置模型列表(self):
        self.可用模型列表 = list(self.官方模型白名单)
        self._原始模型映射 = {m: m for m in self.官方模型白名单}

    def 注入价格抓取器(self, 抓取器):
        self._价格抓取器 = 抓取器
        self._获取并缓存价格信息()
        logger.info("[AI助手] 💉 价格抓取器已注入")

    # ==================== 模型名归一化 ====================

    def _归一化模型名(self, 原始名: str) -> str:
        if not 原始名:
            return 原始名
        if 原始名 in self.模型别名:
            return self.模型别名[原始名]
        if 原始名 in self.官方模型白名单:
            return 原始名
        排序后 = sorted(self.官方模型白名单,
                        key=len, reverse=True)
        for 标准名 in 排序后:
            if 原始名 == 标准名:
                return 标准名
            if 原始名.startswith(标准名 + "-"):
                剩余 = 原始名[len(标准名) + 1:]
                if re.fullmatch(r'\d{4}(-[a-zA-Z0-9.\-]+)?', 剩余):
                    return 标准名
        return 原始名

    def _获取并缓存模型列表(self):
        if self.禁用网络 or not self.api密钥:
            self._使用内置模型列表()
            return
        try:
            响应 = httpx.get(
                self._地址("models", self.模型列表地址),
                headers={"Authorization":
                        f"Bearer {self.api密钥}"},
                timeout=15)
            响应.raise_for_status()
            数据 = 响应.json()
            api模型 = [str(m.get("id") or "") for m in 数据.get("data", [])
                      if m.get("id")]
            if api模型 and not self.是DeepSeek:
                # 别的厂家（百炼/Kimi/智谱/火山/硅基流动…）：**API 说什么就是什么**。
                # 以前这里一律套 DeepSeek 的白名单过滤，结果别家模型全被滤掉、
                # 只剩内置信箱里的 DeepSeek 名字 —— 用户反馈"模型名称不太准确"就是这个。
                厂家 = self._取厂家()
                推荐 = [str(x) for x in (厂家.get("推荐模型") or [])]
                排序 = {名: i for i, 名 in enumerate(推荐)}
                self.可用模型列表 = sorted(
                    dict.fromkeys(api模型),
                    key=lambda m: (0 if m in 排序 else 1,
                                   排序.get(m, 0),
                                   self._模型门类(m), m))
                self._原始模型映射 = {m: m for m in self.可用模型列表}
                logger.info("✅ 可用模型（%s）: %s",
                            厂家.get("名称") or "在线厂家", self.可用模型列表[:8])
                return
            有效模型 = []
            原始映射 = {}
            for 原始id in api模型:
                归一 = self._归一化模型名(原始id)
                if 原始id in self.废弃模型黑名单:
                    continue
                if 归一 in self.官方模型白名单:
                    if 归一 not in 有效模型:
                        有效模型.append(归一)
                        原始映射[归一] = 原始id
            for m in self.官方模型白名单:
                if m not in 有效模型:
                    有效模型.append(m)
                    原始映射[m] = m
            self.可用模型列表 = 有效模型
            self._原始模型映射 = 原始映射
            logger.info("✅ 可用模型: %s", self.可用模型列表)
        except httpx.HTTPError as e:
            logger.warning("获取模型列表失败(HTTP): %s", e)
            self._使用内置模型列表()
        except Exception as e:
            logger.warning("获取模型列表失败: %s", e)
            self._使用内置模型列表()

    # ==================== 价格 ====================

    def _获取并缓存价格信息(self):
        self.模型价格信息 = {}
        if self._价格抓取器:
            try:
                for 模型名 in self._价格抓取器.获取所有模型名():
                    价格 = self._价格抓取器.获取价格(模型名)
                    self.模型价格信息[模型名] = {
                        "空闲": {
                            "输入_缓存命中": 价格["空闲"]["缓存命中"],
                            "输入_缓存未命中":
                                价格["空闲"]["缓存未命中"],
                            "输出": 价格["空闲"]["输出"],
                        },
                        "高峰": {
                            "输入_缓存命中": 价格["高峰"]["缓存命中"],
                            "输入_缓存未命中":
                                价格["高峰"]["缓存未命中"],
                            "输出": 价格["高峰"]["输出"],
                        },
                    }
                if self.模型价格信息:
                    return
            except Exception as e:
                logger.warning("[AI助手] ⚠️ 价格抓取失败: %s", e)

        self.模型价格信息 = {
            "deepseek-flash": {
                "空闲": {"输入_缓存命中": 0.02,
                        "输入_缓存未命中": 1.0, "输出": 4.0},
                "高峰": {"输入_缓存命中": 0.04,
                        "输入_缓存未命中": 2.0, "输出": 8.0},
            },
            "deepseek-v4-pro": {
                "空闲": {"输入_缓存命中": 0.15,
                        "输入_缓存未命中": 4.5, "输出": 13.5},
                "高峰": {"输入_缓存命中": 0.30,
                        "输入_缓存未命中": 9.0, "输出": 27.0},
            },
        }

    def _选价格(self, 模型名: str) -> dict:
        真实名 = self.模型别名.get(模型名, 模型名)
        价格信息 = self.模型价格信息.get(真实名, {})
        if not 价格信息:
            价格信息 = self.模型价格信息.get(
                "deepseek-flash", {})
        try:
            时段 = (self.时段管理器.获取当前时段类型()
                   if self.时段管理器 else "空闲")
        except Exception:
            时段 = "空闲"
        if 时段 == "高峰":
            return 价格信息.get("高峰",
                            价格信息.get("空闲", {}))
        return 价格信息.get("空闲", {})

    def 获取缓存价格描述(self, 模型名: str = None) -> str:
        if not 模型名:
            模型名 = 取模型名(self.AI配置)
        价格 = self._选价格(模型名)
        return (f"缓存命中: ¥{价格.get('输入_缓存命中', 0):.3f}/M | "
                f"未命中: ¥{价格.get('输入_缓存未命中', 0):.2f}/M | "
                f"输出: ¥{价格.get('输出', 0):.2f}/M")

    # ==================== 余额 ====================

    def 查询真实余额(self) -> dict:
        if not self.api密钥:
            return {"成功": False, "余额": 0,
                    "错误": "未配置 API 密钥"}
        if self.禁用网络:
            return {"成功": False, "余额": 0,
                    "错误": "已禁用网络"}
        if not self.是DeepSeek:
            # 余额查询是 DeepSeek 官方专有接口，别家没有 —— 明确说清楚，
            # 而不是拿 DeepSeek 的地址去请求、报一个看不懂的错。
            return {"成功": False, "余额": 0,
                    "错误": "当前厂家不提供余额查询（只有 DeepSeek 官方有）"}
        try:
            响应 = httpx.get(
                self.余额查询地址,
                headers={"Authorization":
                        f"Bearer {self.api密钥}"},
                timeout=10)
            响应.raise_for_status()
            数据 = 响应.json()
            for 项 in 数据.get("balance_infos", []):
                if 项.get("currency") == "CNY":
                    return {"成功": True,
                            "余额": float(项.get("total_balance", 0))}
            return {"成功": True, "余额": 0.0}
        except httpx.HTTPError as e:
            return {"成功": False, "余额": 0, "错误": str(e)}
        except Exception as e:
            return {"成功": False, "余额": 0, "错误": str(e)}

    def 检查预算并刷新余额(self) -> str:
        结果 = self.查询真实余额()
        if 结果["成功"]:
            刚熔断 = self.预算管理器.更新余额(结果["余额"])
            if 刚熔断:
                return f"🚨 余额 ¥{结果['余额']:.2f} 已熔断！"
            return ""
        self.预算管理器.标记查询失败()
        return f"⚠️ 余额查询失败: {结果.get('错误', '')}"

    # ==================== 对外接口 ====================

    def 本地模型可用(self) -> bool:
        """本地模型是否已启用并且探测到可用服务（结果缓存，失败会重新探测）。"""
        if not self.本地模型配置.启用:
            return False
        状态 = self.本地状态缓存
        if 状态 is None or not 状态.可用:
            状态 = self.本地模型.检测()
            self.本地状态缓存 = 状态
        return bool(状态.可用)

    def 是否可用(self) -> bool:
        """密钥 / AI 开关 / 时段 / 预算 都满足时为 True（不发网络请求）。

        V8_3 起：**本地模型可用时不受密钥/时段/预算限制**——它免费、离线，
        没理由跟着云端的"高峰时段不调用"和余额熔断一起被卡住。
        """
        try:
            if self.本地模型配置.启用 and self.本地模型可用():
                return True
        except Exception:
            pass
        if not self.api密钥 or self.禁用网络:
            return False
        try:
            if not self.AI配置.get("启用", True):
                return False
        except Exception:
            pass
        try:
            if self.预算管理器 is not None and self.预算管理器.是否已熔断:
                return False
        except Exception:
            return False
        try:
            if self.时段管理器 is not None and \
                    not self.时段管理器.是否启用AI():
                return False
        except Exception:
            return False
        return True

    def 获取模型列表(self):
        return self.可用模型列表

    def 获取原始模型ID(self, 标准名: str) -> str:
        return self._原始模型映射.get(标准名, 标准名)

    def 获取建议max_tokens(self, 模型名: str) -> int:
        with self._自适应锁:
            v = self._自适应表.get(模型名)
            if v:
                return v["当前建议"]
        return self._token起步

    def 获取自适应表(self) -> dict:
        with self._自适应锁:
            return {k: dict(v) for k, v in self._自适应表.items()}

    def 获取token统计(self) -> dict:
        return dict(self._token统计)

    def _从磁盘加载(self):
        try:
            if not os.path.exists(self.持久化路径):
                return
            with open(self.持久化路径, "r",
                      encoding="utf-8") as f:
                数据 = json.load(f)
            with self._自适应锁:
                self._自适应表 = {}
                for 模型, v in 数据.items():
                    self._自适应表[模型] = {
                        "真实最大": int(v.get("真实最大", 0)),
                        "当前建议": int(v.get("当前建议",
                                            self._token起步)),
                        "成功次数": int(v.get("成功次数", 0)),
                        "失败次数": int(v.get("失败次数", 0)),
                        "更新时间": v.get("更新时间", ""),
                    }
        except Exception as e:
            logger.warning("[AI助手] ⚠️ 加载自适应失败: %s", e)

    def _保存到磁盘(self, 强制: bool = False):
        now = time.time()
        if (not 强制 and now - self._上次保存时间
                < self._最小保存间隔):
            return
        try:
            目录 = os.path.dirname(self.持久化路径)
            if 目录 and not os.path.exists(目录):
                os.makedirs(目录, exist_ok=True)
            with self._自适应锁:
                快照 = {k: dict(v)
                       for k, v in self._自适应表.items()}
            with open(self.持久化路径, "w",
                      encoding="utf-8") as f:
                json.dump(快照, f, ensure_ascii=False, indent=2)
            self._上次保存时间 = now
        except Exception:
            pass

    # ==================== 结构化请求 ====================

    def _请求JSON(self, 系统提示: str, 用户消息: str, 用途: str = "重",
                  强制模型: str = None,
                  最大尝试: int = 2) -> dict:
        if not self.是否可用():
            return {}

        标准名 = self._归一化模型名(
            强制模型 or 取模型名(self.AI配置))
        if 标准名 not in self.可用模型列表:
            if "deepseek-flash" in self.可用模型列表:
                标准名 = "deepseek-flash"
            else:
                return {}

        当前 = self._获取当前建议(标准名)
        尝试 = 0
        最佳结果 = None
        本次真实输出 = 0

        while 尝试 < 最大尝试:
            尝试 += 1
            当前 = self._对齐2的幂(当前)
            当前 = max(self._token下界,
                      min(self._token上界, 当前))
            结果, 输出tokens, finish = self._调用一次(
                标准名, 系统提示, 用户消息, 当前, 用途=用途)
            if 输出tokens > 本次真实输出:
                本次真实输出 = 输出tokens
            if 结果 is not None:
                最佳结果 = 结果
                self._更新真实最大(标准名, 输出tokens,
                                  成功=True)
                break
            else:
                if finish == "length" and 当前 < self._token上界:
                    当前 = min(self._token上界, 当前 * 2)
                    self._更新真实最大(标准名, 输出tokens,
                                      成功=False)
                else:
                    break

        if 最佳结果:
            self._调整建议值(标准名, 当前, 本次真实输出)
        return 最佳结果 or {}

    def _获取当前建议(self, 模型: str) -> int:
        with self._自适应锁:
            v = self._自适应表.get(模型)
            if v:
                return v["当前建议"]
        return self._token起步

    def _更新真实最大(self, 模型: str, 输出tokens: int,
                      成功: bool):
        if 输出tokens <= 0:
            return
        with self._自适应锁:
            v = self._自适应表.setdefault(模型, {
                "真实最大": 0, "当前建议": self._token起步,
                "成功次数": 0, "失败次数": 0, "更新时间": "",
            })
            if 输出tokens > v["真实最大"]:
                v["真实最大"] = 输出tokens
            if 成功:
                v["成功次数"] += 1
            else:
                v["失败次数"] += 1
            v["更新时间"] = time.strftime("%Y-%m-%d %H:%M:%S")
        self._保存到磁盘()

    def _调整建议值(self, 模型: str, 本次max: int,
                    本次真实输出: int):
        with self._自适应锁:
            v = self._自适应表.setdefault(模型, {
                "真实最大": 0, "当前建议": self._token起步,
                "成功次数": 0, "失败次数": 0, "更新时间": "",
            })
            基础 = max(v["真实最大"], 本次真实输出)
            建议 = int(基础 * self._安全系数)
            建议 = self._对齐2的幂(建议)
            建议 = max(self._token下界,
                      min(self._token上界, 建议))
            v["当前建议"] = 建议
            v["更新时间"] = time.strftime("%Y-%m-%d %H:%M:%S")
        self._保存到磁盘(强制=True)

    def _调用本地(self, 系统提示: str, 用户消息: str,
                max_tokens: int):
        """走本地模型；成功返回与云端同构的 ``(parsed, 输出tokens, finish)``。"""
        try:
            结果 = self.本地模型.对话(
                用户消息, 系统提示,
                最大tokens=min(int(max_tokens or 0) or self.本地模型配置.最大tokens,
                            self.本地模型配置.最大tokens))
        except Exception as e:  # noqa: BLE001
            logger.warning("[AI助手] 本地模型异常：%s", e)
            self.本地状态缓存 = None
            return None
        if not 结果.成功:
            logger.warning("[AI助手] 本地模型失败：%s", 结果.错误)
            self.本地状态缓存 = None
            return None
        self.本地调用次数 += 1
        self.本地状态缓存 = getattr(self.本地模型, "_状态", None)
        待解析 = (结果.内容 or 结果.推理内容 or "").strip()
        parsed = self._解析JSON(待解析)
        if parsed is None:
            logger.info("[AI助手] 本地模型返回不是 JSON（%.1fs）：%s",
                        结果.用时秒, 待解析[:80])
            return None
        logger.info("[AI助手] 🏠 本地模型 %s | 输出 %st | %.1fs | 免费",
                   结果.模型, 结果.输出tokens, 结果.用时秒)
        return parsed, int(结果.输出tokens or 0), "stop"

    def 获取本地模型状态(self, 重新检测: bool = False):
        """返回本地模型状态（界面/自检用）。

        **未启用时不主动探测**（除非 ``重新检测=True``）：否则每次读状态都会去戳
        本机端口，在"禁止联网/纯离线"场景下会让人以为程序偷偷联网。
        """
        if not self.本地模型配置.启用 and not 重新检测:
            from .本地模型 import 本地模型状态, _找可执行文件
            return self.本地状态缓存 or 本地模型状态(
                可用=False, 说明="本地模型未启用（可在 AI 页开启，免费离线）",
                已安装运行时=bool(_找可执行文件()),
                可执行文件=_找可执行文件())
        if 重新检测 or self.本地状态缓存 is None:
            self.本地状态缓存 = self.本地模型.检测()
        return self.本地状态缓存

    def 本地模型测速(self) -> dict:
        return self.本地模型.测速()

    def 保存本地模型配置(self, **变更) -> None:
        """改本地模型配置并落盘（``配置.json`` 的 ``AI.本地模型`` 段）。"""
        段 = dict(self.AI配置.get("本地模型") or {})
        段.update({k: v for k, v in 变更.items() if v is not None})
        self.AI配置["本地模型"] = 段
        self.本地模型配置 = 取本地模型配置(self.AI配置)
        self.本地模型.配置 = self.本地模型配置
        self.本地状态缓存 = None
        try:
            # 必须"读-改-写"整份配置：保存配置() 把传进去的 dict 当成**整份配置**
            # 写盘，只传 {"AI": ...} 会把适配器列表等全抹掉。
            from ..配置 import 加载配置, 保存配置, 配置文件 as 默认配置路径
            路径 = getattr(self, "配置路径", None) or 默认配置路径
            整份 = 加载配置(路径)
            ai段 = dict(整份.get("AI") or {})
            ai段["本地模型"] = 段
            整份["AI"] = ai段
            保存配置(整份, 路径)
        except Exception as e:  # noqa: BLE001
            logger.warning("[AI助手] 保存本地模型配置失败：%s", e)

    def _调用一次(self, 模型: str, 系统提示: str,
                  用户消息: str, max_tokens: int, 用途: str = "重"):
        # ``用途=仅轻量`` 时，重决策（批次策略）不走本地，交给云端
        # （纯 CPU 上一次本地策略调用约 15 秒，太拖批次启动）
        本地允许 = self.本地模型配置.启用 and (
            self.本地模型配置.用途 != "仅轻量" or 用途 == "轻")
        # ---- 本地模型优先（免费、离线）----
        if 本地允许 and self.本地模型配置.优先本地:
            结果 = self._调用本地(系统提示, 用户消息, max_tokens)
            if 结果 is not None:
                return 结果
            if not self.本地模型配置.云端兜底:
                self._token统计["总失败"] += 1
                return None, 0, "error"
        if self.禁用网络 or not self.api密钥:
            # 没密钥时，若本地模型可用（哪怕"云端兜底"关着）也再试一次本地
            if 本地允许:
                结果 = self._调用本地(系统提示, 用户消息, max_tokens)
                if 结果 is not None:
                    return 结果
            return None, 0, "error"
        self._token统计["总尝试"] += 1
        API模型ID = self.获取原始模型ID(模型)
        try:
            响应 = httpx.post(
                self._地址("chat/completions", self.对话接口地址),
                headers={
                    "Authorization": f"Bearer {self.api密钥}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": API模型ID,
                    "messages": [
                        {"role": "system", "content": 系统提示},
                        {"role": "user", "content": 用户消息},
                    ],
                    "temperature": 0.2,
                    "max_tokens": max_tokens,
                    "stream": False,
                },
                timeout=30)
            响应.raise_for_status()
            数据 = 响应.json()
        except httpx.HTTPError as e:
            logger.warning("[AI助手] 请求异常(HTTP): %s", e)
            self._token统计["总失败"] += 1
            return None, 0, "error"
        except Exception as e:
            logger.warning("[AI助手] 请求异常: %s", e)
            self._token统计["总失败"] += 1
            return None, 0, "error"

        用量 = 数据.get("usage", {})
        缓存 = 用量.get("prompt_tokens_details", {}).get(
            "cached_tokens", 0)
        输出tokens = 用量.get("completion_tokens", 0)
        本次费用 = self._计算费用(
            模型, 用量.get("prompt_tokens", 0),
            输出tokens, 缓存)
        self.累计消耗 += 本次费用
        self._token统计["总费用"] += 本次费用

        消息 = 数据["choices"][0]["message"]
        finish = 数据["choices"][0].get("finish_reason", "?")
        内容 = (消息.get("content") or "").strip()
        推理内容 = (消息.get("reasoning_content") or "").strip()

        logger.info("[AI助手] 💰 max_tokens=%s | 输出 %st | 费用 ¥%.6f",
                    max_tokens, 输出tokens, 本次费用)

        待解析 = 内容 or 推理内容
        if not 待解析:
            self._token统计["总失败"] += 1
            return None, 输出tokens, finish

        parsed = self._解析JSON(待解析)
        if parsed is None:
            self._token统计["总失败"] += 1
            return None, 输出tokens, finish
        self._token统计["总成功"] += 1
        return parsed, 输出tokens, finish

    @staticmethod
    def _解析JSON(文本: str):
        r"""从模型输出里抠 JSON。

        以前只做 ``json.loads`` + 一次 ``\{.*\}`` 正则，**代码围栏和截断都救不回来**：
        实测本地模型把诊断 JSON 停在 ``"起播等待秒":``（撞 token 上限），于是这一轮
        40~50 秒的本地推理被整段丢掉。现在统一交给 ``JSON修复.提取JSON对象``。
        """
        if not 文本:
            return None
        try:
            from .JSON修复 import 提取JSON对象
            return 提取JSON对象(文本)
        except Exception:  # noqa: BLE001 - 兜底：老逻辑永远可用
            try:
                return json.loads(文本)
            except Exception:
                m = re.search(r'\{.*\}', 文本, re.DOTALL)
                if m:
                    try:
                        return json.loads(m.group(0))
                    except Exception:
                        pass
                return None

    @staticmethod
    def _对齐2的幂(值: int) -> int:
        for p in (256, 512, 1024, 2048, 4096, 8192):
            if 值 <= p:
                return p
        return 8192

    def _计算费用(self, 模型: str, 输入tokens: int,
                  输出tokens: int, 缓存命中: int) -> float:
        价格 = self._选价格(模型)
        return ((缓存命中 / 1000000)
                * 价格.get("输入_缓存命中", 0.02)
                + ((输入tokens - 缓存命中) / 1000000)
                * 价格.get("输入_缓存未命中", 1.0)
                + (输出tokens / 1000000)
                * 价格.get("输出", 4.0))

    # ==================== 深度参与传输的接口 ====================

    def 请求结构化策略(self, 系统提示: str, 用户消息: str,
                      强制模型: str = None) -> dict:
        return self._请求JSON(系统提示, 用户消息, 强制模型)

    def 请求文件优先级(self, 文件列表: list,
                      最大样本: int = 30) -> dict:
        if not 文件列表:
            return {"优先级映射": {}, "理由": "空列表"}
        if not self.是否可用():
            return self._规则优先级(文件列表)

        采样 = 文件列表[:最大样本]
        精简 = []
        for f in 采样:
            名 = os.path.basename(f.get("路径", ""))
            精简.append({
                "文件名": 名[:60],
                "大小MB": round(f.get("大小", 0) / 1024 / 1024, 2),
            })

        系统提示 = (
            "你是文件传输调度专家。分析文件名和大小，为每个文件打分。\n"
            "【打分规则】\n"
            "  +50 ~ +100: 小文件、ASCII 文件名\n"
            "  -100 ~ -50: 大文件、含敏感词\n"
            "【输出格式 - 严格 JSON】\n"
            '{"优先级映射": {"文件名": 分数, ...}, "理由": "..."}\n'
            "只输出 JSON。"
        )
        用户消息 = json.dumps(精简, ensure_ascii=False)
        结果 = self._请求JSON(系统提示, 用户消息, 最大尝试=1,
                              用途="轻")
        if not 结果 or not isinstance(结果.get("优先级映射"), dict):
            return self._规则优先级(文件列表)

        名到路径 = {}
        for f in 文件列表:
            名 = os.path.basename(f.get("路径", ""))
            名到路径.setdefault(名, []).append(f.get("路径", ""))

        优先级映射 = {}
        for 名, 分数 in 结果["优先级映射"].items():
            for 路径 in 名到路径.get(名[:60], []):
                try:
                    优先级映射[路径] = max(-100, min(100, int(分数)))
                except Exception:
                    pass

        for f in 文件列表:
            路径 = f.get("路径", "")
            if 路径 not in 优先级映射:
                优先级映射[路径] = self._单文件规则分(f)

        return {
            "优先级映射": 优先级映射,
            "理由": 结果.get("理由", "AI 决策"),
        }

    def _规则优先级(self, 文件列表: list) -> dict:
        映射 = {}
        for f in 文件列表:
            映射[f.get("路径", "")] = self._单文件规则分(f)
        return {"优先级映射": 映射, "理由": "规则回退"}

    @staticmethod
    def _单文件规则分(f: dict) -> int:
        分 = 0
        大小MB = f.get("大小", 0) / 1024 / 1024
        if 大小MB < 1:
            分 += 30
        elif 大小MB < 10:
            分 += 15
        elif 大小MB > 100:
            分 -= 30
        elif 大小MB > 1000:
            分 -= 60
        名 = os.path.basename(f.get("路径", ""))
        含中文 = any('\u4e00' <= c <= '\u9fff' for c in 名)
        if 含中文:
            分 -= 10
        if 名.isascii():
            分 += 10
        return max(-100, min(100, 分))

    def 请求失败诊断(self, 错误信息: str, 文件名: str,
                    已重试次数: int) -> dict:
        规则结果 = self._规则诊断(错误信息, 已重试次数)
        if 规则结果:
            return 规则结果
        if not self.是否可用():
            return {"动作": "重试", "退避秒": 2.0,
                    "理由": "AI未启用：默认重试"}

        系统提示 = (
            "你是网络传输故障诊断专家。根据错误信息决定动作。\n"
            '【输出】{"动作":"重试|降并发|改名|跳过",'
            '"退避秒":数字,"理由":"..."}\n'
            "只输出 JSON。"
        )
        用户消息 = json.dumps({
            "错误": 错误信息[:200],
            "文件名": 文件名[:60],
            "已重试次数": 已重试次数,
        }, ensure_ascii=False)
        结果 = self._请求JSON(系统提示, 用户消息, 最大尝试=1,
                              用途="轻")
        if (not 结果 or 结果.get("动作") not in (
                "重试", "降并发", "改名", "跳过")):
            return {"动作": "重试", "退避秒": 2.0,
                    "理由": "AI解析失败"}
        try:
            退避 = max(0.0, min(30.0,
                              float(结果.get("退避秒", 2.0))))
        except Exception:
            退避 = 2.0
        return {
            "动作": 结果["动作"], "退避秒": 退避,
            "理由": 结果.get("理由", "AI 决策"),
        }

    @staticmethod
    def _规则诊断(错误信息: str, 已重试次数: int):
        if not 错误信息:
            return None
        错 = 错误信息.lower()
        if "405" in 错:
            return {"动作": "改名", "退避秒": 0.0,
                    "理由": "规则：405 敏感词"}
        for code in ("500", "502", "503", "504"):
            if code in 错:
                退避 = min(30.0, 2.0 ** 已重试次数)
                return {"动作": "重试", "退避秒": 退避,
                        "理由": f"规则：{code}"}
        if "timeout" in 错 or "超时" in 错:
            return {"动作": "重试", "退避秒": 3.0,
                    "理由": "规则：超时"}
        if "404" in 错:
            return {"动作": "跳过", "退避秒": 0.0,
                    "理由": "规则：404"}
        if "403" in 错 or "unauthorized" in 错:
            return {"动作": "跳过", "退避秒": 0.0,
                    "理由": "规则：权限拒绝"}
        if "connection refused" in 错:
            return {"动作": "降并发", "退避秒": 5.0,
                    "理由": "规则：连接被拒"}
        return None

    def 请求运行时调优(self, 当前并发: int, 吞吐_MBps: float,
                      失败率: float, 队列长度: int,
                      已完成: int, 总数: int) -> dict:
        规则结果 = self._规则调优(
            当前并发, 失败率, 队列长度)
        if 规则结果:
            return 规则结果
        if not self.是否可用():
            return {"并发数": 当前并发, "动作": "继续",
                    "理由": "AI未启用"}

        系统提示 = (
            "你是传输运行时调优专家。\n"
            '【输出】{"并发数":整数(8~64),'
            '"动作":"继续|暂停","理由":"..."}\n'
            "只输出 JSON。"
        )
        用户消息 = json.dumps({
            "当前并发": 当前并发,
            "吞吐_MBps": round(吞吐_MBps, 2),
            "失败率": round(失败率, 3),
            "队列长度": 队列长度,
            "已完成": 已完成, "总数": 总数,
        }, ensure_ascii=False)
        结果 = self._请求JSON(系统提示, 用户消息, 最大尝试=1,
                              用途="轻")
        if not 结果:
            return {"并发数": 当前并发, "动作": "继续",
                    "理由": "AI解析失败"}
        try:
            并发 = max(4, min(64, int(结果.get("并发数",
                                             当前并发))))
        except Exception:
            并发 = 当前并发
        return {
            "并发数": 并发,
            "动作": 结果.get("动作", "继续"),
            "理由": 结果.get("理由", "AI 调优"),
        }

    @staticmethod
    def _规则调优(当前并发: int, 失败率: float,
                  队列长度: int):
        if 失败率 > 0.5:
            return {"并发数": max(4, 当前并发 // 2),
                    "动作": "继续",
                    "理由": f"规则：失败率 {失败率:.0%} 降并发"}
        if 失败率 > 0.2:
            return {"并发数": max(4, int(当前并发 * 0.75)),
                    "动作": "继续",
                    "理由": f"规则：失败率 {失败率:.0%} 微降"}
        if 队列长度 > 100 and 失败率 < 0.1:
            return {"并发数": 当前并发, "动作": "继续",
                    "理由": "规则：队列充足"}
        return None
