"""测试统一登录协议（6 种方式）：能力表 + 各方式走桥的真实往返。

用假网盘后端（离线、无网络），覆盖：
  * ``auth_caps`` 能力表结构与"未支持方式"的说明；
  * 扫码（图片二维码 → 等待 → 成功）、取消；
  * 导入 Cookie（成功 / 失败路径）；
  * 短信（发送 → 校验成功 / 验证码错误）；
  * 令牌登录（成功 / 空令牌失败）；
  * 邮箱 / 账号密码：适配器未提供 → 返回"不支持"而不是抛异常。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

项目根 = Path(__file__).resolve().parents[1]
if str(项目根) not in sys.path:
    sys.path.insert(0, str(项目根))

from v8_3.配置 import 准备假网盘目录
from v8_3.核心.适配器 import 适配器规格
from v8_3.核心.子进程客户端 import 子进程适配器
from v8_3.核心.模型 import 网盘类型

#: 六种标准登录方式（与 后端_基类.登录方式 一致）
方式顺序 = ("qrcode", "cookie", "sms", "email", "token", "password")


class 统一登录测试(unittest.TestCase):
    def setUp(self):
        self.临时 = tempfile.TemporaryDirectory(prefix="v8_3_login_")
        self.根 = Path(self.临时.name)
        self.项目 = self.根 / "假盘"
        准备假网盘目录(self.项目)
        self.适配器 = 子进程适配器(
            适配器规格(类型=网盘类型.假, 项目根=str(self.项目), 线程数=4))
        self.适配器.启动()

    def tearDown(self):
        try:
            self.适配器.关闭()
        except Exception:
            pass
        self.临时.cleanup()

    # ---------------- 能力表 ----------------

    def test_六种方式都在能力表里(self):
        表 = self.适配器.登录方式表()
        self.assertEqual(list(表), list(方式顺序))
        for 键, 值 in 表.items():
            self.assertIn("支持", 值)
            self.assertTrue(值["名称"])
            self.assertTrue(值["说明"])
        self.assertTrue(表["qrcode"]["支持"])
        self.assertTrue(表["cookie"]["支持"])
        self.assertTrue(表["sms"]["支持"])
        self.assertTrue(表["token"]["支持"])
        self.assertFalse(表["email"]["支持"])
        self.assertFalse(表["password"]["支持"])
        self.assertIn("未提供", 表["email"]["说明"])

    def test_支持方式列表(self):
        self.assertEqual(set(self.适配器.支持登录方式()),
                        {"qrcode", "cookie", "sms", "token"})

    # ---------------- 扫码 ----------------

    def test_扫码登录成功并写入凭证(self):
        开始 = self.适配器.扫码开始()
        self.assertEqual(开始.get("类型"), "图片")
        self.assertTrue(开始.get("图片base64"))
        self.assertTrue(开始.get("会话"))
        结果 = self.适配器.扫码等待(开始["会话"], 超时秒=5)
        self.assertEqual(结果.get("状态"), "成功")
        self.assertTrue(结果.get("账号", {}).get("logged_in"))
        凭证 = (self.项目 / "数据" / "登录.json")
        self.assertTrue(凭证.is_file())
        self.assertIn("qrcode", 凭证.read_text(encoding="utf-8"))

    def test_扫码取消返回已取消(self):
        开始 = self.适配器.扫码开始()
        结果 = self.适配器.扫码取消(开始.get("会话", ""))
        self.assertEqual(结果.get("状态"), "已取消")

    # ---------------- Cookie ----------------

    def test_导入Cookie成功(self):
        结果 = self.适配器.Cookie登录("BDUSS=demo-value; STOKEN=demo-value")
        self.assertEqual(结果.get("状态"), "成功")
        self.assertIn("账号", 结果)

    def test_导入Cookie失败会给出提示(self):
        结果 = self.适配器.Cookie登录("这一行没有等号")
        self.assertEqual(结果.get("状态"), "失败")
        self.assertIn("BDUSS", 结果.get("提示", ""))

    # ---------------- 短信 ----------------

    def test_短信登录流程(self):
        发送 = self.适配器.短信发送("+86 13800000000")
        self.assertEqual(发送.get("状态"), "成功")
        会话 = 发送.get("会话")
        self.assertTrue(会话)
        错 = self.适配器.短信校验(会话, "000000", "+86 13800000000")
        self.assertEqual(错.get("状态"), "失败")
        对 = self.适配器.短信校验(会话, "123456", "+86 13800000000")
        self.assertEqual(对.get("状态"), "成功")

    def test_短信手机号太短会被拒(self):
        结果 = self.适配器.短信发送("123")
        self.assertEqual(结果.get("状态"), "失败")

    # ---------------- 令牌 ----------------

    def test_令牌登录成功与失败(self):
        失败 = self.适配器.令牌登录("")
        self.assertEqual(失败.get("状态"), "失败")
        成功 = self.适配器.令牌登录("access-demo-token", "refresh-demo-token")
        self.assertEqual(成功.get("状态"), "成功")
        凭证 = (self.项目 / "数据" / "登录.json").read_text(encoding="utf-8")
        self.assertIn("token", 凭证)
        self.assertNotIn("access-demo-token", 凭证)      # 只记长度，不落明文

    # ---------------- 邮箱 / 账号密码 ----------------

    def test_未支持方式返回不支持而不是抛异常(self):
        for 方法, 参数 in ((self.适配器.邮箱登录, ("a@b.c", "pw")),
                         (self.适配器.账号密码登录, ("user", "pw"))):
            结果 = 方法(*参数)
            self.assertEqual(结果.get("状态"), "不支持")
            self.assertIn("未提供", 结果.get("消息", ""))

    # ---------------- 客户端封装 ----------------

    def test_客户端方法名与能力表一致(self):
        for 名字 in ("登录方式表", "支持登录方式", "扫码开始", "扫码等待",
                   "扫码取消", "Cookie登录", "短信发送", "短信校验",
                   "令牌登录", "账号密码登录", "邮箱登录"):
            self.assertTrue(callable(getattr(self.适配器, 名字, None)),
                            f"子进程适配器缺少 {名字}")


if __name__ == "__main__":
    unittest.main()
