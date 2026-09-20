"""QtWebEngine 引擎适配器：把"现在这套 Chromium"塞进 :class:`浏览器引擎` 接口。

为什么单独一个文件
==================
它**唯一**的职责是"渲染页面 + 收 cookie + 把鼠标键盘送进去"，不含任何业务判断。
将来换 WebView2 / CDP / Obscura / 自研引擎时，只需要再写一个同样大小的适配器，
界面层与凭证层（712 行）**一行都不用改**。

QtWebEngine 特有的两个坑（从原文件搬过来，别删）
================================================
1. **cookie 字段是 bytes**：``QNetworkCookie.name()/domain()/value()`` 返回
   ``b'BDUSS'``，所以一律走 :func:`浏览器引擎.规范cookie`；
2. **已有登录态收不到 cookieAdded**：profile 是持久化的，重开窗口时 cookie
   早就在库里，``loadAllCookies()`` 不会再发信号 —— 必须主动
   ``filterCookies`` **并且**直读 profile 的 Cookies 库（后者还能拿到
   ``.baidu.com`` 这种"按 URL 过滤会漏掉"的域 cookie，实测漏过 BDUSS）。

另外：本文件**只做引擎**；"哪个 profile 目录、要不要直读库"由调用方通过
``配置`` 传进来（``数据目录`` / ``直读cookie库``）。
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path

from .浏览器引擎 import 浏览器引擎, 规范cookie

#: 内置浏览器的 Chromium 开关（必须在建第一个 QWebEngineView **之前**设好）
os.environ.setdefault(
    "QTWEBENGINE_CHROMIUM_FLAGS",
    "--disable-gpu --disable-dev-shm-usage --disable-software-rasterizer")


def 读取cookie库(库文件: Path, 需要: tuple[str, ...] = ()) -> list[dict]:
    """直读 Chromium 的 Cookies 库（QtWebEngine 的库是**明文**的）。

    为什么不只用 ``filterCookies``：它按 **URL** 匹配，``.baidu.com`` 这种
    域 cookie（BDUSS 挂在它下面）在按 ``https://www.baidu.com/`` 过滤时
    **不一定**返回（实测就漏掉了 BDUSS）。直读库最稳：不依赖事件循环。
    """
    结果: list[dict] = []
    临时 = ""
    try:
        if not Path(库文件).is_file():
            return []
        try:
            连接 = sqlite3.connect(f"file:{库文件}?mode=ro&immutable=1", uri=True)
        except Exception:
            临时 = tempfile.mktemp(suffix=".db")
            shutil.copy2(库文件, 临时)
            连接 = sqlite3.connect(临时)
        try:
            连接.row_factory = sqlite3.Row
            if 需要:
                占位 = ",".join("?" for _ in 需要)
                行们 = 连接.execute(
                    f"select host_key,name,value,path,is_httponly,is_secure "
                    f"from cookies where name in ({占位})", tuple(需要))
            else:
                行们 = 连接.execute(
                    "select host_key,name,value,path,is_httponly,is_secure "
                    "from cookies")
            for 行 in 行们:
                名字 = str(行["name"] or "")
                # 清理历史脏数据：早期 bug 把 bytes 直接 str() 写进了库，
                # 库里出现过字面量名字 "b'STOKEN'"。
                if len(名字) > 4 and 名字.startswith("b'") and 名字.endswith("'"):
                    名字 = 名字[2:-1]
                elif len(名字) > 5 and 名字.startswith('b"') and 名字.endswith('"'):
                    名字 = 名字[2:-1]
                值 = 行["value"]
                if isinstance(值, (bytes, bytearray)):
                    值 = bytes(值).decode("utf-8", "replace")
                结果.append(规范cookie({
                    "domain": str(行["host_key"] or ""), "name": 名字,
                    "value": str(值 or ""), "path": str(行["path"] or "/"),
                    "httpOnly": bool(行["is_httponly"]),
                    "secure": bool(行["is_secure"]),
                }))
        finally:
            连接.close()
    except Exception:
        return 结果
    finally:
        if 临时:
            try:
                Path(临时).unlink(missing_ok=True)
            except Exception:
                pass
    return 结果


class QtWebEngine引擎(浏览器引擎):
    """用 PySide6 自带 Chromium 的引擎（默认引擎）。"""

    名字 = "QtWebEngine"

    #: filterCookies 用的网址清单（覆盖三家网盘的 cookie 域）
    过滤网址 = (
        "https://pan.baidu.com/", "https://passport.baidu.com/",
        "https://pcs.baidu.com/", "https://pcsdata.baidu.com/",
        "https://www.baidu.com/", "https://pan.quark.cn/",
        "https://www.guangyapan.com/", "https://account.guangyapan.com/",
    )

    def __init__(self, 配置: dict | None = None, 父=None):
        super().__init__(配置, 父)
        self._profile = None
        self._视图 = None
        self._页面 = None
        self._加载回调 = None
        self._数据目录 = Path(self.配置.get("数据目录")
                         or (Path(__file__).resolve().parents[2]
                             / "数据" / "内置浏览器"))
        self._直读库 = bool(self.配置.get("直读cookie库", True))
        self._库文件 = self._数据目录 / "storage" / "Cookies"

    # ---------------- 可用性 ----------------

    @staticmethod
    def 可用() -> tuple[bool, str]:
        try:
            from PySide6.QtWebEngineCore import QWebEngineProfile  # noqa: F401
            from PySide6.QtWebEngineWidgets import QWebEngineView  # noqa: F401
        except Exception as e:  # noqa: BLE001
            return False, f"内置浏览器不可用（QtWebEngine 缺失）：{e}"
        return True, ""

    # ---------------- 视图 ----------------

    def 取视图(self):
        """建好（或返回已有的）QWebEngineView。"""
        if self._视图 is not None:
            return self._视图
        from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
        from PySide6.QtWebEngineWidgets import QWebEngineView
        try:
            self._数据目录.mkdir(parents=True, exist_ok=True)
            self._profile = QWebEngineProfile("v8_3_内置登录", self.父)
            self._profile.setPersistentStoragePath(str(self._数据目录 / "storage"))
            self._profile.setCachePath(str(self._数据目录 / "cache"))
            self._profile.setHttpCacheType(QWebEngineProfile.DiskHttpCache)
            self._profile.setPersistentCookiesPolicy(
                QWebEngineProfile.ForcePersistentCookies)
        except Exception:
            self._profile = QWebEngineProfile.defaultProfile()
        try:
            self._profile.cookieStore().loadAllCookies()
        except Exception:
            pass
        self._视图 = QWebEngineView(self.父)
        try:
            self._页面 = QWebEnginePage(self._profile, self._视图)
            self._视图.setPage(self._页面)
        except Exception:
            pass
        self._视图.loadFinished.connect(self._加载完)
        return self._视图

    def _加载完(self, 好: bool) -> None:
        if self._加载回调 is not None:
            self._加载回调(bool(好))

    def 挂加载回调(self, 回调) -> None:
        self._加载回调 = 回调

    # ---------------- 导航 ----------------

    def 打开(self, url: str) -> None:
        from PySide6.QtCore import QUrl
        视图 = self.取视图()
        视图.load(QUrl(str(url)))

    def 页面地址(self) -> str:
        try:
            return self._视图.url().toString() if self._视图 is not None else ""
        except Exception:
            return ""

    def 页面标题(self) -> str:
        try:
            return self._视图.title() if self._视图 is not None else ""
        except Exception:
            return ""

    # ---------------- 交互 ----------------

    def 点(self, x: int, y: int) -> bool:
        """把点击送进页面 DOM（用 JS 派发，等价于"人点了这个位置"）。"""
        if self._页面 is None:
            return False
        js = (
            "(function(){var e=document.elementFromPoint(%d,%d);if(!e)return false;"
            "var o={bubbles:true,cancelable:true,view:window,clientX:%d,clientY:%d};"
            "e.dispatchEvent(new MouseEvent('mousedown',o));"
            "e.dispatchEvent(new MouseEvent('mouseup',o));"
            "e.dispatchEvent(new MouseEvent('click',o));"
            "if(e.focus)e.focus();return true;})()" % (x, x, y))
        try:
            self._页面.runJavaScript(js)
            return True
        except Exception:
            return False

    def 输入文本(self, 文本: str) -> bool:
        if self._页面 is None:
            return False
        import json as _json
        安全 = _json.dumps(str(文本), ensure_ascii=False)
        js = (
            "(function(){var t=%s;var e=document.activeElement;"
            "if(!e||(e.tagName!=='INPUT'&&e.tagName!=='TEXTAREA')){return false;}"
            "var p=e.value;e.value=p+t;"
            "e.dispatchEvent(new Event('input',{bubbles:true}));"
            "e.dispatchEvent(new Event('change',{bubbles:true}));"
            "return true;})()" % 安全)
        try:
            self._页面.runJavaScript(js)
            return True
        except Exception:
            return False

    def 按键(self, 键: str) -> bool:
        if self._页面 is None:
            return False
        import json as _json
        安全 = _json.dumps(str(键))
        js = (
            "(function(){var k=%s;var e=document.activeElement||document.body;"
            "var o={bubbles:true,cancelable:true,key:k};"
            "e.dispatchEvent(new KeyboardEvent('keydown',o));"
            "e.dispatchEvent(new KeyboardEvent('keyup',o));"
            "if(k==='Enter'&&e.form&&e.form.requestSubmit){e.form.requestSubmit();}"
            "return true;})()" % 安全)
        try:
            self._页面.runJavaScript(js)
            return True
        except Exception:
            return False

    def 执行JS(self, 脚本: str):
        """在页面里跑 JS 并同步拿回结果（runJavaScript 是异步的，这里转成同步）。

        做法：QEventLoop + 回调置结果 + 超时兜底（页面卡住也不会把界面挂死）。
        """
        if self._页面 is None:
            return None
        from PySide6.QtCore import QEventLoop, QTimer
        结果: dict = {"值": None}

        def 收到(值) -> None:
            结果["值"] = 值
            循环.quit()

        循环 = QEventLoop()
        try:
            self._页面.runJavaScript(脚本, 收到)
        except Exception:
            return None
        QTimer.singleShot(2500, 循环.quit)      # 兜底：最多等 2.5 秒
        循环.exec()
        return 结果["值"]

    def 滚动(self, 增量: int) -> bool:
        if self._页面 is None:
            return False
        try:
            self._页面.runJavaScript(f"window.scrollBy(0,{int(增量)});")
            return True
        except Exception:
            return False

    # ---------------- 凭证 ----------------

    #: 直读 cookie 库失败时重试几次、每次间隔多少秒
    #: （用户刚在内置浏览器里点完"登录"，Chromium 把 cookie 写进 sqlite 需要一点时间；
    #   实测踩过：窗口一关就报"已捕获 0 条 cookie"，其实几秒后 cookie 就在库里了）
    取cookie重试 = 4
    取cookie间隔秒 = 0.7

    def 取cookie(self, 只要名字: tuple[str, ...] = (),
              域们: tuple[str, ...] = ()) -> list[dict]:
        """三条路都走一遍（按可靠性排序），并做去重。

        :param 只要名字: 只取这些名字（界面按此判断"齐不齐"）
        :param 域们: 只取这些域（子串匹配）
        """
        结果: list[dict] = []
        名字集 = {str(x) for x in (只要名字 or ()) if x}
        if self._直读库:
            for 第次 in range(max(1, self.取cookie重试)):
                批 = 读取cookie库(self._库文件, tuple(只要名字) if 只要名字 else ())
                if 批:
                    结果.extend(批)
                    break
                if 第次 + 1 < self.取cookie重试:
                    time.sleep(self.取cookie间隔秒)
        if self._profile is not None:
            try:
                from PySide6.QtCore import QUrl
                收集: list[dict] = []

                def _收(饼们) -> None:
                    for c in 饼们 or []:
                        干净 = 规范cookie(c)
                        if 干净:
                            收集.append(干净)

                self._profile.cookieStore().filterCookies(
                    [QUrl(u) for u in self.过滤网址], _收)   # 异步：拿到的下次就有了
                结果.extend(收集)
            except Exception:
                pass
        去重: dict[tuple, dict] = {}
        for c in 结果:
            if 名字集 and c.get("name") not in 名字集:
                continue
            if 域们 and not any(域 in (c.get("domain") or "") for 域 in 域们):
                continue
            去重[(c["name"], c["domain"])] = c
        return list(去重.values())

    def 清cookie(self) -> None:
        if self._profile is not None:
            try:
                self._profile.cookieStore().deleteAllCookies()
            except Exception:
                pass

    def 关闭(self) -> None:
        if self._已关闭:
            return
        super().关闭()
        try:
            if self._视图 is not None:
                self._视图.stop()
                self._视图.setParent(None)
        except Exception:
            pass
        self._视图 = None
        self._页面 = None
