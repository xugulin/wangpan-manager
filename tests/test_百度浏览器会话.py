"""浏览器会话导入（写权限的正解）离线单测。

对应现场问题：扫码登录后「已登录、能列目录、但上传/改名/删除一律 errno:-6」。
真机实测结论：**写权限 = BDUSS + pan 域 STOKEN**，而扫码登录那份会话缺 pan 域
STOKEN。正解是从本机浏览器（调试端口，走 CDP 让浏览器自己解密 cookie）
把**完整**会话取回来 —— 见 核心/认证/浏览器会话导入.py。

这些用例全部离线：用假 cookie 列表验证"取哪一份"的规则，
用本机 http.server 假装一个调试端口验证发现逻辑，用不存在的端口验证失败路径。
真机联调（真去浏览器抓）由集成用例按环境变量开启。
"""

from __future__ import annotations

import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

适配器根 = Path(__file__).resolve().parents[1] / "适配器" / "百度网盘适配器"
if str(适配器根) not in sys.path:
    sys.path.insert(0, str(适配器根))

from 核心.认证.浏览器会话导入 import (  # noqa: E402
    取浏览器会话, 找调试端口, 会话字段, 默认候选端口,
)


def 饼(名字: str, 值: str, 域: str, http_only: bool = False) -> dict:
    return {"name": 名字, "value": 值, "domain": 域,
            "httpOnly": http_only, "path": "/", "secure": True}


class 会话字段测试(unittest.TestCase):
    def test_优先取pan域的STOKEN(self):
        """同名 STOKEN 两份：pan 域那份才是写权限认的。"""
        会话 = 会话字段([
            饼("BDUSS", "b" * 192, ".baidu.com", True),
            饼("STOKEN", "p" * 64, ".passport.baidu.com", True),
            饼("STOKEN", "P" * 64, ".pan.baidu.com", True),
        ])
        self.assertEqual(会话["stoken"], "P" * 64,
                         "必须取 pan 域那份 STOKEN（passport 域的不顶用）")

    def test_没有pan域时才退而取别的(self):
        会话 = 会话字段([饼("BDUSS", "b" * 192, ".baidu.com"),
                     饼("STOKEN", "s" * 64, ".baidu.com")])
        self.assertEqual(会话["stoken"], "s" * 64)

    def test_只认百度域(self):
        会话 = 会话字段([
            饼("BDUSS", "x" * 192, ".example.com"),
            饼("STOKEN", "y" * 64, ".example.com"),
        ])
        self.assertEqual(会话, {}, "非 baidu 域的 cookie 一律不要")

    def test_bduss_bfess缺省时用bduss兜底(self):
        会话 = 会话字段([饼("BDUSS", "b" * 192, ".baidu.com"),
                     饼("STOKEN", "s" * 64, ".pan.baidu.com")])
        self.assertEqual(会话["bduss_bfess"], "b" * 192)

    def test_空值不入库(self):
        会话 = 会话字段([饼("BDUSS", "", ".baidu.com"),
                     饼("STOKEN", "s" * 64, ".pan.baidu.com")])
        self.assertNotIn("bduss", 会话)


class _假调试端口(BaseHTTPRequestHandler):
    """假装是 Chrome 的 /json/version 与 /json/list。"""

    端口记录: dict = {"version": 0, "list": 0}

    def do_GET(self):  # noqa: N802 - http.server 的约定
        if self.path.startswith("/json/version"):
            self.端口记录["version"] += 1
            体 = json.dumps({"Browser": "FakeChrome/1.0",
                           "webSocketDebuggerUrl": "ws://127.0.0.1:1/x"})
        elif self.path.startswith("/json/list"):
            self.端口记录["list"] += 1
            体 = json.dumps([{"type": "page", "url": "https://pan.baidu.com/",
                            "webSocketDebuggerUrl": "ws://127.0.0.1:1/y"}])
        else:
            self.send_response(404)
            self.end_headers()
            return
        数据 = 体.encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(数据)))
        self.end_headers()
        self.wfile.write(数据)

    def log_message(self, *a):  # 别往 stderr 刷日志
        pass


class 端口发现测试(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.服务 = HTTPServer(("127.0.0.1", 0), _假调试端口)
        cls.端口 = cls.服务.server_address[1]
        cls.线程 = threading.Thread(target=cls.服务.serve_forever, daemon=True)
        cls.线程.start()

    @classmethod
    def tearDownClass(cls):
        cls.服务.shutdown()
        cls.服务.server_close()

    def test_能从候选端口里认出来(self):
        端口, 说明 = 找调试端口(候选=(self.端口,))
        self.assertEqual(端口, self.端口)
        self.assertIn("FakeChrome", 说明)

    def test_端口不通时如实回空(self):
        """给一个几乎不可能开着的端口：显式候选全都失败就如实回 (0, "")。

        注意：不传候选时函数还会去扫进程命令行找真实的浏览器
        （本机确实开着 38489），所以这里必须显式传候选。
        """
        端口, 说明 = 找调试端口(候选=(1,), 扫进程=False)
        self.assertEqual(端口, 0, f"端口 1 上不该有调试协议，却回了 {端口}")
        self.assertEqual(说明, "")

    def test_默认候选端口包含浏览器的常见值(self):
        self.assertIn(38489, 默认候选端口, "本项目浏览器面板的端口要在候选里")
        self.assertIn(9222, 默认候选端口, "Chrome 手动开调试的默认端口要在候选里")


class 失败路径测试(unittest.TestCase):
    def test_端口不通时如实报错(self):
        结果 = 取浏览器会话(端口=1)
        self.assertFalse(结果["成功"])
        self.assertIn("失败", 结果["消息"])
        self.assertEqual(结果["会话"], {})

    @unittest.skipUnless(os.environ.get("V8_3_真机") == "1",
                         "真机联调（要浏览器开着调试端口）默认跳过")
    def test_真机取到完整会话(self):  # pragma: no cover - 真机专用
        结果 = 取浏览器会话()
        self.assertTrue(结果["成功"], 结果["消息"])
        会话 = 结果["会话"]
        self.assertGreaterEqual(len(会话.get("bduss") or ""), 100)
        self.assertGreaterEqual(len(会话.get("stoken") or ""), 32)


if __name__ == "__main__":
    unittest.main(verbosity=2)
