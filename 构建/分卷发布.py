"""把发布包切成小分卷上传（大文件直传会被 GitHub 限量/500，小分卷稳得多）。

为什么这样做
============
实测：**≤90 MB 的单次上传几乎 100% 成功**（几秒到十几秒），而 400 MB～1.4 GB 的
整包上传成功率很低（GitHub 回 500 "Error saving asset"，或长时间挂住）。
所以把每个 zip 切成若干小分卷逐个传，每个分卷都能单独校验、失败只重传那一个。

用户侧体验：把某个包的所有分卷下载到同一个文件夹，双击/执行附带的
`JOIN-AND-EXTRACT-Windows.bat`（或 `JOIN-AND-EXTRACT-Linux.sh`）即可——
脚本用系统自带命令（Windows 的 copy/tar、Linux 的 cat/unzip）合并再解压，
不需要额外装任何东西。

用法（项目根下）::

    运行环境/venv/bin/python 构建/分卷发布.py --检查          # 只列计划，不动手
    运行环境/venv/bin/python 构建/分卷发布.py --分卷 80       # 切 80 MB 分卷并上传
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
import time
from pathlib import Path

项目根 = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(项目根))

from 构建.发布到github import 会话, 确保发布, 传资源, 发布目录  # noqa: E402

分卷目录 = 发布目录 / "分卷"
连接脚本目录 = 发布目录

Windows合并脚本 = """@echo off
chcp 65001 >nul
rem ===========================================================================
rem  网盘管理 · 分卷合并并解压（Windows）
rem  把同一个包的所有 .partNN 下载到**同一个文件夹**，然后双击本脚本即可。
rem  只用到系统自带命令：copy /b 合并，tar 解压（Windows 10 1803+ 自带）。
rem ===========================================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"

set "TARGET="
for %%F in (*.zip.part01) do set "TARGET=%%F"
if "%TARGET%"=="" (
  echo 没找到 .zip.part01 文件。
  echo 请把同一个包的所有分卷下载到本文件夹后再运行本脚本。
  pause & exit /b 1
)

rem 取基础名：xxx.zip.part01 -> xxx.zip
set "BASE=%TARGET:.part01=%"
echo 正在合并分卷到 %BASE% …
if exist "%BASE%" del "%BASE%"
copy /b "%BASE%.part*" "%BASE%" >nul
if errorlevel 1 (
  echo 合并失败：请确认所有分卷都在同一个文件夹里。
  pause & exit /b 1
)

echo 正在解压 %BASE% …
tar -xf "%BASE%"
if errorlevel 1 (
  echo 解压失败：如果系统没有 tar，可以手动用 7-Zip 打开 %BASE% 解压。
  pause & exit /b 1
)
echo.
echo 完成！解压出来的文件夹里双击 启动.exe 即可使用。
pause
"""

Linux合并脚本 = """#!/usr/bin/env bash
# ============================================================================
#  网盘管理 · 分卷合并并解压（Linux）
#  把同一个包的所有 .zip.partNN 下载到同一个目录，然后执行本脚本即可。
#  只用系统自带命令：cat 合并 + unzip（没有 unzip 时退回 python 的 zipfile）。
# ============================================================================
set -eu
cd "$(dirname "$0")"

TARGET="$(ls *.zip.part01 2>/dev/null | head -1 || true)"
if [ -z "$TARGET" ]; then
  echo "没找到 .zip.part01：请把同一个包的所有分卷下载到本目录后再运行。"
  exit 1
fi
BASE="${TARGET%.part01}"
echo "正在合并分卷到 $BASE …"
cat "$BASE".part* > "$BASE"
echo "正在解压 $BASE …"
if command -v unzip >/dev/null 2>&1; then
  unzip -q -o "$BASE"
elif command -v python3 >/dev/null 2>&1; then
  python3 -m zipfile -e "$BASE" .
else
  echo "既没有 unzip 也没有 python3，请手动解压 $BASE"
  exit 1
fi
echo
echo "完成！进入解压出来的目录，执行 ./启动.sh 即可使用。"
"""


def 说(文本: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {文本}", flush=True)


def 切分卷(zip路径: Path, 每卷MB: int) -> list[Path]:
    """把一个 zip 切成若干分卷，返回分卷路径列表（已存在就复用）。"""
    分卷目录.mkdir(parents=True, exist_ok=True)
    前缀 = 分卷目录 / (zip路径.name + ".part")
    已有 = sorted(分卷目录.glob(zip路径.name + ".part[0-9][0-9]"))
    总长 = zip路径.stat().st_size
    期望个数 = (总长 + 每卷MB * 1048576 - 1) // (每卷MB * 1048576)
    if len(已有) == 期望个数 and sum(p.stat().st_size for p in 已有) == 总长:
        return 已有
    for p in 已有:
        p.unlink()
    块 = 每卷MB * 1048576
    结果: list[Path] = []
    with open(zip路径, "rb") as 源:
        序号 = 1
        while True:
            数据 = 源.read(块)
            if not 数据:
                break
            分卷 = Path(f"{前缀}{序号:02d}")
            分卷.write_bytes(数据)
            结果.append(分卷)
            序号 += 1
    return 结果


def 写合并脚本() -> list[Path]:
    w = 连接脚本目录 / "JOIN-AND-EXTRACT-Windows.bat"
    l = 连接脚本目录 / "JOIN-AND-EXTRACT-Linux.sh"
    w.write_text(Windows合并脚本, encoding="utf-8")
    l.write_text(Linux合并脚本, encoding="utf-8")
    l.chmod(0o755)
    return [w, l]


def main() -> int:
    解析 = argparse.ArgumentParser(description="切分卷并上传")
    解析.add_argument("--分卷", type=int, default=80, help="每个分卷多少 MB（默认 80）")
    解析.add_argument("--检查", action="store_true", help="只列计划")
    解析.add_argument("--只", default="", help="只传名字含这个关键词的包（可并发跑多个）")
    参数 = 解析.parse_args()

    包们 = sorted(发布目录.glob("*.zip"))
    if 参数.只:
        包们 = [x for x in 包们 if 参数.只 in x.name]
    if not 包们:
        raise SystemExit("构建/发布 下没有 zip")
    计划: list[tuple[Path, list[Path]]] = []
    for 包 in 包们:
        分卷 = 切分卷(包, 参数.分卷)
        计划.append((包, 分卷))
        说(f"{包.name}（{包.stat().st_size / 1048576:.0f} MB）→ {len(分卷)} 个分卷")
    脚本 = 写合并脚本()
    说(f"合并脚本：{[p.name for p in 脚本]}")
    总计 = sum(len(v) for _, v in 计划)
    说(f"合计 {总计} 个分卷要传")
    if 参数.检查:
        return 0

    with 会话() as s:
        s.get("https://api.github.com/user").raise_for_status()
        发布 = 确保发布(s)
        失败 = 0
        for 包, 分卷 in 计划:
            说(f"== {包.name} ==")
            for i, 卷 in enumerate(分卷, 1):
                for 试 in range(1, 6):
                    if 传资源(s, 发布, 卷):
                        break
                    说(f"  重试 {卷.name}（第 {试} 次失败后换连接）")
                    time.sleep(10)
                else:
                    失败 += 1
                    说(f"  ✗ {卷.name} 五次都没传上去")
            说(f"   {包.name} 的分卷处理完（{i}/{len(分卷)}）")
        for 脚本文件 in 脚本:
            传资源(s, 发布, 脚本文件)
        说(f"完成，失败 {失败} 个分卷")
        return 1 if 失败 else 0


if __name__ == "__main__":
    raise SystemExit(main())
