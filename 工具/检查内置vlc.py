#!/usr/bin/env python3
"""检查"包内自带的 VLC"能不能用（发布包必须内置，Windows 上尤其）。

背景（用户报的致命问题）
========================
Windows 版发布出去后点播放，播放页直接报
``libvlc 不可用：找不到 libvlc（VLC 的运行库）`` —— Windows 不像桌面 Linux 自带 VLC，
上一版又没内置，"在线看 4K 视频"在 Windows 上等于没有。现在包里带官方 VLC 运行时
（``运行环境/vlc``），这个脚本就是**钉住它**用的：

* 在**没装 VLC 的机器**上（CI 的干净 runner、Wine 环境）跑，必须报"可用"；
* 同时打印实际加载的 DLL 路径与版本，方便排查。

用法（项目根下，用包内解释器）::

    运行环境/venv/bin/python 工具/检查内置vlc.py     # 退出码 0 = 通过

为什么单独做成脚本、而不是让 CI 用 ``python -c "…"``：Windows 的 ``python -c``
走命令行编码（cp1252/GBK），源码里的中文会直接抛 UnicodeEncodeError ——
实测把 CI 卡红过一次。写成 UTF-8 文件就没这个问题。
"""

from __future__ import annotations

import sys
from pathlib import Path

# Windows 控制台默认是 GBK/cp936，打印 ✅ 这类字符会抛 UnicodeEncodeError
# （Wine 实测）。这里把输出流切到 UTF-8 并允许替换，脚本本身就不再因"打印"失败。
for 流 in (sys.stdout, sys.stderr):
    try:
        流.reconfigure(encoding="utf-8", errors="replace")   # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass

项目根 = Path(__file__).resolve().parents[1]
if str(项目根) not in sys.path:
    sys.path.insert(0, str(项目根))


def main() -> int:
    from v8_3.播放 import vlc绑定 as 绑定

    库目录 = 绑定.自带库目录()
    print(f"包内 VLC 目录：{库目录}")
    if 库目录 is None:
        print("✗ 包里没有自带 VLC（运行环境/vlc/libvlc.dll）—— Windows 版必须内置")
        return 1
    插件 = 库目录 / "plugins"
    插件数 = len(list(插件.rglob("*.dll"))) if 插件.is_dir() else 0
    print(f"插件数：{插件数}（解码/HTTP/输出等）")
    if 插件数 < 50:
        print("✗ 插件不全，播放会失败")
        return 1
    许可 = [名 for 名 in ("COPYING.txt", "COPYING", "COPYING.LIB")
          if (库目录 / 名).is_file()]
    if not 许可:
        print("✗ 缺 VLC 的许可证文件（GPL/LGPL 要求随包分发）")
        return 1
    print(f"许可证：{'、'.join(许可)}")
    if not 绑定.可用():
        print(f"✗ 内置 VLC 加载失败：{绑定.不可用原因()}")
        return 1
    库 = 绑定.VLC库.取()
    print(f"实际加载：{库.路径}")
    print(f"libvlc 版本：{绑定.VLC库.版本()}")
    路径 = Path(str(库.路径)).resolve()
    if 库目录.resolve() not in 路径.parents and 路径.parent != 库目录.resolve():
        print(f"⚠️ 加载的不是包内那份（{路径}）—— 系统里也装了 VLC？"
              "自带优先的查找链可能没生效")
    print("✅ 内置 VLC 可用")
    # 顺手体检：Windows 上起子进程必须带 CREATE_NO_WINDOW（否则会弹黑框 ——
    # VIP 用户实测"下载模型时弹出两个大黑框"，就是 ollama serve/pull）。
    from v8_3.进程 import 无窗口参数
    参数 = 无窗口参数()
    if sys.platform == "win32":
        标志 = int(参数.get("creationflags", 0))
        if not (标志 & 0x08000000):
            print(f"✗ Windows 上起子进程没带 CREATE_NO_WINDOW（{标志}）：下载模型会弹黑框")
            return 1
        print(f"✅ 起子进程不弹黑框（creationflags={hex(标志)}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
