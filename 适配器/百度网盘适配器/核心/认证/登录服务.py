# 百度网盘适配器/核心/认证/登录服务.py
"""
登录服务
作用：百度网盘扫码登录 + 手工 Cookie 导入兜底

扫码登录四步（真机抓包实测，见 PLAN.md §4.3）
--------------------------------------------
  ① GET passport.baidu.com/v2/api/getqrcode?lp=pc&qrloginfrom=pc&gid=<GUID>
        &apiver=v3&tpl=netdisk&tt=<ms>
     → { errno:0, sign:"<hex32>",
         imgurl:"passport.baidu.com/v2/api/qrcode?sign=<hex32>&...",
         prompt:"登录后威马将获得百度账号的公开信息" }
     ⚠️ 真实响应**既没有 `channel_id`，也没有 base64 的 `qrcode`**
        （2026-09-14 真机复测确认，见 PLAN.md §4.3）。
        `imgurl` 是**省略了 scheme 的相对地址**，需补 `https://` 才能拉取。
  ② GET passport.baidu.com/v2/api/qrcode?sign=<sign>&lp=pc&qrloginfrom=pc
     → 二维码 PNG 图片（实测 1012 字节，魔数 \x89PNG）
  ③ GET passport.baidu.com/channel/unicast?channel_id=<hex32>&gid=<GUID>
         &tpl=netdisk&apiver=v3&tt=<ms>&_sdkFrom=1          ← 长轮询
     ⚠️ **`channel_id` 的值就是 ① 里的 `sign`**。实测：传 `sign=<sign>`
        直接返回 `{"errno":-1}`，而传 `channel_id=<sign>` 会挂起等待，
        证明该接口吃的是 channel_id 且其值即 sign。
     ⚠️ 长轮询：用户未扫码时会一直挂住（实测 60s 后被服务端以 200 空体放行）。
        因此**读超时必须当作「尚未扫码」**，不能当失败（见 网络读超时）。
     → channel_v: {"status":0} 未扫 / {"status":1} 已扫待确认
                  {"status":2,"v":"<32位hex登录令牌>"} 已确认
  ④ GET passport.baidu.com/v3/login/main/qrbdusslogin
        ?bduss=<③返回的32位登录令牌>&u=<urlencode(跳转)>&loginVersion=v5
        &qrcode=1&tpl=netdisk&apiver=v3&time=<秒>
     → 下发 BDUSS Cookie
  ⑤ GET passport.baidu.com/v3/login/api/auth/?return_type=5&tpl=netdisk
        &u=https://pan.baidu.com/disk/main                     → 302（建立 pan 域会话）
  ⑥ 子域种植 plantcookie（对 pcs / pcsdata 各一次）
     GET passport.baidu.com/v3/login/api/auth?return_type=3&tpl=netdisk&u=<plantcookie地址>
     → 302 到 pcs.baidu.com / pcsdata.baidu.com 的 plantcookie
     **不做这一步，上传下载通道会 401**

⚠️ 已知不确定项（诚实标注）
  抓包中 ①③④ 的**响应体未被 Chrome 记录**（只有 Content-Length），
  其中 ① 已于 2026-09-14 真机复测补齐（字段为 errno/sign/imgurl/prompt）。
  ③ 的参数语义已复测确认（channel_id = ① 的 sign），但**扫码后的
  `channel_v` 报文仍未实测**，代码对 JSONP 包裹与多种字段名做了容错；
  若首次真机联调取不到字段，日志会打印响应原文（已脱敏）便于比对。

兜底路径：`手工导入cookie()` —— 直接粘贴浏览器里的 Cookie 文本（见 PLAN.md §4.6，
  最小集为 BDUSS + STOKEN，推荐再加 BDUSS_BFESS）。
"""
from __future__ import annotations

import base64
import json
import logging
import re
import time
import uuid
from typing import Callable, Optional
from urllib.parse import parse_qs, urlparse

from .会话仓库 import 会话仓库, 全局会话仓库
from ..网络.网络客户端 import (
    网络客户端, 账号基础地址, 接口错误, 网络读超时,
)

logger = logging.getLogger("百度网盘.登录")

PASSPORT = "https://passport.baidu.com"
网盘首页 = "https://pan.baidu.com/disk/main"
#: pan 域基础地址（`/api/*` 与 `/disk/main` 都在这个域下）
pan基础地址 = "https://pan.baidu.com"

轮询间隔秒 = 1.5
轮询最长等待秒 = 300.0
# /channel/unicast 是长轮询（实测挂 60s 才被服务端放行），读超时设 60s：
# 既不会像默认的 3600s 那样一直干等，也不至于过短导致空转。
unicast读超时秒 = 60.0

# 子域种植目标（不做会导致 pcs 通道 401）
_种植目标 = (
    "https://pcs.baidu.com/rest/2.0/pcs/file?method=plantcookie&source=pcs",
    "https://pcsdata.baidu.com/rest/2.0/pcs/file?method=plantcookie&source=pcs",
)

# 轮询状态码
状态_未扫码 = 0
状态_已扫码 = 1
状态_已确认 = 2


def _去JSONP(文本: str) -> str:
    """剥掉 JSONP 外壳 `callback({...})`，返回纯 JSON 文本。

    抓包显示 passport 接口默认走 JSONP（query 带 callback=），
    Python 客户端不带 callback 时应直接得到 JSON；此处兼容两种情况。
    """
    s = (文本 or "").strip()
    if not s:
        return s
    if s.startswith("{") or s.startswith("["):
        return s
    m = re.match(r"^[A-Za-z_$][\w$]*\s*\(\s*(.*?)\s*\)\s*;?\s*$", s, re.S)
    return m.group(1) if m else s


def _脱敏(数据) -> str:
    """日志用：抹掉 sign / qrcode / bduss 等敏感值后转字符串。"""
    s = 数据 if isinstance(数据, str) else json.dumps(数据, ensure_ascii=False)
    for k in ("sign", "qrcode", "bduss", "BDUSS", "stoken", "channel_id"):
        s = re.sub(rf'"{k}"\s*:\s*"[^"]*"', f'"{k}":"<已脱敏>"', s)
    return s[:400]


class 登录服务:
    """百度网盘登录。

    :param 仓库: 会话仓库（默认全局单例）
    """

    def __init__(self, 超时秒: float = 20.0,
                 仓库: 会话仓库 | None = None):
        self.仓库 = 仓库 or 全局会话仓库
        self.超时秒 = 超时秒
        # 登录走 passport 域，且不带网盘业务公共参数
        # 超时秒 作用于连接阶段（原骨架的「超时秒」在网络层是死参数，已修正）
        self.网络 = 网络客户端(基础地址=PASSPORT, 连接超时秒=超时秒)
        # 一次登录会话内复用同一个 gid（抓包中 gid 全程不变）
        self.gid = str(uuid.uuid4()).upper()
        # 只用于日志：上一次看到的 unicast status（便于记录每次变化）
        self._上次状态指纹 = None
        self._已见此令牌 = False

    # ---------------- 内部：passport 请求 ----------------

    def _passport请求(self, 路径: str, params: dict,
                      超时秒: float | None = None) -> tuple[object, str]:
        """请求 passport 接口，返回 (响应, 已去 JSONP 的文本)。

        :param 超时秒: 覆盖读超时。长轮询接口（`/channel/unicast`）传
                       `unicast读超时秒`，避免用默认的 3600s 干等。
        """
        参数 = dict(params)
        参数.setdefault("apiver", "v3")
        参数.setdefault("tpl", "netdisk")
        参数.setdefault("tt", str(int(time.time() * 1000)))
        kwargs = {}
        if 超时秒 is not None:
            kwargs["超时秒"] = 超时秒
        响应 = self.网络.请求(
            "GET", 路径,
            基础地址=PASSPORT,
            params=参数,
            带业务头=False,
            需要认证=False,
            raw=True,
            headers={"referer": "https://pan.baidu.com/"},
            **kwargs,
        )
        return 响应, _去JSONP(响应.text)

    # ---------------- ① 二维码 ----------------

    def 请求二维码(self) -> dict:
        """步骤 ①②：获取二维码，并把图片拉回来转成 base64。

        **真机实测的响应形态**（2026-09-14）：:

            {"errno": 0, "sign": "<hex32>",
             "imgurl": "passport.baidu.com/v2/api/qrcode?sign=<hex32>&lp=pc&...",
             "prompt": "登录后威马将获得百度账号的公开信息（用户名、头像）"}

        ⚠️ 与旧假设的差异（原实现据此报「缺少 sign/channel_id」）：
          1. **没有 `channel_id`** —— `sign` 本身就是轮询要用的 channel_id；
          2. **没有 base64 的 `qrcode`** —— 图片得拿 `imgurl` 去下载；
          3. `imgurl` **省略了 scheme**（`passport.baidu.com/...`），
             直接丢给 httpx 会因为「没有协议」报错，必须补 `https://`。

        :return: `{sign, 轮询标识, channel_id, qrcode(base64), imgurl(绝对地址)}`
                 - `qrcode`：图片的 base64（无 data: 前缀），界面直接
                   `QPixmap.loadFromData(base64.b64decode(...))` 即可。
        """
        self.gid = str(uuid.uuid4()).upper()
        响应, 文本 = self._passport请求("/v2/api/getqrcode", {
            "lp": "pc",
            "qrloginfrom": "pc",
            "gid": self.gid,
            "logPage": "traceId:pc_loginv5,logPage:loginv5",
        })
        try:
            数据 = json.loads(文本)
        except Exception:
            raise 接口错误(
                f"getqrcode 响应无法解析：{_脱敏(文本)}",
                状态码=响应.status_code)
        if not isinstance(数据, dict):
            raise 接口错误(f"getqrcode 响应异常：{_脱敏(文本)}")

        # ---- sign：优先从 imgurl 的 query 里取（它一定和图片配套）----
        图片地址 = str(数据.get("imgurl") or "").strip()
        if 图片地址 and not 图片地址.startswith(("http://", "https://")):
            # 实测就是这种「只有主机名」的形态
            图片地址 = "https://" + 图片地址.lstrip("/")

        sign = (parse_qs(urlparse(图片地址).query).get("sign") or [""])[0].strip()
        if not sign:
            sign = str(数据.get("sign") or "").strip()
        if not sign:
            raise 接口错误(f"getqrcode 未取到 sign：{_脱敏(数据)}")

        # ---- 二维码图片：优先用响应里的 base64，否则下载 imgurl ----
        二维码图片 = str(数据.get("qrcode") or "").strip()
        if not 二维码图片:
            if not 图片地址:
                raise 接口错误(f"getqrcode 既无 qrcode 也无 imgurl：{_脱敏(数据)}")
            try:
                图片响应 = self.网络.客户端.get(
                    图片地址, timeout=20.0,
                    headers={"referer": "https://pan.baidu.com/"})
            except Exception as e:
                raise 接口错误(
                    f"二维码图片下载失败：{type(e).__name__}: {e}") from e
            if 图片响应.status_code != 200 or not 图片响应.content:
                raise 接口错误(
                    f"二维码图片下载失败：HTTP {图片响应.status_code}",
                    状态码=图片响应.status_code)
            二维码图片 = base64.b64encode(图片响应.content).decode("ascii")
            logger.info(f"[登录] 二维码图片已下载：{len(图片响应.content)} 字节")

        # ✅ 实测：unicast 吃的就是 sign（传 sign= 会得 errno:-1，传 channel_id= 才挂起）
        轮询标识 = str(数据.get("channel_id") or "").strip() or sign
        logger.info(f"[登录] 已获取二维码：sign=len{len(sign)} "
                    f"轮询标识=len{len(轮询标识)} "
                    f"图片base64=len{len(二维码图片)}")
        return {
            "sign": sign,
            "channel_id": 轮询标识,
            "轮询标识": 轮询标识,
            "qrcode": 二维码图片,
            "imgurl": 图片地址,
        }

    def 取登录二维码(self) -> dict:
        """界面层直接可用的二维码信息（Phase 7.7）。

        :return: `{qrcode_b64, sign, channel_id, 轮询标识, imgurl}`
                 - `qrcode_b64`：二维码 PNG 的 base64。**服务端不再下发
                   base64**，这里是本方法下载 `imgurl` 后自己转的，
                   界面无需再发请求。
                 - `imgurl`：图片绝对地址（`https://` 已补全）。
        """
        原始 = self.请求二维码()
        return {
            "qrcode_b64": 原始.get("qrcode") or "",
            "sign": 原始.get("sign") or "",
            "channel_id": 原始.get("channel_id") or "",
            "轮询标识": 原始.get("轮询标识") or "",
            "imgurl": 原始.get("imgurl") or "",
        }

    def 下载二维码图片(self, sign: str) -> bytes:
        """步骤 ②：按 sign 下载二维码 PNG 字节（兜底接口）。"""
        响应, _ = self._passport请求("/v2/api/qrcode", {
            "sign": sign,
            "lp": "pc",
            "qrloginfrom": "pc",
        })
        if 响应.status_code != 200:
            raise 接口错误(f"二维码下载失败：HTTP {响应.status_code}",
                           状态码=响应.status_code)
        return 响应.content

    # ---------------- ③ 轮询 ----------------

    def 查询扫码状态(self, 轮询标识: str) -> dict:
        """步骤 ③：查询一次扫码状态（长轮询，最多挂 `unicast读超时秒`）。

        :param 轮询标识: `请求二维码()` 返回的 `轮询标识`（= `sign`）。
            实测 `channel_id` 参数吃的就是这个值。
        :return: {status:int, token:str|None}
        :raises 网络读超时: 本轮内服务端没有事件 —— 调用方应继续轮询
        """
        _, 文本 = self._passport请求("/channel/unicast", {
            "channel_id": 轮询标识,
            "gid": self.gid,
            "_sdkFrom": "1",
        }, 超时秒=unicast读超时秒)
        try:
            外层 = json.loads(文本)
        except Exception:
            logger.debug(f"[登录] unicast 响应无法解析：{_脱敏(文本)}")
            return {"status": 状态_未扫码, "token": None}

        # channel_v 可能是 JSON 字符串（双层编码），也可能是对象
        内层 = 外层.get("channel_v") if isinstance(外层, dict) else None
        if isinstance(内层, str):
            try:
                内层 = json.loads(内层)
            except Exception:
                内层 = {}
        if not isinstance(内层, dict):
            内层 = {}

        status = 内层.get("status", 外层.get("status", 状态_未扫码))
        token = self._取令牌(外层, 内层)
        # ✅ 真机实测（2026-09-14，两次独立复现）：
        #    「已确认」的报文是 **status=0 且带 v**，而不是 status=2：
        #        {"channel_v":"{\"status\":1}"}                       ← 已扫描
        #        {"channel_v":"{\"status\":0,\"v\":\"<hex32>\",\"u\":\"\"}"} ← 已确认
        #    状态是从 1 **回退到 0** 的。之前一直等 status==2，所以永远等不到，
        #    表现为「手机上点了确认，桌面却一直没反应」。
        #    因此这里以「有没有令牌」作为确认的判据，状态位只用于界面文案。
        if token:
            显示状态 = 状态_已确认
        else:
            显示状态 = status
        # 顶层 errno 非 0 且没有 status：报文形态与预期不符。
        # 实测未扫码时长轮询约 30s 后返回 {"errno":1}，此处按「未扫码」继续轮询，
        # 但留一条指纹日志，便于真机比对是否需要额外处理。
        errno = 外层.get("errno") if isinstance(外层, dict) else None
        if errno not in (0, None) and "status" not in 外层 and not 内层:
            logger.info(f"[登录] unicast 返回 errno={errno}（无 status），"
                        f"按未扫码继续轮询；响应指纹={_脱敏(文本)}")
            return {"status": 状态_未扫码, "token": None}
        # 🔍 状态一变就记指纹（含回退到 0 的情况）——
        #    之前只在「首次非 0」记一次，导致 1→0 的报文没被记下来，
        #    真机排查时无从判断服务端到底返回了什么。
        if 显示状态 != self._上次状态指纹 or (token and not self._已见此令牌):
            self._上次状态指纹 = 显示状态
            if token:
                self._已见此令牌 = True
            logger.info(f"[登录] unicast 原始status={status} 显示状态={显示状态} "
                        f"有token={bool(token)} 响应指纹={_脱敏(文本)}")
        return {"status": 显示状态, "token": token}

    @staticmethod
    def _取令牌(外层: dict, 内层: dict) -> str | None:
        """从 unicast 报文里取登录令牌（32 位 hex）。

        已知的官方位置是 `channel_v.v`，但报文形态未经真机确认，
        因此这里按「优先已知字段 → 再全文兜底」两级查找：
        任何 32 位 hex 字符串都可能是令牌，兜底能避免字段改名就登不上。
        """
        for 容器 in (内层, 外层):
            if not isinstance(容器, dict):
                continue
            for 键 in ("v", "bduss", "token", "bduss_token", "channel_v"):
                值 = 容器.get(键)
                if isinstance(值, str) and re.fullmatch(r"[0-9a-fA-F]{32}", 值):
                    return 值
        # 兜底：在整份报文里找 32 位 hex（排除已知的非令牌字段）
        排除 = {"channel_id", "sign", "gid"}
        for 容器 in (内层, 外层):
            if not isinstance(容器, dict):
                continue
            for 键, 值 in 容器.items():
                if 键 in 排除 or not isinstance(值, str):
                    continue
                if re.fullmatch(r"[0-9a-fA-F]{32}", 值):
                    logger.info(f"[登录] 令牌取自兜底字段 {键!r}")
                    return 值
        return None

    def 等待扫码(self, 轮询标识: str,
                 状态回调: Optional[Callable[[int], None]] = None,
                 超时秒: float = 轮询最长等待秒) -> str:
        """轮询直到确认，返回 32 位登录令牌。

        :param 轮询标识: `请求二维码()` 返回的 `轮询标识`（实测等于 `sign`）。
        """
        开始 = time.monotonic()
        上次状态 = None
        读超时次数 = 0
        while time.monotonic() - 开始 < 超时秒:
            try:
                结果 = self.查询扫码状态(轮询标识)
            except 网络读超时:
                # ✅ 长轮询超时 ≠ 失败：只说明这一轮内用户还没动作，接着等
                读超时次数 += 1
                if 读超时次数 % 5 == 1:
                    logger.debug(f"[登录] unicast 长轮询超时 ×{读超时次数}，继续等待扫码")
                continue
            状态 = 结果["status"]
            if 状态 != 上次状态:
                logger.info(f"[登录] 扫码状态变化：{上次状态} → {状态}")
                上次状态 = 状态
                if 状态回调:
                    try:
                        状态回调(状态)
                    except Exception:
                        pass
            if 状态 == 状态_已确认 and 结果.get("token"):
                return 结果["token"]
            time.sleep(轮询间隔秒)
        raise TimeoutError("扫码登录超时，请重新获取二维码")

    # ---------------- ④⑤⑥ 换取 Cookie ----------------

    def 换取会话(self, 登录令牌: str) -> bool:
        """步骤 ④：用扫码令牌换取 BDUSS Cookie，并写入会话仓库。

        失败时把 HTTP 状态码与响应指纹一起抛出来 —— 之前只说「未取得
        BDUSS/STOKEN」，真机上完全看不出是令牌无效、被重定向到验证页，
        还是单纯字段没解析到。
        """
        响应, 文本 = self._passport请求(
            "/v3/login/main/qrbdusslogin",
            {
                "bduss": 登录令牌,
                "u": 网盘首页,
                "loginVersion": "v5",
                "qrcode": "1",
                "time": str(int(time.time())),
            },
        )
        # ✅ 逐条 Set-Cookie 头解析：不能 join 后再切，expires 里的逗号
        #    会把下一个 cookie 的名字吃掉（详见 会话仓库.从cookie头列表提取）
        命中 = self.仓库.从cookie头列表提取(响应.headers.get_list("set-cookie"))
        if not self.仓库.是否有效():
            # 兜底：httpx 的 cookie jar 里可能已有
            jar = "; ".join(f"{k}={v}" for k, v in 响应.cookies.items())
            命中 += self.仓库.从响应头提取(jar)
        if not self.仓库.是否有效():
            # 把服务端错误码解析出来 —— 之前只贴 200 字指纹，
            # 恰好把 errInfo/code 截掉，真机上分不清失败原因。
            错误码 = "?"
            try:
                载荷 = json.loads(re.sub(r"'", '"', 文本 or ""))
                错误码 = (载荷.get("code")
                          or (载荷.get("errInfo") or {}).get("no") or "?")
            except Exception:
                m = re.search(r'"no"\s*:\s*"([^"]+)"', 文本 or "")
                错误码 = m.group(1) if m else "?"
            raise 接口错误(
                f"扫码已确认，但换取 BDUSS 失败。HTTP {响应.status_code}；"
                f"服务端错误码={错误码}；识别到 {命中 or '无 Cookie'}；"
                f"响应指纹={_脱敏(文本)[:300]}",
                状态码=响应.status_code)
        logger.info(f"[登录] 已取得会话：{命中}")
        return True

    def 建立pan域会话_换发stoken(self) -> bool:
        """再访问一次 pan 域，把 passport 域那份 STOKEN 换成 pan 域那份。

        真机实测（2026-09-19）：**pan 域的 STOKEN 不在登录响应里**，
        而是访问 pan 域（`/v3/login/api/auth/?return_type=5&u=https://pan.baidu.com/disk/main`）
        时由服务端换发的。少了这一步，会话就是"只能读"：
        `/api/list` 正常、`/api/gettemplatevariable` 与所有写接口 errno:-6。

        :return: 是否真的换到了不同的 STOKEN（True 通常意味着写权限已就绪）
        """
        之前 = self.仓库.获取stoken()
        self.建立网盘会话()
        # 顺带把 pan 域首页走一遍：实测这一下也会触发换发
        try:
            self.网络.请求("GET", "/disk/main", 基础地址=pan基础地址,
                        带业务头=False, 需要认证=False, raw=True)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[登录] 访问 pan 首页异常（可忽略）：{e}")
        self.种植子域会话()
        之后 = self.仓库.获取stoken()
        if 之后 and 之后 != 之前:
            logger.info("[登录] ✅ 已换发 pan 域 STOKEN（写权限就绪）")
            return True
        if 之后:
            logger.info("[登录] pan 域 STOKEN 与换取会话时相同（可能已是 pan 域那份）")
            return False
        logger.warning("[登录] 仍未拿到 STOKEN（写操作会失败，可稍后重试或导入浏览器会话）")
        return False

    def 种植子域会话(self) -> list[str]:
        """步骤 ⑥：向 pcs / pcsdata 子域种植会话。

        不做这一步，superfile2 等上传通道会返回未登录。
        :return: 成功的子域列表
        """
        成功: list[str] = []
        for 目标 in _种植目标:
            子域 = "pcsdata" if "pcsdata" in 目标 else "pcs"
            try:
                响应 = self.网络.请求(
                    "GET", "/v3/login/api/auth",
                    基础地址=PASSPORT,
                    params={"return_type": "3", "tpl": "netdisk", "u": 目标},
                    带业务头=False,
                    需要认证=False,
                    raw=True,
                    headers={"referer": "https://pan.baidu.com/"},
                )
                logger.info(f"[登录] 子域种植 {子域} → HTTP {响应.status_code}")
                # ⚠️ 跟随重定向后最终可能落在 wappass 的错误页（实测 400
                #    "Unsupported open api"）。**不要重试**：重试会被
                #    wappass 反复打回（日志里会有 _retry_=1 循环）。
                #    该步骤对文件列表/登录态均非必需，失败仅记录。
                if 响应.status_code in (200, 302):
                    成功.append(子域)
                else:
                    logger.info(f"[登录] 子域种植 {子域} 未成功"
                                f"（HTTP {响应.status_code}，不影响登录）")
            except Exception as e:
                logger.warning(
                    f"[登录] 子域种植 {子域} 失败：{type(e).__name__}: {e}")
        return 成功

    def 建立网盘会话(self) -> None:
        """步骤 ⑤：访问 /v3/login/api/auth/ 建立 pan 域会话。"""
        try:
            响应 = self.网络.请求(
                "GET", "/v3/login/api/auth/",
                基础地址=PASSPORT,
                params={"return_type": "5", "tpl": "netdisk", "u": 网盘首页},
                带业务头=False,
                需要认证=False,
                raw=True,
                headers={"referer": "https://pan.baidu.com/"},
            )
            logger.info(f"[登录] pan 域会话建立 → HTTP {响应.status_code}")
        except Exception as e:
            logger.warning(f"[登录] pan 域会话建立失败：{type(e).__name__}: {e}")

    # ---------------- 编排 ----------------

    def 完成扫码登录(
        self,
        二维码回调: Optional[Callable[[str, str], None]] = None,
        用户码回调: Optional[Callable[[str], None]] = None,
        状态回调: Optional[Callable[[int], None]] = None,
        超时秒: float = 轮询最长等待秒,
    ) -> dict:
        """完整扫码登录流程，返回会话摘要。

        :param 二维码回调: `(二维码内容, 标识)`。
            百度由**服务端下发二维码图片**，由本服务下载后转成 base64
            回传（服务端自 2026-09 起不再直接给 base64），
            与原骨架「本地用 qrcode 库生成」不同——界面层需相应适配。
        :param 用户码回调: `(标识)`。百度无「用户码」概念，回传 sign。
        :param 状态回调: `(status)`，取值 0/1/2。
        """
        二维码 = self.请求二维码()
        sign = 二维码["sign"]
        内容 = 二维码["qrcode"] or 二维码["imgurl"]

        logger.info("[登录] 请使用百度网盘 App 扫描二维码")
        if 二维码回调:
            二维码回调(内容, sign)
        if 用户码回调:
            用户码回调(sign)

        轮询标识 = 二维码.get("轮询标识") or 二维码.get("channel_id") or sign
        登录令牌 = self.等待扫码(轮询标识,
                                状态回调=状态回调, 超时秒=超时秒)
        self.换取会话(登录令牌)
        # ✅ **关键一步**（真机实测，2026-09-19）：换取会话拿到的是
        #    **passport 域**的 STOKEN，读接口能用、写接口一律 errno:-6。
        #    pan 域的 STOKEN 是**访问 pan 域时换发**的 —— 必须在这里多走一步
        #    `建立网盘会话()`（return_type=5，落到 pan 域）再 `种植子域会话()`，
        #    换完 stoken 就变了（实测 9dfe29b5… → a1c9d58d…，与浏览器那份一致），
        #    写接口随之从 errno:-6 变成可写。
        #
        #    ⚠️ 以前只在"手工导入 Cookie"路径里跑这两步，扫码路径漏了，
        #       于是扫码登录永远是"只读会话"：能列目录、上传/改名/删除全废。
        self.建立网盘会话()
        try:
            self.建立pan域会话_换发stoken()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[登录] 换发 pan 域 STOKEN 异常（不影响登录）：{e}")

        # ✅ 先补齐 bdstoken / uk，再谈子域种植。
        #    真机实测：走到这一步会话**已经可用**（检查登录态直接成功），
        #    但 bdstoken/uk 只有当次请求才下发，不主动取一次就是空的，
        #    表现为「登录成功但账号信息页空白、写操作可能失败」。
        #
        #    ⚠️ 必须用 **pan 域** 的客户端取：`/api/gettemplatevariable` 是
        #       网盘接口，本服务的 self.网络 是 base_url=passport.baidu.com，
        #       在 passport 上没这个路径 → 被重定向到登录页 HTML →
        #       「响应不是合法 JSON」。所以这里新建一个默认(pan)客户端，
        #       并把本服务自己的仓库注进去（而不是用界面的全局仓库）。
        if self.仓库.获取bdstoken() is None or self.仓库.获取uk() is None:
            try:
                from .认证服务 import 认证服务
                网盘网络 = 网络客户端(
                    会话提供者=self.仓库.取会话,
                    连接超时秒=self.超时秒)
                try:
                    if 认证服务(网络=网盘网络, 仓库=self.仓库).刷新会话():
                        logger.info(
                            f"[登录] 已补齐 bdstoken/uk：uk={self.仓库.获取uk()} "
                            f"bdstoken={'有' if self.仓库.获取bdstoken() else '无'}")
                    else:
                        logger.warning("[登录] 补齐 bdstoken/uk 未成功（不影响登录）")
                finally:
                    网盘网络.关闭()
            except Exception as e:
                logger.warning(f"[登录] 补齐 bdstoken/uk 异常（不影响登录）：{e}")

        # ⚠️ 子域种植是**尽力而为**：官方那条 return_type=3 的链路
        #    会被 wappass 302 到一个 errno=2 的错误页（实测 400
        #    "Unsupported open api"），且**不影响文件列表/登录态**。
        #    因此失败只记警告，绝不当作登录失败 —— 更不要重试
        #    （重试只会被 wappass 不断打回，日志里那串 _retry_=1 就是它）。
        子域 = self.种植子域会话()

        logger.info(f"[登录] 扫码登录成功（uk={self.仓库.获取uk()} "
                    f"bdstoken={'有' if self.仓库.获取bdstoken() else '无'} "
                    f"子域={子域 or '无'}）")
        return {
            "方式": "扫码",
            "uk": self.仓库.获取uk(),
            "种植子域": 子域,
            "会话": self.仓库.取会话(),
        }

    # ---------------- 兜底：手工导入 ----------------

    def 手工导入cookie(self, 文本: str) -> list[str]:
        """兜底登录：直接导入浏览器 Cookie 文本。

        从 pan.baidu.com 复制任意请求的 `Cookie:` 头即可。
        实测最小集为 **BDUSS + STOKEN**（推荐再加 BDUSS_BFESS）。
        """
        命中 = self.仓库.从cookie文本导入(文本)
        logger.info(f"[登录] 手工导入成功：{命中}")
        return 命中

    def 关闭(self) -> None:
        self.网络.关闭()
