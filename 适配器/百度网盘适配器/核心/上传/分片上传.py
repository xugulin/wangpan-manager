# 百度网盘适配器/核心/上传/分片上传.py
"""
分片上传
作用：解析上传通道域名 + 逐片调用 `superfile2`

⚠️ 上传域名是**动态的**（PLAN.md §5.2.2 实测确认）
------------------------------------------------
同账号同机 1 小时内，上传域名从 `bddwd-cm01.pcs.baidu.com` 变成了
`bdbl-cm01.pcs.baidu.com`。**绝不可硬编码**。

来源已确证为 `locateupload` 响应里的 **`server[0]`**，上传器 JS 逐字印证：

    _locateUpload = function(e) {
        Sc.get(locateUploadUrl, function(n) {
            null != n.server && (Object.assign(conf, {
                serverHost: n.server[0],        // ←★
                serverUrlList: n.server
            }), conf.serverUrlList.push(conf.defaultServerHost),
               _setServerUrl(e), n.timestamp = +new Date,
               localStorage.setItem(key, JSON.stringify(n)));   // 缓存
        }, "json")
    }
    // 缓存有效期：+new Date - n.timestamp < 6e4  （60 秒，与响应 expire:60 一致）

分片规则（两次独立实测确认）
----------------------------
分片大小 = **4 MB**（4194304）。9 MB 文件 → 3 片 = 4194304 / 4194304 / 1048576。

`superfile2` 响应里的 `md5` 是**纯净 MD5**（非展示态加密值），
`create` 的 `block_list` 必须用这个返回值。
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

import httpx

from ..网络.网络客户端 import 网络客户端, 接口错误
from ..网络.dp_logid import 生成logid, DpLogId生成器
from .哈希计算器 import 分片大小

logger = logging.getLogger("百度网盘.上传.分片")

# locateupload 接入点（实测不需要认证：不带 Cookie 也返回 error_code:0）
接入点域名 = "https://d.pcs.baidu.com"
接入点路径 = "/rest/2.0/pcs/file"
# 缓存有效期：与响应里的 expire 一致
缓存秒 = 60.0
# 兜底域名（仅当 locateupload 完全不可用时使用；正常情况下绝不该走到这里）
兜底域名 = "bddwd-cm01.pcs.baidu.com"

上传UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")


class 上传域名解析器:
    """通过 `locateupload` 动态获取上传通道域名，带 60s 缓存与失败切换。"""

    def __init__(self, 网络: 网络客户端, 缓存有效期: float = 缓存秒):
        self.网络 = 网络
        self.缓存有效期 = 缓存有效期
        self._域名列表: list[str] = []
        self._当前索引 = 0
        self._更新时间 = 0.0
        self._锁 = threading.Lock()

    # ---------------- 内部 ----------------

    def _刷新(self) -> list[str]:
        """调 locateupload 拿 server 列表（不需要认证）。"""
        try:
            响应 = self.网络.请求(
                "GET", 接入点路径,
                基础地址=接入点域名,
                params={"method": "locateupload"},
                带业务头=False,
                需要认证=False,
                raw=True,
            )
            数据 = 响应.json()
        except Exception as e:
            logger.warning(f"[上传域名] locateupload 失败：{type(e).__name__}: {e}")
            return []

        if 数据.get("error_code") not in (0, None):
            logger.warning(f"[上传域名] locateupload 返回 error_code="
                           f"{数据.get('error_code')} {数据.get('error_msg')}")
            return []

        服务器 = [s for s in (数据.get("server") or []) if s]
        logger.info(f"[上传域名] locateupload → server={服务器} "
                    f"host={数据.get('host')} prov={数据.get('prov')} "
                    f"isp={数据.get('isp')}")
        # ⚠️ 用 server[0] 起头；JS 里还会把 defaultServerHost 追加到列表尾部
        结果 = list(服务器)
        默认 = 数据.get("host")
        if 默认 and 默认 not in 结果:
            结果.append(默认)
        return 结果

    # ---------------- 对外 ----------------

    def 取域名(self) -> str:
        """返回当前可用上传域名（必要时刷新缓存）。"""
        with self._锁:
            过期 = (time.monotonic() - self._更新时间) > self.缓存有效期
            if 过期 or not self._域名列表:
                新列表 = self._刷新()
                if 新列表:
                    self._域名列表 = 新列表
                    self._当前索引 = 0
                    self._更新时间 = time.monotonic()
                elif not self._域名列表:
                    logger.warning(f"[上传域名] 解析失败，临时兜底 {兜底域名}")
                    self._域名列表 = [兜底域名]
                    self._更新时间 = time.monotonic()
            域名 = self._域名列表[min(self._当前索引, len(self._域名列表) - 1)]
        logger.debug(f"[上传域名] 使用 {域名}（候选 {len(self._域名列表)} 个）")
        return 域名

    def 切换下一个(self) -> str | None:
        """当前域名失败时切到下一个；已无候选则重新解析。返回新域名或 None。"""
        with self._锁:
            if self._当前索引 + 1 < len(self._域名列表):
                self._当前索引 += 1
                新 = self._域名列表[self._当前索引]
                logger.warning(f"[上传域名] 切换到备用域名 {新}")
                return 新
            # 候选耗尽 → 强制刷新一次
            self._更新时间 = 0.0
        return self.取域名()

    def 候选列表(self) -> list[str]:
        with self._锁:
            return list(self._域名列表)


class 分片上传器:
    """`superfile2` 分片上传。"""

    def __init__(self, 网络: 网络客户端,
                 域名解析器: 上传域名解析器 | None = None):
        self.网络 = 网络
        self.域名解析器 = 域名解析器 or 上传域名解析器(网络)

    # ---------------- 单片 ----------------

    def 上传分片(
        self,
        路径: str,
        uploadid: str,
        片号: int,
        数据: bytes,
        超时秒: float = 300.0,
    ) -> dict:
        """上传单个分片，返回 superfile2 的响应 dict（含**纯净** `md5`）。"""
        参数 = [
            ("method", "upload"),
            ("logid", 生成logid()),
            ("app_id", "250528"),
            ("channel", "chunlei"),
            ("web", "1"),
            ("clienttype", "0"),
            ("path", 路径),
            ("uploadid", uploadid),
            ("uploadsign", "0"),          # 实测恒为 "0"
            ("partseq", str(片号)),
            ("dp-logid", DpLogId生成器().下一个()),
        ]
        请求头 = {
            "User-Agent": 上传UA,
            "Referer": "https://pan.baidu.com/disk/main",
            "Origin": "https://pan.baidu.com",
            "Accept": "*/*",
        }
        cookie = self.网络.构造cookie头()
        if cookie:
            请求头["Cookie"] = cookie

        最后异常: Exception | None = None
        for 尝试 in range(3):
            域名 = self.域名解析器.取域名()
            try:
                with httpx.Client(timeout=超时秒) as c:
                    响应 = c.post(
                        f"https://{域名}/rest/2.0/pcs/superfile2",
                        params=参数, headers=请求头,
                        files={"file": (f"part{片号}", 数据,
                                        "application/octet-stream")},
                    )
                if 响应.status_code != 200:
                    raise 接口错误(
                        f"superfile2 返回 HTTP {响应.status_code}：{响应.text[:150]}",
                        状态码=响应.status_code)
                try:
                    结果 = 响应.json()
                except Exception:
                    raise 接口错误(
                        f"superfile2 响应不是 JSON：{响应.text[:150]}",
                        状态码=响应.status_code)
                if not 结果.get("md5"):
                    raise 接口错误(f"superfile2 未返回 md5：{结果}")
                logger.debug(
                    f"[分片] partseq={片号} {len(数据)}B → md5={结果['md5']}")
                return 结果
            except Exception as e:
                最后异常 = e
                logger.warning(
                    f"[分片] partseq={片号} 第 {尝试+1}/3 次失败"
                    f"（{域名}）：{type(e).__name__}: {str(e)[:120]}")
                if 尝试 < 2:
                    self.域名解析器.切换下一个()
                    time.sleep(0.5 * (尝试 + 1))

        raise 接口错误(
            f"分片 {片号} 上传失败（已重试 3 次）：{最后异常}")

    # ---------------- 整文件 ----------------

    def 上传全部分片(
        self,
        本地文件路径: str | Path,
        目标路径: str,
        uploadid: str,
        每片字节: int = 分片大小,
        进度回调=None,
    ) -> list[str]:
        """逐片上传整个文件。

        :param 进度回调: `回调(已上传字节, 总字节, 当前片号)`
        :return: **服务端返回的纯净 md5 列表**，顺序与 partseq 一致
                  （`create` 的 `block_list` 必须用它，不能用本地算的）
        """
        路径 = Path(本地文件路径)
        总大小 = 路径.stat().st_size
        片数 = max(1, (总大小 + 每片字节 - 1) // 每片字节)
        服务端md5列表: list[str] = []
        已上传 = 0

        logger.info(f"[分片] 开始上传 {路径.name}：{总大小} 字节 / {片数} 片 "
                    f"（每片 {每片字节} 字节）")

        with 路径.open("rb") as f:
            for 片号 in range(片数):
                f.seek(片号 * 每片字节)
                数据 = f.read(每片字节)
                结果 = self.上传分片(目标路径, uploadid, 片号, 数据)
                服务端md5列表.append(结果["md5"])
                已上传 += len(数据)
                if 进度回调:
                    进度回调(已上传, 总大小, 片号)

        logger.info(f"[分片] ✅ 全部 {片数} 片上传完成：{路径.name}")
        return 服务端md5列表
