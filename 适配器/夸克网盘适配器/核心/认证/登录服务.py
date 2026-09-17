# 夸克网盘适配器/核心/认证/登录服务.py
"""
登录服务（阶段二 2.3）

提供两个登录通道：

  ① 扫码登录      —— 夸克 CAS 二维码（推荐）
  ② 手动Cookie登录 —— 从浏览器 F12 复制 Cookie（兜底，最可靠）

夸克与原适配器的登录模型完全不同：

  ┌──────────┬────────────────────────────────────┬──────────────────────────────────┐
  │          │ 原适配器                            │ 夸克                              │
  ├──────────┼────────────────────────────────────┼──────────────────────────────────┤
  │ 流程      │ OAuth2 设备码 + 轮询 /v1/auth/token │ CAS 二维码 token → service_ticket │
  │ 产物      │ access_token + refresh_token        │ Cookie（__pus/__kps/__uid…）       │
  │ 续期      │ refresh_token 静默续期               │ 无，过期只能重新登录               │
  │ 短信登录  │ 支持                                │ **不支持**（本模块已删除短信通道）  │
  └──────────┴────────────────────────────────────┴──────────────────────────────────┘

扫码 5 步（对齐 QuarkPan `auth/api_login.py`）：
  1) GET  uop.quark.cn/cas/ajax/getTokenForQrcodeLogin
          → data.members.token                 （api_login.py:94-141）
  2) 拼二维码 URL  su.quark.cn/4_eMHBJ?...      （api_login.py:143-171）
  3) 轮询 uop.quark.cn/cas/ajax/getServiceTicketByQrcodeToken
          → data.members.service_ticket        （api_login.py:255-345）
  4) GET  pan.quark.cn/account/info?st=<ticket>&lw=scan
          → 服务端 Set-Cookie 落到 httpx 的 cookie jar（api_login.py:428-465）
  5) 从 jar 中筛出 quark.cn 域名的 Cookie       （api_login.py:490-499）
"""
import logging
import time
import urllib.parse
import uuid
from typing import Any, Callable, Optional

import httpx

from .凭证仓库 import (
    全局凭证仓库, 校验Cookie, 解析Cookie字符串, 拼接Cookie字符串,
)
from ..网络.网络客户端 import 网络客户端

logger = logging.getLogger("夸克网盘.登录")

# ==================== 常量（对齐 QuarkPan auth/api_login.py）====================

客户端ID = "532"                 # 夸克 CAS 固定 client_id（api_login.py:109）
授权版本 = "1.2"

取二维码Token地址 = "https://uop.quark.cn/cas/ajax/getTokenForQrcodeLogin"
查扫码状态地址 = "https://uop.quark.cn/cas/ajax/getServiceTicketByQrcodeToken"
二维码基址 = "https://su.quark.cn/4_eMHBJ"
二维码业务串 = "S:custom|OPT:SAREA@0|OPT:IMMERSIVE@1|OPT:BACK_BTN_STYLE@0"
账号信息地址 = "https://pan.quark.cn/account/info"

扫码成功状态码 = 2000000          # api_login.py:319
扫码等待状态码 = 50004001         # api_login.py:336
扫码失败状态码 = (50004002, 50004003, 50004004)   # api_login.py:337

轮询间隔秒 = 2.0                  # api_login.py:393
默认扫码超时秒 = 300.0            # api_login.py:23
默认请求超时秒 = 30.0


def 二维码请求头() -> dict:
    """CAS 域名用的浏览器请求头（对齐 QuarkPan api_login.py:40-52）"""
    return {
        "user-agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/139.0.0.0 Safari/537.36"
        ),
        "accept": "application/json, text/plain, */*",
        "accept-language": "zh-CN,zh;q=0.9",
        "cache-control": "no-cache",
        "pragma": "no-cache",
        "origin": "https://pan.quark.cn",
        "referer": "https://pan.quark.cn/",
    }


class 登录错误(Exception):
    """登录流程中的可预期失败（凭据无效、超时、服务端拒绝等）"""


class 登录服务:
    """夸克登录服务：扫码 + 手动 Cookie"""

    def __init__(self, 超时秒: float = 默认请求超时秒,
                 扫码超时秒: float = 默认扫码超时秒):
        self.超时秒 = 超时秒
        self.扫码超时秒 = 扫码超时秒
        # follow_redirects=True 是必须的：第 4 步的 Set-Cookie 发生在重定向链上
        self.客户端 = httpx.Client(
            timeout=httpx.Timeout(
                connect=10.0, read=超时秒, write=超时秒, pool=30.0),
            http2=False,
            follow_redirects=True,
            headers=二维码请求头(),
        )

    # ==================== 通道一：扫码登录 ====================

    def 取登录二维码(self) -> dict:
        """第 1~2 步：取 qr token 并拼出二维码 URL

        :return: {"token": qr_token, "url": 二维码URL}
        """
        请求参数 = {
            "client_id": 客户端ID,
            "v": 授权版本,
            "request_id": str(uuid.uuid4()),
        }
        logger.info("[登录] 请求扫码 token")
        try:
            响应 = self.客户端.get(取二维码Token地址, params=请求参数)
        except Exception as e:
            raise 登录错误(f"请求二维码 token 失败：{e}") from e

        if 响应.status_code != 200:
            raise 登录错误(f"请求二维码 token 失败：HTTP {响应.status_code}")

        try:
            数据 = 响应.json()
        except Exception as e:
            raise 登录错误(f"二维码 token 响应不是合法 JSON：{e}") from e

        if int(数据.get("status", -1)) != 扫码成功状态码:
            raise 登录错误(
                f"获取二维码 token 失败：{数据.get('message') or 数据}")

        token = ((数据.get("data") or {}).get("members") or {}).get("token")
        if not token:
            raise 登录错误("二维码 token 响应中未找到 data.members.token")

        return {"token": token, "url": self._构造二维码URL(token)}

    def _构造二维码URL(self, token: str) -> str:
        """第 2 步：拼二维码 URL（对齐 api_login.py:143-171）"""
        查询 = urllib.parse.urlencode({
            "token": token,
            "client_id": 客户端ID,
            "ssb": "weblogin",
            "uc_param_str": "",
            "uc_biz_str": 二维码业务串,
        })
        return f"{二维码基址}?{查询}"

    def 查扫码状态(self, token: str) -> dict:
        """第 3 步：查询一次扫码状态

        :return: {"状态": "等待"|"成功"|"失败", "消息": str, "service_ticket": str|None}
        """
        请求参数 = {
            "client_id": 客户端ID,
            "v": 授权版本,
            "token": token,
            "request_id": str(uuid.uuid4()),
        }
        try:
            响应 = self.客户端.get(查扫码状态地址, params=请求参数)
        except Exception as e:
            return {"状态": "等待", "消息": f"查询异常：{e}", "service_ticket": None}

        if 响应.status_code != 200:
            return {"状态": "等待",
                    "消息": f"HTTP {响应.status_code}", "service_ticket": None}

        try:
            数据 = 响应.json()
        except Exception:
            return {"状态": "等待", "消息": "响应非 JSON", "service_ticket": None}

        状态码 = 数据.get("status")
        消息 = str(数据.get("message") or "")
        ticket = ((数据.get("data") or {}).get("members") or {}).get(
            "service_ticket")

        if int(状态码 or -1) == 扫码成功状态码 and ticket:
            return {"状态": "成功", "消息": 消息, "service_ticket": ticket}

        失败 = (
            int(状态码 or -1) in 扫码失败状态码
            or any(k in 消息.lower()
                   for k in ("expired", "failed", "error", "timeout", "invalid"))
        )
        if 失败:
            return {"状态": "失败", "消息": 消息 or "服务端判定失败",
                    "service_ticket": None}

        return {"状态": "等待", "消息": 消息, "service_ticket": None}

    def 等扫描码(
        self,
        token: str,
        状态回调: Optional[Callable[[str], None]] = None,
    ) -> dict:
        """第 3 步：轮询直到扫码成功 / 失败 / 超时"""
        开始时间 = time.time()
        次数 = 0
        logger.info("[登录] 开始等待扫码")
        while time.time() - 开始时间 < self.扫码超时秒:
            次数 += 1
            状态 = self.查扫码状态(token)
            已等待 = int(time.time() - 开始时间)
            if 状态回调:
                try:
                    状态回调(f"等待扫码…（第 {次数} 次查询，已等待 {已等待}s）")
                except Exception:
                    pass
            if 状态["状态"] == "成功":
                logger.info("[登录] 扫码成功，已取得 service_ticket")
                return 状态
            if 状态["状态"] == "失败":
                raise 登录错误(f"扫码失败：{状态.get('消息')}")
            time.sleep(轮询间隔秒)
        raise 登录错误(f"扫码超时（{int(self.扫码超时秒)} 秒）")

    def 扫码登录(
        self,
        二维码回调: Optional[Callable[[str, str], None]] = None,
        状态回调: Optional[Callable[[str], None]] = None,
        保存: bool = True,
    ) -> dict:
        """完整扫码登录流程

        :param 二维码回调: `回调(二维码链接, 用户码)`；夸克没有用户码，第二参恒为 ""
        :param 状态回调: `回调(状态文本)`
        :param 保存: 是否写入全局凭证仓库
        :return: 凭证字典（见 `_用Ticket换凭证`）
        """
        二维码 = self.取登录二维码()
        if 二维码回调:
            try:
                二维码回调(二维码["url"], "")
            except Exception:
                logger.exception("[登录] 二维码回调异常")

        状态 = self.等扫描码(二维码["token"], 状态回调)
        结果 = self._用Ticket换凭证(状态["service_ticket"], 来源="qrcode")

        if 保存:
            self.保存凭证(结果)
        return 结果

    # ==================== 第 4~5 步：ticket 换 Cookie ====================

    def _用Ticket换凭证(self, service_ticket: str, 来源: str = "qrcode") -> dict:
        """用 service_ticket 请求账号信息，从 cookie jar 提取 Cookie"""
        if not service_ticket:
            raise 登录错误("service_ticket 为空")

        logger.info("[登录] 用 service_ticket 换取 Cookie")
        try:
            响应 = self.客户端.get(
                账号信息地址, params={"st": service_ticket, "lw": "scan"})
        except Exception as e:
            raise 登录错误(f"请求账号信息失败：{e}") from e

        if 响应.status_code != 200:
            raise 登录错误(f"换取 Cookie 失败：HTTP {响应.status_code}")

        用户信息: Any = None
        try:
            用户信息 = 响应.json()
        except Exception:
            logger.debug("[登录] 账号信息响应非 JSON，忽略")

        # ⚠️ 关键补全（2026-09-14 实测）：
        #   CAS 扫码登录**不会**下发 `__puus`，而下载直链的 OSS 回调鉴权
        #   强制要求它（缺失时返回 403 RequestDeniedByCallback / auth miss）。
        #   登录后调用 `member` 可让服务端把 `__puus` 写进 cookie jar。
        try:
            补 = self.客户端.get(
                "https://pan.quark.cn/1/clouddrive/member",
                params={"pr": "ucpro", "fr": "pc"})
            logger.debug(f"[登录] 补全 __puus 请求 → HTTP {补.status_code}")
        except Exception as e:
            logger.warning(f"[登录] 补全 __puus 失败（下载会受影响）：{e}")

        字典, 字符串, 过期时间 = self._从Jar提取Cookie()
        if "__puus" not in 字典:
            logger.warning(
                "[登录] 仍未获取到 __puus —— 上传可用，但**下载会被 CDN 拒绝**。"
                "建议改用「手动 Cookie 登录」（浏览器 Cookie 自带 __puus）。")
        通过, 原因 = 校验Cookie(字典)
        if not 通过:
            raise 登录错误(
                f"服务端未下发有效 Cookie（{原因}）；"
                f"实际收到 {len(字典)} 个：{sorted(字典)[:8]}")

        logger.info(f"[登录] 已取得 {len(字典)} 个 Cookie")
        return {
            "cookie": 字符串,
            "cookies": 字典,
            "过期时间": 过期时间,
            "用户信息": 用户信息,
            "来源": 来源,
        }

    def _从Jar提取Cookie(self) -> tuple[dict[str, str], str, Optional[int]]:
        """从 httpx 的 cookie jar 中筛出 quark.cn 域名下的 Cookie

        对齐 QuarkPan `api_login.py:490-499`。

        ⚠️ 过期时间只取**鉴权关键 Cookie** 的最小 expires（2026-09-14 修正）。
        QuarkPan `login.py:62-77` 是对**全部** cookie 取 min，而登录响应里含
        `ctoken` 之类只活 ~10 分钟的跟踪 cookie，会把整份凭证的过期时间
        拉到 10 分钟，导致会话其实还好好的却本地判定为「已过期」。
        """
        字典: dict[str, str] = {}
        过期列表: list[int] = []
        关键过期键 = ("__pus", "__puus", "__kps", "__uid", "__ktd")
        try:
            迭代 = list(self.客户端.cookies.jar)
        except Exception:
            迭代 = []
        for c in 迭代:
            域名 = getattr(c, "domain", "") or ""
            if 域名 and "quark.cn" not in 域名:
                continue
            名字 = getattr(c, "name", "")
            if not 名字:
                continue
            字典[名字] = getattr(c, "value", "") or ""
            过期 = getattr(c, "expires", None)
            try:
                if 过期 and 名字 in 关键过期键:
                    过期列表.append(int(过期))
            except (TypeError, ValueError):
                pass
        return 字典, 拼接Cookie字符串(字典), (min(过期列表) if 过期列表 else None)

    # ==================== 通道二：手动 Cookie 登录 ====================

    def 手动Cookie登录(
        self,
        cookie字符串: str,
        验证: bool = True,
        保存: bool = True,
    ) -> dict:
        """从浏览器复制的 Cookie 字符串登录

        :param 验证: 是否用 `GET capacity` 向服务端确认 Cookie 真的可用
                     （默认 True；离线环境可传 False 跳过）
        :param 保存: 是否写入全局凭证仓库
        """
        字典 = 解析Cookie字符串(cookie字符串)
        通过, 原因 = 校验Cookie(字典)
        if not 通过:
            raise 登录错误(f"Cookie 校验失败：{原因}")

        字符串 = 拼接Cookie字符串(字典)
        用户信息: Any = None
        if 验证:
            容量 = self.验证Cookie(字符串)
            if 容量 is None:
                raise 登录错误(
                    "Cookie 字段齐全（含 __kps/__uid），但服务端校验未通过，"
                    "通常表示已过期或复制不完整。可勾选「跳过校验」强制保存。")
            用户信息 = 容量

        logger.info(f"[登录] 手动 Cookie 登录成功（{len(字典)} 个字段）")
        结果 = {
            "cookie": 字符串,
            "cookies": 字典,
            "过期时间": None,
            "用户信息": 用户信息,
            "来源": "manual",
        }
        if 保存:
            self.保存凭证(结果)
        return 结果

    # ==================== 公共 ====================

    def 验证Cookie(self, cookie: str) -> Optional[dict]:
        """用 `GET capacity` 验证 Cookie 是否真的能访问云盘

        :return: 成功返回 capacity 的 data 字典；失败返回 None
        """
        if not cookie:
            return None
        try:
            with 网络客户端(cookie=cookie, 超时秒=self.超时秒) as 客户端:
                数据 = 客户端.请求("GET", "capacity")
        except Exception as e:
            logger.warning(f"[登录] Cookie 校验失败：{type(e).__name__}: {e}")
            return None
        if not isinstance(数据, dict):
            return None
        return 数据.get("data") or {}

    def 保存凭证(self, 结果: dict) -> None:
        """把登录结果写入全局凭证仓库"""
        全局凭证仓库.保存Cookie(
            结果.get("cookies") or 结果.get("cookie"),
            来源=str(结果.get("来源") or "unknown"),
            过期时间=结果.get("过期时间"),
        )

    def 关闭(self) -> None:
        try:
            self.客户端.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.关闭()
