"""关窗速度回归：点右上角的 × 必须"马上就没了"。

用户反馈（2026-09-21）：**点击右上方的 × 不能立即退出程序**。

原因（本地复现量到的）：`主窗口.closeEvent` 里的等待预算是 **20 秒 + 6 秒**，
而 Qt 是**先跑完 closeEvent 才隐藏窗口** —— 只要关窗时有后台线程在跑
（AI 页预热基座版本、模型市场刷新目录、凭证核对、正在传输……），
窗口就僵在屏幕上十几二十秒：

    有一个 30 秒的线程在跑 → close() 阻塞 26.0 秒

修完之后同一条复现脚本：`close() 返回耗时 1.6s，窗口还可见=False`。

另外还钉住两个**不能再犯**的坑：
  ① 收尾时**不能** `self._活动线程.clear()` —— 那会把还在运行的 QThread 交给 GC，
     Qt 直接 `Fatal Python error: Aborted`（实测：关窗后核心转储）。
     现在改成"摘下来"（`setParent(None)` + 记进 `遗留线程`），并由 `_收尾退出` 在
     进程退出时一并结束；
  ② 窗口必须在**等待之前**就隐藏（否则用户看到的是"点了没反应"）。
"""

from __future__ import annotations

import os
import sys
import threading
import time
import unittest
from pathlib import Path

# 没有显示环境时强制离屏：本机环境里 QT_QPA_PLATFORM="wayland;xcb"，
# setdefault 不会覆盖它，Qt 会在创建 QApplication 时直接 abort（整进程崩）。
if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
    os.environ["QT_QPA_PLATFORM"] = "offscreen"

项目根 = Path(__file__).resolve().parents[1]
if str(项目根) not in sys.path:
    sys.path.insert(0, str(项目根))

try:
    from PySide6.QtWidgets import QApplication
    Qt可用 = True
except Exception:  # noqa: BLE001 - 没装 PySide6 的机器上跳过
    Qt可用 = False

import v8_3.界面.主窗口 as 主窗口模块                              # noqa: E402
from v8_3.配置 import 加载配置                                    # noqa: E402


@unittest.skipUnless(Qt可用, "没装 PySide6")
class 关窗速度测试(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.应用 = QApplication.instance() or QApplication([])

    def _建窗口(self):
        配置 = 加载配置("配置.json")
        配置["AI"] = {**(配置.get("AI") or {}), "启用": False}
        窗口 = 主窗口模块.主窗口(dict(配置), 配置路径="配置.json")
        窗口.show()
        self._泵()
        return 窗口

    def _泵(self, 秒: float = 0.2) -> None:
        结束 = time.time() + 秒
        while time.time() < 结束:
            self.应用.processEvents()
            time.sleep(0.005)

    def test_关窗不必等慢线程(self):
        """关窗时有一个"卡住"的后台线程 → 也必须秒关，且线程被摘下来而不是被回收。"""
        from v8_3.界面.后台线程 import 任务线程
        窗口 = self._建窗口()
        放行 = threading.Event()
        self.addCleanup(放行.set)
        慢线程 = 任务线程(放行.wait, 父=窗口)      # 模拟"卡在网络/子进程上"的线程
        窗口.登记线程(慢线程)
        慢线程.start()
        self._泵(0.1)

        # 关窗时窗口必须是"已经隐藏"的状态（用户点 × 就该马上没）
        窗口可见记录: list[bool] = []
        原等 = 主窗口模块.主窗口._等在跑的线程

        def 包等(self, 秒):
            窗口可见记录.append(bool(self.isVisible()))
            return 原等(self, 秒)

        主窗口模块.主窗口._等在跑的线程 = 包等
        self.addCleanup(lambda: setattr(主窗口模块.主窗口, "_等在跑的线程", 原等))

        t0 = time.perf_counter()
        try:
            窗口.close()
        finally:
            self._泵(0.05)
            耗 = time.perf_counter() - t0

        self.assertLess(
            耗, 5.0,
            f"关窗花了 {耗:.1f} 秒 —— 用户要的是「点了就走」"
            "（预算是 1.0+0.6 秒，超了说明又被哪个 wait 拖住了）")
        self.assertTrue(窗口可见记录, "closeEvent 里根本没走等待那一步？")
        self.assertFalse(any(窗口可见记录),
                         "等待线程时窗口还是可见的：Qt 会先把 closeEvent 跑完才隐藏，"
                         "所以必须在 closeEvent 开头就 self.hide()")
        # 摘下来 = 不再受窗口销毁影响，也不会被 GC 掉（否则 Qt 直接 abort）
        self.assertIn(慢线程, 主窗口模块.遗留线程,
                      "还在跑的线程必须记进 遗留线程（否则解释器收尾时会被回收 → abort）")
        self.assertIsNone(慢线程.parent(),
                          "还在跑的线程要从窗口上摘下来（setParent(None)）")
        放行.set()
        慢线程.wait(5000)
        self._泵(0.1)
        主窗口模块.遗留线程.remove(慢线程)
        窗口.deleteLater()
        self._泵(0.05)

    def test_关窗预算是秒级(self):
        """预算是秒级，不是十几秒 —— 这两条数字就是用户那次「点了 × 不退出」的根因。"""
        self.assertLessEqual(主窗口模块.主窗口.关窗等待预算秒, 2.0)
        self.assertLessEqual(主窗口模块.主窗口.关窗收尾预算秒, 1.0)

    def test_收尾不再clear活动线程(self):
        """源码级守卫：``self._活动线程.clear()`` 会让运行中的 QThread 被 GC → abort。"""
        源 = (项目根 / "v8_3" / "界面" / "主窗口.py").read_text(encoding="utf-8")
        # 只看**整行就是这条语句**的情况：注释里引用旧写法（解释为什么不能这么写）不算
        语句行 = {行.strip() for 行 in 源.splitlines()}
        self.assertNotIn("self._活动线程.clear()", 语句行,
                         "关窗收尾里不能用 _活动线程.clear()：运行中的 QThread 被回收时 "
                         "Qt 会 Fatal Python error: Aborted（实测过，关窗后核心转储）")
        self.assertIn("_脱离还在跑的线程", 源)

    def test_关窗第一步是隐藏窗口(self):
        """行为级守卫：closeEvent 里 `self.hide()` 必须出现在等待线程之前。"""
        源 = (项目根 / "v8_3" / "界面" / "主窗口.py").read_text(encoding="utf-8")
        关窗 = 源[源.index("def closeEvent"):]
        关窗 = 关窗[:关窗.index("super().closeEvent")]
        self.assertIn("self.hide()", 关窗)
        self.assertLess(关窗.index("self.hide()"), 关窗.index("_等在跑的线程"),
                        "先把窗口藏起来，再去等/收尾 —— 顺序反了用户就看着窗口发呆")


if __name__ == "__main__":
    unittest.main()
