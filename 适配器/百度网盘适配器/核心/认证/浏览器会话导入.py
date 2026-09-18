# 百度网盘适配器/核心/认证/浏览器会话导入.py
"""从本机浏览器的调试端口取回**完整**登录会话（含 HttpOnly 的 BDUSS / STOKEN）。

为什么需要这个（真机实测，2026-09-19）
=====================================
百度的**写权限**（上传、改名、删除、建目录）要求会话里同时有
``BDUSS`` **和** ``pan.baidu.com`` 域上的 ``STOKEN``：

* 只有 BDUSS（或用 passport 域那份 STOKEN）→ 读接口正常（``/api/list`` errno 0），
  写接口一律 ``errno:-6``（表现为"能列目录、上传/改名/删除全废"）；
* 扫码登录换来的 BDUSS 不带 pan 域 STOKEN，或者那份 STOKEN 随后被
  别处登录轮换掉了 → 现场就是"提示已登录、用户:-、写操作不可用"。

而**浏览器里那份会话是好的**：真机上用浏览器 Cookie 打
``/api/create``、``/api/filemanager`` 全部 ``errno:0``，实测建目录/改名/删除全通。

难点在于：浏览器 profile 里的 cookie **值是加密的**（Chromium v20 加密 +
系统密钥环），直接读 sqlite 只能拿到空字符串。所以这里走
**Chrome DevTools Protocol**（``Network.getCookies``）—— 由浏览器自己解密，
把 cookie（包括 HttpOnly 的）通过本机回环端口交给同一个用户。这正是
"浏览器插件式"的取法，但不需要用户安装任何插件。

安全说明
========
* 只用 ``127.0.0.1`` 上的调试端口；不联网、不上传任何东西；
* 只取 ``baidu.com`` 域的 cookie，只写进本适配器自己的
  ``数据/会话.json``（0600）；
* 浏览器必须**已经开着调试端口**（本项目自带的 DSH 浏览器面板默认就开着）；
* 取完会立刻用真接口验证（读列表 + 取 bdstoken），失败就如实报错、
  不覆盖原有会话。
"""
from __future__ import annotations

import json
import subprocess
import time
import urllib.request
from typing import Any, Optional

__all__ = [
    "取浏览器会话", "找调试端口", "默认候选端口", "会话字段",
]

#: 常见调试端口（本项目的浏览器面板用 38489；其余是 Chrome/Brave 手动开的常见值）
默认候选端口: tuple[int, ...] = (38489, 9222, 9223, 9229)


def _http_json(地址: str, 超时: float = 6.0) -> Any:
    with urllib.request.urlopen(地址, timeout=超时) as 应答:
        return json.loads(应答.read().decode("utf-8", "replace"))


#: 上次成功用过的端口（浏览器面板每次启动会随机换端口，记住它省一次扫描）
_上次端口: dict = {"端口": 0}


def 候选端口(候选: tuple[int, ...] | None = None) -> tuple[int, ...]:
    """端口候选表：显式指定 > 环境变量 V8_3_浏览器调试端口 > 上次用过的 >
    常见端口。"""
    if 候选:
        return tuple(候选)
    表: list[int] = []
    try:
        import os
        环境 = str(os.environ.get("V8_3_浏览器调试端口") or "").strip()
        if 环境.isdigit():
            表.append(int(环境))
    except Exception:
        pass
    if _上次端口.get("端口"):
        表.append(int(_上次端口["端口"]))
    表.extend(默认候选端口)
    去重: list[int] = []
    for 项 in 表:
        if 项 not in 去重:
            去重.append(项)
    return tuple(去重)


def 找调试端口(候选: tuple[int, ...] | None = None,
            扫进程: bool = True) -> tuple[int, str]:
    """找一个**正开着调试端口**的 Chromium 系浏览器。

    :return: ``(端口, 浏览器说明)``；找不到返回 ``(0, "")``。
    先试常见端口，再退一步从进程命令行里抓 ``--remote-debugging-port``。
    """
    # ① 候选端口先试（显式候选 / 环境变量 / 上次用过 / 常见端口）
    for 端口 in 候选端口(候选):
        try:
            信息 = _http_json(f"http://127.0.0.1:{端口}/json/version", 超时=4.0)
            _上次端口["端口"] = 端口
            return 端口, f"{信息.get('Browser', 'Chromium')}（端口 {端口}）"
        except Exception:
            continue
    端口们 = list(候选端口(候选)) if not 扫进程 else []

    if not 扫进程:
        return 0, ""

    # ② 从进程命令行里找（最可靠：端口可能被改成别的）
    for 关键词 in ("brave", "chrome", "chromium", "edge"):
        try:
            子 = subprocess.run(["pgrep", "-af", 关键词],
                                capture_output=True, text=True, timeout=8)
        except Exception:
            continue
        for 行 in (子.stdout or "").splitlines():
            if "--remote-debugging-port=" not in 行:
                continue
            if "--type=" in 行:          # 只要主进程
                continue
            try:
                片段 = 行.split("--remote-debugging-port=", 1)[1].split()[0]
                端口 = int(片段.split(",")[0])
            except Exception:
                continue
            if 端口 and 端口 not in 端口们:
                端口们.insert(0, 端口)

    for 端口 in 端口们:
        try:
            信息 = _http_json(f"http://127.0.0.1:{端口}/json/version", 超时=4.0)
            _上次端口["端口"] = 端口
            return 端口, f"{信息.get('Browser', 'Chromium')}（端口 {端口}）"
        except Exception:
            continue
    return 0, ""


def _cdp调用(地址: str, 方法: str, 参数: dict | None = None,
           超时: float = 20.0) -> Any:
    from websocket import create_connection       # websocket-client（项目已带）
    连接 = create_connection(地址, timeout=超时)
    try:
        连接.send(json.dumps({"id": 1, "method": 方法, "params": 参数 or {}}))
        截止 = time.time() + 超时
        while time.time() < 截止:
            数据 = json.loads(连接.recv())
            if 数据.get("id") == 1:
                if "error" in 数据:
                    raise RuntimeError(数据["error"])
                return 数据.get("result")
        raise TimeoutError(方法)
    finally:
        连接.close()


def _抓cookie(端口: int) -> tuple[list[dict], str]:
    """优先连**页面** target（Network 域只在页面级有效），再退回浏览器级。"""
    目标们 = _http_json(f"http://127.0.0.1:{端口}/json/list")
    页面 = [t for t in 目标们
           if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
    if 页面:
        try:
            结果 = _cdp调用(页面[0]["webSocketDebuggerUrl"], "Network.getCookies",
                         {"urls": ["https://pan.baidu.com/", "https://baidu.com/",
                                   "https://passport.baidu.com/",
                                   "https://www.baidu.com/"]})
            return (结果 or {}).get("cookies") or [], "页面"
        except Exception:
            pass
    版本 = _http_json(f"http://127.0.0.1:{端口}/json/version")
    结果 = _cdp调用(版本["webSocketDebuggerUrl"], "Storage.getCookies",
                  {"browserContextId": None})
    return (结果 or {}).get("cookies") or [], "浏览器"


def 会话字段(饼: list[dict]) -> dict:
    """把 cookie 列表整理成会话仓库认的字段。

    ⚠️ 关键：``STOKEN`` 同名 cookie 有 **pan 域** 与 **passport 域** 两份，
    只有 **pan 域**那份能让写接口通过（实测 passport 域那份照样 errno:-6）。
    """
    表: dict[str, list[dict]] = {}
    for c in 饼 or []:
        if "baidu" not in str(c.get("domain", "")):
            continue
        表.setdefault(str(c.get("name") or ""), []).append(c)

    def 取(名: str, 域名优先: tuple[str, ...]) -> str:
        候选 = 表.get(名) or []
        for 域 in 域名优先:
            命中 = [c for c in 候选 if str(c.get("domain")) == 域]
            if 命中:
                return str(命中[0].get("value") or "")
        return str(候选[0].get("value") or "") if 候选 else ""

    字段 = {
        "bduss": 取("BDUSS", (".baidu.com", "baidu.com")),
        "bduss_bfess": 取("BDUSS_BFESS", (".baidu.com", "baidu.com")),
        "stoken": 取("STOKEN", (".pan.baidu.com", ".baidu.com",
                              ".passport.baidu.com")),
        "baiduid": 取("BAIDUID", (".baidu.com", "baidu.com")),
        "baiduid_bfess": 取("BAIDUID_BFESS", (".baidu.com", "baidu.com")),
    }
    if not 字段["bduss_bfess"]:
        字段["bduss_bfess"] = 字段["bduss"]
    return {k: v for k, v in 字段.items() if v}


def 取浏览器会话(端口: int = 0, 超时: float = 20.0) -> dict:
    """抓一次浏览器会话。

    :return: ``{"成功": bool, "消息": str, "会话": {...}, "浏览器": str,
                "端口": int, "cookie数": int}``
    """
    if 端口:
        端口们 = (端口,)
        说明 = f"指定端口 {端口}"
    else:
        找到端口, 说明 = 找调试端口()
        if 找到端口:
            端口们 = (找到端口,)
        else:
            端口们 = ()
            return {"成功": False, "端口": 0, "浏览器": "",
                    "消息": "没有找到开着调试端口的浏览器。\n"
                          "请用带调试端口启动的 Chrome/Brave（本项目的浏览器面板"
                          "默认开着），并在里面登录 pan.baidu.com 后重试。",
                    "会话": {}, "cookie数": 0}

    for 端口 in 端口们:
        try:
            饼, 层级 = _抓cookie(端口)
        except Exception as e:  # noqa: BLE001
            return {"成功": False, "端口": 端口, "浏览器": 说明,
                    "消息": f"连接浏览器调试端口失败：{type(e).__name__}: {e}",
                    "会话": {}, "cookie数": 0}
        会话 = 会话字段(饼)
        if not 会话.get("bduss"):
            return {"成功": False, "端口": 端口, "浏览器": 说明,
                    "消息": f"在浏览器（{说明}）里没找到 BDUSS —— "
                          f"说明这个浏览器当前**没有登录** pan.baidu.com。\n"
                          f"请先在那个浏览器里登录百度网盘，再回来点一次。",
                    "会话": {}, "cookie数": len(饼)}
        if not 会话.get("stoken"):
            return {"成功": False, "端口": 端口, "浏览器": 说明,
                    "消息": "拿到了 BDUSS，但**没有 pan 域 STOKEN**：\n"
                          "这种会话能读不能写（上传/改名/删除会 errno:-6）。\n"
                          "请在浏览器里打开 pan.baidu.com 随便点一下（让网盘域"
                          "下发 cookie），或重新登录一次再试。",
                    "会话": 会话, "cookie数": len(饼)}
        return {"成功": True, "端口": 端口, "浏览器": f"{说明}（{层级}级取 cookie）",
                "消息": (f"已从浏览器取到完整会话：BDUSS {len(会话['bduss'])} 字节、"
                       f"pan 域 STOKEN {len(会话['stoken'])} 字节"
                       f"（共 {len(饼)} 条 cookie）"),
                "会话": 会话, "cookie数": len(饼)}
    return {"成功": False, "端口": 0, "浏览器": "", "消息": "没有可用的浏览器调试端口",
            "会话": {}, "cookie数": 0}
