# 夸克网盘适配器/核心/下载/下载服务.py
"""
下载服务（阶段二 2.6 / 阶段三修复）

    ① POST file/download {"fids":[...]}  → data[].download_url（直链，含签名）
    ② 用直链做流式 GET，边下边写盘

⚠️ 下载直链的**防盗链回调**（`RequestDeniedByCallback`）会校验：
    1. 请求必须带 Cookie（直链里内嵌的 callback 模板会转发 `${httpHeader.cookie}`）
    2. Cookie 里的 **`__puus`** 字段是强制的 —— 缺它就返回
       403 `require login [auth miss]`（浏览器登录天然带，CAS 扫码不带）

    因此本模块做两层保障：
      · 请求前：若 Cookie 缺 `__puus`，先 `GET member` 补一次
      · 请求后：若仍被拒（`RequestDeniedByCallback`），**强制刷新 `__puus` 重试 1 次**
        （`__puus` 是滚动下发的，旧值可能失效）

其他要点：
  - 必须 `follow_redirects=True`（直链可能 302 到 CDN）。
  - **同名去重**：目标已存在时自动改名为 `名字 (1).ext`，不静默覆盖。
  - 失败要删掉半成品 `.part` 文件，不留垃圾。
"""
import logging
from pathlib import Path
from typing import Any, Callable, Optional

import httpx

from ..网络.网络客户端 import 网络客户端, 接口错误
from ..接口.文件接口 import 文件接口

logger = logging.getLogger("夸克网盘.下载")

默认块大小 = 64 * 1024
下载UA = ("Mozilla/5.0 (Windows NT 10.0; WOW64) AppleWebKit/537.36 "
         "(KHTML, like Gecko) Chrome/94.0.4606.71 Safari/537.36 "
         "Core/1.94.225.400 QQBrowser/12.2.5544.400")

# 触发 __puus 下发的端点（实测：member 与 config 都会 Set-Cookie: __puus）
补凭证路径 = "member"


def 去重路径(目录: Path, 文件名: str) -> Path:
    """若目标已存在，则在扩展名前插入 ` (n)` 直到不冲突

    例：`报告.pdf` → `报告 (1).pdf` → `报告 (2).pdf`
    """
    目录 = Path(目录)
    目标 = 目录 / 文件名
    if not 目标.exists():
        return 目标

    原 = Path(文件名)
    主干, 后缀 = 原.stem, 原.suffix
    for i in range(1, 10000):
        候选 = 目录 / f"{主干} ({i}){后缀}"
        if not 候选.exists():
            return 候选
    raise RuntimeError(f"同名文件过多，无法去重：{文件名}")


class 下载服务:
    """夸克下载：取直链 + 流式落盘（含 __puus 补全与 403 重试）"""

    def __init__(self, 网络: 网络客户端):
        self.网络 = 网络
        self.文件 = 文件接口(网络)

    # ==================== 鉴权补全 ====================

    def _刷新下载凭证(self, 强制: bool = False) -> bool:
        """向服务端要一次新的 `__puus`

        :param 强制: True 时无论本地是否已有都重新请求（用于 403 后重试）
        :return: 刷新后 Cookie 里是否有 `__puus`
        """
        cookie = getattr(self.网络, "Cookie", "") or ""
        if not 强制 and "__puus=" in cookie:
            return True

        logger.info(
            f"[下载] {'强制刷新' if 强制 else '补全'} __puus（GET {补凭证路径}）")
        try:
            self.网络.请求("GET", 补凭证路径)
        except Exception as e:
            logger.warning(f"[下载] 刷新 __puus 失败：{e}")
            return False

        有了 = "__puus=" in (getattr(self.网络, "Cookie", "") or "")
        logger.info(f"[下载] __puus {'已就绪 ✔' if 有了 else '仍缺失 ✘'}")
        return 有了

    def _确保下载凭证(self) -> None:
        """请求前保障：Cookie 缺 `__puus` 时补一次"""
        cookie = getattr(self.网络, "Cookie", "") or ""
        if "__puus=" in cookie:
            return
        self._刷新下载凭证(强制=False)

    @staticmethod
    def _是回调拒绝(异常) -> bool:
        """判断是否属于「CDN 防盗链回调拒绝」（可通过刷新 __puus 重试）"""
        文本 = str(异常)
        return ("RequestDeniedByCallback" in 文本
                or "auth miss" in 文本 or "auth not found" in 文本)

    def _下载请求头(self) -> dict:
        """下载直链所需的请求头（防盗链回调会读 cookie / referer）"""
        头 = {
            "user-agent": 下载UA,
            "referer": "https://pan.quark.cn/",
            "accept": "*/*",
        }
        cookie = getattr(self.网络, "Cookie", "") or ""
        if cookie:
            头["cookie"] = cookie
        return 头

    @staticmethod
    def _下载错误(响应) -> 接口错误:
        文本 = (响应.text or "")[:300]
        if "RequestDeniedByCallback" in 文本 and "auth miss" in 文本:
            return 接口错误(
                "下载被 CDN 拒绝（403 RequestDeniedByCallback / auth miss）："
                "Cookie 缺少或已失效的 __puus。"
                f"原始响应：{文本}")
        if "RequestDeniedByCallback" in 文本:
            return 接口错误(
                f"下载被 CDN 防盗链拒绝（403）：{文本}")
        return 接口错误(f"下载失败：HTTP {响应.status_code} {文本}")

    @staticmethod
    def _清理半成品(临时: Path, 目标: Path) -> None:
        for p in (临时,):
            try:
                if p.exists():
                    p.unlink()
            except Exception:
                pass
        try:
            if 目标.exists() and 目标.stat().st_size == 0:
                目标.unlink(missing_ok=True)
        except Exception:
            pass

    # ==================== 直链 ====================

    def 取下载地址(self, fileId: str) -> str:
        """取单个文件的直链 URL"""
        self._确保下载凭证()
        信息 = self.文件.取下载地址(fileId)
        return str(信息.get("download_url") or "")

    def 取下载信息(self, fileId: str) -> dict:
        """取单个文件的完整下载信息（含真实文件名与大小）"""
        self._确保下载凭证()
        return self.文件.取下载地址(fileId)

    # ==================== 落盘 ====================

    def _流式落盘(self, 直链: str, 临时: Path,
                  进度回调: Optional[Callable[[int, int], None]],
                  超时秒: float, 总大小: int) -> int:
        """把直链内容流式写入 `临时`，返回写入字节数"""
        超时 = httpx.Timeout(connect=30.0, read=超时秒,
                             write=30.0, pool=30.0)
        已下载 = 0
        with httpx.Client(timeout=超时, http2=False,
                          follow_redirects=True,
                          headers=self._下载请求头()) as 客户端:
            with 客户端.stream("GET", 直链) as 响应:
                if 响应.status_code != 200:
                    响应.read()
                    raise self._下载错误(响应)
                头长度 = 响应.headers.get("content-length")
                if 头长度 and str(头长度).isdigit():
                    总大小 = int(头长度)
                with 临时.open("wb") as f:
                    for 块 in 响应.iter_bytes(chunk_size=默认块大小):
                        f.write(块)
                        已下载 += len(块)
                        if 进度回调:
                            try:
                                进度回调(已下载, 总大小)
                            except Exception:
                                pass
        return 已下载

    def 下载到文件(
        self,
        fileId: str,
        保存目录: str | Path,
        文件名: Optional[str] = None,
        进度回调: Optional[Callable[[int, int], None]] = None,
        覆盖: bool = False,
        超时秒: float = 3600.0,
    ) -> dict:
        """下载单个文件到本地

        :param 文件名: 不传则用服务端返回的 file_name
        :param 进度回调: `回调(已下载字节, 总字节)`
        :param 覆盖: False（默认）时同名自动改名，True 时直接覆盖
        :return: {"path","size","file_name","fid"}
        """
        信息 = self.取下载信息(fileId)
        直链 = str(信息.get("download_url") or "")
        if not 直链:
            raise 接口错误(f"未取到下载直链：{信息}")

        名称 = 文件名 or str(信息.get("file_name") or f"{fileId}.bin")
        目录 = Path(保存目录)
        目录.mkdir(parents=True, exist_ok=True)

        if 覆盖:
            目标 = 目录 / 名称
        else:
            目标 = 去重路径(目录, 名称)
            if 目标.name != 名称:
                logger.info(f"[下载] 同名已存在，改存为 {目标.name}")

        总大小 = int(信息.get("size") or 0)
        logger.info(f"[下载] {名称} → {目标}（{总大小} 字节）")

        临时 = 目标.with_name(目标.name + ".part")
        已下载 = 0
        # 最多 2 次：首次若被 CDN 回调拒绝，强制刷新 __puus 后再试一次
        for 尝试 in (1, 2):
            try:
                已下载 = self._流式落盘(直链, 临时, 进度回调, 超时秒, 总大小)
                break
            except Exception as e:
                self._清理半成品(临时, 目标)
                if 尝试 == 1 and self._是回调拒绝(e):
                    logger.warning(
                        f"[下载] 被 CDN 拒绝，刷新 __puus 后重试一次：{e}")
                    self._刷新下载凭证(强制=True)
                    continue
                raise 接口错误(
                    f"下载失败：{type(e).__name__}: {e}") from e

        临时.replace(目标)
        logger.info(f"[下载] 完成 {目标}（{已下载} 字节）")
        return {
            "path": str(目标),
            "size": 已下载,
            "file_name": 目标.name,
            "fid": str(信息.get("fid") or fileId),
        }

    def 下载多个(
        self,
        fileIds: list[str],
        保存目录: str | Path,
        进度回调: Optional[Callable[[int, int, str], None]] = None,
        覆盖: bool = False,
    ) -> list[dict]:
        """批量下载

        :param 进度回调: `回调(第几个, 总数, 文件名)`
        """
        结果: list[dict] = []
        总数 = len(fileIds)
        for i, fid in enumerate(fileIds):
            信息 = self.文件.取下载地址(fid)
            名称 = str(信息.get("file_name") or "")
            if 进度回调:
                try:
                    进度回调(i + 1, 总数, 名称)
                except Exception:
                    pass
            结果.append(self.下载到文件(fid, 保存目录, 覆盖=覆盖))
        return 结果

    def 下载到内存(self, fileId: str) -> bytes:
        """把文件读进内存（小文件/校验用）"""
        self._确保下载凭证()
        信息 = self.文件.取下载地址(fileId)
        直链 = str(信息.get("download_url") or "")
        if not 直链:
            raise 接口错误(f"未取到下载直链：{信息}")

        for 尝试 in (1, 2):
            with httpx.Client(timeout=120.0, http2=False,
                              follow_redirects=True,
                              headers=self._下载请求头()) as 客户端:
                响应 = 客户端.get(直链)
            if 响应.status_code == 200:
                return 响应.content
            错误 = self._下载错误(响应)
            if 尝试 == 1 and self._是回调拒绝(错误):
                logger.warning(f"[下载] 被 CDN 拒绝，刷新 __puus 后重试一次：{错误}")
                self._刷新下载凭证(强制=True)
                continue
            raise 错误
        raise 接口错误("下载失败：重试后仍被拒绝")
