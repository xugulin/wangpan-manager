# 夸克网盘适配器/核心/认证/认证服务.py
"""
认证服务（阶段二 2.4）

夸克**没有**「取当前用户」那样的独立接口（原适配器是账号域名下的
`/v1/user/me`）。经 2026-09-14 实测，可用的账号信息端点是 **`GET member`**：

    GET /1/clouddrive/member?pr=ucpro&fr=pc&uc_param_str=&__t=…&__dt=1000
    → data.total_capacity / data.use_capacity / data.member_type / data.exp_at …

⚠️ 参考实现 QuarkPan 的 `services/file_service.py:240-248` 用的是
   `GET capacity`，该路径**实测返回 404**（`/1/clouddrive/capacity` 不存在），
   其 `get_storage_info()` 实际是坏的。`capacity` / `file/capacity` /
   `member/capacity` 三个路径均 404，只有 `member` 可用。

因此本模块的职责：
  1. `取账号信息()` —— GET member（会员、容量、回收站策略等）
  2. `取容量()`     —— 从 member 结果里抽出容量三元组
  3. `是否已登录()` —— 本地凭证 + 服务端探测

需要账号昵称等资料时，走扫码登录时保存的 `用户信息`（见 登录服务）。
"""
import logging
from typing import Optional

from .凭证仓库 import 全局凭证仓库
from ..网络.网络客户端 import 网络客户端, 接口错误

logger = logging.getLogger("夸克网盘.认证")


class 认证服务:
    """夸克账号信息（以 capacity 为核心）"""

    def __init__(self, 网络: 网络客户端 | None = None):
        # 由 GUI 注入的网络客户端（已带 Cookie）；不注入则按需自建
        self._外部网络 = 网络
        self._自有网络: Optional[网络客户端] = None

    # ---------- 内部 ----------

    def _取网络(self, 网络: 网络客户端 | None = None) -> 网络客户端:
        if 网络 is not None:
            return 网络
        if self._外部网络 is not None:
            return self._外部网络
        if self._自有网络 is None:
            cookie = 全局凭证仓库.取Cookie()
            if not cookie:
                raise 接口错误("未登录：本地没有可用凭证，请先登录")
            self._自有网络 = 网络客户端(cookie=cookie)
        return self._自有网络

    # ---------- 对外 ----------

    def 取账号信息(self, 网络: 网络客户端 | None = None) -> dict:
        """GET member —— 账号/会员/容量信息

        实测返回字段（2026-09-14）：
          total_capacity / use_capacity / secret_total_capacity
          member_type（如 SUPER_VIP）/ exp_at / subscribe_status
          deep_recycle_stat（回收站保留天数策略）

        :return: 响应 data 字典（失败时返回 {}）
        """
        客户端 = self._取网络(网络)
        logger.debug("[认证] GET member")
        结果 = 客户端.请求("GET", "member")
        if isinstance(结果, dict):
            return 结果.get("data") or {}
        return {}

    def 取容量(self, 网络: 网络客户端 | None = None) -> dict:
        """从 member 结果中抽取容量三元组

        :return: {"total_capacity":…, "use_capacity":…, "free_capacity":…,
                  "member_type":…}
        """
        信息 = self.取账号信息(网络)
        总 = 信息.get("total_capacity") or 0
        已用 = 信息.get("use_capacity") or 0
        try:
            剩余 = max(0, int(总) - int(已用))
        except (TypeError, ValueError):
            剩余 = 0
        return {
            "total_capacity": 总,
            "use_capacity": 已用,
            "free_capacity": 剩余,
            "member_type": 信息.get("member_type"),
        }

    def 取会员权益(self, 网络: 网络客户端 | None = None) -> dict:
        """从 member 结果里抽取会员权益（阶段三 3.7）

        夸克没有独立的「用户权益」接口（原适配器的
        `/nd.bizassets.s/v1/get_user_rights` 在夸克 404）；
        权益信息本来就在 `member` 的这几个子对象里。
        """
        信息 = self.取账号信息(网络)
        权益: dict = {}
        for 键 in ("member_type", "subscribe_status", "exp_at",
                   "subscribe_pay_channel"):
            if 键 in 信息:
                权益[键] = 信息[键]
        权益.update(信息.get("member_info") or {})
        权益.update(信息.get("member_status") or {})
        if 信息.get("deep_recycle_stat"):
            权益["deep_recycle_stat"] = 信息["deep_recycle_stat"]
        if 信息.get("extend_capacity_composition"):
            权益["extend_capacity_composition"] = \
                信息["extend_capacity_composition"]
        return 权益

    def 是否已登录(self, 网络: 网络客户端 | None = None) -> bool:
        """本地凭证有效 **且** 服务端探测通过"""
        if not 全局凭证仓库.是否有效(安全余量=60.0):
            return False
        if 网络 is None and self._外部网络 is None:
            # 没有可复用的客户端时，只信本地凭证，避免每次判登录都发请求
            return True
        try:
            self.取账号信息(网络)
            return True
        except Exception as e:
            logger.warning(f"[认证] 登录态探测失败：{type(e).__name__}: {e}")
            return False

    def 关闭(self) -> None:
        """只关闭自己创建的客户端；外部注入的由调用方负责"""
        if self._自有网络 is not None:
            try:
                self._自有网络.关闭()
            except Exception:
                pass
            self._自有网络 = None
