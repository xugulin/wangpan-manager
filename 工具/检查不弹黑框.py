#!/usr/bin/env python3
"""在**真 Windows** 上验证"起子进程不弹黑框"（VIP 用户反馈的那两个大黑框）。

用户实测：Windows 版"下载 AI 模型"时会弹出两个关不掉的大黑框
（标题是 ``…\\运行环境\\本地模型\\ollama.exe``）—— 一个是 ``ollama serve``、
一个是 ``ollama pull``。GUI 程序起控制台程序时，Windows 默认给它**新开一个控制台
窗口**；关掉那个窗口就等于中断下载，所以体验很差。

判据（只有真 Windows 才做得出来，所以放在 CI 的 Windows runner 上跑）：

* 用一个**GUI 子系统**的解释器（``pythonw.exe``，它自己完全没有控制台）当父进程；
* 让它分别用"裸 subprocess"和"``v8_3.进程.起``（带 CREATE_NO_WINDOW）"各起一个
  控制台子进程，子进程打印自己的 ``GetConsoleWindow()``：
  - 裸起 → 父进程没有控制台，Windows 会给子进程**新建**一个（句柄非 0 → 就是那个黑框）；
  - 我们的起法 → 明确要求"不创建控制台"（句柄为 0 → 没有黑框）。

用法（真 Windows / Wine，项目根下）::

    运行环境\\python\\pythonw.exe 工具\\检查不弹黑框.py     # 退出码 0 = 通过
    运行环境/venv/bin/python 工具/检查不弹黑框.py           # 非 Windows 直接跳过
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

项目根 = Path(__file__).resolve().parents[1]
if str(项目根) not in sys.path:
    sys.path.insert(0, str(项目根))

for 流 in (sys.stdout, sys.stderr):
    try:
        流.reconfigure(encoding="utf-8", errors="replace")   # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass

#: 子进程要打印的东西：自己的控制台窗口句柄 + 控制台里的进程数
探测代码 = (
    "import ctypes,sys;"
    "k=ctypes.windll.kernel32;"
    "h=int(k.GetConsoleWindow());"
    "buf=(ctypes.c_ulong*4)();"
    "n=int(k.GetConsoleProcessList(buf,4));"
    "print('CONSOLE',h,n);"
    "sys.exit(0 if h==0 else 3)"
)


def 找解释器(名字: str) -> str | None:
    """在包内找 python.exe / pythonw.exe。"""
    候选 = [
        项目根 / "运行环境" / "python" / 名字,
        项目根 / "运行环境" / "venv" / "Scripts" / 名字,
        项目根 / "运行环境" / "venv" / "bin" / 名字,
    ]
    for 路径 in 候选:
        if 路径.is_file():
            return str(路径)
    import shutil
    return shutil.which(名字)


def main() -> int:
    if os.name != "nt":
        print("非 Windows：控制台窗口是 Windows 独有的问题，跳过")
        return 0
    from v8_3.进程 import 起并等待, 无窗口参数

    父 = 找解释器("pythonw.exe")     # GUI 子系统：它自己没有控制台
    子 = 找解释器("python.exe")
    print(f"父（GUI，无控制台）：{父}")
    print(f"子（控制台程序）：{子}")
    if not 父 or not 子:
        print("✗ 找不到包内解释器")
        return 1

    # ① 对照：GUI 父进程 + 裸起控制台子进程 → Windows 会给子进程新建控制台（= 黑框）
    对照脚本 = 项目根 / "数据" / "_对照裸起.py"
    对照脚本.parent.mkdir(parents=True, exist_ok=True)
    对照脚本.write_text(
        "import subprocess, sys\n"
        f"r = subprocess.run([r'{子}', '-c', {探测代码!r}], capture_output=True, text=True)\n"
        "print(r.stdout.strip())\n"
        "sys.exit(0 if 'CONSOLE 0' not in r.stdout else 4)\n",
        encoding="utf-8")
    # ② 我们的做法：GUI 父进程 + v8_3.进程.起 → 明确不创建控制台
    我们脚本 = 项目根 / "数据" / "_对照我们的起法.py"
    我们脚本.write_text(
        "import sys\n"
        f"sys.path.insert(0, r'{项目根}')\n"
        "from v8_3.进程 import 起并等待, 无窗口参数\n"
        f"r = 起并等待([r'{子}', '-c', {探测代码!r}], capture_output=True, text=True, **无窗口参数())\n"
        "print(r.stdout.strip())\n"
        "sys.exit(0 if 'CONSOLE 0' in r.stdout else 3)\n",
        encoding="utf-8")

    def 经GUI父进程跑(脚本: Path) -> tuple[int, str]:
        进程 = subprocess.run([父, str(脚本)], capture_output=True, text=True, timeout=120)
        return 进程.returncode, (进程.stdout or "").strip()

    码1, 出1 = 经GUI父进程跑(对照脚本)
    码2, 出2 = 经GUI父进程跑(我们脚本)
    对照拿到控制台 = ("CONSOLE 0" not in 出1)
    我们拿到控制台 = ("CONSOLE 0" not in 出2)
    print(f"（对照）裸 subprocess：{出1 or '（无输出）'}｜退出码 {码1}"
          f"｜{'拿到了控制台窗口（就是那个黑框）' if 对照拿到控制台 else '没拿到控制台'}")
    print(f"（我们）v8_3.进程.起：{出2 or '（无输出）'}｜退出码 {码2}"
          f"｜{'❌ 还是拿到了控制台' if 我们拿到控制台 else '✅ 没有控制台窗口'}")

    判定 = "通过" if not 我们拿到控制台 else "失败"
    结果行 = (f"{判定}\n"
            f"对照（裸起）拿到控制台：{对照拿到控制台}\n"
            f"我们（v8_3.进程.起）拿到控制台：{我们拿到控制台}\n"
            f"对照输出：{出1}\n我们输出：{出2}\n")
    try:                       # 退出码在 GUI 子系统下不可靠（pwsh 不等待），
        结果文件 = 项目根 / "数据" / "不弹黑框检查.txt"   # 所以同时写文件给 CI 判定
        结果文件.parent.mkdir(parents=True, exist_ok=True)
        结果文件.write_text(结果行, encoding="utf-8")
        print(f"结果已写入：{结果文件}")
    except Exception as 错:  # noqa: BLE001
        print(f"（结果文件写不了：{错}）")
    if 我们拿到控制台:
        print("✗ 我们的起法仍然让子进程拿到了控制台窗口（会弹黑框）")
        return 1
    if not 对照拿到控制台:
        print("⚠️ 这台机器上裸起也没拿到控制台（判据没法对照），"
              "但我们明确带了 CREATE_NO_WINDOW，按通过处理")
    print("✅ 起子进程不会弹黑框（CREATE_NO_WINDOW 生效）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
