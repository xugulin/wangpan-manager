"""测试传输引擎：目录展开、并发、跳过、重试。"""

from __future__ import annotations

import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

项目根 = Path(__file__).resolve().parents[1]
if str(项目根) not in sys.path:
    sys.path.insert(0, str(项目根))

from v8_3.核心.传输引擎 import 传输引擎, 传输请求
from v8_3.核心.适配器 import 云盘适配器
from v8_3.核心.模型 import 网盘类型, 远端条目, 账号信息, 缓存配置, 规范路径


class 本地假适配器(云盘适配器):
    def __init__(self, 根: Path):
        self.根 = 根
        self.类型 = 网盘类型.假
        self.失败次数 = 0
        self._锁 = threading.Lock()

    def _路径(self, 逻辑: str) -> Path:
        逻辑 = 规范路径(逻辑)
        if 逻辑 == "/":
            return self.根
        return self.根 / 逻辑.lstrip("/")

    def 账号状态(self):
        return 账号信息(self.类型, True)

    def 列目录(self, 路径="/"):
        目录 = self._路径(路径)
        return [
           远端条目(name=p.name, path=规范路径(路径 + "/" + p.name),
                  is_dir=p.is_dir(), size=0 if p.is_dir() else p.stat().st_size)
            for p in sorted(目录.iterdir())
        ]

    def 文件信息(self, 路径):
        p = self._路径(路径)
        if not p.exists():
            return None
        return 远端条目(name=p.name, path=规范路径(路径),
                      is_dir=p.is_dir(), size=0 if p.is_dir() else p.stat().st_size)

    def 确保目录(self, 路径):
        self._路径(路径).mkdir(parents=True, exist_ok=True)
        return {"path": 规范路径(路径)}

    def 上传(self, 本地路径, 远端目录, *, 名称=None, 任务ID="", 进度=None):
        with self._锁:
            if self.失败次数 > 0:
                self.失败次数 -= 1
                raise RuntimeError("注入的失败")
        源 = Path(本地路径)
        目标 = self._路径(远端目录) / (名称 or 源.name)
        目标.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(源, 目标)
        if 进度:
            进度("upload", 目标.stat().st_size, 目标.stat().st_size)
        return {"path": str(目标), "size": 目标.stat().st_size}

    def 下载(self, 远端路径, 本地路径, *, 任务ID="", 进度=None,
             续传: bool = True):
        with self._锁:
            if self.失败次数 > 0:
                self.失败次数 -= 1
                raise RuntimeError("注入的失败")
        源 = self._路径(远端路径)
        目标 = Path(本地路径)
        目标.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(源, 目标)
        if 进度:
            进度("download", 目标.stat().st_size, 目标.stat().st_size)
        return {"path": str(目标), "size": 目标.stat().st_size}

    def 删除(self, 路径):
        目标 = self._路径(路径)
        if 目标.is_dir():
            shutil.rmtree(目标)
        elif 目标.exists():
            目标.unlink()
        return {"deleted": True}


class 引擎测试(unittest.TestCase):
    def setUp(self):
        self.临时 = tempfile.TemporaryDirectory(prefix="v8_3_engine_")
        根 = Path(self.临时.name)
        self.源根, self.目标根 = 根 / "源", 根 / "目标"
        self.源根.mkdir(); self.目标根.mkdir()
        (self.源根 / "目录A" / "子目录").mkdir(parents=True)
        (self.源根 / "目录A" / "a.txt").write_text("A" * 100, encoding="utf-8")
        (self.源根 / "目录A" / "子目录" / "b.bin").write_bytes(b"B" * 1000)
        for i in range(20):
            (self.源根 / "目录A" / f"小_{i:03d}.txt").write_text(
                f"小文件{i}" * 10, encoding="utf-8")
        self.源 = 本地假适配器(self.源根)
        self.目标 = 本地假适配器(self.目标根)
        self.引擎 = 传输引擎({
            网盘类型.百度: self.源,
            网盘类型.夸克: self.目标,
        })

    def tearDown(self):
        self.临时.cleanup()

    def test_目录传输与并发(self):
        请求 = 传输请求(
            源网盘=网盘类型.百度, 源路径="/目录A",
            目标网盘=网盘类型.夸克, 目标路径="/备份",
            小文件并发=8, 大文件并发=2,
            缓存=缓存配置(目录=str(Path(self.临时.name) / "缓存")),
        )
        统计 = self.引擎.传输(请求)
        self.assertEqual(统计.失败, 0)
        self.assertEqual(统计.已完成, 22)
        self.assertTrue((self.目标根 / "备份" / "a.txt").is_file())
        self.assertTrue((self.目标根 / "备份" / "子目录" / "b.bin").is_file())
        self.assertTrue((self.目标根 / "备份" / "小_000.txt").is_file())

    def test_跳过已存在(self):
        (self.目标根 / "备份").mkdir()
        (self.目标根 / "备份" / "a.txt").write_text("old")
        请求 = 传输请求(
            源网盘=网盘类型.百度, 源路径="/目录A",
            目标网盘=网盘类型.夸克, 目标路径="/备份",
            缓存=缓存配置(目录=str(Path(self.临时.name) / "缓存")),
        )
        统计 = self.引擎.传输(请求)
        self.assertEqual(统计.跳过, 1)
        self.assertEqual((self.目标根 / "备份" / "a.txt").read_text(), "old")

    def test_失败重试(self):
        self.目标.失败次数 = 2
        请求 = 传输请求(
            源网盘=网盘类型.百度, 源路径="/目录A/a.txt",
            目标网盘=网盘类型.夸克, 目标路径="/重试.txt",
            重试次数=3,
            缓存=缓存配置(目录=str(Path(self.临时.name) / "缓存")),
        )
        统计 = self.引擎.传输(请求)
        self.assertEqual(统计.完成 if hasattr(统计, "完成") else 统计.已完成, 1)


if __name__ == "__main__":
    unittest.main()
