"""用 wine 在**真实 Windows 运行时**里跑一遍界面验证（重发前先跑它）。

用法（在 wine 里，用包里自带的 Windows Python）::

    WINEPREFIX=~/.cache/wineprefix-网盘 QT_QPA_PLATFORM=offscreen \
      wine 运行环境/python/python.exe 'Z:\\home\\xgl\\python\\网盘管理\\工具\\wine验证.py'

要点：
* ``sys.path`` 指向 Linux 侧的实时源码（wine 的 Z: 盘），验的是最新代码而不是旧安装包；
* 必须设 ``V8_3_不自动换解释器=1``，否则入口自举会想换成 Linux 解释器；
* 用 ``QT_QPA_PLATFORM=offscreen``：wine 的 GL 在渲染复杂界面时会段错误（实测过），
  而 offscreen 下 ``widget.grab()`` 拿到的仍是真实 Windows 渲染结果；
* 结果写 ``Z:\\tmp\\wine结果.json``（wine 控制台编码会把中文搞乱，别靠 stdout）。

注意：wine 里 Qt 找不到中文字体，截图上的汉字会显示成方框 —— 那是 wine 的字体映射问题，
不是程序问题（真实 Windows 上正常）。
"""
# -*- coding: utf-8 -*-
"""在 wine（真实 Windows 运行时）里跑**当前源码**的界面验证。

用包里的 Windows Python + PySide6，但 sys.path 指向 Linux 侧的实时项目（wine 的 Z: 盘），
这样验的是最新代码，而不是已经过期的安装包。
"""
import os, sys, json, time, tempfile

os.environ["V8_3_不自动换解释器"] = "1"          # 别让入口自举去换解释器
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
项目 = r"Z:\home\xgl\python\网盘管理"
sys.path.insert(0, 项目)

日志 = open(r"Z:\tmp\wine_verify.log", "w", encoding="utf-8")
def 记(文本):
    print(文本, flush=True)
    日志.write(str(文本) + "\n"); 日志.flush()

结果 = {}
from PySide6.QtWidgets import QApplication, QComboBox, QScrollArea, QLineEdit
from v8_3.配置 import 加载配置, 保存配置, 准备假网盘目录

app = QApplication([])
记("STEP app ok")
临时 = tempfile.TemporaryDirectory()
工作 = 临时.name
盘目录 = os.path.join(工作, "盘1")
准备假网盘目录(盘目录)
c = 加载配置(os.path.join(工作, "配置.json"))
c["适配器"] = [{"标识": "d1", "类型": "fake", "名称": "百度小号",
             "路径": 盘目录, "线程数": 12, "启用": True}]
c["数据库路径"] = os.path.join(工作, "t.db")
c["敏感词"] = {"启用": True, "自动改名上传": True,
           "数据库路径": os.path.join(工作, "s.db")}
c["AI"] = {"启用": False, "api密钥": ""}
保存配置(c, os.path.join(工作, "配置.json"))

from v8_3.界面.主窗口 import 主窗口
窗口 = 主窗口(dict(c), 配置路径=os.path.join(工作, "配置.json"))
窗口.resize(1280, 820); 窗口.show()
for _ in range(8): app.processEvents()
记("STEP 主窗口 ok")

t0 = time.time(); 窗口.切换到AI页()
for _ in range(6): app.processEvents()
结果["切AI页毫秒"] = round((time.time() - t0) * 1000)
页 = 窗口.AI页面()
结果["本地模型下拉项数"] = 页.本地模型框.count()
结果["下拉可选"] = bool(页.本地模型框.isEnabled())
结果["一键装模型按钮"] = bool(getattr(页, "一键按钮", None))
if 页.运行时 is not None:
    结果["时段摘要"] = 页.运行时.获取时段摘要()
    try:
        价 = 页.运行时.价格抓取器.获取价格("deepseek-flash") or {}
        结果["flash空闲"] = 价.get("空闲")
        结果["flash高峰"] = 价.get("高峰")
    except Exception as e:
        结果["价格读取失败"] = str(e)
记("STEP AI页 ok")

窗口.切换到传输页()
for _ in range(8): app.processEvents()
def 找区(页):
    return 页 if isinstance(页, QScrollArea) else 页.findChild(QScrollArea)
结果["宽窗横滑"] = 找区(窗口.堆叠.currentWidget()).horizontalScrollBar().maximum()
窗口.resize(900, 620)
for _ in range(10): app.processEvents()
结果["窄窗横滑"] = 找区(窗口.堆叠.currentWidget()).horizontalScrollBar().maximum()
表 = getattr(窗口.传输页面(), "任务表", None)
结果["表格最小宽"] = 表.minimumWidth() if 表 else -1
记("STEP 传输页 ok")

框 = QComboBox(); 框.addItems(["百度网盘", "夸克网盘"]); 框.setMinimumHeight(34)
框.resize(300, 34); 框.show()
for _ in range(4): app.processEvents()
图 = 框.grab().toImage(); 底 = 图.pixelColor(6, 6)
def 有墨(x):
    for y in range(2, 图.height() - 2):
        p = 图.pixelColor(x, y)
        if abs(p.red()-底.red()) + abs(p.green()-底.green()) + abs(p.blue()-底.blue()) > 60:
            return True
    return False
结果["箭头_输入区脏列"] = sum(1 for x in range(int(图.width()*0.42), int(图.width()*0.86), 2) if 有墨(x))
结果["箭头_按钮区有"] = any(有墨(x) for x in range(图.width()-22, 图.width()-4))
框.hide()
记("STEP 下拉框 ok")

from v8_3.界面.网盘对话框 import 网盘编辑对话框
弹窗 = 网盘编辑对话框(dict(c), None, 窗口)
弹窗.show()
for _ in range(5): app.processEvents()
结果["弹窗默认尺寸"] = f"{弹窗.width()}x{弹窗.height()}"
弹窗.resize(弹窗.minimumWidth(), 弹窗.minimumHeight())
for _ in range(5): app.processEvents()
区3 = 弹窗.findChild(QScrollArea)
结果["弹窗最小尺寸可竖滑"] = 区3.verticalScrollBar().maximum() if 区3 else -1
弹窗.close()
记("STEP 弹窗 ok")

框2 = QLineEdit()
结果["右键菜单首项"] = [a.text() for a in 框2.createStandardContextMenu().actions() if a.text()][:1]
结果["退出按钮"] = hasattr(窗口, "退出按钮")
结果["版本"] = __import__("v8_3").__version__
try:
    from v8_3.AI.DeepSeek价格抓取 import DeepSeek价格抓取器 as _抓
    结果["价格体检"] = _抓.价格是否可信(
        __import__("json").load(open(r"Z:\home\xgl\python\网盘管理\数据\DeepSeek价格.json",
                                     encoding="utf-8")))
except Exception as e:
    结果["价格体检"] = f"跳过：{e}"
记("STEP 杂项 ok")

图目录 = r"Z:\tmp\wine_shots"
os.makedirs(图目录, exist_ok=True)
窗口.resize(1280, 820); 窗口.切换到AI页()
for _ in range(6): app.processEvents()
ok1 = 窗口.grab().save(图目录 + r"\ai_page.png"); 记(f"SAVE ai={ok1}")
窗口.切换到传输页()
for _ in range(6): app.processEvents()
ok2 = 窗口.grab().save(图目录 + r"\transfer.png"); 记(f"SAVE transfer={ok2}")
窗口.resize(820, 600)
for _ in range(8): app.processEvents()
ok3 = 窗口.grab().save(图目录 + r"\transfer_narrow.png"); 记(f"SAVE narrow={ok3}")
记("STEP 截图 ok")

with open(r"Z:\tmp\wine结果.json", "w", encoding="utf-8") as f:
    json.dump(结果, f, ensure_ascii=False, indent=2)
记("DONE")
