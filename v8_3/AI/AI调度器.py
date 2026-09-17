# v8_3/AI/AI调度器.py
"""AI 调度器 - 分层决策 + 缓存 + 频率控制 + 学习闭环。

移植自 V8 ``AI层/AI调度器.py``，改动：

* 队列特征分析的引用改成包内相对导入（不再依赖 V8 的 ``AI层`` 顶层包名）；
* 构造函数支持 V8_3 写法：``AI调度器(配置)`` / ``AI调度器(配置=整份配置)``，
  缺的部件（助手/时段/预算/学习库/相似度函数）会自动补齐；
* ``_AI可用()`` 在 V8 的“预算未熔断 + 时段允许”之外，**额外要求助手真的可用**
  （有 api密钥、AI 未关闭、没禁用网络）；这样无密钥时会直接返回规则基线，
  不会白发一次注定失败的请求；
* 学习库默认懒创建：只有 AI 真的启用（``启用`` 且 ``api密钥`` 非空）时才建，
  ``AI学习库路径(配置)`` → ``数据/AI学习库.db``；AI 关闭/无密钥时**不建库、不落盘**；
* 日志走 ``logging``。

构造函数签名
============
``AI调度器(AI助手=None, 时段管理器=None, 预算管理器=None, 学习库=None,
          相似度函数=None, *, 配置=None, 初始化联网=None,
          禁用网络=False, 自动建学习库=None)``

* ``AI调度器(配置)``：一个 dict（``AI配置()`` 或整份配置）自动装配全部部件；
* ``AI调度器(AI助手)`` / V8 老写法（关键字同名）都能用；
* ``学习库=None`` 且 ``自动建学习库=None``：按“AI 是否真的启用”自动决定；
  想强制关掉就传 ``自动建学习库=False`` 或 ``学习库=False``；
* ``配置`` 里的 ``调度`` 段：``每分钟最多调用``（60 秒滚动窗口）、
  ``缓存条数``（指纹缓存上限），以及 ``自适应并发`` / ``文件优先级`` /
  ``失败诊断`` 三个开关；``每小时上限`` / ``每天上限`` 也可以直接写在
  ``调度`` 段里，覆盖 V8 默认的 4 次/小时、50 次/天。

公开方法
========
``请求策略(特征, 决策来源标记=None)`` → dict
``请求文件优先级(文件列表)`` → dict（``{"路径": 分值}``）
``请求失败诊断(错误信息, 文件名, 已重试次数)`` → dict
``请求运行时调优(当前并发, 吞吐_MBps, 失败率, 队列长度, 已完成, 总数)`` → dict
``回填效果(决策来源标记, 吞吐_MBps, 失败率, 总耗时_秒, 对比基准吞吐=None)``
``获取统计()`` → dict
``清空缓存()``
"""
import time
import json
import logging
import threading
from collections import OrderedDict
from typing import Optional, Dict

from ..配置 import AI学习库路径
from .配置适配 import 取AI配置
from .队列特征分析 import 队列特征分析器
from .AI学习库 import AI学习库
from .AI智能助手 import AI智能助手
from .时段预算 import (
    时段管理器 as 时段管理器类, 预算管理器 as 预算管理器类,
)

logger = logging.getLogger(__name__)


class AI调度器:
    """分层 AI 调度器"""

    最小批次 = 100
    每小时上限 = 4
    每天上限 = 50

    指纹缓存TTL = 1800
    指纹缓存上限 = 200
    相似度阈值 = 0.85

    优先级缓存上限 = 500
    优先级缓存TTL = 600

    def __init__(self, AI助手=None, 时段管理器=None, 预算管理器=None,
                 学习库=None, 相似度函数=None, *, 配置=None,
                 初始化联网=None, 禁用网络: bool = False,
                 自动建学习库=None):
        # ---- 兼容 V8_3 写法：第一个位置参数可能就是配置 dict ----
        if isinstance(AI助手, dict) and 配置 is None:
            配置, AI助手 = AI助手, None
        if isinstance(时段管理器, dict) and 配置 is None:
            配置, 时段管理器 = 时段管理器, None
        if isinstance(预算管理器, dict) and 配置 is None:
            配置, 预算管理器 = 预算管理器, None

        self._原始配置 = 配置 if isinstance(配置, dict) else None
        if 配置 is not None:
            self._AI段 = 取AI配置(配置)
        elif AI助手 is not None and hasattr(AI助手, "AI配置"):
            self._AI段 = 取AI配置(AI助手.AI配置)
            self._原始配置 = getattr(AI助手, "_原始配置", None)
        else:
            self._AI段 = 取AI配置()

        if 时段管理器 is None:
            时段管理器 = 时段管理器类(
                配置=self._原始配置 or self._AI段,
                打印启动状态=False)
        if 预算管理器 is None:
            预算管理器 = 预算管理器类(
                配置=self._原始配置 or self._AI段)
        if AI助手 is None:
            AI助手 = AI智能助手(
                配置=self._原始配置 or self._AI段,
                时段管理器=时段管理器, 预算管理器=预算管理器,
                初始化联网=初始化联网, 禁用网络=禁用网络)

        self.AI助手 = AI助手
        self.时段管理器 = 时段管理器
        self.预算管理器 = 预算管理器
        self.相似度函数 = 相似度函数 or 队列特征分析器.相似度

        # 调度参数（AI配置()['调度'] 可覆盖）
        调度 = self._AI段.get("调度") or {}
        try:
            self.每分钟最多调用 = max(1, int(
                调度.get("每分钟最多调用") or 6))
        except Exception:
            self.每分钟最多调用 = 6
        try:
            self.每小时上限 = max(1, int(
                调度.get("每小时上限") or self.每小时上限))
        except Exception:
            pass
        try:
            self.每天上限 = max(1, int(
                调度.get("每天上限") or self.每天上限))
        except Exception:
            pass
        try:
            self.指纹缓存上限 = max(10, int(
                调度.get("缓存条数") or self.指纹缓存上限))
        except Exception:
            pass
        self.自适应并发 = bool(调度.get("自适应并发", True))
        self.文件优先级 = bool(调度.get("文件优先级", True))
        self.失败诊断 = bool(调度.get("失败诊断", True))

        self._学习库 = 学习库
        self._学习库锁 = threading.RLock()
        if 自动建学习库 is None:
            self._自动建库 = bool(self._AI段.get("启用")
                              and self._AI段.get("api密钥"))
        else:
            self._自动建库 = bool(自动建学习库)

        self._指纹缓存 = OrderedDict()
        self._缓存锁 = threading.RLock()

        self._优先级缓存 = OrderedDict()
        self._优先级锁 = threading.RLock()

        self._频率记录 = []
        self._频率锁 = threading.Lock()

        self._统计 = {
            "今日调用": 0, "今日日期": "",
            "缓存命中": 0, "相似复用": 0,
            "规则降级": 0, "AI调用": 0, "AI失败": 0,
            "优先级AI": 0, "诊断AI": 0, "调优AI": 0,
        }
        self._统计锁 = threading.Lock()

        self._活跃批次: Dict[str, dict] = {}
        self._活跃锁 = threading.Lock()

    # ==================== 学习库（懒创建） ====================

    @property
    def 学习库(self):
        if self._学习库 is None and self._自动建库:
            with self._学习库锁:
                if self._学习库 is None:
                    self._学习库 = self._创建学习库()
        return self._学习库

    @学习库.setter
    def 学习库(self, 值):
        self._学习库 = 值

    def _创建学习库(self):
        try:
            路径 = (AI学习库路径(self._原始配置)
                  if isinstance(self._原始配置, dict)
                  else AI学习库路径())
            return AI学习库(路径)
        except Exception as e:
            logger.warning("[AI调度器] 学习库不可用: %s", e)
            return None

    # ==================== 批次策略 ====================

    def 请求策略(self, 特征: dict,
                 决策来源标记: str = None) -> dict:
        if not self._AI可用():
            self._增加统计("规则降级")
            return self._默认策略("AI未启用")
        if 特征.get("总任务数", 0) < self.最小批次:
            self._增加统计("规则降级")
            return self._默认策略("批次过小")
        if not self._频率允许():
            self._增加统计("规则降级")
            return self._默认策略("频率超限")

        指纹 = self._计算指纹(特征)
        缓存策略 = self._查缓存(指纹)
        if 缓存策略 is not None:
            self._增加统计("缓存命中")
            return 缓存策略

        相似 = self._查相似(特征)
        if 相似 is not None:
            self._增加统计("相似复用")
            策略 = dict(相似["记录"]["策略"])
            策略["调度原因"] = (
                f"复用相似场景（{相似['相似度']:.2f}）")
            self._写缓存(指纹, 策略)
            if self.学习库:
                try:
                    self.学习库.记录策略(
                        指纹, 特征, 策略, 决策来源="相似复用")
                    if 决策来源标记:
                        with self._活跃锁:
                            self._活跃批次[决策来源标记] = {
                                "指纹": 指纹,
                                "开始时间": time.time(),
                            }
                except Exception:
                    pass
            return 策略

        try:
            策略 = self._调用AI(特征)
            self._增加统计("AI调用")
            self._记频率()
            self._写缓存(指纹, 策略)
            if self.学习库:
                try:
                    # 来源标记：本地模型 / 云端 —— 界面的「决策来源」和
                    # 学习库都能看出这次是谁给的策略
                    self.学习库.记录策略(
                        指纹, 特征, 策略,
                        决策来源=("本地模型" if self._本地模型可用() else "ai"))
                    if 决策来源标记:
                        with self._活跃锁:
                            self._活跃批次[决策来源标记] = {
                                "指纹": 指纹,
                                "开始时间": time.time(),
                            }
                except Exception:
                    pass
            return 策略
        except Exception as e:
            logger.warning("[AI调度器] AI 失败: %s", e)
            self._增加统计("AI失败")
            return self._默认策略(f"AI失败: {e}")

    # ==================== 文件优先级 ====================

    def 请求文件优先级(self, 文件列表: list) -> dict:
        if not 文件列表:
            return {}
        现在 = time.time()
        结果 = {}
        未缓存 = []

        with self._优先级锁:
            for f in 文件列表:
                路径 = f.get("路径", "")
                缓存 = self._优先级缓存.get(路径)
                if 缓存 and (现在 - 缓存[1]) < self.优先级缓存TTL:
                    结果[路径] = 缓存[0]
                else:
                    未缓存.append(f)

        if not 未缓存:
            return 结果

        if not self.文件优先级 or not self._AI可用():
            规则结果 = self._规则优先级(未缓存)
            结果.update(规则结果["优先级映射"])
            self._写优先级缓存(结果)
            return 结果

        try:
            AI结果 = self.AI助手.请求文件优先级(未缓存)
            映射 = AI结果.get("优先级映射", {})
            if 映射:
                self._增加统计("优先级AI")
                结果.update(映射)
                self._写优先级缓存(映射)
                return 结果
        except Exception as e:
            logger.warning("[AI调度器] 优先级失败: %s", e)

        规则结果 = self._规则优先级(未缓存)
        结果.update(规则结果["优先级映射"])
        self._写优先级缓存(规则结果["优先级映射"])
        return 结果

    def _规则优先级(self, 文件列表: list) -> dict:
        """助手缺失时的兜底（正常路径直接调助手的 ``_规则优先级``）。"""
        if self.AI助手 is not None:
            try:
                return self.AI助手._规则优先级(文件列表)
            except Exception:
                pass
        return {
            "优先级映射": {
                f.get("路径", ""): AI智能助手._单文件规则分(f)
                for f in 文件列表},
            "理由": "规则回退",
        }

    def _写优先级缓存(self, 映射: dict):
        now = time.time()
        with self._优先级锁:
            for 路径, 分 in 映射.items():
                self._优先级缓存[路径] = (分, now)
                self._优先级缓存.move_to_end(路径)
            while len(self._优先级缓存) > self.优先级缓存上限:
                self._优先级缓存.popitem(last=False)

    # ==================== 失败诊断 ====================

    def 请求失败诊断(self, 错误信息: str, 文件名: str,
                    已重试次数: int) -> dict:
        if not self.失败诊断:
            return {"动作": "重试", "退避秒": 2.0,
                    "理由": "规则回退（诊断已关闭）"}
        try:
            结果 = self.AI助手.请求失败诊断(
                错误信息, 文件名, 已重试次数)
            if 结果.get("理由", "").startswith("AI"):
                self._增加统计("诊断AI")
            return 结果
        except Exception as e:
            logger.warning("[AI调度器] 诊断失败: %s", e)
            return {"动作": "重试", "退避秒": 2.0,
                    "理由": "调度器回退"}

    # ==================== 运行时调优 ====================

    def 请求运行时调优(self, 当前并发: int, 吞吐_MBps: float,
                      失败率: float, 队列长度: int,
                      已完成: int, 总数: int) -> dict:
        if not self.自适应并发:
            return {"并发数": 当前并发, "动作": "继续",
                    "理由": "规则回退（自适应并发已关闭）"}
        try:
            结果 = self.AI助手.请求运行时调优(
                当前并发, 吞吐_MBps, 失败率,
                队列长度, 已完成, 总数)
            if 结果.get("理由", "").startswith("AI"):
                self._增加统计("调优AI")
            return 结果
        except Exception as e:
            logger.warning("[AI调度器] 调优失败: %s", e)
            return {"并发数": 当前并发, "动作": "继续",
                    "理由": "调度器回退"}

    # ==================== 回填 ====================

    def 回填效果(self, 决策来源标记: str, 吞吐_MBps: float,
                失败率: float, 总耗时_秒: float,
                对比基准吞吐: float = None):
        if not self.学习库:
            return
        with self._活跃锁:
            批次 = self._活跃批次.pop(决策来源标记, None)
        if not 批次:
            return
        指纹 = 批次["指纹"]
        收益 = 0
        if 对比基准吞吐 and 对比基准吞吐 > 0:
            比值 = 吞吐_MBps / 对比基准吞吐
            if 比值 >= 1.1:
                收益 = 1
            elif 比值 <= 0.9:
                收益 = -1
        try:
            self.学习库.更新效果(
                指纹, 吞吐_MBps=吞吐_MBps,
                失败率=失败率, 总耗时_秒=总耗时_秒,
                收益评分=收益)
        except Exception as e:
            logger.warning("[AI调度器] 回填失败: %s", e)

    def 获取统计(self) -> dict:
        with self._统计锁:
            s = dict(self._统计)
        with self._缓存锁:
            s["缓存条数"] = len(self._指纹缓存)
        with self._优先级锁:
            s["优先级缓存"] = len(self._优先级缓存)
        with self._频率锁:
            s["近期调用"] = len(self._频率记录)
        s["AI可用"] = self._AI可用()
        s["学习库"] = bool(self.学习库)
        if self.学习库:
            try:
                s.update(self.学习库.获取统计())
            except Exception:
                pass
        return s

    def 清空缓存(self):
        with self._缓存锁:
            self._指纹缓存.clear()
        with self._优先级锁:
            self._优先级缓存.clear()

    # ==================== 内部 ====================

    def _本地模型可用(self) -> bool:
        """本地 DeepSeek 模型是否可用（免费、离线）。"""
        try:
            助手 = self.AI助手
            if 助手 is None:
                return False
            判断 = getattr(助手, "本地模型可用", None)
            return bool(判断()) if callable(判断) else False
        except Exception:
            return False

    def _AI可用(self) -> bool:
        # V8_3：本地模型可用时**不受云端预算/高峰时段限制**——它免费且离线，
        # 没有理由跟着云端的"贵时段不调用"和余额熔断一起被卡住。
        if self._本地模型可用():
            return True
        try:
            if self.预算管理器 is not None and \
                    self.预算管理器.是否已熔断:
                return False
        except Exception:
            return False
        try:
            if self.时段管理器 is not None and \
                    not self.时段管理器.是否启用AI():
                return False
        except Exception:
            return False
        if self.AI助手 is None:
            return False
        # V8_3 追加：没有密钥 / 禁用网络时直接降级，不发注定失败的请求
        try:
            是否可用 = getattr(self.AI助手, "是否可用", None)
            if callable(是否可用) and not 是否可用():
                return False
        except Exception:
            return False
        return True

    def _频率允许(self) -> bool:
        now = time.time()
        with self._频率锁:
            self._频率记录 = [t for t in self._频率记录
                             if now - t < 3600]
            if len(self._频率记录) >= self.每小时上限:
                return False
            # V8_3 追加：配置里的“每分钟最多调用”按 60 秒滚动窗口卡
            if sum(1 for t in self._频率记录
                   if now - t < 60) >= self.每分钟最多调用:
                return False
        今天 = time.strftime("%Y%m%d")
        with self._统计锁:
            if self._统计["今日日期"] != 今天:
                self._统计["今日日期"] = 今天
                self._统计["今日调用"] = 0
            if self._统计["今日调用"] >= self.每天上限:
                return False
        return True

    def _记频率(self):
        now = time.time()
        with self._频率锁:
            self._频率记录.append(now)
        with self._统计锁:
            self._统计["今日调用"] += 1

    def _默认策略(self, 原因: str = "") -> dict:
        return {
            "并发数": 16,
            "优先级规则": ["大文件优先", "敏感词提前改名"],
            "分批策略": "顺序处理",
            "预检行为": "含中文名批量预检",
            "失败重试策略": {
                "5xx": "指数退避 3 次",
                "405": "立刻走三级绕过",
                "超时": "2 次后降并发",
            },
            "预期效果": "使用规则基线",
            "置信度": 0.5,
            "调度原因": f"规则基线（{原因}）" if 原因 else "规则基线",
            "来源": "规则",
        }

    def _调用AI(self, 特征: dict) -> dict:
        系统提示 = self._构造系统提示()
        用户消息 = self._构造用户消息(特征)
        结果 = self.AI助手.请求结构化策略(
            系统提示, 用户消息, 强制模型="deepseek-flash")
        if not 结果:
            raise RuntimeError("AI 未返回有效策略")
        策略 = self._校验策略(结果)
        策略["来源"] = "AI"
        return 策略

    @staticmethod
    def _构造系统提示() -> str:
        return (
            "你是光鸭云盘传输调度专家。分析队列特征，输出最优传输策略。\n"
            "必须直接输出合法 JSON，以 { 开头，以 } 结尾。\n"
            "字段：\n"
            '{"并发数":整数(8~48),"优先级规则":[字符串],'
            '"分批策略":字符串,"预检行为":字符串,'
            '"失败重试策略":{"5xx":字符串,"405":字符串,'
            '"超时":字符串},'
            '"预期效果":字符串,"置信度":浮点数}\n'
            "规则：大文件多→降并发；小文件多→升并发；"
            "中文名多→强化预检；类型集中→顺序处理。"
        )

    @staticmethod
    def _构造用户消息(特征: dict) -> str:
        精简 = {
            "总任务数": 特征.get("总任务数", 0),
            "总MB": 特征.get("总MB", 0),
            "平均文件大小_MB": 特征.get("平均文件大小_MB", 0),
            "最大文件_MB": 特征.get("最大文件_MB", 0),
            "大小分布": 特征.get("大小分布", {}),
            "类型分布": 特征.get("类型分布", {}),
            "含中文文件名数": 特征.get("含中文文件名数", 0),
        }
        return json.dumps(精简, ensure_ascii=False)

    def _校验策略(self, 策略: dict) -> dict:
        默认 = self._默认策略()
        结果 = dict(默认)
        if not isinstance(策略, dict):
            return 结果
        try:
            并发 = int(策略.get("并发数", 16))
            结果["并发数"] = max(4, min(64, 并发))
        except Exception:
            pass
        规则 = 策略.get("优先级规则")
        if isinstance(规则, list) and 规则:
            结果["优先级规则"] = [str(x) for x in 规则[:3]]
        for k in ("分批策略", "预检行为", "预期效果"):
            v = 策略.get(k)
            if isinstance(v, str) and v.strip():
                结果[k] = v.strip()
        try:
            结果["置信度"] = max(0.0, min(
                1.0, float(策略.get("置信度", 0.5))))
        except Exception:
            pass
        结果["调度原因"] = "AI 决策"
        return 结果

    @staticmethod
    def _计算指纹(特征: dict) -> str:
        return 队列特征分析器.计算指纹(特征)

    def _查缓存(self, 指纹: str) -> Optional[dict]:
        now = time.time()
        with self._缓存锁:
            if 指纹 in self._指纹缓存:
                策略, 时间戳 = self._指纹缓存[指纹]
                if now - 时间戳 < self.指纹缓存TTL:
                    self._指纹缓存.move_to_end(指纹)
                    return dict(策略)
                else:
                    del self._指纹缓存[指纹]
        return None

    def _写缓存(self, 指纹: str, 策略: dict):
        with self._缓存锁:
            self._指纹缓存[指纹] = (dict(策略), time.time())
            self._指纹缓存.move_to_end(指纹)
            while len(self._指纹缓存) > self.指纹缓存上限:
                self._指纹缓存.popitem(last=False)

    def _查相似(self, 特征: dict) -> Optional[dict]:
        if not self.学习库:
            return None
        try:
            return self.学习库.查询相似高收益策略(
                特征, self.相似度函数, self.相似度阈值)
        except Exception:
            return None

    def _增加统计(self, key: str, n: int = 1):
        with self._统计锁:
            self._统计[key] = self._统计.get(key, 0) + n
