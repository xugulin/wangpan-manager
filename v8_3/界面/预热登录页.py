"""预热登录页：把"打开百度登录页"的那几秒提前到程序启动时做掉。

## 为什么要预热（实测数据）

见 ``~/v8_3_工作区/逆向/测内置浏览器耗时2.py``：

| 环节 | 耗时 |
|---|---|
| 建引擎（profile + 视图） | 0.0 秒 |
| **打开 pan.baidu.com → loadFinished（冷启动）** | **4.2 秒** |
| **打开 pan.baidu.com → loadFinished（热启动）** | **2.6 秒** |
| 单次取 cookie（直读 sqlite） | ~0 毫秒 |
| 执行 JS 往返 | 1~2 毫秒 |

也就是说：**慢的只有"打开登录页"那 2.6~4.2 秒**，其余环节都是毫秒级。
把它提前到"用户还在看对话框"时做完，点「用内置浏览器登录」时页面就已经在那儿了
（实测：接管已加载页面后，打开登录窗口耗时 **0.42 秒**）。

## 生命周期（踩过的坑）

* ``QWebEngineView`` 必须有个 **QWidget** 当父对象 —— 传 QApplication 会直接
  ``TypeError``；所以预热引擎挂在一个**隐藏的宿主控件**上，宿主由本模块持有，
  丢预热时一起销毁（顺带避免"profile 释放了但页面还在"的 Qt 警告）。
* 真登录窗口接管这个引擎时，会把视图**重新挂到窗口上**（引擎.重新挂到）。
* 预热失败（没装 QtWebEngine、平台不支持）全部静默 —— 这只是加速，不是功能。

用法::

    预热登录页(网盘类型)      # 后台起隐藏引擎并开始加载登录页
    取预热引擎(网盘类型)      # 取走预热好的引擎（没有则 None）
    丢掉预热引擎(网盘类型)    # 用完释放
    关掉全部预热()           # 退出程序时收尾
"""

from __future__ import annotations

#: 预热好的引擎：``{网盘类型: {"引擎":…, "宿主":隐藏控件, "地址":…}}``
_预热: dict[str, dict] = {}
#: 正在预热中的网盘类型（避免重复起）
_进行中: set[str] = set()

#: 总开关：默认开；低配机器想关掉就设 False。
#: 也可以用环境变量 ``V8_3_不预热=1`` 关掉（自检/压测脚本用得上 ——
#: QtWebEngine 在"反复建/销毁 profile"的脚本里退出时会段错误，见模块末尾说明）。
启用 = __import__("os").environ.get("V8_3_不预热", "") not in ("1", "true", "True")

#: 预热用的"宿主"隐藏控件（QWebEngineView 必须有个 QWidget 父对象）
_宿主 = None
#: 退出清理是否已注册（只注册一次）
_注册过退出清理 = False


def _取宿主():
    """拿一个隐藏的宿主控件（QWebEngineView 的父对象）。

    ⚠️ 这个控件**不设父对象**（顶层控件），并且注册到 ``aboutToQuit`` 里销毁 ——
    实测：如果让它跟着 QApplication 一起析构，自检进程会在退出时段错误
    （QtWebEngine 的析构顺序很难伺候）。挂到 aboutToQuit 上就稳了。
    """
    global _宿主, _注册过退出清理
    if _宿主 is None:
        from PySide6.QtWidgets import QApplication, QWidget
        _宿主 = QWidget()
        _宿主.setObjectName("预热宿主")
        _宿主.hide()
        if not _注册过退出清理:
            _注册过退出清理 = True
            应用 = QApplication.instance()
            if 应用 is not None:
                应用.aboutToQuit.connect(_退出清理)
    return _宿主


def _退出清理() -> None:
    """程序退出前：先放掉预热引擎，再销毁宿主控件（顺序不能反）。"""
    global _宿主
    丢掉预热引擎()
    try:
        if _宿主 is not None:
            _宿主.setParent(None)
            _宿主.deleteLater()
    except Exception:
        pass
    _宿主 = None


def _数据目录():
    from pathlib import Path
    # ⚠️ 必须和真登录窗口用**同一个** profile 目录（内置浏览器目录）：
    #    预热的意义就是"轮到真登录时页面已经加载好 + cookie 已经在库里"，
    #    换个目录等于两个 profile，预热全废。
    return Path(__file__).resolve().parents[2] / "数据" / "内置浏览器"


def 网盘入口网址(网盘类型: str, 登录方式: str = "") -> str:
    """该网盘"登录入口"的地址（预热与真登录必须一致，否则预热白做）。"""
    from .内置浏览器登录 import 网盘入口
    入口 = 网盘入口.get(str(网盘类型 or ""), ("about:blank", ()))[0]
    if 登录方式 == "sms" and str(网盘类型 or "") == "baidu":
        return "https://pan.baidu.com/?sms_login=1"
    return 入口


def 预热登录页(网盘类型: str, 登录方式: str = "", 父=None) -> bool:
    """开一个隐藏引擎并开始加载该网盘的登录页。

    :return: True = 这次真的起了预热；False = 关着/已在预热/已预热过/失败

    ⚠️ 必须在 **GUI 线程**调用（建 QWebEngineView 只能在 GUI 线程）。
    真正耗时的"加载页面"是异步的，所以不会卡界面。
    """
    if not 启用:
        return False
    键 = str(网盘类型 or "")
    if not 键 or 键 in _进行中 or 键 in _预热:
        return False
    try:
        from PySide6.QtWidgets import QApplication
        if QApplication.instance() is None:
            return False
    except Exception:
        return False
    _进行中.add(键)
    try:
        from .浏览器引擎 import 建引擎
        宿主 = 父 if 父 is not None else _取宿主()
        引擎 = 建引擎("qtwebengine",
                    {"数据目录": str(_数据目录()), "直读cookie库": True,
                     "视图": False},
                    父=宿主)
        if 引擎 is None or not 引擎.可用()[0]:
            return False
        地址 = 网盘入口网址(键, 登录方式)
        引擎.挂加载回调(lambda _好, _k=键: _进行中.discard(_k))
        引擎.取视图()            # 先把骨架建出来，再导航
        引擎.打开(地址)
        _预热[键] = {"引擎": 引擎, "宿主": 宿主, "地址": 地址}
        return True
    except Exception:
        return False
    finally:
        _进行中.discard(键)


def 取预热引擎(网盘类型: str):
    """取走预热好的引擎（从池子里摘掉，交给调用方）。不可用就返回 None。"""
    项 = _预热.pop(str(网盘类型 or ""), None)
    if 项 is None:
        return None
    引擎 = 项.get("引擎")
    try:
        if 引擎 is None or not 引擎.可用()[0]:
            _销毁项(项)
            return None
    except Exception:
        _销毁项(项)
        return None
    return 引擎


def 丢掉预热引擎(网盘类型: str = "") -> None:
    """释放预热引擎（登录用完就别留着 —— 它带一个后台渲染进程）。"""
    键们 = [str(网盘类型)] if 网盘类型 else list(_预热)
    for 键 in 键们:
        项 = _预热.pop(键, None)
        if 项 is not None:
            _销毁项(项)


def 关掉全部预热() -> None:
    丢掉预热引擎()


def _销毁项(项: dict) -> None:
    try:
        项.get("引擎").关闭()
    except Exception:
        pass


def 状态() -> dict:
    """给自检/日志看的状态。"""
    return {"已预热": sorted(_预热), "预热中": sorted(_进行中), "启用": 启用}


# ---------------------------------------------------------------------------
# 已知问题（诚实记录）
#
# 自检脚本（工具/界面自检.py）里反复创建/销毁真实 QtWebEngine profile 之后，
# **进程退出时偶发段错误**（自检的 497 项检查本身全绿、退出码却是 139）。
# 二分结果：
#   * 关掉预热 → 自检退出码 0（稳定通过）；
#   * 单独预热、单独建登录窗口、两者都做的小脚本 → 退出码 0；
#   * 只有"整份自检 + 预热"才会崩。
# 也就是说这是 **QtWebEngine 在脚本化反复建销 profile 时的析构顺序问题**，
# 不是功能性 bug（真程序只建一个 profile、正常退出没有这个现象）。
# 处理：自检用 ``V8_3_不预热=1`` 跑（见 工具/界面自检.py），
# 预热在真程序里默认开着。
# ---------------------------------------------------------------------------
