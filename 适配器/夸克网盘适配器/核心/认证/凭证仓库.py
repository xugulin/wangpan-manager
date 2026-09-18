# 夸克网盘适配器/核心/认证/凭证仓库.py
"""
凭证仓库（阶段二 2.2）

夸克与原适配器的认证模型完全不同：

  ┌────────────┬──────────────────────────────────┬──────────────────────────┐
  │            │ 原适配器                          │ 夸克                      │
  ├────────────┼──────────────────────────────────┼──────────────────────────┤
  │ 凭证形态    │ OAuth access_token               │ Cookie 字符串             │
  │ 续期方式    │ refresh_token 静默续期            │ 无 refresh token，只能重登 │
  │ 关键字段    │ access_token / refresh_token      │ __pus / __kps / __uid     │
  └────────────┴──────────────────────────────────┴──────────────────────────┘

因此本模块只做三件事：
  1. Cookie 字典的读写与持久化
  2. 过期判断（取所有 cookie 中最小 expires；没有则默认 7 天
     —— 对齐 QuarkPan `auth/login.py:62-92`）
  3. 校验是否具备夸克必需字段 `__kps` / `__uid`
     —— 对齐 QuarkPan `auth/simple_login.py:133-150`

⚠️ 刻意规避了参考实现的一个 bug：QuarkPan 的 `login.py` 与 `simple_login.py`
   向同一个 `cookies.json` 写入**两种互不兼容的结构**（`list[dict]` vs `dict`），
   用前者读后者会 TypeError。本模块只使用**一种** schema（见下）。

持久化位置：项目根目录 / 数据 / 凭证.json
（兜底 ~/.夸克网盘/凭证.json）

JSON schema（唯一）：
{
  "schema": 1,
  "cookies": {"__pus": "...", "__kps": "...", "__uid": "..."},
  "cookie_string": "__pus=...; __kps=...; __uid=...",
  "source": "qrcode" | "manual",
  "saved_at": 1789000000,
  "expires_at": 1789604800
}
"""
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("夸克网盘.凭证")

# 夸克必需的 Cookie 字段（对齐 QuarkPan simple_login.py:139）
必备Cookie键 = ("__kps", "__uid")

# 下载直链的 OSS 回调鉴权**额外**要求 `__puus`（2026-09-14 实测）：
#   缺失时 CDN 返回 403 RequestDeniedByCallback / "require login [auth miss]"
# 它不在 CAS 扫码登录的响应里，但登录后调用 `member` 会由服务端下发。
下载必需Cookie键 = ("__puus",)

# 无 expires 信息时的默认有效期（秒）—— 对齐 QuarkPan login.py:74-75
默认有效期秒 = 7 * 24 * 3600

# JSON schema 版本号
_SCHEMA = 1


def _默认存储文件() -> Path:
    """默认凭证文件路径：项目根目录/数据/凭证.json

    兜底到用户目录 ~/.夸克网盘/凭证.json
    """
    try:
        项目根 = Path(__file__).resolve().parents[2]  # 核心/认证/凭证仓库.py → 上2级
        候选 = 项目根 / "数据" / "凭证.json"
        候选.parent.mkdir(parents=True, exist_ok=True)
        return 候选
    except Exception:
        回退 = Path.home() / ".夸克网盘" / "凭证.json"
        回退.parent.mkdir(parents=True, exist_ok=True)
        return 回退


# ==================== Cookie 字符串 <-> 字典 ====================

def 解析Cookie字符串(字符串: str | None) -> dict[str, str]:
    """`a=1; b=2` → {"a": "1", "b": "2"}（同时兼容换行分隔）

    对齐 QuarkPan `login.py:211-224` / `simple_login.py:156-162` 的解析方式，
    但额外做了：丢掉空 key、去掉值两端引号。
    """
    结果: dict[str, str] = {}
    if not 字符串:
        return 结果
    for 片段 in str(字符串).replace("\n", ";").split(";"):
        片段 = 片段.strip()
        if not 片段 or "=" not in 片段:
            continue
        键, 值 = 片段.split("=", 1)
        键 = 键.strip()
        if not 键:
            continue
        结果[键] = 值.strip().strip('"\'')
    return 结果


def 拼接Cookie字符串(字典: dict[str, Any] | None) -> str:
    """{"a": "1", "b": "2"} → `a=1; b=2`"""
    if not 字典:
        return ""
    return "; ".join(f"{k}={v}" for k, v in 字典.items() if k)


def 规范化Cookie(来源: Any) -> dict[str, str]:
    """把 str / dict / list[dict] 统一成 dict[name] = value

    list[dict] 形态来自 httpx 的 cookie jar（元素含 name/value 键）。
    """
    if not 来源:
        return {}
    if isinstance(来源, str):
        return 解析Cookie字符串(来源)
    if isinstance(来源, dict):
        if "name" in 来源 and "value" in 来源:      # 单个 cookie dict
            return {str(来源["name"]): str(来源["value"])}
        return {str(k): str(v) for k, v in 来源.items() if k}
    if isinstance(来源, (list, tuple)):              # cookie jar
        结果: dict[str, str] = {}
        for 项 in 来源:
            if isinstance(项, dict) and 项.get("name"):
                结果[str(项["name"])] = str(项.get("value", ""))
            elif isinstance(项, str) and "=" in 项:
                键, 值 = 项.split("=", 1)
                结果[键.strip()] = 值.strip()
        return 结果
    return {}


def 校验Cookie(cookie: Any) -> tuple[bool, str]:
    """校验 Cookie 是否可用：返回 (是否通过, 原因)

    夸克只要求存在 `__kps` 与 `__uid`（对齐 QuarkPan `simple_login.py:139`）。
    """
    字典 = 规范化Cookie(cookie)
    if not 字典:
        return False, "Cookie 为空"
    缺少 = [k for k in 必备Cookie键 if not 字典.get(k)]
    if 缺少:
        return False, f"缺少必需 Cookie 字段：{'、'.join(缺少)}"
    return True, ""


# ==================== 仓库 ====================

class 凭证仓库:
    """夸克 Cookie 凭证的持久化仓库"""

    def __init__(self, 文件路径: str | Path | None = None):
        self._文件路径 = Path(文件路径) if 文件路径 else _默认存储文件()
        self._锁 = threading.RLock()
        self._数据: dict | None = None
        self._文件时间: float = 0.0
        # 启动即尝试加载一次，便于 GUI 立刻判断登录态
        self._加载()

    # ---------- 基础属性 ----------

    @property
    def 文件路径(self) -> Path:
        return self._文件路径

    def _必要时重载(self) -> None:
        """磁盘凭证比内存新就重读（登录在另一个进程里完成，这里是桥进程）。

        与百度那边同一个坑：仓库只在进程启动时加载一次，于是"明明登录成功了，
        列目录却一直报未登录"，非要重启程序才生效。
        """
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

    def _加载(self) -> dict | None:
        """从磁盘加载（不加锁，调用方保证）"""
        try:
            if not self._文件路径.exists():
                self._数据 = None
                return None
            with self._文件路径.open("r", encoding="utf-8") as f:
                数据 = json.load(f)
            if not isinstance(数据, dict):
                logger.warning("[凭证] 文件内容不是对象，已忽略")
                self._数据 = None
                return None
            self._数据 = 数据
            return 数据
        except Exception as e:
            logger.warning(f"[凭证] 加载失败，按未登录处理：{e}")
            self._数据 = None
            return None

    def _保存(self, 数据: dict) -> None:
        try:
            self._文件路径.parent.mkdir(parents=True, exist_ok=True)
            # 原子写：先写临时文件再替换，避免中断导致凭证文件损坏
            临时 = self._文件路径.with_suffix(".json.tmp")

            # ⚠️ 权限必须是 0600 —— 这个文件里装的是**可直接登录的会话 Cookie**。
            # 之前用普通 open() 写，会遵循 umask（通常 022 → 0644），
            # 同机其他用户就能读到你的网盘凭证（2026-09-14 实测发现权限
            # 从 600 退化成了 644）。这里显式以 0600 创建，
            # 替换后再 chmod 兜底一次。
            描述符 = os.open(
                临时, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(描述符, "w", encoding="utf-8") as f:
                json.dump(数据, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            临时.replace(self._文件路径)
            try:
                os.chmod(self._文件路径, 0o600)
            except OSError as e:
                logger.warning(f"[凭证] 收紧权限失败（不影响功能）：{e}")
            logger.debug("[凭证] 已保存（权限 0600）")
        except Exception as e:
            logger.error(f"[凭证] 保存失败：{e}")
            raise

    # ---------- 读写 ----------

    def 保存Cookie(
        self,
        cookie: Any,
        来源: str = "unknown",
        过期时间: int | None = None,
        合并: bool = True,
    ) -> dict:
        """保存 Cookie（str / dict / list[dict] 均可）

        :param 来源: "qrcode" / "manual" / ...
        :param 过期时间: Unix 秒；不传则 now + 7 天
        :param 合并: True 则与已存 Cookie 合并（补齐 __pus 等附加字段），
                     False 则整体覆盖
        :return: 写入的数据
        """
        新Cookie = 规范化Cookie(cookie)
        if not 新Cookie:
            raise ValueError("Cookie 无效：内容为空")

        with self._锁:
            旧Cookie: dict[str, str] = {}
            if 合并 and isinstance(self._数据, dict):
                旧Cookie = 规范化Cookie(self._数据.get("cookies"))
            合并后 = {**旧Cookie, **新Cookie}
            # 校验的是**合并后**的结果：允许「只补一个附加字段」的部分更新，
            # 但仓库最终状态必须含 __kps / __uid。
            通过, 原因 = 校验Cookie(合并后)
            if not 通过:
                raise ValueError(f"Cookie 无效：{原因}")
            现在 = int(time.time())
            数据 = {
                "schema": _SCHEMA,
                "cookies": 合并后,
                "cookie_string": 拼接Cookie字符串(合并后),
                "source": 来源,
                "saved_at": 现在,
                "expires_at": int(过期时间) if 过期时间 else 现在 + 默认有效期秒,
            }
            self._保存(数据)
            self._数据 = 数据
        logger.info(
            f"[凭证] 已保存 {len(合并后)} 个 Cookie（来源={来源}，"
            f"过期={数据['expires_at']}）")
        return 数据

    def 取Cookie字典(self) -> dict[str, str]:
        with self._锁:
            if self._数据 is None:
                self._加载()
            if not isinstance(self._数据, dict):
                return {}
            return dict(规范化Cookie(self._数据.get("cookies")))

    def 取Cookie(self, 安全余量: float = 0.0) -> str:
        """取可用的 Cookie 字符串；无效或已过期时返回 "" """
        self._必要时重载()
        if not self.是否有效(安全余量):
            return ""
        return 拼接Cookie字符串(self.取Cookie字典())

    def 取Cookie忽略过期(self) -> str:
        """不做过期判断直接返回（用于「再试一次」等场景）"""
        return 拼接Cookie字符串(self.取Cookie字典())

    def 取来源(self) -> str:
        with self._锁:
            if not isinstance(self._数据, dict):
                return ""
            return str(self._数据.get("source") or "")

    def 过期时间(self) -> Optional[int]:
        with self._锁:
            if not isinstance(self._数据, dict):
                return None
            值 = self._数据.get("expires_at")
            try:
                return int(值) if 值 else None
            except (TypeError, ValueError):
                return None

    def 是否有效(self, 安全余量: float = 0.0) -> bool:
        """Cookie 是否可用

        判定顺序（对齐 QuarkPan login.py:79-92）：
          1. 有 expires_at → now + 安全余量 < expires_at
          2. 无 expires_at  → 距离 saved_at 不超过 7 天
        另外必须能通过 `校验Cookie` 的必需字段检查。
        """
        self._必要时重载()
        with self._锁:
            if self._数据 is None:
                self._加载()
            字典 = self.取Cookie字典()
        通过, _ = 校验Cookie(字典)
        if not 通过:
            return False

        现在 = time.time()
        过期 = self.过期时间()
        if 过期 and 过期 > 0:
            return 现在 + 安全余量 < 过期

        with self._锁:
            保存时间 = 0
            if isinstance(self._数据, dict):
                try:
                    保存时间 = int(self._数据.get("saved_at") or 0)
                except (TypeError, ValueError):
                    保存时间 = 0
            if not 保存时间:
                # 兼容手工导入的凭证文件：用户手写的 JSON 不会有 saved_at，
                # 退回文件 mtime，否则会被误判为「无效」而静默拒绝登录。
                try:
                    保存时间 = int(self._文件路径.stat().st_mtime)
                except Exception:
                    保存时间 = 0
        if not 保存时间:
            return False
        return (现在 - 保存时间) < 默认有效期秒

    def 剩余秒(self) -> int:
        """剩余有效秒数；无效返回 0"""
        过期 = self.过期时间()
        if not 过期:
            return 0
        return max(0, int(过期 - time.time()))

    def 清空(self) -> None:
        """删除本地凭证文件并清空内存"""
        with self._锁:
            self._数据 = None
            try:
                if self._文件路径.exists():
                    self._文件路径.unlink()
                    logger.info("[凭证] 已删除本地凭证文件")
            except Exception as e:
                logger.warning(f"[凭证] 删除凭证文件失败：{e}")

    def 重新加载(self) -> None:
        with self._锁:
            self._加载()

    def __repr__(self) -> str:
        with self._锁:
            有效 = self.是否有效()
        return (f"凭证仓库(文件={self._文件路径}, 有效={有效}, "
                f"cookie数={len(self.取Cookie字典())})")


# 全局单例（GUI 与核心共用）
全局凭证仓库 = 凭证仓库()
