#!/usr/bin/env python3
"""下载官方 VLC（Windows x64）并抽出**内嵌播放需要的部分**，供发布包内置。

为什么要有这个（用户报的致命问题）
==================================
Windows 版发布出去之后，播放页直接报"libvlc 不可用：找不到 libvlc（VLC 的运行库）"——
因为 Windows 上不像桌面 Linux 那样自带 VLC，用户得自己装一个才能看视频。
**用户要求：Windows 版也要内置**。所以这里把官方 VLC 的
``libvlc.dll`` / ``libvlccore.dll`` / ``plugins/`` 抽出来放进包里，
播放页用包内的这一份（不依赖系统装没装 VLC）。

用法（项目根下）::

    运行环境/venv/bin/python 构建/取vlc.py            # 下载 + 抽取（已有就跳过）
    运行环境/venv/bin/python 构建/取vlc.py --强制      # 重新下

产物：``构建/windows/vlc/``（打包脚本会把它拷进发布包的 ``运行环境/vlc/``）：

    vlc/libvlc.dll
    vlc/libvlccore.dll
    vlc/plugins/**                 ← 解码/HTTP/输出等插件（约 100 MB）
    vlc/COPYING.txt                ← VLC 的许可证（GPLv2+ / LGPLv2.1+，必须随包分发）

版本与 Linux 侧对齐（当前 3.0.23）；下载源是官方 download.videolan.org（有备用镜像）。
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

项目根 = Path(__file__).resolve().parent.parent
输出目录 = 项目根 / "构建" / "windows" / "vlc"

#: 与 Linux 侧保持一致；两边的 libvlc ABI 一样，行为也一致
VLC版本 = "3.0.23"
压缩包名 = f"vlc-{VLC版本}-win64.zip"
下载源们 = (
    f"https://download.videolan.org/pub/vlc/{VLC版本}/win64/{压缩包名}",
    f"https://mirrors.tuna.tsinghua.edu.cn/videolan-ftp/vlc/{VLC版本}/win64/{压缩包名}",
    f"https://get.videolan.org/vlc/{VLC版本}/win64/{压缩包名}",
)
#: 抽取这些（相对 zip 里 vlc-3.0.23/ 的前缀）；plugins 是整目录
要抽的文件 = ("libvlc.dll", "libvlccore.dll", "COPYING.txt", "COPYING",
           "COPYING.LIB", "AUTHORS.txt", "AUTHORS")
要抽的目录 = ("plugins",)
#: 这些插件占地方又用不到（macOS/Windows 无关的语言、皮肤、可视化等）
不抽的插件前缀 = ("libskins2", "libvisual", "libgoom", "libprojectm", "libncurses",
             "libmotionblur", "libaudiobargraph", "libexport", "libgestures")


def 说(文本: str) -> None:
    print(f"[取vlc] {文本}", flush=True)


def 下载(目标: Path) -> Path:
    """下载压缩包（官方源失败就换镜像）。"""
    if 目标.is_file() and 目标.stat().st_size > 50 * 1024 * 1024:
        说(f"已有压缩包，跳过下载：{目标}（{目标.stat().st_size / 1048576:.0f} MB）")
        return 目标
    最后错误 = None
    for 源 in 下载源们:
        try:
            说(f"下载 {源}")
            with urllib.request.urlopen(源, timeout=120) as 应答, \
                    open(目标, "wb") as 写出:
                已读 = 0
                块 = 1024 * 256
                while True:
                    数据 = 应答.read(块)
                    if not 数据:
                        break
                    写出.write(数据)
                    已读 += len(数据)
                    if 已读 % (20 * 1048576) < 块:
                        说(f"  … {已读 / 1048576:.0f} MB")
            说(f"下载完成：{目标.stat().st_size / 1048576:.0f} MB")
            return 目标
        except Exception as 错:  # noqa: BLE001
            最后错误 = 错
            说(f"  失败：{错}")
            目标.unlink(missing_ok=True)
    raise SystemExit(f"三个下载源都失败了：{最后错误}")


def 抽取(压缩包: Path, 输出: Path) -> None:
    """把要用的文件解到 ``构建/windows/vlc/``（保持 VLC 自己的目录结构）。"""
    if 输出.exists():
        shutil.rmtree(输出)
    输出.mkdir(parents=True, exist_ok=True)
    前缀 = f"vlc-{VLC版本}/"
    个数 = 0
    with zipfile.ZipFile(压缩包) as 包:
        for 项 in 包.infolist():
            名 = 项.filename
            if not 名.startswith(前缀) or 名.endswith("/"):
                continue
            相对 = 名[len(前缀):]
            if 相对.startswith("vlc.exe") or 相对.startswith("sdk/"):
                continue
            顶层 = 相对.split("/", 1)[0]
            if 顶层 in 要抽的文件:
                落点 = 输出 / 相对
            elif 顶层 in 要抽的目录:
                if any(Path(相对).name.startswith(前) for 前 in 不抽的插件前缀):
                    continue
                落点 = 输出 / 相对
            else:
                continue
            落点.parent.mkdir(parents=True, exist_ok=True)
            with 包.open(项) as 读, open(落点, "wb") as 写:
                shutil.copyfileobj(读, 写)
            个数 += 1
    说(f"抽出 {个数} 个文件 → {输出}")


def 核对(输出: Path) -> None:
    核心 = 输出 / "libvlc.dll"
    插件 = 输出 / "plugins"
    if not 核心.is_file():
        raise SystemExit("✗ 没抽出 libvlc.dll —— 包不完整，不能发布")
    # ⚠️ VLC 3.x 的插件是**分子目录**放的（plugins/access/、plugins/codec/…），
    #    所以必须递归数 —— 只数 plugins/*.dll 会误判成"不完整"（踩过一次）。
    插件数 = len(list(插件.rglob("*.dll"))) if 插件.is_dir() else 0
    if 插件数 < 50:
        raise SystemExit(f"✗ plugins 目录不完整（只有 {插件数} 个插件）—— 不能发布")
    总 = sum(p.stat().st_size for p in 输出.rglob("*") if p.is_file())
    说(f"核对通过：libvlc.dll {核心.stat().st_size / 1048576:.1f} MB｜"
      f"插件 {插件数} 个｜合计 {总 / 1048576:.0f} MB")
    # 许可证必须随包走（VLC 是 GPLv2+/LGPLv2.1+）
    if not any((输出 / 名).is_file()
              for 名 in ("COPYING.txt", "COPYING", "COPYING.LIB")):
        raise SystemExit("✗ 没带 VLC 的许可证文件（GPL/LGPL）—— 不能发布")


def main() -> int:
    解析 = argparse.ArgumentParser(description="下载并抽取内置 VLC（Windows 版用）")
    解析.add_argument("--强制", action="store_true", help="重新下载压缩包")
    参数 = 解析.parse_args()

    缓存 = 项目根 / "构建" / "windows" / 压缩包名
    缓存.parent.mkdir(parents=True, exist_ok=True)
    if 参数.强制:
        缓存.unlink(missing_ok=True)
    说(f"版本 {VLC版本}（与 Linux 侧一致）")
    压缩包 = 下载(缓存)
    抽取(压缩包, 输出目录)
    核对(输出目录)
    说("完成：构建/windows/vlc 已就绪，打包脚本会自动放进 Windows 发布包的 "
      "运行环境/vlc")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
