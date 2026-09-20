#!/usr/bin/env python3
"""给界面**逐项计时**：找出 Windows 上"点一下卡几分钟"到底是哪一步慢。

用离屏平台跑（不需要显示器），量的是 CPU 侧的真实开销：
  ① 建主窗口（含启动自检）
  ② 切到每个功能页（传输 / 播放 / 敏感词 / AI / 日志 / 设置）
  ③ AI 页里三个子页（AI状态 / AI设置 / 模型商店）—— 模型商店卡片最多
  ④ 打开登录对话框、刷新状态
  ⑤ 追加 200 行日志
另外统计控件总数（QSS 复杂时，控件数直接决定重绘成本）。

用法：运行环境/venv/bin/python 工具/界面测速.py
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtWidgets import QApplication, QWidget     # noqa: E402

应用 = QApplication([])


def 泵(秒: float = 0.02) -> None:
    结束 = time.time() + 秒
    while time.time() < 结束:
        应用.processEvents()
        time.sleep(0.005)


def 计时(标签: str, 动作) -> float:
    t = time.perf_counter()
    动作()
    泵(0.005)
    耗 = time.perf_counter() - t
    print(f"{耗 * 1000:9.1f} ms  {标签}", flush=True)
    return 耗


def 控件数(部件) -> int:
    try:
        return len(部件.findChildren(QWidget))
    except Exception:
        return -1


from v8_3.配置 import 加载配置                        # noqa: E402
from v8_3.界面.主窗口 import 主窗口                   # noqa: E402

配置 = 加载配置("配置.json")
配置["AI"] = {**(配置.get("AI") or {}), "启用": False}

结果: dict[str, float] = {}
结果["建主窗口"] = 计时("① 建主窗口（含启动自检）", lambda: None)  # 占位，下面真建
t0 = time.perf_counter()
窗口 = 主窗口(dict(配置), 配置路径="配置.json")
窗口.show()
泵(0.05)
结果["建主窗口"] = time.perf_counter() - t0
print(f"{结果['建主窗口'] * 1000:9.1f} ms  ① 建主窗口（含启动自检）")
print(f"{控件数(窗口):9d} 个 主窗口里的 QWidget 数量")

for 名, 动作 in (
        ("传输页", lambda: 窗口.切换到传输页()),
        ("播放页", lambda: 窗口.切换到播放页()),
        ("敏感词页", lambda: 窗口.切换到敏感词页()),
        ("AI 页", lambda: 窗口.切换到AI页()),
        ("日志页", lambda: 窗口.切换到日志页()),
        ("设置页", lambda: 窗口.切换到设置页()),
):
    结果[名] = 计时(f"② 切到{名}", 动作)
    print(f"{控件数(窗口):9d} 个 控件（累计）")

AI页 = None
try:
    AI页 = 窗口.AI页面()
except Exception as e:
    print("取 AI 页失败：", e)
if AI页 is not None:
    for 子页 in ("AI状态", "AI设置", "模型商店"):
        方法 = getattr(AI页, f"切到{子页}", None) or getattr(AI页, f"显示{子页}", None)
        if 方法 is None:
            continue
        结果[子页] = 计时(f"③ AI → {子页}", 方法)
        print(f"{控件数(AI页):9d} 个 控件（AI 页内）")

计时("⑤ 追加 200 行日志", lambda: [窗口.追加日志(f"测速日志 {i}") for i in range(200)])
泵(0.3)
print(f"{控件数(窗口):9d} 个 控件（最终）")

print("\n=== 汇总（按耗时排序）===")
for 名, 耗 in sorted(结果.items(), key=lambda x: -x[1]):
    print(f"{耗 * 1000:9.1f} ms  {名}")
窗口.close()
