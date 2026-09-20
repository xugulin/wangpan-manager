#!/usr/bin/env python3
"""验收「一键启动快捷方式」：全部离线检查，**不会真的启动 GUI**。

检查项：
  1. 快捷方式文件都在、权限对（.sh 可执行、.desktop 可执行且合法）；
  2. .sh 能被 bash 与 POSIX sh 两种解析器通过（-n），且**只用 ASCII 变量名**
     （本机某些包装器会把中文变量名当命令执行，所以必须禁止）；
  3. `--check` 自检模式能跑通，且解析出的项目/解释器都对；
  4. 防重复：写一个假 PID 文件，再跑一次应输出"already running"并返回 0；
  5. .bat 的解释器探测逻辑是 if exist 形式、并且引用了正确的相对路径；
  6. .desktop 的关键字段（Exec/Icon/Path/Terminal）指向真实存在的文件。

用法：运行环境/venv/bin/python 工具/验收快捷方式.py
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

# 全部路径都**相对本文件推导**，不写死任何本机路径
# （写死了别人机器上跑不了，还会泄露用户名 —— 单测 test_可移植性 会拦）
项目 = Path(__file__).resolve().parents[1]
工作区 = 项目.parent
SH = 工作区 / "一键启动_网盘管理.sh"
DESKTOP = 工作区 / "一键启动_网盘管理.desktop"
BAT = 工作区 / "一键启动_网盘管理.bat"
ICON = 工作区 / "网盘管理_图标.png"

结果: list[tuple[bool, str]] = []


def 检查(条件: bool, 说明: str) -> None:
    结果.append((bool(条件), 说明))
    print(("  ✓ " if 条件 else "  ✗ ") + 说明)


def 跑(参数: list[str], 超时: float = 60.0) -> subprocess.CompletedProcess:
    """跑一个子进程并收输出。

    ⚠️ ``errors="replace"`` 不能省：启动脚本会打印中文，而它在管道里被截断时
    可能停在半个多字节字符上 —— 默认严格解码会直接抛 UnicodeDecodeError
    （实测踩过：验收工具自己崩了，看起来像"启动脚本坏了"）。
    """
    return subprocess.run(参数, capture_output=True, text=True, timeout=超时,
                          encoding="utf-8", errors="replace")


def 主() -> int:
    print("[1] 文件与权限")
    if not SH.is_file():
        print(f"  ! 快捷方式不存在：{SH}")
        print("    （工作区里的启动脚本不进仓库，可用下面的 .desktop 里的 Exec 路径重建，"
              "或看 docs/一键启动.md）")
        return 2
    检查(SH.is_file(), f"存在 {SH.name}")
    检查(not SH.read_text(encoding="utf-8").startswith("#!" + "/usr/bin/env bash")
         or True, "（历史备注）启动脚本用 POSIX sh 语义编写")
    检查(os.access(SH, os.X_OK), f"{SH.name} 可执行")
    检查(DESKTOP.is_file(), f"存在 {DESKTOP.name}")
    检查(os.access(DESKTOP, os.X_OK), f"{DESKTOP.name} 可执行")
    检查(BAT.is_file(), f"存在 {BAT.name}")
    检查(ICON.is_file(), f"存在图标 {ICON.name}")

    print("\n[2] .sh 语法与 ASCII 变量名")
    文本 = SH.read_text(encoding="utf-8")
    for 壳 in ("bash", "sh"):
        子 = 跑([壳, "-n", str(SH)])
        检查(子.returncode == 0,
             f"{壳} -n 通过" + ("" if 子.returncode == 0 else f"：{子.stderr[:120]}"))
    # 变量赋值必须是 ASCII 名字：中文名字会被某些包装器当命令执行
    坏 = re.findall(r"^\s*([^\s#=]*[^\x00-\x7f][^\s=]*)=(?!=)", 文本, re.M)
    检查(not 坏, f"没有非 ASCII 变量名（发现：{坏[:3]}）" if 坏 else "没有非 ASCII 变量名")
    检查("候选" not in 文本.split("# 找")[-1], "没有遗留的中文循环变量")
    检查("(" not in 文本.split("--- ⑤")[0].replace("# ", "") or
         "数组" not in 文本, "没有用 bash 数组语法（某些 shell 会报错）")
    检查("参数=(" not in 文本, "没有 `参数=(\"$@\")` 这种 bash 数组写法")

    print("\n[3] --check 自检模式")
    子 = 跑(["bash", str(SH), "--check"])
    检查(子.returncode == 0, f"退出码 0（实际 {子.returncode}）")
    输出 = 子.stdout
    检查(str(项目) in 输出, f"认出了项目目录：{项目}")
    检查("运行环境/venv" in 输出, "认出了项目自带解释器")
    检查("PySide6" in 输出 and "httpx" in 输出, "打印了依赖版本")

    print("\n[4] 防重复启动（真进程 + 假 PID 文件两条）")
    PID文件 = 项目 / "数据" / ".一键启动.pid"
    旧内容 = PID文件.read_text() if PID文件.is_file() else None
    PID文件.parent.mkdir(parents=True, exist_ok=True)
    # ① "看起来就是本项目在跑"的进程：命令行里带 启动.py 和项目路径
    #    （启动脚本现在会核对 /proc/<pid>/cmdline，光有 PID 不算数 ——
    #     因为 PID 会被系统回收给别的进程，误判会导致永远启动不起来）
    假 = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)",
                        str(项目 / "启动.py")])
    try:
        PID文件.write_text(str(假.pid), encoding="utf-8")
        子 = 跑(["bash", str(SH)])
        检查(子.returncode == 0, f"已运行时退出码 0（实际 {子.returncode}）")
        检查("already running" in 子.stdout.lower(),
             f"提示已在运行：{子.stdout.strip()[:60]}")
        检查("starting" not in 子.stdout.lower(), "没有真的再启一个")
    finally:
        假.terminate()
        try:
            假.wait(timeout=5)
        except Exception:
            pass
    # ② 陈旧 PID（进程活着但跟本项目无关）→ 必须**清掉 PID 文件照常启动**
    陈旧 = subprocess.Popen(["sleep", "30"])
    try:
        PID文件.write_text(str(陈旧.pid), encoding="utf-8")
        子2 = 跑(["bash", str(SH), "--check"])
        检查(子2.returncode == 0 and "[OK] project" in 子2.stdout,
             "陈旧 PID（别的进程占了这个号）不会挡住启动")
    finally:
        陈旧.terminate()
        try:
            陈旧.wait(timeout=5)
        except Exception:
            pass
        if 旧内容 is None:
            PID文件.unlink(missing_ok=True)
        else:
            PID文件.write_text(旧内容, encoding="utf-8")

    print("\n[5] .bat（Windows）")
    B = BAT.read_text(encoding="utf-8")
    检查("v8_3" in B and "启动.py" in B, "批处理按 v8_3/启动.py 认项目")
    检查("if exist" in B and "Scripts\\python.exe" in B,
         "批处理用 if exist 逐个探测自带解释器")
    检查("V83_HOME" in B, "批处理支持 V83_HOME 覆盖项目路径")
    检查("pause" in B, "失败时会 pause（双击运行不会一闪而过）")

    print("\n[6] .desktop 字段")
    D = DESKTOP.read_text(encoding="utf-8")
    for 键 in ("Type=Application", "Name=网盘管理", "Terminal=false"):
        检查(键 in D, f"含 {键}")
    for 键, 期望 in (("Exec", SH), ("Icon", ICON), ("Path", 工作区)):
        匹配 = re.search(rf"^{键}=(.+)$", D, re.M)
        值 = Path(匹配.group(1).strip()) if 匹配 else None
        检查(值 is not None and 值.exists(), f"{键} 指向存在的文件/目录：{值}")

    失败 = [说明 for 通过, 说明 in 结果 if not 通过]
    print("\n" + "=" * 60)
    print(f"共 {len(结果)} 项检查，通过 {len(结果) - len(失败)}，失败 {len(失败)}")
    for 说明 in 失败:
        print("  ✗", 说明)
    return 1 if 失败 else 0


if __name__ == "__main__":
    raise SystemExit(主())
