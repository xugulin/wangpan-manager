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
import threading
import time

from .会话仓库 import 会话仓库, 全局会话仓库
from ..网络.网络客户端 import 网络客户端, 接口错误

logger = logging.getLogger("百度网盘.认证")

# 会话引导需要的字段
_模板字段 = '["bdstoken","token","uk","isdocuser","servertime"]'

# ==========================================================================
# 认证层「刷新会话」的全局重入保护 + 日志节流
# ==========================================================================
# 现场故障（用户报告，2026-09-19）：
#
#   [认证] 刷新会话失败：RecursionError: maximum recursion depth exceeded
#   [认证] 刷新会话失败：接口返回异常（errno:-6）   ← 一次会话刷了 730 条
#
# 成因：刷新会话本身要发**网络请求**，请求失败又会回头找认证层刷新 ——
#       认证层 ↔ 网络层互相调用。网络层只按**线程**做了重入保护
#       （网络客户端._刷新节流），刷新要是发生在另一个线程上就兜不住；
#       而每次失败都 `logger.warning` 一条，于是日志被刷成瀑布。
#
# 修法与网络层逐条对齐：
#   ① 全局重入保护：同线程/其它线程正在刷新时，本次直接返回上次结论；
#   ② 失败结论 60 秒内复用（成功结论 30 秒），不重复打日志；
#   ③ 失败日志**只在状态变化时打一条**（首次失败、或从成功变失败），
#      之后降到 debug —— 界面上的日志窗口不再被同一条错误淹没。
_认证节流锁 = threading.Lock()
_认证节流: dict = {
    "进行中": False,
    "上次时间": 0.0,
    "上次结果": False,
    "连续失败": 0,
}

#: 成功结论复用窗口（秒）
刷新成功复用时 = 30.0
#: 失败结论复用窗口（秒）——失败往往要等用户重新登录，别反复试
刷新失败复用时 = 60.0


def _取模板变量_不经刷新器(网络: 网络客户端) -> dict:
    """取 bdstoken/uk，且**绝不触发会话刷新回调**。

    这条路径是刷新器自己用的：它再触发刷新就成环了。做法是临时摘掉
    网络客户端的 `会话刷新器`（网络层遇到 errno:-6 时就只会老实报错）。
    """
    原刷新器 = getattr(网络, "会话刷新器", None)
    try:
        网络.会话刷新器 = None
        结果 = 网络.请求(
            "GET", "/api/gettemplatevariable",
            params={"fields": _模板字段},
        )
    finally:
        try:
            网络.会话刷新器 = 原刷新器
        except Exception:
            pass
    if not isinstance(结果, dict):
        raise 接口错误(f"gettemplatevariable 响应异常：{结果}")
    数据 = 结果.get("result")
    if not isinstance(数据, dict):
        raise 接口错误(f"gettemplatevariable 响应异常：{结果}")
    return 数据


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

    def 取模板变量(self, 允许刷新: bool = True) -> dict:
        """GET /api/gettemplatevariable —— 取 bdstoken / token / uk。

        :param 允许刷新: 为 False 时**不触发**会话刷新回调（刷新器内部用，
                         否则 认证层 ↔ 网络层 会互相调用成环）。
        """
        结果 = (_取模板变量_不经刷新器(self.网络) if not 允许刷新
                else self.网络.请求(
                    "GET", "/api/gettemplatevariable",
                    params={"fields": _模板字段},
                ).get("result"))
        if not isinstance(结果, dict):
            raise 接口错误(f"gettemplatevariable 响应异常：{结果}")
        数据 = 结果
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
        带全局重入保护 + 结论复用 + 日志节流，详见模块头注释。
        """
        现在 = time.time()
        with _认证节流锁:
            if _认证节流["进行中"]:
                # 有人正在刷（可能是别的线程）：直接复用上次结论，
                # 绝不再发一次请求 —— 这就是原先 RecursionError 的入口。
                return bool(_认证节流["上次结果"])
            间隔 = 现在 - float(_认证节流["上次时间"] or 0.0)
            窗口 = (刷新成功复用时 if _认证节流["上次结果"]
                    else 刷新失败复用时)
            if _认证节流["上次时间"] and 间隔 < 窗口:
                return bool(_认证节流["上次结果"])
            _认证节流["进行中"] = True
            _认证节流["上次时间"] = 现在

        try:
            if not self.仓库.是否有效():
                logger.debug("[认证] 会话无效，跳过刷新")
                结果 = False
            else:
                数据 = self.取模板变量(允许刷新=False)
                结果 = bool(数据.get("bdstoken"))
        except RecursionError:
            # 真出现递归就彻底收手，并且只报一次
            self._记失败("会话刷新出现递归，已中止", 级别="warning")
            结果 = False
        except Exception as e:
            self._记失败(f"{type(e).__name__}: {e}", 级别="warning")
            结果 = False
        finally:
            with _认证节流锁:
                _认证节流["进行中"] = False
                _认证节流["上次结果"] = 结果
                if 结果:
                    _认证节流["连续失败"] = 0
        if 结果:
            logger.debug("[认证] 会话已刷新（bdstoken 已更新）")
        return 结果

    @staticmethod
    def _记失败(描述: str, 级别: str = "warning") -> None:
        """失败日志节流：只在**状态变化**时打一条，其余降到 debug。"""
        with _认证节流锁:
            首次 = _认证节流["连续失败"] == 0
            _认证节流["连续失败"] = int(_认证节流["连续失败"]) + 1
            次数 = _认证节流["连续失败"]
        if 首次:
            logger.log(
                logging.WARNING if 级别 == "warning" else logging.INFO,
                f"[认证] 刷新会话失败（后续同类失败只记 debug，不再刷屏）：{描述}")
        else:
            logger.debug(f"[认证] 刷新会话仍然失败（第 {次数} 次）：{描述}")

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

    def 从登录态补bdstoken(self) -> str:
        """兜底路径：`/api/loginStatus` 的 `login_info.bdstoken`。

        ✅ 真机实测（2026-09-19）：同一个会话上
             `/api/gettemplatevariable` → `{"errno":-6,"result":[]}`
             `/api/loginStatus`          → `{"errno":0,...,"bdstoken":"c5ff…"}`
        也就是说**取 bdstoken 不是只有一条路**：模板变量接口会因会话形态
        （只读会话 / 风控 / SESSION 过期）回 -6，而 loginStatus 照样给出
        bdstoken。以前只认模板变量，于是"读接口全通、写操作全废"，
        界面只能报「bdstoken 刷新失败（errno -6）」。
        """
        信息 = self.检查登录态()
        令牌 = str(信息.get("bdstoken") or "")
        uk = 信息.get("uk")
        if 令牌:
            self.仓库.更新bdstoken(令牌, uk)
            logger.info(f"[认证] 已从 loginStatus 补到 bdstoken（uk={uk}）")
        return 令牌

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
