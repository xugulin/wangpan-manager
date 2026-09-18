"""把项目打成绿色版压缩包：Linux / Windows × 完整版（含模型）/ 精简版。

用法（项目根下，用自带解释器跑）::

    运行环境/venv/bin/python 构建/打包.py                # 四个包全打
    运行环境/venv/bin/python 构建/打包.py --平台 linux    # 只打 Linux
    运行环境/venv/bin/python 构建/打包.py --口味 精简     # 只打精简版

产物在 ``构建/发布/`` 下。**发布包里不含** ``配置.json`` 与 ``数据/``，
所以解压出来就是干净的新装，升级覆盖时也不会动用户自己的东西。

依赖：``7z``（打 zip）、Windows 版还需要先跑 ``构建/构建_windows.sh`` 备好运行时。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

项目根 = Path(__file__).resolve().parent.parent
构建目录 = 项目根 / "构建"
发布目录 = 构建目录 / "发布"
包内容 = 构建目录 / "包内容"
Windows运行时 = 构建目录 / "windows"

def _读版本() -> str:
    """版本号只在 v8_3/__init__.py 里定义一次，这里读出来用（别再手写）。"""
    for 行 in (项目根 / "v8_3" / "__init__.py").read_text(encoding="utf-8").splitlines():
        if 行.startswith("__version__"):
            return 行.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("读不到 __version__")


版本 = _读版本()

#: 打进包里的源码/文档（从项目根拷）
#: 两个平台都带的源码/文档
源码项 = ("启动.py", "pyproject.toml", "README.md",
        "v8_3", "工具", "tests", "docs", "适配器")

#: 只给 Linux 的（Windows 包里有 Windows 启动器，不需要 .sh）
Linux源码 = ("启动.sh",)

#: 平台附加文件（各平台只带自己那份，别把 Windows 的 .exe 塞进 Linux 包）
Linux附加 = ("创建桌面图标.sh", "使用说明.txt", "assets") + Linux源码
Windows附加 = ("创建桌面图标.bat", "启动（看报错）.bat", "使用说明.txt", "assets")
启动器exe = Path(__file__).resolve().parent / "启动器" / "启动.exe"

#: 一律不拷的东西
跳过名 = {"__pycache__", ".git", ".idea", ".venv", "更新", "构建",
        "数据", "配置.json", "*.pyc", "*.pyo"}


def 说(文本: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {文本}", flush=True)


def _忽略(目录: str, 名称: list[str]) -> set[str]:
    结果 = set()
    for 名 in 名称:
        if 名 in 跳过名 or 名.endswith((".pyc", ".pyo")):
            结果.add(名)
    return 结果


def 拷源码(顶层: Path, 平台: str) -> None:
    for 名 in 源码项:
        源 = 项目根 / 名
        if not 源.exists():
            continue
        目标 = 顶层 / 名
        if 源.is_dir():
            shutil.copytree(源, 目标, ignore=_忽略, dirs_exist_ok=True)
        else:
            shutil.copy2(源, 目标)
    # 各家适配器只带代码：数据/ 里是登录凭证，绝不进发布包
    for 适配器 in (顶层 / "适配器").glob("*"):
        if 适配器.is_dir():
            for 子 in 适配器.iterdir():
                if 子.is_dir() and 子.name == "数据":
                    for 文件 in 子.iterdir():
                        文件.unlink()
    # 包内容里的平台文件（启动器、说明、图标、创建桌面图标的脚本）
    for 名 in (Linux附加 if 平台 == "linux" else Windows附加):
        源 = 包内容 / 名
        if not 源.exists():
            continue
        目标 = 顶层 / 名
        if 源.is_dir():
            shutil.copytree(源, 目标, dirs_exist_ok=True)
        else:
            shutil.copy2(源, 目标)
            if 名.endswith(".sh"):
                目标.chmod(0o755)
    if 平台 == "windows" and 启动器exe.is_file():
        shutil.copy2(启动器exe, 顶层 / "启动.exe")
        (顶层 / "启动.exe").chmod(0o755)


def 拷运行环境(顶层: Path, 平台: str, 含模型: bool) -> None:
    目标 = 顶层 / "运行环境"
    目标.mkdir(parents=True, exist_ok=True)
    if 平台 == "linux":
        来源 = 项目根 / "运行环境"
        # ⚠️ "本地模型" = **便携版 ollama 运行时**。用户要求：打包**预装基础
        #    ollama**，但**不预装任何模型权重** —— 权重由 AI 页的
        #    「🛒 本地小模型市场」按需一键安装（也会存进项目目录）。
        for 名 in ("python", "venv", "语音识别", "本地模型"):
            if not (来源 / 名).exists():
                continue
            shutil.copytree(来源 / 名, 目标 / 名, ignore=_忽略,
                            dirs_exist_ok=True, symlinks=True)
    else:
        if not (Windows运行时 / "python主").is_dir():
            raise SystemExit("Windows 运行时还没准备好：先跑 构建/构建_windows.sh")
        shutil.copytree(Windows运行时 / "python主", 目标 / "python",
                        ignore=_忽略, dirs_exist_ok=True)
        if (Windows运行时 / "语音识别" / "python").is_dir():
            shutil.copytree(Windows运行时 / "语音识别" / "python",
                            目标 / "语音识别" / "python",
                            ignore=_忽略, dirs_exist_ok=True)
        # Windows 的便携 ollama：构建/构建_windows.sh 里下好就放在这儿
        for 源 in (Windows运行时 / "本地模型", Windows运行时 / "ollama"):
            if 源.is_dir():
                shutil.copytree(源, 目标 / "本地模型", ignore=_忽略,
                                dirs_exist_ok=True)
                break
    # 模型（平台无关）：精简版不带，用户第一次用字幕时会自动下载
    if not 含模型:
        模型目录 = 目标 / "语音识别" / "模型"
        if 模型目录.exists():
            shutil.rmtree(模型目录, ignore_errors=True)
        return
    if 平台 == "linux":
        return                       # Linux 的运行环境是直接拷过来的，模型已经在了
    来源模型 = 项目根 / "运行环境" / "语音识别" / "模型"
    if 来源模型.is_dir():
        shutil.copytree(来源模型, 目标 / "语音识别" / "模型",
                        ignore=_忽略, dirs_exist_ok=True)


def 清理垃圾(顶层: Path) -> None:
    for 路径 in list(顶层.rglob("__pycache__")):
        shutil.rmtree(路径, ignore_errors=True)
    for 后缀 in ("*.pyc", "*.pyo"):
        for 路径 in 顶层.rglob(后缀):
            路径.unlink(missing_ok=True)


def 打包(顶层: Path, 输出: Path) -> Path:
    输出.parent.mkdir(parents=True, exist_ok=True)
    输出.unlink(missing_ok=True)
    # 7z 打 zip：支持 UTF-8 文件名与 >4GB 的包，-mx=3 兼顾速度与体积
    子 = subprocess.run(
        ["7z", "a", "-tzip", "-mx=3", "-bso0", "-bsp0", str(输出), 顶层.name],
        cwd=str(顶层.parent), capture_output=True, text=True)
    if 子.returncode != 0:
        raise SystemExit(f"7z 打包失败：{子.stdout[-500:]} {子.stderr[-500:]}")
    return 输出


def 备好本地模型运行时() -> None:
    """把便携版 ollama 下到 ``运行环境/本地模型``（打包会把它一起带进发布包）。

    用户要求：打包**预装基础 ollama**，但**不预装任何模型权重**。
    * 已经下过就直接跳过（幂等）；
    * 参数 ``--不要ollama`` 可跳过下载（离线构建/不想让包变大时用）。
    """
    目标 = 项目根 / "运行环境" / "本地模型"
    名字 = "ollama.exe" if os.name == "nt" else "ollama"
    现成 = [目标 / 名字, 目标 / "bin" / 名字]
    if any(x.is_file() for x in 现成):
        说("  · 便携 ollama 已在 运行环境/本地模型（跳过下载）")
        return
    说("  · 下载便携 ollama 运行时（约 2 GB，只放运行时，不含模型权重）…")
    sys.path.insert(0, str(项目根))
    try:
        from v8_3.AI.本地模型 import 下载便携运行时
        上次 = [0.0]

        def 进度(已下: int, 总: int) -> None:
            现在 = time.time()
            if 现在 - 上次[0] < 3:
                return
            上次[0] = 现在
            尾巴 = f"{已下 / 1048576:.0f}"
            if 总:
                尾巴 += f"/{总 / 1048576:.0f}"
            说(f"    下载中 {尾巴} MB")

        好, 消息 = 下载便携运行时(进度回调=进度)
    except Exception as e:  # noqa: BLE001
        说(f"  ⚠️ 下载便携 ollama 失败：{type(e).__name__}: {e}")
        说("     发布包里将不含 ollama；用户可在 AI 页点「⬇️ 装运行时」补装。")
        return
    说(("  ✓ " if 好 else "  ⚠️ ") + str(消息))


def main() -> int:
    解析 = argparse.ArgumentParser(description="打绿色版压缩包")
    解析.add_argument("--平台", choices=("linux", "windows", "全部"), default="全部")
    解析.add_argument("--口味", choices=("完整", "精简", "全部"), default="全部")
    解析.add_argument("--不要ollama", action="store_true",
                    help="不下载/不预装便携 ollama（默认会预装运行时，但不含模型权重）")
    参数 = 解析.parse_args()

    平台们 = ("linux", "windows") if 参数.平台 == "全部" else (参数.平台,)
    口味们 = ((True, "完整版-含AI语音模型"), (False, "精简版-不含模型")) \
        if 参数.口味 == "全部" else \
        (((True, "完整版-含AI语音模型"),) if 参数.口味 == "完整"
         else ((False, "精简版-不含模型"),))

    发布目录.mkdir(parents=True, exist_ok=True)
    结果: list[tuple[str, int]] = []
    if not 参数.不要ollama:
        说("准备本地模型运行时（预装 ollama，不含模型权重）")
        备好本地模型运行时()
    else:
        说("按参数要求：发布包不预装 ollama（AI 页里可点「⬇️ 装运行时」补装）")
    for 平台 in 平台们:
        平台名 = {"linux": "Linux", "windows": "Windows"}[平台]
        for 含模型, 口味名 in 口味们:
            包名 = f"网盘管理-V{版本}-{平台名}-{口味名}"
            说(f"打包：{包名}")
            目标 = 发布目录 / 包名
            if 目标.exists():
                shutil.rmtree(目标)
            目标.mkdir(parents=True)
            顶层 = 目标 / 包名
            顶层.mkdir()
            说("  · 拷源码与文档")
            拷源码(顶层, 平台)
            说("  · 拷运行环境（这步最慢）")
            拷运行环境(顶层, 平台, 含模型)
            说("  · 清理缓存文件")
            清理垃圾(顶层)
            说("  · 压缩")
            zip路径 = 打包(顶层, 发布目录 / f"{包名}.zip")
            shutil.rmtree(目标, ignore_errors=True)
            大小 = zip路径.stat().st_size
            结果.append((zip路径.name, 大小))
            说(f"  ✓ {zip路径.name}  {大小 / 1048576:.0f} MB")

    说("全部完成：")
    for 名, 大小 in 结果:
        print(f"   {名}  {大小 / 1048576:.0f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
