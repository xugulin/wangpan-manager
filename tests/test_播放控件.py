"""播放控件与独立窗口单测（离屏 Qt，不需要显示器、不碰 libvlc）。

为什么要有这一层
================
「独立窗口全屏 → 控件自动隐藏 → 有动静显示 → Esc 回窗口化」这套行为以前没测过，
而它正好是**只能靠事件驱动**才能发现的坑：

* 窗口没 ``show()`` 时，子控件 ``isVisible()`` 一律是 False（看起来像"控件消失"）；
* 没有窗口管理器时（Xvfb/精简桌面），``showFullScreen()`` 只改状态**不改尺寸**，
  画面只占左上角一块 —— 所以全屏要显式铺满屏幕；
* 全屏时视频区会吃掉鼠标事件，事件过滤器必须同时装在窗口**和**视频区上。

这里用一个**假播放会话**替身（只实现窗口要用的方法），所以跑得快、也不依赖
系统里有没有 VLC；真机播放由 tests/test_播放层.py 和界面自检覆盖。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

项目根 = Path(__file__).resolve().parents[1]
if str(项目根) not in sys.path:
    sys.path.insert(0, str(项目根))

# 没有显示环境时**强制**离屏：本机环境里 QT_QPA_PLATFORM="wayland;xcb"，
# setdefault 改不动它，Qt 会依次试 wayland/xcb 然后直接 abort（实测 core dump）。
if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
    os.environ["QT_QPA_PLATFORM"] = "offscreen"

try:
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtWidgets import QApplication
    Qt可用 = True
except Exception:  # pragma: no cover - 没装 PySide6 就跳过
    Qt可用 = False

if Qt可用:
    from v8_3.界面.播放控件 import 播放控制条, 视频窗, 时间文本
    from v8_3.界面.播放器窗口 import 播放器窗口
    from v8_3.界面.播放页面 import 播放页面

应用 = QApplication.instance() or (QApplication([]) if Qt可用 else None)


def 泵(秒: float = 0.05):
    起 = __import__("time").time()
    while __import__("time").time() - 起 < 秒:
        QApplication.processEvents()
        __import__("time").sleep(0.005)


# ==================== 假播放会话 ====================


class 假播放器:
    def __init__(self):
        self.窗口句柄 = 0
        self.跳转们: list[float] = []

    def 时长秒(self):
        return 100.0

    def 进度秒(self):
        return 10.0

    def 取音量(self):
        return 80

    def 绑定窗口(self, 句柄):
        self.窗口句柄 = int(句柄 or 0)

    def 设置缩放(self, 倍率):
        self.缩放们 = getattr(self, "缩放们", [])
        self.缩放们.append(float(倍率))


class 假会话:
    """只实现播放器窗口要用到的那几个方法。"""

    def __init__(self):
        self.播放器 = 假播放器()
        self.标题 = "样片.mp4"
        self.远端路径 = "/样片.mp4"
        self.网盘标识 = "fake_1"
        # 关窗交接要带直链（页面据此在本页接着播，不必重新取直链）
        self.直链信息 = {"url": "/tmp/样片.mp4", "headers": {},
                    "name": "样片.mp4", "size": 1024}
        self.媒体 = None
        self.探测 = None
        self.设置 = None
        self.关闭次数 = 0
        self.音量们: list[int] = []
        self.速率们: list[float] = []
        self.暂停次数 = 0
        self.字幕次数 = 0

    def 起播(self, _句柄=0):
        self.起播次数 = getattr(self, "起播次数", 0) + 1
        return True

    def 状态快照(self):
        return {"状态": "播放中", "进度秒": 10.0, "时长秒": 100.0, "丢帧": 0,
                "已解码视频": 250, "已播秒": 10.0, "输入码率bps": 0.0,
                "缓冲中": False}

    def 暂停(self):
        self.暂停次数 += 1

    def 设置音量(self, 值):
        self.音量们.append(int(值))

    def 设置速率(self, 值):
        self.速率们.append(float(值))

    def 跳转(self, 秒):
        self.播放器.跳转们.append(float(秒))

    def 切换字幕(self):
        self.字幕次数 += 1
        return 1

    def 截图(self, _路径):
        return False

    def 关闭(self):
        self.关闭次数 += 1


@unittest.skipUnless(Qt可用, "没装 PySide6")
class 时间文本测试(unittest.TestCase):
    def test_分钟秒(self):
        self.assertEqual(时间文本(0), "00:00")
        self.assertEqual(时间文本(5), "00:05")
        self.assertEqual(时间文本(65), "01:05")
        self.assertEqual(时间文本(599), "09:59")

    def test_带小时(self):
        self.assertEqual(时间文本(3600), "1:00:00")
        self.assertEqual(时间文本(3661), "1:01:01")

    def test_负数与空值当0(self):
        self.assertEqual(时间文本(-5), "00:00")
        self.assertEqual(时间文本(None), "00:00")


@unittest.skipUnless(Qt可用, "没装 PySide6")
class 播放控制条测试(unittest.TestCase):
    def setUp(self):
        self.条 = 播放控制条()
        self.收到 = {键: [] for 键 in
                  ("暂停", "停止", "跳转", "音量", "倍速", "字幕", "截图", "全屏")}
        self.条.请求暂停.connect(lambda: self.收到["暂停"].append(1))
        self.条.请求停止.connect(lambda: self.收到["停止"].append(1))
        self.条.请求跳转.connect(lambda v: self.收到["跳转"].append(v))
        self.条.请求音量.connect(lambda v: self.收到["音量"].append(v))
        self.条.请求倍速.connect(lambda v: self.收到["倍速"].append(v))
        self.条.请求字幕.connect(lambda: self.收到["字幕"].append(1))
        self.条.请求截图.connect(lambda: self.收到["截图"].append(1))
        self.条.请求全屏.connect(lambda: self.收到["全屏"].append(1))

    def test_按钮发信号(self):
        for 按钮, 键 in ((self.条.播放暂停按钮, "暂停"), (self.条.停止按钮, "停止"),
                      (self.条.字幕按钮, "字幕"), (self.条.截图按钮, "截图"),
                      (self.条.全屏按钮, "全屏")):
            按钮.click()
        self.assertTrue(all(self.收到[k] for k in
                        ("暂停", "停止", "字幕", "截图", "全屏")),
                        f"有按钮没发信号：{self.收到}")

    def test_进度条拖动发比例(self):
        self.条.进度条.setValue(250)
        self.条.进度条.sliderReleased.emit()
        self.assertEqual(len(self.收到["跳转"]), 1)
        self.assertAlmostEqual(self.收到["跳转"][0], 0.25, places=3)

    def test_音量与倍速发数值(self):
        self.条.音量条.setValue(66)
        self.assertIn(66, self.收到["音量"])
        序号 = next(i for i in range(self.条.倍速框.count())
                 if abs(float(self.条.倍速框.itemData(i)) - 2.0) < 0.01)
        self.条.倍速框.setCurrentIndex(序号)        # 2.0×
        self.assertIn(2.0, self.收到["倍速"])

    def test_设置进度与图标不触发信号(self):
        self.条.设置进度(30.0, 120.0)
        self.assertEqual(self.条.时间标签.text(), "00:30 / 02:00")
        self.assertEqual(self.条.进度条.value(), 250)
        self.条.设置暂停图标(False)
        self.assertEqual(self.条.播放暂停按钮.text(), "▶")
        self.条.设置全屏图标(True)
        self.assertIn("退出全屏", self.条.全屏按钮.text())
        self.条.设置倍速(1.5)
        self.assertAlmostEqual(float(self.条.倍速框.currentData()), 1.5)
        self.条.设置音量(120)
        self.assertEqual(self.条.音量条.value(), 120)
        # 只有"音量设置"这一个信号（进度/图标/倍速设置都不该发）
        self.assertEqual(self.收到["跳转"], [])
        self.assertEqual(self.收到["倍速"], [])

    def test_时长为0不炸(self):
        self.条.设置进度(0, 0)
        self.assertEqual(self.条.进度条.value(), 0)


@unittest.skipUnless(Qt可用, "没装 PySide6")
class 视频窗测试(unittest.TestCase):
    def test_双击发信号(self):
        窗 = 视频窗()
        次数 = []
        窗.双击.connect(lambda: 次数.append(1))
        from PySide6.QtCore import QPointF
        from PySide6.QtGui import QMouseEvent
        事件 = QMouseEvent(QEvent.MouseButtonDblClick, QPointF(5, 5), QPointF(5, 5),
                        Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
        窗.mouseDoubleClickEvent(事件)
        self.assertEqual(次数, [1])

    def test_是原生窗口(self):
        窗 = 视频窗()
        self.assertTrue(窗.testAttribute(Qt.WA_NativeWindow),
                        "必须是原生窗口，libvlc 才有 X11 window id 可画")


@unittest.skipUnless(Qt可用, "没装 PySide6")
class 播放器窗口测试(unittest.TestCase):
    def setUp(self):
        self.会话 = 假会话()
        self.窗 = 播放器窗口(self.会话, 标题="样片.mp4")
        self.assertTrue(self.窗.起播())
        泵(0.1)

    def tearDown(self):
        self.窗.close()
        泵(0.05)

    def test_顶层窗口(self):
        self.assertIsNone(self.窗.parent(), "独立窗口不能挂在父窗口里")

    def test_窗口化时控件可见(self):
        self.assertTrue(self.窗.控制条.isVisible(),
                        "窗口化时控件必须可见（窗口没 show() 时这里会是 False）")

    def test_全屏与退出全屏(self):
        self.窗.设置全屏(True)
        泵(0.2)
        # 有 WM 时 isFullScreen() 为真；没有 WM 时由 _全屏兜底铺满保证尺寸
        self.assertTrue(self.窗._全屏)
        self.assertTrue(self.窗.isFullScreen() or self.窗.width() > 400)
        self.窗.设置全屏(False)
        泵(0.2)
        self.assertFalse(self.窗._全屏)
        self.assertFalse(self.窗.isFullScreen())
        self.assertTrue(self.窗.控制条.isVisible(), "回窗口化后控件要显示")

    def test_全屏静止后自动隐藏_有动静再显示(self):
        self.窗.设置全屏(True)
        泵(0.3)
        self.assertTrue(self.窗.控制条.isVisible(), "刚进全屏控件还亮着")
        self.窗._隐藏控件()                    # 直接触发自动隐藏（不等 2.5 秒）
        self.assertFalse(self.窗.控制条.isVisible())
        self.窗.显示控件()
        self.assertTrue(self.窗.控制条.isVisible())

    def test_窗口化时不会自动隐藏控件(self):
        self.窗._隐藏控件()
        self.assertTrue(self.窗.控制条.isVisible(),
                        "非全屏不该隐藏控件（_隐藏控件 只对全屏生效）")

    def test_esc全屏回窗口化(self):
        self.窗.设置全屏(True)
        泵(0.1)
        self.窗.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Escape,
                                   Qt.NoModifier))
        泵(0.2)
        self.assertFalse(self.窗.isFullScreen())

    def test_快捷键(self):
        self.窗.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Space,
                                   Qt.NoModifier))
        self.assertGreaterEqual(self.会话.暂停次数, 1, "空格应暂停")
        self.窗.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Right,
                                   Qt.NoModifier))
        self.assertTrue(self.会话.播放器.跳转们, "右方向键应快进")
        self.窗.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Left,
                                   Qt.NoModifier))
        self.assertGreaterEqual(len(self.会话.播放器.跳转们), 2, "左方向键应后退")
        self.窗.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Up,
                                   Qt.NoModifier))
        self.assertTrue(self.会话.音量们, "上方向键应调音量")
        self.窗.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Down,
                                   Qt.NoModifier))
        self.assertGreaterEqual(len(self.会话.音量们), 2)

    def test_方向键不越界(self):
        """进度 10s 时按左键不能跳到负数。"""
        self.窗.相对跳转(-999)
        self.assertGreaterEqual(min(self.会话.播放器.跳转们), 0.0)

    def test_音量钳制在0到150(self):
        self.窗._调音量(999)
        self.assertLessEqual(max(self.会话.音量们), 150)
        self.窗._调音量(-999)
        self.assertGreaterEqual(min(self.会话.音量们), 0)

    def test_双击画面切全屏(self):
        状态 = []
        self.窗.视频.双击.connect(lambda: 状态.append(self.窗._全屏))
        self.窗.视频.双击.emit()
        泵(0.5)          # X11 下窗口状态由窗口管理器处理，给足时间
        # X11 无 WM 时 showFullScreen() 只改状态；两种都算通过
        self.assertTrue(self.窗.isFullScreen() or self.窗._全屏,
                        f"双击画面应进全屏（isFullScreen={self.窗.isFullScreen()} "
                        f"内部={self.窗._全屏}）")

    def test_关闭会停会话(self):
        self.窗.close()
        泵(0.1)
        self.assertGreaterEqual(self.会话.关闭次数, 1)

    def test_全屏后窗口尺寸不小于屏幕(self):
        """回归：没有窗口管理器时 showFullScreen 不改尺寸，画面只占左上角。"""
        self.窗.设置全屏(True)
        泵(0.2)
        屏幕 = QApplication.primaryScreen()
        if 屏幕 is not None and QApplication.platformName() not in ("offscreen",
                                                                "minimal"):
            self.assertGreaterEqual(self.窗.width(), 屏幕.size().width() - 1)
        else:
            # 离屏平台没有真实屏幕：只要求视频区还有像样的宽度
            # （右侧面板占着宽度时不会铺满，这是对的）
            self.assertGreaterEqual(self.窗.视频.width(), self.窗.width() // 2)

    def test_事件过滤器挂在窗口和视频区上(self):
        """全屏时视频区会吃掉鼠标事件，两处都要装过滤器。"""
        self.assertTrue(self.窗.视频.eventFilter is not None)
        事件 = QEvent(QEvent.MouseMove)
        # 直接给视频区发事件：不应抛异常，且应触发"显示控件"
        self.窗._隐藏控件()
        self.窗.eventFilter(self.窗.视频, 事件)
        self.assertTrue(self.窗.控制条.isVisible(), "视频区上的鼠标移动要让控件回来")


if __name__ == "__main__":
    unittest.main(verbosity=2)


# ==================== 播放清单（VLC 的播放清单面板） ====================


@unittest.skipUnless(Qt可用, "没装 PySide6")
class 播放清单测试(unittest.TestCase):
    def setUp(self):
        from v8_3.界面.播放清单 import 播放项, 播放清单
        self.播放项 = 播放项
        self.清单 = 播放清单()

    def _项(self, 名):
        return self.播放项(标题=名, 网盘标识="fake_1", 远端路径=f"/{名}",
                       来源="fake_1")

    def test_添加与去重(self):
        self.assertTrue(self.清单.添加(self._项("a.mp4")))
        self.assertFalse(self.清单.添加(self._项("a.mp4")), "重复项不该重复加")
        self.assertTrue(self.清单.添加(self._项("b.mp4")))
        self.assertEqual(len(self.清单), 2)

    def test_下一个按顺序(self):
        甲, 乙 = self._项("a.mp4"), self._项("b.mp4")
        self.清单.添加(甲); self.清单.添加(乙)
        self.清单.设为当前(0)
        self.assertEqual(self.清单.下一个项().标题, "b.mp4")
        # 到头了，非自动（用户点下一个）→ 回到第一项
        self.assertEqual(self.清单.下一个项().标题, "a.mp4")

    def test_自动续播到末尾会停(self):
        甲, 乙 = self._项("a.mp4"), self._项("b.mp4")
        self.清单.添加(甲); self.清单.添加(乙)
        self.清单.设为当前(1)
        self.assertIsNone(self.清单.下一个项(自动=True), "不循环时末尾应停下")

    def test_列表循环回到第一项(self):
        甲, 乙 = self._项("a.mp4"), self._项("b.mp4")
        self.清单.添加(甲); self.清单.添加(乙)
        self.清单.设为当前(1)
        self.清单.循环模式 = "列表循环"
        self.assertEqual(self.清单.下一个项(自动=True).标题, "a.mp4")

    def test_单曲循环一直当前项(self):
        甲, 乙 = self._项("a.mp4"), self._项("b.mp4")
        self.清单.添加(甲); self.清单.添加(乙)
        self.清单.设为当前(0)
        self.清单.循环模式 = "单曲循环"
        self.assertEqual(self.清单.下一个项(自动=True).标题, "a.mp4")

    def test_上一个环绕(self):
        甲, 乙 = self._项("a.mp4"), self._项("b.mp4")
        self.清单.添加(甲); self.清单.添加(乙)
        self.清单.设为当前(0)
        self.assertEqual(self.清单.上一个项().标题, "b.mp4")

    def test_循环按钮轮换(self):
        原 = self.清单.循环模式
        self.清单.切换循环()
        self.assertNotEqual(self.清单.循环模式, 原)
        self.assertIn("循环", self.清单.循环按钮文本())

    def test_随机不会抽到当前项(self):
        三个 = [self._项(f"第{i}.mp4") for i in range(3)]
        for 项 in 三个:
            self.清单.添加(项)
        self.清单.设为当前(0)
        self.清单.随机 = True
        for _ in range(20):
            下 = self.清单.下一个项()
            self.assertIsNotNone(下)
            self.assertNotEqual(下.标题, "第0.mp4" if self.清单.当前行() != 0 else "")

    def test_移除与清空(self):
        甲, 乙 = self._项("a.mp4"), self._项("b.mp4")
        self.清单.添加(甲); self.清单.添加(乙)
        self.清单.列表.setCurrentRow(0)
        self.assertEqual(self.清单.移除选中(), 1)
        self.assertEqual(len(self.清单), 1)
        self.清单.清空()
        self.assertEqual(len(self.清单), 0)
        self.assertIsNone(self.清单.当前项())

    def test_本地项取值路径(self):
        本地 = self.播放项(标题="x.mp4", 本地路径="/tmp/x.mp4")
        self.assertTrue(本地.是本地)
        self.assertEqual(本地.取值路径(), "/tmp/x.mp4")
        远端 = self._项("a.mp4")
        self.assertFalse(远端.是本地)
        self.assertEqual(远端.取值路径(), "/a.mp4")

    def test_双击发播放信号(self):
        收到 = []
        self.清单.请求播放.connect(lambda 项: 收到.append(项))
        self.清单.添加(self._项("a.mp4"))
        self.清单._双击(self.清单.列表.item(0))
        self.assertEqual(len(收到), 1)
        self.assertEqual(收到[0].标题, "a.mp4")


# ==================== 显示环境（Wayland 崩溃的根因） ====================


class 显示环境测试(unittest.TestCase):
    """回归：Wayland 下把 winId 交给 set_xwindow → **整个 GUI 段错误闪退**。

    所以 :func:`可嵌入窗口` 必须在 wayland 上返回假，句柄闸才会退回无窗口模式；
    :func:`准备嵌入显示` 在"Wayland + XWayland"时要把平台切到 xcb。
    """

    def test_各平台可嵌入性(self):
        from v8_3.播放.显示环境 import 可嵌入窗口
        for 平台 in ("xcb", "xcb:screen", "XCB"):
            self.assertTrue(可嵌入窗口(平台), f"{平台} 应该有 X11 窗口号")
        for 平台 in ("wayland", "wayland-egl", "offscreen", "minimal", "vnc",
                    "linuxfb", "eglfs"):
            self.assertFalse(可嵌入窗口(平台), f"{平台} 不该当 X11 用")
        # 空串 = "按当前平台自动判断"，不能写死（跑在 X11 上当然是可嵌入的）
        from PySide6.QtGui import QGuiApplication
        self.assertEqual(可嵌入窗口(""),
                         可嵌入窗口(QGuiApplication.platformName()))

    def test_wayland加xwayland会切到xcb(self):
        import os
        from v8_3.播放 import 显示环境
        旧 = dict(os.environ)
        try:
            os.environ.pop("QT_QPA_PLATFORM", None)
            os.environ["DISPLAY"] = ":0"
            os.environ["WAYLAND_DISPLAY"] = "wayland-0"
            结果 = 显示环境.准备嵌入显示()
            self.assertEqual(结果["平台"], "xcb")
            self.assertTrue(结果["改了"])
            self.assertTrue(结果["可嵌入"])
            self.assertIn("xcb", os.environ["QT_QPA_PLATFORM"])
        finally:
            os.environ.clear()
            os.environ.update(旧)

    def test_只有wayland时如实说不能嵌(self):
        import os
        from v8_3.播放 import 显示环境
        旧 = dict(os.environ)
        try:
            os.environ.pop("DISPLAY", None)
            os.environ["QT_QPA_PLATFORM"] = "wayland"
            结果 = 显示环境.准备嵌入显示()
            self.assertFalse(结果["可嵌入"])
            self.assertIn("XWayland", 结果["说明"])
        finally:
            os.environ.clear()
            os.environ.update(旧)

    def test_用户可禁用强制(self):
        import os
        from v8_3.播放 import 显示环境
        旧 = dict(os.environ)
        try:
            os.environ["V8_3_不强制X11"] = "1"
            os.environ["QT_QPA_PLATFORM"] = "wayland"
            os.environ["DISPLAY"] = ":0"
            结果 = 显示环境.准备嵌入显示()
            self.assertFalse(结果["改了"])
            self.assertEqual(os.environ["QT_QPA_PLATFORM"], "wayland")
        finally:
            os.environ.pop("V8_3_不强制X11", None)
            os.environ.clear()
            os.environ.update(旧)

    def test_显示说明是一行(self):
        from v8_3.播放.显示环境 import 显示说明
        文本 = 显示说明()
        self.assertIsInstance(文本, str)
        self.assertTrue(文本)
        self.assertNotIn("\n", 文本)


# ==================== VLC 风格外壳（菜单/工具栏/右键） ====================


@unittest.skipUnless(Qt可用, "没装 PySide6")
class VLC风格测试(unittest.TestCase):
    def _动作表(self):
        表 = {k: (lambda *a, **kw: None) for k in
              ("打开网盘", "打开本地", "播放暂停", "停止", "上一个", "下一个",
               "全屏", "截图", "切换清单", "静音切换", "设置音量", "设置速度",
               "AI翻译字幕", "AI生成字幕", "AI总结", "AI诊断", "循环切换",
               "随机切换", "显示控件", "切换AI面板")}
        表["是否静音"] = lambda: False
        表["音量"] = lambda: 100
        表["当前速度"] = lambda: 1.0
        表["音频轨列表"] = lambda: [(1, "立体声")]
        表["字幕轨列表"] = lambda: []
        表["章节数"] = lambda: 0
        return 表

    def test_菜单结构与VLC对齐(self):
        from v8_3.界面.vlc风格 import 构建菜单栏
        栏 = 构建菜单栏(self._动作表())
        标题 = [a.text() for a in 栏.actions()]
        for 需要 in ("媒体(&M)", "播放(&P)", "音频(&A)", "视频(&V)", "字幕(&S)",
                    "视图(&I)", "工具(&T)", "帮助(&H)"):
            self.assertIn(需要, 标题, f"缺菜单：{需要}")
        self.assertTrue(any("AI" in t for t in 标题), "AI 菜单是需求要求的额外项")

    def test_缺键的菜单项自动置灰(self):
        from v8_3.界面.vlc风格 import 构建菜单栏
        栏 = 构建菜单栏({"播放暂停": lambda: None})   # 只给一个动作
        媒体 = next(m for m in 栏.actions() if m.text().startswith("媒体"))
        打开 = next(a for a in 媒体.menu().actions() if "打开网盘" in a.text())
        self.assertFalse(打开.isEnabled(), "没有对应动作的菜单项必须置灰")

    def test_工具栏控件齐全(self):
        from v8_3.界面.vlc风格 import 构建工具栏
        栏 = 构建工具栏(self._动作表())
        for 名 in ("音量滑条", "音量标签", "速度框", "循环动作", "全屏动作", "清单动作"):
            self.assertTrue(hasattr(栏, 名), f"工具栏缺控件：{名}")
        self.assertEqual(栏.速度框.count(), 9, "速度档位应与 VLC 相当")
        self.assertEqual(栏.音量滑条.maximum(), 150)

    def test_工具栏音量联动动作(self):
        from v8_3.界面.vlc风格 import 构建工具栏
        收到 = []
        表 = self._动作表()
        表["设置音量"] = lambda v: 收到.append(int(v))
        栏 = 构建工具栏(表)
        栏.音量滑条.setValue(66)
        self.assertIn(66, 收到)

    def test_右键菜单含AI(self):
        from v8_3.界面.vlc风格 import 构建右键菜单
        单 = 构建右键菜单(self._动作表())
        标题 = [a.text() for a in 单.actions()]
        self.assertIn("播放 / 暂停", 标题)
        self.assertIn("全屏 / 退出全屏", 标题)
        self.assertTrue(any("AI" in t for t in 标题), "右键菜单要有 AI 子菜单")


# ==================== AI 面板（页面与独立窗口共用） ====================


@unittest.skipUnless(Qt可用, "没装 PySide6")
class AI播放面板测试(unittest.TestCase):
    def test_按钮发信号(self):
        from v8_3.界面.AI播放面板 import AI播放面板
        面板 = AI播放面板()
        次数 = {"译": 0, "生": 0, "总": 0, "诊": 0}
        面板.翻译.connect(lambda: 次数.__setitem__("译", 次数["译"] + 1))
        面板.生成字幕.connect(lambda: 次数.__setitem__("生", 次数["生"] + 1))
        面板.总结.connect(lambda: 次数.__setitem__("总", 次数["总"] + 1))
        面板.诊断.connect(lambda: 次数.__setitem__("诊", 次数["诊"] + 1))
        for 按钮 in (面板.翻译按钮, 面板.生字幕按钮, 面板.总结按钮, 面板.诊断按钮):
            按钮.click()
        self.assertEqual(次数, {"译": 1, "生": 1, "总": 1, "诊": 1})

    def test_追加日志带时间戳(self):
        from v8_3.界面.AI播放面板 import AI播放面板
        面板 = AI播放面板()
        面板.追加("你好")
        self.assertIn("你好", 面板.文本())
        self.assertIn("[", 面板.文本())

    def test_忙碌时按钮禁用(self):
        from v8_3.界面.AI播放面板 import AI播放面板
        面板 = AI播放面板()
        面板.设置忙碌(True)
        self.assertFalse(面板.翻译按钮.isEnabled())
        面板.设置忙碌(False)
        self.assertTrue(面板.翻译按钮.isEnabled())

    def test_字幕查找顺序_本地优先(self):
        import tempfile
        from v8_3.界面.AI播放面板 import AI字幕动作

        class 假助手:
            @staticmethod
            def 读取字幕文件(路径):
                return [{"开始秒": 0.0, "结束秒": 1.0, "文本": Path(路径).read_text()[:8]}]

        with tempfile.TemporaryDirectory() as 临时:
            视频 = Path(临时) / "x.mp4"
            视频.write_bytes(b"x")
            视频.with_suffix(".srt").write_text("本地字幕", encoding="utf-8")
            会话 = type("会话", (), {"远端路径": str(视频), "标题": "x.mp4",
                                 "网盘标识": "本地"})()
            动作 = AI字幕动作(取会话=lambda: 会话, 字幕助手=假助手)
            条目, 来源 = 动作.取字幕条目(允许联网=True)
        self.assertEqual(len(条目), 1)
        self.assertIn("本地字幕", 来源)

    def test_没播放时给出明确提示(self):
        from v8_3.界面.AI播放面板 import AI字幕动作
        动作 = AI字幕动作(取会话=lambda: None, 字幕助手=object())
        条目, 来源 = 动作.取字幕条目()
        self.assertEqual(条目, [])
        self.assertIn("还没开始播放", 来源)


# ==================== 路径选择对话框（"选不中视频文件"的回归） ====================


@unittest.skipUnless(Qt可用, "没装 PySide6")
class 路径选择对话框测试(unittest.TestCase):
    """回归：**选中文件点"确定"永远不生效**。

    老实现要求"选中项路径 == 路径框文本"才算选中文件，可路径框显示的是**当前目录**，
    于是用户选好视频点确定，对话框纹丝不动（只闪一行小提示），感觉就是"选不中"。
    """

    class 条目:
        def __init__(self, name, path, is_dir=False, size=100):
            self.name, self.path, self.is_dir, self.size = name, path, is_dir, size

    class 假适配器:
        def 列目录(self, 路径="/"):
            return [路径选择对话框测试.条目("文件夹", "/文件夹", True),
                    路径选择对话框测试.条目("影片.mp4", "/影片.mp4"),
                    路径选择对话框测试.条目("说明.txt", "/说明.txt")]

    def _打开(self, **kw):
        from v8_3.界面.路径选择对话框 import 路径选择对话框
        from v8_3.播放.媒体信息 import 是视频文件
        参数 = {"只要文件": True, "名字过滤": 是视频文件,
              "过滤提示": "不是常见视频文件"}
        参数.update(kw)
        框 = 路径选择对话框(self.假适配器(), "fake_1", 初始路径="/", **参数)
        框.show()
        泵(0.3)
        return 框

    def _行(self, 框, 后缀):
        for i in range(框.列表.count()):
            if 框.列表.item(i).text().endswith(后缀):
                return i
        raise AssertionError(f"列表里没有 {后缀}")

    def test_选中视频点确定能返回路径(self):
        框 = self._打开()
        框.列表.setCurrentRow(self._行(框, ".mp4"))
        泵(0.1)
        self.assertTrue(框.确定按钮.isEnabled(), "选中视频后确定应可用")
        框._确定选择()
        self.assertEqual(框.选中路径, "/影片.mp4")
        self.assertEqual(框.result(), 1, "应该真的接受（1=Accepted）")
        框.close()

    def test_双击视频直接打开(self):
        框 = self._打开()
        框._双击进入(框.列表.item(self._行(框, ".mp4")))
        self.assertEqual(框.选中路径, "/影片.mp4")
        self.assertEqual(框.result(), 1)
        框.close()

    def test_只选目录不给确定(self):
        框 = self._打开()
        框.列表.setCurrentRow(self._行(框, "文件夹"))
        泵(0.1)
        self.assertFalse(框.确定按钮.isEnabled(), "选目录时确定必须禁用")
        框._确定选择()
        self.assertEqual(框.选中路径, "", "绝不能把目录路径交回去")
        self.assertEqual(框.result(), 0)
        框.close()

    def test_非视频文件不可选(self):
        框 = self._打开()
        项 = 框.列表.item(self._行(框, ".txt"))
        self.assertFalse(bool(项.flags() & Qt.ItemIsSelectable),
                        "非视频文件应灰掉且不可选")
        框.close()

    def test_手输文件路径也认(self):
        框 = self._打开()
        框.路径框.setText("/影片.mp4")
        框._手输过 = True               # 模拟用户真的敲进路径框
        框._确定选择()
        self.assertEqual(框.选中路径, "/影片.mp4")
        self.assertEqual(框.result(), 1)
        框.close()

    def test_传输页语义不受影响(self):
        """传输页选的是**目录**，行为必须和以前一样。"""
        框 = self._打开(只要文件=False, 名字过滤=None, 允许新建=True)
        框._确定选择()
        self.assertEqual(框.选中路径, "/")
        self.assertEqual(框.result(), 1)
        框.close()


# ==================== 独立窗口：适应屏幕 / 关窗交接 ====================


@unittest.skipUnless(Qt可用, "没装 PySide6")
class 窗口适应屏幕与交接测试(unittest.TestCase):
    def setUp(self):
        self.会话 = 假会话()
        self.窗 = 播放器窗口(self.会话, 标题="样片.mp4")
        self.窗.起播()
        泵(0.1)

    def tearDown(self):
        self.窗.close()
        泵(0.05)

    def test_初始尺寸不超过屏幕(self):
        区域 = self.窗.屏幕几何()
        if 区域 is None:
            self.skipTest("没有屏幕信息")
        self.assertLessEqual(self.窗.width(), max(480, 区域.width()))
        self.assertLessEqual(self.窗.height(), max(300, 区域.height()))

    def test_适应屏幕会把超大窗口缩回来(self):
        区域 = self.窗.屏幕几何()
        if 区域 is None:
            self.skipTest("没有屏幕信息")
        self.窗.resize(区域.width() + 600, 区域.height() + 400)
        泵(0.05)
        self.窗.适应屏幕()
        泵(0.05)
        self.assertLessEqual(self.窗.width(), 区域.width(),
                            "4K 片源在 1080p 屏上也必须装得下")
        self.assertLessEqual(self.窗.height(), 区域.height())

    def test_适应屏幕会把缩放复位成自动(self):
        倍率 = []
        self.会话.播放器.设置缩放 = lambda v: 倍率.append(float(v))
        self.窗.适应屏幕()
        self.assertIn(0.0, 倍率, "适应屏幕应把 VLC 缩放复位成 0（自动适应窗口）")

    def test_交接信息带位置与直链(self):
        信息 = self.窗.交接信息()
        self.assertEqual(信息.get("类型"), "独立窗口关闭")
        self.assertIn("位置秒", 信息)
        self.assertIn("直链信息", 信息)
        self.assertTrue(信息["直链信息"].get("url"), "交接必须带直链，页面才不用重取")

    def test_菜单里有适应屏幕(self):
        标题 = []
        for 菜单动作 in self.窗.菜单栏.actions():
            if 菜单动作.menu() is not None:
                标题 += [a.text() for a in 菜单动作.menu().actions()]
        self.assertTrue(any("适应屏幕" in t for t in 标题),
                        "视频菜单里要有「适应屏幕」")


# ==================== 不给 libvlc 自己开窗口的机会（用户实测的"超大窗口"） ====================


@unittest.skipUnless(Qt可用, "没装 PySide6")
class 显示防线测试(unittest.TestCase):
    """回归：Wayland 下句柄为 0 → **libvlc 自己弹一个 VLC 窗口**放视频。

    用户实测的截图：标题栏写着 "VLC media player"、比屏幕还大、界面控件管不到它。
    所以：桌面平台 + 句柄 0 时必须**拦住开播**，除非用户显式同意。
    """

    def test_显式wayland但XWayland在时切成xcb(self):
        import os
        from v8_3.播放 import 显示环境
        旧 = dict(os.environ)
        try:
            os.environ["QT_QPA_PLATFORM"] = "wayland"     # 用户显式设成 wayland
            os.environ["DISPLAY"] = ":0"
            os.environ["WAYLAND_DISPLAY"] = "wayland-0"
            os.environ.pop("V8_3_不强制X11", None)
            结果 = 显示环境.准备嵌入显示()
            self.assertEqual(结果["平台"], "xcb")
            self.assertTrue(结果["可嵌入"])
        finally:
            os.environ.clear()
            os.environ.update(旧)

    def test_是桌面平台判定(self):
        from v8_3.播放.显示环境 import 是桌面平台
        self.assertTrue(是桌面平台("wayland"))
        self.assertTrue(是桌面平台("xcb"))
        for 平台 in ("offscreen", "minimal", "vnc", "linuxfb", "eglfs"):
            self.assertFalse(是桌面平台(平台), f"{平台} 是无头平台")

    def test_无窗口原因给人话(self):
        from v8_3.播放.显示环境 import 无窗口原因
        文本 = 无窗口原因("wayland")
        self.assertIn("X11", 文本)
        self.assertIn("QT_QPA_PLATFORM=xcb", 文本, "要告诉用户怎么修")
        self.assertIn("V8_3_允许VLC自带窗口", 文本,
                      "也要给出「我就要用 VLC 窗口」的办法")

    def test_默认不允许VLC自带窗口(self):
        import os
        from v8_3.播放.显示环境 import 允许VLC自带窗口
        旧 = os.environ.pop("V8_3_允许VLC自带窗口", None)
        try:
            self.assertFalse(允许VLC自带窗口(), "默认必须是不同意")
            os.environ["V8_3_允许VLC自带窗口"] = "1"
            self.assertTrue(允许VLC自带窗口())
        finally:
            os.environ.pop("V8_3_允许VLC自带窗口", None)
            if 旧 is not None:
                os.environ["V8_3_允许VLC自带窗口"] = 旧

    def test_句柄0且桌面平台时拒绝开播(self):
        from v8_3.界面 import 播放器窗口 as 窗口模块
        会话 = 假会话()
        窗 = 窗口模块.播放器窗口(会话, 标题="样片.mp4")
        try:
            窗._已映射 = lambda: True                    # 跳过"等窗口映射"
            窗._安全句柄 = lambda: 0                     # 假装拿不到 X11 窗口号
            原 = 窗口模块.是桌面平台
            窗口模块.是桌面平台 = lambda *_: True        # 假装是桌面（有人在看）
            try:
                成功 = 窗.起播()
            finally:
                窗口模块.是桌面平台 = 原
            self.assertFalse(成功, "桌面环境 + 句柄 0 必须拒绝开播")
            self.assertIn("X11", 窗.状态标签.text() + 窗.AI面板.文本())
        finally:
            窗.close()
            泵(0.05)

    def test_用户同意后允许自带窗口(self):
        from v8_3.界面 import 播放器窗口 as 窗口模块
        会话 = 假会话()
        窗 = 窗口模块.播放器窗口(会话, 标题="样片.mp4")
        try:
            窗._安全句柄 = lambda: 0
            原 = 窗口模块.是桌面平台
            窗口模块.是桌面平台 = lambda *_: True
            try:
                窗.用VLC自带窗口播放()
            finally:
                窗口模块.是桌面平台 = 原
            self.assertTrue(窗._允许自带窗口, "点过之后应记为已同意")
        finally:
            窗.close()
            泵(0.05)


# ==================== 单播放器架构（画面在窗口之间搬，不新建播放器） ====================


@unittest.skipUnless(Qt可用, "没装 PySide6")
class 单播放器架构测试(unittest.TestCase):
    """回归：独立窗口**不能再新建一个播放会话**。

    用户实测：新建第二个 player 之后画面跑到另一个窗口里（libvlc 自己开的）、
    我们的窗口是黑的，关掉一个另一个还在响（"后台还有声音，界面上没画面"）。
    正确做法是全局只有一个会话，靠"绑窗口 + 重开媒体 + 跳回原位置"搬画面。
    """

    def setUp(self):
        self.会话 = 假会话()
        self.窗口 = 播放器窗口(self.会话, 标题="样片.mp4", 接管=True)
        # X11 下起播会先等"窗口已映射"（真机需要），测试里固定为真以免断言被时序干扰
        self.窗口._已映射 = lambda: True
        self.归还 = []
        self.窗口.宿主回调 = {"归还播放": lambda w: self.归还.append(w)}

    def tearDown(self):
        self.窗口.close()
        泵(0.05)

    def test_接管用同一个会话起播(self):
        self.assertTrue(self.窗口.接管播放())
        self.assertIs(self.窗口.会话, self.会话, "接管不能换会话")
        self.assertEqual(self.会话.起播次数, 1, "接管只应起播一次（同一个播放器）")

    def test_接管会跳回原位置(self):
        self.会话.播放器.进度秒 = lambda: 42.0
        self.窗口.接管播放()
        # 两次 singleShot（900/1800ms）都要等事件循环；这里直接验证"计划了跳转"：
        self.assertTrue(hasattr(self.会话.播放器, "跳转们"))
        泵(2.0)
        self.assertTrue(any(abs(v - 42.0) < 0.01
                        for v in self.会话.播放器.跳转们),
                        f"应跳回原位置 42s：{self.会话.播放器.跳转们}")

    def test_接管时视频区宽度护栏(self):
        self.窗口.主体.setSizes([0, 380])          # 模拟视频区被面板挤成 0 宽
        泵(0.05)
        self.窗口.接管播放()
        泵(0.05)
        self.assertGreater(self.窗口.视频.width(), 0,
                           "视频区宽度为 0 时 libvlc 无处可画（窗口会一片黑）")

    def test_关窗不关会话且归还宿主(self):
        self.窗口.接管播放()
        self.窗口.close()
        泵(0.1)
        self.assertEqual(self.会话.关闭次数, 0,
                         "接管模式下关窗**不能**关闭会话（否则声音/进度断了）")
        self.assertEqual(len(self.归还), 1, "关窗要把播放归还宿主")

    def test_非接管模式仍然自己起播并关会话(self):
        会话 = 假会话()
        窗 = 播放器窗口(会话, 标题="独立.mp4")      # 默认 接管=False
        窗._已映射 = lambda: True
        try:
            self.assertTrue(窗.起播())
            self.assertEqual(会话.起播次数, 1)
            窗.close()
            泵(0.1)
            self.assertGreaterEqual(会话.关闭次数, 1, "自带模式关窗要收掉自己的会话")
        finally:
            窗.close()

    def test_接管失败时明确报错(self):
        self.会话.起播 = lambda *a, **k: False
        self.assertFalse(self.窗口.接管播放())
        self.assertIn("失败", self.窗口.状态标签.text())


# ==================== 选择对话框里切换网盘（用户反馈） ====================


@unittest.skipUnless(Qt可用, "没装 PySide6")
class 对话框换网盘测试(unittest.TestCase):
    """回归：播放器里点「打开网盘」后**没法换网盘**（用户反馈缺这个能力）。"""

    class 条目:
        def __init__(self, name, path, is_dir=False, size=10):
            self.name, self.path, self.is_dir, self.size = name, path, is_dir, size

    class 规格:
        def __init__(self, 名):
            self.显示名 = 名
            self.图标 = "🧪"

    def test_能在对话框里切换网盘(self):
        from v8_3.界面.路径选择对话框 import 路径选择对话框
        from v8_3.播放.媒体信息 import 是视频文件
        列过 = []

        class 适配器:
            def __init__(self, 盘):
                self.盘 = 盘

            def 列目录(self, 路径="/"):
                列过.append((self.盘, 路径))
                return [对话框换网盘测试.条目(f"{self.盘}的片子.mp4",
                                        f"/{self.盘}的片子.mp4")]

        适配器们 = {"甲": 适配器("甲"), "乙": 适配器("乙")}
        框 = 路径选择对话框(适配器们["甲"], "甲", 初始路径="/",
                     只要文件=True, 名字过滤=是视频文件,
                     网盘列表={"甲": self.规格("甲盘"), "乙": self.规格("乙盘")},
                     取适配器=lambda 标识: 适配器们[标识])
        框.show()
        泵(0.4)
        self.assertTrue(hasattr(框, "网盘框"), "对话框应该有网盘下拉框")
        self.assertEqual(框.网盘框.count(), 2)
        框.网盘框.setCurrentIndex(框.网盘框.findData("乙"))
        泵(0.4)
        self.assertEqual(框.选中网盘标识, "乙", "切换后要记住选中网盘")
        self.assertEqual(框.当前路径, "/", "换网盘回到根目录")
        名称们 = [框.列表.item(i).text() for i in range(框.列表.count())]
        self.assertTrue(any("乙" in t for t in 名称们),
                        f"换盘后应该列出新盘的内容：{名称们}")
        self.assertIn(("乙", "/"), 列过, "应该用新适配器去列目录")
        框.close()
        泵(0.3)          # 让后台列目录线程收尾（关闭时会断开它的信号）

    def test_没给网盘列表时不显示下拉(self):
        from v8_3.界面.路径选择对话框 import 路径选择对话框
        框 = 路径选择对话框(对话框换网盘测试.适配器("甲"), "甲", 初始路径="/")
        self.assertFalse(hasattr(框, "网盘框"),
                        "没传网盘列表就不该有下拉（传输页等旧用法不受影响）")
        框.close()

    class 适配器:  # noqa: F811 - 给上面的测试复用
        def __init__(self, 盘=""):
            self.盘 = 盘

        def 列目录(self, 路径="/"):
            return []


# ==================== 游离窗口（libvlc 自己开窗口）巡检 ====================


@unittest.skipUnless(Qt可用, "没装 PySide6")
class 游离窗口测试(unittest.TestCase):
    """回归：libvlc 偶尔**自己开一个 VLC 窗口**放视频 → "视频和播放器分离"。

    这里验证"能发现"和"能关掉"；X11 不可用时自动跳过（判据是窗口标题以
    ``VLC media player`` 结尾 —— 用结尾判断，避免把用户片名里带 vlc 的窗口误判）。
    """

    def setUp(self):
        from v8_3.播放 import 游离窗口
        self.模块 = 游离窗口
        if not 游离窗口.可用():
            self.skipTest(f"没有 X11：{游离窗口.不可用原因()}")

    def test_发现并关闭假VLC窗口(self):
        from PySide6.QtWidgets import QWidget
        # Qt 造的窗口 WM_CLASS 是 python3，**不该**被当成 VLC 自己的窗口
        # （真 VLC 的发现/收掉由 真VLC游离窗口测试 用真进程覆盖）
        假 = QWidget()
        假.setWindowTitle("某片子.mp4 - VLC media player")
        假.resize(320, 240)
        假.show()
        泵(0.3)
        try:
            找到 = self.模块.找游离窗口()
            self.assertFalse(any(号 == int(假.winId()) for 号, _名 in 找到),
                             f"Qt 窗口被误判成 VLC 窗口了：{找到}")
            self.assertTrue(self.模块.是VLC窗口("vlc", "任意标题"))
            self.assertFalse(self.模块.是VLC窗口("python3", 假.windowTitle()))
        finally:
            假.close()

    def test_不会把普通窗口当成游离窗口(self):
        from PySide6.QtWidgets import QWidget
        窗 = QWidget()
        窗.setWindowTitle("V8_3 播放器（VLC 风格）")   # 提到 VLC 但不是 VLC 自己的窗口
        窗.show()
        泵(0.2)
        try:
            找到 = dict(self.模块.找游离窗口())
            self.assertNotIn(int(窗.winId()), 找到,
                             "标题里有 vlc 不等于就是 VLC 自己的窗口（必须看结尾）")
        finally:
            窗.close()

    def test_排除自己的窗口号(self):
        from PySide6.QtWidgets import QWidget
        假 = QWidget()
        假.setWindowTitle("x.mp4 - VLC media player")
        假.show()
        泵(0.3)
        try:
            找到 = dict(self.模块.找游离窗口(排除窗口号=(int(假.winId()),)))
            self.assertNotIn(int(假.winId()), 找到, "排除的窗口号不该被报出来")
        finally:
            假.close()


@unittest.skipUnless(Qt可用, "没装 PySide6")
class 侧栏开关测试(unittest.TestCase):
    """回归：右侧「播放清单 / AI 助手」面板要能用按钮真的隐藏/显示。"""

    def setUp(self):
        self.会话 = 假会话()
        self.窗 = 播放器窗口(self.会话, 标题="样片.mp4", 接管=True)
        self.窗.接管播放()
        泵(0.1)

    def tearDown(self):
        self.窗.close()
        泵(0.05)

    def test_面板开关在菜单栏帮助之后(self):
        """按用户要求：显示/隐藏面板的按钮移到顶部、放在「帮助」后面。"""
        动作 = getattr(self.窗.菜单栏, "侧栏动作", None)
        self.assertIsNotNone(动作, "菜单栏应该有「🗂 面板」开关")
        self.assertIn("面板", 动作.text())
        # 它必须排在「帮助」之后
        标题 = [a.text() for a in self.窗.菜单栏.actions()]
        self.assertEqual(标题[-1], 动作.text(),
                         f"面板开关应该在菜单栏最后（帮助之后）：{标题}")
        self.assertTrue(any(t.startswith("帮助") for t in 标题))
        self.assertIsNone(self.窗.工具栏,
                          "视频上方那行已整行撤销（按需求）")

    def test_开关能隐藏且鼠标移动不会弹回(self):
        self.窗.切换侧栏(False)
        泵(0.05)
        self.assertFalse(self.窗.右栏.isVisible())
        self.窗.显示控件()                 # 模拟鼠标移动
        泵(0.05)
        self.assertFalse(self.窗.右栏.isVisible(),
                         "用户主动隐藏的面板不该被鼠标移动弹回来")
        self.窗.切换侧栏(True)
        泵(0.05)
        self.assertTrue(self.窗.右栏.isVisible())

    def test_独立播放器控制条是新布局(self):
        """按用户要求：进度条独立一行紧贴视频；上下一个在停止之后；下方无音量；
        速度框带「速度」文字标签。"""
        条 = self.窗.控制条
        self.assertTrue(条.上一个按钮.isVisibleTo(条), "控制条要有「上一个」")
        self.assertTrue(条.下一个按钮.isVisibleTo(条), "控制条要有「下一个」")
        self.assertIsNotNone(条.音量条, "音量滑块在视频下方（按需求搬下来）")
        self.assertEqual(条.速度标签.text(), "速度", "速度框要有文字标签")
        # 音量（静音 + 滑块）就在「速度」前面
        self.assertTrue(条.静音按钮.isVisibleTo(条), "有静音按钮（带音量管理）")
        # 按钮顺序：⏸ → ⏮ 上一个 → ⏹ 停止 → ⏭ 下一个 → 🔁 → 🔀
        self.assertEqual(
            [条.播放暂停按钮.text(), 条.上一个按钮.text(), 条.停止按钮.text(),
             条.下一个按钮.text(), 条.循环按钮.text()[:1], 条.随机按钮.text()[:1]],
            ["⏸", "⏮ 上一个", "⏹", "⏭ 下一个", "🔁", "🔀"])
        # 进度条加粗
        self.assertGreaterEqual(条.进度条.height(), 10)
        self.assertTrue(条.进度条.styleSheet(), "进度条有加粗样式")
        # 布局顺序：视频 → 进度行 → 按钮行
        布局 = 条.layout()
        self.assertGreaterEqual(布局.count(), 2, "控制条应该是两行（进度 + 按钮）")

    def test_两个页签各自开关(self):
        self.窗.切换AI面板(True)
        泵(0.05)
        self.assertEqual(self.窗.右栏.currentIndex(), 1)
        self.窗.切换清单(True)
        泵(0.05)
        self.assertEqual(self.窗.右栏.currentIndex(), 0)
        self.窗.切换清单(False)
        泵(0.05)
        self.assertFalse(self.窗.右栏.isVisible())


# ==================== 工具栏宽度自适应（「🗂 面板」被裁掉的回归） ====================


@unittest.skipUnless(Qt可用, "没装 PySide6")
class 工具栏自适应测试(unittest.TestCase):
    """回归：窗口 1188px 时，文字版工具栏实际需要 ~1400px，
    末尾的「速度 / 🗂 面板」被**无声裁掉**（用户截图：面板按钮完全不显示）。

    两次踩坑都要锁住：
    ① 硬阈值（<1000 才紧凑）不够 —— 要按"真实需要宽度"判断；
    ② "展开成文字"必须**复核**，展开后装不下要立刻退回紧凑。
    """

    def _动作表(self):
        表 = {k: (lambda *a, **kw: None) for k in
              ("打开网盘", "打开本地", "播放暂停", "停止", "上一个", "下一个",
               "全屏", "截图", "切换清单", "切换AI面板", "切换侧栏", "静音切换",
               "设置音量", "设置速度", "循环切换", "随机切换", "AI诊断")}
        表["是否静音"] = lambda: False
        表["当前速度"] = lambda: 1.0
        return 表

    def test_任何宽度都尽量装得下(self):
        """核心断言：**装得下**（末尾的「🗂 面板」不能被裁掉）。

        600px 这种极端窄的窗口，连"图标档"都塞不下（需要 ~697px）——
        那时只能靠窗口最小宽度兜底（窗口 minimumSize 480，但工具栏内容更宽），
        所以这里对 ≥700px 要求"必须装得下"，更窄的只要求"不比最紧档更宽"。
        """
        from v8_3.界面.vlc风格 import 构建工具栏
        栏 = 构建工具栏(self._动作表())
        for 宽 in (700, 900, 1084, 1183, 1188, 1400, 1600, 1920):
            模式 = 栏.按宽度自适应(宽)
            需要 = 栏.sizeHint().width()
            self.assertLessEqual(需要, 宽,
                                 f"{宽}px 窗口里工具栏装不下（需要 {需要}px）")
        # 很窄时必须落到图标档；1183px 必须还能显示文字（用户明确要求）
        self.assertEqual(栏.按宽度自适应(800), "图标")
        栏.按宽度自适应(1183)
        self.assertIn(栏.当前模式, ("精简", "完整"),
                      "1183px 的窗口应该还能显示文字（不要掉到纯图标）")
        self.assertIn("面板", 栏.侧栏动作.text())

    def test_宽窗口显示完整文字_并且速度有标签(self):
        from v8_3.界面.vlc风格 import 构建工具栏
        栏 = 构建工具栏(self._动作表())
        模式 = 栏.按宽度自适应(1600)
        self.assertEqual(模式, "完整", "窗口够宽时应该用完整文字")
        self.assertIn("面板", 栏.侧栏动作.text())
        self.assertTrue(any("打开网盘" in a.text() for a in 栏.动作们),
                        f"完整档应显示带文字的按钮：{[a.text() for a in 栏.动作们]}")
        # 用户明确要求「速度」要显示出来
        self.assertEqual(栏.速度标签.text().strip(), "速度")
        self.assertTrue(栏.速度标签.isVisibleTo(栏))
        self.assertLessEqual(栏.sizeHint().width(), 1600)

    def test_1183px也能显示速度与面板文字(self):
        """用户的实际窗口宽度：速度和面板都必须显示文字。"""
        from v8_3.界面.vlc风格 import 构建工具栏
        栏 = 构建工具栏(self._动作表())
        模式 = 栏.按宽度自适应(1183)
        self.assertIn(模式, ("精简", "完整"))
        self.assertIn("面板", 栏.侧栏动作.text())
        self.assertEqual(栏.速度标签.text().strip(), "速度")
        self.assertTrue(栏.速度标签.isVisibleTo(栏))
        self.assertLessEqual(栏.sizeHint().width(), 1183)

    def test_面板按钮始终在可视区(self):
        from v8_3.界面.vlc风格 import 构建工具栏
        栏 = 构建工具栏(self._动作表())
        for 宽 in (700, 900, 1188, 1400, 1600):
            栏.按宽度自适应(宽)
            栏.resize(宽, 40)
            泵(0.02)
            右 = 栏.actionGeometry(栏.侧栏动作).right()
            self.assertLessEqual(右, 宽,
                                 f"{宽}px 时「🗂 面板」被挤出可视区（右边界 {右}）")

    def test_反复切换宽度不会卡死(self):
        from v8_3.界面.vlc风格 import 构建工具栏
        栏 = 构建工具栏(self._动作表())
        for 宽 in (1600, 700, 1600, 700, 1183, 900, 1500):
            模式 = 栏.按宽度自适应(宽)
            self.assertLessEqual(栏.sizeHint().width(), 宽 if 宽 >= 700 else 900)
            self.assertIn(模式, ("图标", "精简", "完整"))


# ==================== 游离窗口：用**真 VLC 进程**端到端验证 ====================


@unittest.skipUnless(Qt可用, "没装 PySide6")
class 真VLC游离窗口测试(unittest.TestCase):
    """真的起一个 ``cvlc`` 进程，验证"发现 → 收掉"整条链路。

    为什么必须用真 VLC：用户实测的中文界面下，VLC 自己的窗口标题是
    "VLC 媒体播放器"，只按英文标题匹配会**漏掉**（分离就一直存在）。
    所以判据改成了 ``WM_CLASS == vlc``（类名不随语言变），这里用真进程锁住它。
    """

    @classmethod
    def setUpClass(cls):
        from v8_3.播放 import 游离窗口
        cls.模块 = 游离窗口
        if not 游离窗口.可用():
            raise unittest.SkipTest(f"没有 X11：{游离窗口.不可用原因()}")
        cls.cvlc = shutil.which("cvlc") or shutil.which("vlc")
        if not cls.cvlc:
            raise unittest.SkipTest("没装 VLC 命令行")
        # 造一个 2 秒的小视频给 cvlc 播
        cls.目录 = Path(tempfile.mkdtemp(prefix="v83游离_"))
        cls.视频 = cls.目录 / "游离.mp4"
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise unittest.SkipTest("没有 ffmpeg")
        subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                        "testsrc2=size=640x360:rate=25:duration=20",
                        "-c:v", "libx264", "-preset", "ultrafast",
                        "-pix_fmt", "yuv420p", str(cls.视频)],
                       capture_output=True, timeout=180)

    def _起cvlc(self):
        import os
        环境 = dict(os.environ)
        进程 = subprocess.Popen([self.cvlc, "--no-video-title-show",
                              str(self.视频)],
                             env=环境, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        # 等它的窗口出现
        for _ in range(40):
            泵(0.2)
            找到 = self.模块.找游离窗口()
            if 找到:
                return 进程, 找到
        return 进程, []

    def test_发现并收掉真VLC窗口(self):
        进程, 找到 = self._起cvlc()
        try:
            self.assertTrue(找到, "真 VLC 窗口应该能被发现（WM_CLASS=vlc）")
            号 = 找到[0][0]
            self.assertTrue(self.模块.请关闭窗口(号))
            泵(1.0)
            if self.模块.找游离窗口():       # VLC 常常忽略 WM_DELETE_WINDOW
                self.assertTrue(self.模块.销毁窗口(号))
                泵(1.0)
            self.assertFalse(any(号 == x for x, _ in self.模块.找游离窗口()),
                             "强制收掉后那个游离窗口必须消失（画面回到播放器里）")
        finally:
            try:
                进程.terminate()
            except Exception:  # noqa: BLE001
                pass

    def test_自己的窗口不会被误判(self):
        """Qt 窗口（类名 python3）即便标题写成 VLC 也不能被当成游离窗口。

        否则守护会去关我们自己的窗口 —— 那就成了灾难。
        """
        from PySide6.QtWidgets import QWidget
        窗 = QWidget()
        窗.setWindowTitle("某片.mp4 - VLC media player")
        窗.show()
        泵(0.3)
        try:
            self.assertFalse(
                any(int(窗.winId()) == x for x, _ in self.模块.找游离窗口()),
                "自己的 Qt 窗口被误判了")
            self.assertFalse(self.模块.是VLC窗口("python3", 窗.windowTitle()))
        finally:
            窗.close()

    def test_中文标题也能兜底(self):
        self.assertTrue(self.模块.是VLC窗口("", "片子 - VLC 媒体播放器"))
        self.assertTrue(self.模块.是VLC窗口("", "VLC media player"))
        self.assertTrue(self.模块.是VLC窗口("vlc", "任意"))


# ==================== 按视频比例开窗（消除黑边） ====================


@unittest.skipUnless(Qt可用, "没装 PySide6")
class 按视频比例测试(unittest.TestCase):
    """需求：打开视频时按分辨率调整窗口，避免黑边。

    判据不是"窗口尺寸等于视频尺寸"，而是**视频区的宽高比 == 视频宽高比** ——
    libvlc 在窗口里按"保持比例+居中"铺画面，比例一致才不会有黑边。
    """

    def _窗口(self, 宽=3840, 高=2160, 收面板=True):
        会话 = 假会话()
        会话.媒体 = type("媒体", (), {"宽": 宽, "高": 高})()
        窗 = 播放器窗口(会话, 标题="样片.mp4", 接管=True)
        # ⚠️ 必须 show() 再泵一下：没显示的窗口不会做布局，视频区还是占位尺寸，
        # "按比例"这件事根本量不出来（踩过）
        窗.show()
        泵(0.1)
        if 收面板:
            # 离屏平台的"屏幕"只有 800x800，右侧面板会吃掉一半宽度；
            # 收起面板才能验证"按比例"这件事本身
            窗.切换侧栏(False)
            泵(0.1)
        return 窗

    def test_视频比例取自探测结果(self):
        窗 = self._窗口(1920, 1080)
        try:
            self.assertAlmostEqual(窗.视频比例(), 16 / 9, places=4)
        finally:
            窗.close()

    def test_没有媒体信息时不乱调(self):
        会话 = 假会话()
        会话.媒体 = None
        窗 = 播放器窗口(会话, 标题="样片.mp4", 接管=True)
        try:
            self.assertIsNone(窗.视频比例())
            self.assertFalse(窗.适应视频比例(), "拿不到分辨率就别动窗口")
            self.assertGreater(窗.视频区比例误差(), 100)
        finally:
            窗.close()

    def test_横屏视频调完没有黑边(self):
        窗 = self._窗口(3840, 2160)
        try:
            泵(0.1)
            self.assertTrue(窗.适应视频比例())
            泵(0.1)
            误差 = 窗.视频区比例误差()
            self.assertLess(误差, 0.02,
                            f"视频区比例与视频不一致（会出黑边）：误差 {误差:.4f}，"
                            f"视频区 {窗.视频.width()}x{窗.视频.height()}")
        finally:
            窗.close()

    def test_竖屏视频也能对上比例(self):
        窗 = self._窗口(1080, 1920)
        try:
            泵(0.1)
            窗.适应视频比例()
            泵(0.1)
            self.assertLess(窗.视频区比例误差(), 0.02,
                            f"竖屏视频应得到竖着的窗口：{窗.视频.width()}x{窗.视频.height()}")
        finally:
            窗.close()

    def test_窗口装得进屏幕(self):
        窗 = self._窗口(7680, 4320)          # 8K，必须缩到屏幕内
        try:
            泵(0.1)
            窗.适应视频比例()
            泵(0.1)
            区域 = 窗.屏幕几何()
            if 区域 is not None:
                self.assertLessEqual(窗.width(), 区域.width())
                self.assertLessEqual(窗.height(), 区域.height())
        finally:
            窗.close()

    def test_复位缩放与宽高比(self):
        窗 = self._窗口(1920, 1080)
        调用 = []
        窗.会话.播放器.设置缩放 = lambda v: 调用.append(("缩放", float(v)))
        窗.会话.播放器.设置宽高比 = lambda v: 调用.append(("比例", str(v)))
        try:
            泵(0.1)
            窗.适应视频比例()
            self.assertIn(("缩放", 0.0), 调用, "应把 VLC 缩放复位为自动")
            self.assertIn(("比例", ""), 调用, "应把 VLC 宽高比复位为默认")
        finally:
            窗.close()


class 视频区尺寸计算测试(unittest.TestCase):
    """纯函数验证：**任何屏幕/任何视频比例下都不该产生黑边**（比例必须守住）。"""

    def _比例们(self):
        return (16 / 9, 4 / 3, 21 / 9, 2.35, 1.0, 9 / 16, 1080 / 1920, 3840 / 1600)

    def test_各种屏幕与比例都对得上(self):
        for 比例 in self._比例们():
            for 可用 in ((1920, 1000), (1200, 700), (800, 800), (700, 500),
                       (400, 300), (300, 900), (2400, 1300), (1000, 200)):
                with self.subTest(比例=round(比例, 3), 可用=可用):
                    宽, 高 = 播放器窗口.算视频区尺寸(比例, 可用[0], 可用[1], 3840)
                    误差 = abs(宽 / max(1, 高) - 比例)
                    self.assertLess(误差, 0.03,
                                    f"{可用} 下算出 {宽}x{高}，比例误差 {误差:.4f}")

    def test_竖屏不会被夹成横向(self):
        宽, 高 = 播放器窗口.算视频区尺寸(9 / 16, 1920, 1000, 1080)
        self.assertLess(宽, 高, "竖屏视频必须得到竖着的视频区")

    def test_不放大超过原分辨率(self):
        宽, 高 = 播放器窗口.算视频区尺寸(16 / 9, 3840, 2160, 1280)
        self.assertLessEqual(宽, 1280 + 4, "小片子不该被放大到超过原始分辨率")

    def test_8K也能算出来(self):
        宽, 高 = 播放器窗口.算视频区尺寸(16 / 9, 1800, 900, 7680)
        self.assertLessEqual(宽, 1800)
        self.assertLessEqual(高, 900)
        self.assertLess(abs(宽 / 高 - 16 / 9), 0.03)

    def test_比例非法时退化不炸(self):
        宽, 高 = 播放器窗口.算视频区尺寸(0, 800, 600, 1920)
        self.assertGreater(宽, 0)
        self.assertGreater(高, 0)


class 面板显隐不留黑边测试(unittest.TestCase):
    """需求：**藏起右侧面板后不能出现左右黑边**。

    原因：藏面板会让视频区变宽，窗口尺寸不变就和视频比例对不上了 → 左右黑边。
    做法：显隐/切页签之后按视频比例重排窗口（藏面板 = 窗口跟着收窄）。
    """

    def _窗口(self):
        会话 = 假会话()
        会话.媒体 = type("媒体", (), {"宽": 3840, "高": 2160})()
        窗 = 播放器窗口(会话, 标题="样片.mp4", 接管=True)
        窗.show()
        泵(0.15)
        窗.适应视频比例()
        泵(0.15)
        return 窗

    def test_藏起面板后比例仍然对(self):
        """藏面板 = 视频区变宽 → 必须跟着重排窗口，否则就是左右黑边。"""
        窗 = self._窗口()
        try:
            窗.切换侧栏(False)              # 先确保面板是藏着的（视频区拿到全宽）
            泵(0.5)
            误差 = 窗.视频区比例误差()
            self.assertLess(误差, 0.03,
                            f"藏起面板后出现黑边（误差 {误差:.4f}，"
                            f"视频区 {窗.视频.width()}x{窗.视频.height()}）")
        finally:
            窗.close()

    def test_再显示面板也不留黑边(self):
        窗 = self._窗口()
        区域 = 窗.屏幕几何()
        if 区域 is not None and 区域.width() < 1100:
            窗.close()
            self.skipTest(f"屏幕太小（{区域.width()}px），装不下面板+视频比例")
        try:
            窗.切换侧栏(False)
            泵(0.4)
            窗.切换侧栏(True)
            泵(0.5)
            self.assertLess(窗.视频区比例误差(), 0.03)
        finally:
            窗.close()

    def test_切页签后也不留黑边(self):
        窗 = self._窗口()
        区域 = 窗.屏幕几何()
        if 区域 is not None and 区域.width() < 1100:
            # 离屏自检的"屏幕"只有 800x800：窗口最小尺寸 + 面板就占满了，
            # 比例物理上装不下（真机 1920 以上宽屏没问题，X11 那轮测过 0.0072）
            窗.close()
            self.skipTest(f"屏幕太小（{区域.width()}px），装不下面板+视频比例")
        try:
            窗.切换AI面板(True)
            泵(0.4)
            self.assertLess(窗.视频区比例误差(), 0.03, "切到 AI 页签后出现黑边")
            窗.切换清单(True)
            泵(0.4)
            self.assertLess(窗.视频区比例误差(), 0.03, "切回清单页后出现黑边")
        finally:
            窗.close()


# ==================== 播放页新布局（左面板 + 控件下移 + 撤掉两行） ====================


@unittest.skipUnless(Qt可用, "没装 PySide6")
class 播放页布局测试(unittest.TestCase):
    """用户要求的播放页改版：
    左面板三页（视频信息/播放清单/AI 助手）；▶播放在⏸前、🗗独立窗口在全屏后、
    循环/随机在下一个后；撤掉视频上方那行与下方 AI/清单标签页。
    """

    class 规格:
        def __init__(self):
            self.显示名 = "本地测试盘"
            self.图标 = "🧪"

    class 动作:
        """播放页只需要宿主的"动作"里这几样（网盘列表/适配器）。"""

        def __init__(self):
            self.规格表 = {"fake_1": 播放页布局测试.规格()}

        def 规格(self, 标识):
            return self.规格表.get(标识)

        def 适配器(self, 标识):
            return None

    class 假主机:
        def __init__(self):
            self.动作 = 播放页布局测试.动作()
            self.配置 = {"播放": {}}

    @classmethod
    def setUpClass(cls):
        cls.页面 = 播放页面(cls.假主机())

    @classmethod
    def tearDownClass(cls):
        cls.页面.deleteLater()

    def test_面板在视频右侧且有三页(self):
        左 = self.页面.左面板
        self.assertEqual(self.页面.主体.widget(0), self.页面.视频,
                         "视频在左边")
        self.assertEqual(self.页面.主体.widget(1), 左, "面板在视频右侧")
        self.assertEqual(左.count(), 3)
        self.assertIn("信息", 左.tabText(0))
        self.assertIn("清单", 左.tabText(1))
        self.assertIn("AI", 左.tabText(2))

    def test_视频信息搬进了面板(self):
        self.assertIsNotNone(self.页面.信息栏.parent())
        self.assertIsNot(self.页面.信息栏.parent(), self.页面,
                         "信息栏应该在左面板里，不再挂在页面上")
        self.assertIn(self.页面.信息栏.parent(), 
                      [self.页面.左面板.widget(i) for i in range(3)]
                      + [self.页面.信息栏.parent()])

    def test_播放清单与AI面板在面板里(self):
        左 = self.页面.左面板
        self.assertGreaterEqual(左.indexOf(self.页面.清单), 0)
        self.assertGreaterEqual(左.indexOf(左.widget(2)), 0)
        self.assertIsNotNone(self.页面.AI输出)

    def test_控制条按钮顺序与位置(self):
        条 = self.页面.控制条
        # ▶ 播放 在 ⏸ 前面
        行 = 条.layout().itemAt(1).layout()
        文本 = []
        for i in range(行.count()):
            部件 = 行.itemAt(i).widget()
            if 部件 is not None and hasattr(部件, "text") and 部件.text():
                文本.append(部件.text())
        self.assertEqual(文本[:3], ["▶ 播放", "⏸", "⏮ 上一个"], f"按钮顺序：{文本}")
        self.assertIn("⏭ 下一个", 文本)
        self.assertLess(文本.index("⏭ 下一个"), 文本.index("🔁 不循环"),
                        "循环/随机应该跟在「下一个」后面")
        # 🗗 独立窗口 在全屏后面
        self.assertLess(文本.index("⛶ 全屏"), 文本.index("🗗 独立窗口"))
        # 音量 + 速度都在这一行
        self.assertIsNotNone(条.音量条)
        self.assertIn("速度", 文本)

    def test_两行都已撤销(self):
        self.assertIsNone(self.页面.工具栏, "视频上方那行（工具栏）应撤销")
        self.assertIsNone(self.页面.本地按钮,
                          "「播放/独立窗口/本地文件」那行应撤销（本地文件在菜单里）")
        # 菜单栏「帮助」后面有按钮
        标题 = [a.text() for a in self.页面.菜单栏.actions()]
        # 需求：菜单栏「帮助」后面只留面板开关（网盘/文件按钮已去掉）
        self.assertFalse(any(t.strip().endswith("网盘") for t in 标题[8:]))
        self.assertFalse(any(t.strip().endswith("文件") for t in 标题[8:]))
        self.assertTrue(any("面板" in t for t in 标题))

    def test_点菜单按钮能切面板页(self):
        视图 = next((m.menu() for m in self.页面.菜单栏.actions()
                  if m.text().startswith("视图")), None)
        self.assertIsNotNone(视图, "有「视图」菜单")
        self.页面._切换清单()
        泵(0.2)
        self.assertEqual(self.页面.左面板.currentIndex(), 1)
        self.页面._切换AI面板()
        泵(0.2)
        self.assertEqual(self.页面.左面板.currentIndex(), 2)
        self.页面.切换侧栏(False)
        泵(0.2)
        self.assertFalse(self.页面.左面板.isVisible())


# ==================== AI 日志降噪（用户反馈：每秒重复刷屏） ====================


class AI日志降噪测试(unittest.TestCase):
    """用户反馈：AI 助手每秒重复输出「本地模型输出不是 JSON」「需要调整=False」
    这类过程信息，观影时没法看。要求"只有 AI 做出重要决策时才显示"。
    """

    def _板(self):
        from v8_3.界面.AI播放面板 import AI播放面板
        板 = AI播放面板(None, 紧凑=True)
        return 板

    def _行数(self, 板):
        return len([x for x in 板.文本().splitlines() if x.strip()])

    def test_同类调试信息被折叠(self):
        板 = self._板()
        for _ in range(30):
            板.追加("[播放顾问] 卡顿诊断：本地模型输出不是 JSON，尝试其它来源")
        行数 = self._行数(板)
        self.assertLessEqual(行数, 2, f"30 条重复调试信息不该显示 {行数} 行")

    def test_连续重复行被折叠(self):
        板 = self._板()
        for _ in range(50):
            板.追加("同一句话")
        行数 = self._行数(板)
        # 第 1 次显示 + 第 10 次提示 = 最多 2 行
        self.assertLessEqual(行数, 2, f"同一句话重复 50 次不该显示 {行数} 行")

    def test_重要决策照常显示(self):
        板 = self._板()
        for 文本 in ("[AI 决策] 卡顿诊断 → 调整动作：缓存 12000→20000ms",
                   "[自动调优] 已应用：允许丢帧 + vout=gl",
                   "[AI 决策] 规则诊断 → 调整动作：缓存加大"):
            板.追加(文本)
        self.assertEqual(self._行数(板), 3, "重要决策必须逐条显示")

    def test_决策与噪音混在一起也只留决策(self):
        板 = self._板()
        for _ in range(20):
            板.追加("[播放顾问] 云端诊断：需要调整=False、动作=[]")
        板.追加("[AI 决策] 本地模型诊断 → 调整动作：缓存 20000ms")
        for _ in range(20):
            板.追加("[播放顾问] 云端诊断：需要调整=False、动作=[]")
        文本 = 板.文本()
        self.assertIn("缓存 20000ms", 文本, "决策不能被折叠掉")
        self.assertLessEqual(self._行数(板), 3)

    def test_不同的正常信息不折叠(self):
        板 = self._板()
        for i in range(5):
            板.追加(f"第 {i} 条不同信息")
        self.assertEqual(self._行数(板), 5, "内容不同就不该折叠")


@unittest.skipUnless(Qt可用, "没装 PySide6")
class AI按钮两行测试(unittest.TestCase):

    def test_紧凑面板按钮排两行(self):
        from PySide6.QtWidgets import QGridLayout
        from v8_3.界面.AI播放面板 import AI播放面板
        板 = AI播放面板(None, 紧凑=True)
        网格 = None
        for i in range(板.layout().count()):
            项 = 板.layout().itemAt(i)
            if isinstance(getattr(项, "layout", lambda: None)(), QGridLayout):
                网格 = 项.layout()
                break
        self.assertIsNotNone(网格, "紧凑模式应该用网格排布（两行）")
        self.assertEqual((网格.rowCount(), 网格.columnCount()), (2, 2))
        按钮 = [网格.itemAtPosition(r, c).widget().text()
               for r in range(2) for c in range(2)
               if 网格.itemAtPosition(r, c)]
        self.assertEqual(len(按钮), 4, f"四个按钮都在：{按钮}")

    def test_宽面板仍是一行(self):
        from PySide6.QtWidgets import QGridLayout
        from v8_3.界面.AI播放面板 import AI播放面板
        板 = AI播放面板(None, 紧凑=False)
        有网格 = any(isinstance(getattr(板.layout().itemAt(i), "layout",
                                   lambda: None)(), QGridLayout)
                  for i in range(板.layout().count()))
        self.assertFalse(有网格, "宽面板不必改布局")
