# 百度网盘适配器/核心/认证/认证服务.py
"""
认证服务
作用：会话引导（取 bdstoken/uk）、登录态自检、用户信息获取

三个端点均来自真机实测（见 PLAN.md §6.5）：

  1. GET /api/gettemplatevariable?fields=["bdstoken","token","uk","isdocuser","servertime"]
     → { errno:0, result:{ bdstoken:"<32hex>", token:"<32hex>", uk:455281002,
                           isdocuser:1, servertime:1789325407 } }
     **这是会话引导入口** —— 登录后第一件事就是取 bdstoken + uk。

  2. GET /api/loginStatus?clienttype=1&app_id=250528&web=1&channel=web&version=0
     → { errno:0, login_info:{...}, newno:"", show_msg:"", request_id }

  3. GET /api/user/getinfo
     → { errno:0, records:[{ uk, uname, avatar_url, vip_type, vip_level, ... }] }
     ⚠️ 用户信息在 **records[0]**，不是 data。

本服务同时充当网络客户端的 `会话刷新器`：网络层遇到 `errno:-6` 时会回调
`刷新会话()` 补取 bdstoken 并重放请求——这是修复原骨架「令牌不回流」缺陷的闭环。
"""
from __future__ import annotations

import logging

from .会话仓库 import 会话仓库, 全局会话仓库
from ..网络.网络客户端 import 网络客户端, 接口错误

logger = logging.getLogger("百度网盘.认证")

# 会话引导需要的字段
_模板字段 = '["bdstoken","token","uk","isdocuser","servertime"]'


class 认证服务:
    def __init__(self, 网络: 网络客户端 | None = None,
                 仓库: 会话仓库 | None = None):
        self.仓库 = 仓库 or 全局会话仓库
        # 把「取会话」注入网络层：每请求实时取，bdstoken 一刷新即生效
        self.网络 = 网络 or 网络客户端(
            会话提供者=self.仓库.取会话,
            会话刷新器=self.刷新会话,
        )

    # ---------------- 会话引导 ----------------

    def 取模板变量(self) -> dict:
        """GET /api/gettemplatevariable —— 取 bdstoken / token / uk。"""
        结果 = self.网络.请求(
            "GET", "/api/gettemplatevariable",
            params={"fields": _模板字段},
        )
        数据 = 结果.get("result") if isinstance(结果, dict) else None
        if not isinstance(数据, dict):
            raise 接口错误(f"gettemplatevariable 响应异常：{结果}")
        # 落库：bdstoken 会随会话变化，必须持久化以便下次启动复用
        bdstoken = 数据.get("bdstoken")
        if bdstoken:
            self.仓库.更新bdstoken(bdstoken, 数据.get("uk"))
        logger.info(
            f"[认证] 模板变量已取：uk={数据.get('uk')} "
            f"isdocuser={数据.get('isdocuser')}")
        return 数据

    def 刷新会话(self) -> bool:
        """供网络层回调：补取 bdstoken。成功返回 True。

        注意本方法**不得抛异常**（网络层在异常路径上调用它）。
        """
        try:
            if not self.仓库.是否有效():
                logger.debug("[认证] 会话无效，跳过刷新")
                return False
            数据 = self.取模板变量()
            return bool(数据.get("bdstoken"))
        except Exception as e:
            logger.warning(f"[认证] 刷新会话失败：{type(e).__name__}: {e}")
            return False

    # ---------------- 登录态 ----------------

    def 检查登录态(self) -> dict:
        """GET /api/loginStatus —— 会话自检。

        ⚠️ 该接口的公共参数与业务接口不同：`clienttype=1`、`channel=web`。
        """
        结果 = self.网络.请求(
            "GET", "/api/loginStatus",
            params={"clienttype": "1", "channel": "web", "version": "0"},
            带渠道=False,
        )
        信息 = 结果.get("login_info") if isinstance(结果, dict) else None
        if not isinstance(信息, dict):
            raise 接口错误(f"loginStatus 响应异常：{结果}")
        return 信息

    def 是否已登录(self) -> bool:
        """轻量自检：拿得到 bdstoken 即视为已登录。"""
        try:
            return bool(self.取模板变量().get("bdstoken"))
        except Exception as e:
            logger.debug(f"[认证] 登录自检失败：{type(e).__name__}: {e}")
            return False

    # ---------------- 用户信息 ----------------

    def 取当前用户(self) -> dict:
        """GET /api/user/getinfo —— 返回 records[0]。

        ⚠️ 实测必需参数（缺任一个返回 `errmsg:"params error"`）：
             need_boardinfo=1
             user_list=[<uk>]      # JSON 数组，uk 来自会话
        """
        if not self.仓库.是否有效():
            raise RuntimeError("无有效会话：请先登录")
        uk = self.仓库.获取uk()
        if not uk:
            # 会话里没有 uk 时先补一次（gettemplatevariable 会回填）
            self.取模板变量()
            uk = self.仓库.获取uk()
        if not uk:
            raise RuntimeError("会话中缺少 uk，无法查询用户信息")

        结果 = self.网络.请求(
            "GET", "/api/user/getinfo",
            params={"need_boardinfo": "1", "user_list": f"[{uk}]"},
        )
        记录 = 结果.get("records") if isinstance(结果, dict) else None
        if not isinstance(记录, list) or not 记录:
            raise 接口错误(f"user/getinfo 响应异常：{结果}")
        用户 = 记录[0]
        # 顺带回填 uk，便于其它模块使用
        if 用户.get("uk"):
            self.仓库.保存会话(uk=用户.get("uk"))
        logger.debug(f"[认证] 当前用户：{用户.get('uname')}")
        return 用户

    # ---------------- 容量 ----------------

    def 取容量(self) -> dict:
        """GET /api/quota —— 网盘容量。

        → { errno:0, total, used, free, expire, recyclestatus, server_time }
        """
        数据 = self.网络.请求("GET", "/api/quota", 带渠道=False)
        if not isinstance(数据, dict) or "total" not in 数据:
            raise 接口错误(f"quota 响应异常：{数据}")
        logger.debug(f"[认证] 容量：{数据.get('used')}/{数据.get('total')}")
        return 数据

    def 关闭(self) -> None:
        self.网络.关闭()
