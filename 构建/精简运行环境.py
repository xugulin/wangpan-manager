"""压缩发布包里的 Python 运行环境（在**打包流程内部**调用，不动本地开发环境）。

为什么要有它
============
绿色版把整个 ``运行环境/venv`` 拷进包里，而 venv 里天生带着大量"开发时才用得上"
的东西（类型存根、Qt 开发工具、305 个语言包的 Qt 翻译、QML/3D/Designer 这些
本项目用不到的 Qt 模块……）。实测这些能占掉上百 MB，而用户一个都用不到。

删什么（稳妥档）
================
1. ``*.pyi`` / ``*.pyi.in``：类型存根，只在写代码时给 IDE 用；
2. ``__pycache__`` / ``*.pyc``：字节码缓存，首次运行自己会生成；
3. Qt 开发工具与文档：``assistant`` / ``designer`` / ``linguist`` / ``qmlls`` /
   ``qmlformat`` / ``lupdate`` / ``lrelease`` / ``include`` / ``typesystems`` /
   ``metatypes`` / ``qml`` / ``libexec``；
4. Qt 翻译：305 个 ``.qm`` 只留中/英（本项目界面自带中文，不需要 Qt 帮我们翻译）；
5. 本项目用不到的 Qt 模块（Python 绑定 + 对应 ``libQt6*.so`` + 插件）：
   QML/Quick、Designer、Pdf、3D、Charts、DataVisualization、Graphs、Multimedia、
   WebView、WebChannel、Bluetooth、Nfc、RemoteObjects、Sensors、SerialPort、
   Location/Positioning、SpatialAudio、TextToSpeech、Scxml、StateMachine、
   Test、Help、UiTools、Sql、Concurrent、OpenGL 之外的一堆 Quick* 组件。

**保留**（删了会出事）
======================
* 本项目真正用到的：QtCore / QtGui / QtWidgets / QtNetwork / QtSvg / QtSvgWidgets /
  QtWebEngineCore / QtWebEngineWidgets / QtWebEngineQuick（内置浏览器登录要用）；
* 平台与图形插件：``plugins/platforms``（xcb/wayland 就靠它）、``platformthemes``、
  ``imageformats``、``iconengines``、``tls``、``networkinformation``、``generic``；
* ``libicudata/libicui18n/libicuuc``：Qt 内部用它做 Unicode，删了中文可能出问题；
* libvlc 相关（在项目自己的 ``运行环境/播放`` 里，不在 venv）。

用法
====
    # 单测/自查（只会打印将删除什么）
    运行环境/venv/bin/python 构建/精简运行环境.py --试运行 某个目录

    # 打包流程内部（构建/打包.py 会调）
    from 精简运行环境 import 精简运行环境
    精简运行环境(顶层目录, 平台="linux")

也可以在**项目根**上直接跑（就地精简本地 venv，慎用）：
    运行环境/venv/bin/python 构建/精简运行环境.py --就地
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

#: 用不到的 Qt 模块（Python 绑定名，不带 .abi3.so）
#:
#: ⚠️ 别把 QtWebChannel / QtWebSockets / QtPositioning / QtWebView 加进来：
#:    「内置浏览器登录」用的 QtWebEngine **依赖**它们 —— 实测删掉后
#:    `from PySide6 import QtWebEngineCore` 会报
#:    "libshiboken: could not import module 'PySide6.QtWebChannel'"。
用不到的Qt模块 = (
    "Qt3DAnimation", "Qt3DCore", "Qt3DExtras", "Qt3DInput", "Qt3DLogic",
    "Qt3DRender", "QtBluetooth", "QtCharts", "QtConcurrent", "QtDataVisualization",
    "QtDesigner", "QtGraphs", "QtGraphsWidgets", "QtHelp", "QtLocation",
    "QtMultimedia", "QtMultimediaWidgets", "QtNfc",
    "QtOpenGLWidgets", "QtPdf", "QtPdfWidgets", "QtQml",
    "QtQuick", "QtQuick3D", "QtQuickControls2", "QtQuickTest", "QtQuickWidgets",
    "QtRemoteObjects", "QtScxml", "QtSensors", "QtSerialBus", "QtSerialPort",
    "QtSpatialAudio", "QtSql", "QtStateMachine", "QtTest", "QtTextToSpeech",
    "QtUiTools",
)

#: 用不到的 Qt 开发工具目录
用不到的Qt目录 = ("assistant", "designer", "linguist", "qmlls", "qmlformat",
            "qmltestrunner", "lupdate", "lrelease", "include", "typesystems",
            "metatypes", "qml", "libexec", "scripts", "glue", "support",
            "examples", "doc")

#: 用不到的 Qt 插件目录
#: 用不到的 Qt 插件目录（同样别动 webview / position / location —— WebEngine 要用）
用不到的Qt插件 = ("assetimporters", "canbus", "designer", "geometryloaders",
            "playlistformats", "qmltooling", "renderers",
            "sceneparsers", "scxmldatamodel", "sensors", "texttospeech",
            "virtualkeyboard", "multimedia", "sqldrivers",
            "mediaservice", "3drender", "3dinput", "3dlogic",
            "3danimation", "3dextras", "graphviz", "networkaccess")

#: Qt 翻译要保留的语言（其余 300 个全删）
保留翻译 = ("zh_CN", "zh_TW", "en")

结果统计: dict[str, int] = {}


def _删(路径: Path, 试运行: bool, 分类: str) -> int:
    """删掉一个文件/目录，返回释放的字节数（试运行时只统计）。"""
    大小 = 0
    try:
        if 路径.is_dir() and not 路径.is_symlink():
            for 子 in 路径.rglob("*"):
                if 子.is_file() and not 子.is_symlink():
                    大小 += 子.stat().st_size
        elif 路径.is_file() or 路径.is_symlink():
            大小 = 路径.lstat().st_size
    except Exception:
        return 0
    if not 试运行:
        try:
            if 路径.is_dir() and not 路径.is_symlink():
                shutil.rmtree(路径, ignore_errors=True)
            else:
                路径.unlink(missing_ok=True)
        except Exception:
            return 0
    结果统计[分类] = 结果统计.get(分类, 0) + 大小
    return 大小


def _精简站点目录(站包目录: Path, 试运行: bool) -> None:
    if not 站包目录.is_dir():
        return
    # ① 类型存根
    for 路径 in 站包目录.rglob("*.pyi"):
        _删(路径, 试运行, "类型存根(.pyi)")
    for 路径 in 站包目录.rglob("*.pyi.in"):
        _删(路径, 试运行, "类型存根(.pyi)")
    # ② 字节码缓存
    for 路径 in 站包目录.rglob("__pycache__"):
        _删(路径, 试运行, "字节码缓存")
    # ③ 第三方包自带的测试/文档（保留本项目的 tests，它们在项目根不在 venv 里）
    for 名 in ("tests", "test", "testing", "demos", "examples", "docs", "doc"):
        for 路径 in 站包目录.glob(f"*/{名}"):
            _删(路径, 试运行, "第三方测试/文档")

    PySide = 站包目录 / "PySide6"
    if not PySide.is_dir():
        return
    Qt = PySide / "Qt"
    # ④ 用不到的 Qt 模块（Python 绑定 + 库 + 附属文件）
    for 模块 in 用不到的Qt模块:
        库名 = 模块.replace("Qt3D", "Qt3D").replace("Qt", "Qt6", 1)
        for 路径 in (站包目录 / "PySide6").glob(f"{模块}.abi3.so"):
            _删(路径, 试运行, f"Qt模块 {模块}")
        for 路径 in (站包目录 / "PySide6").glob(f"{模块}.pyi"):
            _删(路径, 试运行, f"Qt模块 {模块}")
        if Qt.is_dir():
            for 路径 in (Qt / "lib").glob(f"lib{库名}*.so*"):
                _删(路径, 试运行, f"Qt模块 {模块}")
    # ⑤ Qt 开发工具与文档
    for 名 in 用不到的Qt目录:
        _删(PySide / 名, 试运行, "Qt开发工具")
    # ⑥ Qt 翻译：只留中/英
    翻译 = Qt / "translations"
    if 翻译.is_dir():
        for 路径 in 翻译.glob("*.qm"):
            语言 = 路径.stem.rsplit("_", 1)[-1]
            if 语言 not in 保留翻译 and 路径.stem.split("_")[-1] not in 保留翻译:
                _删(路径, 试运行, "Qt翻译")
    # ⑦ 用不到的插件与 Qt 资源
    for 名 in 用不到的Qt插件:
        _删(Qt / "plugins" / 名, 试运行, "Qt插件")
    for 名 in ("qtwebengine_devtools_resources.pak",):
        _删(Qt / "resources" / 名, 试运行, "Qt资源")


def 精简运行环境(顶层: Path, 平台: str = "linux", 试运行: bool = False,
            verbose: bool = True) -> dict[str, int]:
    """精简 ``顶层/运行环境`` 里的 Python 环境。返回各分类释放的字节数。

    :param 顶层: 发布包的顶层目录（里面应有 ``运行环境/``）
    :param 平台: "linux" / "windows"（Windows 包结构不同，按目录名兼容处理）
    :param 试运行: True 时只统计不删除
    """
    结果统计.clear()
    运行环境 = 顶层 / "运行环境"
    if not 运行环境.is_dir():
        if verbose:
            print(f"  （没有 {运行环境}，跳过）")
        return dict(结果统计)
    站点目录们 = list(运行环境.glob("*/lib/python*/site-packages"))
    站点目录们 += list(运行环境.glob("*/Lib/site-packages"))
    if not 站点目录们:
        站点目录们 = [运行环境]
    for 站包 in 站点目录们:
        _精简站点目录(站包, 试运行)
    # ⑧ 运行环境根部的缓存（venv/python 都可能有）
    for 路径 in 运行环境.rglob("__pycache__"):
        _删(路径, 试运行, "字节码缓存")
    if verbose:
        总计 = sum(结果统计.values())
        print(f"  · 精简运行环境：{'（试运行）' if 试运行 else ''}释放约 {总计 / 1048576:.0f} MB")
        for 分类, 大小 in sorted(结果统计.items(), key=lambda x: -x[1])[:8]:
            print(f"      {分类:<16} {大小 / 1048576:>7.1f} MB")
    return dict(结果统计)


def main() -> int:
    解析 = argparse.ArgumentParser(description="压缩发布包里的 Python 运行环境")
    解析.add_argument("目标", nargs="?", default="", help="发布包顶层目录（含 运行环境/）")
    解析.add_argument("--试运行", action="store_true", help="只统计，不删除")
    解析.add_argument("--就地", action="store_true",
                     help="直接精简项目自己的 运行环境（开发用，慎用）")
    参数 = 解析.parse_args()

    if 参数.就地:
        顶层 = Path(__file__).resolve().parent.parent
        print(f"⚠️ 就地精简本项目的运行环境：{顶层}")
    elif 参数.目标:
        顶层 = Path(参数.目标).resolve()
    else:
        print("用法：精简运行环境.py <发布包顶层目录> [--试运行]  或  --就地")
        return 2
    精简运行环境(顶层, 试运行=参数.试运行)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
