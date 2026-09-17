# 百度网盘适配器/核心/下载/下载服务.py
"""
下载服务
作用：取直链 → 带 Cookie 流式下载到本地，支持 Range 断点续传

实现路线（R10 的结论，见 PLAN.md §6.6）
--------------------------------------
**不需要破解 `/api/download` 的客户端签名**。改用 `/api/filemetas` 的 `dlink=1`，
服务端会把直链签好返回：

    ① GET /api/filemetas?target=["<路径>"]&dlink=1&text=1
       → { errno:0, info:[{ ..., dlink:"https://d.pcs.baidu.com/file/<md5>?fid=...&sign=...&expires=8h" }] }

    ② GET <dlink>   （自动跟随 302）
       请求头必须带： User-Agent + Referer: https://pan.baidu.com/ + Cookie
       → 302 → bdbl-cm01.baidupcs.com → 200 / 206 字节流

🔴 **dlink 必须带 Cookie**：实测不带 Cookie 会返回
   `HTTP 403 {"error_code":31045,"error_msg":"user not exists"}`，
   且**不会** 302 跳转到 CDN。Cookie 用最小集 `BDUSS + BDUSS_BFESS + STOKEN` 即可。

⚠️ 另一个易踩点：`filemetas` 的响应键是 **`info`**（不是 `list`）。

断点续传
--------
`下载到文件()` 默认 `断点续传=True`：目标文件已存在时，从已有字节数继续，
发 `Range: bytes=<已下载>-`；服务端返回 206 则追加写入，返回 200（不支持 Range）
则退化为从头覆盖。

文件名去重
----------
`下载到目录()` 会在目标文件已存在时自动加 ` (1)` / ` (2)` 后缀，**绝不覆盖**。
（若需覆盖或续传，请用 `下载到文件()` 指定精确路径。）
"""

from __future__ import annotations

import logging
from pathlib import Path

import httpx

from ..网络.网络客户端 import 网络客户端, 接口错误

logger = logging.getLogger("百度网盘.下载")

# 流式下载的读块大小
读块大小 = 64 * 1024

# 下载超时：连接快、读取长（大文件）
下载超时 = httpx.Timeout(connect=30.0, read=3600.0, write=30.0, pool=30.0)

下载UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")


def 去重路径(保存路径: str | Path) -> Path:
    """目标已存在时自动加 ` (1)` / ` (2)` … 后缀，**绝不覆盖**。"""
    路径 = Path(保存路径)
    if not 路径.exists():
        return 路径
    stem, suffix, parent = 路径.stem, 路径.suffix, 路径.parent
    for i in range(1, 1000):
        候选 = parent / f"{stem} ({i}){suffix}"
        if not 候选.exists():
            return 候选
    raise FileExistsError(f"同名文件过多，无法去重：{保存路径}")


def _解析总大小(响应, 已有字节: int) -> int:
    """从 Content-Range / Content-Length 推断文件总大小。"""
    范围 = 响应.headers.get("content-range")       # 形如 "bytes 100-999/1000"
    if 范围 and "/" in 范围:
        try:
            return int(范围.rsplit("/", 1)[1])
        except (ValueError, IndexError):
            pass
    长度 = 响应.headers.get("content-length")
    if 长度:
        try:
            return int(长度) + 已有字节
        except ValueError:
            pass
    return 0


class 下载服务:
    """百度网盘下载服务。"""

    def __init__(self, 网络: 网络客户端):
        self.网络 = 网络

    # ==================== 直链 ====================

    def 取直链(self, 远端路径: str) -> str:
        """GET /api/filemetas?dlink=1 —— 取服务端签好的下载直链。

        :param 远端路径: 网盘里的完整路径，如 `/dir/file.bin`
        :return: dlink URL
        :raises 接口错误: 未返回 dlink
        """
        import json as _json
        数据 = self.网络.请求(
            "GET", "/api/filemetas",
            params={
                "target": _json.dumps([远端路径], ensure_ascii=False,
                                      separators=(",", ":")),
                "dlink": "1",
                "text": "1",
            },
            带渠道=False,
        )
        # ⚠️ 响应键是 info，不是 list
        信息 = 数据.get("info") or []
        if not 信息:
            raise 接口错误(f"filemetas 未返回信息：{远端路径}", 响应数据=数据)
        dlink = 信息[0].get("dlink")
        if not dlink:
            raise 接口错误(
                f"未取得下载直链（该文件可能不支持下载）：{远端路径}",
                响应数据=信息[0])
        logger.debug(f"[下载] 已取直链：{远端路径} → {dlink.split('?')[0]}")
        return str(dlink)

    # ==================== 请求头 ====================

    def _下载请求头(self, 已下载: int = 0) -> dict:
        """dlink 下载必需的请求头。

        ⚠️ 缺 Cookie 会 403 `31045 user not exists`（实测）。
        """
        头 = {
            "User-Agent": 下载UA,
            "Referer": "https://pan.baidu.com/",
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
        cookie = self.网络.构造cookie头()
        if cookie:
            头["Cookie"] = cookie
        if 已下载 > 0:
            头["Range"] = f"bytes={已下载}-"
        return 头

    # ==================== 下载 ====================

    def 下载到文件(
        self,
        远端路径: str,
        保存路径: str | Path,
        进度回调=None,
        断点续传: bool = True,
    ) -> bool:
        """下载到**精确路径**，支持断点续传与进度回调。

        保持与旧接口一致的调用形态：`下载到文件(路径, 保存路径, 进度回调)`。

        :param 远端路径: 网盘里的完整路径
        :param 保存路径: 本地目标路径（父目录会自动创建）
        :param 进度回调: `回调(已下载字节, 总字节)`；总字节未知时传 0
        :param 断点续传: True 且目标文件已存在时，从已有字节数继续
        :return: True = 成功；失败抛异常
        """
        保存 = Path(保存路径)
        保存.parent.mkdir(parents=True, exist_ok=True)

        已有 = 0
        if 断点续传 and 保存.is_file():
            已有 = 保存.stat().st_size

        dlink = self.取直链(远端路径)
        头 = self._下载请求头(已有)
        logger.info(f"[下载] {远端路径} → {保存.name}"
                    f"{f'（续传，已有 {已有} 字节）' if 已有 else ''}")

        写入模式 = "ab" if 已有 else "wb"
        已下载 = 已有

        try:
            with httpx.Client(timeout=下载超时, follow_redirects=True) as c:
                with c.stream("GET", dlink, headers=头) as 响应:
                    if 响应.status_code == 403:
                        正文 = 响应.read()[:200].decode("utf-8", "replace")
                        raise 接口错误(
                            f"直链被拒绝（HTTP 403）：{正文}。"
                            f"通常是因为缺少 Cookie。",
                            状态码=403)
                    响应.raise_for_status()

                    # 服务端不支持 Range（返回 200 而非 206）→ 从头覆盖
                    if 已有 and 响应.status_code == 200:
                        logger.warning("[下载] 服务端不支持断点续传，改为从头下载")
                        写入模式, 已下载 = "wb", 0

                    总大小 = _解析总大小(响应, 已下载)

                    with 保存.open(写入模式) as f:
                        for 块 in 响应.iter_bytes(chunk_size=读块大小):
                            f.write(块)
                            已下载 += len(块)
                            if 进度回调:
                                try:
                                    进度回调(已下载, 总大小)
                                except Exception:
                                    pass
        except httpx.HTTPError as e:
            raise 接口错误(f"下载失败：{type(e).__name__}: {e}") from e

        logger.info(f"[下载] ✅ {保存.name} 共 {已下载} 字节")
        return True

    def 下载到目录(
        self,
        远端路径: str,
        保存目录: str | Path,
        进度回调=None,
        文件名: str | None = None,
    ) -> Path:
        """下载到目录，**文件名自动去重**（已存在则加 ` (1)` 后缀）。

        :return: 实际写入的本地路径
        """
        目录 = Path(保存目录)
        目录.mkdir(parents=True, exist_ok=True)
        名字 = 文件名 or 远端路径.rstrip("/").rsplit("/", 1)[-1] or "download"
        目标 = 去重路径(目录 / 名字)
        if 目标.name != 名字:
            logger.info(f"[下载] 同名文件已存在，改存为 {目标.name}")
        # 目标刚去重过，必然是全新文件 → 不需要续传
        self.下载到文件(远端路径, 目标, 进度回调=进度回调, 断点续传=False)
        return 目标

    # ==================== 元信息 ====================

    def 取文件信息(self, 远端路径: str) -> dict:
        """取单个文件的元信息（含 size / md5 等）。"""
        import json as _json
        数据 = self.网络.请求(
            "GET", "/api/filemetas",
            params={
                "target": _json.dumps([远端路径], ensure_ascii=False,
                                      separators=(",", ":")),
                "dlink": "0",
                "text": "1",
            },
            带渠道=False,
        )
        信息 = 数据.get("info") or []
        if not 信息:
            raise 接口错误(f"filemetas 未返回信息：{远端路径}", 响应数据=数据)
        return 信息[0]
