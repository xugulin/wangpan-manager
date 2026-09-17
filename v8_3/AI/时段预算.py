# v8_3/AI/时段预算.py
"""时段管理器 + 预算管理器（移植自 V8 ``业务层/配置管理.py``，只取这两个类）。

V8 的这两个类依赖 ``配置加载器``；V8_3 改为直接吃普通 ``dict``：

* 时段来自 ``AI配置()['高峰时段']`` → ``{"启用","开始","结束","时区"}``；
  同时兼容 V8 的 ``{"工作日","时段":[{"start","end"}],"时区"}``（也就是
  ``价格抓取器.获取高峰时段()`` 的返回值），以及 ``{"类型": "高峰"}`` 直给写法；
* 预算来自 ``AI配置()['预算']`` → ``{"初始充值金额","低余额阈值","当前余额"}``。

时段语义（与 V8 完全一致）
==========================
* 处于配置的时段内 → ``"高峰"``；否则 → ``"空闲"``（即“低谷”，
  ``低谷`` 只是 ``空闲`` 的别名；AI 只在 ``空闲``/低谷 时段工作）；
* ``是否启用AI()``：手动覆盖优先；其次 ``AI配置()['启用']`` 为假 → 关闭；
  最后 ``获取当前时段类型() == "空闲"`` 才启用。

构造函数签名
============
``时段管理器(价格抓取器=None, 配置=None, 打印启动状态=True)``

* ``价格抓取器``：任何带 ``获取高峰时段()`` 的对象（V8 兼容，可为 None）；
* ``配置``：``AI配置()`` 的 dict / 整份配置 dict / V8 风格配置对象；
  为空时用 ``AI配置()``（读 V8_3 自己的 ``配置.json``）；
* 也可以直接 ``时段管理器(AI配置())``：第一个位置参数是 dict 时按 ``配置`` 处理。

``预算管理器(初始充值金额=None, 低余额阈值=None, 配置=None, 当前余额=None,
            自动同步=False, 保存回调=None)``

* ``初始充值金额`` / ``低余额阈值``：V8 兼容的位置参数，为空时取
  ``AI配置()['预算']``；
* 余额优先取 ``配置['预算']['当前余额']``（不为 None 时），否则用初始充值金额；
* ``自动同步=True`` 时，余额变化会写回**原始配置 dict**；
  ``保存回调(配置)`` 不为空时还会顺带落盘（默认都不做，避免偷偷写文件）。

公开 API（与 V8 一致）
======================
时段管理器：``获取当前时段类型()``、``是否启用AI()``、``设置手动覆盖(状态)``
预算管理器：``当前余额``、``是否已熔断``、``更新余额(新余额)``、
``标记查询失败()``、``获取预算摘要()``、``解除熔断()``
"""
import logging
from datetime import datetime
from typing import Optional

from ..配置 import 设置AI配置
from .配置适配 import 取AI配置

logger = logging.getLogger(__name__)

try:                                       # pragma: no cover - 平台相关
    from zoneinfo import ZoneInfo
    _ZONEINFO_AVAILABLE = True
except ImportError:                        # pragma: no cover
    _ZONEINFO_AVAILABLE = False


#: 常见时区名 → IANA 名（V8_3 配置默认写的是“北京时间”）
_时区别名 = {
    "北京时间": "Asia/Shanghai",
    "中国标准时间": "Asia/Shanghai",
    "上海": "Asia/Shanghai",
    "asia/shanghai": "Asia/Shanghai",
    "utc": "UTC",
    "gmt": "UTC",
}


def _解析时区(时区名):
    """把配置里的时区文本转成 tzinfo；无法识别时返回 None（用本地时间）。"""
    if not 时区名 or not _ZONEINFO_AVAILABLE:
        return None
    文本 = str(时区名).strip()
    目标 = (_时区别名.get(文本)
           or _时区别名.get(文本.lower())
           or 文本)
    try:
        return ZoneInfo(目标)
    except Exception:
        return None


class 时段管理器:
    """时段管理器 - 高峰/空闲判断（公开 API 与 V8 一致）。"""

    兜底时段 = {
        "工作日": [0, 1, 2, 3, 4],
        "时段": [
            {"start": "09:00", "end": "12:00"},
            {"start": "14:00", "end": "18:00"},
        ],
        "时区": "Asia/Shanghai",
    }

    名称_高峰 = "高峰"
    名称_空闲 = "空闲"

    def __init__(self, 价格抓取器=None, 配置=None,
                 打印启动状态: bool = True):
        if isinstance(价格抓取器, dict) and 配置 is None:
            价格抓取器, 配置 = None, 价格抓取器
        self.AI手动覆盖状态: Optional[bool] = None
        self._价格抓取器 = 价格抓取器
        self._原始配置 = 配置 if isinstance(配置, dict) else None
        self._配置 = 取AI配置(配置)
        if 打印启动状态:
            self._打印启动状态()

    # ==================== 配置 ====================

    @property
    def 配置(self) -> dict:
        """当前生效的 AI 段配置（``AI配置()`` 同构）。"""
        return self._配置

    def 设置配置(self, 配置=None):
        """换一份配置（dict / 整份配置 / V8 风格对象）。"""
        self._原始配置 = 配置 if isinstance(配置, dict) else None
        self._配置 = 取AI配置(配置)

    def 刷新配置(self):
        """重新读一遍 V8_3 的 ``配置.json``。"""
        self.设置配置(self._原始配置)

    # ==================== 时间/时段 ====================

    def 获取当前时间(self) -> datetime:
        """按配置时区取当前时间（时区无效时退回本地时间）。"""
        时区 = _解析时区(self._获取时段配置().get("时区")
                       or (self._配置.get("高峰时段") or {}).get("时区"))
        if 时区 is not None:
            try:
                return datetime.now(时区)
            except Exception:
                pass
        return datetime.now()

    def 获取时区名(self) -> str:
        return str(self._获取时段配置().get("时区") or "")

    @staticmethod
    def _解析时间字符串(s: str) -> tuple:
        try:
            部分 = str(s).strip().split(":")
            return int(部分[0]), int(部分[1]) if len(部分) > 1 else 0
        except Exception:
            return 0, 0

    @staticmethod
    def _在时段内(现在: datetime, 时段: dict) -> bool:
        try:
            起时, 起分 = 时段管理器._解析时间字符串(
                时段.get("start", 时段.get("开始", "00:00")))
            止时, 止分 = 时段管理器._解析时间字符串(
                时段.get("end", 时段.get("结束", "23:59")))
        except Exception:
            return False

        当前分钟 = 现在.hour * 60 + 现在.minute
        起分钟 = 起时 * 60 + 起分
        止分钟 = 止时 * 60 + 止分

        if 起分钟 <= 止分钟:
            return 起分钟 <= 当前分钟 < 止分钟
        return 当前分钟 >= 起分钟 or 当前分钟 < 止分钟

    def _归一化时段配置(self, 配置) -> Optional[dict]:
        """把各种写法的时段配置统一成 V8 形态 ``{"工作日","时段","时区"}``。"""
        if not isinstance(配置, dict) or not 配置:
            return None

        时区 = 配置.get("时区") or "Asia/Shanghai"

        # 0) 直给写法：{"类型": "高峰"/"低谷"/"空闲"}
        直给 = 配置.get("类型")
        if 直给:
            文本 = str(直给).strip()
            名称 = self.名称_高峰 if 文本 in ("高峰", "峰值") else self.名称_空闲
            return {"工作日": [0, 1, 2, 3, 4, 5, 6], "时段": [],
                    "时区": 时区, "_强制类型": 名称}

        # 1) V8 写法：{"工作日","时段":[{"start","end"}]}，也容错单个 dict。
        #    注意：V8 风格的时段配置（例如 ai_schedule）与 V8_3 默认段深合并后
        #    会同时带上 "时段" 和 "开始/结束"，此时以显式的 "时段" 列表为准。
        时段列表 = 配置.get("时段")
        if isinstance(时段列表, dict):
            时段列表 = [时段列表]
        if isinstance(时段列表, list) and (时段列表
                                       or 配置.get("工作日") is not None):
            if 配置.get("启用") is False:
                时段列表 = []
            规范化 = []
            for 项 in 时段列表:
                if not isinstance(项, dict):
                    continue
                起 = 项.get("start", 项.get("开始"))
                止 = 项.get("end", 项.get("结束"))
                if 起 is None or 止 is None:
                    continue
                规范化.append({"start": str(起), "end": str(止)})
            return {
                "工作日": list(配置.get("工作日", [0, 1, 2, 3, 4])),
                "时段": 规范化,
                "时区": 时区,
            }

        # 2) V8_3 写法：{"启用","开始","结束","时区"}
        if "开始" in 配置 or "结束" in 配置:
            启用 = bool(配置.get("启用", True))
            时段列表 = []
            if 启用:
                时段列表 = [{"start": str(配置.get("开始") or "00:00"),
                           "end": str(配置.get("结束") or "23:59")}]
            return {"工作日": [0, 1, 2, 3, 4, 5, 6] if 启用 else [],
                    "时段": 时段列表, "时区": 时区}

        return None

    def _获取时段配置(self) -> dict:
        """价格抓取器优先（V8 行为），否则用 ``AI配置()['高峰时段']``。"""
        if self._价格抓取器:
            try:
                归一 = self._归一化时段配置(
                    self._价格抓取器.获取高峰时段())
                if 归一:
                    return 归一
            except Exception:
                pass
        归一 = self._归一化时段配置(self._配置.get("高峰时段"))
        if 归一:
            return 归一
        return dict(self.兜底时段)

    # ==================== 对外接口（V8 API） ====================

    def 获取当前时段类型(self) -> str:
        """返回 ``"高峰"`` 或 ``"空闲"``（低谷）。"""
        现在 = self.获取当前时间()
        配置 = self._获取时段配置()
        强制 = 配置.get("_强制类型")
        if 强制:
            return 强制
        星期几 = 现在.weekday()
        if 星期几 not in 配置.get("工作日", [0, 1, 2, 3, 4]):
            return self.名称_空闲
        for 时段 in 配置.get("时段", []):
            if self._在时段内(现在, 时段):
                return self.名称_高峰
        return self.名称_空闲

    def 是否空闲时段(self) -> bool:
        """低谷（空闲）时段 → True；等价于 V8 的“可以启用 AI”。"""
        return self.获取当前时段类型() == self.名称_空闲

    def 是否启用AI(self) -> bool:
        if self.AI手动覆盖状态 is not None:
            return bool(self.AI手动覆盖状态)
        if not self._配置.get("启用", True):
            return False
        return self.是否空闲时段()

    def 设置手动覆盖(self, 状态):
        """``None`` 取消覆盖；``True`` 强制启用 AI；``False`` 强制关闭。"""
        self.AI手动覆盖状态 = 状态

    def 获取时段摘要(self) -> str:
        名称 = self.获取当前时段类型()
        覆盖 = ("自动" if self.AI手动覆盖状态 is None
              else ("手动开" if self.AI手动覆盖状态 else "手动关"))
        return (f"{名称} | AI: {'✅' if self.是否启用AI() else '❌'} "
                f"({覆盖})")

    def _打印启动状态(self):
        现在 = self.获取当前时间()
        星期名 = ["周一", "周二", "周三", "周四", "周五",
                 "周六", "周日"][现在.weekday()]
        时段 = self.获取当前时段类型()
        AI状态 = "✅" if self.是否启用AI() else "❌"
        logger.info("[时段管理器] 🕐 %s (%s) | 当前: %s | AI: %s",
                    现在.strftime("%Y-%m-%d %H:%M:%S"), 星期名,
                    时段, AI状态)


class 预算管理器:
    """预算管理器 - 余额/熔断（公开 API 与 V8 一致）。"""

    def __init__(self, 初始充值金额=None, 低余额阈值=None, 配置=None,
                 当前余额=None, 自动同步: bool = False, 保存回调=None):
        if isinstance(初始充值金额, dict) and 配置 is None:
            初始充值金额, 配置 = None, 初始充值金额
        AI段 = 取AI配置(配置)
        预算 = AI段.get("预算") or {}
        self._原始配置 = 配置 if isinstance(配置, dict) else None
        self._AI段 = AI段
        self.自动同步 = bool(自动同步)
        self._保存回调 = 保存回调

        self.初始充值金额 = float(预算.get("初始充值金额") or 50.0) \
            if 初始充值金额 is None else float(初始充值金额)
        self.阈值 = float(预算.get("低余额阈值") or 2.0) \
            if 低余额阈值 is None else float(低余额阈值)

        初值 = 当前余额 if 当前余额 is not None else 预算.get("当前余额")
        self._余额 = max(0.0, float(初值)) if 初值 is not None \
            else self.初始充值金额

        self.熔断状态 = False
        self._查询失败次数 = 0

    # ==================== 只读属性（V8 API） ====================

    @property
    def 当前余额(self) -> float:
        return self._余额

    @property
    def 是否已熔断(self) -> bool:
        return self.熔断状态 or self._余额 < self.阈值

    @property
    def 查询失败次数(self) -> int:
        return self._查询失败次数

    # ==================== 变更（V8 API） ====================

    def 更新余额(self, 新余额: float) -> bool:
        """更新余额；余额跌破阈值而**刚刚**熔断时返回 True。"""
        self._余额 = max(0.0, float(新余额))
        刚熔断 = False
        if self._余额 < self.阈值 and not self.熔断状态:
            self.熔断状态 = True
            刚熔断 = True
        elif self._余额 >= self.阈值 and self.熔断状态:
            self.熔断状态 = False
            self._查询失败次数 = 0
        self._变更后()
        return 刚熔断

    def 标记查询失败(self):
        """连续 3 次余额查询失败 → 熔断。"""
        self._查询失败次数 += 1
        if self._查询失败次数 >= 3:
            self.熔断状态 = True
        self._变更后()

    def 解除熔断(self):
        self.熔断状态 = False
        self._查询失败次数 = 0
        self._变更后()

    def 设置阈值(self, 低余额阈值: float):
        self.阈值 = max(0.0, float(低余额阈值))
        if self._余额 < self.阈值 and not self.熔断状态:
            self.熔断状态 = True
        elif self._余额 >= self.阈值 and self.熔断状态:
            self.熔断状态 = False
        self._变更后()

    def 重置余额(self, 新余额: float = None):
        """把余额重置成“初始充值金额”（或给定值），并解除熔断。"""
        self._余额 = max(0.0, float(新余额 if 新余额 is not None
                                  else self.初始充值金额))
        self.熔断状态 = False
        self._查询失败次数 = 0
        self._变更后()

    # ==================== 摘要 / 回写 ====================

    def 获取预算摘要(self) -> str:
        图标 = "🚨" if self.是否已熔断 else "✅"
        return (f"{图标} 余额: ¥{self._余额:.2f} | "
                f"阈值: ¥{self.阈值:.2f}")

    def 获取预算字典(self) -> dict:
        return {
            "初始充值金额": self.初始充值金额,
            "低余额阈值": self.阈值,
            "当前余额": round(self._余额, 4),
        }

    def 同步到配置(self, 配置=None, 落盘: bool = False) -> bool:
        """把余额写回配置 dict 的 ``AI.预算``，可选落盘（走 ``保存回调``）。

        * ``配置`` 省略时用构造时传进来的那份（V8_3 的 dict 才是可写的）；
        * 整份配置 dict → ``设置AI配置(整份, 预算=...)``；
        * AI 段 dict → 直接更新它的 ``预算`` 键。

        返回是否真的写进去了。
        """
        目标 = 配置 if 配置 is not None else self._原始配置
        if not isinstance(目标, dict):
            return False
        预算 = self.获取预算字典()
        try:
            if isinstance(目标.get("AI"), dict):
                设置AI配置(目标, 预算=预算)
            else:
                目标["预算"] = 预算
        except Exception as e:               # pragma: no cover - 容错
            logger.warning("[预算管理器] 回写配置失败: %s", e)
            return False
        if 落盘 and self._保存回调 is not None:
            try:
                self._保存回调(目标)
            except Exception as e:           # pragma: no cover - 容错
                logger.warning("[预算管理器] 保存配置失败: %s", e)
        return True

    def _变更后(self):
        if self.自动同步:
            self.同步到配置(落盘=self._保存回调 is not None)
