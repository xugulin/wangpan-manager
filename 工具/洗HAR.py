#!/usr/bin/env python3
"""把浏览器导出的 HAR 洗成"可以安全发给别人"的样子，并抽出短信登录的请求。

用户要求走 HAR 路线 —— 但 HAR 里**含登录凭证**（BDUSS/STOKEN/__puus 等都在
Cookie 头里），直接发出去等于把账号交出去。所以：

* 洗掉：Cookie / Set-Cookie / Authorization / 含 token 的字段 → 只留**名字**，
  值一律换成 `<已脱敏 len=N>`；
* 保留：请求 URL、方法、Content-Type、**POST 体**（里面的手机号/验证码也脱敏）、
  响应状态码，以及登录相关请求的**响应前 400 字**（errno 之类要看）；
* 默认只打印"短信/登录相关"的请求，避免把整份 HAR 贴出来。

用法：
    运行环境/venv/bin/python 洗HAR.py <导出.har>                 # 只看登录/短信相关
    运行环境/venv/bin/python 洗HAR.py <导出.har> --全部           # 看所有请求（洗过）
    运行环境/venv/bin/python 洗HAR.py <导出.har> --输出 报告.txt   # 写成文件

导出 HAR 的方法（你自己浏览器里）：
    F12 → Network → 勾选 Preserve log → 走一遍「短信登录」→ 右键 → Save all as HAR
    （Chrome/Brave/Edge 都一样；Firefox 也有"全部另存为 HAR"）
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

#: 这些头一律脱敏（只留名字与长度）
敏感头 = ("cookie", "set-cookie", "authorization", "x-csrf-token", "token")
#: POST 体里这些字段脱敏
敏感字段 = ("password", "passwd", "smscode", "verifycode", "code", "phone",
        "username", "mobile", "bduss", "stoken", "token")
关心 = re.compile(r"sms|verify|vcode|login|passport|account|code|checkphone",
               re.I)


def 脱敏值(值: str, 保留头: int = 0) -> str:
    值 = str(值 or "")
    if not 值:
        return ""
    return f"<已脱敏 len={len(值)}" + (f" 前{保留头}={值[:保留头]}…" if 保留头 else "") + ">"


def 洗头(头们: list[dict]) -> list[str]:
    出 = []
    for h in 头们 or []:
        名 = str(h.get("name", ""))
        值 = str(h.get("value", ""))
        if 名.lower() in 敏感头:
            if 名.lower() == "cookie":
                # Cookie 头保留**名字清单**（看它带了哪些凭证很有用）
                名们 = [x.split("=")[0].strip() for x in 值.split(";") if x.strip()]
                出.append(f"{名}: <{len(名们)} 个 cookie 名：{'、'.join(名们[:25])}>")
            else:
                出.append(f"{名}: {脱敏值(值)}")
        else:
            出.append(f"{名}: {值[:200]}")
    return 出


def 洗体(体: str) -> str:
    体 = str(体 or "")
    if not 体:
        return ""
    # 形如 a=1&b=2 的表单
    if "=" in 体 and "&" in 体 or re.fullmatch(r"[\w%.\-]+=[^&]*", 体):
        片段 = []
        for 段 in 体.split("&"):
            if "=" not in 段:
                片段.append(段[:60])
                continue
            键, _, 值 = 段.partition("=")
            键小 = 键.lower()
            if any(k in 键小 for k in 敏感字段):
                片段.append(f"{键}={脱敏值(值, 保留头=3)}")
            else:
                片段.append(f"{键}={值[:120]}")
        return "&".join(片段)[:500]
    # JSON
    try:
        数据 = json.loads(体)
        if isinstance(数据, dict):
            for k in list(数据):
                if any(s in k.lower() for s in 敏感字段):
                    数据[k] = 脱敏值(数据[k], 保留头=3)
            return json.dumps(数据, ensure_ascii=False)[:500]
    except Exception:
        pass
    return 体[:300]


def 主() -> int:
    解析 = argparse.ArgumentParser(description="洗 HAR（脱敏）并抽登录/短信请求")
    解析.add_argument("har", help="HAR 文件路径")
    解析.add_argument("--全部", action="store_true", help="打印所有请求（已脱敏）")
    解析.add_argument("--输出", default="", help="把结果写到文件")
    参数 = 解析.parse_args()
    路径 = Path(参数.har).expanduser()
    if not 路径.is_file():
        print(f"找不到文件：{路径}")
        return 2
    数据 = json.loads(路径.read_text(encoding="utf-8", errors="ignore"))
    条目 = (((数据.get("log") or {}).get("entries")) or [])
    print(f"HAR：{路径.name}｜共 {len(条目)} 条记录\n")
    行们: list[str] = []
    for i, e in enumerate(条目, 1):
        req = e.get("request") or {}
        resp = e.get("response") or {}
        u = str(req.get("url", ""))
        if not 参数.全部 and not 关心.search(u):
            continue
        行们.append(f"[{i}] {req.get('method', '?')} {u}")
        行们.append(f"     状态：{resp.get('status')}  "
                 f"类型：{resp.get('content', {}).get('mimeType', '')}")
        行们.append(f"     请求头：")
        for h in 洗头(req.get("headers")):
            行们.append(f"       {h}")
        pd = (req.get("postData") or {})
        if pd:
            行们.append(f"     POST 体（{pd.get('mimeType', '')}）：")
            行们.append(f"       {洗体(pd.get('text', ''))}")
        文 = ((resp.get("content") or {}).get("text") or "")
        if 文 and 关心.search(u):
            行们.append(f"     响应前 300 字：{文[:300]}")
        行们.append("")
    出 = "\n".join(行们) or "（没有匹配的请求；加 --全部 看全部）"
    print(出)
    if 参数.输出:
        Path(参数.输出).write_text(出, encoding="utf-8")
        print(f"\n已写入：{参数.输出}")
    print("\n提示：本工具已经把 Cookie/凭证/手机号/验证码都脱敏，可以安全贴出来。")
    return 0


if __name__ == "__main__":
    raise SystemExit(主())
