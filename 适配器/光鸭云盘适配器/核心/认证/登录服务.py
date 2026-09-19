# 光鸭云盘适配器/核心/认证/登录服务.py
"""
登录服务
支持两种登录方式：
  1. 设备授权码扫码登录（原）
  2. 手机短信验证码登录（新）

短信登录流程（参考 2026-09-13 HAR）：
  1) POST /v1/shield/captcha/init        盾初始化，获取 captcha_token
        body: {"client_id", "action":"POST:/v1/auth/verification",
               "device_id", "captcha_token":"", "meta":{"phone_number"}}
        resp: {"captcha_token":"ck0....", "expires_in":300}
  2) POST /v1/auth/verification          请求发送短信
        Header: x-captcha-token=<上一步的 captcha_token>
        body: {"phone_number","target":"ANY","client_id"}
        resp: {"verification_id":"eyJ...", "is_user":bool,
               "expires_in":300, "selected_channel":"VERIFICATION_PHONE"}
  3) POST /v1/auth/verification/verify   校验短信验证码
        body: {"verification_id","verification_code","client_id"}
        resp: {"verification_token":"eyJ...", "expires_in":600}
  4) POST /v1/auth/signin                登录
        body: {"verification_code","verification_token",
               "username","client_id"}
        resp: {"token_type":"Bearer", "access_token",
               "refresh_token", "expires_in":7200, "sub":"..."}

手机号格式：+86 13800000000（国家码 + 空格 + 号码）
"""
import logging
import time
from typing import Callable, Optional

import httpx

from .令牌仓库 import 全局令牌仓库
from ..网络.设备身份 import (
    获取设备ID, 获取设备指纹, 获取设备类型,
)

logger = logging.getLogger("光鸭云盘.登录")

认证中心 = "https://account.guangyapan.com"
客户端ID = "aMe-8VSlkrbQXpUR"
客户端版本 = "0.0.1"       # x-client-version
SDK版本 = "9.1.3"           # x-sdk-version（对齐 HAR）
协议版本 = "301"            # x-protocol-version
轮询最长等待秒 = 300

# 短信登录默认国家码
默认国家码 = "+86"

# 盾保护的动作
盾保护动作_发送短信 = "POST:/v1/auth/verification"


class 登录服务:
    def __init__(self, 超时秒: float = 20.0):
        self.设备ID = 获取设备ID()
        self.设备指纹 = 获取设备指纹()
        self.设备类型 = 获取设备类型()
        self.客户端 = httpx.Client(
            base_url=认证中心,
            timeout=超时秒,
            http2=False,
            headers={
                "accept": "*/*",
                "content-type": "application/json",
                "origin": "https://www.guangyapan.com",
                "referer": "https://www.guangyapan.com/",
                "user-agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/147.0.0.0 Safari/537.36"
                ),
            },
        )

    # ==================== 公共请求头 ====================

    def _账户请求头(self) -> dict:
        """对齐 HAR 中的 Authing SDK 设备头"""
        return {
            "accept": "*/*",
            "content-type": "application/json",
            "origin": "https://www.guangyapan.com",
            "referer": "https://www.guangyapan.com/",
            "user-agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/147.0.0.0 Safari/537.36"
            ),
            "x-client-id": 客户端ID,
            "x-client-version": 客户端版本,
            "x-device-id": self.设备ID,
            "x-device-model": "chrome%2F147.0.0.0",
            "x-device-name": "PC-Chrome",
            "x-net-work-type": "NONE",
            "x-os-version": "MacIntel",
            "x-platform-version": "1",
            "x-protocol-version": 协议版本,
            "x-provider-name": "NONE",
            "x-sdk-version": SDK版本,
        }

    # ==================== 设备授权码扫码登录（原） ====================

    def 请求设备授权码(self) -> dict:
        logger.info("[登录] 请求设备授权码")
        响应 = self.客户端.post(
            "/v1/auth/device/code",
            headers=self._账户请求头(),
            json={
                "scope": "",
                "client_id": 客户端ID,
                "project_id": "",
            },
        )
        响应.raise_for_status()
        return 响应.json()

    def 轮询令牌(self, device_code: str,
                轮询间隔: int = 3,
                有效期秒: int = 120) -> Optional[dict]:
        logger.info("[登录] 开始轮询令牌")
        开始时间 = time.time()
        最长等待 = min(有效期秒, 轮询最长等待秒)

        while time.time() - 开始时间 < 最长等待:
            time.sleep(轮询间隔)
            try:
                响应 = self.客户端.post(
                    "/v1/auth/token",
                    headers=self._账户请求头(),
                    json={
                        "grant_type":
                            "urn:ietf:params:oauth:grant-type:device_code",
                        "device_code": device_code,
                        "client_id": 客户端ID,
                    },
                )
                if 响应.status_code != 200:
                    continue
                数据 = 响应.json()
                if "access_token" in 数据:
                    return 数据
            except Exception as e:
                logger.debug(f"[登录] 轮询异常：{e}")
                continue
        return None

    def 完成扫码登录(
        self,
        二维码回调: Optional[Callable[[str, str], None]] = None,
        用户码回调: Optional[Callable[[str], None]] = None,
    ) -> dict:
        设备码数据 = self.请求设备授权码()
        设备码 = 设备码数据.get("device_code")
        用户码 = 设备码数据.get("user_code", "")
        验证地址 = (设备码数据.get("verification_uri_complete")
                    or 设备码数据.get("verification_uri")
                    or 设备码数据.get("verification_url", ""))
        轮询间隔 = int(设备码数据.get("interval", 3))
        有效期秒 = int(设备码数据.get("expires_in", 120))

        if not 设备码:
            raise RuntimeError(f"设备授权码响应异常：{设备码数据}")

        logger.info(f"[登录] 请扫码授权：{验证地址}")
        logger.info(f"[登录] 用户码：{用户码}")

        if 二维码回调:
            二维码回调(验证地址, 用户码)
        if 用户码回调:
            用户码回调(用户码)

        令牌数据 = self.轮询令牌(设备码, 轮询间隔, 有效期秒)
        if not 令牌数据:
            raise RuntimeError("扫码登录超时或被拒绝")

        self._保存令牌到仓库(令牌数据)
        logger.info("[登录] 扫码登录成功，令牌已保存")
        return 令牌数据

    # ==================== 手机短信登录（新） ====================

    def 短信登录_初始化盾(self, 手机号: str) -> str:
        """步骤 1：初始化盾，返回 captcha_token

        :param 手机号: 完整格式（含国家码），例如 "+86 13800000000"
        :return: captcha_token
        """
        手机号 = 手机号.strip()
        logger.info(f"[短信登录] 初始化盾 phone={手机号[:6]}***")

        响应 = self.客户端.post(
            "/v1/shield/captcha/init",
            headers=self._账户请求头(),
            json={
                "client_id": 客户端ID,
                "action": 盾保护动作_发送短信,
                "device_id": self.设备ID,
                "captcha_token": "",
                "meta": {"phone_number": 手机号},
            },
        )
        响应.raise_for_status()
        数据 = 响应.json()
        token = 数据.get("captcha_token", "")
        if not token:
            raise RuntimeError(f"盾初始化失败，未返回 captcha_token：{数据}")
        logger.info(
            f"[短信登录] 盾初始化成功，expires_in="
            f"{数据.get('expires_in', '?')}s")
        return token

    def 短信登录_发送验证码(
        self,
        手机号: str,
        captcha_token: str,
    ) -> dict:
        """步骤 2：发送短信验证码

        :return: {"verification_id", "is_user", "expires_in", "selected_channel"}
        """
        手机号 = 手机号.strip()
        headers = self._账户请求头()
        headers["x-captcha-token"] = captcha_token

        logger.info(f"[短信登录] 发送短信 phone={手机号[:6]}***")
        响应 = self.客户端.post(
            "/v1/auth/verification",
            headers=headers,
            json={
                "phone_number": 手机号,
                "target": "ANY",
                "client_id": 客户端ID,
            },
        )

        if 响应.status_code != 200:
            # 尝试提取服务端错误信息
            try:
                错误信息 = 响应.json()
            except Exception:
                错误信息 = 响应.text[:300]
            raise RuntimeError(
                f"发送验证码失败 HTTP {响应.status_code}: {错误信息}")

        数据 = 响应.json()
        if "verification_id" not in 数据:
            raise RuntimeError(f"发送验证码响应异常：{数据}")

        logger.info(
            f"[短信登录] 验证码已发送 "
            f"is_user={数据.get('is_user')} "
            f"channel={数据.get('selected_channel')} "
            f"expires_in={数据.get('expires_in', '?')}s")
        return 数据

    def 短信登录_校验验证码(
        self,
        verification_id: str,
        验证码: str,
    ) -> dict:
        """步骤 3：校验短信验证码

        :return: {"verification_token", "expires_in"}
        """
        logger.info("[短信登录] 校验验证码")
        响应 = self.客户端.post(
            "/v1/auth/verification/verify",
            headers=self._账户请求头(),
            json={
                "verification_id": verification_id,
                "verification_code": 验证码,
                "client_id": 客户端ID,
            },
        )

        if 响应.status_code != 200:
            try:
                错误信息 = 响应.json()
            except Exception:
                错误信息 = 响应.text[:300]
            raise RuntimeError(
                f"验证码校验失败 HTTP {响应.status_code}: {错误信息}")

        数据 = 响应.json()
        if "verification_token" not in 数据:
            raise RuntimeError(f"验证码校验响应异常：{数据}")

        logger.info(
            f"[短信登录] 验证码校验通过 "
            f"expires_in={数据.get('expires_in', '?')}s")
        return 数据

    def 短信登录_登录(
        self,
        手机号: str,
        验证码: str,
        verification_token: str,
    ) -> dict:
        """步骤 4：登录换取 access_token / refresh_token"""
        手机号 = 手机号.strip()
        logger.info(f"[短信登录] 登录 phone={手机号[:6]}***")

        响应 = self.客户端.post(
            "/v1/auth/signin",
            headers=self._账户请求头(),
            json={
                "verification_code": 验证码,
                "verification_token": verification_token,
                "username": 手机号,
                "client_id": 客户端ID,
            },
        )

        if 响应.status_code != 200:
            try:
                错误信息 = 响应.json()
            except Exception:
                错误信息 = 响应.text[:300]
            raise RuntimeError(
                f"登录失败 HTTP {响应.status_code}: {错误信息}")

        令牌数据 = 响应.json()
        if "access_token" not in 令牌数据:
            raise RuntimeError(f"登录响应异常，无 access_token：{令牌数据}")

        self._保存令牌到仓库(令牌数据)
        logger.info(
            f"[短信登录] ✅ 登录成功 sub={令牌数据.get('sub')}")
        return 令牌数据

    # ==================== 令牌保存 ====================

    def _保存令牌到仓库(self, 令牌数据: dict) -> None:
        """统一把登录响应里的令牌保存到全局令牌仓库"""
        access_token = 令牌数据.get("access_token")
        if not access_token:
            raise RuntimeError(f"登录响应缺少 access_token：{令牌数据}")

        有效期 = int(令牌数据.get("expires_in", 7200))
        刷新令牌 = 令牌数据.get("refresh_token", "")
        用户标识 = 令牌数据.get("sub", "")

        全局令牌仓库.保存令牌(
            access_token, 有效期,
            刷新令牌=刷新令牌, 用户标识=用户标识,
        )
        logger.info(
            f"[登录] 令牌已保存：有效期 {有效期}s，"
            f"有刷新令牌={bool(刷新令牌)}，"
            f"有用户标识={bool(用户标识)}")

    # ==================== 收尾 ====================

    def 关闭(self):
        try:
            self.客户端.close()
        except Exception:
            pass