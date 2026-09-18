# v8_3/界面/内置浏览器登录.py
"""内置浏览器登录窗口（方案 B）：在程序里登录，凭证**自动**回填。

为什么要有它（真机实测结论，2026-09-19）
========================================
百度**扫码登录**换来的 BDUSS 只能用于网盘 API，进不了**网页版** pan 域：

    GET https://pan.baidu.com/disk/main
      → 200，但重定向到 /disk/main?errmsg=Auth Login Params Not Corret
        Set-Cookie 只有一个 PANPSC=（清空）
    GET https://pan.baidu.com/api/list
      → 200（读接口正常）

而 pan 域的 STOKEN（写操作唯一认的凭证）**只在网页版登录时才下发**。
所以扫码会话天生"只能读"：能列目录，上传/改名/删除恒 errno:-6。

本窗口直接走**百度自己的网页登录链路**（扫码/短信/账号密码都行），
然后用 QtWebEngine 的 `QWebEngineCookieStore` 把 cookie（**含 HttpOnly 的
BDUSS 与 pan 域 STOKEN**）取回来 —— 不依赖任何浏览器插件、不依赖外部
浏览器的调试端口，用户只需在这个窗口里登录一次。

设计要点
========
* profile 是**持久化**的且放在项目内 ``数据/内置浏览器``：下次打开还是登录态，
  跟着项目目录走，删目录即清干净（与"绿色版"承诺一致）；
* 不碰用户自己的浏览器（独立 profile、独立 storage）；
* 支持多家网盘：百度（要 BDUSS + pan 域 STOKEN）、夸克（要 __pus/__kps/__uid）、
  光鸭（走 localStorage/IndexedDB 的 access_token，取不到就提示用短信登录）；
* 取到足够凭证就自动回调（用户在窗口里看到"✅ 已捕获"），也可以手动点「我已登录完成」。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QTimer, QUrl, Qt
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout,
)

#: 内置浏览器的 Chromium 开关（必须在建第一个 QWebEngineView **之前**设好）：
#: * --disable-gpu：无 GPU/软件渲染环境下更稳（本机 QtWebEngine 会报
#:   "Failed to create RHI for backend: OpenGL"，不影响用，但日志很吵）；
#: * --disable-dev-shm-usage：/dev/shm 小的机器（容器/受限环境）不会崩。
os.environ.setdefault(
    "QTWEBENGINE_CHROMIUM_FLAGS",
    "--disable-gpu --disable-dev-shm-usage --disable-software-rasterizer")

#: 各网盘的入口地址与"必需 cookie"
网盘入口 = {
    "baidu": ("https://pan.baidu.com/disk/main", ("BDUSS", "STOKEN")),
    "quark": ("https://pan.quark.cn/", ("__pus", "__kps", "__uid")),
    "guangya": ("https://www.guangyapan.com/", ("access_token",)),
    "fake": ("https://example.invalid/", ()),
}

网盘名称 = {"baidu": "百度网盘", "quark": "夸克网盘", "guangya": "光鸭云盘"}


def _文本(值) -> str:
    """把 Qt 给的 cookie 字段统一成 str。

    ⚠️ 实测踩坑（2026-09-19）：PySide6 的 ``QNetworkCookie.name()`` /
    ``domain()`` / ``path()`` 返回的是 **bytes**（``b'BDUSS'``），
    ``value()`` 也是 bytes。以前只对 value 做了 decode，于是：
      * 窗口判"缺不缺凭证"永远判缺（名字是 b'BDUSS' 而不是 'BDUSS'）；
      * 桥那边按名字找 BDUSS 也找不到 → 明明抓到了 45 条 cookie（含
        BDUSS/STOKEN，用户已经登录成功）却报"还没拿到 BDUSS"。
    这里统一成 str，bytes 用 utf-8 解码、失败再退回 latin-1。
    """
    if 值 is None:
        return ""
    if isinstance(值, (bytes, bytearray)):
        try:
            return bytes(值).decode("utf-8")
        except Exception:
            return bytes(值).decode("latin-1", "replace")
    return str(值)


def 内置浏览器目录() -> Path:
    """项目的内置浏览器数据目录（独立 profile，不碰用户自己的浏览器）。"""
    根 = Path(__file__).resolve().parents[2] / "数据" / "内置浏览器"
    根.mkdir(parents=True, exist_ok=True)
    return 根


def 可用() -> tuple[bool, str]:
    """QtWebEngine 能不能用（缺模块/缺系统库时给出原因）。"""
    try:
        from PySide6.QtWebEngineCore import QWebEngineProfile  # noqa: F401
        from PySide6.QtWebEngineWidgets import QWebEngineView  # noqa: F401
    except Exception as e:  # noqa: BLE001
        return False, f"内置浏览器不可用（QtWebEngine 缺失）：{e}"
    return True, ""


def 读取当前离线cookie(需要: tuple[str, ...] = ()) -> list[dict]:
    """直接从内置浏览器 profile 的 Cookies 库读 cookie（含 HttpOnly）。

    为什么不用 Qt 的 `filterCookies`：它是按 **URL** 匹配的，
    `.baidu.com` 这种"域 cookie"（BDUSS 就挂在它下面）在按
    `https://www.baidu.com/` 过滤时**不一定**返回 —— 实测就漏掉了 BDUSS。
    而 QtWebEngine 的 profile 是我们自己的目录，cookie 库是**明文**的
    （Value 列直接可读，实测 value=192/enc=0），所以直接读库最稳：
    不依赖事件循环、不依赖调试端口、不受域名匹配规则影响。

    :param 需要: 只要这些名字的 cookie（空 = 全都要）
    """
    import shutil
    import sqlite3
    import tempfile

    库 = 内置浏览器目录() / "storage" / "Cookies"
    结果: list[dict] = []
    临时 = ""
    try:
        if not 库.is_file():
            return []
        # 浏览器可能正占用库文件：拷贝一份再读（避免 database is locked）
        try:
            连接 = sqlite3.connect(f"file:{库}?mode=ro&immutable=1", uri=True)
        except Exception:
            临时 = tempfile.mktemp(suffix=".db")
            shutil.copy2(库, 临时)
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
                值 = 行["value"]
                if isinstance(值, (bytes, bytearray)):
                    值 = bytes(值).decode("utf-8", "replace")
                名字 = str(行["name"] or "")
                # ⚠️ 清理历史脏数据：早期版本把 bytes 直接 str() 写进了库，
                #    于是库里出现过字面量名字 "b'STOKEN'"。这里还原成 STOKEN。
                if len(名字) > 4 and 名字.startswith("b'") and 名字.endswith("'"):
                    名字 = 名字[2:-1]
                elif len(名字) > 5 and 名字.startswith('b"') and 名字.endswith('"'):
                    名字 = 名字[2:-1]
                结果.append({
                    "domain": str(行["host_key"] or ""),
                    "name": 名字,
                    "value": str(值 or ""),
                    "path": str(行["path"] or "/"),
                    "httpOnly": bool(行["is_httponly"]),
                    "secure": bool(行["is_secure"]),
                })
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


class 内置浏览器登录窗口(QDialog):
    """在程序里打开网盘网页登录，登录完成后把凭证交给调用方。"""

    #: 多久查一次"凭证够不够了"（毫秒）
    检查间隔毫秒 = 1500

    def __init__(self, 网盘类型: str, 父=None,
                 完成回调: Optional[Callable[[dict], None]] = None):
        super().__init__(父)
        self.网盘类型 = str(网盘类型 or "")
        self._完成回调 = 完成回调
        self._凭证: list[dict] = []
        self._已回调 = False
        self._profile = None
        self._视图 = None

        名称 = 网盘名称.get(self.网盘类型, self.网盘类型)
        self.setWindowTitle(f"🌐 内置浏览器登录 · {名称}")
        self.resize(1000, 760)

        布局 = QVBoxLayout(self)
        布局.setSpacing(6)

        说明 = QLabel(
            f"在下面的窗口里登录 <b>{名称}</b>（扫码 / 短信 / 账号密码都行）。\n"
            "登录成功后**程序会自动取走完整会话**（含 HttpOnly 的凭证），"
            "不用你复制任何东西；看到 ✅ 就可以关掉这个窗口。")
        说明.setWordWrap(True)
        说明.setStyleSheet("font-size: 12px; color: #2c3e50; padding: 4px;")
        布局.addWidget(说明)

        工具行 = QHBoxLayout()
        入口, _需要 = 网盘入口.get(self.网盘类型, ("about:blank", ()))
        self.地址框 = QLineEdit(入口)
        self.地址框.returnPressed.connect(self._跳转)
        工具行.addWidget(self.地址框, 1)
        去按钮 = QPushButton("跳转")
        去按钮.clicked.connect(self._跳转)
        工具行.addWidget(去按钮)
        刷新按钮 = QPushButton("🔄 刷新")
        刷新按钮.clicked.connect(lambda: self._视图 and self._视图.reload())
        工具行.addWidget(刷新按钮)
        布局.addLayout(工具行)

        self.状态标签 = QLabel("⏳ 正在加载登录页…")
        self.状态标签.setWordWrap(True)
        self.状态标签.setStyleSheet(
            "font-size: 12px; padding: 6px; border-radius: 4px;"
            "background: #34495e; color: #ecf0f1;")
        布局.addWidget(self.状态标签)

        self._建视图(布局)

        底部 = QHBoxLayout()
        self.完成按钮 = QPushButton("✅ 我已登录完成（取走凭证）")
        self.完成按钮.setObjectName("PrimaryButton")
        self.完成按钮.clicked.connect(lambda: self._回调(self._凭证, 手动=True))
        底部.addWidget(self.完成按钮)
        底部.addStretch(1)
        取消按钮 = QPushButton("关闭")
        取消按钮.clicked.connect(self.reject)
        底部.addWidget(取消按钮)
        布局.addLayout(底部)

        self._计时 = QTimer(self)
        self._计时.setInterval(int(self.检查间隔毫秒))
        self._计时.timeout.connect(self._查凭证)
        self._计时.start()

    # ---------------- 视图 ----------------

    def _建视图(self, 布局) -> None:
        try:
            from PySide6.QtWebEngineCore import QWebEngineProfile
            from PySide6.QtWebEngineWidgets import QWebEngineView
        except Exception as e:  # noqa: BLE001
            self.状态标签.setText(f"❌ 内置浏览器不可用：{e}")
            return
        目录 = 内置浏览器目录()
        try:
            # 持久化 profile：登录态留在项目里，下次打开还在
            self._profile = QWebEngineProfile("v8_3_内置登录", self)
            self._profile.setPersistentStoragePath(str(目录 / "storage"))
            self._profile.setCachePath(str(目录 / "cache"))
            self._profile.setHttpCacheType(QWebEngineProfile.DiskHttpCache)
            self._profile.setPersistentCookiesPolicy(
                QWebEngineProfile.ForcePersistentCookies)
        except Exception:
            self._profile = QWebEngineProfile.defaultProfile()
        try:
            self._profile.cookieStore().cookieAdded.connect(self._收cookie)
            self._profile.cookieStore().loadAllCookies()
        except Exception as e:  # noqa: BLE001
            self.状态标签.setText(f"⚠️ cookie 监听失败：{e}")

        self._视图 = QWebEngineView(self)
        try:
            from PySide6.QtWebEngineCore import QWebEnginePage
            页面 = QWebEnginePage(self._profile, self._视图)
            self._视图.setPage(页面)
        except Exception:
            pass
        布局.addWidget(self._视图, 1)
        self._视图.loadFinished.connect(self._加载完)
        入口, _ = 网盘入口.get(self.网盘类型, ("about:blank", ()))
        self._视图.load(QUrl(入口))

    def _加载完(self, 好: bool) -> None:
        if not self._已回调:
            self.状态标签.setText(
                f"{'✅ 页面已加载' if 好 else '⚠️ 页面加载失败'}："
                f"{self._视图.url().toString()[:80]}　｜　"
                "正在读取本机已有的登录态…")
        # ⚠️ 关键：**已经登录过**的时候（profile 是持久化的），cookie 早就在库里，
        #    `loadAllCookies()` 不会再发 cookieAdded —— 必须主动 filterCookies 捞一遍，
        #    否则"打开窗口就是登录态"却永远收不到凭证。
        self._捞全部cookie()
        self._查凭证()

    def _捞全部cookie(self) -> None:
        """把当前 profile 里所有相关 cookie 主动捞出来（含 HttpOnly）。"""
        if self._profile is None:
            return
        try:
            from PySide6.QtCore import QUrl
            网址们 = [
                QUrl("https://pan.baidu.com/"),
                QUrl("https://passport.baidu.com/"),
                QUrl("https://pcs.baidu.com/"),
                QUrl("https://pcsdata.baidu.com/"),
                QUrl("https://www.baidu.com/"),
                QUrl("https://pan.quark.cn/"),
                QUrl("https://www.guangyapan.com/"),
                QUrl("https://account.guangyapan.com/"),
            ]

            def _收(饼们) -> None:
                for c in 饼们 or []:
                    self._收cookie(c)
                self._查凭证()

            self._profile.cookieStore().filterCookies(网址们, _收)
        except Exception as e:  # noqa: BLE001
            try:
                self.状态标签.setText(f"⚠️ 读取已有 cookie 失败：{e}")
            except Exception:
                pass

    def _跳转(self) -> None:
        if self._视图 is not None:
            self._视图.load(QUrl(self.地址框.text().strip()))

    # ---------------- 凭证 ----------------

    def _收字典(self, 项: dict) -> None:
        """把一条"已经是 dict"的 cookie 合并进凭证表（与 _收cookie 同一套去重）。"""
        干净 = {
            "domain": _文本(项.get("domain")),
            "name": _文本(项.get("name")),
            "value": _文本(项.get("value")),
            "path": _文本(项.get("path")) or "/",
            "httpOnly": bool(项.get("httpOnly")),
            "secure": bool(项.get("secure")),
        }
        if not 干净["name"] or not 干净["value"]:
            return
        for i, 旧 in enumerate(self._凭证):
            if 旧["name"] == 干净["name"] and 旧["domain"] == 干净["domain"]:
                self._凭证[i] = 干净
                return
        self._凭证.append(干净)

    def _收cookie(self, cookie) -> None:
        try:
            项 = {
                "domain": _文本(cookie.domain()),
                "name": _文本(cookie.name()),
                "value": _文本(cookie.value()),
                "httpOnly": bool(cookie.isHttpOnly()),
                "secure": bool(cookie.isSecure()),
                "path": _文本(cookie.path()) or "/",
            }
        except Exception:
            return
        # 统一走 _收字典：Qt 这条路有时给的是 bytes（b'STOKEN'），归一化只写一处
        self._收字典(项)

    def _查凭证(self) -> None:
        """看凭证够不够；够了就自动回调（用户不用手动点）。"""
        if self._已回调 or self._视图 is None:
            return
        # 先直接读库（最可靠：BDUSS 挂在 .baidu.com 下，Qt 的按 URL 过滤会漏）
        需要 = 网盘入口.get(self.网盘类型, ("", ()))[1]
        for c in 读取当前离线cookie(tuple(需要) if 需要 else ()):
            if c.get("name") and c.get("value"):
                self._收字典(c)
        try:
            # 每次查之前让 store 把库里的都吐一遍（新建 profile 首次可能没有信号），
            # 同时主动 filterCookies 一遍（已有登录态时这是唯一能拿到 cookie 的路）
            self._profile.cookieStore().loadAllCookies()
        except Exception:
            pass
        if len(self._凭证) < 8:
            self._捞全部cookie()
        名字 = {str(c["name"]) for c in self._凭证}
        缺 = [x for x in 需要 if x not in 名字]
        if not 缺 and 需要:
            self._回调(self._凭证, 手动=False)
        elif self._凭证:
            self.状态标签.setText(
                f"⏳ 已捕获 {len(self._凭证)} 条 cookie，还缺：{'、'.join(缺)}"
                f"（请继续在页面里完成登录）")

    def 摘要素() -> str:
        """一行摘要：捕获了哪些 cookie（名字@域名），诊断用。"""
        组 = []
        for c in self._凭证:
            组.append(f"{c.get('name')}@{c.get('domain')}")
        return "、".join(sorted(set(组)))[:400]

    def _回调(self, 凭证: list[dict], 手动: bool = False) -> None:
        if self._已回调:
            return
        self._已回调 = True
        try:
            self._计时.stop()
        except Exception:
            pass
        self.状态标签.setText(
            f"✅ 已捕获 {len(凭证)} 条 cookie（{'手动确认' if 手动 else '自动识别'}）"
            "，正在写回凭证…")
        if self._完成回调 is not None:
            try:
                self._完成回调(list(凭证))
            except Exception as e:  # noqa: BLE001
                self.状态标签.setText(f"⚠️ 回填凭证失败：{e}")
                self._已回调 = False
                return
        QTimer.singleShot(800, self.accept)

    # ---------------- 工具 ----------------

    @staticmethod
    def 取cookie值(凭证: list[dict], 名: str,
                域名优先: tuple[str, ...] = ()) -> str:
        """按域名优先级取某个 cookie 的值（同名 cookie 不同域值可能不同）。"""
        候选 = [c for c in (凭证 or []) if _文本(c.get("name")) == 名]
        for 域 in 域名优先:
            命中 = [c for c in 候选 if _文本(c.get("domain")) == 域]
            if 命中:
                return _文本(命中[0].get("value"))
        return _文本(候选[0].get("value")) if 候选 else ""

    @staticmethod
    def 拼cookie头(凭证: list[dict], 只要: tuple[str, ...] = ()) -> str:
        """把 cookie 拼成 `Cookie:` 头（同名取第一个；给了白名单就只拼白名单）。"""
        对 = []
        见过 = set()
        for c in (凭证 or []):
            名 = _文本(c.get("name"))
            if not 名 or 名 in 见过:
                continue
            if 只要 and 名 not in 只要:
                continue
            见过.add(名)
            对.append(f"{名}={_文本(c.get('value'))}")
        return "; ".join(对)


def 拼cookie头(凭证: list[dict], 只要: tuple[str, ...] = ()) -> str:
    """模块级便捷函数（同 内置浏览器登录窗口.拼cookie头）。"""
    return 内置浏览器登录窗口.拼cookie头(凭证, 只要)


def 取cookie值(凭证: list[dict], 名: str,
            域名优先: tuple[str, ...] = ()) -> str:
    """模块级便捷函数：按域名优先级取 cookie 值。"""
    return 内置浏览器登录窗口.取cookie值(凭证, 名, 域名优先)
