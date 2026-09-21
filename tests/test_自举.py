"""解释器自举（``v8_3/自举.py``）单测。

管的是这件事：``./启动.py``、``工具/*.py`` 被 PATH 里的**系统 python** 直接跑起来时，
入口要自己换成项目自带的 ``运行环境/venv/bin/python`` 再执行一遍，
否则会撞上 ``No module named 'PySide6'`` / ``'httpx'``。

跑法（项目根下，用项目自带解释器）::

    运行环境/venv/bin/python -m unittest tests.test_自举 -v
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

项目根 = Path(__file__).resolve().parents[1]
if str(项目根) not in sys.path:
    sys.path.insert(0, str(项目根))

from v8_3.自举 import (主环境, 在主环境里, 确保项目环境, 环境解释器,
                     项目解释器, 项目根 as 自举项目根)


def _找个外来解释器() -> Path | None:
    """找一个**不在项目内**的 python 当对照（系统 python 优先）。

    刻意不写死 ``/usr/bin/python3.14``：别人机器上不一定有这个版本，
    但"PATH 里有个别的 python"这件事哪儿都一样。
    """
    候选: list[Path] = []
    for 名 in ("python3.14", "python3.13", "python3.12", "python3.11", "python3"):
        找到 = shutil.which(名)
        if 找到:
            候选.append(Path(找到))
    候选.extend(sorted(Path("/usr/bin").glob("python3.*")))
    for 路径 in 候选:
        try:
            if 路径.is_file() and 路径.resolve() != 项目解释器.resolve():
                return 路径
        except OSError:
            continue
    return None


外来解释器 = _找个外来解释器()

#: 这台机器上有没有"项目自带解释器"。
#: 单测里几条断言的前提就是"本套测试是用项目解释器跑的"（开发机/用户机都成立），
#: 而 CI（windows-latest + 系统 python）里 运行环境/ 是 .gitignore 掉的、根本不存在 ——
#: 那时候这些断言没法成立，应当**跳过**而不是红（否则工作流永远失败，没人看信号）。
缺项目解释器 = not 项目解释器.is_file()


class 路径常量测试(unittest.TestCase):
    def test_路径都落在项目内(self):
        # 解释器路径按平台算：Windows 绿色版在 运行环境/python + Scripts\python.exe
        期望环境 = 项目根 / "运行环境" / ("python" if os.name == "nt" else "venv")
        self.assertEqual(自举项目根, 项目根)
        self.assertEqual(主环境, 期望环境)
        self.assertEqual(项目解释器, 环境解释器(主环境))

    @unittest.skipIf(缺项目解释器, "本机没有项目自带解释器（CI 用系统 python 跑）")
    def test_项目解释器存在且可执行(self):
        self.assertTrue(项目解释器.is_file(), f"缺少项目内解释器：{项目解释器}")
        self.assertTrue(os.access(项目解释器, os.X_OK),
                        f"项目内解释器不可执行：{项目解释器}")


class 判断与开关测试(unittest.TestCase):
    @unittest.skipIf(缺项目解释器, "本机没有项目自带解释器（CI 用系统 python 跑）")
    def test_跑在项目环境里时判定为真(self):
        # 本套测试本身就是用 运行环境/venv/bin/python 跑的
        self.assertTrue(
            在主环境里(),
            f"当前 sys.prefix={sys.prefix} 不在 {主环境} 下；"
            "测试请用项目自带解释器跑")

    def test_关闭开关时立即返回(self):
        """``V8_3_不自动换解释器=1`` 时不做任何事（也不会 exec）。"""
        旧 = os.environ.get("V8_3_不自动换解释器")
        os.environ["V8_3_不自动换解释器"] = "1"
        try:
            前缀 = sys.prefix
            确保项目环境()
            self.assertEqual(sys.prefix, 前缀)
        finally:
            if 旧 is None:
                os.environ.pop("V8_3_不自动换解释器", None)
            else:
                os.environ["V8_3_不自动换解释器"] = 旧

    @unittest.skipIf(缺项目解释器, "本机没有项目自带解释器（CI 用系统 python 跑）")
    def test_已在对的环境时不换(self):
        self.assertTrue(在主环境里())
        保证 = sys.executable
        确保项目环境()                     # 已在对的环境 → 直接返回
        self.assertEqual(sys.executable, 保证)


class 真跑一遍测试(unittest.TestCase):
    """拿系统 python 跑一个入口脚本，看它会不会自己换过去。"""

    def test_外来解释器跑工具脚本会自举(self):
        if 外来解释器 is None:
            self.skipTest("机器上找不到项目外的 python 作对照")
        if 缺项目解释器:
            self.skipTest("本机没有项目自带解释器（CI 用系统 python 跑）")
        脚本 = 项目根 / "工具" / "自检.py"
        self.assertTrue(脚本.is_file())
        结果 = subprocess.run(
            [str(外来解释器), str(脚本)],
            capture_output=True, text=True, timeout=300, cwd=str(项目根))
        self.assertIn("重新执行", 结果.stderr,
                      f"没看到自举提示；stderr={结果.stderr[:300]}")
        self.assertIn(str(项目解释器), 结果.stdout,
                      f"输出里的解释器不是项目内那个；stdout={结果.stdout[:300]}")
        self.assertIn("依赖 PySide6", 结果.stdout,
                      "自检没有跑到依赖检查，说明中途就挂了")
