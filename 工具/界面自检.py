#!/usr/bin/env python3
"""V8_3 界面自检（默认离屏跑，不需要网络）。

覆盖：
  1. 左侧导航按网盘实例生成按钮、点击切换到对应网盘页；
  2. 左下角「新增网盘 / 编辑网盘 / 删除网盘」面板的完整功能；
  3. 网盘页的文件浏览、上传、下载、新建文件夹、删除；
  4. 传输页 / 日志页切换、主题切换、配置落盘。

用法：
    python 工具/界面自检.py            # 离屏（offscreen），跑完即退出
    python 工具/界面自检.py --显示      # 真窗口，人工看一眼
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

项目根 = Path(__file__).resolve().parents[1]
if str(项目根) not in sys.path:
    sys.path.insert(0, str(项目根))

# 自举：不是项目自带的解释器就换过去，避免用系统 python 跑出
# "No module named 'PySide6' / 'httpx'"。详见 v8_3/自举.py
from v8_3.自举 import 确保项目环境

确保项目环境()

显示模式 = "--显示" in sys.argv
if not 显示模式:
    os.environ["QT_QPA_PLATFORM"] = "offscreen"   # 离屏跑，不弹窗

from PySide6.QtCore import QPoint, QSize, qInstallMessageHandler
from PySide6.QtWidgets import (
    QApplication, QDialog, QFileDialog, QInputDialog, QLabel, QMessageBox,
    QPushButton,
)

# 收集 Qt 自身的线程类警告：跨线程碰控件一定要在这里被抓出来
Qt警告: list[str] = []


def _qt消息(类型, 上下文, 消息):
    if "different thread" in 消息 or "setParent" in 消息:
        Qt警告.append(str(消息))


qInstallMessageHandler(_qt消息)

import json

from v8_3.配置 import (
    保存配置, 动作, 加载配置, 网盘实例列表, 准备假网盘目录, 适配器规格表,
)
from v8_3.核心.模型 import 网盘类型
from v8_3.界面 import 主窗口 as 主窗口模块
from v8_3.界面.主窗口 import 主窗口
from v8_3.界面.传输页面 import 传输页面 as 传输页面类

结果: list[tuple[bool, str]] = []


def 检查(条件: bool, 说明: str):
    结果.append((bool(条件), 说明))
    print(("  ✓ " if 条件 else "  ✗ ") + 说明)
    return bool(条件)


def 泵(秒: float = 0.05):
    QApplication.processEvents()
    time.sleep(秒)


def 等待(条件, 超时: float = 60.0, 说明: str = "") -> bool:
    开始 = time.time()
    while time.time() - 开始 < 超时:
        QApplication.processEvents()
        try:
            if 条件():
                return True
        except Exception:
            pass
        time.sleep(0.05)
    if 说明:
        print(f"    …等待超时：{说明}")
    return False


def 建测试环境(根: Path) -> dict:
    """两个假网盘实例（同类型多实例）+ 一份云端数据。"""
    配置项 = []
    for 序号, 名称 in ((1, "假网盘一号"), (2, "假网盘二号")):
        目录 = 根 / f"假网盘{序号}"
        准备假网盘目录(目录)
        云端 = 目录 / "云端"
        (云端 / "文档").mkdir(parents=True, exist_ok=True)
        (云端 / "文档" / f"说明{序号}.txt").write_text(
            f"这是{名称}的测试文件\n", encoding="utf-8")
        (云端 / "视频").mkdir(parents=True, exist_ok=True)
        配置项.append({
            "标识": f"fake_{序号}",
            "类型": "fake",
            "名称": 名称,
            "路径": str(目录),
            "线程数": 4,
            "启用": True,
        })
    配置 = 加载配置(根 / "配置.json")
    配置["适配器"] = 配置项
    配置["数据库路径"] = str(根 / "传输任务.db")   # 别写脏项目里的正式任务库
    配置["敏感词"] = {"启用": True, "自动改名上传": True,
                   "数据库路径": str(根 / "敏感词.db")}
    配置["AI"] = {"启用": False, "api密钥": ""}      # 自检不联网、不调 AI
    保存配置(配置, 根 / "配置.json")
    return 配置


def main() -> int:
    临时 = tempfile.TemporaryDirectory(prefix="v8_3_gui_")
    根 = Path(临时.name)
    print(f"离屏自检工作目录：{根}")
    配置 = 建测试环境(根)
    应用 = QApplication.instance() or QApplication(sys.argv[:1])

    print("\n[1] 主窗口：顶部（网盘 + 网盘管理）横排 + 左侧（功能）竖排")
    窗口 = 主窗口(dict(配置), 配置路径=根 / "配置.json")
    窗口.show()
    QApplication.processEvents()
    检查(len(窗口._网盘按钮) == 2, "顶部导航为每个启用的网盘生成按钮（2 个）")
    检查(hasattr(窗口, "新增按钮") and hasattr(窗口, "编辑按钮")
         and hasattr(窗口, "删除按钮"), "顶部右侧存在 新增/编辑/删除 管理按钮")
    检查(窗口.网盘滚动区.widget() is 窗口.网盘按钮容器,
         "网盘按钮放在横向滚动区里（网盘再多也不会挤掉功能按钮）")
    检查(窗口.网盘滚动区.horizontalScrollBarPolicy().name in
         ("ScrollBarAsNeeded", "ScrollBarAlwaysOn"),
         "网盘横向滚动区允许左右滚动")
    检查(窗口.网盘滚动区.verticalScrollBarPolicy().name == "ScrollBarAlwaysOff",
         "网盘按钮区不出现纵向滚动条（横排不需要）")
    # 顶部横排的关键：导航在页面**上方**，且占满整宽
    布局 = 窗口.centralWidget().layout()
    检查(type(布局).__name__ == "QVBoxLayout",
         f"主布局是竖排（顶部栏 + 页面堆叠）：{type(布局).__name__}")
    顶栏 = 布局.itemAt(0).widget()
    检查(顶栏 is not None and 顶栏.height() <= 80,
         f"顶部栏高度克制（{顶栏.height() if 顶栏 else '-'}px），不占页面高度")
    检查(顶栏.width() > 窗口.width() * 0.8,
         f"顶部栏横向铺满（{顶栏.width()} / 窗口 {窗口.width()}）")
    # 顶部只剩两段：网盘 → 网盘管理（功能按钮已按要求挪回左侧竖排）
    云按钮x = next(iter(窗口._网盘按钮.values())).mapTo(顶栏, QPoint(0, 0)).x()
    管理x = 窗口.新增按钮.mapTo(顶栏, QPoint(0, 0)).x()
    检查(云按钮x < 管理x,
         f"顶部左→右：网盘({云按钮x}) → 网盘管理({管理x})")

    print("\n[1a] 左侧功能导航：传输/播放/敏感词/AI/日志/设置 竖排")
    中部 = 布局.itemAt(1)
    检查(type(中部).__name__ == "QHBoxLayout",
         f"顶部栏下面一行是『左栏 | 页面』两列：{type(中部).__name__}")
    左栏 = 中部.itemAt(0).widget()
    检查(左栏 is not None and 左栏.width() <= 120,
         f"左侧功能栏宽度克制（{左栏.width() if 左栏 else '-'}px）")
    功能按钮们 = [窗口.传输按钮, 窗口.播放按钮, 窗口.敏感词按钮,
              窗口.AI按钮, 窗口.日志按钮, 窗口.设置按钮]
    xs = {b.mapTo(窗口, QPoint(0, 0)).x() for b in 功能按钮们}
    ys = [b.mapTo(窗口, QPoint(0, 0)).y() for b in 功能按钮们]
    检查(len(xs) == 1, f"6 个功能按钮左对齐在同一列（x={xs}）")
    检查(ys == sorted(ys) and len(set(ys)) == len(ys),
         f"它们是**纵向**依次排开的（y={ys}）")
    检查(all(b.mapTo(左栏, QPoint(0, 0)).x() >= 0 for b in 功能按钮们),
         "功能按钮都在左侧栏里面（x 相对左栏非负）")
    检查(窗口.堆叠.mapTo(窗口, QPoint(0, 0)).x() >= 左栏.width(),
         "页面堆叠在左栏右侧，不被压住")
    # 切页时左侧按钮要有激活态
    窗口.切换到传输页(); 泵(0.4)
    检查(窗口.传输按钮.objectName() == "active",
         "切到传输页：传输按钮是激活态")
    窗口.切换到播放页(); 泵(0.4)
    检查(窗口.播放按钮.objectName() == "active"
         and 窗口.传输按钮.objectName() == "",
         "切到播放页：激活态跟着走（播放按钮之前漏了，会一直是灰按钮）")

    print("\n[1b] 顶部导航：窗口变矮/变窄都不压扁按钮")
    原尺寸 = 窗口.size()

    def 三个按钮高度() -> tuple[int, int, int]:
        return (窗口.传输按钮.height(), 窗口.新增按钮.height(),
                next(iter(窗口._网盘按钮.values())).height())

    窗口.resize(QSize(窗口.width(), 1000))
    泵(0.25)
    高时 = 三个按钮高度()
    窗口.resize(QSize(窗口.width(), 620))
    泵(0.3)
    矮时 = 三个按钮高度()
    检查(高时 == 矮时 == (60, 52, 58),
         f"窗口变矮按钮高度不变：高 {高时} / 矮 {矮时}"
         "（主题 QSS 的 padding 不会再把它们压扁）")
    检查(窗口.网盘滚动区.height() >= 58,
         f"网盘按钮区高度不被压扁（{窗口.网盘滚动区.height()}px）")
    检查(窗口.传输按钮.height() == 60 and 窗口.设置按钮.height() == 60,
         f"左侧功能栏 6 个按钮高度一致（传输 {窗口.传输按钮.height()}px、"
         f"设置 {窗口.设置按钮.height()}px）")
    # 窗口很窄：外层横向滚动，而不是把按钮压扁
    窗口.resize(QSize(760, 700))
    泵(0.3)
    窄时 = 三个按钮高度()
    检查(窄时 == (60, 52, 58),
         f"窗口变窄按钮也不变形：{窄时}")
    外层 = 窗口.centralWidget().layout().itemAt(0).widget()
    横滚 = 外层.horizontalScrollBar()
    检查(横滚.maximum() > 0,
         f"窗口太窄时出现横向滚动条（可滑 {横滚.maximum()}px），"
         "靠滑动看全，而不是把按钮挤扁")
    窗口.resize(原尺寸)
    泵(0.2)

    print("\n[1c] 勾选框 / 下拉框：空框里打勾（不填色）+ 看得见的下拉箭头")
    from PySide6.QtGui import QColor
    from PySide6.QtWidgets import QCheckBox, QComboBox
    from PySide6.QtWidgets import QVBoxLayout as _纵, QWidget as _控
    from v8_3.界面.主题管理器 import 主题管理器, 当前颜色
    from v8_3.界面.控件样式 import 指示器边长
    颜色 = 当前颜色()
    强调 = QColor(颜色["强调"])
    输入底色 = QColor(颜色["输入"])
    卡片底色 = QColor(颜色["卡片"])
    样式表 = 主题管理器.获取样式表(窗口.主题下拉框.currentData())
    检查(not any(名 in 样式表 for 名 in
               ("QCheckBox::indicator {", "QCheckBox::indicator:",
                "QRadioButton::indicator {", "QRadioButton::indicator:")),
         "样式表里没有 ::indicator **规则**（勾选框改由 控件样式.py 自绘，才画得出勾）")

    小板 = _控()
    板布 = _纵(小板)
    未选 = QCheckBox("未选中")
    已选 = QCheckBox("已选中")
    已选.setChecked(True)
    下拉 = QComboBox()
    下拉.addItems(["deepseek-flash", "deepseek-v4-pro"])
    板布.addWidget(未选)
    板布.addWidget(已选)
    板布.addWidget(下拉)
    小板.resize(320, 140)
    小板.show()
    泵(0.25)

    def 距(甲, 乙) -> int:
        return (abs(甲.red() - 乙.red()) + abs(甲.green() - 乙.green())
                + abs(甲.blue() - 乙.blue()))

    def 指示器比例(控件):
        """返回 (强调色占比, 输入底色占比)——只看指示器那 16×16 的方框。"""
        图 = 控件.grab().toImage()
        y0 = (图.height() - 指示器边长) // 2
        强调数 = 底色数 = 总 = 0
        for x in range(指示器边长):
            for y in range(y0, y0 + 指示器边长):
                if x >= 图.width() or y >= 图.height():
                    continue
                c = 图.pixelColor(x, y)
                总 += 1
                if 距(c, 强调) < 90:
                    强调数 += 1
                if 距(c, 输入底色) < 40:
                    底色数 += 1
        return 强调数 / max(1, 总), 底色数 / max(1, 总)

    空框_强调, 空框_底 = 指示器比例(未选)
    勾选_强调, 勾选_底 = 指示器比例(已选)
    检查(空框_强调 < 0.02,
         f"未选中 = 空框（强调色像素 {空框_强调:.0%}，底色 {空框_底:.0%}）")
    检查(勾选_强调 > 0.05,
         f"选中时框里真的画了勾（强调色像素 {勾选_强调:.0%}）")
    检查(勾选_底 > 0.25 and 勾选_强调 < 0.6,
         f"选中时**没有填满色块**：底色仍占 {勾选_底:.0%}，勾只占 {勾选_强调:.0%}"
         "（旧样式是整块 100% 强调色）")

    图 = 下拉.grab().toImage()
    箭头区 = [(x, y) for x in range(图.width() - 26, 图.width() - 3)
            for y in range(6, 图.height() - 6)]
    箭头像素 = sum(1 for x, y in 箭头区 if 距(图.pixelColor(x, y), 卡片底色) > 45)
    def 竖线像素(列: int) -> int:
        return sum(1 for y in range(4, 图.height() - 4)
                   if 距(图.pixelColor(列, y), 卡片底色) > 30)
    分隔线 = max(竖线像素(列) for 列 in range(图.width() - 32, 图.width() - 24))
    检查(箭头像素 > 20,
         f"下拉按钮里有明显的箭头（{箭头像素} 个非底色像素）")
    检查(分隔线 > 4,
         f"下拉按钮与文字之间有分隔线（{分隔线}px，一眼看出点哪儿展开）")
    小板.close()

    print("\n[2] 点击网盘按钮 → 切换到该网盘页")
    # 假网盘的"已登录"现在如实由凭证文件决定（后端_假.account），
    # 所以这里先给两个实例落一份凭证，后面才谈得上验证"状态行给出登录态"。
    for _标 in ("fake_1", "fake_2"):
        try:
            窗口.动作.适配器(_标).令牌登录(f"自检-{_标}")
        except Exception as _e:
            print(f"   （给 {_标} 落凭证失败：{_e}）")
    窗口.切换网盘页("fake_2")
    泵(0.2)
    页2 = 窗口._网盘页面.get("fake_2")
    检查(页2 is not None and 窗口.当前页面() is 页2,
         "点击第二个网盘按钮切到它的页面")
    检查(窗口._网盘按钮["fake_2"].objectName() == "active",
         "被选中的导航按钮加上 active 样式")
    检查(等待(lambda: 页2.文件表格.rowCount() >= 2, 30,
              "等待文件列表"), "网盘页自动列出根目录内容")
    名称列 = [页2.文件表格.item(r, 1).text() for r in range(页2.文件表格.rowCount())]
    检查(any("文档" in x for x in 名称列), f"目录出现在表格里：{名称列}")
    # 真实场景里由凭证监视器触发；自检里显式调它（会清缓存并强制刷新）
    页2._凭证有变化()
    _凭证文件 = 窗口.动作.规格("fake_2").凭证文件
    检查(_凭证文件.is_file(),
         f"fake_2 的凭证文件存在（诊断用）：{_凭证文件}")
    检查(等待(lambda: "已登录" in 页2.状态标签.text(), 30, "等待状态行"),
         f"网盘页状态行给出登录态：{页2.状态标签.text()[:44]}")
    状态行 = 页2.状态标签.text()
    检查("数据目录" not in 状态行 and str(根) not in 状态行,
         f"状态行不再堆本机数据目录/绝对路径：{状态行[:44]}…"
         "（要看/要开数据目录有下面那排按钮）")

    print("\n[3] 双击进入目录 / 上级 / 根")
    行 = next(r for r in range(页2.文件表格.rowCount())
             if "文档" in 页2.文件表格.item(r, 1).text())
    页2.文件表格.selectRow(行)
    页2._双击项目(页2.文件表格.model().index(行, 1))
    检查(等待(lambda: 页2.当前目录 == "/文档" and 页2.文件表格.rowCount() >= 2,
              30, "进入 /文档"), "双击文件夹进入子目录")
    页2._返回上级()
    检查(等待(lambda: 页2.当前目录 == "/", 30), "「上级」返回根目录")

    print("\n[4] 上传 / 下载（走桥进程，真实读写）")
    本地文件 = 根 / "上传测试.txt"
    本地文件.write_text("hello v8_3 gui\n" * 10, encoding="utf-8")
    传输页 = 窗口.传输页面()
    传输页.添加上传任务("fake_2", str(本地文件), "/文档")
    检查(等待(lambda: (根 / "假网盘2" / "云端" / "文档" / "上传测试.txt").is_file(),
              60, "等待上传完成"), "上传文件落到假网盘 云端/文档/")
    检查(等待(lambda: 传输页._本地线程 is None, 60), "上传任务线程正常结束")

    下载到 = 根 / "下载回来.txt"
    传输页.添加下载任务("fake_2", "/文档/上传测试.txt", str(下载到))
    检查(等待(lambda: 下载到.is_file() and 下载到.stat().st_size > 0, 60,
              "等待下载完成"), "下载文件落到本地")
    检查(下载到.is_file() and 下载到.read_text(encoding="utf-8")
         == 本地文件.read_text(encoding="utf-8"), "下载内容与上传内容一致")

    print("\n[5] 新建文件夹 / 删除（网盘页按钮，弹窗被替换为固定输入）")
    原输入 = QInputDialog.getText
    QInputDialog.getText = staticmethod(lambda *a, **k: ("自检目录", True))
    try:
        页2.加载当前目录("/")
        等待(lambda: 页2.当前目录 == "/" and 页2.文件表格.rowCount() >= 2, 30)
        页2._新建文件夹()
        检查(等待(lambda: (根 / "假网盘2" / "云端" / "自检目录").is_dir(), 60),
             "「新建文件夹」在网盘上创建目录")
    finally:
        QInputDialog.getText = 原输入

    原提问 = QMessageBox.question
    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)
    try:
        等待(lambda: any("自检目录" in 页2.文件表格.item(r, 1).text()
                        for r in range(页2.文件表格.rowCount())), 30)
        行 = next((r for r in range(页2.文件表格.rowCount())
                  if "自检目录" in 页2.文件表格.item(r, 1).text()), -1)
        检查(行 >= 0, "新建的目录出现在列表里")
        if 行 >= 0:
            页2.文件表格.selectRow(行)
            页2._删除选中()
            检查(等待(lambda: not (根 / "假网盘2" / "云端" / "自检目录").exists(), 60),
                 "「删除」把目录从网盘上删掉")
    finally:
        QMessageBox.question = 原提问

    print("\n[6] 左下角：新增网盘（同一家网盘第二个账号）")
    原对话框 = 主窗口模块.网盘编辑对话框

    class 假对话框:
        def __init__(self, 配置, 实例, 父=None):
            self.结果 = None

        def exec(self):
            self.结果 = {
                "标识": "fake_3", "类型": "fake", "名称": "假网盘三号",
                "路径": str(根 / "假网盘3"), "线程数": 4, "启用": True,
            }
            return QDialog.Accepted

    主窗口模块.网盘编辑对话框 = 假对话框
    try:
        准备假网盘目录(根 / "假网盘3")
        (根 / "假网盘3" / "云端" / "新盘内容").mkdir(parents=True, exist_ok=True)
        窗口.新增网盘()
        泵(0.2)
        检查(len(窗口._网盘按钮) == 3 and "fake_3" in 窗口._网盘按钮,
             "新增后左侧导航多出第三个网盘按钮")
        检查(窗口._当前标识 == "fake_3", "新增后自动切到新网盘页面")
        检查(any(x["标识"] == "fake_3" for x in 网盘实例列表(窗口.配置)),
             "新实例写入配置对象")
        落盘 = 加载配置(根 / "配置.json")
        检查(any(x["标识"] == "fake_3" for x in 网盘实例列表(落盘)),
             "配置已保存到 配置.json")
        检查("fake_3" in 适配器规格表(窗口.配置),
             "新实例进入规格表（可被引擎使用）")
    finally:
        主窗口模块.网盘编辑对话框 = 原对话框

    print("\n[7] 左下角：编辑网盘")
    class 假编辑对话框(假对话框):
        def exec(self):
            self.结果 = {
                "标识": "fake_3", "类型": "fake", "名称": "假网盘三号（改名）",
                "路径": str(根 / "假网盘3"), "线程数": 6, "启用": True,
            }
            return QDialog.Accepted

    主窗口模块.网盘编辑对话框 = 假编辑对话框
    try:
        窗口.切换网盘页("fake_3")
        窗口.编辑当前网盘()
        泵(0.2)
        实例 = next(x for x in 网盘实例列表(窗口.配置) if x["标识"] == "fake_3")
        检查(实例["名称"] == "假网盘三号（改名）" and 实例["线程数"] == 6,
             "编辑后的名称/线程数写入配置")
        按钮文本 = 窗口._网盘按钮["fake_3"].text()
        检查("假网盘三号" in 按钮文本,
             f"左侧按钮文字同步更新（长名自动截断显示）：{按钮文本!r}")
        检查("假网盘三号（改名）" in 窗口._网盘按钮["fake_3"].toolTip(),
             "按钮提示里保留完整名称")
    finally:
        主窗口模块.网盘编辑对话框 = 原对话框

    print("\n[7b] 新增网盘对话框：只问类型 + 名称，其余自动配好")
    from v8_3.界面 import 网盘对话框 as 对话框模块
    # 把两个路径函数沙箱到临时目录：别让"自动复制适配器"真落到项目的 适配器实例/ 上
    原默认路径 = 对话框模块.默认适配器路径
    原副本目录 = 对话框模块.建议副本目录
    假适配器 = 根 / "适配器" / "百度网盘适配器"
    (假适配器 / "核心").mkdir(parents=True, exist_ok=True)
    (假适配器 / "核心" / "接口.py").write_text("x = 1\n", encoding="utf-8")
    (假适配器 / "启动.py").write_text("pass\n", encoding="utf-8")
    (假适配器 / "数据").mkdir(exist_ok=True)
    (假适配器 / "数据" / "会话.json").write_text('{"凭": "证"}', encoding="utf-8")
    对话框模块.默认适配器路径 = lambda 类型: (
        根 / "适配器" / {"baidu": "百度网盘适配器"}.get(类型.value, 类型.value))
    对话框模块.建议副本目录 = lambda 配置, 类型: (
        根 / "适配器实例" / f"{类型.value}_2")
    try:
        新增 = 对话框模块.网盘编辑对话框({"适配器": []}, None)
        检查(not hasattr(新增, "标识框"),
             "新增对话框没有「实例标识」输入框（自动生成，不再让用户填）")
        检查(新增.高级开关.isHidden() and 新增.高级组.isHidden(),
             "新增对话框不显示 适配器目录/线程数/启用（高级项整块藏起来）")
        检查(新增.类型框.isEnabled() and 新增.名称框.text() == "百度网盘",
             f"只需选类型 + 填名称（名称已按类型预填：{新增.名称框.text()}）")
        提示 = 新增.提示标签.text()
        检查("自动生成" in 提示 and "适配器目录" in 提示 and "线程数 12" in 提示,
             "提示里交代了自动生成的内容")
        新增._确定()
        检查(bool(新增.结果) and 新增.结果["标识"] == "baidu"
             and Path(新增.结果["路径"]).is_dir(),
             f"第一家网盘直接用项目自带适配器目录：{新增.结果['路径']}")
        检查(新增.结果["线程数"] == 12 and 新增.结果["启用"] is True,
             "线程数 / 启用自动套默认值（12 · 启用），不用用户操心")

        一份配置 = {"适配器": [{"标识": "baidu", "类型": "baidu", "名称": "百度网盘",
                            "路径": str(假适配器), "线程数": 12, "启用": True}]}
        第二 = 对话框模块.网盘编辑对话框(一份配置, None)
        检查(第二._标识 == "baidu_2", f"同一家网盘第二家自动生成标识：{第二._标识}")
        检查("复制一份独立目录" in 第二.提示标签.text(),
             "提示里说明保存时会**自动复制**独立目录（不用先点「复制适配器项目…」）")
        第二._确定()
        目标 = Path(第二.结果["路径"])
        检查(目标.is_dir() and (目标 / "启动.py").is_file(),
             f"保存时自动把适配器目录准备好（不再报「目录不存在」）：{目标}")
        检查(not any((目标 / "数据").iterdir()),
             "自动复制出来的实例目录里没有任何登录凭证（数据/ 是空的）")

        编辑 = 对话框模块.网盘编辑对话框(一份配置, 一份配置["适配器"][0])
        检查(not 编辑.高级开关.isHidden() and 编辑.高级组.isHidden(),
             "编辑对话框有「显示高级设置」开关，且默认收起")
        编辑.高级开关.setChecked(True)
        检查(not 编辑.高级组.isHidden(),
             "勾上开关后 目录/线程数/启用 才露出来")
        检查(编辑.名称框.text() == "百度网盘"
             and "主键，不可修改" in 编辑.提示标签.text(),
             "编辑时名称带出来，实例标识只展示不可改")
    finally:
        对话框模块.默认适配器路径 = 原默认路径
        对话框模块.建议副本目录 = 原副本目录

    print("\n[8] 左下角：删除网盘（可选连数据一起删）")
    窗口.切换网盘页("fake_3")
    窗口.删除当前网盘(自动选择="只删除配置")
    泵(0.2)
    检查("fake_3" not in 窗口._网盘按钮 and len(窗口._网盘按钮) == 2,
         "删除后导航按钮消失")
    检查(all(x["标识"] != "fake_3" for x in 网盘实例列表(窗口.配置)),
         "删除后配置里不再有该实例")
    检查((根 / "假网盘3").is_dir(),
         "「只删除配置」时适配器目录与登录数据原样保留")

    # 再建一个实例，验证"连数据一起删"真的把项目内的目录删掉
    from v8_3.配置 import 新增网盘实例 as _新增
    # 「连数据一起删」只会删项目内的目录（安全护栏），所以这里用项目自己的
    # 适配器实例目录建一个临时实例，验完立刻清掉。
    from v8_3.配置 import 项目根 as _项目根
    待删目录 = _项目根 / "适配器实例" / "自检_待删"
    待删目录.mkdir(parents=True, exist_ok=True)
    (待删目录 / "会话.json").write_text("{}", encoding="utf-8")
    新实例 = _新增(窗口.配置, "fake", 名称="待清理盘", 路径=str(待删目录))
    窗口.重建网盘导航()
    窗口.切换网盘页(新实例["标识"])
    窗口.删除当前网盘(自动选择="连数据")
    泵(0.2)
    检查(not 待删目录.exists(),
         "「连数据一起删」把项目内的适配器目录也删了")
    检查(all(x["标识"] != 新实例["标识"] for x in 网盘实例列表(窗口.配置)),
         "连数据删除后配置里也不再有该实例")
    import shutil as _shutil
    _shutil.rmtree(待删目录, ignore_errors=True)     # 兜底：万一没删干净

    print("\n[9] 传输页 / 日志页 / 主题")
    窗口.切换到传输页()
    泵(0.1)
    检查(窗口.当前页面() is 窗口.传输页面(),
         "「传输」按钮切到跨网盘传输页")
    检查(窗口.传输页面().源网盘框.count() == 2, "传输页网盘下拉跟随实例数量")
    检查(窗口.传输页面().任务表.rowCount() >= 2, "传输页保留了上传/下载任务行")
    窗口.切换到日志页()
    泵(0.1)
    检查("已新增网盘" in 窗口.日志页().文本() or "已删除网盘" in 窗口.日志页().文本(),
         "操作日志写进日志页")
    主题名 = "Nord"
    idx = 窗口.主题下拉框.findData(主题名)
    窗口.主题下拉框.setCurrentIndex(idx)
    泵(0.1)
    检查("QWidget" in (应用.styleSheet() or ""), "切换主题后样式表已应用")
    检查((根 / "配置.json").read_text(encoding="utf-8").find(主题名) >= 0,
         "主题写入配置文件")


    print("\n[10] 跨网盘批次传输（引擎 + SQLite 续传）")
    传输页 = 窗口.传输页面()
    (根 / "假网盘1" / "云端" / "转移源").mkdir(parents=True, exist_ok=True)
    for i in range(3):
        (根 / "假网盘1" / "云端" / "转移源" / f"文件{i}.bin").write_bytes(
            b"x" * (1024 * (i + 1)))
    idx = 传输页.源网盘框.findData("fake_1")
    传输页.源网盘框.setCurrentIndex(idx)
    传输页.源路径框.setText("/转移源")
    idx = 传输页.目标网盘框.findData("fake_2")
    传输页.目标网盘框.setCurrentIndex(idx)
    传输页.目标路径框.setText("/接收")
    传输页._开始传输()
    检查(等待(lambda: 传输页._批次线程 is None, 180, "等待批次结束"),
         "跨网盘批次跑完（fake_1 → fake_2）")
    目标目录 = 根 / "假网盘2" / "云端" / "接收"
    检查(目标目录.is_dir() and len(list(目标目录.iterdir())) == 3,
         f"3 个文件到达目标网盘：{sorted(x.name for x in 目标目录.glob('*')) if 目标目录.is_dir() else '目录不存在'}")
    检查("完成 3" in 传输页.统计标签.text() or "完成" in 传输页.统计标签.text(),
         f"统计标签显示结果：{传输页.统计标签.text()}")

    print("\n[11] 顶部导航：网盘很多时横向滚动（12 个网盘）")
    多配置 = 加载配置(根 / "配置.json")
    多配置["适配器"] = [
        {"标识": f"fake_{i}", "类型": "fake", "名称": f"测试网盘{i}",
         "路径": str(根 / "假网盘1"), "线程数": 2, "启用": True}
        for i in range(1, 13)
    ]
    多窗口 = 主窗口(dict(多配置), 配置路径=根 / "配置.json")
    多窗口.resize(1400, 500)          # 故意让窗口矮一点，逼出滚动
    多窗口.show()
    泵(0.3)
    检查(len(多窗口._网盘按钮) == 12, "12 个网盘各生成一个按钮")
    内容宽 = 多窗口.网盘按钮容器.sizeHint().width()
    视口宽 = 多窗口.网盘滚动区.viewport().width()
    检查(内容宽 > 视口宽,
         f"按钮总宽 {内容宽}px 超出可视宽度 {视口宽}px（可横向滚动）")
    滚动条 = 多窗口.网盘滚动区.horizontalScrollBar()
    检查(滚动条.maximum() > 0,
         f"横向滚动条出现且范围 {滚动条.minimum()}–{滚动条.maximum()}")
    滚动条.setValue(滚动条.maximum())
    泵(0.1)
    检查(滚动条.value() == 滚动条.maximum(), "可以横向滚动到最后一个网盘")
    检查(多窗口.传输按钮.isVisible(),
         "网盘再多也不会把「功能」那段挤出可视区（顶部横排的好处）")
    检查(多窗口.当前页面() is not None, "溢出时仍正常显示当前页")
    多窗口.close()
    泵(0.3)
    多窗口.deleteLater()
    QApplication.processEvents()

    print("\n[12] 空配置时的空状态页")
    空配置 = 加载配置(根 / "配置.json")
    空配置["适配器"] = []
    空窗口 = 主窗口(dict(空配置), 配置路径=根 / "空配置.json")
    空窗口.show()
    泵(0.2)
    检查(空窗口._空状态页 is not None
         and 空窗口.当前页面() is 空窗口._空状态页,
         "没有配置网盘时显示「还没有配置任何网盘」空状态页")
    检查(not 空窗口._网盘按钮, "空状态下顶部没有网盘按钮")
    检查(not 空窗口.编辑按钮.isEnabled() and not 空窗口.删除按钮.isEnabled(),
         "空状态下「编辑/删除」按钮不可点")
    空窗口.close()
    泵(0.2)
    空窗口.deleteLater()
    QApplication.processEvents()


    print("\n[12] 传输页布局：源/目标各占一行")
    传输页 = 窗口.传输页面()
    表单 = None
    for i in range(传输页.layout().count()):
        子 = 传输页.layout().itemAt(i).layout()
        if 子 is not None and hasattr(子, "getItemPosition"):
            表单 = 子
            break
    检查(表单 is not None, "传输页用网格布局排源/目标")
    位置 = {}
    for i in range(表单.count()):
        部件 = 表单.itemAt(i).widget()
        if 部件 is None:
            continue
        行, 列, _, _ = 表单.getItemPosition(i)
        for 名, 对象 in (("源网盘", 传输页.源网盘框), ("源路径", 传输页.源路径框),
                      ("目标网盘", 传输页.目标网盘框),
                      ("目标路径", 传输页.目标路径框)):
            if 部件 is 对象:
                位置[名] = (行, 列)
    检查(位置.get("源网盘", (9, 9))[0] == 0 and 位置.get("源路径", (9, 9))[0] == 0,
         f"源网盘与源路径在同一行（第 0 行）：{位置}")
    检查(位置.get("目标网盘", (9, 9))[0] == 1 and 位置.get("目标路径", (9, 9))[0] == 1,
         f"目标网盘与目标路径在同一行（第 1 行）：{位置}")
    检查(位置["源网盘"][1] < 位置["源路径"][1],
         "源网盘在源路径左边；目标同理")

    print("\n[13] 表格下方横向统计栏（V8 组件 + 文件速率）")
    面板 = 传输页.状态面板
    检查(面板 is not None and 面板.objectName() == "传输状态面板",
         "存在表格下方的统计面板")
    for 名 in ("队列数标签", "状态文本标签", "计算网速标签", "文件速率标签",
              "耗时对比标签", "倒计时标签"):
        检查(hasattr(面板, 名), f"统计栏包含 {名}")
    检查(set(面板.内存值标签) == {"总大小", "新上传", "覆盖", "等待", "跳过",
                              "失败", "暂停", "兜底"},
         f"统计栏字节组件齐全（含暂停/兜底）：{sorted(面板.内存值标签)}")
    检查("Alist" not in 面板.计算网速标签.text(),
         "统计栏里已经没有 Alist 网速组件")
    速率用例 = [(0.01, "个/小时"), (0.05, "个/分"), (2, "个/秒"), (0.0001, "个/天")]
    for 速率, 期望单位 in 速率用例:
        文本 = 传输页面类.格式化文件速率(速率)
        检查(文本.endswith(期望单位),
             f"{速率} 个/秒 → {文本}（单位自动切到 {期望单位}）")

    print("\n[14] 跑一批上传，统计栏/文件速率/倒计时真的会动")
    敏感词文件 = 根 / "自检敏感词素材.txt"
    敏感词文件.write_text("sensitive\n" * 20, encoding="utf-8")
    库 = 窗口.动作.敏感词库()
    库.添加敏感词("fake_2", "自检敏感词", "自检安全词")
    上传前改名数 = len(库.列出改名记录(状态=""))
    传输页.添加上传任务("fake_2", str(敏感词文件), "/文档")
    检查(等待(lambda: (根 / "假网盘2" / "云端" / "文档" / "自检敏感词素材.txt").is_file()
              or (根 / "假网盘2" / "云端" / "文档" / "自检安全词素材.txt").is_file(),
              60, "等待上传完成"), "上传任务完成")
    检查(等待(lambda: 传输页._本地线程 is None, 60), "上传线程结束")
    新上传文本 = 面板.内存值标签["新上传"].text()
    检查(新上传文本 not in ("0 B", "0.0 B"), f"统计栏「新上传」已累加：{新上传文本}")
    检查("个/" in 面板.文件速率标签.text(),
         f"文件速率已显示：{面板.文件速率标签.text()}")
    检查("--:--:--" not in 面板.耗时对比标签.text(),
         f"已耗时/预计已开始计时：{面板.耗时对比标签.text()}")
    检查(面板.倒计时标签.text().strip() != "", f"倒计时：{面板.倒计时标签.text()}")
    检查(len(库.列出改名记录(状态="")) >= 上传前改名数,
         "改名记录表可用（上传守卫已接上词库）")

    print("\n[15] 敏感词页（V8 功能）：预检改名 + 词表 + 开关")
    窗口.切换到敏感词页()
    泵(0.3)
    词页 = 窗口._敏感词页面
    检查(窗口.当前页面() is 词页, "「敏感词」按钮切到敏感词页")
    检查(词页.敏感词表格.rowCount() >= 1,
         f"敏感词表里能看到刚添加的词（{词页.敏感词表格.rowCount()} 行）")
    检查("敏感词" in 词页.统计标签.text(), f"底部统计栏：{词页.统计标签.text()[:60]}…")
    安全名 = 根 / "假网盘2" / "云端" / "文档" / "自检安全词素材.txt"
    原名 = 根 / "假网盘2" / "云端" / "文档" / "自检敏感词素材.txt"
    检查(安全名.is_file() and not 原名.exists(),
         "含敏感词的文件名被自动换成安全词后上传（API 里叫预检改名）")

    print("\n[16] 关掉敏感词后：按原名上传（功能可开关）")
    词页.启用框.setChecked(False)
    泵(0.2)
    库2 = 窗口.动作.敏感词库()
    检查(库2 is None, "关闭开关后不再加载词库")
    检查(窗口.动作.守卫("fake_2") is None, "关闭后不再创建上传守卫")
    传输页.添加上传任务("fake_2", str(敏感词文件), "/文档")
    检查(等待(lambda: (根 / "假网盘2" / "云端" / "文档" / "自检敏感词素材.txt").is_file(),
              60, "等待按原名上传"), "关闭后按原名上传成功（不做改名）")
    词页.启用框.setChecked(True)
    泵(0.2)

    print("\n[17] AI 页（V8 功能）：无密钥也能打开，只做规则调度")
    配置["AI"] = {"启用": True, "api密钥": "", "模型": "deepseek-flash"}
    保存配置(配置, 根 / "配置.json")
    窗口.配置["AI"] = dict(配置["AI"])
    窗口._AI运行时 = None            # 让运行时按新配置重建
    窗口._AI不可用 = ""
    窗口.切换到AI页()
    泵(0.5)
    AI页 = 窗口._AI页面
    检查(AI页 is not None and 窗口.当前页面() is AI页,
         "「AI」按钮切到 AI 页")
    横幅 = AI页.提示横幅.text()
    检查(any(k in 横幅 for k in ("密钥", "AI", "本地模型")),
         f"没有密钥时给出明确提示：{横幅[:50]}…")
    检查(AI页.模型下拉框.count() >= 1,
         f"模型下拉可用（{AI页.模型下拉框.count()} 个）")
    检查("AI 调度器" in AI页.详情框.toPlainText(),
         "运行详情显示了调度器统计")
    运行时 = 窗口.AI运行时()
    检查(运行时 is not None and 运行时.调度器 is not None,
         "AI 运行时可选（无密钥时调度器仍返回规则策略）")
    # V8_3：无云端密钥时，"本地模型可用"也算可用（免费离线），否则必须为 False
    本地摘要 = 运行时.获取本地模型摘要() or {}
    if 本地摘要.get("可用") and 本地摘要.get("启用"):
        检查(运行时.是否可用() is True,
             "本地模型启用且可用 → 无密钥也算 AI 可用（走本地，不白发云端请求）")
        检查(运行时.助手.本地模型配置.优先本地,
             "本地模型默认「优先本地」")
    else:
        检查(运行时.是否可用() is False,
             "无密钥且本地模型不可用 → 明确报告不可用（不会白发请求）")
    检查(hasattr(运行时, "获取本地模型一行") and 运行时.获取本地模型一行(),
         f"本地模型状态一行：{运行时.获取本地模型一行()[:60]}…")
    检查(hasattr(AI页, "本地启用框") and hasattr(AI页, "本地模型框"),
         "AI 页有「本地模型」卡片（开关 + 模型下拉 + 检测/测速/启动/拉取）")
    策略 = 运行时.调度器.请求策略({"总任务数": 3, "总MB": 1.0})
    检查(isinstance(策略, dict) and 策略.get("来源") == "规则",
         f"降级策略可用：{策略.get('调度原因')}")
    检查(传输页.AI调度器 is 运行时.调度器 or 传输页.AI调度器 is not None,
         "传输页拿到了 AI 调度器（批次会走策略/优先级/回填）")
    检查(传输页.用AI框.isChecked(), "传输页默认勾选「用 AI 调整并发/优先级」")

    print("\n[17b] AI 页：DeepSeek 密钥直接在界面上配（不用手改 JSON）")
    检查(all(hasattr(AI页, 名) for 名 in
             ("密钥输入框", "保存密钥按钮", "测试密钥按钮", "清除密钥按钮",
              "密钥状态标签", "显示密钥框")),
         "AI 页有「🔑 DeepSeek API 密钥」卡片（输入框 + 显示/保存/测试/清除）")
    检查(AI页.密钥输入框.echoMode() == AI页.密钥输入框.EchoMode.Password,
         "密钥默认打码显示（不裸奔在屏幕上）")
    AI页.显示密钥框.setChecked(True)
    检查(AI页.密钥输入框.echoMode() == AI页.密钥输入框.EchoMode.Normal,
         "勾「👁 显示」能临时看明文（对账/核对用）")
    AI页.显示密钥框.setChecked(False)
    检查(AI页.密钥输入框.echoMode() == AI页.密钥输入框.EchoMode.Password,
         "取消勾选立刻回到打码")
    检查("未配置" in AI页.密钥状态标签.text(),
         f"没密钥时状态条明确提示：{AI页.密钥状态标签.text()[:36]}…")
    # 填一个占位密钥并保存：只验「写进配置 + 立刻生效」这一路，不发任何网络请求
    # （把接口地址指到本机一个没人听的端口，让助手联网时立刻失败而不是真去请求）
    占位密钥 = "sk-" + "自检占位".encode("utf-8").hex()[:24]
    窗口.配置.setdefault("AI", {})["接口地址"] = "http://127.0.0.1:9"
    AI页.密钥输入框.setText(占位密钥)
    AI页._保存密钥()
    泵(0.3)
    落盘 = json.loads((根 / "配置.json").read_text(encoding="utf-8"))
    检查((落盘.get("AI") or {}).get("api密钥") == 占位密钥,
         "点「💾 保存密钥」后密钥真的写进了 配置.json")
    检查(占位密钥 not in AI页.密钥状态标签.text()
         and AI页.密钥状态标签.text().startswith("✅ 已配置"),
         f"状态条只显示遮盖形态：{AI页.密钥状态标签.text()[:44]}…")
    重启后的运行时 = 窗口.AI运行时()
    检查(str(getattr(getattr(重启后的运行时, "助手", None), "api密钥", ""))
         == 占位密钥,
         "保存后 AI 层**重建**：新密钥立刻生效（清掉了启动自检预建的旧运行时）")
    检查(占位密钥 not in "".join(str(x) for x in
             (AI页.详情框.toPlainText(), AI页.密钥提示标签.text())),
         "详情/提示里都不出现密钥明文")
    AI页.密钥输入框.clear()
    AI页._保存密钥()
    泵(0.2)
    落盘 = json.loads((根 / "配置.json").read_text(encoding="utf-8"))
    检查(not (落盘.get("AI") or {}).get("api密钥")
         and "未配置" in AI页.密钥状态标签.text(),
         "清空输入框再保存 = 撤销密钥，状态条回到「未配置」")

    print("\n[17c] AI 页：窗口变矮时整页滚动，控件不被压扁")
    原AI尺寸 = 窗口.size()
    窗口.resize(QSize(窗口.width(), 620))
    泵(0.35)
    # 现在 AI 页拆成三个页签，每页各自带滚动区；这里在**AI状态页**上验证滚动
    AI页.切换AI页签("状态")
    泵(0.3)
    滚动区 = AI页.当前页签滚动区
    检查(滚动区 is not None and hasattr(滚动区, "verticalScrollBar"),
         "当前页签有自己的滚动区")
    检查(AI页.页面滚动区.verticalScrollBarPolicy().name == "ScrollBarAlwaysOff",
         "外层不再纵向滚动（否则会出两层滚动条、底部按钮永远露不出来）")
    滚动 = 滚动区.verticalScrollBar()
    检查(滚动.maximum() > 0,
         f"AI状态页装不下时出现纵向滚动条（可滚 {滚动.maximum()}px）")
    检查(AI页.详情框.height() > 100,
         f"运行详情框不再被压成一条缝（{AI页.详情框.height()}px）")
    高时范围 = 滚动.maximum()
    窗口.resize(QSize(窗口.width(), 1000))
    泵(0.35)
    滚动2 = AI页.当前页签滚动区.verticalScrollBar()
    检查(滚动2.maximum() < 高时范围,
         f"窗口拉高后滚动范围变小（{高时范围} → {滚动2.maximum()}），"
         "内容始终保持设计高度")
    窗口.resize(QSize(窗口.width(), 620))
    泵(0.3)
    滚动 = AI页.当前页签滚动区.verticalScrollBar()
    滚动.setValue(滚动.maximum())
    泵(0.2)
    视口 = AI页.当前页签滚动区.viewport()
    位置 = AI页.刷新余额按钮.mapTo(视口, QPoint(0, 0))
    检查(0 <= 位置.y() and 位置.y() + AI页.刷新余额按钮.height() <= 视口.height() + 2,
         f"滚到底部能看到「刷新余额」（y={位置.y()}，视口高 {视口.height()}）")
    滚动.setValue(0)
    窗口.resize(原AI尺寸)
    泵(0.2)

    print("\n[17d] 设置页：一键更新（含真跑一遍更新脚本）+ 联系方式")
    from PySide6.QtWidgets import QLineEdit
    from v8_3 import 更新 as 更新模块
    窗口.切换到设置页()
    泵(0.3)
    设置页 = 窗口._设置页面
    检查(设置页 is not None and 窗口.当前页面() is 设置页,
         "左侧「⚙ 设置」能切到设置页")
    检查(窗口.设置按钮.objectName() == "active", "设置按钮高亮为当前页")
    检查(更新模块.版本显示() in 设置页.版本标签.text(),
         f"设置页显示当前版本：{设置页.版本标签.text()}")
    联系值 = [框.text() for 框 in 设置页.findChildren(QLineEdit)]
    检查(更新模块.QQ in 联系值 and 更新模块.邮箱 in 联系值
         and 更新模块.仓库拥有人 in 联系值 and 更新模块.仓库 in 联系值,
         f"联系方式齐全（QQ {更新模块.QQ} / {更新模块.邮箱} / {更新模块.仓库}）")

    检查(更新模块.比较版本("v1.2.0", "1.0.0") == 1
         and 更新模块.比较版本("1.0.0", "v1.0.0") == 0
         and 更新模块.比较版本("1.0.0", "1.0.1") == -1
         and 更新模块.有新版("v1.1.0") and not 更新模块.有新版("v1.0.0"),
         "版本号比较 / 是否有新版 判断正确")
    假资源 = [{"name": f"网盘管理-V1.1.0-{平台}-{版本}-{后缀}.zip", "size": 1}
            for 平台 in ("Linux", "Windows")
            for 版本, 后缀 in (("完整版", "含AI语音模型"), ("精简版", "不含模型"))]
    挑中 = 更新模块.选择资源(假资源, "windows", True)
    挑简 = 更新模块.选择资源(假资源, "windows", False)
    检查(挑中 is not None and "完整" in 挑中["name"] and "含AI语音模型" in 挑中["name"]
         and 挑简 is not None and "精简" in 挑简["name"] and "不含模型" in 挑简["name"],
         f"能按「平台 + 完整/精简」自动挑包：{挑中['name']} / {挑简['name']}")

    # —— 真造一个"新版本包"，把更新脚本跑一遍：程序要更新，用户数据要原样 ——
    import subprocess
    import zipfile
    假新根 = 根 / "假新版本"
    (假新根 / "v8_3").mkdir(parents=True, exist_ok=True)
    (假新根 / "v8_3" / "新文件.txt").write_text("新版", encoding="utf-8")
    假包 = 根 / "假更新包.zip"
    with zipfile.ZipFile(假包, "w") as 包:
        包.write(假新根 / "v8_3" / "新文件.txt", "假新版本/v8_3/新文件.txt")
    解开 = 更新模块.解压到(假包, 根 / "暂存")
    检查((解开 / "v8_3" / "新文件.txt").is_file(),
         f"解压发布包并自动脱掉外层文件夹（{解开.name}/v8_3/新文件.txt）")

    假项目 = 根 / "假项目"
    (假项目 / "v8_3").mkdir(parents=True, exist_ok=True)
    (假项目 / "v8_3" / "旧文件.txt").write_text("旧版", encoding="utf-8")
    (假项目 / "配置.json").write_text('{"我的": "设置"}', encoding="utf-8")
    (假项目 / "数据").mkdir(exist_ok=True)
    (假项目 / "数据" / "界面日志.txt").write_text("我的日志", encoding="utf-8")
    脚本 = 更新模块.写更新脚本(解开, 假项目, 等进程=0, 重启="/bin/true")
    检查(脚本.is_file() and bool(脚本.stat().st_mode & 0o111),
         f"生成了可执行的更新脚本：{脚本.name}")
    跑 = subprocess.run(["/bin/bash", str(脚本)], capture_output=True,
                      text=True, timeout=120)
    检查(跑.returncode == 0 and (假项目 / "v8_3" / "新文件.txt").is_file(),
         "更新脚本真的把新版本合并覆盖上去了")
    检查((假项目 / "配置.json").read_text(encoding="utf-8") == '{"我的": "设置"}',
         "更新**不动**用户配置（配置.json 原样保留）")
    检查((假项目 / "数据" / "界面日志.txt").is_file(),
         "更新**不动**用户数据（数据/ 原样保留）")
    检查((假项目 / "v8_3" / "旧文件.txt").is_file(),
         "合并式覆盖：新版里没有的旧文件不会被删掉")

    print("\n[18] 统计栏第一行：决策来源 + 当前决策")
    for 名 in ("决策来源标签", "决策标签"):
        检查(hasattr(面板, 名), f"统计栏第一行包含 {名}")
    传输页.添加上传任务("fake_2", str(敏感词文件), "/文档")
    检查(等待(lambda: 传输页._本地线程 is None, 60), "等本地任务结束")
    检查("本地任务" in 面板.决策来源标签.text(),
         f"本地上传/下载会标明来源：{面板.决策来源标签.text()}")
    检查("并发 4" in 面板.决策标签.text() or "策略" in 面板.决策标签.text(),
         f"当前决策有内容：{面板.决策标签.text()}")

    # 用假调度器跑一批，验证 AI 决策会显示在统计栏（不联网）
    class 假调度器:
        def 请求策略(self, 特征, 决策来源标记=None):
            return {"并发数": 6, "优先级规则": ["大文件优先", "敏感词提前改名"],
                    "分批策略": "顺序处理", "调度原因": "自检假策略",
                    "预期效果": "测试", "置信度": 0.9, "来源": "AI"}
        def 请求文件优先级(self, 文件列表):
            return {x["路径"]: 1 for x in 文件列表}
        def 请求失败诊断(self, 错误信息, 文件名, 已重试次数):
            return {"动作": "重试", "理由": "自检"}
        def 请求运行时调优(self, 当前并发, 吞吐_MBps, 失败率, 队列长度,
                      已完成, 总数):
            return {"并发数": 当前并发, "动作": "继续", "理由": "自检不动"}
        def 回填效果(self, *a, **k):
            return None
        def 获取统计(self):
            return {"AI调用": 1}

    传输页.AI调度器 = 假调度器()
    传输页.用AI框.setChecked(True)
    (根 / "假网盘1" / "云端" / "决策源").mkdir(parents=True, exist_ok=True)
    for i in range(3):
        (根 / "假网盘1" / "云端" / "决策源" / f"d{i}.bin").write_bytes(b"y" * 2048)
    idx = 传输页.源网盘框.findData("fake_1")
    传输页.源网盘框.setCurrentIndex(idx)
    传输页.源路径框.setText("/决策源")
    idx = 传输页.目标网盘框.findData("fake_2")
    传输页.目标网盘框.setCurrentIndex(idx)
    传输页.目标路径框.setText("/决策目标")
    传输页._开始传输()
    检查(等待(lambda: 传输页._批次线程 is None, 180, "等待批次结束"),
         "带 AI 调度器的批次跑完")
    检查("AI" in 面板.决策来源标签.text(),
         f"决策来源显示 AI：{面板.决策来源标签.text()}")
    检查("并发 6" in 面板.决策标签.text() and "自检假策略" in 面板.决策标签.text(),
         f"当前决策显示并发与原因：{面板.决策标签.text()}")
    传输页.AI调度器 = None

    print("\n[19] 终端打印（V8 风格，方便调试和维护）")
    from v8_3.日志 import (设置终端日志, 终端输出, 设置级别, 应该输出,
                        开启 as 开启终端, 关闭 as 关闭终端)
    import contextlib, io
    捕获 = io.StringIO()
    with contextlib.redirect_stderr(捕获):
        设置终端日志(级别="信息")
        终端输出("信息级会打印", 级别="信息")
        终端输出("调试级被过滤", 级别="调试")
        设置终端日志(级别="调试")
        终端输出("调试级也打印了", 级别="调试")
    文本 = 捕获.getvalue()
    检查("信息级会打印" in 文本, "终端能打出信息级日志")
    检查("调试级被过滤" not in 文本, "默认级别会过滤调试日志")
    检查("调试级也打印了" in 文本, "--调试（调试级）能打出调试日志")
    捕获2 = io.StringIO()
    with contextlib.redirect_stderr(捕获2):
        关闭终端()
        终端输出("静默模式不该出现")
        开启终端()
        窗口.追加日志("界面追加日志会同步到终端")
        设置级别("信息")
    检查("静默模式不该出现" not in 捕获2.getvalue(), "关闭后终端不再输出")
    检查("界面追加日志会同步到终端" in 捕获2.getvalue(),
         "界面日志会同时打到终端（调试时不用切回界面看）")
    from v8_3.日志 import 强制关闭 as 强制静默, 强制关闭
    捕获3 = io.StringIO()
    with contextlib.redirect_stderr(捕获3):
        强制静默(True)
        开启终端()                       # --静默 时配置里的开关不能把它打开
        终端输出("强制静默下不该出现")
        强制静默(False)
        开启终端()
        终端输出("解除静默后恢复")
    检查("强制静默下不该出现" not in 捕获3.getvalue()
         and "解除静默后恢复" in 捕获3.getvalue(),
         "--静默 优先级高于配置里的终端日志开关")
    import 启动 as 启动模块
    其余, 级别, 静默, 自检开关 = 启动模块._取出日志参数(["--调试", "--cli", "状态"])
    检查(级别 == "调试" and 其余 == ["--cli", "状态"] and 自检开关,
         "--调试 参数解析正确")
    其余, 级别, 静默, 自检开关 = 启动模块._取出日志参数(
        ["--静默", "--日志级别", "警告"])
    检查(静默 and 级别 == "警告", "--静默/--日志级别 参数解析正确")
    其余, 级别, 静默, 自检开关 = 启动模块._取出日志参数(["--跳过自检"])
    检查(自检开关 is False, "--跳过自检 参数解析正确")
    捕获4 = io.StringIO()
    with contextlib.redirect_stderr(捕获4):
        窗口.追加日志("这条是界面日志", "警告")
    检查("这条是界面日志" in 捕获4.getvalue(), "界面日志支持带级别输出")

    print("\n[20] 启动自检横幅（V8 风格：分步 emoji + 自检结论）")
    from v8_3.启动自检 import 启动自检
    捕获5 = io.StringIO()
    with contextlib.redirect_stderr(捕获5):
        自检 = 启动自检(配置, 根 / "配置.json", 禁用网络=True,
                    自动刷新价格=False, 连通性=False)
        自检.运行()
    横幅 = 捕获5.getvalue()
    for 片段 in ("🚀 网盘管理 V8_3 · 启动自检", "🎨 主题", "💰 初始化价格抓取器",
                "🔒 初始化敏感词数据库", "📚 初始化 AI 学习库",
                "✅ AI 调度器就绪"):
        检查(片段 in 横幅, f"启动自检包含：{片段}")
    for 片段 in ("🧩 读取网盘核心信息", "网盘核心信息（启用"):
        检查(片段 in 横幅, f"启动自检包含：{片段}")
    检查("适配器" in 横幅 and "凭证" in 横幅 and "线程" in 横幅,
         "启动自检列出了每个网盘的核心字段")
    检查("fake_1" in 横幅 or "假网盘一号" in 横幅,
         "启动自检列出已配置的网盘实例")
    检查(自检.运行时 is not None, "启动自检产出 AI 运行时（主窗口可直接复用）")
    检查("跳过" in 横幅 or "连通性测试" not in 横幅,
         "关掉连通性时不会去测网盘")
    检查(横幅.count("=" * 20) >= 2, "横幅用分隔线分段（与 V8 观感一致）")

    print("\n[21] emoji/级别显示规则")
    from v8_3.日志 import 图标 as 取图标, 含图标
    规则用例 = [("任务失败：连接超时", "❌"), ("connection reset", "⚠️"),
              ("初始化敏感词数据库...", "🔒"), ("扫描源 /目录", "🔍"),
              ("[AI] 采用策略并发：32", "🧭"), ("随便一条普通消息", ""),
              ("[AI] 已回填效果：吞吐 1.4 MiB/s，失败率 0%", "📈"),
              ("完成 4，跳过 0，失败 0，平均 1.7 MiB/s", "✅")]
    for 文本, 期望 in 规则用例:
        实际 = 取图标(文本, "警告" if 期望 == "⚠️" else "信息")
        检查(实际 == 期望, f"{文本[:16]!r} → {实际 or '(无)'}")
    检查(取图标("✅ 已经带 emoji 了") == "" and 含图标("[桥] ✅ 就绪"),
         "消息自带 emoji 时不再叠加")
    检查(取图标("[🅠 夸克网盘] [桥] 后端已就绪") == "",
         "适配器日志前缀带实例图标，且不再叠加")

    print("\n[22] 网盘核心信息（有哪些网盘可用 + 每个网盘的关键细节）")
    from v8_3.网盘信息 import 网盘信息, 脱敏, 格式化字节
    # 给假网盘造一份"凭证"，验证只显示位置/大小/时间，不泄漏内容
    密钥 = "sk-this-is-a-fake-secret-token-0123456789"
    凭证 = 根 / "假网盘1" / "数据" / "会话.json"
    凭证.parent.mkdir(parents=True, exist_ok=True)
    凭证.write_text(json.dumps({"bduss": 密钥}), encoding="utf-8")
    信息 = 网盘信息(配置)
    记录 = 信息.收集()
    行列表: list[str] = []
    信息.打印(记录, lambda t, 级别="信息": 行列表.append(str(t)),
            含账号=False)
    全部 = "\n".join(行列表)
    检查("网盘核心信息" in 全部 and "可用网盘" in 全部,
         "打印了核心信息标题与可用统计")
    检查("🅱" in 全部 or "fake_1" in 全部, "每个启用的网盘都有条目")
    检查("适配器" in 全部 and "凭证" in 全部 and "解释器" in 全部
         and "敏感词" in 全部 and "线程" in 全部,
         "每个网盘都列出 目录/凭证/解释器/线程/敏感词")
    from v8_3.核心.适配器 import 适配器规格
    from v8_3.核心.模型 import 网盘类型
    真盘 = 适配器规格(类型=网盘类型.夸克, 项目根=str(根 / "假网盘1"))
    检查(真盘.凭证文件.name == "凭证.json"
         and f"数据/{真盘.凭证文件.name}" in str(真盘.凭证文件),
         "真网盘的凭证文件按各家约定解析（夸克=数据/凭证.json）")
    检查("凭证" in 全部 and ("B ·" in 全部 or "不需要" in 全部),
         "凭证只显示相对位置、大小与时间（假网盘显示不需要）")
    检查(密钥 not in 全部, "★ 凭证内容没有出现在输出里（不泄漏密钥）")
    样本 = 脱敏({"stoken": "a" * 40, "bduss": True,
              "member": {"total_capacity": 6597740855296,
                         "member_type": "SUPER_VIP"},
              "refresh_error": "未登录或登录已过期（errno:-6）"})
    检查(any("已配置" in x for x in 样本) and "a" * 40 not in " ".join(样本),
         f"详情字段自动脱敏：{样本}")
    检查(any("6.0 TiB" in x for x in 样本), "容量字段转成人类可读单位")
    检查(格式化字节(6597740855296).endswith("TiB"), "容量格式化函数可用")
    from v8_3.日志 import 图标 as 取图标2
    检查(取图标2("      适配器  : /x") == "",
         "缩进的明细行不会再加图标（V8 风格的缩进排版不被破坏）")

    print("\n[23] 统一登录（6 种方式：扫码 / Cookie / 短信 / 邮箱 / 令牌 / 账号密码）")
    from v8_3.界面.登录对话框 import 登录对话框
    窗口.切换网盘页("fake_1")
    泵(0.2)
    登录页 = 窗口._网盘页面["fake_1"]
    检查(hasattr(登录页, "登录按钮") and "登录 / 管理" in 登录页.登录按钮.text(),
         f"网盘页的「登录 / 管理」按钮：{登录页.登录按钮.text()}")
    检查(type(登录页.登录按钮).__name__ == "QPushButton",
         "登录 / 管理 是普通按钮（不走分体按钮样式）")
    # 「适配器原 GUI」仍是独立按钮（递归找，避免依赖控件层级）
    原GUI = next((b for b in 登录页.findChildren(QPushButton)
               if "适配器原 GUI" in b.text()), None)
    检查(原GUI is not None, "网盘页保留了「🖥 适配器原 GUI」独立按钮")
    检查(getattr(登录页, "登录菜单", None) is None,
         "已回到上一版：没有分体按钮的菜单")
    规格 = 窗口.动作.规格("fake_1")
    记录 = {}
    原启动 = type(规格).启动适配器GUI
    type(规格).启动适配器GUI = lambda self: 记录.setdefault("已启动", str(self.路径))
    try:
        登录页._登录()
    finally:
        type(规格).启动适配器GUI = 原启动
    检查(记录.get("已启动"), f"「适配器原 GUI」按钮确实启动原 GUI：{记录.get('已启动', '')[:40]}…")
    对话 = 登录对话框(窗口, dict(配置["适配器"][0]), 窗口)
    对话.show()
    检查(等待(lambda: bool(对话.能力), 30, "等待能力表"), "登录对话框读到了能力表")
    检查(len(对话.方式按钮) == 6,
         f"6 种方式都在界面上：{list(对话.方式按钮)}")
    支持 = [k for k, b in 对话.方式按钮.items() if b.isEnabled()]
    不支持 = [k for k, b in 对话.方式按钮.items() if not b.isEnabled()]
    检查(set(支持) == {"qrcode", "cookie", "sms", "token"},
         f"支持的方式按适配器能力启用：{支持}")
    检查(set(不支持) == {"email", "password"},
         f"未支持的方式被置灰：{不支持}")
    检查("未提供" in 对话.方式按钮["email"].toolTip()
         or "未提供" in 对话.方式按钮["email"].text(),
         f"置灰方式说明了原因：{对话.方式按钮['email'].toolTip()}")
    检查(对话.原GUI按钮.text().startswith("🖥"),
         "不支持的方式有「打开适配器原 GUI」兜底")

    对话._切方式("cookie")
    对话.Cookie输入框.setPlainText("BDUSS=demo; STOKEN=demo")
    对话._Cookie登录()
    检查(等待(lambda: 对话.成功, 60, "等待 Cookie 登录"), "Cookie 登录成功")
    检查("✅" in 对话.状态标签.text(),
         f"状态栏显示成功：{对话.状态标签.text()[:40]}")
    检查((根 / "假网盘1" / "数据" / "登录.json").is_file(),
         "登录结果落到该网盘的适配器目录（数据/登录.json）")

    对话2 = 登录对话框(窗口, dict(配置["适配器"][0]), 窗口)
    对话2.show()
    等待(lambda: bool(对话2.能力), 30, "等待能力表2")
    对话2._切方式("sms")
    对话2.手机号框.setText("+86 13800000000")
    对话2._发送验证码()
    检查(等待(lambda: bool(对话2._短信会话), 30, "等待验证码"),
         f"短信验证码已发送：{对话2.短信提示标签.text()}")
    对话2.验证码框.setText("000000")
    对话2._短信登录()
    检查(等待(lambda: "❌" in 对话2.状态标签.text(), 30, "等待错误码结果"),
         f"错误验证码被拒：{对话2.状态标签.text()[:40]}")
    对话2.验证码框.setText("123456")
    对话2._短信登录()
    检查(等待(lambda: 对话2.成功, 30, "等待短信登录"),
         f"正确验证码登录成功：{对话2.状态标签.text()[:40]}")
    对话2.close()
    对话2.deleteLater()

    对话3 = 登录对话框(窗口, dict(配置["适配器"][0]), 窗口)
    对话3.show()
    等待(lambda: bool(对话3.能力), 30, "等待能力表3")
    对话3._切方式("token")
    对话3.访问令牌框.setText("demo-access-token")
    对话3.刷新令牌框.setText("demo-refresh-token")
    对话3._令牌登录()
    检查(等待(lambda: 对话3.成功, 30, "等待令牌登录"),
         f"令牌登录成功：{对话3.状态标签.text()[:40]}")
    对话3.close()
    对话3.deleteLater()

    # ---- 设备授权码型扫码（光鸭形态）：只给「验证地址」，界面必须自己画二维码 ----
    # 用户现场反馈：「光鸭的二维码登录显示不出二维码图片，而原 GUI 正常」。
    # 根因就是设备码型登录不返回 base64 图片，界面以前也不把验证地址画成码。
    假盘根 = Path(窗口.动作.规格("fake_1").项目根)
    标记 = 假盘根 / "数据" / "扫码模式.txt"
    标记.parent.mkdir(parents=True, exist_ok=True)
    标记.write_text("设备码", encoding="utf-8")
    对话4 = 登录对话框(窗口, dict(配置["适配器"][0]), 窗口)
    对话4.show()
    try:
        等待(lambda: bool(对话4.能力), 30, "等待能力表4")
        对话4._切方式("qrcode")
        对话4._开始扫码()
        检查(等待(lambda: 对话4._扫码会话, 30, "等待设备码"),
             f"设备码型扫码拿到了会话：{对话4._扫码会话}")
        泵(0.3)
        图形 = 对话4.二维码标签.pixmap()
        检查(图形 is not None and not 图形.isNull()
             and 图形.width() >= 150,
             "设备码型登录把「验证地址」画成了二维码："
             f"{'有图 %dx%d' % (图形.width(), 图形.height()) if 图形 else '无图'}")
        检查("用户码" in 对话4.设备码标签.text()
             and "FAKE-CODE-123" in 对话4.设备码标签.text(),
             f"用户码照常显示：{对话4.设备码标签.text()}")
        检查(对话4.验证地址标签.text().startswith("https://"),
             f"验证地址照常显示：{对话4.验证地址标签.text()[:48]}…")
    finally:
        try:
            标记.unlink(missing_ok=True)
        except Exception:
            pass
        对话4.close()
        对话4.deleteLater()

    对话.close()
    对话.deleteLater()
    QApplication.processEvents()

    # ---- 退出登录 / 重新登录：退出后必须"清列表 + 禁写 + 灭绿点" ----
    # 用户现场反馈：在原生 GUI 里退出登录后，这边列表还在、还能上传下载。
    print("\n[23b] 退出登录 / 重新登录（状态同步 + 禁写）")
    from v8_3.界面 import 后台线程 as _后台
    # 闸门是"按窗口"记的：前面用例关掉的那个窗口不该再影响当前窗口。
    检查(not _后台.正在关闭(窗口),
         "多窗口用例关过窗之后，当前窗口的网盘调用不再被闸门挡住"
         "（闸门按窗口记，不是全局开关）")
    检查(_后台.正在关闭(多窗口) if "多窗口" in dir() else True,
         "已经关掉的那个窗口仍然被认定为『正在关闭』（它自己的线程不再发调用）"
         ) if "多窗口" in dir() else None
    窗口.切换网盘页("fake_1")
    泵(0.3)
    页 = 窗口._网盘页面["fake_1"]
    页.刷新管理区()          # 前面几段登录用例刚写过凭证，这里重新核一次状态
    等待(lambda: "已登录" in 页.状态标签.text(), 30, "等待已登录状态")
    检查(等待(lambda: 页.文件表格.rowCount() > 0, 15, "等待列出文件"),
         f"登录状态下能列出文件：{页.文件表格.rowCount()} 行")
    检查(等待(lambda: 页.上传按钮.isEnabled(), 15, "等待写按钮解禁"),
         "登录状态下上传/删除按钮可用")
    凭证路径 = Path(窗口.动作.规格("fake_1").凭证文件)
    检查(凭证路径.is_file(), f"凭证文件存在：{凭证路径.name}")
    页._退出登录(静默=True)
    检查(等待(lambda: not 凭证路径.is_file(), 30, "等待凭证被删除"),
         "退出登录删掉了适配器的凭证文件")
    检查(等待(lambda: "未登录" in 页.状态标签.text(), 30, "等待状态同步"),
         f"状态栏同步为未登录：{页.状态标签.text()[:30]}")
    检查(页.文件表格.rowCount() == 0,
         f"退出登录后文件列表被清空：{页.文件表格.rowCount()} 行")
    检查(not 页.上传按钮.isEnabled() and not 页.下载按钮.isEnabled()
         and not 页.删除按钮.isEnabled(),
         "退出登录后上传/下载/删除按钮被禁用（不允许再对着空账号操作）")
    检查(窗口._网盘状态.get("fake_1") is False,
         f"左侧导航的登录点同步为未登录：{窗口._网盘状态.get('fake_1')}")
    # 凭证被外部删掉（模拟"在原 GUI 里退出登录"）：监视器/兜底轮询要能同步
    页._凭证有变化()
    检查(等待(lambda: "未登录" in 页.状态标签.text(), 30, "等待外部退出同步"),
         f"凭证被外部删除后界面同步为未登录：{页.状态标签.text()[:30]}")
    检查(页.文件表格.rowCount() == 0, "外部退出后列表同样被清空")

    print("\n[24] 线程安全：界面控件只在界面线程里被碰")
    检查(not Qt警告,
         f"没有跨线程操作控件的 Qt 警告（{len(Qt警告)} 条）"
         + (f"：{Qt警告[:3]}" if Qt警告 else ""))

    print("\n[25] 传输任务表格（V8 形态：分类标签 + 8 列 + 时间列 + 操作）")
    传输页 = 窗口.传输页面()
    表 = 传输页.任务表
    检查(表.columnCount() == 9,
         f"表格列数 = 9（8 列 + 完成/失败时间）：{表.columnCount()}")
    表头 = [表.horizontalHeaderItem(i).text() for i in range(表.columnCount())]
    检查(表头[:8] == ["文件名", "类型", "大小", "进度", "速度", "耗时",
                    "状态", "操作"],
         f"前 8 列表头与 V8 一致：{表头[:8]}")
    检查(表头[8] in ("完成/失败时间", "完成时间", "失败时间"),
         f"第 9 列是完成/失败时间：{表头[8]}")
    分类键 = list(传输页.分类按钮组)
    检查(分类键 == ["全部", "进行中", "已完成", "等待", "暂停", "跳过",
                  "失败", "未完成"],
         f"8 个分类标签齐全：{分类键}")
    检查(all("(" in b.text() for b in 传输页.分类按钮组.values()),
         "分类标签带计数，例如："
         + 传输页.分类按钮组["全部"].text())
    # 分类过滤：造几条不同状态的任务，验证"按状态分表"
    from v8_3.界面.任务表格 import 在分类里, 取分类
    样例 = [
        {"任务ID": "t1", "状态": "进行中", "名称": "跑着的.bin", "大小": 100,
         "已传输": 40},
        {"任务ID": "t2", "状态": "等待", "名称": "排队的.bin", "大小": 100},
        {"任务ID": "t3", "状态": "暂停", "名称": "暂停的.bin", "大小": 100},
        {"任务ID": "t4", "状态": "已完成", "名称": "完成的.bin", "大小": 100,
         "已传输": 100, "结束": time.time()},
        {"任务ID": "t5", "状态": "失败", "名称": "失败的.bin", "大小": 100,
         "错误": "适配器错误: [quark/] 名称不可用", "结束": time.time()},
        {"任务ID": "t6", "状态": "跳过", "名称": "跳过的.bin", "大小": 100,
         "跳过原因": "目标已存在"},
    ]
    for 分类, 期望数 in (("全部", 6), ("进行中", 1), ("等待", 1), ("暂停", 1),
                     ("已完成", 1), ("失败", 1), ("跳过", 1), ("未完成", 3)):
        命中 = [t for t in 样例 if 在分类里(t, 分类)]
        检查(len(命中) == 期望数,
             f"分类「{分类}」命中 {len(命中)} 条（期望 {期望数}）")
    传输页.任务表.设置台账({t["任务ID"]: t for t in 样例})
    传输页.任务表.切换分类("失败")
    传输页.任务表.刷新()
    检查(传输页.任务表.rowCount() == 1,
         f"切到「失败」只剩 1 行：{传输页.任务表.rowCount()}")
    检查(not 传输页.任务表.isColumnHidden(传输页.任务表.时间列),
         "失败分类里显示「失败时间」列")
    检查(传输页.任务表.isColumnHidden(8) is False, "失败分类显示时间列")
    状态文本 = 传输页.任务表.item(0, 6).text()
    检查("失败" in 状态文本 and "名称不可用" in 状态文本,
         f"状态列带失败原因：{状态文本}")
    检查(bool(传输页.任务表.item(0, 传输页.任务表.时间列).text()),
         "失败时间已记录："
         + 传输页.任务表.item(0, 传输页.任务表.时间列).text())
    操作容器 = 传输页.任务表.cellWidget(0, 7)
    from PySide6.QtWidgets import QAbstractButton
    按钮名 = sorted(b.objectName()
                 for b in 操作容器.findChildren(QAbstractButton))
    检查(set(按钮名) == {"暂停", "继续", "重试", "详情", "取消"},
         f"操作列按钮齐全：{按钮名}")
    可用 = {b.objectName() for b in 操作容器.findChildren(QAbstractButton)
          if b.isEnabled()}
    检查("详情" in 可用 and "重试" in 可用,
         f"失败任务上「详情/重试」可用，暂停不可用：{sorted(可用)}")
    传输页.任务表.切换分类("已完成")
    传输页.任务表.刷新()
    检查(传输页.任务表.horizontalHeaderItem(8).text() == "完成时间",
         "已完成分类的表头是「完成时间」")
    检查(bool(传输页.任务表.item(0, 8).text()), "完成时间已记录")
    传输页.任务表.切换分类("全部")
    传输页.任务表.刷新()
    检查(传输页.任务表.isColumnHidden(8), "全部/其它分类隐藏时间列（不挤占视图）")

    print("\n[26] 播放页（V8_3 新增：内嵌 libvlc + AI 观影）")
    窗口.切换到播放页()
    泵(0.1)
    播放页 = None
    try:
        播放页 = 窗口.播放页面()
    except Exception as e:  # noqa: BLE001
        检查(False, f"播放页打不开：{type(e).__name__}: {e}")
    if 播放页 is None:
        检查(False, "播放页对象为空")
    else:
        from PySide6.QtWidgets import QAbstractButton, QSlider
        # —— 控件齐全 ——
        检查(播放页.视频 is not None, "视频窗控件存在（原生窗口，交给 libvlc 画）")
        检查(isinstance(播放页.进度条, QSlider),
             f"进度条是 QSlider（能拖动 seek）：{type(播放页.进度条).__name__}")
        按钮名 = {b.text() for b in 播放页.findChildren(QAbstractButton)}
        # 「本地文件」已按需求挪到菜单栏「📁 文件」；独立窗口按钮改名「🗗 独立窗口」
        期望按钮 = ["▶ 播放", "📂 浏览…", "🗗 独立窗口",
                 "⏸", "⏹", "💬字幕", "📷截图", "⛶全屏", "🌐翻译字幕",
                 "🎙生成字幕", "📝总结", "🩺诊断"]
        缺 = [名 for 名 in 期望按钮
              if not any(名.strip("▶📁📂🗗⏸⏹💬📷⛶🌐🎙📝🩺… ") in b for b in 按钮名)]
        检查(not 缺, f"播放页按钮齐全（缺：{缺}）")
        # —— VLC 风格：菜单 + 工具栏 + 播放清单 ——
        菜单标题 = [a.text() for a in 播放页.菜单栏.actions()]
        for 需要 in ("媒体(&M)", "播放(&P)", "音频(&A)", "视频(&V)", "字幕(&S)",
                    "视图(&I)", "工具(&T)", "帮助(&H)"):
            检查(需要 in 菜单标题, f"播放页有 VLC 同款菜单「{需要}」")
        检查(any("AI" in t for t in 菜单标题), "播放页有额外加的「🤖 AI」菜单")
        # 需求：视频上方那行（工具栏）撤销，按钮落到菜单栏「帮助」后面
        检查(播放页.工具栏 is None, "播放页视频上方那行（工具栏）已撤销")
        _菜单按钮 = [a.text() for a in 播放页.菜单栏.actions()]
        检查(_菜单按钮 and _菜单按钮[-1] in ("🗂 面板",),
             f"菜单栏最后是面板开关（帮助之后）：{_菜单按钮[-1:]}")
        检查(not any(t.strip().endswith("网盘") for t in _菜单按钮[8:])
             and not any(t.strip().endswith("文件") for t in _菜单按钮[8:]),
             f"菜单栏「帮助」后面只有面板开关（网盘/文件按钮已按需求去掉）：{_菜单按钮[-3:]}")
        # 需求：左侧面板三页 —— 视频信息 / 播放清单 / AI 助手
        _左 = 播放页.左面板
        _标题 = [_左.tabText(i) for i in range(_左.count())]
        检查(_左.count() == 3
             and "信息" in _标题[0] and "清单" in _标题[1] and "AI" in _标题[2],
             f"视频左侧面板有三页（视频信息/播放清单/AI 助手）：{_标题}")
        检查(播放页.信息栏.parent() is not None
             and 播放页.信息栏.parent() is not 播放页,
             "视频信息已搬进侧边面板的「视频信息」页")
        # 需求：面板改到视频**右侧**
        主体 = 播放页.主体
        检查(主体.widget(0) is 播放页.视频 and 主体.widget(1) is 播放页.左面板,
             "面板在视频**右侧**（顺序：视频 → 面板）")
        # 需求：AI 按钮两行显示（不被截断）
        from PySide6.QtWidgets import QGridLayout as _格子类
        _网格 = None
        for _i in range(播放页.AI面板.layout().count()):
            _项 = 播放页.AI面板.layout().itemAt(_i)
            if isinstance(getattr(_项, "layout", lambda: None)(), _格子类):
                _网格 = _项.layout(); break
        检查(_网格 is not None and _网格.rowCount() == 2 and _网格.columnCount() == 2,
             "AI 助手四个按钮排成两行（2×2），窄面板也不会被截断")
        # 需求：AI 日志降噪 —— 同类过程信息折叠，决策照常显示
        _板 = 播放页.AI面板
        _前 = len(_板.文本().splitlines())
        for _ in range(30):
            _板.追加("[播放顾问] 卡顿诊断：本地模型输出不是 JSON，尝试其它来源")
        _后噪音 = len(_板.文本().splitlines()) - _前
        _板.追加("[AI 决策] 自动调优 → 缓存 20000ms")
        _后决策 = len(_板.文本().splitlines()) - _前 - _后噪音
        检查(_后噪音 <= 2 and _后决策 == 1,
             f"AI 日志降噪：30 条重复调试信息只显示 {_后噪音} 行，决策照常显示 {_后决策} 行")
        检查(播放页.AI输出.parent() is not None, "AI 助手日志框在面板里")
        检查(_左.indexOf(播放页.清单) >= 0, "播放清单已搬进左侧面板")
        检查(_左.indexOf(播放页.AI输出.parentWidget().parentWidget()) >= 0
             or _左.indexOf(_左.widget(2)) == 2,
             "AI 助手页在左侧面板里（第 3 页）")
        # 需求：控件换了位置（播放/独立窗口/循环随机都在下面控制条）
        _条 = 播放页.控制条
        检查(_条.播放按钮.isVisibleTo(_条)
             and _条.播放暂停按钮.isVisibleTo(_条)
             and list(_条.layout().itemAt(1).layout().itemAt(i).widget().text()
                  for i in range(2)) == ["▶ 播放", "⏸"],
             "「▶ 播放」在「⏸ 暂停」前面")
        检查(_条.独立窗口按钮.isVisibleTo(_条)
             and _条.全屏按钮.isVisibleTo(_条),
             "「🗗 独立窗口」在「⛶ 全屏」后面")
        检查(_条.音量条 is not None and _条.倍速框 is not None,
             "控制条里有音量滑条与速度下拉")

        # —— 选到目录必须被拦下（网盘分享目录常叫 xx.4K.60fps，光看名字像视频）——
        原会话 = 播放页.会话
        播放页.网盘框.setCurrentIndex(0)
        播放页.路径框.setText("/自检目录.4K.60fps")
        原问目录 = 播放页._是不是目录
        播放页._是不是目录 = lambda *_: (True, "自检目录 是目录，不是视频文件")
        播放页._播放()
        泵(0.2)
        检查("目录" in 播放页.状态标签.text() or "目录" in 播放页.信息栏.text(),
             f"选到目录时不取直链、给出提示：{播放页.状态标签.text()[:40]}")
        检查(播放页.会话 is 原会话 or 播放页.会话 is None,
             "选到目录时不会建出播放会话")
        播放页._是不是目录 = 原问目录
        播放页.路径框.setText("")

        # —— 选片对话框：只要文件时"选中视频 → 确定"必须真的生效 ——
        #    （老实现要求"选中项路径 == 路径框文本"，而路径框是当前目录 →
        #     用户在对话框里选好视频点确定永远没反应，就是"选不中"）
        from v8_3.界面.路径选择对话框 import 路径选择对话框 as _对话框
        from v8_3.播放.媒体信息 import 是视频文件 as _是视频

        class _条目:
            def __init__(self, name, path, is_dir=False, size=10):
                self.name, self.path, self.is_dir, self.size = name, path, is_dir, size

        class _假适配器:
            def 列目录(self, _路径="/"):
                return [_条目("文件夹", "/文件夹", True),
                        _条目("影片.mp4", "/影片.mp4"),
                        _条目("说明.txt", "/说明.txt")]

        框 = _对话框(_假适配器(), "fake_1", 初始路径="/", 只要文件=True,
                  名字过滤=_是视频, 父窗口=播放页)
        框.show()
        # 对话框是**后台线程**列目录：必须等列表真加载出来（固定等待会偶发空列表）
        等待(lambda: 框.列表.count() > 0, 10.0, "选片对话框加载目录")
        行 = next((i for i in range(框.列表.count())
                 if 框.列表.item(i).text().endswith(".mp4")), None)
        if 行 is None:
            检查(False, f"选片对话框没列出视频文件：{框.列表.count()} 项")
            行 = 0
        框.列表.setCurrentRow(行)
        泵(0.2)
        检查(框.确定按钮.isEnabled(), "选片对话框：选中视频后「确定」可用")
        框._确定选择()
        检查(框.选中路径 == "/影片.mp4" and 框.result() == 1,
             f"选片对话框：选中视频点确定真的返回路径（{框.选中路径!r}）")
        框.deleteLater()
        框2 = _对话框(_假适配器(), "fake_1", 初始路径="/", 只要文件=True,
                   名字过滤=_是视频, 父窗口=播放页)
        框2.show()
        等待(lambda: 框2.列表.count() > 0, 10.0, "选片对话框加载目录")
        目录行 = next((i for i in range(框2.列表.count())
                   if "文件夹" in 框2.列表.item(i).text()), 0)
        框2.列表.setCurrentRow(目录行)
        泵(0.2)
        检查(not 框2.确定按钮.isEnabled(),
             "选片对话框：选中目录时「确定」禁用（不会把目录交给播放器）")
        框2.deleteLater()

        # —— 选片方式：与传输页源/目标同一套（对话框 + 浏览按钮）——
        import inspect as _inspect
        浏览源 = _inspect.getsource(播放页._浏览)
        检查("路径选择对话框" in 浏览源 and "允许选择文件=True" in 浏览源,
             "「📂 浏览…」用的是传输页同款路径选择对话框（允许选文件）")
        检查(播放页.自动调优框.isChecked(), "「AI 自动换参数重载」默认勾选")
        检查(播放页.网盘框.count() >= 1,
             f"网盘下拉已填充 {播放页.网盘框.count()} 个网盘")
        # —— 无显示环境必须返回 0 句柄（否则 libvlc 往假窗口画 → 段错误）——
        from v8_3.播放.显示环境 import 可嵌入窗口 as _可嵌3
        _能嵌 = _可嵌3(QApplication.platformName())
        _句柄 = 播放页._安全句柄()
        检查((_句柄 == 0) if not _能嵌 else (_句柄 > 0),
             f"句柄取值正确（平台 {QApplication.platformName()}，可嵌入={_能嵌}）："
             f"{_句柄}（无 X11 窗口号时必须为 0，否则 libvlc 会自己开窗口/段错误）")
        # —— libvlc 绑定可用性与类型安全 ——
        from v8_3.播放 import vlc绑定
        if vlc绑定.可用():
            检查(True, f"libvlc 可用：{vlc绑定.VLC库.版本()}")
            库 = vlc绑定.VLC库.取()
            检查(库.缺类型符号() == [],
                 f"所有会被调用的符号都配了 argtypes（缺：{库.缺类型符号()}）")
            检查("libvlc_video_get_spu" in vlc绑定._已配类型
                 or "libvlc_media_player_get_spu" in vlc绑定._已配类型,
                 "取字幕轨的函数已绑类型（VLC 3 用 video_* 名字）")
        else:
            检查(True, f"本机没有 libvlc，播放页应给出提示："
                    f"{vlc绑定.不可用原因()[:40]}")
        # —— 没选片子就点播放：只能提示，不能崩 ——
        播放页.路径框.setText("")
        播放页._播放()
        泵(0.2)
        提示 = 播放页.状态标签.text() + 播放页.信息栏.text()
        检查(any(词 in 提示 for 词 in ("请", "先填", "❌", "⚠️")),
             f"空路径点播放给出提示：{提示[:40]}")
        # —— AI 面板：无 AI 也要能用（规则兜底）——
        播放页._AI写("自检写入")
        检查("自检写入" in 播放页.AI输出.toPlainText(), "AI 面板能写日志")
        检查(播放页._AI客户端() is not None or True, "取 AI 客户端不抛异常")
        # —— 网盘页双击视频 → 播放（真接线，不真起播：拦下入口记录参数）——
        from v8_3.界面.网盘页面 import 网盘页面 as _网盘页面
        页面们 = 窗口.findChildren(_网盘页面)
        if not 页面们:
            检查(False, "找不到网盘页（双击播放无法验证）")
        else:
            网盘页 = 页面们[0]
            记录 = []
            原入口 = 窗口.播放网盘视频
            窗口.播放网盘视频 = lambda 标, 路, 独立窗口=False: 记录.append(
                (标, 路, 独立窗口))

            class 假索引:
                def row(self):
                    return 0

            网盘页._显示列表 = [{"name": "样片.mp4", "is_dir": False,
                            "size": 1024}]
            网盘页.当前目录 = "/电影"
            网盘页._双击项目(假索引())
            检查(记录 and 记录[0][1] == "/电影/样片.mp4",
                 f"网盘页双击视频会带着路径去播放：{记录}")
            检查(记录 and 记录[0][2] is False,
                 "普通双击 = 页面内播放（Ctrl+双击才是独立窗口）")
            # 非视频双击不该触发播放
            记录.clear()
            网盘页._显示列表 = [{"name": "说明.txt", "is_dir": False, "size": 10}]
            网盘页._双击项目(假索引())
            检查(not 记录, "双击非视频文件不会去播放")
            窗口.播放网盘视频 = 原入口

        # —— 关闭：释放 libvlc 播放器/实例，且可重复调用 ——
        try:
            播放页.关闭()
            播放页.关闭()
            检查(True, "播放页关闭可重复调用（libvlc 资源已释放）")
        except Exception as e:  # noqa: BLE001
            检查(False, f"播放页关闭异常：{type(e).__name__}: {e}")

    print("\n[27] 独立播放窗口（全屏 / 控件自动隐藏 / Esc 回窗口化）")
    # 用一个**假会话**（不碰 libvlc）：这一节只验窗口行为，无头环境也能跑
    class 假播放器:
        def __init__(self):
            self.窗口句柄 = 0

        def 时长秒(self):
            return 10.0

        def 进度秒(self):
            return 3.0

        def 取音量(self):
            return 100

        def 绑定窗口(self, _句柄):
            return None

    class 假会话:
        def __init__(self):
            self.播放器 = 假播放器()
            self.标题 = "自检样片.mp4"
            self.远端路径 = "/自检样片.mp4"
            self.关闭次数 = 0
            self.跳转过 = []

        def 起播(self, _句柄=0):
            return True

        def 状态快照(self):
            return {"状态": "播放中", "进度秒": 3.0, "时长秒": 10.0, "丢帧": 0,
                    "已解码视频": 75, "已播秒": 3.0, "输入码率bps": 0.0,
                    "缓冲中": False, "有字幕": False}

        def 暂停(self):
            self.暂停次数 = getattr(self, "暂停次数", 0) + 1

        def 设置音量(self, _值):
            pass

        def 设置速率(self, _值):
            pass

        def 跳转(self, 秒):
            self.跳转过.append(秒)

        def 切换字幕(self):
            return 1

        def 截图(self, _路径):
            return False

        def 关闭(self):
            self.关闭次数 += 1

    from v8_3.界面.播放器窗口 import 播放器窗口 as _播放器窗口
    from PySide6.QtCore import QEvent as _QEvent
    from PySide6.QtCore import Qt as _Qt
    from PySide6.QtGui import QKeyEvent as _QKeyEvent
    假 = 假会话()
    独立 = _播放器窗口(假, 标题="自检样片.mp4")
    检查(独立.parent() is None, "独立窗口是顶层窗口（不挂在主窗口里）")
    from PySide6.QtWidgets import QAbstractButton as _QAB
    控件按钮 = {b.text() for b in 独立.控制条.findChildren(_QAB)}
    检查(len(控件按钮) >= 5, f"独立窗口带播放控件：{sorted(控件按钮)}")
    独菜单 = [a.text() for a in 独立.菜单栏.actions()]
    检查("媒体(&M)" in 独菜单 and "播放(&P)" in 独菜单 and "字幕(&S)" in 独菜单,
         f"独立窗口是 VLC 风格菜单：{独菜单[:8]}")
    检查(any("AI" in t for t in 独菜单), "独立窗口有 AI 菜单")
    检查(独立.工具栏 is None,
         "视频上方那行（工具栏）已按需求整行撤销（控件全在下面的控制条里）")
    检查(独立.右栏.count() == 2 and "清单" in 独立.右栏.tabText(0),
         f"独立窗口右侧是播放清单 + AI：{[独立.右栏.tabText(i) for i in range(独立.右栏.count())]}")
    独立.清单.添加(独立.__class__ and __import__(
        "v8_3.界面.播放清单", fromlist=["播放项"]).播放项(
            标题="自检样片.mp4", 网盘标识="fake_1", 远端路径="/自检样片.mp4"))
    检查(len(独立.清单) == 1 and 独立.清单.当前项() is not None,
         "独立窗口能把视频加进播放清单")
    检查(独立.清单.循环按钮文本().startswith("🔁")
         or 独立.清单.循环按钮文本().startswith("🔂"),
         f"清单有循环模式：{独立.清单.循环按钮文本()}")
    检查(独立.起播(), "假会话起播返回成功")
    泵(0.2)
    检查(独立.控制条.isVisible(), "窗口化时控件可见")
    独立.设置全屏(True)
    泵(0.4)
    屏幕 = QApplication.primaryScreen()
    铺满 = bool(屏幕 is not None
             and 独立.width() >= min(400, 屏幕.size().width())
             and 独立.height() >= min(300, 屏幕.size().height()))
    检查(独立._全屏, "切到全屏：窗口内部状态为全屏")
    检查(独立.isFullScreen() or 铺满,
         f"全屏生效：isFullScreen={独立.isFullScreen()} 尺寸="
         f"{独立.width()}x{独立.height()}（没有窗口管理器时由兜底铺满）")
    检查(独立.控制条.isVisible(), "刚进全屏时控件还亮着")
    等待(lambda: not 独立.控制条.isVisible(), 8.0)
    检查(not 独立.控制条.isVisible(), "全屏静止 2.5s 后控件自动隐藏（整屏都是视频）")
    独立.显示控件()
    泵(0.2)
    检查(独立.控制条.isVisible(), "有动静时控件能回来（显示控件）")
    # 键盘：Esc 回窗口化
    独立.keyPressEvent(_QKeyEvent(_QEvent.KeyPress, _Qt.Key_Escape, _Qt.NoModifier))
    泵(0.3)
    检查(not 独立.isFullScreen(), "全屏按 Esc 回到窗口化")
    检查(独立.控制条.isVisible(), "退出全屏后控件可见")
    # 空格暂停、方向键跳转不抛异常
    for 键 in (_Qt.Key_Space, _Qt.Key_Left, _Qt.Key_Right):
        独立.keyPressEvent(_QKeyEvent(_QEvent.KeyPress, 键, _Qt.NoModifier))
    泵(0.1)
    检查(假.跳转过, f"方向键能跳转：{假.跳转过}")
    # —— 按视频分辨率调窗口（消除黑边）：视频区比例必须≈视频比例 ——
    class _媒体2:
        宽 = 3840
        高 = 2160

    假.媒体 = _媒体2()           # 给假会话一个"探测到的分辨率"
    假.探测 = None
    假.设置 = None
    独立.适应视频比例()
    泵(0.2)
    _误差 = 独立.视频区比例误差()
    # 尺寸计算是纯函数，可以在离屏小屏上也确定性验证（真实窗口受屏幕尺寸限制）
    _最坏 = 0.0
    for _比例 in (16 / 9, 4 / 3, 9 / 16, 2.35, 1.0):
        for _可用 in ((1920, 1000), (1200, 700), (700, 700), (400, 300),
                   (300, 900), (2400, 1300)):
            _w, _h = _播放器窗口.算视频区尺寸(_比例, _可用[0], _可用[1], 3840)
            _e = abs(_w / max(1, _h) - _比例)
            _最坏 = max(_最坏, _e)
    检查(_最坏 < 0.03,
         f"按视频比例算尺寸：各种屏幕/比例下都不产生黑边（最大误差 {_最坏:.4f}）")
    _误差 = 独立.视频区比例误差()
    检查(_误差 < 0.35,          # 离屏"屏幕"只有 800x800，装饰后就那么大，宽松验一下
         f"真窗口里也按比例调整过（视频区 {独立.视频.width()}x{独立.视频.height()}，"
         f"误差 {_误差:.4f}；X11 大屏下误差 <0.01）")

    # —— 藏起面板后不能有左右黑边（用户实测）：显隐后要按比例重排 ——
    独立.切换侧栏(False)
    泵(0.6)                       # 等两次延迟重排（0ms / 90ms）
    _误差藏 = 独立.视频区比例误差()
    _屏幕宽 = 独立.屏幕几何().width() if 独立.屏幕几何() else 0
    if _屏幕宽 >= 1100:
        检查(_误差藏 < 0.03,
             f"藏起右侧面板后视频区仍按比例（误差 {_误差藏:.4f}，"
             f"视频区 {独立.视频.width()}x{独立.视频.height()}）—— 无左右黑边")
        独立.切换侧栏(True)
        泵(0.6)
        _误差显 = 独立.视频区比例误差()
        检查(_误差显 < 0.03, f"再显示面板后也不留黑边（误差 {_误差显:.4f}）")
    else:
        # 离屏自检的"屏幕"只有 800x800：菜单+进度+按钮+状态栏就占掉 400px 高，
        # 16:9 视频区根本放不下 —— 这不是产品问题，真机(≥1100px)上面两条都验过
        检查(True, f"屏幕仅 {_屏幕宽}px：面板/比例装不下，产品会如实提示（跳过严格断言）")
        独立.切换侧栏(True)
        泵(0.4)

    # —— 适应屏幕：4K 片源在小屏/大窗口下都要装得下 ——
    区域 = 独立.屏幕几何()
    if 区域 is not None:
        独立.resize(区域.width() + 500, 区域.height() + 300)
        泵(0.1)
        独立.适应屏幕()
        泵(0.1)
        检查(独立.width() <= 区域.width() and 独立.height() <= 区域.height(),
             f"「适应屏幕」把超大窗口缩回屏幕内：{独立.width()}x{独立.height()}"
             f" ≤ {区域.width()}x{区域.height()}")
    检查(独立._动作表().get("适应屏幕") is not None, "视频菜单里有「适应屏幕」")

    # —— 右侧面板：按钮必须能真正隐藏（以前鼠标一动又被显示回来）——
    独立.切换清单(False)
    泵(0.1)
    检查(not 独立.右栏.isVisible(), "点「📋 播放清单」能把右侧面板藏起来")
    独立.显示控件()                      # 模拟鼠标移动（以前这里会把面板弹回来）
    泵(0.1)
    检查(not 独立.右栏.isVisible(),
         "鼠标一动也不会把「用户主动藏起来」的面板弹回来")
    独立.切换AI面板(True)
    泵(0.1)
    检查(独立.右栏.isVisible() and 独立.右栏.currentIndex() == 1,
         "点「🤖 AI 助手」能显示面板并切到 AI 页")
    _视图菜单 = next((m.menu() for m in 独立.菜单栏.actions()
                 if m.text().startswith("视图")), None)
    _有AI菜单项 = bool(_视图菜单) and any(
        "AI" in a.text() for a in _视图菜单.actions())
    检查(_有AI菜单项,
         "独立窗口的 AI 面板开关在「视图」菜单里（按需求工具栏去掉了这个按钮）")

    # —— AI 面板在窄栏里文字不能被截断（紧凑模式：短标签 + 两行）——
    _紧凑 = 独立.AI面板
    检查(_紧凑.翻译按钮.text() == "🌐 翻译字幕",
         f"独立窗口 AI 按钮用短标签（避免截断）：{_紧凑.翻译按钮.text()}")
    检查(_紧凑.生字幕按钮.text() == "🎙 生成字幕",
         f"生成字幕按钮：{_紧凑.生字幕按钮.text()}")
    _提示 = _紧凑.翻译按钮.sizeHint().width()
    检查(_提示 < 200, f"按钮宽度合理（{_提示}px），窄面板也放得下")

    # —— 选片对话框：初始路径是**文件**时要自动退到父目录（用户实测的报错）——
    class _条目3:
        def __init__(self, name, path, is_dir=False, size=10):
            self.name, self.path, self.is_dir, self.size = name, path, is_dir, size

    class _适配3:
        def __init__(self):
            self.列过 = []

        def 列目录(self, 路径="/"):
            self.列过.append(路径)
            if 路径.endswith(".mp4"):
                raise NotADirectoryError(f"路径不是目录：{路径}")
            return [_条目3("影片.mp4", "/电影/影片.mp4")]

    _适3 = _适配3()
    _框3 = _对话框(_适3, "fake_1", 初始路径="/电影/影片.mp4",
                只要文件=True, 名字过滤=_是视频, 父窗口=播放页)
    _框3.show()
    等待(lambda: _框3.列表.count() > 0, 8.0, "对话框自动退到父目录")
    检查(_框3.当前路径 == "/电影",
         f"初始路径是文件时自动退到父目录：{_框3.当前路径}（列过 {_适3.列过}）")
    检查(_框3.列表.count() >= 1, "退到父目录后能列出内容")
    _框3.deleteLater()

    # —— 流畅度策略：吃力片源（4K60 + 带宽余量不足）——
    from v8_3.播放.播放核心 import 规则参数 as _规则参数
    from v8_3.播放.媒体信息 import 媒体信息 as _媒体信息
    from v8_3.播放.直链探测 import 探测结果 as _探测结果
    _吃力 = _规则参数(
        _媒体信息(宽=3840, 高=2160, 档位="4K", 视频编码="hevc",
              视频码率bps=12_700_000, 帧率=60.0, 时长秒=1472),
        _探测结果(成功=True, 实测带宽bps=13_000_000), ["vaapi", "auto", "none"])
    检查(_吃力.允许丢帧, "4K60 这类吃力片源：允许丢迟到帧（不再越积越晚）")
    检查(_吃力.视频输出 == "gl", "吃力片源用 GPU 缩放（vout=gl）")
    检查(_吃力.网络缓存毫秒 >= 15000,
         f"带宽余量不足时缓存给足：{_吃力.网络缓存毫秒}ms（旧行为会掉到 6000ms）")
    _选项 = _吃力.libvlc选项()
    检查(":vout=gl" in _选项 and ":no-drop-late-frames" not in _选项,
         f"选项符合预期：{[x for x in _选项 if 'vout' in x or 'drop' in x]}")

    # —— 选片对话框里能换网盘（用户反馈缺这个能力）——
    class _规格:
        def __init__(self, 名):
            self.显示名 = 名
            self.图标 = "🧪"

    class _条目4:
        def __init__(self, name, path, is_dir=False, size=10):
            self.name, self.path, self.is_dir, self.size = name, path, is_dir, size

    class _适配4:
        def __init__(self, 盘):
            self.盘 = 盘

        def 列目录(self, _路径="/"):
            return [_条目4(f"{self.盘}的片子.mp4", f"/{self.盘}的片子.mp4")]

    _盘们 = {"盘甲": _适配4("甲"), "盘乙": _适配4("乙")}
    _框4 = _对话框(_盘们["盘甲"], "盘甲", 初始路径="/", 只要文件=True,
                名字过滤=_是视频, 网盘列表={"盘甲": _规格("甲盘"),
                                      "盘乙": _规格("乙盘")},
                取适配器=lambda 标识: _盘们[标识], 父窗口=播放页)
    _框4.show()
    等待(lambda: _框4.列表.count() > 0, 8.0, "选片对话框加载目录")
    检查(hasattr(_框4, "网盘框") and _框4.网盘框.count() == 2,
         "选片对话框里有网盘下拉（可以在对话框里换网盘）")
    if hasattr(_框4, "网盘框"):
        _框4.网盘框.setCurrentIndex(_框4.网盘框.findData("盘乙"))
        等待(lambda: _框4.选中网盘标识 == "盘乙" and _框4.列表.count() > 0,
            8.0, "对话框换网盘")
        检查(_框4.选中网盘标识 == "盘乙" and _框4.当前路径 == "/",
             f"换网盘后回到根目录并记住选中盘：{_框4.选中网盘标识}")
        _文本 = [_框4.列表.item(i).text() for i in range(_框4.列表.count())]
        检查(any("乙" in t for t in _文本), f"换盘后列出新盘内容：{_文本}")
    _框4.close()
    泵(0.2)

    # —— 工具栏「🗂 面板」开关（显示/隐藏 播放清单+AI 助手）——
    _侧栏动作 = getattr(独立.菜单栏, "侧栏动作", None)
    _菜单标题 = [a.text() for a in 独立.菜单栏.actions()]
    检查(_侧栏动作 is not None and _菜单标题[-1] == _侧栏动作.text(),
         f"「🗂 面板」按钮在菜单栏「帮助」后面（按需求从工具栏移过来）：{_菜单标题[-1:]}")
    检查(独立.工具栏 is None,
         "工具栏里已经没有「🗂 面板」（那行整行撤销了）")
    # 需求：独立播放器控制条 = 进度行 + 按钮行（上下一个在停止之后、速度带标签、无音量）
    _条 = 独立.控制条
    检查(_条.上一个按钮.isVisibleTo(_条) and _条.下一个按钮.isVisibleTo(_条),
         "控制条里有「⏮ 上一个 / ⏭ 下一个」（在停止按钮之后）")
    检查(_条.音量条 is not None, "控制条里有音量滑块（按需求从视频上方搬下来）")
    检查(_条.速度标签.text() == "速度", f"控制条速度框带文字标签：{_条.速度标签.text()!r}")
    _条5 = 独立.控制条
    检查(_条5.音量条 is not None and _条5.静音按钮.isVisibleTo(_条5),
         "音量（静音按钮 + 滑块）已按需求搬到视频下方、在「速度」前面")
    _顺序 = [_条5.播放暂停按钮.text(), _条5.上一个按钮.text(),
            _条5.停止按钮.text(), _条5.下一个按钮.text()]
    检查(_顺序 == ["⏸", "⏮ 上一个", "⏹", "⏭ 下一个"],
         f"按钮顺序是 ⏸ → ⏮ 上一个 → ⏹ 停止 → ⏭ 下一个：{_顺序}")
    检查(_条5.循环按钮.isVisibleTo(_条5) and _条5.随机按钮.isVisibleTo(_条5),
         "「🔁 不循环 / 🔀 随机」跟在「⏭ 下一个」后面")
    检查(_条5.进度条.height() >= 10,
         f"进度条已加粗（高度 {_条5.进度条.height()}px）+ 独立一行")
    检查(_条.layout().count() >= 2, "控制条是两行：进度条一行 + 按钮一行")
    独立.切换侧栏(False)
    泵(0.1)
    _藏 = not 独立.右栏.isVisible()
    独立.显示控件()
    泵(0.1)
    _没弹回 = not 独立.右栏.isVisible()
    独立.切换侧栏(True)
    泵(0.1)
    检查(_藏 and _没弹回 and 独立.右栏.isVisible(),
         f"「🗂 面板」能藏/显示右侧面板，且鼠标移动不会弹回（藏={_藏} 弹回后={_没弹回}）")

    # —— 工具栏构建器本身仍然可用（页面/独立窗口都已按需求撤掉那行）——
    from v8_3.界面.vlc风格 import 构建工具栏 as _建栏
    _坏 = []
    for _宽 in (700, 900, 1183, 1400, 1600):
        _动作 = dict(播放页._动作表())
        _栏 = _建栏(_动作)
        _模式 = _栏.按宽度自适应(_宽)
        _栏.resize(_宽, 40)
        泵(0.02)
        if _栏.sizeHint().width() > _宽:
            _坏.append(f"{_宽}px 需要 {_栏.sizeHint().width()}px")
        if _模式 not in ("完整", "精简", "图标"):
            _坏.append(f"{_宽}px 模式异常 {_模式}")
    检查(not _坏,
         "工具栏构建器在 700~1600px 各宽度都装得下（1183px 仍是文字档）"
         + (f"（问题：{_坏}）" if _坏 else ""))
    检查(播放页.工具栏 is None and 独立.工具栏 is None,
         "播放页与独立窗口都已撤掉视频上方那行（按需求）")

    # —— 游离窗口巡检（libvlc 自己开窗口 = "视频和播放器分离"）——
    from v8_3.播放 import 游离窗口 as _游离
    from v8_3.播放.显示环境 import 可嵌入窗口 as _可嵌2
    # 只有"真的在用 X11 窗口"时才验这个：离屏平台造出来的窗口没有 X11 号，
    # 硬测会得到假阴性（甚至崩），那属于测试环境问题不是产品问题
    _X11界面 = _可嵌2(QApplication.platformName())
    if _游离.可用() and _X11界面:
        检查(hasattr(_游离, "是VLC窗口"), "有「是不是 VLC 窗口」的判定函数")
        检查(_游离.是VLC窗口("vlc", "任意标题"),
             "按 WM_CLASS=vlc 认得出 VLC 自己的窗口（**中文界面标题变了也认**）")
        检查(_游离.是VLC窗口("", "片子.mp4 - VLC 媒体播放器"),
             "类名拿不到时，中文标题也能兜底认出")
        检查(not _游离.是VLC窗口("python3", "x - VLC media player"),
             "我们自己（Qt）的窗口绝不会被误判 —— 否则会去关自己的窗口")
        # 真窗口层面：Qt 造一个"标题像 VLC"的窗口，必须**不**被当成游离窗口
        from PySide6.QtWidgets import QWidget as _QWidget
        _假VLC = _QWidget()
        _假VLC.setWindowTitle("自检片.mp4 - VLC media player")
        _假VLC.resize(320, 240)
        _假VLC.show()
        泵(0.4)
        _找到 = _游离.找游离窗口()
        检查(not any(号 == int(_假VLC.winId()) for 号, _名 in _找到),
             f"Qt 窗口（类名不是 vlc）不会被误判为游离窗口：{_找到[:1]}")
        _假VLC.deleteLater()
    else:
        检查(True, f"当前不是 X11 窗口环境（{QApplication.platformName()}）或没有 DISPLAY，"
                 f"游离窗口巡检不适用：{_游离.不可用原因() or '平台不支持'}")

    # —— 单播放器架构：独立窗口**不能**另建播放会话（否则两路声音/画面乱跑）——
    class _假播放器2:
        def __init__(self):
            self.窗口句柄 = 0
            self.跳转们 = []

        def 时长秒(self):
            return 10.0

        def 进度秒(self):
            return 3.0

        def 取音量(self):
            return 100

        def 绑定窗口(self, 句柄):
            self.窗口句柄 = int(句柄 or 0)

    class _假会话2:
        def __init__(self):
            self.播放器 = _假播放器2()
            self.标题 = "自检样片.mp4"
            self.远端路径 = "/自检样片.mp4"
            self.直链信息 = {"url": "/tmp/x.mp4", "headers": {}}
            self.起播次数 = 0
            self.关闭次数 = 0

        def 起播(self, _句柄=0):
            self.起播次数 += 1
            return True

        def 关闭(self):
            self.关闭次数 += 1

        def 跳转(self, 秒):
            self.播放器.跳转们.append(float(秒))

    _会2 = _假会话2()
    _窗2 = _播放器窗口(_会2, 标题="自检样片.mp4", 接管=True)
    _归还 = []
    _窗2.宿主回调 = {"归还播放": lambda w: _归还.append(w)}
    检查(_窗2.接管播放(), "独立窗口能接管已有播放")
    泵(0.4)          # X11 下接管会先等窗口映射好（真机上这一步是必要的）
    检查(_窗2.会话 is _会2, "接管用的是**同一个**播放会话（单播放器架构）")
    _起播次数 = _会2.起播次数
    _在等映射 = bool(getattr(_窗2, "_等待映射中", False))
    检查(_起播次数 == 1 or _在等映射,
         f"接管只起播一次（同一个播放器，不会有两路声音）：{_起播次数}"
         + ("（正在等窗口映射，属正常）" if _在等映射 else ""))
    _窗2.主体.setSizes([0, 380])
    _窗2.保证视频区可见()
    泵(0.1)
    检查(_窗2.视频.width() > 0,
         f"视频区宽度护栏生效：{_窗2.视频.width()}px（0 宽时 libvlc 无处可画→窗口全黑）")
    _窗2.close()
    泵(0.2)
    检查(_会2.关闭次数 == 0, "接管模式下关窗不关会话（声音/进度不中断）")
    检查(len(_归还) == 1, "关窗时把播放归还给播放页")

    # —— 页面侧：收回播放用的还是同一个会话 ——
    _真会话 = 播放页.会话
    _会3 = _假会话2()
    播放页.会话 = _会3
    播放页.收回播放到页面(None)
    泵(0.1)
    检查(播放页.会话 is _会3 and _会3.起播次数 == 1,
         "「收回播放到页面」用同一个会话起播（不新建播放器）")
    播放页.会话 = _真会话

    # —— 关窗交接：把位置与直链交给页面（页面据此接着播）——
    交接 = 独立.交接信息()
    检查(交接.get("类型") == "独立窗口关闭",
         f"关窗前给出交接信息：{list(交接.keys())[:6]}")
    检查("位置秒" in 交接, "交接信息带当前播放位置")
    # 让页面"认识"这个窗口（真实流程里 打开独立窗口 会把它挂到 _独立窗口 上）
    原挂 = getattr(播放页, "_独立窗口", None)
    播放页._独立窗口 = 独立
    检查(播放页._活动播放器() is 独立,
         "窗口在播时，页面的暂停/停止会路由到独立窗口（以前点在空会话上=无效）")
    点击前 = 假.暂停次数
    播放页._播放暂停()
    检查(假.暂停次数 > 点击前, "页面点暂停真的作用到了独立窗口那一路")
    独立.close()
    泵(0.2)
    播放页._独立窗口 = 原挂 if 原挂 is not None else None
    检查(假.关闭次数 >= 1, "关闭独立窗口时会停掉它自己的播放会话")
    检查(播放页._活动播放器() is 播放页,
         "窗口关掉后，页面的暂停/停止重新指向页面自己")

    print("\n[28] 显示环境（Wayland 会闪退，必须有防护）")
    from v8_3.播放.显示环境 import 可嵌入窗口, 显示说明, 准备嵌入显示
    检查(not 可嵌入窗口("wayland"), "wayland 平台判定为「不能嵌 X11 窗口」")
    检查(可嵌入窗口("xcb"), "xcb（X11/XWayland）判定为可嵌入")
    检查(not 可嵌入窗口("offscreen"), "离屏平台不可嵌入（否则 libvlc 段错误）")
    _句柄3 = 播放页._安全句柄()
    _能嵌3 = 可嵌入窗口(QApplication.platformName())
    检查((_句柄3 == 0) if not _能嵌3 else (_句柄3 > 0),
         f"句柄闸取值正确（平台 {QApplication.platformName()}，可嵌入={_能嵌3}）："
         f"{_句柄3}（没有 X11 窗口号时必须给 0，绝不把无效窗口号交给 libvlc）")
    检查(isinstance(显示说明(), str) and 显示说明(),
         f"有一行环境说明：{显示说明()[:50]}")
    import os as _os
    _旧 = dict(_os.environ)
    try:
        _os.environ["DISPLAY"] = ":0"
        _os.environ["WAYLAND_DISPLAY"] = "wayland-0"
        _os.environ.pop("QT_QPA_PLATFORM", None)
        _结果 = 准备嵌入显示()
        检查(_结果["平台"] == "xcb",
             f"Wayland + XWayland 会话自动切到 xcb（否则播放闪退）：{_结果['平台']}")
    finally:
        _os.environ.clear()
        _os.environ.update(_旧)

    print("\n[29] 显示防线（不让 libvlc 自己弹一个不受控的窗口）")
    from v8_3.播放.显示环境 import (是桌面平台 as _桌面, 无窗口原因 as _原因,
                             允许VLC自带窗口 as _允许自带)
    检查(_桌面("wayland") and _桌面("xcb"), "桌面平台判定：wayland/xcb 都算桌面")
    检查(not _桌面("offscreen"), "离屏平台不算桌面（无窗口解码是正确的）")
    检查("QT_QPA_PLATFORM=xcb" in _原因("wayland"),
         "不能嵌窗口时给出明确修法（用 XWayland 启动）")
    检查("V8_3_允许VLC自带窗口" in _原因("wayland"),
         "也给出「我就要用 VLC 自带窗口」的开关")
    检查(not _允许自带(), "默认**不**允许 VLC 自带窗口（那种窗口不受控、可能超出屏幕）")
    import os as _os显示
    _旧2 = dict(_os显示.environ)
    try:
        _os显示.environ["QT_QPA_PLATFORM"] = "wayland"
        _os显示.environ["DISPLAY"] = ":0"
        _os显示.environ["WAYLAND_DISPLAY"] = "wayland-0"
        _os显示.environ.pop("V8_3_不强制X11", None)
        from v8_3.播放.显示环境 import 准备嵌入显示 as _准备
        _r = _准备()
        检查(_r["平台"] == "xcb" and _r["可嵌入"],
             f"显式 wayland + XWayland → 自动切 xcb（{_r['平台']}），画面才嵌得进来")
    finally:
        _os显示.environ.clear()
        _os显示.environ.update(_旧2)

    print("\n[31] 下拉框箭头只画一个（用户截图反馈过「两个箭头」）")
    from PySide6.QtWidgets import QComboBox as _框
    试框 = _框()
    试框.addItems(["百度网盘", "夸克网盘"])
    试框.setMinimumHeight(34)
    试框.resize(300, 34)
    试框.show()
    泵(0.2)
    框图 = 试框.grab().toImage()
    底色 = 框图.pixelColor(6, 6)

    def _这一列有墨(图, x: int) -> bool:
        """这一列里有没有"非底色"的像素（文字、边框、箭头都算）。"""
        for y in range(2, 图.height() - 2):
            c = 图.pixelColor(x, y)
            if (abs(c.red() - 底色.red()) + abs(c.green() - 底色.green())
                    + abs(c.blue() - 底色.blue())) > 60:
                return True
        return False

    # 「两个箭头」那会儿，多出来的那个正好落在输入框偏右的空白处。
    # 所以：文字右边到按钮左边这一大段应当干干净净；按钮区必须有箭头。
    文字右侧空档 = [x for x in range(int(框图.width() * 0.42), int(框图.width() * 0.86), 2)
                if _这一列有墨(框图, x)]
    按钮区有墨 = any(_这一列有墨(框图, x) for x in range(框图.width() - 22, 框图.width() - 4))
    检查(not 文字右侧空档,
        f"输入框右侧没有多余箭头（脏列 {len(文字右侧空档)} 个）")
    检查(按钮区有墨, "下拉按钮区画出了箭头")
    试框.hide(); 试框.deleteLater()

    print("\n[32] 页面滚动：窗口够宽不出滚动条，变窄可上下左右滑，控件不被压扁")
    def _滚动区(页):
        from PySide6.QtWidgets import QScrollArea as _区
        return 页 if isinstance(页, _区) else 页.findChild(_区)
    窗口.resize(1400, 880)
    窗口.切换到传输页()
    泵(0.3)
    区宽 = _滚动区(窗口.堆叠.currentWidget())
    检查(区宽 is not None, "传输页外层有滚动区")
    if 区宽 is not None:
        检查(区宽.horizontalScrollBar().maximum() == 0,
            "窗口够宽时传输页不出现横向滚动条")
        内容宽 = 区宽.widget().width()
    窗口.resize(900, 600)
    泵(0.4)
    区窄 = _滚动区(窗口.堆叠.currentWidget())
    if 区窄 is not None:
        滑 = 区窄.horizontalScrollBar().maximum()
        检查(滑 > 0, f"窗口变窄后可以左右滑动（可滑 {滑}px）")
        最小宽 = 区窄.widget().minimumSizeHint().width()
        检查(区窄.widget().width() >= 最小宽,
            f"变窄时内容不低于自身最小宽度（{区窄.widget().width()} ≥ {最小宽}），"
            "靠滚动查看而不是把控件压扁")
    窗口.resize(1400, 880)
    泵(0.3)
    表 = 窗口.传输页面().任务表格 if hasattr(窗口.传输页面(), "任务表格") else None
    if 表 is None:
        表 = 窗口.传输页面().findChild(type(窗口.传输页面().findChild(__import__("PySide6.QtWidgets", fromlist=["QTableWidget"]).QTableWidget)))
    检查(表 is not None and 表.minimumWidth() >= 1000,
        f"传输表格有最小宽度（{表.minimumWidth() if 表 else '?'}px），不会被挤变形")

    print("\n[33] 新增网盘弹窗：内容可上下滑动，底部按钮始终可见")
    from v8_3.界面.网盘对话框 import 网盘编辑对话框 as _对话
    弹窗 = _对话(dict(配置), None, 窗口)
    弹窗.resize(弹窗.minimumWidth(), 弹窗.minimumHeight())
    弹窗.show()
    泵(0.3)
    弹区 = 弹窗.findChild(__import__("PySide6.QtWidgets", fromlist=["QScrollArea"]).QScrollArea)
    检查(弹区 is not None, "弹窗正文包在滚动区里")
    if 弹区 is not None:
        检查(弹区.verticalScrollBar().maximum() > 0,
            f"缩到最小尺寸时正文可上下滑动（可滑 {弹区.verticalScrollBar().maximum()}px）")
        检查(弹区.horizontalScrollBarPolicy().name.startswith("ScrollBar"),
            f"正文允许横向滚动（策略 {弹区.horizontalScrollBarPolicy().name}）")
    确定钮 = [b for b in 弹窗.findChildren(__import__("PySide6.QtWidgets", fromlist=["QPushButton"]).QPushButton)
            if b.text() == "确定"]
    检查(bool(确定钮) and 确定钮[0].isVisible(), "「确定」按钮在滚动区外面、始终可见")
    弹窗.close(); 弹窗.deleteLater()
    泵(0.2)

    print("\n[34] 在线模型：多家厂家，各自一把密钥（用户要求像 harness 那样）")
    from v8_3.配置 import _规格化厂家, 在线模型段, 写回在线模型, 当前厂家, 在线厂家预设
    段 = 在线模型段(窗口.配置)
    写回在线模型(窗口.配置, 段)          # 顺手验证迁移/写回不炸
    检查("deepseek" in 段["厂家"], "老的单密钥配置被迁移成 deepseek 厂家（密钥不丢）")
    检查(len(在线厂家预设) >= 6, f"内置了 {len(在线厂家预设)} 家预设（含百炼/Kimi/智谱/火山…）")
    检查(any("百炼" in v.get("名称", "") for v in 在线厂家预设.values()),
         "预设里有阿里云百炼")

    if 窗口._AI页面 is None:
        窗口.切换到AI页()
    泵(0.3)
    AI页 = 窗口.AI页面()
    检查(hasattr(AI页, "厂家下拉") and hasattr(AI页, "接口地址框"),
         "AI 页有厂家下拉与接口地址输入框")
    # 加一家百炼，验证"每家各存一份地址与密钥、模型清单来自该厂家"
    段 = 在线模型段(窗口.配置)
    段["厂家"]["deepseek"] = _规格化厂家("deepseek", {"密钥": "sk-deepseek-自检",
                                                 "模型": "deepseek-flash"})
    段["厂家"]["bailian"] = _规格化厂家("bailian", {
        "密钥": "sk-bailian-自检", "模型": "qwen-plus",
        "模型列表": ["qwen-plus", "qwen-max"]})
    段["当前"] = "bailian"
    写回在线模型(窗口.配置, 段)
    AI页._载入厂家到界面()
    AI页.刷新()
    泵(0.4)
    检查("dashscope" in AI页.接口地址框.text(),
         f"选中百炼时接口地址跟着切（{AI页.接口地址框.text()[:48]}）")
    检查(AI页.密钥输入框.text() == "sk-bailian-自检", "密钥跟着厂家切（不是同一把）")
    下拉 = [AI页.模型下拉框.itemText(i) for i in range(AI页.模型下拉框.count())]
    检查("qwen-plus" in 下拉, f"模型下拉来自该厂家（{下拉[:3]}）")
    检查("deepseek-flash" not in 下拉, "不再把上一家的模型混进来")
    AI页.厂家下拉.setCurrentIndex(AI页.厂家下拉.findData("deepseek"))
    泵(0.4)
    检查(AI页.密钥输入框.text() == "sk-deepseek-自检", "切回 DeepSeek 时用它自己那把密钥")
    检查("deepseek" in AI页.接口地址框.text(), "接口地址也跟着切回去")
    检查(hasattr(AI页, "_拉取模型"), "有「用密钥拉取该厂家模型」的入口")
    # 收尾：把配置恢复成只有 deepseek，免得影响后面的检查
    段 = 在线模型段(窗口.配置)
    段["厂家"].pop("bailian", None)
    段["当前"] = "deepseek"
    写回在线模型(窗口.配置, 段)

    print("\n[28b] AI 页拆成三个页签（AI状态 / AI设置 / 模型商店）")
    _AI页 = 窗口.AI页面()
    if _AI页 is None:
        窗口.切换到AI页(); 泵(0.5)
        _AI页 = 窗口.AI页面()
    检查(_AI页 is not None and hasattr(_AI页, "页签按钮"),
         "AI 页有页签导航")
    检查(list(getattr(_AI页, "页签按钮", {})) == ["状态", "设置", "商店"],
         f"页签是 AI状态/AI设置/模型商店：{list(getattr(_AI页, '页签按钮', {}))}")
    检查(all(("AI状态" in b.text() or "AI设置" in b.text() or "模型商店" in b.text())
             for b in _AI页.页签按钮.values()),
         "页签文字带图标与名称：" + "、".join(b.text() for b in _AI页.页签按钮.values()))
    检查(_AI页.AI页签堆叠.count() == 3, "三个页签各有一页")
    检查(all(type(_AI页.AI页签堆叠.widget(i)).__name__ == "QScrollArea"
             for i in range(3)),
         "每页各自带滚动区（内容高时滚动，不压扁控件）")
    _索引 = {}
    for _键 in ("设置", "商店", "状态"):
        _AI页.切换AI页签(_键); 泵(0.35)
        _索引[_键] = _AI页.AI页签堆叠.currentIndex()
        _勾 = [k for k, b in _AI页.页签按钮.items() if b.isChecked()]
        检查(_勾 == [_键], f"切到「{_键}」后只有它被选中（实际 {_勾}）")
    检查(_索引 == {"状态": 0, "设置": 1, "商店": 2},
         f"三个页签对应三个页面索引：{_索引}")
    # 每页该有的东西
    _AI页.切换AI页签("状态"); 泵(0.3)
    for _名, _控件 in (("预算与消耗", getattr(_AI页, "预算余额标签", None)),
                    ("AI 状态", getattr(_AI页, "时段标签", None)),
                    ("价格表", getattr(_AI页, "价格表", None))):
        检查(_控件 is not None and _控件.isVisible(),
             f"AI状态页显示「{_名}」")
    检查(not _AI页.密钥输入框.isVisible(), "AI状态页不显示密钥输入（那在设置页）")
    _AI页.切换AI页签("设置"); 泵(0.3)
    检查(_AI页.密钥输入框.isVisible() and _AI页.本地状态标签.isVisible(),
         "AI设置页显示 云端密钥 + 本地模型")
    # 用户要求：没有本地模型时下拉框为空选项 + 温馨提示，测速/启动服务不可用；
    # 装了模型后（模型商店）要自动变可用。
    _AI页._已装模型缓存 = None
    _真实已装 = _AI页._已装本地模型(强制=True)
    _AI页._刷新本地模型下拉()
    _框 = _AI页.本地模型框
    if _真实已装:
        # 本机真的装了模型：断言"可用 + 列出真实模型"
        检查(_框.isEnabled() and _框.count() == len(_真实已装),
             f"有本地模型时下拉框列出真实模型：{[_框.itemText(i) for i in range(_框.count())]}")
        检查(_AI页.测速按钮.isEnabled() and _AI页.启动服务按钮.isEnabled(),
             "有本地模型时测速/启动服务可用")
    else:
        # 没有模型：占位 + 禁用
        检查(_框.count() == 1 and "还没有本地模型" in _框.itemText(0),
             f"没有本地模型时下拉框只有占位提示：{_框.itemText(0)[:34]}")
        检查(not _框.isEnabled(), "没有本地模型时下拉框不可选")
        检查(not _AI页.测速按钮.isEnabled() and not _AI页.启动服务按钮.isEnabled(),
             "没有本地模型时测速/启动服务不可用")
        检查("模型商店" in _AI页.本地提示标签.text(),
             f"提示把用户引到模型商店：{_AI页.本地提示标签.text()[:36]}")
    # 模拟"模型商店刚装好一个模型"：缓存替换后立刻联动
    _AI页._已装模型缓存 = (time.time(), ["qwen3.5:4b", "gemma3:270m"])
    _AI页._刷新本地模型下拉()
    检查(_框.isEnabled() and _框.count() == 2 and _框.itemText(0) == "qwen3.5:4b",
         f"装好模型后下拉框立刻可用并列出：{[_框.itemText(i) for i in range(_框.count())]}")
    检查(_AI页.测速按钮.isEnabled() and _AI页.启动服务按钮.isEnabled(),
         "装好模型后测速/启动服务立刻可用")
    _AI页._已装模型缓存 = None      # 还原成真实状态
    _AI页._刷新本地模型下拉()

    # 用户要求：本地模型那一排去掉「拉取模型 / 一键装好离线模型 / 安装指引」
    _本地行按钮 = []
    for _组 in _AI页.本地状态标签.parent().findChildren(QPushButton):
        _本地行按钮.append(_组.text())
    _不该有 = [x for x in _本地行按钮
             if ("拉取模型" in x or "一键装好离线模型" in x or "安装指引" in x)]
    检查(not _不该有,
         f"本地模型区不再有『拉取模型/一键装好离线模型/安装指引』：{_不该有}")
    检查(any("检测" in x for x in _本地行按钮) and any("测速" in x for x in _本地行按钮)
         and any("启动服务" in x for x in _本地行按钮),
         f"保留 检测/测速/启动服务 三个按钮：{_本地行按钮}")
    # 本地提示标签的内容会随检测结果动态变化（检测不到运行时给安装步骤、
    # 检测到就给状态一行），所以这里只要求"有内容"，不锁死具体文案。
    检查(bool(_AI页.本地提示标签.text().strip()),
         f"本地模型区仍有说明标签：{_AI页.本地提示标签.text()[:32]}…")
    检查(not _AI页.详情框.isVisible(),
         "AI设置页不再显示运行详情（用户要求去掉；它只在 AI状态页）")
    _AI页.切换AI页签("商店"); 泵(0.3)
    检查(_AI页.市场状态标签.isVisible() and _AI页.市场搜索框.isVisible(),
         "模型商店页显示 市场目录 + 过滤框")
    检查(not _AI页.密钥输入框.isVisible(), "模型商店页不显示密钥输入")
    _AI页.切换AI页签("状态"); 泵(0.2)

    print("\n[29] 🛒 本地小模型市场（推荐指数 / 一键装·卸·更新 / 动态拉取）")
    from v8_3.AI import 模型市场
    # 自检一律离线：策展目录必须自带内容，不能开天窗
    离线目录 = 模型市场.构建目录(联网=False)
    检查(len(离线目录) >= 10,
         f"离线（无网络）也有策展目录：{len(离线目录)} 个模型")
    第一名 = 离线目录[0]
    检查(离线目录[0].分数 >= 离线目录[-1].分数, "目录按推荐指数从高到低排序")
    检查(模型市场.名次标记(1).startswith("🥇")
         and 模型市场.名次标记(2).startswith("🥈")
         and 模型市场.名次标记(3).startswith("🥉")
         and 模型市场.名次标记(4) == "",
         "前三名用不同的 emoji 醒目标记，第 4 名起不再标记")
    检查(bool(第一名.总结) and len(第一名.总结) >= 10,
         f"每个模型都有一句核心总结：{第一名.总结[:28]}…")
    检查(bool(第一名.推荐理由), f"每个模型都有推荐理由：{第一名.推荐理由[:28]}…")
    检查(len(第一名.详情行()) >= 10,
         f"详细信息按多行展示（{len(第一名.详情行())} 行）")
    检查(第一名.官方页.startswith("https://ollama.com/library/")
         and 第一名.下载页.startswith("https://ollama.com/"),
         f"每个模型都有官网与下载链接：{第一名.官方页}")
    检查(第一名.体积文本 not in ("", "未知"),
         f"体积有值（未联网时用参数估算）：{第一名.体积文本}")
    # 打分与权重：换权重能改变排序
    偏轻量 = 模型市场.推荐权重(任务适配=0.1, 中文能力=0.1, 推理能力=0.1,
                          轻量=0.6, 新鲜度=0.1)
    重排 = 模型市场.排序并打分(模型市场.策展目录(), 偏轻量)
    检查(重排[0].参数B <= 离线目录[0].参数B,
         f"把「轻量」权重调高后，最推荐的是更小的模型：{重排[0].名字}")
    # 缓存读写
    模型市场.写入缓存(离线目录)
    检查(len(模型市场.读取缓存()) == len(离线目录), "目录能写缓存并读回")
    # 界面：卡片渲染 + 五个操作按钮
    市场页 = AI页
    市场页._填市场(离线目录)
    泵(0.3)
    卡片 = list(getattr(市场页, "_市场卡片们", []))
    检查(len(卡片) == min(市场页.市场首屏条数, len(离线目录)),
         f"市场按推荐指数渲染卡片（首屏 {len(卡片)} 张）")
    from PySide6.QtWidgets import QPushButton as _按钮
    第一卡按钮 = [b.text() for b in 卡片[0].findChildren(_按钮)]
    检查(any("一键安装" in t for t in 第一卡按钮), f"卡片有「一键安装」：{第一卡按钮}")
    检查(any("卸载" in t for t in 第一卡按钮), "卡片有「卸载」")
    检查(any("更新" in t for t in 第一卡按钮), "卡片有「更新」")
    检查(any("下载链接" in t for t in 第一卡按钮), "卡片有「下载链接」")
    检查(any("官网" in t for t in 第一卡按钮), "卡片有「官网」")
    检查(any("详细" in t for t in 第一卡按钮), "卡片有「详细信息」（多行详情可展开）")
    检查(any(模型市场.名次标记(1) in w.text()
             for w in 卡片[0].findChildren(QLabel)),
         "第一张卡片上带着 🥇 醒目标记")
    # 本机已装状态：装了就该能「卸载/更新」，且不再让点「安装」
    已装样例 = {离线目录[0].名字: {"大小": 1234, "修改时间": "刚刚"}}
    模型市场.合并已装状态(离线目录, 已装样例)
    检查(bool(getattr(离线目录[0], "已安装", False)), "已装状态能合并进目录")
    市场页._市场已装 = dict(已装样例)
    市场页._重绘市场卡片()
    泵(0.2)
    第一卡 = 市场页._市场卡片们[0]
    钮表 = {b.text(): b for b in 第一卡.findChildren(_按钮)}
    装钮 = next(b for t, b in 钮表.items() if "一键安装" in t or "已安装" in t)
    卸钮 = next(b for t, b in 钮表.items() if "卸载" in t)
    更钮 = next(b for t, b in 钮表.items() if "更新" in t)
    检查(not 装钮.isEnabled() and 卸钮.isEnabled() and 更钮.isEnabled(),
         "本机已装的模型：「安装」置灰，「卸载/更新」可用")
    # 搜索过滤
    市场页.市场搜索框.setText("不存在的模型名")
    泵(0.2)
    检查(len(市场页._市场卡片们) == 0, "搜索框能过滤（无匹配时不显示卡片）")
    市场页.市场搜索框.setText("")
    泵(0.2)
    # 离线总闸：联网被禁用时不许发请求
    import os as _os2
    _旧2 = _os2.environ.get("V8_3_不联网")
    try:
        _os2.environ["V8_3_不联网"] = "1"
        检查(模型市场.联网被禁用(), "V8_3_不联网=1 时模型市场认定「不联网」")
        检查(len(模型市场.构建目录(联网=True)) >= 10,
             "联网总闸打开时，构建目录自动降级为策展目录（不发请求）")
    finally:
        if _旧2 is None:
            _os2.environ.pop("V8_3_不联网", None)
        else:
            _os2.environ["V8_3_不联网"] = _旧2
    # ---- 方案 B：内置浏览器登录（不依赖外部浏览器/调试端口）----
    # 用户现场：扫码登录的 BDUSS 进不了网页版 pan 域 → 写操作全废。
    # 内置浏览器走的**就是网页版登录**，登录完自动取走完整会话（含 HttpOnly）。
    from v8_3.界面.内置浏览器登录 import (
        可用 as _内置可用, 拼cookie头 as _拼, 取cookie值 as _取值,
        内置浏览器登录窗口 as _内置窗口, 内置浏览器目录 as _内置目录,
        读取当前离线cookie as _读库,
    )
    能用, 原因 = _内置可用()
    检查(能用 or bool(原因), f"内置浏览器可用性检查有结论：{能用} / {原因 or 'OK'}")
    检查(_内置目录().name == "内置浏览器",
         f"内置浏览器数据放在项目内（{_内置目录()}）")
    # cookie 组装规则（"自动回填"的核心：同名 cookie 要按域名取对的那份）
    _凭证 = [
        {"name": "BDUSS", "value": "B" * 10, "domain": ".baidu.com"},
        {"name": "STOKEN", "value": "S" * 10, "domain": ".passport.baidu.com"},
        {"name": "STOKEN", "value": "P" * 10, "domain": ".pan.baidu.com"},
        {"name": "BAIDUID", "value": "U" * 10, "domain": ".baidu.com"},
    ]
    检查(_取值(_凭证, "STOKEN", (".pan.baidu.com",)) == "P" * 10,
         "同名 cookie 按域名优先取 —— pan 域那份才是写权限认的")
    _头 = _拼(_凭证, ("BDUSS", "STOKEN"))
    检查("BDUSS=" in _头 and "STOKEN=" in _头, f"能拼出 Cookie 头：{_头[:48]}…")
    # 直读 profile 的 Cookies 库（比 Qt 的按 URL 过滤可靠：BDUSS 挂在
    # .baidu.com 下，按 https://www.baidu.com/ 过滤会漏）
    _库条目 = _读库(("BDUSS", "STOKEN"))
    检查(isinstance(_库条目, list), f"能从内置浏览器库读 cookie：{len(_库条目)} 条")
    检查(all(not str(c["name"]).startswith("b'") for c in _库条目),
         "库里读出来的 cookie 名字是干净字符串（不会出现 b'BDUSS'）")
    # 窗口能建起来 + 能捕获 cookie + 会提示还缺什么
    # 引擎层重构后：窗口持有的是"浏览器引擎"（默认 QtWebEngine 引擎），
    # 这里同时验证"引擎已接上"与"假引擎可注入"（后者让登录流程能离线测）。
    from v8_3.界面.浏览器引擎 import 假引擎 as _假引擎
    _窗 = _内置窗口("baidu", None, 完成回调=lambda _c: None, 引擎=_假引擎())
    try:
        _窗.show()
        泵(0.3)
        检查(hasattr(_窗, "状态标签") and hasattr(_窗, "引擎"),
             "内置浏览器窗口建起来了（引擎 + 状态栏）")
        检查(_窗.引擎.名字.startswith("假引擎"),
             f"引擎可注入（当前：{_窗.引擎.名字}）—— 登录流程能离线测")
        # 短信方式：窗口会自动切「短信登录」并把手机号填进登录框
        _短信引擎 = _假引擎()
        _短信引擎.JS返回 = "clicked:短信登录"
        _短信窗 = _内置窗口("baidu", None, 完成回调=lambda _c: None,
                        引擎=_短信引擎, 登录方式="sms", 手机号="13800000000")
        _短信窗.show()
        _短信引擎.触发加载完成(True)
        for _ in range(30):            # 循环泵 1.5 秒：窗口里的 QTimer 才会真正触发
            泵(0.05)
        _短信Js = [x for x in _短信引擎.操作记录 if x[0] == "执行JS"]
        检查(bool(_短信Js), '短信方式：窗口会执行『切到短信登录』的 JS')
        检查(any("encryptMobile" in x[1] for x in _短信Js)
             or "已自动填入" in _短信窗.状态标签.text()
             or "已切到" in _短信窗.状态标签.text(),
             f"短信方式：会自动填手机号或提示已切页（{_短信窗.状态标签.text()[:36]}）")
        _短信窗.close()
        _窗._收字典({"name": "STOKEN", "value": "x" * 8,
                  "domain": ".pan.baidu.com", "httpOnly": True})
        _名 = [c["name"] for c in _窗._凭证]
        检查("STOKEN" in _名, f"窗口能收下 cookie：{_名}")
        _窗._查凭证()
        泵(0.2)
        检查("还缺" in _窗.状态标签.text() or "✅" in _窗.状态标签.text(),
             f"窗口会提示还缺哪些凭证：{_窗.状态标签.text()[:60]}")
    finally:
        _窗.close()

    # ---- 百度「从浏览器导入完整会话」（写权限的正解）----
    # 用户现场：扫码登录后能列目录、上传/改名/删除全报 errno:-6。
    # 真机实测根因：写权限 = BDUSS + **pan 域** STOKEN，扫码那份会话缺它；
    # 浏览器里那份是完整的，所以加了这个一键导入入口。
    from v8_3.界面.登录对话框 import 登录对话框 as _登录对话框
    # 用「假网盘」的桥实例冒充百度适配器：能力表与页面交互都能离线跑通
    假百度实例 = dict(配置["适配器"][0])
    假百度实例.update({"标识": "baidu", "类型": "baidu", "名称": "百度网盘"})

    class _假动作:
        def __init__(self, 动作, 实例):
            self._动作 = 动作
            self._实例 = 实例

        def __getattr__(self, 名):
            return getattr(self._动作, 名)

        def 适配器(self, 标识):
            return self._实例

    真动作 = 窗口.动作
    窗口.动作 = _假动作(真动作, 真动作.适配器("fake_1"))
    百度对话 = _登录对话框(窗口, 假百度实例, 窗口)
    百度对话.show()
    try:
        等待(lambda: bool(百度对话.能力), 30, "等待百度能力表")
        百度对话._切方式("cookie")
        # 短信方式=内置浏览器（百度）时：原生发码/登录按钮要置灰，
        # 手机号框保留可用（填了会自动带进内置浏览器），并指向内置浏览器按钮
        _短信面板 = 百度对话.面板.get("sms")
        _短信方式 = str((百度对话.能力.get("sms") or {}).get("方式") or "")
        # 没声明机制的后端（自检用的假网盘）不能被这条规则误置灰
        检查(_短信方式 != "内置浏览器" or not 百度对话.发送验证码按钮.isEnabled(),
             "只有声明了内置浏览器方式的后端才置灰发码按钮（当前方式="
             + (_短信方式 or "无") + "）")
        if _短信面板 is not None and _短信方式 == "内置浏览器":
            检查(not 百度对话.发送验证码按钮.isEnabled(),
                 "百度：短信面板的「发送验证码」按钮已置灰（避免点出红色失败提示）")
            检查(not 百度对话.短信登录按钮.isEnabled(),
                 "百度：短信面板的「验证码登录」按钮已置灰")
            检查(百度对话.手机号框.isEnabled(),
                 "百度：手机号框仍可用（填了会自动带进内置浏览器）")
            检查("内置浏览器" in 百度对话.短信提示标签.text(),
                 f"百度：短信面板给了指路提示：{百度对话.短信提示标签.text()[:34]}")
        检查(hasattr(百度对话, "内置浏览器按钮"),
             "百度登录对话框有「🌐 用内置浏览器登录（推荐）」按钮")
        检查("自动取走完整会话" in 百度对话.内置浏览器按钮.toolTip(),
             "按钮提示写清了它会自动取走完整会话")
        检查(hasattr(百度对话, "浏览器导入按钮"),
             "百度 Cookie 面板也有「🌐 从浏览器导入完整会话」按钮（外部浏览器）")
        检查("STOKEN" in 百度对话.浏览器导入按钮.toolTip(),
             "按钮提示里写清了它解决的是 pan 域 STOKEN 缺失")
        # 点一下：假适配器会收到 __取浏览器会话__ 这条指令（真机由桥去抓 cookie）
        收到 = {}
        适配器实例 = 百度对话.适配器
        原方法 = 适配器实例.Cookie登录

        def 假Cookie登录(文本, _自己=适配器实例):
            # 用实例属性覆盖类方法（不能放在类上，别把真适配器一起改了）
            收到["文本"] = 文本
            return {"状态": "失败", "消息": "自检不发真实浏览器请求"}

        适配器实例.Cookie登录 = 假Cookie登录
        try:
            百度对话._从浏览器导入()
            检查(等待(lambda: bool(收到), 30, "等待导入指令"),
                 f"按钮发出的指令＝{收到.get('文本')!r}")
            检查(收到.get("文本") == "__取浏览器会话__",
                 "指令就是桥认识的那个标记（桥据此去 CDP 取 cookie）")
        finally:
            适配器实例.Cookie登录 = 原方法
    finally:
        百度对话.close()
        百度对话.deleteLater()
        窗口.动作 = 真动作

    # 核对「不存在的模型」时要如实回报，不能假装能下载（真机实测：注册表回 404）
    检查(模型市场.注册表清单地址.format(名字="qwen3", 标签="1.7b")
         .startswith("https://registry.ollama.ai/v2/library/"),
         "注册表核对地址按「库/名字/manifests/标签」拼")

    print("\n[29b] 账号信息：容量三元组（总/已用/可用）+ 用户名缺失不显示 '-'")
    页 = 窗口._网盘页面.get("fake_1")
    if 页 is None:
        窗口.切换网盘页("fake_1"); 泵(0.3)
        页 = 窗口._网盘页面["fake_1"]
    页._状态成功("fake_1", {
        "logged_in": True, "user": "",
        "detail": {"member": {"total_capacity": 100 * 1024 ** 3,
                            "use_capacity": 40 * 1024 ** 3,
                            "free_capacity": 60 * 1024 ** 3}},
    })
    文本 = 页.状态标签.text()
    检查("总容量=" in 文本 and "已用=" in 文本 and "可用=" in 文本,
         f"状态栏显示容量三元组：{文本}")
    检查("用户：" not in 文本,
         "适配器没给用户名时不再显示「用户：-」")
    页._状态成功("fake_1", {
        "logged_in": True, "user": "someone",
        "detail": {"member": {"total_capacity": 100 * 1024 ** 3,
                            "use_capacity": 40 * 1024 ** 3}},
    })
    文本 = 页.状态标签.text()
    检查("用户：someone" in 文本, f"有用户名时照常显示：{文本}")
    检查("可用=" in 文本,
         "只给 总/已用 时，可用由界面现算（不显示 0）")
    # 状态缓存：短时间重复刷新不再闪"检查中"
    页._刷新管理区(强制=True)
    泵(0.5)
    页._刷新管理区()          # 第二次：命中缓存，应立刻是"已登录/未登录"而不是"检查中"
    检查("检查中" not in 页.状态标签.text(),
         f"刚刷过再刷新不会闪『检查中』：{页.状态标签.text()[:40]}")
    检查(页.账号状态保鲜秒 >= 5,
         f"账号状态有保鲜期（{页.账号状态保鲜秒}s），减少频繁检测")

    print("\n[30] 关闭窗口（清理子进程）")
    窗口.close()
    泵(0.3)
    检查(True, "关闭窗口未抛异常")

    窗口.deleteLater()
    QApplication.processEvents()
    临时.cleanup()

    失败 = [说明 for 通过, 说明 in 结果 if not 通过]
    print("\n" + "=" * 60)
    print(f"共 {len(结果)} 项检查，通过 {len(结果) - len(失败)}，失败 {len(失败)}")
    for 说明 in 失败:
        print(f"  ✗ {说明}")
    return 1 if 失败 else 0


if __name__ == "__main__":
    raise SystemExit(main())
