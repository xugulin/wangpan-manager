# 百度网盘适配器/核心/认证/会话仓库.py
"""
会话仓库
作用：持久化百度网盘的登录会话（Cookie 凭证 + CSRF 令牌），供全进程共享

与原骨架「令牌仓库」的根本差异（见 PLAN.md §4.2）
-----------------------------------------------
| 维度     | 原骨架 令牌仓库                  | 百度 会话仓库（本模块）        |
|----------|--------------------------------|--------------------------------|
| 凭证形态 | access_token + refresh_token   | BDUSS(+BFESS) + STOKEN         |
| 续期机制 | refresh_token 静默刷新         | 无刷新概念，失效只能重新登录   |
| CSRF     | 无                             | bdstoken（会变，需定期重取）   |
| 存储字段 | access/refresh/expires/sub     | bduss/bduss_bfess/stoken/...   |

⚠️ 实测结论（PLAN.md §4.6）：**每个业务请求都必须同时携带 BDUSS 与 STOKEN**，
   缺少 STOKEN 时即使 BDUSS 完全有效也返回 `errno:-6`。

安全约定
--------
`数据/` 已在 .gitignore 中排除；BDUSS 等价于账号密码，
**严禁**写入日志、提交或上报。本模块所有日志只输出字段「形态」，不输出明文。
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("百度网盘.认证.会话")

# 数据目录：项目根/数据
_项目根 = Path(__file__).resolve().parents[2]
_默认文件 = _项目根 / "数据" / "会话.json"

# 需要从 Cookie 中提取的字段（名字 → 存储键）
_COOKIE_键映射 = {
    "BDUSS": "bduss",
    "BDUSS_BFESS": "bduss_bfess",
    "STOKEN": "stoken",
    "BAIDUID": "baiduid",
    "BAIDUID_BFESS": "baiduid_bfess",
}

# 导出的 Cookie 头中携带的字段（顺序即发送顺序）
_导出顺序 = ("BDUSS", "BDUSS_BFESS", "STOKEN")


def _形态(值: Any) -> str:
    """值的形态指纹，用于安全日志（不含明文）。"""
    if not 值:
        return "空"
    s = str(值)
    if re.fullmatch(r"[0-9a-fA-F]+", s):
        return f"hex{len(s)}"
    if re.fullmatch(r"[0-9a-zA-Z_\-=+/]+", s):
        return f"b64ish{len(s)}"
    return f"mixed{len(s)}"


def 解析cookie文本(文本: str) -> dict[str, str]:
    """解析裸 Cookie 头 / key=value 文本，返回 {名字: 值}。

    支持分号分隔、换行分隔，以及带 "Cookie:" 前缀的整行粘贴。
    """
    结果: dict[str, str] = {}
    for 段 in re.split(r"[;\n]", 文本 or ""):
        段 = 段.strip()
        if 段.lower().startswith("cookie:"):
            段 = 段[len("cookie:"):].strip()
        if not 段 or "=" not in 段:
            continue
        名, 值 = 段.split("=", 1)
        名 = 名.strip()
        if 名:
            结果[名] = 值.strip()
    return 结果


class 会话仓库:
    """线程安全的会话存储。

    内存中持有当前会话，任何写操作都会立即落盘；
    `取会话()` 每次返回内存中的最新副本——这正是修复原骨架
    「令牌不回流」缺陷的关键（网络客户端每请求实时调用它，而非构造时快照）。
    """

    def __init__(self, 文件路径: str | Path | None = None):
        self._文件 = Path(文件路径) if 文件路径 else _默认文件
        self._锁 = threading.RLock()
        self._数据: dict[str, Any] = {}
        self._加载()

    # ---------------- 基础 ----------------

    @property
    def 文件路径(self) -> Path:
        return self._文件

    def _加载(self) -> None:
        if not self._文件.is_file():
            logger.debug(f"[会话] 无已保存会话：{self._文件}")
            return
        try:
            with self._文件.open(encoding="utf-8") as f:
                self._数据 = json.load(f) or {}
            logger.debug(
                f"[会话] 已加载：bduss={_形态(self._数据.get('bduss'))} "
                f"stoken={_形态(self._数据.get('stoken'))} "
                f"uk={self._数据.get('uk')}")
        except Exception as e:
            logger.warning(f"[会话] 读取失败，按未登录处理：{type(e).__name__}: {e}")
            self._数据 = {}

    def _保存(self) -> None:
        self._文件.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._文件.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(self._数据, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self._文件)
        # 会话文件含账号凭证，收紧权限到 0600
        try:
            os.chmod(self._文件, 0o600)
        except Exception:
            pass

    # ---------------- 读 ----------------

    def 取会话(self) -> dict[str, Any]:
        """返回当前会话的浅拷贝（网络客户端每请求调用）。"""
        with self._锁:
            return dict(self._数据)

    def 取字段(self, 键: str) -> Any:
        with self._锁:
            return self._数据.get(键)

    def 获取bduss(self) -> Optional[str]:
        """返回 BDUSS 明文；未登录返回 None。

        命名为「获取」而非「是否有效」，是为了兼容界面层既有的
        `if 全局会话仓库.获取bduss():` 真值判断写法。
        """
        with self._锁:
            return self._数据.get("bduss") or None

    def 获取stoken(self) -> Optional[str]:
        with self._锁:
            return self._数据.get("stoken") or None

    def 获取bdstoken(self) -> Optional[str]:
        with self._锁:
            return self._数据.get("bdstoken") or None

    def 获取uk(self) -> Optional[int]:
        with self._锁:
            v = self._数据.get("uk")
        try:
            return int(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    def 是否有效(self) -> bool:
        """最小必需集是否齐备（BDUSS 或 BDUSS_BFESS 之一 + STOKEN）。"""
        with self._锁:
            有bduss = bool(self._数据.get("bduss") or self._数据.get("bduss_bfess"))
            有stoken = bool(self._数据.get("stoken"))
        return 有bduss and 有stoken

    def 导出cookie头(self) -> str:
        """拼出可直接放进 `Cookie:` 请求头的字符串。

        实测推荐携带 BDUSS + BDUSS_BFESS + STOKEN 三者
        （两个 BDUSS 变体互为备份，应对 BFE 边缘节点差异）。
        """
        with self._锁:
            对 = []
            for 名 in _导出顺序:
                键 = _COOKIE_键映射.get(名)
                值 = self._数据.get(键) if 键 else None
                if 值:
                    对.append(f"{名}={值}")
            return "; ".join(对)

    # ---------------- 写 ----------------

    def 保存会话(self, **字段: Any) -> None:
        """写入/更新字段并落盘。仅接受白名单键。"""
        允许 = {
            "bduss", "bduss_bfess", "stoken", "bdstoken", "uk",
            "baiduid", "baiduid_bfess", "更新时间",
        }
        with self._锁:
            for k, v in 字段.items():
                if k in 允许 and v is not None:
                    self._数据[k] = v
            self._数据["更新时间"] = int(time.time())
            self._保存()
        logger.info(
            f"[会话] 已保存：bduss={_形态(self._数据.get('bduss'))} "
            f"stoken={_形态(self._数据.get('stoken'))} "
            f"bdstoken={_形态(self._数据.get('bdstoken'))} "
            f"uk={self._数据.get('uk')}")

    def 更新bdstoken(self, bdstoken: str, uk: int | None = None) -> None:
        """仅刷新 CSRF 令牌与用户 ID（由认证服务调用）。"""
        self.保存会话(bdstoken=bdstoken, uk=uk)

    def 从cookie文本导入(self, 文本: str) -> list[str]:
        """手工导入 Cookie 文本（BDUSS 兜底登录路径）。

        :return: 实际识别并保存的 cookie 名列表（便于界面反馈）
        """
        jar = 解析cookie文本(文本)
        字段: dict[str, Any] = {}
        命中: list[str] = []
        for 名, 值 in jar.items():
            键 = _COOKIE_键映射.get(名.upper())
            if 键 and 值:
                字段[键] = 值
                命中.append(名)
        if not 字段:
            raise ValueError(
                "未在文本中找到 BDUSS / BDUSS_BFESS / STOKEN，请检查粘贴内容")
        self.保存会话(**字段)
        logger.info(f"[会话] 已导入 cookie：{命中}")
        return 命中

    def 从cookie头列表提取(self, 头列表) -> list[str]:
        """从**多个** Set-Cookie 头中提取凭证（每个头单独解析）。

        ⚠️ 必须逐个头解析，不能先 join 再按 `[,\\n]` 切分 ——
        `expires=Mon, 13-Sep-27 ...` 里带逗号，join 后再切会把
        **下一个 cookie 的名字吃掉**，导致 BDUSS/STOKEN 被静默丢弃：

            '...domain=.baidu.com, BDUSS=xxx; expires=...'
                                 ↑ 在这里切 → BDUSS 的名字没了

        每个头内部只按 `;` 切属性（属性值里不会出现分号），因此安全。
        """
        字段: dict[str, Any] = {}
        命中: list[str] = []
        for 头 in (头列表 or []):
            if not 头:
                continue
            主体 = str(头).split(";", 1)[0].strip()
            if "=" not in 主体:
                continue
            名, 值 = 主体.split("=", 1)
            键 = _COOKIE_键映射.get(名.strip().upper())
            if 键 and 值.strip():
                字段[键] = 值.strip()
                if 名.strip() not in 命中:
                    命中.append(名.strip())
        if 字段:
            self.保存会话(**字段)
        if not 命中:
            logger.warning(
                f"[会话] 从 {len(list(头列表 or []))} 个 Set-Cookie 头里"
                f"没识别到 BDUSS/STOKEN")
        return 命中

    def 从响应头提取(self, set_cookie头: str) -> list[str]:
        """从 Set-Cookie 响应头文本中提取并保存凭证（兼容旧调用）。

        传单条或多条头都可以；多条时按 `\\n` 切分（调用方若有多条头，
        应优先用 `从cookie头列表提取`，语义更明确）。
        """
        段列表 = re.split(r"[\n]+", set_cookie头 or "")
        return self.从cookie头列表提取(段列表)

    def 清空(self) -> None:
        """退出登录：清空内存与磁盘上的会话。"""
        with self._锁:
            self._数据 = {}
            try:
                if self._文件.is_file():
                    self._文件.unlink()
            except Exception as e:
                logger.warning(f"[会话] 删除会话文件失败：{e}")
        logger.info("[会话] 已清空")


# 全局单例：全进程共享
全局会话仓库 = 会话仓库()
