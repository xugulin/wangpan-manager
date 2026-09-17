#!/usr/bin/env python3
"""V8_3 环境自检：依赖、适配器路径、登录数据、假网盘桥。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

项目根 = Path(__file__).resolve().parents[1]
if str(项目根) not in sys.path:
    sys.path.insert(0, str(项目根))

# 自举：不是项目自带的解释器就换过去，避免用系统 python 跑出
# "No module named 'PySide6' / 'httpx'"。详见 v8_3/自举.py
from v8_3.自举 import 确保项目环境

确保项目环境()

from v8_3.配置 import 加载配置, 适配器规格表, 网盘实例列表
from v8_3.核心.适配器 import 适配器规格, 类型图标
from v8_3.核心.模型 import 网盘类型


def main() -> int:
    配置 = 加载配置()
    print(f"Python：{sys.version.split()[0]} ({sys.executable})")
    print(f"项目根：{项目根}")
    失败 = []
    for 模块 in ("httpx", "PySide6"):
        ok = importlib.util.find_spec(模块) is not None
        print(f"依赖 {模块:<10}：{'OK' if ok else '缺失'}")
        if not ok:
            失败.append(模块)
    print("-" * 60)
    实例们 = 网盘实例列表(配置)
    if not 实例们:
        print("尚未配置任何网盘（界面左下角「新增网盘」或 CLI `网盘新增`）")
    规格表 = 适配器规格表(配置)
    for 项 in 实例们:
        规格 = 规格表.get(项["标识"])
        if 规格 is None:
            图标 = 类型图标.get(网盘类型.解析(项['类型']), "☁")
            print(f"{图标} {项['名称']:<10} 已停用（标识={项['标识']}）")
            continue
        存在 = 规格.路径.is_dir()
        核心 = (规格.路径 / "核心").is_dir()
        可用, 提示 = 适配器规格.校验目录(规格.路径)
        打印 = [
            f"{规格.图标} {规格.显示名:<10}",
            f"标识={规格.标识:<10}",
            f"目录={'OK' if 存在 and 核心 else '缺失'}",
        ]
        凭证 = 规格.凭证文件
        打印.append(f"登录数据={'有' if 凭证.is_file() else '无'}")
        打印.append(f"Python={规格.Python解释器}")
        print("  ".join(打印))
        if not 可用:
            print(f"    提示：{提示}")
            失败.append(规格.显示名)
    return 1 if 失败 else 0


if __name__ == "__main__":
    raise SystemExit(main())
