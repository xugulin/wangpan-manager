"""浏览器引擎层测试：接口 + 假引擎 + 用假引擎驱动的登录流程。

为什么这些测试重要
==================
「内置浏览器登录」以前只能用真 QtWebEngine 测（要显示器、要真网盘页面），
所以自检里只能测"窗口建得起来"。引擎抽出来之后，**登录流程本身可以完全离线测**：
把 cookie 预置进假引擎，界面流程照跑 —— 取凭证、判缺、回调、去重这些逻辑
现在都有断言兜着。

对应提交：重构引擎层（v8_3/界面/浏览器引擎.py + 引擎_QtWebEngine.py）。
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

项目根 = Path(__file__).resolve().parents[1]
if str(项目根) not in sys.path:
    sys.path.insert(0, str(项目根))
os.environ["QT_QPA_PLATFORM"] = "offscreen"   # 离屏跑；本机没有 wayland/xcb 显示

from PySide6.QtWidgets import QApplication  # noqa: E402

from v8_3.界面.浏览器引擎 import (  # noqa: E402
    假引擎, 建引擎, 引擎类型, 拼cookie头, 取cookie值, 规范cookie,
)

应用 = QApplication.instance() or QApplication(sys.argv[:1])


class 引擎接口测试(unittest.TestCase):
    def test_假引擎实现了全部接口(self):
        假 = 假引擎()
        for 名 in ("可用", "打开", "取视图", "截图", "点", "输入文本", "按键",
                 "取cookie", "清cookie", "关闭"):
            self.assertTrue(callable(getattr(假, 名, None)), f"缺少接口：{名}")

    def test_bytes_cookie_归一化(self):
        """PySide6 的 cookie 字段是 bytes（b'BDUSS'），必须归一成字符串。"""
        干净 = 规范cookie({"name": b"BDUSS", "value": b"x" * 5,
                       "domain": b".baidu.com", "path": b"/",
                       "httpOnly": True, "secure": True})
        self.assertEqual(干净["name"], "BDUSS")
        self.assertEqual(干净["domain"], ".baidu.com")
        self.assertEqual(干净["value"], "x" * 5)
        self.assertTrue(干净["httpOnly"])

    def test_脏数据名字被清理(self):
        """库里出现过字面量名字 "b'STOKEN'"（早期 bug 写进去的）。"""
        干净 = 规范cookie({"name": "b'STOKEN'", "value": "v"})
        self.assertEqual(干净["name"], "b'STOKEN'")   # 规范cookie 不管这个（读库时才修）
        from v8_3.界面.引擎_QtWebEngine import 读取cookie库   # noqa: F401
        # 该清理在 读取cookie库 里做，这里只确认它可导入（真库清了见界面自检）

    def test_按域名优先级取值(self):
        凭证 = [
            {"name": "STOKEN", "value": "passport", "domain": ".passport.baidu.com"},
            {"name": "STOKEN", "value": "pan", "domain": ".pan.baidu.com"},
            {"name": "BDUSS", "value": "B", "domain": ".baidu.com"},
        ]
        self.assertEqual(取cookie值(凭证, "STOKEN", (".pan.baidu.com",)), "pan")
        self.assertEqual(取cookie值(凭证, "STOKEN"), "passport")     # 没给优先级取第一个
        self.assertEqual(取cookie值(凭证, "NOPE"), "")

    def test_拼cookie头_白名单与去重(self):
        凭证 = [
            {"name": "BDUSS", "value": "B", "domain": ".baidu.com"},
            {"name": "BDUSS", "value": "B2", "domain": ".yoojia.com"},
            {"name": "STOKEN", "value": "S", "domain": ".pan.baidu.com"},
            {"name": "OTHER", "value": "O", "domain": ".baidu.com"},
        ]
        头 = 拼cookie头(凭证, ("BDUSS", "STOKEN"))
        self.assertIn("BDUSS=B;", 头 + ";")
        self.assertIn("STOKEN=S", 头)
        self.assertNotIn("OTHER", 头)
        self.assertEqual(头.count("BDUSS="), 1)        # 同名只拼一次

    def test_工厂能建假引擎与默认引擎(self):
        self.assertIn("假", 引擎类型)
        self.assertIn("qtwebengine", 引擎类型)
        self.assertIsInstance(建引擎("假"), 假引擎)
        # 默认引擎在没装 QtWebEngine 的机器上会退化成"不可用的假引擎"，不能抛异常
        self.assertIsNotNone(建引擎())


class 假引擎驱动登录流程测试(unittest.TestCase):
    """**不需要任何浏览器**就能测完整登录流程 —— 这就是重构的核心收益。"""

    def _建窗(self, 网盘类型="baidu", 引擎=None):
        from v8_3.界面.内置浏览器登录 import 内置浏览器登录窗口
        抓到: list[dict] = []
        窗 = 内置浏览器登录窗口(网盘类型, None,
                          完成回调=lambda c: 抓到.extend(c or []),
                          引擎=引擎 or 假引擎())
        窗.show()
        应用.processEvents()
        return 窗, 抓到

    def test_窗口只依赖引擎接口(self):
        引擎 = 假引擎()
        窗, _ = self._建窗(引擎=引擎)
        self.assertIs(窗.引擎, 引擎)
        self.assertTrue(any(x[0] == "打开" for x in 引擎.操作记录),
                        "窗口应该让引擎去打开登录页")
        窗.close()

    def test_完整会话自动回调(self):
        引擎 = 假引擎()
        窗, 抓到 = self._建窗(引擎=引擎)
        引擎.预置cookie([
            {"name": "BDUSS", "value": "B" * 32, "domain": ".baidu.com", "httpOnly": True},
            {"name": "STOKEN", "value": "S" * 32, "domain": ".pan.baidu.com"},
        ])
        引擎.触发加载完成(True)
        窗._查凭证()
        应用.processEvents()
        名字 = {c["name"] for c in 抓到}
        self.assertEqual(名字, {"BDUSS", "STOKEN"})
        self.assertIn("已捕获", 窗.状态标签.text())
        窗.close()

    def test_缺凭证不回调且提示缺什么(self):
        引擎 = 假引擎()
        窗, 抓到 = self._建窗(引擎=引擎)
        引擎.预置cookie([{"name": "BDUSS", "value": "B" * 32, "domain": ".baidu.com"}])
        窗._查凭证()
        应用.processEvents()
        self.assertEqual(抓到, [], "缺 STOKEN 时不该回调（否则写操作必失败）")
        self.assertIn("STOKEN", 窗.状态标签.text())
        窗.close()

    def test_同名cookie按域名去重(self):
        引擎 = 假引擎()
        窗, 抓到 = self._建窗(引擎=引擎)
        引擎.预置cookie([
            {"name": "BDUSS", "value": "老", "domain": ".baidu.com"},
            {"name": "BDUSS", "value": "新", "domain": ".baidu.com"},
            {"name": "STOKEN", "value": "S", "domain": ".pan.baidu.com"},
        ])
        引擎.触发加载完成(True)
        窗._查凭证()
        应用.processEvents()
        值 = [c["value"] for c in 抓到 if c["name"] == "BDUSS"]
        self.assertEqual(值, ["新"], "同 name+domain 只保留最新的一条")
        窗.close()

    def test_手动完成按钮也能回调(self):
        引擎 = 假引擎()
        窗, 抓到 = self._建窗(引擎=引擎)
        引擎.预置cookie([{"name": "BDUSS", "value": "B", "domain": ".baidu.com"}])
        窗.完成按钮.click()
        应用.processEvents()
        self.assertTrue(抓到, "手动点「我已登录完成」应把当前凭证交出去")
        self.assertIn("手动确认", 窗.状态标签.text())
        窗.close()

    def test_夸克需要三个cookie(self):
        引擎 = 假引擎()
        窗, 抓到 = self._建窗(网盘类型="quark", 引擎=引擎)
        引擎.预置cookie([
            {"name": "__pus", "value": "p", "domain": ".quark.cn"},
            {"name": "__kps", "value": "k", "domain": ".quark.cn"},
        ])
        窗._查凭证()
        应用.processEvents()
        self.assertEqual(抓到, [], "夸克缺 __uid 时不该回调")
        self.assertIn("__uid", 窗.状态标签.text())
        引擎.预置cookie([{"name": "__uid", "value": "u", "domain": ".quark.cn"}])
        窗._查凭证()
        应用.processEvents()
        self.assertEqual({c["name"] for c in 抓到}, {"__pus", "__kps", "__uid"})
        窗.close()

    def test_引擎不可用时给提示不崩(self):
        引擎 = 假引擎()
        引擎.设可用(False, "本包不含内置浏览器")
        窗, 抓到 = self._建窗(引擎=引擎)
        self.assertIn("本包不含内置浏览器", 窗.状态标签.text())
        self.assertEqual(抓到, [])
        窗.close()

    def test_关闭窗口会关引擎(self):
        引擎 = 假引擎()
        窗, _ = self._建窗(引擎=引擎)
        窗.close()
        应用.processEvents()
        self.assertTrue(引擎._已关闭, "关窗要释放引擎（profile/渲染进程）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
