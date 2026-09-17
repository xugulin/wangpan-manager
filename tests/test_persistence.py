"""测试任务持久化与恢复批次。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

项目根 = Path(__file__).resolve().parents[1]
if str(项目根) not in sys.path:
    sys.path.insert(0, str(项目根))

from tests.test_engine import 本地假适配器
from v8_3.核心.传输引擎 import 传输引擎, 传输请求
from v8_3.核心.模型 import 网盘类型, 缓存配置


class 持久化测试(unittest.TestCase):
    def setUp(self):
        self.临时 = tempfile.TemporaryDirectory(prefix="v8_3_db_")
        根 = Path(self.临时.name)
        (根 / "源").mkdir(); (根 / "目标").mkdir()
        (根 / "源" / "a.txt").write_text("A" * 10, encoding="utf-8")
        (根 / "源" / "b.txt").write_text("B" * 20, encoding="utf-8")
        self.源 = 本地假适配器(根 / "源")
        self.目标 = 本地假适配器(根 / "目标")
        self.数据库 = str(根 / "任务.db")
        self.缓存 = 缓存配置(目录=str(根 / "缓存"))
        self.适配器表 = {网盘类型.百度: self.源, 网盘类型.夸克: self.目标}

    def tearDown(self):
        self.临时.cleanup()

    def _请求(self):
        return 传输请求(
            源网盘=网盘类型.百度, 源路径="/",
            目标网盘=网盘类型.夸克, 目标路径="/备份",
            重试次数=0, 缓存=self.缓存,
        )

    def test_失败后恢复(self):
        self.目标.失败次数 = 5
        引擎 = 传输引擎(self.适配器表, 数据库路径=self.数据库)
        统计 = 引擎.传输(self._请求())
        self.assertEqual(统计.失败, 2)  # 两个文件都失败
        批次 = 引擎.批次列表()
        self.assertTrue(批次)
        引擎.关闭()

        # 模拟程序重启：新引擎 + 新适配器对象，但目标失败已消除
        目标2 = 本地假适配器(Path(self.临时.name) / "目标")
        源2 = 本地假适配器(Path(self.临时.name) / "源")
        引擎2 = 传输引擎({网盘类型.百度: 源2, 网盘类型.夸克: 目标2},
                        数据库路径=self.数据库)
        try:
            统计2 = 引擎2.恢复批次(批次[0]["batch_id"])
            self.assertEqual(统计2.已完成, 2)
            self.assertEqual(统计2.失败, 0)
            self.assertTrue((Path(self.临时.name) / "目标" / "备份" / "a.txt").is_file())
        finally:
            引擎2.关闭()


if __name__ == "__main__":
    unittest.main()
