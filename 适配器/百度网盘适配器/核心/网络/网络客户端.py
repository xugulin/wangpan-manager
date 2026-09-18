# 百度网盘适配器/核心/网络/网络客户端.py
"""
网络客户端
作用：统一封装 httpx，自动注入 Cookie 会话与公共 query 参数，统一 errno 错误处理

与原骨架版本的根本差异（见 PLAN.md §4）
------------------------------------
| 维度       | 原骨架                          | 百度（本模块）                     |
|------------|-------------------------------|------------------------------------|
| 认证载体   | `Authorization: Bearer <jwt>` | **Cookie：BDUSS + STOKEN**         |
| CSRF       | 无                            | **`bdstoken`**（按需注入 query）   |
| 设备头     | did / dt / smid               | 无（改用 Cookie 内的 BAIDUID）     |
| 公共 query | 无                            | clienttype / app_id / web / dp-logid|
| 错误模型   | code / msg                    | **errno / errmsg**                 |

【关键修复：令牌不回流 - 2026-09-14】
  原骨架的致命缺陷是 `网络客户端.访问令牌` 只是登录时的一次性快照，
  刷新后的新令牌从不回灌，导致会话超时后持续 401。
  本版本改为**注入 `会话提供者` 回调**：每次请求前实时向会话仓库取最新会话，
  因此 bdstoken 一经刷新立刻生效。另配 `会话刷新器`，遇到 `errno:-6`
  时自动补取一次 bdstoken 并重放请求（仅一次，避免死循环）。

【连接层重试 - 沿用原骨架设计】
  对 ConnectError / ConnectTimeout 做指数退避（0.5s → 1s → 2s）。
  只重试「连接未建立成功」的错误；ReadTimeout / WriteTimeout 不重试
  （请求可能已被服务端接收，重试有重复操作风险）。

注意：已关闭 HTTP/2（Python 3.14 上 hpack 会段错误）。
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import quote, urlsplit, urlunsplit

import httpx

from .dp_logid import 下一个dp_logid

logger = logging.getLogger("百度网盘.网络")

# ---------------- 域名 ----------------
# 主业务 API
接口基础地址 = "https://pan.baidu.com"
# 账号/登录域名（百度没有独立 account 域，passport 承担该角色）
账号基础地址 = "https://passport.baidu.com"

# 网盘 Web 版固定 app id（实测所有业务接口均为 250528）
APP_ID = "250528"
# 渠道标识（写操作普遍携带）
默认渠道 = "chunlei"

# ---------------- errno 语义（实测确认，见 PLAN.md） ----------------
成功 = 0
未登录 = -6            # ✅ 实测：缺 STOKEN 或 BDUSS 失效时返回
风控验证 = 132         # ✅ 实测：彻底删除回收站触发短信二次验证
秒传未命中 = 404       # ✅ 实测：rapidupload 未命中，属正常分支
默认允许errno = (成功,)

# ---------------- 连接层重试参数（沿用原骨架） ----------------
默认最多重试 = 3
默认初始延迟 = 0.5
默认延迟倍增 = 2.0

_连接层错误 = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
)

# 读超时单独成类：长轮询接口要靠它区分「本轮无事件」与「真失败」
_读超时错误 = (httpx.ReadTimeout,)

# 浏览器指纹：与抓包环境一致（Brave 151 / Linux x86_64）
默认UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")


# ==========================================================================
# 模块级工具（平台无关，沿用原骨架实现）
# ==========================================================================
def _安全URL(地址: str) -> str:
    """将 URL 中的非 ASCII 字符（如中文路径）百分号编码。

    仅对 path/query/fragment 编码，不动 scheme/netloc。
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
        return urlunsplit((parts.scheme, parts.netloc, path, query, fragment))
    except Exception:
        return 地址


def _校验请求头(请求头: dict) -> dict:
    """确保请求头 value 全为 ASCII（RFC 7230）。"""
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


def _默认会话提供者() -> dict:
    """默认会话来源：全局会话仓库（惰性导入，保持网络层不依赖认证层）。"""
    try:
        from ..认证.会话仓库 import 全局会话仓库
        return 全局会话仓库.取会话()
    except Exception as e:  # 认证层不可用时按未登录处理
        logger.debug(f"[网络] 取会话失败：{type(e).__name__}: {e}")
        return {}


def _执行带重试(操作,
               描述: str,
               最多重试: int = 默认最多重试,
               初始延迟: float = 默认初始延迟,
               倍增: float = 默认延迟倍增):
    """对连接层错误做指数退避重试（只重试「连接未建立」类错误）。"""
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


# ==========================================================================
# 异常
# ==========================================================================
class 接口错误(Exception):
    """百度网盘接口错误。

    :param errno: 业务错误码（0 成功；-6 未登录；132 风控验证；404 秒传未命中）
    :param errmsg: 服务端返回的错误描述
    """

    def __init__(self, 消息: str, 状态码: int | None = None,
                 errno: int | None = None, errmsg: str | None = None,
                 请求ID: str | int | None = None,
                 响应数据: Any = None):
        super().__init__(消息)
        self.消息 = 消息
        self.状态码 = 状态码
        self.errno = errno
        self.errmsg = errmsg
        self.请求ID = 请求ID
        self.响应数据 = 响应数据

    @property
    def 是未登录(self) -> bool:
        return self.errno == 未登录

    @property
    def 是风控验证(self) -> bool:
        """是否被风控拦截、需要人工完成短信/验证码校验。"""
        return self.errno == 风控验证

    def __repr__(self) -> str:
        return (f"接口错误(消息={self.消息!r}, 状态码={self.状态码}, "
                f"errno={self.errno}, 请求ID={self.请求ID!r})")


class 网络读超时(接口错误):
    """读超时（服务端迟迟不返回）。

    对普通接口算失败；但对**长轮询**接口（passport `/channel/unicast`）
    只代表「本轮没有事件」，调用方应继续轮询而不是报错退出。
    调用方用 `except 网络读超时` 精确识别，不必依赖 httpx 类型。
    """


# ==========================================================================
# 网络客户端
# ==========================================================================
#: 会话刷新的**全局**节流/重入状态。
#: 必须是全局的：刷新会话时会新建网络客户端，按实例记状态等于没记 ——
#: 实测就是那样递归下去的（RecursionError，242 条重复日志，主程序被拖崩）。
_刷新节流: dict = {"进行中": None, "上次时间": 0.0, "上次结果": False}

#: 「没有 bdstoken 仍按无 bdstoken 发送」这条提醒的节流状态。
#: 用户现场实测：一次会话里这条 warning 能刷 700+ 条（每个请求一条），
#: 把界面日志和终端都淹了。改成 60 秒最多一条。
_缺令牌提醒 = {"上次时间": 0.0, "次数": 0}


def _提醒缺bdstoken(有刷新器: bool) -> None:
    """缺 bdstoken 的提醒：60 秒最多一条，其余降到 debug。"""
    import time as _time
    现在 = _time.time()
    if 现在 - float(_缺令牌提醒["上次时间"] or 0.0) >= 60.0:
        _缺令牌提醒["上次时间"] = 现在
        _缺令牌提醒["次数"] = int(_缺令牌提醒["次数"]) + 1
        logger.warning(
            "[网络] 没有 bdstoken，仍按无 bdstoken 发送；若服务端要求会回 "
            "errno:-6 并触发自愈重放"
            + (f"（同一分钟内不再重复提醒，累计第 {_缺令牌提醒['次数']} 次）"
               if _缺令牌提醒["次数"] > 1 else ""))
    else:
        logger.debug("[网络] 仍然没有 bdstoken（已提醒过，不再刷屏）")


class 网络客户端:
    """百度网盘 HTTP 客户端。

    :param 会话提供者: 无参可调用，返回会话 dict。**每次请求前调用**，
                       这是修复「令牌不回流」的关键。
    :param 会话刷新器: 无参可调用，返回 bool。遇到 errno:-6 时调用一次，
                       用于补取 bdstoken 后重放请求。
    """

    def __init__(self,
                 基础地址: str = 接口基础地址,
                 连接超时秒: float = 10.0,
                 客户端标识: str = APP_ID,
                 会话提供者: Optional[Callable[[], dict]] = None,
                 会话刷新器: Optional[Callable[[], bool]] = None):
        self.基础地址 = 基础地址
        self.连接超时秒 = float(连接超时秒)
        # 兼容原骨架的属性名（客户端标识 → 百度侧对应 app_id）
        self.客户端标识 = 客户端标识
        self.会话提供者 = 会话提供者 or _默认会话提供者
        self.会话刷新器 = 会话刷新器
        self.默认渠道 = 默认渠道

        self.客户端 = httpx.Client(
            base_url=基础地址,
            timeout=httpx.Timeout(
                # ✅ 连接超时由构造参数控制（原骨架的「超时秒」是死参数）
                connect=self.连接超时秒, read=3600.0,
                write=3600.0, pool=30.0),
            http2=False,
            # ✅ 必须跟随重定向：登录链（qrbdusslogin / login/api/auth /
            #    plantcookie）靠 302 串联下发 Cookie，httpx 默认**不跟随**。
            follow_redirects=True,
            limits=httpx.Limits(
                max_connections=100,
                max_keepalive_connections=20,
                keepalive_expiry=60.0,
            ),
            headers={
                "accept": "application/json, text/plain, */*",
                "accept-language": "zh-CN,zh;q=0.9",
                "origin": "https://pan.baidu.com",
                "referer": "https://pan.baidu.com/disk/main",
                "user-agent": 默认UA,
            },
        )

    # ---------------- 会话 ----------------

    def 设置访问令牌(self, 令牌: str | None) -> None:
        """【兼容垫片】百度模型下凭证由会话仓库统一注入，本方法已无效。

        保留是为了不破坏界面层既有的 `网络.设置访问令牌(...)` 调用。
        """
        logger.debug(
            "[网络] 设置访问令牌() 已被忽略——凭证由会话仓库按请求注入")

    def 取会话(self) -> dict:
        try:
            return self.会话提供者() or {}
        except Exception as e:
            logger.warning(f"[网络] 会话提供者异常：{type(e).__name__}: {e}")
            return {}

    def _构造cookie头(self) -> str:
        """拼 Cookie 头：BDUSS + BDUSS_BFESS + STOKEN（实测推荐组合）。"""
        会话 = self.取会话()
        对 = []
        for 名, 键 in (("BDUSS", "bduss"),
                       ("BDUSS_BFESS", "bduss_bfess"),
                       ("STOKEN", "stoken")):
            值 = 会话.get(键)
            if 值:
                对.append(f"{名}={值}")
        return "; ".join(对)

    def 构造cookie头(self) -> str:
        """公开版本：供需要直连其它域名（如 pcs 上传通道）的模块复用。"""
        return self._构造cookie头()

    # ---------------- 请求构造 ----------------

    def _构造公共请求头(self, 带业务头: bool = True,
                        content_type: str | None = None) -> dict:
        请求头: dict = {}
        if content_type:
            请求头["content-type"] = content_type
        if 带业务头:
            请求头["referer"] = "https://pan.baidu.com/disk/main"
        请求头["user-agent"] = 默认UA
        cookie头 = self._构造cookie头()
        if cookie头:
            请求头["cookie"] = cookie头
        return 请求头

    def _构造公共参数(self, 带业务头: bool, 带渠道: bool,
                      需要bdstoken: bool, params: dict | None) -> dict:
        """注入公共 query：clienttype / app_id / web / dp-logid / channel / bdstoken。

        实测每条业务请求都带（见 PLAN.md §4.4）。
        """
        参数: dict = dict(params or {})
        if 带业务头:
            参数.setdefault("clienttype", "0")
            参数.setdefault("app_id", APP_ID)
            参数.setdefault("web", "1")
            参数.setdefault("dp-logid", 下一个dp_logid())
            if 带渠道:
                参数.setdefault("channel", self.默认渠道)
        if 需要bdstoken:
            bdstoken = self.取会话().get("bdstoken")
            if not bdstoken and self.会话刷新器:
                logger.debug("[网络] 缺少 bdstoken，尝试刷新会话")
                try:
                    self.会话刷新器()
                except Exception as e:
                    logger.warning(f"[网络] 会话刷新失败：{e}")
                bdstoken = self.取会话().get("bdstoken")
            if not bdstoken:
                # ⚠️ 这里**不再直接抛错**。
                #
                # 实测（真机会话）：`/api/list`、`/rest/2.0/xpan/nas?method=uinfo`
                # 这些读接口**不需要 bdstoken 也能成功**；而取 bdstoken 的
                # `/api/gettemplatevariable` 对某些会话会回 errno:-6。
                # 以前缺 bdstoken 就在这里 raise，导致"明明登录成功、也能列目录的会话"
                # 被本地逻辑挡死 —— 界面表现就是一直未登录/列表空白，重启也没用。
                # 现在照发请求，让服务端 errno 说话：真需要 bdstoken 的接口
                # （上传、改名、删除等写操作）会回 errno:-6，网络层已有
                # "刷新会话后重放一次"的自愈路径。
                #
                # 提醒本身做节流：每个请求都打一条 warning 会把日志刷爆
                # （用户现场一次会话 730+ 条）。
                _提醒缺bdstoken(bool(self.会话刷新器))
            else:
                参数["bdstoken"] = bdstoken
        return 参数

    # ---------------- 业务码检查 ----------------

    @staticmethod
    def _检查errno(数据: Any, 响应: httpx.Response,
                   允许errno: tuple | list | None) -> None:
        """检查 errno；不在白名单则抛接口错误。

        ✅ **白名单语义已修正**：`默认允许errno` 恒为 **`(0,)`**（仅成功），
        是一个**不可变的模块级常量**，不会随任何调用累积。
        需要放宽的调用方必须**逐次显式声明** `允许errno=(...)`，
        例如秒传传 `(0, 404)` —— 这样每个调用点的容忍范围都是可审计的。

        （原骨架的缺陷是维护一个全局累加的「业务码并集」，不同平台/不同接口的
        合法码会互相污染，导致本该报错的响应被静默放过。）
        """
        允许集 = set(默认允许errno)
        if 允许errno:
            允许集.update(允许errno)
            logger.debug(f"[网络] 本调用额外允许 errno={sorted(set(允许errno))}")
        if isinstance(数据, dict) and "errno" in 数据:
            errno = 数据.get("errno")
            if errno not in 允许集:
                errmsg = (数据.get("errmsg") or 数据.get("err_msg")
                          or 数据.get("show_msg") or "")
                if errno == 未登录:
                    默认消息 = "未登录或登录已过期（errno:-6）"
                elif errno == 风控验证:
                    默认消息 = "操作被风控拦截，需要人工完成短信验证（errno:132）"
                else:
                    默认消息 = "接口返回异常"
                raise 接口错误(
                    消息=errmsg or 默认消息,
                    状态码=响应.status_code,
                    errno=errno,
                    errmsg=errmsg,
                    请求ID=数据.get("request_id"),
                    响应数据=数据,
                )

    # ---------------- 核心请求 ----------------

    def _执行一次(self, 方法: str, 地址: str, *,
                  请求头: dict,
                  params: dict | None,
                  json数据: Any,
                  表单数据: Any,
                  文件: Any,
                  raw: bool,
                  需要认证: bool,
                  允许errno: tuple | list | None,
                  超时秒: float | None = None):
        def _发一次():
            额外 = {} if 超时秒 is None else {"timeout": 超时秒}
            try:
                return self.客户端.request(
                    方法, 地址, params=params, headers=请求头,
                    json=json数据, data=表单数据, files=文件, **额外)
            except _读超时错误 as e:
                # 包成 网络读超时：长轮询调用方据此继续等待而不是判失败
                raise 网络读超时(
                    f"读超时（服务端未在超时时间内返回）：{方法} {地址}"
                ) from e

        响应 = _执行带重试(_发一次, 描述=f"{方法} {地址}")
        logger.debug(f"[网络] {方法} {地址} → HTTP {响应.status_code}")

        if raw:
            return 响应, None

        # ✅ 非 2xx 一律转成统一的 接口错误（原骨架会抛原生 httpx.HTTPStatusError）
        if not (200 <= 响应.status_code < 300):
            提示 = "未登录或登录已过期" if 响应.status_code == 401 else \
                   f"HTTP {响应.status_code}"
            raise 接口错误(
                f"{提示}：{响应.text[:200]}",
                状态码=响应.status_code,
                请求ID=响应.headers.get("x-request-id"))

        try:
            数据 = 响应.json()
        except Exception:
            # superfile2 等接口 mimeType 为 text/html 但内容仍是 JSON；
            # 这里统一按 JSON 解析，失败才报错。
            raise 接口错误(
                f"响应不是合法 JSON：{响应.text[:200]}",
                状态码=响应.status_code)

        self._检查errno(数据, 响应, 允许errno)
        return 响应, 数据

    def 请求(self, 方法: str, 路径: str,
             json数据: Any = None,
            表单数据: Any = None,
            params: dict | None = None,
            headers: dict | None = None,
            基础地址: str | None = None,
            带业务头: bool = True,
            带渠道: bool = True,
            需要认证: bool = True,
            需要bdstoken: bool = False,
            raw: bool = False,
            允许errno: tuple | list | None = None,
            超时秒: float | None = None) -> Any:
        """普通请求（JSON / form-urlencoded）。

        :param 带业务头: 是否注入公共 query 与网盘 Referer
        :param 带渠道:   是否注入 `channel=chunlei`
        :param 需要bdstoken: 写操作置 True，自动从会话取 CSRF 令牌
        :param raw:      返回 httpx.Response 而非解析后的 JSON
        :param 允许errno: 额外允许的业务码（如 404=秒传未命中）
        :param 超时秒:   覆盖默认读超时（默认 3600s）。**长轮询接口必须传**，
                        否则服务端挂起时会干等一小时（见 passport /channel/unicast）

        遇到 `errno:-6`（未登录）且配置了 `会话刷新器` 时，
        会自动补取 bdstoken 并重放**一次**请求。
        """
        内容类型 = None if 表单数据 is not None else "application/json"
        请求头 = self._构造公共请求头(带业务头, 内容类型)
        if headers:
            请求头.update(headers)
        请求头 = _校验请求头(请求头)

        参数 = self._构造公共参数(带业务头, 带渠道, 需要bdstoken, params)
        地址 = _安全URL((基础地址 or self.基础地址).rstrip("/") + 路径)

        try:
            响应, 数据 = self._执行一次(
                方法, 地址, 请求头=请求头, params=参数,
                json数据=json数据, 表单数据=表单数据, 文件=None,
                raw=raw, 需要认证=需要认证, 允许errno=允许errno,
                超时秒=超时秒)
        except 接口错误 as e:
            # 【自愈】补取 bdstoken 后重放一次（仅一次，避免死循环）
            if not (e.是未登录 and 需要认证 and self.会话刷新器):
                raise
            logger.info("[网络] 收到 errno:-6，刷新会话后重放一次")
            if not self._刷新会话():
                raise
            参数 = self._构造公共参数(
                带业务头, 带渠道, 需要bdstoken,
                {k: v for k, v in 参数.items()
                 if k not in ("bdstoken", "dp-logid")})
            响应, 数据 = self._执行一次(
                方法, 地址, 请求头=请求头, params=参数,
                json数据=json数据, 表单数据=表单数据, 文件=None,
                raw=raw, 需要认证=需要认证, 允许errno=允许errno,
                超时秒=超时秒)

        return 响应 if raw else 数据

    def _刷新会话(self) -> bool:
        """调用会话刷新器；异常与 False 一律视为失败。

        ⚠️ 加了**节流**：没登录时每个请求都会回 errno:-6，于是每个请求都来刷一次
        会话 —— 实测一次会话里刷出 2000+ 条同样的失败日志，白烧 CPU 和网络，
        界面也会被拖卡。现在 30 秒内只真正刷一次，其余请求直接复用上次结论。

        2026-09-19 补强（用户现场 RecursionError）：
          * 原来的重入保护只认**同一个线程**，另一个线程撞进来照样会再刷一次；
            刷新本身慢（超时 10s、读超时 3600s）时，多个线程就会层层叠进去；
          * 现在"正在刷新"是全局标记，并且**先写标记再动手**，动手期间
            30 秒窗口不刷新（否则刷一次超过 30s 就又会放行下一次）；
          * 同线程重入（认证层 ↔ 网络层互相调用）直接返回上次结论，绝不递归。
        """
        import threading as _threading
        import time as _time
        全局 = _刷新节流
        # ① 重入保护（线程无关）：刷新会话本身要发请求（而且往往新建客户端），
        #    请求再失败又会回来刷新 —— 不加这个会**无限递归**
        #    （实测：RecursionError + 重复日志，最后把主程序拖崩）。
        本线程 = _threading.get_ident()
        进行中 = 全局.get("进行中")
        if 进行中 is not None:
            if 进行中 == 本线程:
                logger.debug("[网络] 会话刷新重入（本线程），直接复用上次结论")
            else:
                logger.debug("[网络] 已有线程正在刷新会话，复用上次结论")
            return bool(全局.get("上次结果"))
        # ② 节流：没登录时每个请求都回 errno:-6，别每个都真刷一次
        现在 = _time.time()
        if 现在 - float(全局.get("上次时间") or 0.0) < 30.0:
            return bool(全局.get("上次结果"))
        全局["进行中"] = 本线程
        全局["上次时间"] = 现在
        try:
            结果 = bool(self.会话刷新器 and self.会话刷新器())
        except RecursionError:
            logger.warning("[网络] 会话刷新出现递归，已中止本次刷新")
            结果 = False
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[网络] 会话刷新失败：{type(e).__name__}: {e}")
            结果 = False
        finally:
            全局["进行中"] = None
        全局["上次结果"] = 结果
        return 结果

    def _刷新会话_原(self) -> bool:
        try:
            return bool(self.会话刷新器 and self.会话刷新器())
        except Exception as e:
            logger.warning(f"[网络] 会话刷新失败：{type(e).__name__}: {e}")
            return False

    def 表单文件请求(
        self,
        路径: str,
        文件字段名: str,
        文件路径: str | Path,
        额外字段: dict | None = None,
        基础地址: str | None = None,
        带业务头: bool = True,
        带渠道: bool = True,
        需要bdstoken: bool = False,
        params: dict | None = None,
        允许errno: tuple | list | None = None,
    ) -> Any:
        """POST multipart/form-data 文件上传（自动设置边界）。"""
        路径对象 = Path(文件路径)
        if not 路径对象.is_file():
            raise FileNotFoundError(f"文件不存在：{文件路径}")

        请求头 = self._构造公共请求头(带业务头, content_type=None)
        请求头 = _校验请求头(请求头)

        参数 = self._构造公共参数(带业务头, 带渠道, 需要bdstoken, params)
        地址 = _安全URL((基础地址 or self.基础地址).rstrip("/") + 路径)

        with 路径对象.open("rb") as f:
            文件内容 = f.read()

        logger.debug(
            f"[网络] POST(multipart) {地址} "
            f"file={路径对象.name}({len(文件内容)}B)")

        响应, 数据 = self._执行一次(
            "POST", 地址, 请求头=请求头, params=参数,
            json数据=None, 表单数据=dict(额外字段 or {}),
            文件={文件字段名: (路径对象.name, 文件内容,
                            "application/octet-stream")},
            raw=False, 需要认证=True, 允许errno=允许errno)
        return 数据

    # 便捷：账号域名请求（登录相关，不注入网盘公共参数）
    def 账号请求(self, 方法: str, 路径: str, **kw) -> Any:
        kw.setdefault("基础地址", 账号基础地址)
        kw.setdefault("带业务头", False)
        return self.请求(方法, 路径, **kw)

    # ==================== 直传 / 流式 ====================

    def 原始PUT(self, 完整地址: str, 数据: bytes,
                请求头: dict | None = None) -> httpx.Response:
        """向任意非 api 域名直接 PUT（小数据，可安全重试）。"""
        安全地址 = _安全URL(完整地址)
        安全头 = _校验请求头(请求头 or {})
        return _执行带重试(
            lambda: self.客户端.put(安全地址, content=数据, headers=安全头),
            描述=f"PUT {安全地址}",
        )

    def 流式PUT(self, 完整地址: str, 数据生成器,
                请求头: dict | None = None,
                总大小: int = 0) -> httpx.Response:
        """流式 PUT（生成器一次性，不可重放，故不重试）。"""
        headers = dict(请求头 or {})
        if 总大小 > 0:
            headers["Content-Length"] = str(总大小)
        headers = _校验请求头(headers)
        安全地址 = _安全URL(完整地址)

        with httpx.Client(
            timeout=httpx.Timeout(connect=30.0, read=3600.0,
                                  write=3600.0, pool=30.0),
            http2=False,
        ) as 客户端:
            return 客户端.put(安全地址, content=数据生成器, headers=headers)

    def 流式GET(self, 地址: str,
                请求头: dict | None = None,
                带会话: bool = True) -> httpx.Response | None:
        """流式 GET（下载）。失败返回 None，不抛异常。"""
        下载超时 = httpx.Timeout(connect=30.0, read=3600.0,
                                 write=30.0, pool=30.0)
        try:
            安全地址 = _安全URL(地址)
            安全头 = dict(请求头 or {})
            if 带会话:
                cookie头 = self._构造cookie头()
                if cookie头:
                    安全头.setdefault("cookie", cookie头)
            安全头 = _校验请求头(安全头)
            请求 = self.客户端.build_request(
                "GET", 安全地址, headers=安全头, timeout=下载超时)
            响应 = self.客户端.send(请求, stream=True)
            if 响应.status_code not in (200, 206):
                logger.warning(f"[网络] 流式下载失败：HTTP {响应.status_code}")
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
