"""测试子进程桥 + 假网盘后端。"""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

项目根 = Path(__file__).resolve().parents[1]
if str(项目根) not in sys.path:
    sys.path.insert(0, str(项目根))

from v8_3.核心.适配器 import 适配器规格
from v8_3.核心.子进程客户端 import 子进程适配器
from v8_3.核心.模型 import 网盘类型


class 假网盘桥测试(unittest.TestCase):
    def setUp(self):
        self.临时 = tempfile.TemporaryDirectory(prefix="v8_3_test_")
        根 = Path(self.临时.name)
        (根 / "项目" / "核心").mkdir(parents=True)
        (根 / "本地").mkdir()
        self.项目 = 根 / "项目"
        self.本地 = 根 / "本地"
        (self.本地 / "hello.txt").write_text("你好，V8_3", encoding="utf-8")
        self.适配器 = 子进程适配器(适配器规格(
            类型=网盘类型.假,
            项目根=str(self.项目),
            Python解释器=sys.executable,
            线程数=4,
        ), 日志回调=lambda l, m: None)
        self.适配器.启动()

    def tearDown(self):
        self.适配器.关闭()
        self.临时.cleanup()

    def test_上传下载列目录删除(self):
        self.assertTrue(self.适配器.账号状态().已登录)
        self.assertEqual(self.适配器.列目录("/"), [])
        结果 = self.适配器.上传(str(self.本地 / "hello.txt"), "/测试/子目录",
                            任务ID="t1")
        self.assertEqual(结果["size"], len("你好，V8_3".encode("utf-8")))
        条目 = {x.name: x for x in self.适配器.列目录("/测试/子目录")}
        self.assertIn("hello.txt", 条目)
        self.assertFalse(条目["hello.txt"].is_dir)
        目标 = self.本地 / "back.txt"
        self.适配器.下载("/测试/子目录/hello.txt", str(目标), 任务ID="t2")
        self.assertEqual(
            目标.read_text(encoding="utf-8"), "你好，V8_3")
        self.适配器.删除("/测试/子目录/hello.txt")
        self.assertEqual(self.适配器.列目录("/测试/子目录"), [])


if __name__ == "__main__":
    unittest.main()
