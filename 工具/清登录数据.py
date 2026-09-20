#!/usr/bin/env python3
"""清掉本机所有网盘登录数据（之后只能靠 GUI 重新登录）。

用途：
  * 换账号 / 借人测试前，把登录态清干净；
  * 想验证"纯 GUI 登录"是否真的能走通（清完必须重新登录）。

清什么（**只删登录态，不动业务数据**）：

| 路径 | 是什么 |
|---|---|
| `适配器/百度网盘适配器/数据/会话.json` | 百度 BDUSS + pan 域 STOKEN |
| `适配器/夸克网盘适配器/数据/凭证.json` | 夸克 Cookie |
| `适配器/光鸭云盘适配器/数据/令牌.json` | 光鸭访问令牌 |
| `数据/内置浏览器/` | **内置浏览器的 profile**（里面是浏览器登录态，也是"自动补全会话"的来源） |
| `数据/终端二维码.png` | 终端扫码留下的临时二维码图 |

**保留**：`上传记录.json`、`设备信息.json`、`敏感词.db`、AI 学习库、界面日志 —— 这些不是登录数据。

用法：
    运行环境/venv/bin/python 工具/清登录数据.py            # 清掉
    运行环境/venv/bin/python 工具/清登录数据.py --看一眼    # 只列出、不删
"""
from __future__ import annotations

import argparse
import shutil
from pathlib import Path

项目根 = Path(__file__).resolve().parents[1]

要清的 = (
    ("适配器/百度网盘适配器/数据/会话.json", "百度 BDUSS + pan 域 STOKEN"),
    ("适配器/夸克网盘适配器/数据/凭证.json", "夸克 Cookie"),
    ("适配器/光鸭云盘适配器/数据/令牌.json", "光鸭访问令牌"),
    ("数据/内置浏览器", "内置浏览器 profile（登录态 + 自愈来源）"),
    ("数据/终端二维码.png", "终端扫码临时图"),
)


def 大小(路径: Path) -> str:
    if 路径.is_dir():
        总 = sum(f.stat().st_size for f in 路径.rglob("*") if f.is_file())
        return f"{总 / 1048576:.1f} MB"
    return f"{路径.stat().st_size} 字节"


def 主() -> int:
    解析 = argparse.ArgumentParser(description="清掉本机所有网盘登录数据")
    解析.add_argument("--看一眼", action="store_true", help="只列出要删什么，不删")
    参数 = 解析.parse_args()
    清掉 = 0
    for 相对, 说明 in 要清的:
        路径 = 项目根 / 相对
        if not 路径.exists():
            print(f"（不存在）            {相对}   —— {说明}")
            continue
        前 = 大小(路径)
        if 参数.看一眼:
            print(f"要删 {前:>10}  {相对}   —— {说明}")
            continue
        if 路径.is_dir():
            shutil.rmtree(路径, ignore_errors=True)
        else:
            路径.unlink(missing_ok=True)
        if 路径.exists():
            print(f"❌ 没删掉（权限？）  {相对}")
            return 1
        print(f"✅ 已删 {前:>10}  {相对}   —— {说明}")
        清掉 += 1
    if 参数.看一眼:
        print("\n（--看一眼：什么都没删）")
        return 0
    print(f"\n共清掉 {清掉} 项。现在打开程序，三家网盘都应显示「⚪ 未登录」，"
          "需要在 GUI 里重新登录。")
    print("提示：清掉内置浏览器 profile 后，「自动补全会话」也就不存在了 —— "
          "这正是「纯 GUI 登录」要验证的状态。")
    return 0


if __name__ == "__main__":
    raise SystemExit(主())
