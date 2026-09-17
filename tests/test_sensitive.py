"""测试敏感词：词库、上传守卫（预检改名 / 拦截后绕过）、引擎接入。

不联网：用一个"看到敏感词就拒绝"的假适配器模拟网盘的 405 拦截。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

项目根 = Path(__file__).resolve().parents[1]
if str(项目根) not in sys.path:
    sys.path.insert(0, str(项目根))

from v8_3.敏感词 import 敏感词数据库, 上传守卫
from v8_3.核心.适配器 import 云盘适配器
from v8_3.核心.传输引擎 import 传输引擎, 传输请求
from v8_3.核心.模型 import 网盘类型, 缓存配置, 远端条目


class 小网盘(云盘适配器):
    """本地目录当云端；文件名含"敏感词"时上传/建目录直接报 405。"""

    def __init__(self, 根: Path, 标识: str):
        self.根 = 根
        self.根.mkdir(parents=True, exist_ok=True)
        self.类型 = 网盘类型.假
        self.标识 = 标识
        self.拒绝关键词 = "敏感词"
        self.上传次数 = 0

    def _路径(self, 远端: str) -> Path:
        return self.根 / str(远端).lstrip("/")

    def 账号状态(self):
        from v8_3.核心.模型 import 账号信息
        return 账号信息(网盘=self.类型, 已登录=True, 用户名="tester",
                     数据目录=str(self.根))

    def 列目录(self, 路径: str = "/"):
        目录 = self._路径(路径)
        if not 目录.is_dir():
            raise FileNotFoundError(f"目录不存在：{路径}")
        结果 = []
        for 项 in sorted(目录.iterdir()):
            结果.append(远端条目(
                name=项.name, path=f"{路径.rstrip('/')}/{项.name}",
                is_dir=项.is_dir(),
                size=0 if 项.is_dir() else 项.stat().st_size))
        return 结果

    def 文件信息(self, 路径: str):
        目标 = self._路径(路径)
        if not 目标.exists():
            return None
        return 远端条目(name=目标.name, path=路径, is_dir=目标.is_dir(),
                     size=0 if 目标.is_dir() else 目标.stat().st_size)

    def 确保目录(self, 路径: str) -> dict:
        目标 = self._路径(路径)
        if self.拒绝关键词 in 目标.name:
            raise RuntimeError("HTTP 405 文件名包含敏感词，禁止创建")
        目标.mkdir(parents=True, exist_ok=True)
        return {"path": 路径}

    def 上传(self, 本地路径: str, 远端目录: str, *, 名称=None,
             任务ID: str = "", 进度=None) -> dict:
        self.上传次数 += 1
        名称 = 名称 or Path(本地路径).name
        if self.拒绝关键词 in 名称:
            raise RuntimeError("HTTP 405 该文件名包含敏感词，已拦截")
        目录 = self._路径(远端目录)
        目录.mkdir(parents=True, exist_ok=True)
        目标 = 目录 / 名称
        数据 = Path(本地路径).read_bytes()
        目标.write_bytes(数据)
        if 进度:
            进度("upload", len(数据), len(数据))
        return {"name": 名称, "path": f"{远端目录.rstrip('/')}/{名称}",
                "size": len(数据)}

    def 下载(self, 远端路径: str, 本地路径: str, *, 任务ID: str = "",
             进度=None,
             续传: bool = True) -> dict:
        源 = self._路径(远端路径)
        数据 = 源.read_bytes()
        Path(本地路径).parent.mkdir(parents=True, exist_ok=True)
        Path(本地路径).write_bytes(数据)
        if 进度:
            进度("download", len(数据), len(数据))
        return {"path": 本地路径, "size": len(数据)}

    def 删除(self, 路径: str) -> dict:
        目标 = self._路径(路径)
        if 目标.is_dir():
            目标.rmdir()
        elif 目标.exists():
            目标.unlink()
        return {"deleted": True}


class 敏感词测试(unittest.TestCase):
    def setUp(self):
        self.临时 = tempfile.TemporaryDirectory(prefix="v8_3_sensi_")
        self.根 = Path(self.临时.name)
        self.库 = 敏感词数据库(str(self.根 / "敏感词.db"))
        self.库.添加敏感词("测试盘", "敏感词", "安全词")
        self.适配器 = 小网盘(self.根 / "云端", "测试盘")
        self.本地 = self.根 / "影片-敏感词-第一集.mp4"
        self.本地.write_bytes(b"data" * 100)

    def tearDown(self):
        self.库.关闭()
        self.临时.cleanup()

    def _守卫(self, **参数):
        return 上传守卫(self.适配器, "测试盘", self.库, **参数)

    # ---------------- 词库 ----------------

    def test_词库添加与扫描(self):
        命中 = self.库.扫描所有敏感词("测试盘", "影片-敏感词-第一集.mp4")
        self.assertEqual([h["敏感词"] for h in 命中], ["敏感词"])
        self.assertEqual(self.库.扫描所有敏感词("测试盘", "无关文件.mp4"), [])
        self.assertTrue(self.库.生成候选安全词列表("测试盘", "敏感词"),
                        "候选安全词列表不应为空（P1–P4 会用它）")
        信息 = self.库.获取敏感词("测试盘", "敏感词")
        self.assertEqual(信息["最后一次成功安全词"], "安全词")

    def test_跨网盘独立学习(self):
        self.库.添加敏感词("另一个盘", "别的词", "别词安全")
        self.assertEqual(len(self.库.列出敏感词("测试盘")), 1)
        self.assertEqual(len(self.库.列出敏感词("另一个盘")), 1)

    # ---------------- 预检 ----------------

    def test_预检改名并写改名记录(self):
        守卫 = self._守卫()
        新名, 改过, 原名 = 守卫.预检名称("/电影", "影片-敏感词-第一集.mp4", 400)
        self.assertTrue(改过)
        self.assertEqual(原名, "影片-敏感词-第一集.mp4")
        self.assertNotIn("敏感词", 新名)
        self.assertIn("安全词", 新名)
        记录 = self.库.列出改名记录(状态="")
        self.assertEqual(len(记录), 1)
        self.assertEqual(记录[0]["改名策略"], "预检")

    def test_预检后按安全名上传(self):
        守卫 = self._守卫()
        守卫.上传(str(self.本地), "/电影", 名称=self.本地.name)
        落盘 = sorted(p.name for p in (self.根 / "云端" / "电影").iterdir())
        self.assertEqual(落盘, ["影片-安全词-第一集.mp4"])

    def test_关闭自动改名后仍会在失败时绕过(self):
        """关掉"上传前改名"只是不做预检；上传被拦后仍会探测式绕过。"""
        守卫 = self._守卫(自动改名=False)
        结果 = 守卫.上传(str(self.本地), "/电影", 名称=self.本地.name)
        self.assertTrue(结果.get("绕过"))
        self.assertEqual(守卫.统计()["预检改名"], 0)
        self.assertEqual(守卫.统计()["绕过成功"], 1)

    # ---------------- 拦截后绕过 ----------------

    def test_拦截后走P1绕过成功(self):
        守卫 = self._守卫(自动改名=False)     # 先按原名失败，再走探测式绕过
        结果 = 守卫.上传(str(self.本地), "/电影", 名称=self.本地.name)
        self.assertTrue(结果.get("绕过"))
        self.assertIn("安全词", 结果["name"])
        落盘 = sorted(p.name for p in (self.根 / "云端" / "电影").iterdir())
        self.assertEqual(落盘, ["影片-安全词-第一集.mp4"])
        统计 = 守卫.统计()
        self.assertEqual(统计["绕过尝试"], 1)
        self.assertEqual(统计["绕过成功"], 1)
        self.assertTrue(self.库.列出改名记录(状态=""))

    def test_文件过大时不做绕过(self):
        守卫 = self._守卫(自动改名=False, 最大绕过字节=10)
        with self.assertRaises(RuntimeError):
            守卫.上传(str(self.本地), "/电影", 名称=self.本地.name)
        self.assertEqual(守卫.统计()["绕过跳过_过大"], 1)

    def test_非敏感错误不触发绕过(self):
        class 网络错(小网盘):
            def 上传(self, 本地路径, 远端目录, *, 名称=None,
                     任务ID="", 进度=None):
                raise RuntimeError("connection reset by peer")

        守卫 = 上传守卫(网络错(self.根 / "云端2", "测试盘"), "测试盘", self.库)
        with self.assertRaises(RuntimeError):
            守卫.上传(str(self.本地), "/电影", 名称=self.本地.name)
        self.assertEqual(守卫.统计()["绕过尝试"], 0)

    def test_守卫识别拦截关键词(self):
        self.assertTrue(上传守卫.是拦截错误("HTTP 405 敏感词"))
        self.assertTrue(上传守卫.是拦截错误("Forbidden: blocked"))
        self.assertFalse(上传守卫.是拦截错误("Read timed out"))

    # ---------------- 引擎接入 ----------------

    def test_引擎批次传输也走守卫(self):
        源 = 小网盘(self.根 / "源盘", "源盘")
        (self.根 / "源盘" / "影片-敏感词-第二集.mp4").write_bytes(b"x" * 50)
        目标 = self.适配器
        守卫 = 上传守卫(目标, "测试盘", self.库, 自动改名=False)
        引擎 = 传输引擎({"源盘": 源, "测试盘": 目标},
                     守卫表={"测试盘": 守卫},
                     数据库路径=self.根 / "任务.db",
                     名称表={"源盘": "源盘", "测试盘": "测试盘"})
        try:
            请求 = 传输请求(源网盘="源盘", 源路径="/影片-敏感词-第二集.mp4",
                          目标网盘="测试盘", 目标路径="/电影/影片-敏感词-第二集.mp4",
                          缓存=缓存配置(目录=str(self.根 / "中转")))
            统计 = 引擎.传输(请求)
        finally:
            引擎.关闭()
        self.assertEqual(统计.失败, 0)
        self.assertEqual(统计.已完成, 1)
        落盘 = sorted(p.name for p in (self.根 / "云端" / "电影").iterdir())
        self.assertEqual(落盘, ["影片-安全词-第二集.mp4"])
        self.assertEqual(守卫.统计()["绕过成功"], 1)


if __name__ == "__main__":
    unittest.main()
