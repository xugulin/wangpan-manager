# 光鸭云盘适配器/核心/网络/网络客户端.py
"""
网络客户端
作用：统一封装 httpx，自动注入公共头、统一错误处理、连接层重试

真实抓包必需请求头：authorization、did、dt、smid、traceparent
注意：已关闭 HTTP/2（Python 3.14 上 hpack 会段错误）

【连接层重试 - 2026-09-13】
  account.guangyapan.com 偶发 ConnectTimeout（DNS 抖动 / 网络闪断），
  导致「用户信息」等接口间歇性失败。修复方式：
    - 对 ConnectError / ConnectTimeout 做指数退避重试（0.5s → 1s → 2s）
    - 只重试「连接未建立成功」的错误；ReadTimeout / WriteTimeout
      不做重试（请求可能已被服务端接收处理，重试有重复操作风险）
    - 连接池 keepalive_expiry 由 5s 提升至 60s，减少重复建连
"""
import logging
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote, urlsplit, urlunsplit

import httpx

from .追踪上下文 import 生成追踪头
from .设备身份 import 获取设备ID, 获取设备指纹, 获取设备类型

logger = logging.getLogger("光鸭云盘.网络")

接口基础地址 = "https://api.guangyapan.com"
账号基础地址 = "https://account.guangyapan.com"
默认客户端标识 = "aMe-8VSlkrbQXpUR"

# 默认允许的业务码：
#   None/0   普通成功
#   147      文件上传中（轮询接口常见）
#   156      秒传命中（get_res_center_token 专用）
#   200      HTTP 语义化成功
默认允许业务码 = (None, 0, 147, 156, 200)

# ---- 连接层重试参数 ----
默认最多重试 = 3       # 首次之外额外尝试次数（共 4 次尝试）
默认初始延迟 = 0.5     # 秒
默认延迟倍增 = 2.0

# 只对「连接未建立成功」的错误重试
# httpx 的类型层级：
#   TransportError
#     ├── ConnectError       ← 要重试
#     ├── TimeoutException
#     │     ├── ConnectTimeout   ← 要重试
#     │     ├── ReadTimeout      ← 不重试
#     │     ├── WriteTimeout     ← 不重试
#     │     └── PoolTimeout      ← 不重试
#     ├── ReadError          ← 不重试
#     ├── WriteError         ← 不重试
#     └── RemoteProtocolError← 不重试
_连接层错误 = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
)


def _安全URL(地址: str) -> str:
    """将 URL 中的非 ASCII 字符（如中文路径）进行百分号编码。

    仅对 path/query/fragment 做编码，不动 scheme/netloc。
    避免 httpx 在拼装请求时因非 ASCII 抛 UnicodeEncodeError。
    """
    try:
        parts = urlsplit(地址)
        try:
            地址.encode("ascii")
            return 地址
        except UnicodeEncodeError:
            pass
        path = quote(parts.path, safe="/%:@&=+$,;~()*!'")
        query = quote(parts.query, safe="=&%:@+,;~()*!'")
        fragment = quote(parts.fragment, safe="=&%:@+,;~()*!'")
        return urlunsplit(
            (parts.scheme, parts.netloc, path, query, fragment))
    except Exception:
        return 地址


def _校验请求头(请求头: dict) -> dict:
    """确保请求头 value 全为 ASCII；非 ASCII 的抛 ValueError。

    HTTP/1.1 与 HTTP/2 的 header value 都必须是 ASCII（RFC 7230）。
    """
    结果 = {}
    for k, v in (请求头 or {}).items():
        if v is None:
            continue
        字符串值 = str(v)
        try:
            字符串值.encode("ascii")
        except UnicodeEncodeError as e:
            raise ValueError(
                f"请求头 {k!r} 的 value 含非 ASCII 字符：{字符串值!r}") from e
        结果[str(k)] = 字符串值
    return 结果


def _执行带重试(操作,
               描述: str,
               最多重试: int = 默认最多重试,
               初始延迟: float = 默认初始延迟,
               倍增: float = 默认延迟倍增):
    """对连接层错误做指数退避重试

    :param 操作: 一个无参可调用，内部执行真正的网络请求
    :param 描述: 日志用描述，如 "GET https://..."
    :param 最多重试: 首次之外额外尝试次数（0 表示不重试）
    :param 初始延迟: 第一次重试前等待秒数
    :param 倍增: 每次重试延迟的倍增系数

    只捕获 `_连接层错误`（ConnectError / ConnectTimeout）：
    这两类错误说明请求根本没有到达服务端，重试是安全的。
    ReadTimeout / WriteTimeout / RemoteProtocolError 直接抛出。
    """
    if 最多重试 <= 0:
        return 操作()

    延迟 = 初始延迟
    最后异常: Exception | None = None

    for 尝试 in range(最多重试 + 1):
        try:
            return 操作()
        except _连接层错误 as e:
            最后异常 = e
            if 尝试 >= 最多重试:
                break
            logger.warning(
                f"[网络] {描述} 连接失败"
                f"（第 {尝试 + 1}/{最多重试} 次重试），"
                f"{延迟:.2f}s 后重试：{type(e).__name__}: {e}")
            try:
                time.sleep(延迟)
            except Exception:
                pass
            延迟 *= 倍增

    assert 最后异常 is not None
    logger.error(
        f"[网络] {描述} 重试 {最多重试} 次后仍失败："
        f"{type(最后异常).__name__}: {最后异常}")
    raise 最后异常


class 接口错误(Exception):
    def __init__(self, 消息: str, 状态码: int | None = None,
                 业务码: int | None = None, 请求ID: str | None = None):
        super().__init__(消息)
        self.消息 = 消息
        self.状态码 = 状态码
        self.业务码 = 业务码
        self.请求ID = 请求ID

    def __repr__(self) -> str:
        return (f"接口错误(消息={self.消息!r}, 状态码={self.状态码}, "
                f"业务码={self.业务码}, 请求ID={self.请求ID!r})")


class 网络客户端:
    def __init__(self, 基础地址: str = 接口基础地址,
                 超时秒: float = 30.0,
                 客户端标识: str = 默认客户端标识):
        self.基础地址 = 基础地址
        self.客户端标识 = 客户端标识
        self.设备ID = 获取设备ID()
        self.设备指纹 = 获取设备指纹()
        self.设备类型 = 获取设备类型()
        # 注意：Python 3.14 上 http2=True 可能导致 hpack 段错误
        self.客户端 = httpx.Client(
            base_url=基础地址,
            timeout=httpx.Timeout(
                connect=10.0, read=3600.0,
                write=3600.0, pool=30.0),
            http2=False,
            # 连接池：keepalive 从默认 5s 提升到 60s，
            # 减少重复 TLS 握手，降低偶发连接抖动。
            limits=httpx.Limits(
                max_connections=100,
                max_keepalive_connections=20,
                keepalive_expiry=60.0,
            ),
            headers={
                "accept": "application/json, text/plain, */*",
                "accept-language": "zh-CN,zh;q=0.8",
                "origin": "https://www.guangyapan.com",
                "referer": "https://www.guangyapan.com/",
                "user-agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/147.0.0.0 Safari/537.36"
                ),
            },
        )
        self.访问令牌: str | None = None

    def 设置访问令牌(self, 令牌: str | None) -> None:
        self.访问令牌 = 令牌
        logger.debug(f"[网络] 设置访问令牌：{令牌[:8] if 令牌 else None}...")

    def _构造公共请求头(
        self,
        带业务头: bool = True,
        content_type: str | None = "application/json",
    ) -> dict:
        请求头: dict = {}
        if content_type:
            请求头["content-type"] = content_type
        if 带业务头:
            请求头.update({
                "did": self.设备ID,
                "dt": self.设备类型,
                "smid": self.设备指纹,
                "traceparent": 生成追踪头(),
            })
        if self.访问令牌:
            请求头["authorization"] = f"Bearer {self.访问令牌}"
        return 请求头

    @staticmethod
    def _检查业务码(数据: Any, 响应: httpx.Response,
                    允许业务码: tuple | list | None) -> None:
        """检查响应业务码，不在白名单则抛接口错误"""
        允许集 = set(默认允许业务码)
        if 允许业务码:
            允许集.update(允许业务码)
        if isinstance(数据, dict) and 数据.get("code") not in 允许集:
            raise 接口错误(
                消息=数据.get("msg", "接口返回异常"),
                状态码=响应.status_code,
                业务码=数据.get("code"),
                请求ID=响应.headers.get("x-request-id"),
            )

    def 请求(self, 方法: str, 路径: str,
             json数据: Any = None,
             params: dict | None = None,
             headers: dict | None = None,
             基础地址: str | None = None,
             带业务头: bool = True,
             需要认证: bool = True,
             raw: bool = False,
             允许业务码: tuple | list | None = None) -> Any:
        """普通 JSON 请求

        :param 允许业务码: 额外允许的业务码（例如 159=目录已存在）
        """
        请求头 = self._构造公共请求头(带业务头)
        if headers:
            请求头.update(headers)
        请求头 = _校验请求头(请求头)

        地址 = (基础地址 or self.基础地址).rstrip("/") + 路径
        地址 = _安全URL(地址)
        logger.debug(f"[网络] {方法} {地址} params={params} json={json数据}")

        响应 = _执行带重试(
            lambda: self.客户端.request(
                方法, 地址, json=json数据,
                params=params, headers=请求头),
            描述=f"{方法} {地址}",
        )

        logger.debug(f"[网络] 响应状态：{响应.status_code}")

        if raw:
            return 响应

        if 响应.status_code == 401 and 需要认证:
            raise 接口错误("未登录或登录已过期", 状态码=401,
                           请求ID=响应.headers.get("x-request-id"))

        响应.raise_for_status()

        try:
            数据 = 响应.json()
        except Exception:
            raise 接口错误(
                f"响应不是合法 JSON: {响应.text[:200]}",
                状态码=响应.status_code,
                请求ID=响应.headers.get("x-request-id"))

        self._检查业务码(数据, 响应, 允许业务码)
        return 数据

    def 表单文件请求(
        self,
        路径: str,
        文件字段名: str,
        文件路径: str | Path,
        额外字段: dict | None = None,
        基础地址: str | None = None,
        带业务头: bool = True,
        允许业务码: tuple | list | None = None,
    ) -> Any:
        """POST multipart/form-data 文件上传

        用于 /cloudcollection/v1/resolve_torrent 等需要上传文件的接口。

        :param 文件字段名: multipart 表单字段名（如 "torrent"）
        :param 文件路径: 本地文件路径
        :param 额外字段: 其它表单字段
        """
        路径对象 = Path(文件路径)
        if not 路径对象.is_file():
            raise FileNotFoundError(f"文件不存在：{文件路径}")

        # 注意：不注入 content-type，httpx 会自动设置 multipart 边界
        请求头 = self._构造公共请求头(带业务头, content_type=None)
        请求头 = _校验请求头(请求头)

        地址 = (基础地址 or self.基础地址).rstrip("/") + 路径
        地址 = _安全URL(地址)

        # 一次性把文件读进内存，保证重试时可以直接重放
        with 路径对象.open("rb") as f:
            文件内容 = f.read()

        文件元组 = (
            路径对象.name,
            文件内容,
            "application/octet-stream",
        )
        files = {文件字段名: 文件元组}
        data = dict(额外字段 or {})

        logger.debug(
            f"[网络] POST(multipart) {地址} "
            f"file={路径对象.name}({len(文件内容)}B) data={data}")

        响应 = _执行带重试(
            lambda: self.客户端.post(
                地址, files=files, data=data, headers=请求头),
            描述=f"POST(multipart) {地址}",
        )

        logger.debug(f"[网络] 响应状态：{响应.status_code}")
        响应.raise_for_status()

        try:
            数据 = 响应.json()
        except Exception:
            raise 接口错误(
                f"响应不是合法 JSON: {响应.text[:200]}",
                状态码=响应.status_code,
                请求ID=响应.headers.get("x-request-id"))

        self._检查业务码(数据, 响应, 允许业务码)
        return 数据

    # 便捷：账号域名请求（不带业务头）
    def 账号请求(self, 方法: str, 路径: str, **kw) -> Any:
        kw.setdefault("基础地址", 账号基础地址)
        kw.setdefault("带业务头", False)
        return self.请求(方法, 路径, **kw)

    # ==================== OSS 直传 ====================

    def 原始PUT(self, 完整地址: str, 数据: bytes,
                请求头: dict | None = None) -> httpx.Response:
        """向 OSS（或任意非 api 域名）直接 PUT

        小文件（≤100MB）场景下，数据已在内存，可安全重试。
        大文件走 流式PUT（不重试）。
        """
        安全地址 = _安全URL(完整地址)
        安全头 = _校验请求头(请求头 or {})
        return _执行带重试(
            lambda: self.客户端.put(
                安全地址, content=数据, headers=安全头),
            描述=f"PUT {安全地址}",
        )

    def 流式PUT(self, 完整地址: str, 数据生成器,
                请求头: dict | None = None,
                总大小: int = 0) -> httpx.Response:
        """流式 PUT 到 OSS（适合大文件）

        生成器是一次性的，无法重放，所以不做重试。
        """
        headers = dict(请求头 or {})
        if 总大小 > 0:
            headers["Content-Length"] = str(总大小)
        headers = _校验请求头(headers)
        安全地址 = _安全URL(完整地址)

        with httpx.Client(
            timeout=httpx.Timeout(
                connect=30.0, read=3600.0,
                write=3600.0, pool=30.0),
            http2=False,
        ) as 客户端:
            响应 = 客户端.put(安全地址, content=数据生成器,
                              headers=headers)
            return 响应

    # 流式 GET（下载）
    def 流式GET(self, 地址: str,
                请求头: dict | None = None) -> httpx.Response | None:
        下载超时 = httpx.Timeout(
            connect=30.0, read=3600.0, write=30.0, pool=30.0)
        try:
            安全地址 = _安全URL(地址)
            安全头 = _校验请求头(请求头 or {})
            请求 = self.客户端.build_request(
                "GET", 安全地址,
                headers=安全头,
                timeout=下载超时,
            )
            响应 = self.客户端.send(请求, stream=True)
            if 响应.status_code not in (200, 206):
                logger.warning(
                    f"[网络] 流式下载失败：HTTP {响应.status_code}")
                响应.close()
                return None
            return 响应
        except Exception as e:
            logger.exception(f"[网络] 流式下载异常：{e}")
            return None

    def 关闭(self) -> None:
        try:
            self.客户端.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.关闭()