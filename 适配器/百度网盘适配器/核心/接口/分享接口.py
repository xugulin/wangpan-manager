# 百度网盘适配器/核心/接口/分享接口.py
"""
分享接口（非转存部分）
作用：创建分享 / 分享列表 / 取消分享 / 提取码查询

端点（HAR 实测，见 PLAN.md §6.4）
--------------------------------
① 创建分享
   POST /share/pset?channel=chunlei&bdstoken=...&clienttype=0&app_id=250528&web=1&dp-logid=...
   body: is_knowledge=0, public=0, period=<0|天数>, pwd=<4位>,
         eflag_disable=true, linkOrQrcode=link, channel_list=[],
         schannel=4, fid_list=[<fs_id>,...]
   → { errno:0, link:"https://pan.baidu.com/s/1LaFlYKlcychx64Na_dmrnQ",
       shorturl:"...", shareid:45163615061, ctime, expiredType, expiretime,
       qrcodeurl, createsharetips_ldlj:"复制这段内容后打开百度网盘手机App，操作更方便哦" }
   ⚠️ `fid_list` 是 **fs_id** 数组（不是路径）
   ⚠️ **`pwd` 是必需的**：传空串会返回 `pwd length param error`（实测）。
      不传时本模块会自动生成一个 4 位数字提取码。
   ⚠️ 实测**创建响应里没有 `pwd` 字段**（与 HAR 不同），提取码需由调用方记住；
      之后可从分享列表的 **`passwd`** 字段取回。

② 分享列表
   GET /share/record?channel=chunlei&clienttype=0&app_id=250528&web=1&dp-logid=...
        &num=100&page=1&order=ctime&desc=1&is_batch=1
   → { errno:0, count:<总数>, list:[...], nextpage:0, ... }
   ⚠️ 分页终止条件是 **`nextpage == 0`**（不是「本页是否满」）
   ⚠️ 条目字段名与其它接口不同（实测 40 个字段），注意这几组易混的：
        `shareId`（大写 I，对外 ID）  `passwd`（提取码，不是 pwd）
        `shortlink`（完整链接）       `shorturl`（仅短码，不是 URL！）
        `fsIds`（文件列表）           `typicalPath` / `typicalCategory`
      请用本模块的 `取shareid()` / `取提取码()` / `取链接()` 读取，勿硬编码字段名。

③ 取消分享
   POST /share/cancel?channel=chunlei&bdstoken=...&clienttype=0&app_id=250528&web=1&dp-logid=...
   body: shareid_list=[<shareid>,...]
   → { errno:0, newno:"", show_msg:"", request_id }
   ⚠️ 用的是 **shareid**（即列表里的 `shareId`）

④ 提取码查询
   GET /share/surlinfoinrecord?channel=chunlei&clienttype=0&app_id=250528&web=1
        &dp-logid=...&shareid=<shareid>&sign=<hex32>&bdstoken=...
   → { errno:0, shorturl:"hqUiutm", pwd:"x", isElink:0, ... }
   ⚠️ `sign` 来源未知（PLAN Q12）。**实测省略 sign 会返回 `errno:2`**，
      因此本模块抛 `提取码需签名`，由界面层标为「暂不支持」。
      变通：创建分享时自己记住提取码，或从分享列表的 `passwd` 字段读取。

⚠️ **转存 `/share/transfer` 未实现** —— `sekey` 来源未知（PLAN Q13，
   抓包里该参数只出现在请求中、不在任何响应体里），待补抓后再做。
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
from typing import Iterable
from urllib.parse import unquote

from ..网络.网络客户端 import 网络客户端, 接口错误

logger = logging.getLogger("百度网盘.分享")

# 有效期（天）→ period 取值。实测 0=永久、365=一年
有效期_永久 = 0
有效期_一天 = 1
有效期_七天 = 7
有效期_三十天 = 30
有效期_一年 = 365

有效期选项 = {
    有效期_永久: "永久有效",
    有效期_一天: "1 天",
    有效期_七天: "7 天",
    有效期_三十天: "30 天",
    有效期_一年: "365 天",
}

# 分享渠道（HAR 中恒为 4）
默认schannel = 4


class 提取码需签名(RuntimeError):
    """`/share/surlinfoinrecord` 需要 `sign` 参数而当前无法提供。"""


class 提取码错误(RuntimeError):
    """分享提取码校验失败（`/share/verify` 返回非 0，或响应缺 `randsk`）。"""


# ==========================================================================
# 转存的 sekey 来源（Q13，2026-09-14 已解明）
# --------------------------------------------------------------------------
# `sekey` **不在任何响应体的 `sekey` 字段里**，它是 `/share/verify` 响应中的
# **`randsk`**，且**必须 URL 解码**后才可用：
#
#     POST /share/verify?surl=<短码>&bioc=1&t=<ms>&channel=chunlei&web=1
#          &app_id=250528&clienttype=0&bdstoken=...
#     body: pwd=<提取码>&vcode=&vcode_str=
#     → { errno:0, randsk:"Tiu6lMaFeDt4fwewkmmU0hKE%2ByMvPYSJn5ckIWQHwXo%3D" }
#       sekey = unquote(randsk)         ← 44 字符，含 + 和 =
#
# 交叉验证：新 HAR 中 `/share/transfer` 实际使用的 sekey 与该分享
# `/share/verify` 返回的 `randsk`（解码后）**sha1 完全一致（1fd96f5626）**。
#
# 上传器 JS 也印证了该机制（preload-helper.bj1169hv.js）：
#     fg = e => { document.cookie = "BDCLND=" + e.replace(/[;\r\n]/g,"") + "; path=/" }
#     __ = (e,t,n,r) => { Po("local", e+t+"_bdclnd", n); ...; fg(n) }
# ——`BDCLND` cookie 的值就是 sekey，同时也会写进 localStorage 的 `<key>_bdclnd`。
# 这正对应分享页内联脚本的注释「按 path/cookie/storage 兜底读取」。
#
# `shareid` 与 `from`（分享者 uk）由 **`/share/list`** 一次性提供，
# **无需解析分享页 HTML**：
#     GET /share/list?shorturl=<短码>&page=1&num=20&root=1&web=1&app_id=250528&...
#     → { errno:0, share_id:22000728871, uk:1101796850381, list:[{fs_id,...}] }
# ==========================================================================

# 新版分享链接形如 /s/1<短码>，而 /share/list 与 /share/verify 用去掉 1 的形式
_短码前缀 = "1"


def 解析分享链接(链接: str) -> dict:
    """从分享链接或短码中解析出 `surl` 与可能的提取码。

    支持：
        https://pan.baidu.com/s/1_8kHQUxSm8BnpQCWjP7kxg
        https://pan.baidu.com/s/1_8kHQUxSm8BnpQCWjP7kxg?pwd=pxi8
        pan.baidu.com/s/1abc              （无 scheme）
        1_8kHQUxSm8BnpQCWjP7kxg           （裸短码）

    :return: `{"surl": 去前缀短码, "surl原样": 原短码, "pwd": 或 ""}`
    """
    文本 = (链接 or "").strip()
    if not 文本:
        raise ValueError("分享链接为空")

    提取码 = ""
    m = re.search(r"[?&]pwd=([0-9a-zA-Z]{4})", 文本)
    if m:
        提取码 = m.group(1)
        文本 = 文本.split("?")[0]

    m = re.search(r"/s/([0-9a-zA-Z_\-]+)", 文本)
    短码原样 = m.group(1) if m else 文本

    短码 = 短码原样
    if len(短码原样) > 1 and 短码原样.startswith(_短码前缀):
        短码 = 短码原样[len(_短码前缀):]

    return {"surl": 短码, "surl原样": 短码原样, "pwd": 提取码}


# ==========================================================================
# 分享列表条目字段适配
# --------------------------------------------------------------------------
# 实测列表条目字段名与其它接口**不一致**，且有几组极易混淆：
#     shareId（对外 ID）  ≠  shareid（创建响应里的名字）
#     passwd（提取码）    ≠  pwd（创建请求里的名字）
#     shortlink（完整链接）≠  shorturl（**只是短码，不是 URL**）
# ==========================================================================
def 取shareid(条目: dict) -> str:
    """从分享列表条目取对外 shareId（兼容大小写/下划线写法）。"""
    for k in ("shareId", "shareid", "share_id"):
        if 条目.get(k) not in (None, ""):
            return str(条目[k])
    return ""


def 取提取码(条目: dict) -> str:
    """从分享列表条目取提取码（字段名是 `passwd`，不是 `pwd`）。"""
    for k in ("passwd", "pwd"):
        v = 条目.get(k)
        if v not in (None, ""):
            return str(v)
    return ""


def 取链接(条目: dict) -> str:
    """从分享列表条目取完整分享链接。

    ⚠️ `shorturl` 实测**只是短码**（如 `1LaFlYKlcychx64Na_dmrnQ`），
    完整链接在 `shortlink`；本函数会把短码补成完整 URL。
    """
    v = 条目.get("shortlink")
    if v:
        return str(v)
    码 = 条目.get("shorturl")
    if 码:
        return 码 if str(码).startswith("http") else f"https://pan.baidu.com/s/{码}"
    return ""


def 取文件数(条目: dict) -> int:
    """条目涉及的文件数。"""
    ids = 条目.get("fsIds")
    return len(ids) if isinstance(ids, list) else 0


class 分享接口:
    """分享的创建 / 列表 / 取消 / 提取码查询 / 转存。"""

    def __init__(self, 网络: 网络客户端):
        self.网络 = 网络
        # 转存需要轮询任务，惰性构造避免循环导入
        from .任务接口 import 任务接口
        self.任务 = 任务接口(网络)
        # `/share/verify` 种下的 BDCLND cookie（= URL 编码的 sekey）
        # 后续 /share/list、/share/transfer 都必须带上它
        self._bdclnd = ""

    def _分享请求头(self, bdclnd: str | None = None) -> dict | None:
        """构造带 `BDCLND` 的请求头。

        ⚠️ **必须显式携带**：本项目的网络客户端为了注入会话 Cookie，
        会设置显式的 `cookie` 请求头，这会**覆盖 httpx 自带的 cookie jar**，
        因此 `/share/verify` 通过 `Set-Cookie` 种下的 `BDCLND` 不会被自动带上。
        实测：不带 BDCLND 访问 `/share/list` 返回 `errno:-9`；
        带上后返回 `errno:0` 并给出 share_id / uk。
        """
        码 = bdclnd or self._bdclnd
        if not 码:
            return None
        基础 = self.网络.构造cookie头()
        return {"cookie": f"{基础}; BDCLND={码}" if 基础 else f"BDCLND={码}"}

    # ==================== 转存（Phase 6b） ====================

    def 取分享信息(self, surl: str, bdclnd: str | None = None) -> dict:
        """GET /share/list —— 取分享的 `share_id` / 分享者 `uk` / 文件清单。

        ⚠️ **必须先在 `校验分享()` 之后调用**：该接口依赖 `/share/verify`
        种下的 `BDCLND` cookie，否则返回 `errno:-9`。

        :param surl: 解析后的短码（见 `解析分享链接`）
        """
        数据 = self.网络.请求(
            "GET", "/share/list",
            params={
                "shorturl": surl,
                "web": "1",
                "app_id": "250528",
                "desc": "1",
                "showempty": "0",
                "page": "1",
                "num": "100",
                "order": "time",
                "root": "1",
                "view_mode": "1",
                "channel": "chunlei",
            },
            headers=self._分享请求头(bdclnd),
            需要bdstoken=True,
            带渠道=False,
        )
        logger.info(
            f"[分享] 取分享信息 surl={surl} → share_id={数据.get('share_id')} "
            f"uk={数据.get('uk')} 顶层 {len(数据.get('list') or [])} 项")
        return 数据

    def 校验分享(self, surl: str, pwd: str = "", vcode: str = "",
                vcode_str: str = "") -> str:
        """POST /share/verify —— 校验提取码，取回 **sekey** 并记住 `BDCLND`。

        ⚠️ 两个关键点：
          1. sekey 就是响应里的 **`randsk`**，且**必须 URL 解码**；
          2. 该接口会 `Set-Cookie: BDCLND=<randsk>`，本方法会记住它，
             供后续 `/share/list` 与 `/share/transfer` 使用。

        :return: sekey（已 URL 解码的 44 字符值）
        :raises 提取码错误: 校验失败
        """
        响应 = self.网络.请求(
            "POST", "/share/verify",
            params={
                "t": str(int(time.time() * 1000)),
                "bioc": "1",
                "surl": surl,
                "channel": "chunlei",
                "web": "1",
                "app_id": "250528",
                "clienttype": "0",
            },
            表单数据={"pwd": pwd, "vcode": vcode, "vcode_str": vcode_str},
            需要bdstoken=True,
            带渠道=False,
            raw=True,
        )
        数据 = 响应.json()
        randsk = 数据.get("randsk")
        if 数据.get("errno") != 0 or not randsk:
            raise 提取码错误(
                f"校验分享失败：errno={数据.get('errno')} "
                f"{数据.get('err_msg') or ''}")

        # 记住 BDCLND（= randsk 原文，URL 编码形态）
        self._bdclnd = 响应.cookies.get("BDCLND") or randsk
        sekey = unquote(randsk)
        logger.info(f"[分享] 校验通过：sekey len={len(sekey)}，"
                    f"BDCLND len={len(self._bdclnd)}")
        return sekey

    def 一键转存(
        self,
        分享链接: str,
        目标路径: str = "/",
        提取码: str = "",
        只转存fsid: list | None = None,
        等待完成: bool = True,
        超时秒: float = 300.0,
        进度回调=None,
    ) -> dict:
        """把分享内容转存到自己的网盘。

        完整链路（全部接口均已实测）：
            ① GET  /share/list     → share_id + uk + 文件清单
            ② POST /share/verify   → randsk（URL 解码 = sekey）
            ③ POST /share/transfer body fsidlist + path → task_id
            ④ GET  /share/taskquery?taskid=... → running → success

        :param 分享链接: 完整链接（可带 `?pwd=xxxx`）或裸短码
        :param 目标路径: 自己网盘里的目标目录，如 `/我的资源`
        :param 提取码:   无密码分享留空；链接带 `?pwd=` 时自动提取
        :param 只转存fsid: 只转存指定 fs_id（默认转存根层全部）
        :return: 含 `task_id` / `fsid列表` / `share_id` / `uk` / `状态`
        """
        信息 = 解析分享链接(分享链接)
        surl = 信息["surl"]
        码 = 提取码 or 信息["pwd"]
        logger.info(f"[转存] surl={surl} 目标={目标路径} "
                    f"提取码={'有' if 码 else '无'}")

        def 报(阶段, p):
            if 进度回调:
                try:
                    进度回调(阶段, p)
                except Exception:
                    pass

        # ---- ① 先校验（拿 sekey + 种下 BDCLND）----
        # ⚠️ 顺序不能反：/share/list 依赖 verify 种下的 BDCLND cookie
        报("校验提取码", 0.15)
        sekey = self.校验分享(surl, 码)

        # ---- ② 分享信息 ----
        报("获取分享信息", 0.3)
        分享信息 = self.取分享信息(surl)
        share_id = 分享信息.get("share_id")
        from_uk = 分享信息.get("uk")
        if not share_id or not from_uk:
            raise 接口错误(f"/share/list 未返回 share_id/uk：{分享信息}",
                           响应数据=分享信息)

        # ---- 选定 fs_id ----
        if 只转存fsid:
            fsid列表 = [int(f) for f in 只转存fsid]
        else:
            fsid列表 = [int(e["fs_id"]) for e in (分享信息.get("list") or [])
                       if e.get("fs_id") is not None]
        if not fsid列表:
            raise 接口错误("分享里没有可转存的文件", 响应数据=分享信息)

        # ---- ③ 转存 ----
        报("提交转存", 0.5)
        数据 = self.网络.请求(
            "POST", "/share/transfer",
            params={
                "shareid": str(share_id),
                "from": str(from_uk),
                "sekey": sekey,
                "ondup": "newcopy",
                "async": "1",
                "channel": "chunlei",
            },
            headers=self._分享请求头(),
            表单数据={
                "fsidlist": json.dumps(fsid列表, separators=(",", ":")),
                "path": 目标路径 or "/",
            },
            需要bdstoken=True,
            带渠道=False,
        )
        task_id = 数据.get("task_id") or 数据.get("taskid")
        logger.info(f"[转存] 已提交：task_id={task_id} "
                    f"show_msg={数据.get('show_msg')!r}")

        结果 = {
            "errno": 数据.get("errno"),
            "task_id": task_id,
            "share_id": share_id,
            "uk": from_uk,
            "fsid列表": fsid列表,
            "目标路径": 目标路径,
            "状态": "已提交",
        }

        # ---- ④ 轮询 ----
        if 等待完成 and task_id:
            报("转存中", 0.7)
            最终 = self.任务.等待任务(task_id, 间隔秒=3.0, 超时秒=超时秒)
            结果["状态"] = 最终.get("status")
            结果["任务结果"] = 最终
            logger.info(f"[转存] ✅ {结果['状态']}")
        报("完成", 1.0)
        return 结果

    # ==================== ① 创建分享 ====================

    def 创建分享(
        self,
        fid列表: Iterable[int | str],
        pwd: str | None = None,
        period: int = 有效期_永久,
        public: int = 0,
        is_knowledge: int = 0,
    ) -> dict:
        """POST /share/pset —— 创建分享。

        :param fid列表: **fs_id** 列表（文件或目录皆可）
        :param pwd:     4 位提取码。⚠️ **服务端必需**：
                        实测传空串会返回 `pwd length param error`；
                        不传（None）时本模块自动生成一个 4 位数字提取码。
        :param period:  有效期天数，0 = 永久
        :return: 含 `link` / `shorturl` / `shareid` 的原始响应。
                 ⚠️ 实测响应**不含 `pwd`**，调用方需自行记住传入的提取码
                 （或用 `返回的提取码` 键，本模块会补上）。
        """
        列表 = [int(f) for f in fid列表 if f not in (None, "")]
        if not 列表:
            raise ValueError("fid_list 不能为空")

        提取码 = (pwd or "").strip()
        if not 提取码:
            提取码 = f"{random.randint(0, 9999):04d}"
            logger.info(f"[分享] 未指定提取码，自动生成 {提取码}")
        if len(提取码) != 4:
            raise ValueError(f"提取码必须是 4 位：{提取码!r}")

        数据 = self.网络.请求(
            "POST", "/share/pset",
            params={"channel": "chunlei"},
            表单数据={
                "is_knowledge": str(is_knowledge),
                "public": str(public),
                "period": str(int(period)),
                "pwd": 提取码,
                "eflag_disable": "true",
                "linkOrQrcode": "link",
                "channel_list": "[]",
                "schannel": str(默认schannel),
                "fid_list": json.dumps(列表, separators=(",", ":")),
            },
            需要bdstoken=True,
        )
        # 服务端不回显提取码，这里补上方便调用方直接使用
        数据.setdefault("返回的提取码", 提取码)
        logger.info(
            f"[分享] 创建 ✅ shareid={数据.get('shareid')} "
            f"链接={数据.get('link')} 提取码={提取码}")
        return 数据

    # ==================== ② 分享列表 ====================

    def 取分享列表(self, page: int = 1, num: int = 100,
                  order: str = "ctime", desc: int = 1) -> dict:
        """GET /share/record —— 单页分享列表。"""
        数据 = self.网络.请求(
            "GET", "/share/record",
            params={
                "channel": "chunlei",
                "num": str(int(num)),
                "page": str(int(page)),
                "order": order,
                "desc": str(int(desc)),
                "is_batch": "1",
            },
        )
        logger.info(
            f"[分享] 列表 page={page} → 本页 {len(数据.get('list') or [])} 项 / "
            f"总计 count={数据.get('count')} nextpage={数据.get('nextpage')}")
        return 数据

    def 取全部分享(self, num: int = 100, 最多页数: int = 50) -> list[dict]:
        """翻页取回全部**有效**分享（条目字段解析请用 `取shareid()` 等辅助）。

        ⚠️ 终止条件是 `nextpage == 0`（实测 page=1 返回 25 条、page=2 返回 0 条），
        不能沿用 `/api/list` 的「本页是否满」判断。
        """
        结果: list[dict] = []
        page = 1
        while page <= 最多页数:
            数据 = self.取分享列表(page=page, num=num)
            条目们 = 数据.get("list") or []
            结果.extend(条目们)
            if not 数据.get("nextpage"):
                break
            if not 条目们:
                break
            page += 1
        return 结果

    # ==================== ③ 取消分享 ====================

    def 取消分享(self, shareid列表: Iterable[int | str]) -> dict:
        """POST /share/cancel —— 取消若干分享。

        :param shareid列表: **shareid** 列表（不是 fs_id）
        """
        列表 = [int(s) for s in shareid列表 if s not in (None, "")]
        if not 列表:
            raise ValueError("shareid_list 不能为空")
        数据 = self.网络.请求(
            "POST", "/share/cancel",
            params={"channel": "chunlei"},
            表单数据={"shareid_list": json.dumps(列表,
                                            separators=(",", ":"))},
            需要bdstoken=True,
        )
        logger.info(f"[分享] 取消 ✅ {len(列表)} 个 shareid")
        return 数据

    def 取消单个(self, shareid: int | str) -> dict:
        return self.取消分享([shareid])

    # ==================== ④ 提取码查询 ====================

    def 取提取码(self, shareid: int | str, sign: str | None = None) -> dict:
        """GET /share/surlinfoinrecord —— 查询分享的短链与提取码。

        ⚠️ 该接口在 HAR 中带 `sign`（hex32），但**来源未知**（PLAN Q12）。
        这里先尝试**省略 sign**；若服务端拒绝则抛 `提取码需签名`，
        由界面层把该功能标为「暂不支持」，而不是静默失败。

        :raises 提取码需签名: 省略 sign 无法通过时
        """
        参数 = {
            "channel": "chunlei",
            "shareid": str(shareid),
        }
        if sign:
            参数["sign"] = sign

        尝试省略 = sign is None
        try:
            数据 = self.网络.请求(
                "GET", "/share/surlinfoinrecord",
                params=参数,
                需要bdstoken=True,
            )
        except 接口错误 as e:
            if 尝试省略:
                logger.warning(
                    f"[分享] 省略 sign 查询提取码失败（errno={e.errno}）—— "
                    f"该接口需要 sign，来源未知（Q12），暂不支持")
                raise 提取码需签名(
                    "查询提取码需要 sign 参数，其来源尚未解明（PLAN Q12）；"
                    "请在分享列表中直接复制创建时返回的提取码。") from e
            raise

        logger.info(f"[分享] 提取码查询 ✅ shareid={shareid} "
                    f"pwd={'有' if 数据.get('pwd') is not None else '无'}")
        return 数据

    @staticmethod
    def 支持提取码查询() -> bool:
        """界面层据此决定是否启用该功能。"""
        return False        # Q12 未解决前恒为 False


# ==========================================================================
# 兼容垫片
# --------------------------------------------------------------------------
# `界面/页面/分享页.py` 尚未重写（Phase 7），它 import 了下面三个名字。
# 为避免「删掉符号 → 整个界面层 import 失败」，此处保留最小定义。
#
# ⚠️ `分享类型映射` / `下载类型映射` 对应的是**原骨架**的 shareType / downloadType
#    概念，百度分享模型里没有对应物，故留空表（旧页面会退化为显示原值）。
#    这两个名字将在 Phase 7 重写分享页时一并移除。
# ==========================================================================
有效期映射 = 有效期选项

分享类型映射: dict = {}
下载类型映射: dict = {}
