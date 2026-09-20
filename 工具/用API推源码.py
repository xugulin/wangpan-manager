#!/usr/bin/env python3
"""用 GitHub API 把本地 main 推到远端（git 通道不通时的备用推送）。

为什么需要它：本机 **github.com:443 时通时不通**（git push 经常失败），
但 `api.github.com` 一直是通的。于是绕开 git 传输，直接：
  ① 把有差异的文件做成 blob；
  ② 以远端 HEAD 的 tree 为 base 建一棵新 tree；
  ③ 建 commit 并把 main 引用指过去。

只上传**有差异的**文件（`git diff --name-status`），所以通常只有十几个 blob。
"""
from __future__ import annotations

import base64
import json
import pathlib
import subprocess
import sys
import urllib.error
import urllib.request

令牌 = (pathlib.Path.home() / "python/令牌/github-token.txt").read_text().strip()
拥有人, 仓库名, 分支 = "xugulin", "wangpan-manager", "main"
API = "https://api.github.com"
项目 = pathlib.Path("/home/xgl/python/网盘管理")


def 请求(方法, 地址, 数据=None, 类型="application/json"):
    头 = {"Authorization": f"token {令牌}", "Accept": "application/vnd.github+json",
         "User-Agent": "wangpan-manager-push"}
    if 数据 is not None:
        头["Content-Type"] = 类型
    req = urllib.request.Request(地址, data=数据, headers=头, method=方法)
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, {"错误": e.read().decode(errors="ignore")[:300]}


def git(*参数) -> str:
    return subprocess.run(["git", *参数], cwd=项目, capture_output=True,
                          text=True).stdout.strip()


# ① 远端现在的 commit / tree
状态, 远端 = 请求("GET", f"{API}/repos/{拥有人}/{仓库名}/branches/{分支}")
if 状态 != 200:
    print("取远端分支失败：", 远端)
    raise SystemExit(1)
远端SHA = 远端["commit"]["sha"]
远端树 = 远端["commit"]["commit"]["tree"]["sha"]
print("远端 main：", 远端SHA[:8])

本地SHA = git("rev-parse", "HEAD")
if 本地SHA == 远端SHA:
    print("已经一致，不需要推")
    raise SystemExit(0)

# ② 差异文件。
# ⚠️ 必须用 -z（NUL 分隔）：中文路径在默认 core.quotePath 下会被转义成
#    "\346\222\255..." 这种八进制串，拿去 open() 一定找不到文件 ——
#    实测踩过：13 个文件全被当成"删除"，建 tree 直接 422。
出 = subprocess.run(["git", "-c", "core.quotePath=false", "diff", "--name-status",
                    "-z", f"{远端SHA}", 本地SHA],
                   cwd=项目, capture_output=True, text=True).stdout
段们 = [x for x in 出.split("\0") if x != ""]
条目 = []
i = 0
while i < len(段们):
    状态码 = 段们[i]
    if 状态码[:1] in ("R", "C"):
        路径 = 段们[i + 2] if i + 2 < len(段们) else 段们[i + 1]
        i += 3
    else:
        路径 = 段们[i + 1] if i + 1 < len(段们) else ""
        i += 2
    if 路径:
        条目.append((状态码, 路径))
print(f"差异文件 {len(条目)} 个")

# ③ 逐个做 blob（删除的直接在 tree 里置空）
树条目 = []
for 状态码, 路径 in 条目:
    完整 = 项目 / 路径
    if 状态码 == "D" or not 完整.is_file():
        树条目.append({"path": 路径, "mode": "100644", "type": "blob", "sha": None})
        print("  删除", 路径)
        continue
    内容 = 完整.read_bytes()
    状态, 结果 = 请求("POST", f"{API}/repos/{拥有人}/{仓库名}/git/blobs",
                   json.dumps({"content": base64.b64encode(内容).decode(),
                               "encoding": "base64"}).encode())
    if 状态 not in (200, 201):
        print("  blob 失败：", 路径, 结果)
        raise SystemExit(1)
    树条目.append({"path": 路径, "mode": "100644", "type": "blob",
                 "sha": 结果["sha"]})
    print(f"  {状态码} {路径}  {len(内容)} 字节")

# ④ 建新 tree（以远端 tree 为 base）
状态, 新树 = 请求("POST", f"{API}/repos/{拥有人}/{仓库名}/git/trees",
               json.dumps({"base_tree": 远端树, "tree": 树条目}).encode())
if 状态 not in (200, 201):
    print("建 tree 失败：", 新树)
    raise SystemExit(1)
print("新 tree：", 新树["sha"][:8])

# ④ 建 commit
信息 = git("log", "-1", "--pretty=%B", 本地SHA) or "更新"
状态, 新提交 = 请求("POST", f"{API}/repos/{拥有人}/{仓库名}/git/commits",
                json.dumps({"message": 信息, "tree": 新树["sha"],
                            "parents": [远端SHA]}).encode())
if 状态 not in (200, 201):
    print("建 commit 失败：", 新提交)
    raise SystemExit(1)
print("新 commit：", 新提交["sha"][:8])

# ⑤ 指过去（不 force：远端此刻应仍是父提交）
状态, _ = 请求("PATCH", f"{API}/repos/{拥有人}/{仓库名}/git/refs/heads/{分支}",
             json.dumps({"sha": 新提交["sha"], "force": False}).encode())
print("更新 main：HTTP", 状态)
