# -*- coding: utf-8 -*-
"""界面线程安全回归测试 —— 专防 2026-09-14 的段错误

背景
----
`界面/页面/日志页.py` 把 logging.Handler 挂在 root logger 上，而 logging 是
**同步**的：工作线程打日志 → 就在工作线程里跑 handler.emit()。旧实现直接
在 emit() 里 `QTextEdit.append()` + `moveCursor()`，于是上传线程会和主线程
的重绘抢同一个 QTextLayout，触发 SIGSEGV（core dump 栈顶即
`QWidgetTextControl::moveCursor`）。

为什么必须有这个测试
--------------------
跨线程改 QWidget 是**数据竞争**，只有在主线程**同时重绘同一控件**时才炸。
普通离屏测试里主线程基本空闲，竞争窗口极小 —— 所以上线前的离屏测试全绿，
真实 GUI 一传文件夹就崩。本测试专门把这个竞争窗口拉到最大：

    · 多个工作线程以最大速度打日志（模拟上传调用链）
    · 主线程以最小时隔反复 repaint() 同一个日志控件

通过判据
--------
  1. 整个压测期间**进程不崩溃**（无 SIGSEGV）
  2. 日志确实被渲染进控件（证明投递通路真的工作，不是「因为没干活所以不崩」）

退出码：0 = 通过，非 0 = 失败
"""
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QThread, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from 界面.页面.日志页 import 日志页  # noqa: E402

每线程条数 = 4000
线程数 = 4
压测秒数 = 12.0


class 打日志线程(QThread):
    """模拟 批量上传线程：run() 里只打日志，不碰任何控件"""

    def __init__(self, 序号: int):
        super().__init__()
        self.序号 = 序号

    def run(self):
        日志 = logging.getLogger(f"夸克网盘.上传.模拟{self.序号}")
        for i in range(每线程条数):
            日志.info(f"[上传] 线程{self.序号} 第 {i} 个文件 → file/update/hash")
            if i % 500 == 0:
                日志.warning(f"[上传] 线程{self.序号} 模拟告警 {i}")


def main() -> int:
    应用 = QApplication.instance() or QApplication([])

    页 = 日志页(None)
    页.resize(800, 600)
    页.show()                      # 必须 show，否则不会真正走绘制路径
    页.添加日志处理器()             # ← 被测对象

    # ⚠️ 必须显式设置 root logger 级别！
    #    root 默认是 WARNING，若不设，logger.info() 会在**生成记录之前**
    #    就被 isEnabledFor() 拦掉，handler 根本收不到 —— 测试会退化成
    #    只压 32 条 warning，看着通过其实几乎没测（这是本测试写出来时
    #    真实踩过的坑）。真实运行里 主界面.py 用 basicConfig(level=DEBUG)，
    #    所以这里也必须放开到 DEBUG 才能还原真实流量。
    根 = logging.getLogger()
    根.setLevel(logging.DEBUG)
    根.addHandler(logging.NullHandler())   # 避免 "No handlers" 提示，且不刷屏

    # 主线程疯狂重绘同一控件，最大化竞争窗口（事故的触发条件）
    重绘次数 = [0]

    def 逼重绘():
        页.viewport().update()
        页.repaint()
        重绘次数[0] += 1

    计时器 = QTimer()
    计时器.timeout.connect(逼重绘)
    计时器.start(0)                 # 尽可能密

    print(f"[回归] 启动 {线程数} 个工作线程，每线程 {每线程条数} 条日志")
    print(f"[回归] 主线程持续 repaint()，压测 {压测秒数:.0f} 秒")

    线程们 = [打日志线程(i) for i in range(线程数)]
    for t in 线程们:
        t.start()

    起 = time.time()
    while time.time() - 起 < 压测秒数:
        应用.processEvents()        # 让排队来的日志真正被 append
        time.sleep(0.001)

    for t in 线程们:
        t.wait(5000)

    # 再多跑一会儿事件循环，确保队列排空
    排空截止 = time.time() + 2.0
    while time.time() < 排空截止:
        应用.processEvents()
        time.sleep(0.001)

    已渲染行数 = 页.document().blockCount()
    总条数 = 线程数 * 每线程条数 + 线程数 * (每线程条数 // 500)

    print(f"[回归] 主线程 repaint() 次数：{重绘次数[0]:,}")
    print(f"[回归] 日志页已渲染 {已渲染行数:,} 块（上限 5000，超出会滚动淘汰）")
    print(f"[回归] 工作线程发日志约 {总条数:,} 条")

    # 判据 1：能走到这里就说明没崩（崩溃会直接 SIGSEGV 掉进程）
    # 判据 2：确实渲染了内容 —— 否则「不崩」可能只是因为压根没干活
    #         阈值取 1000：本轮应产生约 16032 条，块数上限 5000，
    #         所以正常情况下 blockCount 会顶到 5000 左右。
    #         若哪天又退化成「大部分日志没进 handler」，这里会立刻报警。
    if 已渲染行数 < 1000:
        print(f"[回归] ✘ 失败：只渲染了 {已渲染行数} 块，远少于预期 —— "
              f"日志投递通路可能被削弱（root 级别？过滤器？handler 级别？）")
        return 1

    print("[回归] ✔ 通过：无崩溃，且日志确实渲染进了控件")
    return 0


if __name__ == "__main__":
    sys.exit(main())
