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

# ④ 工具栏三档到底要多宽 —— Windows 上中文字体/emoji 更宽，这一项最能说明问题
try:
    from v8_3.界面.vlc风格 import 构建工具栏
    表 = {k: (lambda *a, **kw: None) for k in
         ("打开网盘", "打开本地", "播放暂停", "停止", "上一个", "下一个",
          "全屏", "截图", "切换清单", "切换AI面板", "切换侧栏", "静音切换",
          "设置音量", "设置速度", "循环切换", "随机切换", "AI诊断")}
    表["是否静音"] = lambda: False
    表["当前速度"] = lambda: 1.0
    栏 = 构建工具栏(表)
    print("\n④ 工具栏各档需要的宽度：")
    for 模式 in 栏.模式们:
        栏.设置模式(模式, 1920)
        宽 = int(栏.sizeHint().width())
        标记 = ""
        if 模式 == "完整":
            标记 = f"→ 需要 ≥{宽 + 4}px 的窗口才用完整文字"
        elif 模式 == "精简":
            标记 = f"→ 需要 ≥{宽 + 4}px"
        else:
            标记 = "→ 极窄时才用；再窄会自动收起音量/循环/随机/上一/下一"
        print(f"{宽:9d} px  {模式}档 {标记}")
        结果[f"工具栏-{模式}档宽度"] = 宽 / 1000.0
except Exception as e:  # noqa: BLE001
    print("工具栏测宽失败：", e)

# ⑥ 微基准：建标签/填文字到底卡在哪 —— Windows 上 AI 页构造要 24 秒，刷新又 24 秒。
#    三个对照：纯中文 / 带 emoji（Windows 要查彩色字体回退）/ 纯 ASCII，
#    每个都量"建 200 个 + 真正布局绘制一次"的耗时。
try:
    from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget
    print("\n⑥ 建标签 + 首帧（200 个一组）：")
    for 名, 文本 in (("纯中文", "模型设置{}"), ("带 emoji", "🤖 模型设置{}"),
                   ("纯 ASCII", "model-{}")):
        底 = QWidget()
        布局 = QVBoxLayout(底)
        t0 = time.time()
        标签们 = [QLabel(文本.format(i)) for i in range(200)]
        for 标 in 标签们:
            布局.addWidget(标)
        建 = (time.time() - t0) * 1000
        底.resize(600, 400)
        t0 = time.time()
        底.grab()                      # 触发真正的样式匹配 + 布局 + 绘制
        画 = (time.time() - t0) * 1000
        底.deleteLater()
        泵(0.05)
        print(f"{建:9.1f} ms 建 200 个 + {画:9.1f} ms 首帧    {名}")
        结果[f"建标签-{名}"] = (建 + 画) / 1000.0
except Exception as e:  # noqa: BLE001
    print("微基准失败：", e)

print("\n=== 汇总（按耗时排序）===")
for 名, 耗 in sorted(结果.items(), key=lambda x: -x[1]):
    print(f"{耗 * 1000:9.1f} ms  {名}")
窗口.close()
