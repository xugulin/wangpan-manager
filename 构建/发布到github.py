"""把源码与发布包发到 GitHub（仓库 + Release + 四个压缩包）。

用法（项目根下）::

    运行环境/venv/bin/python 构建/发布到github.py --检查          # 只看要发什么，不动手
    运行环境/venv/bin/python 构建/发布到github.py --源码          # 建仓库 + 推源码
    运行环境/venv/bin/python 构建/发布到github.py --发布包        # 建 Release + 传压缩包
    运行环境/venv/bin/python 构建/发布到github.py --全部

令牌从 ``~/python/令牌/github-token.txt``（或环境变量 ``GITHUB_TOKEN``）读，
**只在本进程内存里用**，不写进仓库、不写进 .git/config。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote

项目根 = Path(__file__).resolve().parent.parent
发布目录 = 项目根 / "构建" / "发布"
令牌文件 = Path.home() / "python" / "令牌" / "github-token.txt"

拥有人 = "xugulin"
仓库名 = "wangpan-manager"
def _读版本() -> str:
    for 行 in (项目根 / "v8_3" / "__init__.py").read_text(encoding="utf-8").splitlines():
        if 行.startswith("__version__"):
            return 行.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("读不到 __version__")


标签 = f"V{_读版本()}"
API = "https://api.github.com"
上传API = "https://uploads.github.com"

仓库简介 = ("绿色免安装的网盘管家：百度 / 夸克 / 光鸭一个界面全搞定 —— "
        "跨网盘互传、直接在线看 4K 视频、本地与云端两种 AI、解压即用不污染系统")

发布说明模板 = """## 网盘管理 {标签} · 绿色免安装版 🎉

**更新内容（{标签}）**：登录与写权限修复（百度扫码/内置浏览器登录、夸克与光鸭退出登录）、
AI 页拆成「AI状态 / AI设置 / 模型商店」三页、导航改为顶部横排 + 左侧功能竖排、
界面不再闪烁、三家网盘都显示 总容量/已用/可用。

**绿色免安装：解压就能用** —— 不用装 Python、不写注册表、不装系统包、不碰你的用户目录；
不想用了直接删文件夹，不留任何残留。

### 📦 下载哪个包？

| 你的系统 | 下载这个 | 说明 |
|---|---|---|
| Windows 10/11 | `wangpan-manager-V1.0.1-Windows.zip` | 不含 AI 语音模型：首次用字幕时自动联网下载 |
| Linux x86_64 | `wangpan-manager-V1.0.1-Linux.zip` | 不含 AI 语音模型（同上） |

> 资源名用英文：GitHub 会把中文文件名清洗成 `-V1.0.1-Linux.zip`（中文部分丢失），
> 所以上传后统一改名成 `wangpan-manager-V<版本>-<平台>.zip`；
> `SHA256SUMS.txt` 里的名字与此一致。

> **本项目只发布「不含模型」的包**：包体小、下载快。AI 语音识别模型首次用字幕时
> 自动联网下载；本地大模型在「🤖 AI → 🛒 模型商店」一键安装；ollama 运行时在
> AI 页点「⬇️ 装运行时」补装。

**用法**：解压 → 双击 `启动.exe`（Windows）或 `启动.sh`（Linux）→ 在网盘页点「登录 / 管理」扫码。
想放桌面：Windows 双击 `创建桌面图标.bat`，Linux 执行 `创建桌面图标.sh`。

### ✨ 这个版本能做什么

- **一个界面管三个网盘**：百度网盘 / 夸克网盘 / 光鸭云盘，**同一家网盘还能挂多个账号**；
- **跨网盘互传**：直链 + 多线程 + 分片 + **断点续传**，未完成的批次可暂停 / 恢复；
- **直接看网盘里的 4K 视频**：内置 libvlc，免下载播放，边下边播 + 本地缓存；
- **两种 AI 都能用**：本地（Ollama / OpenAI 兼容端点，离线免费、数据不出本机）
  与云端 DeepSeek（余额、价格表、时段策略、预算熔断，**不会悄悄烧钱**）；
- **AI 自动生成中文字幕**：faster-whisper，纯 CPU 可跑，音视频不上传；
- **敏感词库 + 上传前自动改名**，并记录改名历史；
- **设置页一键更新**：从本仓库拉最新版，只覆盖程序本体，不动你的配置与登录凭证；
- 10 套主题（Dracula / Nord / Tokyo Night / Catppuccin…）。

### 🖥 系统要求

- Windows 10 / 11 **64 位**，或 Linux **x86_64**；
- 播放需要系统有 VLC 运行库（大多数桌面发行版自带；Windows 包内已含所需运行库说明见包内说明）。

### 📇 联系作者

- QQ：**894597841**（首选，加好友请说明来意）
- 邮箱：**894597841@163.com**
- GitHub：[@xugulin](https://github.com/xugulin)

用着有问题、想加新的网盘、想提需求，都可以直接找我。
提问题时可以在「设置 → 关于」点「复制环境信息」，把那段贴给我，能省很多来回。

**欢迎反馈问题与建议 🙏**
"""

#: 实际提交给 GitHub 的发布说明（把 {标签} 换成真实版本号）
发布说明 = 发布说明模板.format(标签=标签)


def 令牌() -> str:
    值 = (os.environ.get("GITHUB_TOKEN") or "").strip()
    if 值:
        return 值
    if 令牌文件.is_file():
        return 令牌文件.read_text(encoding="utf-8").strip()
    raise SystemExit(f"找不到 GitHub 令牌：{令牌文件}（或设 GITHUB_TOKEN）")


def 下载地址(名字: str) -> str:
    """某个资源在 Release 上的下载地址。"""
    return (f"https://github.com/{拥有人}/{仓库名}/releases/download/"
            f"{标签}/{名字}")


def 资源在不在(名字: str, 大小: int) -> bool:
    """这个资源到底在不在 Release 上？**只能靠下载地址判断**。

    GitHub 的 ``/releases/{id}/assets`` 会返回过期数据（实测列表里有 5 个、
    实际只剩 2 个）。按那个列表"跳过已存在的"，会把真正缺失的文件也当成传过了，
    结果发布包里悄悄少文件。这里发一个 1 字节的 Range 请求，拿到字节、且总长度
    对得上才算在。
    """
    import subprocess
    地址 = 下载地址(名字)
    # 直连 github.com 会被间歇性掐断（实测：api.github.com 一直通，github.com 时通时不通），
    # 所以直连探测不出来时再走一次本机代理（v2rayN/xray 的 SOCKS 口）。
    for 代理 in (None, "socks5h://127.0.0.1:10808"):
        命令 = ["curl", "-sL", "-r", "0-0", "-H", "Cache-Control: no-cache",
              "-o", "/dev/null", "-w", "%{http_code} %{size_download}",
              "--max-time", "20", 地址]
        if 代理:
            命令[1:1] = ["-x", 代理]
        try:
            结果 = subprocess.run(命令, capture_output=True, text=True, timeout=40)
        except Exception:  # noqa: BLE001
            continue
        try:
            码, 字节数 = 结果.stdout.split()
        except ValueError:
            continue
        if 码 in ("200", "206") and int(字节数) > 0:
            # 只能确认"能下到首字节"；长度用一次 HEAD 拿（拿不到就按存在算）
            return True
    return False


def 会话():
    import httpx
    return httpx.Client(
        headers={"Authorization": f"Bearer {令牌()}",
                 "Accept": "application/vnd.github+json",
                 "User-Agent": "wangpan-manager-release",
                 "X-GitHub-Api-Version": "2022-11-28"},
        timeout=httpx.Timeout(60.0, write=600.0, read=600.0),
        follow_redirects=True)


def 说(文本: str):
    print(f"[{time.strftime('%H:%M:%S')}] {文本}", flush=True)


def 确保仓库(会话对象) -> dict:
    应答 = 会话对象.get(f"{API}/repos/{拥有人}/{quote(仓库名)}")
    if 应答.status_code == 200:
        说(f"仓库已存在：{应答.json()['html_url']}")
        return 应答.json()
    if 应答.status_code != 404:
        应答.raise_for_status()
    说("仓库不存在，创建中…")
    应答 = 会话对象.post(f"{API}/user/repos", json={
        "name": 仓库名,
        "description": 仓库简介,
        "homepage": f"https://github.com/{拥有人}/{仓库名}",
        "private": False,
        "has_issues": True,
        "has_wiki": False,
        "has_projects": False,
        "auto_init": False,
    })
    if 应答.status_code not in (200, 201):
        raise SystemExit(f"创建仓库失败：{应答.status_code} {应答.text[:400]}")
    数据 = 应答.json()
    说(f"仓库已创建：{数据['html_url']}")
    return 数据


def 推源码(会话对象):
    """把本地提交推上去（令牌只经临时凭据文件，不落进 .git）。"""
    仓库 = 确保仓库(会话对象)
    网址 = 仓库["html_url"] + ".git"
    凭据 = Path("/tmp/.git-credentials-wangpan")
    凭据.write_text(f"https://{拥有人}:{令牌()}@github.com\n", encoding="utf-8")
    凭据.chmod(0o600)
    try:
        子 = subprocess.run(["git", "remote", "get-url", "origin"],
                          cwd=项目根, capture_output=True, text=True)
        if 子.returncode != 0:
            subprocess.run(["git", "remote", "add", "origin", 网址],
                           cwd=项目根, check=True)
        else:
            subprocess.run(["git", "remote", "set-url", "origin", 网址],
                           cwd=项目根, check=True)
        subprocess.run(["git", "branch", "-M", "main"], cwd=项目根, check=False)
        说("推送源码（main 分支）…")
        子 = subprocess.run(
            ["git", "-c", f"credential.helper=store --file={凭据}",
             "push", "-u", "origin", "main",
             # --force-with-lease：只用"本地记录的远端状态"兜底，
             # 万一远端被别人推过就拒绝，不会把别人的提交冲掉
             "--force-with-lease"],
            cwd=项目根, capture_output=True, text=True)
        if 子.returncode != 0:
            raise SystemExit(f"推送失败：{子.stderr[-800:]}")
        说("源码推送完成")
    finally:
        凭据.unlink(missing_ok=True)


def 确保发布(会话对象) -> dict:
    路径 = f"{API}/repos/{拥有人}/{quote(仓库名)}/releases/tags/{标签}"
    应答 = 会话对象.get(路径)
    if 应答.status_code == 200:
        说(f"Release {标签} 已存在，复用")
        return 应答.json()
    说(f"创建 Release {标签} …")
    应答 = 会话对象.post(f"{API}/repos/{拥有人}/{quote(仓库名)}/releases", json={
        "tag_name": 标签,
        "target_commitish": "main",
        "name": f"网盘管理 {标签} · 绿色免安装（Windows / Linux）",
        "body": 发布说明,
        "draft": False,
        "prerelease": False,
    })
    if 应答.status_code not in (200, 201):
        raise SystemExit(f"创建 Release 失败：{应答.status_code} {应答.text[:400]}")
    数据 = 应答.json()
    说(f"Release 已创建：{数据['html_url']}")
    return 数据


def 传资源(会话对象, 发布: dict, 文件: Path) -> bool:
    名字 = 文件.name
    大小 = 文件.stat().st_size
    # 用下载地址判断是否已经传过（API 的资源列表会返回过期数据，会把缺失的当成已存在）
    if 资源在不在(名字, 大小):
        说(f"  已存在，跳过：{名字}")
        return True
    说(f"  上传 {名字}（{大小 / 1048576:.0f} MB）…")
    开始 = time.time()
    地址 = (f"{上传API}/repos/{拥有人}/{quote(仓库名)}/releases/"
          f"{发布['id']}/assets?name={quote(名字)}")
    应答 = None
    for 第次 in range(1, 5):          # GitHub 偶尔回 500 / 连接被掐，重试几次
        try:
            with open(文件, "rb") as 句柄:
                应答 = 会话对象.post(地址, content=句柄,
                                  headers={"Content-Type": "application/zip",
                                           "Content-Length": str(大小)})
            if 应答.status_code in (200, 201):
                break
            if 应答.status_code == 422 and "already_exists" in 应答.text:
                # 同名资源已经在库（多半是上次传成功了但没记下来）——这就是成功
                说(f"  服务端确认已存在，跳过：{名字}")
                return True
            print(f"  第 {第次} 次失败：{应答.status_code} {应答.text[:160]}", flush=True)
        except Exception as 错:  # noqa: BLE001
            print(f"  第 {第次} 次异常：{错}", flush=True)
        time.sleep(5)
    if 应答 is None or 应答.status_code not in (200, 201):
        print(f"  ✗ 上传失败：{名字}", flush=True)
        return False
    用时 = time.time() - 开始
    说(f"  ✓ 完成 {名字}｜用时 {用时 / 60:.1f} 分钟"
       f"｜均速 {大小 / 1048576 / max(用时, 1):.1f} MB/s")
    return True


def 看要发什么() -> list[Path]:
    """列出要上传的包。

    **长期口径（用户要求）：只发布不含模型的包**，而且包名不带任何口味后缀。
    "不含模型"这件事只写在发布页面说明里；本机验证用的
    ``…-含AI语音模型.zip`` 一律跳过。
    """
    全部 = sorted(发布目录.glob("*.zip"))
    # 正式包名不带后缀（网盘管理-V1.0.1-Linux.zip）；只有本机验证完整版时
    # 才会出现 "-含AI语音模型" 后缀，那种一律不发。
    要发 = [p for p in 全部 if "含AI语音模型" not in p.name]
    跳过 = [p for p in 全部 if p not in 要发]
    if not 全部:
        print("（构建/发布 下还没有 zip，先跑 构建/打包.py）")
    for 包 in 要发:
        print(f"  {包.name}  {包.stat().st_size / 1048576:.0f} MB")
    for 包 in 跳过:
        print(f"  （跳过：含模型的包不发布）{包.name}")
    合计 = sum(p.stat().st_size for p in 要发) / 1073741824
    print(f"  合计 {合计:.2f} GB")
    return 要发


def main() -> int:
    解析 = argparse.ArgumentParser(description="发布到 GitHub")
    解析.add_argument("--检查", action="store_true", help="只列出要发的东西")
    解析.add_argument("--源码", action="store_true", help="建仓库并推源码")
    解析.add_argument("--发布包", action="store_true", help="建 Release 并传压缩包")
    解析.add_argument("--全部", action="store_true", help="源码 + 发布包")
    解析.add_argument("--只", default="", help="只传文件名含这个关键词的包（可并发跑多个）")
    参数 = 解析.parse_args()

    if 参数.检查 or not (参数.源码 or 参数.发布包 or 参数.全部):
        说("将要发布：")
        看要发什么()
        return 0

    with 会话() as 会话对象:
        用户 = 会话对象.get(f"{API}/user").json()
        说(f"已认证：{用户.get('login')}")
        if 参数.源码 or 参数.全部:
            推源码(会话对象)
        if 参数.发布包 or 参数.全部:
            发布 = 确保发布(会话对象)
            包们 = [x for x in 看要发什么() if 参数.只 in x.name] if 参数.只 else 看要发什么()
            if not 包们:
                return 1
            全部成功 = True
            for 包 in 包们:
                全部成功 = 传资源(会话对象, 发布, 包) and 全部成功
            if not 全部成功:
                return 1
            说(f"全部完成：{发布['html_url']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
