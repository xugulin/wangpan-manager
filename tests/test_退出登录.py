"""退出登录 / 重新登录 的回归测试（全程离线、离屏）。

用户现场反馈两条：
  1. 从原生 GUI 退出登录后，网盘管理这边「状态不对 —— 列表还在、还能上传下载」；
  2. GUI 里根本没有「退出登录 / 重新登录」入口。

覆盖：
  * 桥协议 ``logout``：删掉本地凭证文件、账号立刻回未登录、不谎报成功；
  * 未支持该能力的后端如实回「失败」而不是抛异常；
  * 界面：退出后清空列表 + 禁用写按钮 + 灭掉左侧绿点；
  * 界面：凭证被**外部**删除（模拟原 GUI 退出）能被监视器/轮询同步到。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

项目根 = Path(__file__).resolve().parents[1]
if str(项目根) not in sys.path:
    sys.path.insert(0, str(项目根))

os.environ["QT_QPA_PLATFORM"] = "offscreen"   # 离屏跑；本机没有 wayland/xcb 显示

from v8_3.配置 import 准备假网盘目录  # noqa: E402
from v8_3.核心.适配器 import 适配器规格  # noqa: E402
from v8_3.核心.子进程客户端 import 子进程适配器  # noqa: E402
from v8_3.核心.模型 import 网盘类型  # noqa: E402


class 退出登录协议测试(unittest.TestCase):
    """桥侧：logout 命令真的清凭证。"""

    def setUp(self):
        self.临时 = tempfile.TemporaryDirectory(prefix="v8_3_logout_")
        self.项目 = Path(self.临时.name) / "假盘"
        准备假网盘目录(self.项目)
        self.适配器 = 子进程适配器(
            适配器规格(类型=网盘类型.假, 项目根=str(self.项目), 线程数=2))
        self.适配器.启动()
        self.凭证 = self.项目 / "数据" / "登录.json"

    def tearDown(self):
        try:
            self.适配器.关闭()
        except Exception:
            pass
        self.临时.cleanup()

    def test_退出登录删掉凭证并回未登录(self):
        登录 = self.适配器.令牌登录("demo-access", "demo-refresh")
        self.assertEqual(登录.get("状态"), "成功")
        self.assertTrue(self.凭证.is_file(), "登录后应有凭证文件")
        结果 = self.适配器.退出登录()
        self.assertEqual(结果.get("状态"), "成功", 结果)
        self.assertFalse(self.凭证.is_file(), "退出登录必须删掉凭证文件")
        账号 = self.适配器.账号状态()
        self.assertFalse(账号.已登录, "退出登录后账号状态必须是未登录")

    def test_退出登录返回清除清单(self):
        self.适配器.令牌登录("demo-access", "demo-refresh")
        结果 = self.适配器.退出登录()
        清除 = [str(x) for x in (结果.get("清除") or [])]
        self.assertTrue(清除, f"应报告清除了哪些文件：{结果}")

    def test_未登录时退出登录也安全(self):
        结果 = self.适配器.退出登录()
        self.assertIn(结果.get("状态"), ("成功", "失败"))
        self.assertTrue(str(结果.get("消息") or ""), "必须有可读消息")


class 退出登录界面测试(unittest.TestCase):
    """界面侧：退出后状态、列表、按钮、绿点全部同步。"""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.应用 = QApplication.instance() or QApplication([])

    def setUp(self):
        from v8_3.配置 import 加载配置, 保存配置
        from v8_3.界面.主窗口 import 主窗口
        self.临时 = tempfile.TemporaryDirectory(prefix="v8_3_logout_ui_")
        self.根 = Path(self.临时.name)
        目录 = self.根 / "假盘"
        准备假网盘目录(目录)
        云端 = 目录 / "云端"
        云端.mkdir(parents=True, exist_ok=True)
        (云端 / "a.txt").write_text("hello", encoding="utf-8")
        配置 = 加载配置(self.根 / "配置.json")
        配置["适配器"] = [{
            "标识": "fake_1", "类型": "fake", "名称": "假网盘一号",
            "路径": str(目录), "线程数": 2, "启用": True,
        }]
        配置["数据库路径"] = str(self.根 / "任务.db")
        配置["AI"] = {"启用": False, "api密钥": ""}
        保存配置(配置, self.根 / "配置.json")
        self.窗口 = 主窗口(dict(配置), 配置路径=self.根 / "配置.json")
        self.窗口.show()
        self.泵(0.3)
        self.页 = self.窗口._网盘页面["fake_1"]
        self.凭证 = Path(self.窗口.动作.规格("fake_1").凭证文件)

    def tearDown(self):
        try:
            self.窗口.close()
        except Exception:
            pass
        self.临时.cleanup()

    def 泵(self, 秒: float = 0.2) -> None:
        import time
        from PySide6.QtWidgets import QApplication
        结束 = time.time() + 秒
        while time.time() < 结束:
            QApplication.processEvents()
            time.sleep(0.02)

    def 等待(self, 条件, 超时: float = 20.0) -> bool:
        import time
        结束 = time.time() + 超时
        while time.time() < 结束:
            if 条件():
                return True
            self.泵(0.1)
        return bool(条件())

    def test_登录后退出把界面收干净(self):
        适配器 = self.窗口.动作.适配器("fake_1")
        self.assertEqual(适配器.令牌登录("a", "b").get("状态"), "成功")
        # 登录成功会让页面清缓存（登录后立即加载），这里再强制刷一次更稳
        self.页._刷新管理区(强制=True)
        self.assertTrue(self.等待(lambda: "已登录" in self.页.状态标签.text()),
                        f"应显示已登录：{self.页.状态标签.text()}")
        self.assertTrue(self.等待(lambda: self.页.文件表格.rowCount() > 0),
                        "登录后应自动列出文件（login 后立即加载）")
        self.assertTrue(self.页.上传按钮.isEnabled())

        self.assertTrue(self.页._退出登录(静默=True), "退出登录应被受理")
        self.assertTrue(self.等待(lambda: not self.凭证.is_file()),
                        "凭证文件应被删除")
        self.assertTrue(self.等待(lambda: "未登录" in self.页.状态标签.text()),
                        f"状态栏应收为未登录：{self.页.状态标签.text()}")
        self.assertEqual(self.页.文件表格.rowCount(), 0, "列表必须清空")
        self.assertFalse(self.页.上传按钮.isEnabled(), "上传按钮必须禁用")
        self.assertFalse(self.页.下载按钮.isEnabled(), "下载按钮必须禁用")
        self.assertFalse(self.页.删除按钮.isEnabled(), "删除按钮必须禁用")
        self.assertFalse(self.窗口._网盘状态.get("fake_1"), "绿点必须灭掉")

    def test_凭证被外部删除后界面同步(self):
        """模拟：在原生 GUI 里退出登录（凭证文件消失），本页要跟着变。"""
        适配器 = self.窗口.动作.适配器("fake_1")
        适配器.令牌登录("a", "b")
        self.页._刷新管理区(强制=True)
        self.assertTrue(self.等待(lambda: self.页.文件表格.rowCount() > 0),
                        "先要有列表，才谈得上'退出后还在不在'")
        self.凭证.unlink()
        self.页._凭证有变化()          # 文件监视器回调（等同真实事件）
        self.assertTrue(self.等待(lambda: "未登录" in self.页.状态标签.text()),
                        f"外部删除凭证后应收为未登录：{self.页.状态标签.text()}")
        self.assertEqual(self.页.文件表格.rowCount(), 0, "外部退出也要清空列表")
        self.assertFalse(self.页.上传按钮.isEnabled(), "外部退出也要禁用写按钮")

    def test_页面有退出与重新登录按钮(self):
        self.assertIn("退出登录", self.页.退出登录按钮.text())
        self.assertIn("重新登录", self.页.重新登录按钮.text())
        self.assertTrue(self.页.退出登录按钮.isEnabled())
        self.assertTrue(self.页.重新登录按钮.isEnabled())


if __name__ == "__main__":
    unittest.main(verbosity=2)
