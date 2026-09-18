#!/usr/bin/env python3
"""清理本项目实测留下的东西 + 报告磁盘现状（**Python 版，最稳**）。

为什么有 Python 版：项目里的同名 .sh 在 fish / 某些终端包装器里会被拆坏
（中文变量名被当成命令、`$VAR` 被吃掉），只打印不干活。Python 只依赖解释器，
不受 shell 方言影响。

用法（项目自带解释器最稳）：

    运行环境/venv/bin/python 工具/清理_本地模型实测残留.py            # 只看
    运行环境/venv/bin/python 工具/清理_本地模型实测残留.py --清理      # 真的清

它会：
  1. 报告 / /home /tmp 的空间与 inode；
  2. 报告 /tmp、$HOME 下最大的目录（只看，不删别人的东西）；
  3. 报告本地模型相关目录（项目内 数据/本地模型、用户目录 ~/.ollama、
     ~/.local/ollama）各占多少；
  4. `--清理` 时：停掉本项目拉起的 ollama serve、删掉**本项目**的测试残留
     （/tmp/v8_3_*、/tmp/ai_market_*、/tmp/gy_qr_*、/tmp/v83*），
     以及**用户明确同意时**才删的旧模型权重（~/.ollama/models）。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

项目根 = Path(__file__).resolve().parents[1]
工作区 = Path(os.environ.get("V83_WORK") or (Path.home() / "v8_3_工作区"))

#: 本项目的测试残留模式（只删这些）
残留模式 = ("v8_3_*", "ai_market_*", "gy_qr_*", "v83*", "v8_3*")


def 人类(字节: float) -> str:
    for 单位 in ("B", "KB", "MB", "GB", "TB"):
        if 字节 < 1000 or 单位 == "TB":
            return f"{字节:.1f} {单位}"
        字节 /= 1000.0
    return f"{字节:.1f} TB"


def 目录大小(路径: Path) -> int:
    """安全地算目录大小（权限不足/不存在返回 0）。"""
    总 = 0
    try:
        with os.scandir(路径) as 项们:
            for 项 in 项们:
                try:
                    if 项.is_symlink():
                        continue
                    if 项.is_dir(follow_symlinks=False):
                        总 += 目录大小(Path(项.path))
                    else:
                        总 += 项.stat(follow_symlinks=False).st_size
                except Exception:
                    continue
    except Exception:
        return 0
    return 总


def 跑(命令: list[str]) -> str:
    try:
        子 = subprocess.run(命令, capture_output=True, text=True, timeout=60)
        return (子.stdout or "") + (子.stderr or "")
    except Exception as e:  # noqa: BLE001
        return f"（{命令[0]} 跑不动：{e}）"


def 报告空间() -> None:
    print("===== ① 磁盘 / inode =====")
    print(跑(["df", "-h", "/", "/home", "/tmp"]).strip())
    print(跑(["df", "-i", "/", "/tmp"]).strip())


def 报告大目录(根: Path, 层数: int = 2, 条数: int = 15) -> None:
    print(f"\n===== {根} 下最大的目录 top{条数}（只看，不删）=====")
    try:
        项们 = sorted(
            ((目录大小(子), 子) for 子 in 根.iterdir() if 子.is_dir()),
            reverse=True)[:条数]
        for 大小, 子 in 项们:
            print(f"  {人类(大小):>10s}  {子}")
    except Exception as e:  # noqa: BLE001
        print(f"  （读不了：{e}）")


def 报告本地模型() -> None:
    print("\n===== ③ 本地模型相关的目录 =====")
    目标 = {
        "项目内 便携运行时": 项目根 / "运行环境" / "本地模型",
        "项目内 模型权重": 项目根 / "数据" / "本地模型",
        "用户目录 ollama 运行时": Path.home() / ".local" / "ollama",
        "用户目录 旧模型权重": Path.home() / ".ollama" / "models",
        "模型市场缓存": 项目根 / "数据" / "本地模型" / "模型市场.json",
    }
    for 名, 路径 in 目标.items():
        存在 = 路径.exists()
        大小 = 目录大小(路径) if 路径.is_dir() else (
            路径.stat().st_size if 存在 else 0)
        print(f"  {名:22s} {'✅' if 存在 else '—'}  {人类(大小):>10s}  {路径}")


def 清理(含旧模型: bool) -> None:
    print("\n===== ④ 清理 =====")
    # 停掉本项目拉起的 ollama serve（只匹配命令行里带 ollama serve 的）
    try:
        子 = subprocess.run(["pgrep", "-af", "ollama serve"],
                            capture_output=True, text=True)
        行 = [x for x in (子.stdout or "").splitlines() if x.strip()]
        if 行:
            subprocess.run(["pkill", "-f", "ollama serve"], timeout=20)
            print(f"  已停 ollama serve（{len(行)} 个进程）")
        else:
            print("  没有 ollama serve 在跑")
    except Exception as e:  # noqa: BLE001
        print(f"  停服务时出错（忽略）：{e}")

    import glob
    清掉 = 0
    for 模式 in 残留模式:
        for 路径 in glob.glob(f"/tmp/{模式}"):
            try:
                p = Path(路径)
                if p.is_dir():
                    shutil.rmtree(p, ignore_errors=True)
                else:
                    p.unlink(missing_ok=True)
                清掉 += 1
            except Exception:
                pass
    print(f"  已清 /tmp 里本项目残留：{清掉} 项")

    if 含旧模型:
        旧 = Path.home() / ".ollama" / "models"
        if 旧.is_dir():
            shutil.rmtree(旧, ignore_errors=True)
            print(f"  已删旧模型权重（用户目录）：{旧}")
        else:
            print("  用户目录没有旧模型权重")
    else:
        print("  （用户目录的旧模型权重没动；要删加 --含旧模型）")

    print("\n===== ⑤ 建/确认工作区 =====")
    for 子 in ("临时", "构建", "模型", "截图", "日志"):
        (工作区 / 子).mkdir(parents=True, exist_ok=True)
    print(f"  {工作区} 已就绪（临时/构建/模型/截图/日志）")
    print(f"  建议：export TMPDIR={工作区 / '临时'}")


def main() -> int:
    解析 = argparse.ArgumentParser(description="清理实测残留并报告磁盘")
    解析.add_argument("--清理", action="store_true", help="真的执行清理")
    解析.add_argument("--含旧模型", action="store_true",
                    help="连 ~/.ollama/models（旧模型权重）一起删")
    参数 = 解析.parse_args()

    报告空间()
    报告大目录(Path.home(), 2, 15)
    报告大目录(Path("/tmp"), 1, 12)
    报告本地模型()
    if 参数.清理:
        清理(参数.含旧模型)
        报告空间()
    else:
        print("\n（只看了没动；要清理加 --清理，连旧模型一起删加 --含旧模型）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
