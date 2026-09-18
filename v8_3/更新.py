"""软件更新：查 GitHub Releases → 下载 → 交给独立进程套用。

设计要点
========
* **纯逻辑，不碰界面**：版本比较、挑资源、解压、生成更新脚本都能单独跑，
  设置页和自检共用同一套代码；
* 更新源就是本项目的 GitHub 仓库（见 :data:`仓库`）。Release 里每个平台放两个包：
  「完整版（含 AI 语音模型）」与「精简版（不含模型）」，靠文件名里的
  ``linux`` / ``windows`` 与 ``完整`` / ``精简`` 区分；
* **套用更新不在本进程里做**：要覆盖的正是当前正在运行的解释器与依赖。
  流程是「下载 → 解压到 ``更新/<版本>/`` → 写一个平台脚本 → 拉起它 → 本程序退出」，
  脚本等本进程退出后再把新版本**合并**覆盖到项目上，最后重启。

  发布包里**不含** ``配置.json`` 与 ``数据/``，所以合并覆盖不会动用户的配置、
  网盘登录凭证与日志 —— 这也是"绿色版"的一部分。
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any, Callable, Optional

__all__ = [
    "仓库拥有人", "仓库名", "仓库", "仓库地址", "发布页", "当前版本", "版本显示",
    "版本元组", "比较版本", "有新版", "平台标记", "平台中文",
    "选择资源", "检查最新发布", "下载资源", "解压到", "写更新脚本", "拉起更新脚本",
    "更新目录", "保留说明",
]

#: GitHub 账号与仓库名（一键更新的地址就是它）
仓库拥有人 = "xugulin"
仓库名 = "wangpan-manager"    # GitHub 不接受非 ASCII 仓库名（会变成 "-"），中文名放在简介与 README 标题里
仓库 = f"{仓库拥有人}/{仓库名}"
仓库地址 = f"https://github.com/{仓库}"
发布页 = f"{仓库地址}/releases"
检查接口 = f"https://api.github.com/repos/{仓库}/releases/latest"

#: 发布包里不含这两样，更新时也就不会覆盖用户自己的东西
保留说明 = "更新只覆盖程序本体，不动 配置.json、数据/（日志、词库）与各家适配器的登录凭证"

#: 联系与作者信息（设置页展示用；只此一份，别处引用这里）
QQ = "894597841"
邮箱 = "894597841@163.com"


def 当前版本() -> str:
    """本程序的版本（形如 ``1.0.0``）。"""
    try:
        from . import __version__
        return str(__version__)
    except Exception:  # noqa: BLE001
        return "0.0.0"


def 版本显示(版本: str = "") -> str:
    """给人看的版本号：``1.0.0`` → ``V1.0.0``。"""
    return "V" + (版本 or 当前版本()).lstrip("vV")


def 版本元组(文本: str) -> tuple[int, ...]:
    """``v1.2.3`` / ``1.2`` → ``(1, 2, 3)``；认不出来的部分当 0。"""
    数字: list[int] = []
    for 段 in str(文本 or "").strip().lstrip("vV").split("."):
        位 = ""
        for 字 in 段:
            if 字.isdigit():
                位 += 字
            else:
                break
        数字.append(int(位) if 位 else 0)
    return tuple(数字) or (0,)


def 比较版本(甲: str, 乙: str) -> int:
    """甲 比 乙：新 → 1，相同 → 0，旧 → -1。"""
    左, 右 = 版本元组(甲), 版本元组(乙)
    长度 = max(len(左), len(右))
    左 = 左 + (0,) * (长度 - len(左))
    右 = 右 + (0,) * (长度 - len(右))
    return (左 > 右) - (左 < 右)


def 有新版(远端标签: str, 本地: str = "") -> bool:
    return 比较版本(远端标签, 本地 or 当前版本()) > 0


def 平台标记() -> str:
    """当前平台：``windows`` / ``linux`` / ``darwin``（认不出就返回系统名）。"""
    名 = platform.system().lower()
    if 名.startswith("win"):
        return "windows"
    if 名.startswith("darwin") or 名.startswith("mac"):
        return "darwin"
    if 名.startswith("linux"):
        return "linux"
    return 名 or "linux"


def 平台中文(标记: str = "") -> str:
    return {"windows": "Windows", "linux": "Linux",
            "darwin": "macOS"}.get(标记 or 平台标记(), 标记 or 平台标记())


def _名字(资源: dict) -> str:
    return str(资源.get("name") or "")


def 选择资源(资源列表: list[dict], 标记: str = "",
            要完整版: Optional[bool] = None) -> Optional[dict]:
    """按当前平台（可选：完整版/精简版）挑一个发布资源。

    认文件名里的 ``linux`` / ``windows`` / ``macos`` 与 ``完整`` / ``精简``
    （``full`` / ``lite`` 也认）。挑不到就返回 ``None``。
    """
    标记 = (标记 or 平台标记()).lower()
    # 分卷（xxx.zip.part01）与说明文件不参与"整包"挑选；整包找不到时另走分卷逻辑
    候选 = [x for x in (资源列表 or [])
          if not _是分卷(x) and not _是辅助文件(x)]
    同平台 = [x for x in 候选
            if 标记 in _名字(x).lower() or f"({标记})" in _名字(x).lower()]
    if not 同平台:
        return None
    精简的 = [x for x in 同平台
            if "精简" in _名字(x) or "lite" in _名字(x).lower()]
    完整 = [x for x in 同平台 if x not in 精简的]
    if 要完整版 is None:
        return (完整 or 同平台)[0]
    return (完整 or 同平台)[0] if 要完整版 else (精简的 or 同平台)[0]


def _是分卷(资源: dict) -> bool:
    名 = _名字(资源)
    return ".part" in 名 and 名[-2:].isdigit()


def _是辅助文件(资源: dict) -> bool:
    名 = _名字(资源).lower()
    return 名.endswith((".txt", ".md", ".sha256", ".bat", ".sh", ".cmd"))


def 分卷组(资源列表: list[dict], 标记: str = "",
         要完整版: Optional[bool] = None) -> list[dict]:
    """发布包被切成小分卷时，挑出属于同一个包的全部分卷（按卷号排序）。

    大文件直传常被 GitHub 限量，所以包可能以 ``xxx.zip.part01`` … 的形式发布；
    这里按"名字里同时含平台与完整/精简标记"把同一组的卷凑齐。
    """
    标记 = (标记 or 平台标记()).lower()
    卷 = [x for x in (资源列表 or []) if _是分卷(x)
        and 标记 in _名字(x).lower()]
    if not 卷:
        return []
    精简 = [x for x in 卷 if "lite" in _名字(x).lower() or "精简" in _名字(x)]
    完整 = [x for x in 卷 if x not in 精简]
    选中 = (完整 or 卷) if 要完整版 is None else ((完整 or 卷) if 要完整版
                                          else (精简 or 卷))
    # 取同一"包名前缀"的那一组
    前缀 = _名字(选中[0]).split(".part")[0]
    组 = [x for x in 卷 if _名字(x).startswith(前缀 + ".part")]
    return sorted(组, key=_名字)


def 下载发布包(资源列表: list[dict], 目录: str | Path,
            标记: str = "", 要完整版: Optional[bool] = None,
            进度: Optional[Callable[[int, int], None]] = None) -> Path:
    """下载当前平台的发布包，返回可解压的 zip 路径。

    * 有整包就直接下整包；
    * 只有分卷（``.partNN``）就把所有卷按顺序下下来拼成一个 zip。
    """
    目录 = Path(目录)
    目录.mkdir(parents=True, exist_ok=True)
    组: list[dict] = []
    单个 = 选择资源(资源列表, 标记, 要完整版)
    if 单个 is not None:
        try:
            return 下载资源(单个, 目录, 进度)
        except Exception:                      # noqa: BLE001
            # GitHub 的资源列表会返回过期数据：列表里有"整包"、实际只发布了分卷。
            # 这时退回分卷方式，别让用户对着 404 干瞪眼。
            组 = 分卷组(资源列表, 标记, 要完整版)
            if not 组:
                raise
    组 = 组 or 分卷组(资源列表, 标记, 要完整版)
    if not 组:
        raise RuntimeError("这个平台还没有可下载的发布包")
    总数 = sum(int(x.get("size") or 0) for x in 组)
    已下 = 0
    zip路径 = 目录 / _名字(组[0]).split(".part")[0]

    def 折算(本次: int, _本次总数: int) -> None:
        if 进度 is not None:
            进度(已下 + 本次, 总数)

    with open(zip路径, "wb") as 汇总:
        for 卷 in 组:
            临时 = 下载资源(卷, 目录 / "分卷", 折算)
            with open(临时, "rb") as f:
                while True:
                    块 = f.read(4 * 1024 * 1024)
                    if not 块:
                        break
                    汇总.write(块)
            已下 += int(卷.get("size") or 临时.stat().st_size)
            临时.unlink(missing_ok=True)
    return zip路径


def _httpx():
    """按需 import httpx：只在真的要联网时才需要它。"""
    import httpx
    return httpx


def 检查最新发布(超时: float = 20.0) -> dict[str, Any]:
    """查 GitHub 上最新的 Release。

    返回 ``{版本, 名称, 说明, 发布时间, 页面, 资源: [...]}``；
    失败时抛异常（调用方负责提示），**不会**悄悄当成"已是最新"。
    """
    httpx = _httpx()
    应答 = httpx.get(检查接口, timeout=超时, follow_redirects=True,
                   headers={"Accept": "application/vnd.github+json",
                            "User-Agent": "wangpan-manager-updater"})
    if 应答.status_code == 404:
        raise RuntimeError(f"仓库或 Release 还不存在：{仓库}")
    应答.raise_for_status()
    数据 = 应答.json()
    return {
        "版本": str(数据.get("tag_name") or ""),
        "名称": str(数据.get("name") or ""),
        "说明": str(数据.get("body") or ""),
        "发布时间": str(数据.get("published_at") or ""),
        "页面": str(数据.get("html_url") or 发布页),
        "资源": [{"name": 名.get("name"), "url": 名.get("browser_download_url"),
                "size": int(名.get("size") or 0)}
               for 名 in (数据.get("assets") or [])],
    }


def 下载资源(资源: dict, 目录: str | Path,
           进度: Optional[Callable[[int, int], None]] = None,
           超时: float = 60.0) -> Path:
    """下载一个发布资源到 ``目录``，返回本地路径。``进度(已下, 总数)`` 可选。"""
    httpx = _httpx()
    目录 = Path(目录)
    目录.mkdir(parents=True, exist_ok=True)
    目标 = 目录 / (资源.get("name") or "更新包.zip")
    总数 = int(资源.get("size") or 0)
    with httpx.stream("GET", str(资源["url"]), timeout=超时,
                     follow_redirects=True,
                     headers={"User-Agent": "wangpan-manager-updater"}) as 响应:
        响应.raise_for_status()
        已下 = 0
        with open(目标, "wb") as 文件:
            for 块 in 响应.iter_bytes(1024 * 256):
                文件.write(块)
                已下 += len(块)
                if 进度 is not None:
                    进度(已下, 总数)
    return 目标


def 解压到(压缩包: str | Path, 目标目录: str | Path) -> Path:
    """把发布包解开到 ``目标目录``；里面如果套了一层同名文件夹会自动脱掉。

    返回**真正的项目根**（脱壳之后那一层）。
    """
    压缩包, 目标目录 = Path(压缩包), Path(目标目录)
    if 目标目录.exists():
        shutil.rmtree(目标目录, ignore_errors=True)
    目标目录.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(压缩包) as 包:
        包.extractall(目标目录)
    子项 = [x for x in 目标目录.iterdir()]
    if len(子项) == 1 and 子项[0].is_dir():
        return 子项[0]
    return 目标目录


def 更新目录(项目根: str | Path) -> Path:
    """下载与解压都放这儿（更新完成后用户可以整个删掉）。"""
    return Path(项目根) / "更新"


def 写更新脚本(源目录: str | Path, 项目根: str | Path,
            等进程: int = 0, 重启: str = "") -> Path:
    """生成"应用更新"的平台脚本，返回脚本路径。

    脚本干三件事：等本进程退出 → 把 ``源目录`` 合并覆盖到项目根 → 重新启动。
    *合并*意味着用户自己的 ``配置.json``、``数据/``、适配器登录凭证都不会被动
    （发布包里本来也不含这些）。
    """
    源, 根 = Path(源目录).resolve(), Path(项目根).resolve()
    脚本目录 = 更新目录(根)
    脚本目录.mkdir(parents=True, exist_ok=True)
    # 脚本里的变量名一律 ASCII：bash 不认中文变量名（``源=...`` 会被当成命令去执行，
    # 报 "No such file or directory"），cmd.exe 的中文变量名在 chcp 65001 下也不稳。
    if 平台标记() == "windows":
        脚本 = 脚本目录 / "应用更新.bat"
        启动 = 重启 or str(根 / "启动.exe")
        脚本.write_text(
            "@echo off\r\n"
            "chcp 65001 >nul\r\n"
            "rem 由「设置 → 一键更新」生成：等旧窗口关掉，再把新版本覆盖上去\r\n"
            f'set "SRC={源}"\r\n'
            f'set "DST={根}"\r\n'
            f'set "WAITPID={int(等进程)}"\r\n'
            "if not \"%WAITPID%\"==\"0\" (\r\n"
            "  :waitloop\r\n"
            "  tasklist /FI \"PID eq %WAITPID%\" 2>nul | find \"%WAITPID%\" >nul\r\n"
            "  if not errorlevel 1 ( timeout /t 1 /nobreak >nul & goto waitloop )\r\n"
            ")\r\n"
            "timeout /t 1 /nobreak >nul\r\n"
            "xcopy \"%SRC%\\*\" \"%DST%\\\" /E /Y /I /Q >nul\r\n"
            f'start "" "{启动}"\r\n',
            encoding="utf-8")
        return 脚本
    脚本 = 脚本目录 / "应用更新.sh"
    启动 = 重启 or str(根 / "启动.sh")
    脚本.write_text(
        "#!/usr/bin/env bash\n"
        "# 由「设置 → 一键更新」生成：等旧进程退出，再把新版本合并覆盖上去\n"
        "set -u\n"
        f'SRC="{源}"\n'
        f'DST="{根}"\n'
        f'WAITPID="{int(等进程)}"\n'
        'if [ "$WAITPID" != "0" ]; then\n'
        '  while kill -0 "$WAITPID" 2>/dev/null; do sleep 1; done\n'
        "fi\n"
        "sleep 1\n"
        'cp -a "$SRC"/. "$DST"/ || exit 1\n'
        f'cd "$DST" && exec "{启动}"\n',
        encoding="utf-8")
    脚本.chmod(0o755)
    return 脚本


def 拉起更新脚本(脚本: str | Path) -> int:
    """脱离本进程拉起更新脚本（本进程随后应当退出）。返回子进程 PID。"""
    脚本 = Path(脚本)
    if not 脚本.is_file():
        raise FileNotFoundError(f"更新脚本不存在：{脚本}")
    if os.name == "nt":
        进程 = subprocess.Popen(
            ["cmd", "/c", "start", "", "/min", str(脚本)],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            cwd=str(脚本.parent), close_fds=True)
        return int(进程.pid)
    进程 = subprocess.Popen(
        ["/bin/bash", str(脚本)],
        cwd=str(脚本.parent), start_new_session=True,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, close_fds=True)
    return int(进程.pid)


def 自我说明() -> str:
    """给"我该下哪个包"用的一句话（设置页显示）。"""
    标记 = 平台标记()
    return (f"当前系统：{平台中文(标记)}（{sys.platform}）｜ "
            f"Python {platform.python_version()}")
