"""断点续传单测：本地 HTTP 服务器模拟"中途断流"，验证真的从断点续、且文件正确。

覆盖：
  * `续传单连接下载`  —— 第一次被掐断 → 保留断点 → 第二次带 Range 续传 → 哈希一致；
  * `续传多段下载`    —— 多段 Range 中途失败 → part 文件与状态保留 → 重试续传完成；
  * `清理续传文件`    —— part 文件与侧车状态被清干净；
  * 状态不匹配（总大小/指纹变了）时**重头下**，不会把不同文件接在一起。
"""

from __future__ import annotations

import hashlib
import http.server
import os
import sys
import threading
import unittest
from pathlib import Path

项目根 = Path(__file__).resolve().parents[1]
if str(项目根) not in sys.path:
    sys.path.insert(0, str(项目根))

sys.path.insert(0, str(项目根 / "v8_3" / "桥"))
from 后端_基类 import (续传多段下载, 续传单连接下载, 清理续传文件,
                    读取续传状态, 已有续传字节)

内容 = bytes(range(256)) * (20 * 1024)          # 5 MiB，内容可校验
内容哈希 = hashlib.sha256(内容).hexdigest()
总量 = len(内容)


class _处理器(http.server.BaseHTTPRequestHandler):
    """支持 Range 的文件服务器；可以被要求"传够 N 字节就掐断"。"""

    掐断字节 = 0            # >0 时：本次响应写到这么多字节就断开
    已收到Range: list[str] = []

    def log_message(self, *args):        # 静音
        pass

    def do_GET(self):
        Range = self.headers.get("Range") or ""
        if Range:
            _处理器.已收到Range.append(Range)
        if Range.startswith("bytes="):
            片段 = Range[len("bytes="):]
            起文本, _, 止文本 = 片段.partition("-")
            起 = int(起文本 or 0)
            止 = int(止文本) if 止文本.strip() else 总量 - 1
        else:
            起, 止 = 0, 总量 - 1
        止 = min(止, 总量 - 1)
        剩余 = 内容[起:止 + 1]
        状态 = 206 if Range else 200
        self.send_response(状态)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Accept-Ranges", "bytes")
        if 状态 == 206:
            self.send_header("Content-Range",
                             f"bytes {起}-{止}/{总量}")
        self.send_header("Content-Length", str(len(剩余)))
        self.end_headers()
        上限 = _处理器.掐断字节
        已写 = 0
        try:
            for i in range(0, len(剩余), 64 * 1024):
                块 = 剩余[i:i + 64 * 1024]
                if 上限 and 已写 + len(块) > 上限:
                    块 = 块[:max(0, 上限 - 已写)]
                if not 块:
                    break
                self.wfile.write(块)
                已写 += len(块)
        except Exception:
            pass
        if 上限 and 已写 >= 上限:
            # 模拟中途断流：直接关掉连接
            try:
                self.wfile.flush()
                self.connection.close()
            except Exception:
                pass


class 断点续传测试(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.服务器 = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _处理器)
        cls.地址 = f"http://127.0.0.1:{cls.服务器.server_address[1]}/f.bin"
        cls.线程 = threading.Thread(target=cls.服务器.serve_forever, daemon=True)
        cls.线程.start()

    @classmethod
    def tearDownClass(cls):
        cls.服务器.shutdown()
        cls.服务器.server_close()

    def setUp(self):
        import tempfile
        self.临时 = Path(tempfile.mkdtemp(prefix="v8_3_续传_"))
        self.目标 = self.临时 / "f.bin"
        _处理器.掐断字节 = 0
        _处理器.已收到Range = []

    def tearDown(self):
        import shutil
        shutil.rmtree(self.临时, ignore_errors=True)

    # ---------------- 单连接续传 ----------------

    def test_单连接被掐断后保留断点(self):
        _处理器.掐断字节 = 512 * 1024
        成功 = 续传单连接下载(self.地址, {}, str(self.目标), 总量,
                         日志=lambda m: None)
        self.assertFalse(成功, "被掐断时应返回 False")
        断点 = self.目标.stat().st_size if self.目标.exists() else 0
        self.assertGreater(断点, 0, "应保留已下载的部分")
        self.assertLess(断点, 总量, "不应被当作完成")

    def test_单连接从断点续传并校验哈希(self):
        _处理器.掐断字节 = 512 * 1024
        续传单连接下载(self.地址, {}, str(self.目标), 总量, 日志=lambda m: None)
        断点 = self.目标.stat().st_size
        _处理器.掐断字节 = 0
        _处理器.已收到Range = []
        成功 = 续传单连接下载(self.地址, {}, str(self.目标), 总量,
                         日志=lambda m: None)
        self.assertTrue(成功)
        self.assertEqual(self.目标.stat().st_size, 总量)
        self.assertEqual(hashlib.sha256(self.目标.read_bytes()).hexdigest(),
                         内容哈希, "续传后的文件必须与服务端内容一致")
        self.assertTrue(_处理器.已收到Range, "续传应带 Range 请求")
        self.assertEqual(_处理器.已收到Range[0], f"bytes={断点}-",
                         "Range 起点必须等于已下载字节数")

    def test_单连接已完整时直接返回(self):
        self.目标.write_bytes(内容)
        成功 = 续传单连接下载(self.地址, {}, str(self.目标), 总量,
                         日志=lambda m: None)
        self.assertTrue(成功)
        self.assertEqual(_处理器.已收到Range, [], "完整文件不应再发请求")

    # ---------------- 多段续传 ----------------

    def test_多段被掐断后保留分片与状态(self):
        _处理器.掐断字节 = 1024 * 1024
        成功 = 续传多段下载(self.地址, {}, str(self.目标), 总量, 4, 1024,
                         日志=lambda m: None)
        self.assertFalse(成功)
        分片 = list(self.临时.glob("f.bin.v8_3part*"))
        self.assertTrue(分片, "应保留分片文件用于续传")
        状态 = 读取续传状态(self.目标, 总量)
        self.assertTrue(状态.get("段"), "应写入续传状态")
        self.assertGreater(已有续传字节(self.目标, 总量), 0)

    def test_多段续传完成且哈希一致(self):
        _处理器.掐断字节 = 1024 * 1024
        续传多段下载(self.地址, {}, str(self.目标), 总量, 4, 1024,
                  日志=lambda m: None)
        已下 = 已有续传字节(self.目标, 总量)
        _处理器.掐断字节 = 0
        成功 = 续传多段下载(self.地址, {}, str(self.目标), 总量, 4, 1024,
                         日志=lambda m: None)
        self.assertTrue(成功, "第二次应续传成功")
        self.assertEqual(self.目标.stat().st_size, 总量)
        self.assertEqual(hashlib.sha256(self.目标.read_bytes()).hexdigest(),
                         内容哈希)
        self.assertGreater(已下, 0, "第一次应留下可复用的字节")
        self.assertFalse(list(self.临时.glob("f.bin.v8_3part*")),
                         "完成后分片应被清理")

    def test_清理续传文件(self):
        _处理器.掐断字节 = 1024 * 1024
        续传多段下载(self.地址, {}, str(self.目标), 总量, 4, 1024,
                  日志=lambda m: None)
        清理续传文件(str(self.目标))
        self.assertFalse(list(self.临时.glob("f.bin.v8_3part*")))
        self.assertFalse((self.临时 / "f.bin.v8_3resume.json").exists())

    def test_总大小变了就重头下(self):
        _处理器.掐断字节 = 512 * 1024
        续传单连接下载(self.地址, {}, str(self.目标), 总量, 日志=lambda m: None)
        断点 = self.目标.stat().st_size
        _处理器.掐断字节 = 0
        假状态 = self.临时 / "f.bin.v8_3resume.json"
        假状态.write_text('{"总大小": 999999, "指纹": "", "段": []}',
                        encoding="utf-8")
        self.assertEqual(已有续传字节(self.目标, 总量), 0,
                         "总大小不匹配时不应复用断点")
        # 单连接只认文件长度：这里模拟"源大小变了"（比本地还小）→ 重下
        小目标 = self.临时 / "small.bin"
        小目标.write_bytes(b"x" * (总量 + 10))
        续传单连接下载(self.地址, {}, str(小目标), 总量, 日志=lambda m: None)
        self.assertEqual(小目标.stat().st_size, 总量)
        self.assertEqual(hashlib.sha256(小目标.read_bytes()).hexdigest(),
                         内容哈希)

    def test_指纹不符时多段重头下(self):
        _处理器.掐断字节 = 1024 * 1024
        续传多段下载(self.地址, {}, str(self.目标), 总量, 4, 1024,
                  日志=lambda m: None, 期望指纹="AAA")
        # 换指纹：状态应被视为无效
        self.assertEqual(读取续传状态(self.目标, 总量, "BBB"), {})
        _处理器.掐断字节 = 0
        成功 = 续传多段下载(self.地址, {}, str(self.目标), 总量, 4, 1024,
                         日志=lambda m: None, 期望指纹="BBB")
        self.assertTrue(成功)
        self.assertEqual(hashlib.sha256(self.目标.read_bytes()).hexdigest(),
                         内容哈希)


if __name__ == "__main__":
    unittest.main()
