"""主题样式表单测（``v8_3/界面/主题管理器.py``）。

管的是这件事：主题的**全局那一层**（每个控件都要沾的背景色 / 文字色 / 字号）
怎么落地。真机 windows-latest 实测（工具/界面测速.py + 工具/主题测速.py）：

    平台插件 windows，同一台机器、同一次运行内对比
      样式表档（QSS 里写 QWidget{...}）：建主窗口 11.2 秒 / 切播放页 107 毫秒
      调色板档（QPalette + 应用字体）  ：建主窗口  6.8 秒 / 切播放页  40 毫秒
    （另一次运行：8.5 秒 → 3.0 秒；切成三档跑：样式表 44ms / 调色板 40ms / 无样式 18ms）

所以默认档是 **调色板**；出问题时可以设 ``V8_3_主题全局层=样式表`` 退回老写法。
这些断言把"默认值"和"两档生成的 QSS 真的不一样"钉住，防止有人无意中改回去。
"""

from __future__ import annotations

import os
import re
import sys
import unittest
from pathlib import Path

# 没有显示环境时**强制**离屏：本机环境里 QT_QPA_PLATFORM="wayland;xcb"，
# setdefault 不会覆盖它，Qt 会在创建 QApplication 时直接 abort（整进程崩）。
if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
    os.environ["QT_QPA_PLATFORM"] = "offscreen"

项目根 = Path(__file__).resolve().parents[1]
if str(项目根) not in sys.path:
    sys.path.insert(0, str(项目根))

try:
    from PySide6.QtWidgets import QApplication
    Qt可用 = True
except Exception:  # noqa: BLE001 - 没装 PySide6 的机器上跳过
    Qt可用 = False

from v8_3.界面.主题管理器 import (主题定义, 主题管理器, 全局字号像素,  # noqa: E402
                              应用全局外观)
import v8_3.界面.主题管理器 as 主题模块                              # noqa: E402


class 全局层档位测试(unittest.TestCase):
    def test_默认档是调色板(self):
        """默认必须走调色板档 —— 老写法在真机上贵 1.6~3 倍（见模块注释）。"""
        档 = os.environ.get("V8_3_主题全局层") or "调色板"
        self.assertEqual(档, "调色板",
                         "主题全局层的默认档被改回样式表了：真机实测它会把"
                         "建主窗口从 ~7 秒拖到 ~11 秒、切页从 40ms 拖到 107ms")

    #: "选择器就是 QWidget 本身"的那条规则（不含 QWidget#TopBar、
    #: QScrollArea > QWidget > QWidget 这些——它们只作用于个别控件，不贵）
    全局规则 = re.compile(r"(?m)^QWidget\s*\{")

    def test_调色板档不再生成全局规则(self):
        样式表 = 主题管理器.获取样式表_档("调色板", "Dracula")
        self.assertIsNone(self.全局规则.search(样式表),
                          "调色板档下 QSS 里不该还有 QWidget{...}（那正是最贵的一条）")

    def test_样式表档仍然保留全局规则(self):
        """退回档要真的能用（排查显示问题时用），不能是空壳。"""
        样式表 = 主题管理器.获取样式表_档("样式表", "Dracula")
        self.assertIsNotNone(self.全局规则.search(样式表))
        self.assertIn("font-size: 13px", 样式表)

    def test_无字号档保留全局规则但不写字号(self):
        样式表 = 主题管理器.获取样式表_档("无字号", "Dracula")
        self.assertIsNotNone(self.全局规则.search(样式表))
        规则 = self.全局规则.search(样式表).group(0)
        起 = 样式表.index(规则)
        块 = 样式表[起:样式表.index("}", 起)]
        self.assertNotIn("font-size", 块)

    def test_两档都还能生成完整样式表(self):
        for 档 in ("样式表", "无字号", "调色板"):
            for 名 in 主题定义:
                表 = 主题管理器.获取样式表_档(档, 名)
                self.assertGreater(len(表), 2000, f"{档}/{名} 生成的样式表太短")
                self.assertIn("QComboBox::down-arrow", 表,
                              f"{档}/{名} 丢了下拉箭头规则")

    def test_档位开关不影响模块默认值(self):
        """``获取样式表_档`` 是给对照实验用的，不能把模块默认档改掉。"""
        旧 = 主题模块.全局层
        主题管理器.获取样式表_档("样式表", "Dracula")
        self.assertEqual(主题模块.全局层, 旧)


@unittest.skipUnless(Qt可用, "没装 PySide6")
class 应用全局外观测试(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.应用 = QApplication.instance() or QApplication([])

    def test_调色板取到了主题色(self):
        应用全局外观(self.应用, "Dracula")
        调色板 = self.应用.palette()
        颜色 = 主题定义["Dracula"]["颜色"]
        self.assertEqual(调色板.color(调色板.ColorRole.Window).name(),
                         颜色["背景"].lower())
        self.assertEqual(调色板.color(调色板.ColorRole.WindowText).name(),
                         颜色["文字"].lower())
        self.assertEqual(调色板.color(调色板.ColorRole.Highlight).name(),
                         颜色["选中背景"].lower())
        self.assertEqual(self.应用.font().pixelSize(), 全局字号像素,
                         "字号没落到应用字体上：界面高度会跟以前不一样")

    def test_每套主题都能应用(self):
        """10 套主题的颜色表都要够全 —— 少一个键就是启动时 KeyError。"""
        for 名 in 主题定义:
            应用全局外观(self.应用, 名)          # 不抛异常即可
        应用全局外观(self.应用, "Dracula")        # 复位，别影响别的测试


if __name__ == "__main__":
    unittest.main()
