# 光鸭云盘适配器/核心/认证/令牌仓库.py
"""
令牌仓库
作用：管理访问令牌 / 刷新令牌的存取与过期判断
默认持久化到项目根目录下的 数据/令牌.json

关键特性：
  - 访问令牌过期前 5 分钟自动用 refresh_token 静默续期
  - 刷新逻辑内聚，不依赖登录服务（登录服务用完即关）
  - 并发刷新有锁保护（双检锁 + 单飞）

光鸭令牌机制：
  - access_token：默认有效期 7200s（2 小时），每次 API 请求带
  - refresh_token：长期有效（数十天或更久），用于换新 access_token
  - 刷新接口：POST https://account.guangyapan.com/v1/auth/token
              body: {"grant_type":"refresh_token",
                     "refresh_token":"...",
                     "client_id":"aMe-8VSlkrbQXpUR"}
"""
import json
import threading
import time
from pathlib import Path
from typing import Callable, Optional

import httpx
import logging

logger = logging.getLogger("光鸭云盘.令牌")

# 认证中心地址与客户端标识（与登录服务保持一致）
认证中心 = "https://account.guangyapan.com"
客户端ID = "aMe-8VSlkrbQXpUR"
客户端版本 = "0.0.1"

# 提前多少秒触发刷新（避免请求在途时恰好过期）
提前刷新余量 = 300  # 5 分钟


def _默认存储文件() -> Path:
    """默认令牌文件路径：项目根目录/数据/令牌.json

    兜底到用户目录 ~/.光鸭云盘/访问令牌.json
    """
    try:
        项目根 = Path(__file__).resolve().parents[2]  # 核心/认证/令牌仓库.py → 上2级
        候选 = 项目根 / "数据" / "令牌.json"
        候选.parent.mkdir(parents=True, exist_ok=True)
        return 候选
    except Exception:
        回退 = Path.home() / ".光鸭云盘" / "访问令牌.json"
        回退.parent.mkdir(parents=True, exist_ok=True)
        return 回退


def _取设备ID() -> str:
    """安全地取设备ID，避免循环依赖"""
    try:
        from ..网络.设备身份 import 获取设备ID
        return 获取设备ID()
    except Exception:
        return ""


def _默认刷新实现(刷新令牌: str) -> Optional[dict]:
    """用 refresh_token 向认证中心换新的 access_token

    返回：
      {"access_token": "...", "refresh_token": "...", "expires_in": 7200, ...}
    失败返回 None。
    """
    if not 刷新令牌:
        return None

    请求头 = {
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
        "x-device-id": _取设备ID(),
        "x-device-model": "chrome%2F147.0.0.0",
        "x-device-name": "PC-Chrome",
        "x-net-work-type": "NONE",
        "x-os-version": "MacIntel",
        "x-platform-version": "1",
        "x-protocol-version": "301",
        "x-provider-name": "NONE",
        "x-sdk-version": "9.0.2",
    }

    try:
        with httpx.Client(
            base_url=认证中心,
            timeout=20.0,
            http2=False,
            headers=请求头,
        ) as 客户端:
            响应 = 客户端.post(
                "/v1/auth/token",
                json={
                    "grant_type": "refresh_token",
                    "refresh_token": 刷新令牌,
                    "client_id": 客户端ID,
                },
            )
            logger.debug(f"[令牌] 刷新响应状态：{响应.status_code}")
            if 响应.status_code != 200:
                logger.warning(
                    f"[令牌] 刷新失败 HTTP {响应.status_code}: "
                    f"{响应.text[:300]}")
                return None
            try:
                数据 = 响应.json()
            except Exception:
                logger.warning(f"[令牌] 刷新响应非 JSON：{响应.text[:200]}")
                return None
            if isinstance(数据, dict) and 数据.get("access_token"):
                return 数据
            logger.warning(f"[令牌] 刷新响应缺少 access_token：{数据}")
            return None
    except Exception as e:
        logger.error(f"[令牌] 刷新请求异常：{e}")
        return None


class 令牌仓库:
    def __init__(self, 文件路径: str | Path | None = None):
        self._文件路径 = Path(文件路径) if 文件路径 else _默认存储文件()
        self._文件路径.parent.mkdir(parents=True, exist_ok=True)

        self._文件时间: float = 0.0
        self._内存令牌: str | None = None
        self._刷新令牌: str = ""
        self._过期时间戳: float = 0.0
        self._用户标识: str = ""

        # 状态锁：保护字段读写
        # 用 RLock 因为 保存令牌 可能被嵌套调用
        self._锁 = threading.RLock()
        # 刷新锁：保证同一时刻只有一个线程在跑刷新
        self._刷新锁 = threading.Lock()

        # 刷新回调：签名 (刷新令牌: str) -> dict | None
        self._刷新回调: Callable[[str], Optional[dict]] = _默认刷新实现

        self._加载()

    @property
    def 文件路径(self) -> Path:
        return self._文件路径

    def 设置刷新回调(
        self,
        回调: Optional[Callable[[str], Optional[dict]]],
    ) -> None:
        """自定义刷新实现（一般用默认即可）"""
        with self._锁:
            self._刷新回调 = 回调 or _默认刷新实现

    def _必要时重载(self) -> None:
        """磁盘令牌比内存新就重读（登录在另一个进程里完成，这里是桥进程）。"""
        try:
            mtime = self._文件路径.stat().st_mtime
        except Exception:      # noqa: BLE001
            return
        if mtime <= self._文件时间:
            return
        with self._锁:
            if mtime > self._文件时间:
                self._加载()
                self._文件时间 = mtime

    def _加载(self) -> None:
        if not self._文件路径.exists():
            return
        try:
            数据 = json.loads(self._文件路径.read_text(encoding="utf-8"))
            self._内存令牌 = 数据.get("访问令牌")
            self._刷新令牌 = 数据.get("刷新令牌", "")
            self._过期时间戳 = float(数据.get("过期时间戳", 0))
            self._用户标识 = 数据.get("用户标识", "")

            剩余秒 = int(self._过期时间戳 - time.time())
            logger.info(
                f"[令牌] 已从 {self._文件路径} 加载令牌 "
                f"(有刷新令牌={bool(self._刷新令牌)}, "
                f"访问令牌剩余={剩余秒}s)")
        except Exception as e:
            logger.warning(f"[令牌] 加载失败：{e}")

    def _保存(self) -> None:
        数据 = {
            "访问令牌": self._内存令牌,
            "刷新令牌": self._刷新令牌,
            "过期时间戳": self._过期时间戳,
            "用户标识": self._用户标识,
            "保存时间": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        try:
            self._文件路径.write_text(
                json.dumps(数据, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"[令牌] 保存失败：{e}")

    def 保存令牌(
        self,
        令牌: str,
        有效期秒: int,
        刷新令牌: str = "",
        用户标识: str = "",
    ) -> None:
        """保存（覆盖）访问令牌

        :param 有效期秒: 服务端返回的 expires_in
        :param 刷新令牌: 若为空则保留原有刷新令牌
        """
        with self._锁:
            self._内存令牌 = 令牌
            self._过期时间戳 = time.time() + max(0, int(有效期秒))
            if 刷新令牌:
                self._刷新令牌 = 刷新令牌
            if 用户标识:
                self._用户标识 = 用户标识
            self._保存()
            logger.info(
                f"[令牌] 已保存到 {self._文件路径}，"
                f"有效期 {有效期秒}s，"
                f"剩余 {max(0, int(self._过期时间戳 - time.time()))}s")

    # ==================== 刷新逻辑 ====================

    def _需要刷新(self) -> bool:
        """判断是否需要提前刷新（不执行刷新本身）"""
        with self._锁:
            if not self._刷新令牌:
                return False
            if not self._内存令牌:
                return True
            return time.time() >= (self._过期时间戳 - 提前刷新余量)

    def _尝试刷新(self) -> bool:
        """用 refresh_token 刷新访问令牌

        双检锁 + 单飞：同一时刻只有一个线程执行网络刷新，
        其他线程等锁后直接复用结果。

        返回 True 表示当前访问令牌可用（刷新成功或无需刷新）。
        """
        with self._刷新锁:
            # 二次检查：可能已被其他线程刷新过
            with self._锁:
                if (
                    self._内存令牌
                    and time.time() < (self._过期时间戳 - 提前刷新余量)
                ):
                    return True
                刷新令牌 = self._刷新令牌
                回调 = self._刷新回调
                用户标识 = self._用户标识

            if not 刷新令牌 or 回调 is None:
                return False

            logger.info("[令牌] 访问令牌即将过期，尝试静默刷新...")
            try:
                结果 = 回调(刷新令牌)
            except Exception as e:
                logger.error(f"[令牌] 刷新异常：{e}")
                结果 = None

            if not 结果 or not 结果.get("access_token"):
                logger.warning(
                    "[令牌] 刷新失败（refresh_token 可能已过期），"
                    "需要重新扫码登录")
                return False

            新访问令牌 = 结果["access_token"]
            新有效期 = int(结果.get("expires_in", 7200))
            新刷新令牌 = 结果.get("refresh_token", "") or 刷新令牌
            新用户标识 = 结果.get("sub", "") or 用户标识

            self.保存令牌(
                令牌=新访问令牌,
                有效期秒=新有效期,
                刷新令牌=新刷新令牌,
                用户标识=新用户标识,
            )
            logger.info(f"[令牌] ✅ 静默刷新成功，新有效期 {新有效期}s")
            return True

    # ==================== 对外获取接口 ====================

    def 获取访问令牌(self, 安全余量: float = 0.0) -> Optional[str]:
        """获取可用的访问令牌。

        若已过期或即将过期，会先用 refresh_token 静默刷新；
        刷新失败返回 None，调用方需引导用户重新登录。
        """
        self._必要时重载()
        # 先判断是否需要刷新（刷新内部会加锁）
        if self._需要刷新():
            self._尝试刷新()

        with self._锁:
            if not self._内存令牌:
                return None
            if time.time() >= (self._过期时间戳 - 安全余量):
                logger.warning("[令牌] 令牌已过期且无法刷新")
                return None
            return self._内存令牌

    def 获取访问令牌忽略过期(self) -> Optional[str]:
        """无论是否过期都返回（仅用于调试/刷新）"""
        with self._锁:
            return self._内存令牌

    def 获取刷新令牌(self) -> str:
        self._必要时重载()
        with self._锁:
            return self._刷新令牌

    def 获取用户标识(self) -> str:
        with self._锁:
            return self._用户标识

    def 是否有效(self, 安全余量: float = 60.0) -> bool:
        self._必要时重载()
        with self._锁:
            return (
                bool(self._内存令牌)
                and time.time() < (self._过期时间戳 - 安全余量)
            )

    def 清空令牌(self) -> None:
        """退出登录：清空内存 + 删除磁盘文件"""
        with self._锁:
            self._内存令牌 = None
            self._刷新令牌 = ""
            self._过期时间戳 = 0
            self._用户标识 = ""
            if self._文件路径.exists():
                try:
                    self._文件路径.unlink()
                    logger.info(f"[令牌] 已删除 {self._文件路径}")
                except Exception as e:
                    logger.warning(f"[令牌] 删除文件失败：{e}")


# 全局单例
全局令牌仓库 = 令牌仓库()