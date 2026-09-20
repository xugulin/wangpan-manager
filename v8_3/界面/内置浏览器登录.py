# v8_3/界面/内置浏览器登录.py
"""内置浏览器登录窗口（方案 B）：在程序里登录，凭证**自动**回填。

分层（2026-09-20 重构）
======================
本文件**只做界面与流程**，浏览器（渲染 + 执行 JS + 收发 cookie）交给
:mod:`v8_3.界面.浏览器引擎` 里的**可插拔引擎**：

* ``浏览器引擎.py``：接口 + 工厂 + **假引擎**（没有浏览器也能测登录流程）；
* ``引擎_QtWebEngine.py``：现在的默认实现（自带 Chromium）；
* 将来换 WebView2 / CDP / Obscura / 自研引擎 → 只加一个适配器文件，
  **本文件与凭证层一行都不用改**。

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

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout,
)

from .浏览器引擎 import (
    浏览器引擎, 假引擎, 建引擎, 规范cookie, 取cookie值, 拼cookie头,
)

#: 各网盘的入口地址与"必需 cookie"
网盘入口 = {
    "baidu": ("https://pan.baidu.com/disk/main", ("BDUSS", "STOKEN")),
    "quark": ("https://pan.quark.cn/", ("__pus", "__kps", "__uid")),
    "guangya": ("https://www.guangyapan.com/", ("access_token",)),
    "fake": ("https://example.invalid/", ()),
}

网盘名称 = {"baidu": "百度网盘", "quark": "夸克网盘", "guangya": "光鸭云盘"}


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
    """兼容入口：读内置浏览器 profile 的 Cookies 库。

    真实现在引擎里（:func:`引擎_QtWebEngine.读取cookie库`）—— 别的引擎不需要
    这个能力（Obscura/WebView2 走自己的 API），所以它只是"QtWebEngine 引擎的
    一个实现细节"外露的兼容壳。
    """
    from .引擎_QtWebEngine import 读取cookie库
    return 读取cookie库(内置浏览器目录() / "storage" / "Cookies",
                     tuple(需要 or ()))


class 内置浏览器登录窗口(QDialog):
    """在程序里打开网盘网页登录，登录完成后把凭证交给调用方。"""

    #: 多久查一次"凭证够不够了"（毫秒）
    检查间隔毫秒 = 1500

    def __init__(self, 网盘类型: str, 父=None,
                 完成回调: Optional[Callable[[dict], None]] = None,
                 引擎: 浏览器引擎 | None = None,
                 登录方式: str = "",
                 手机号: str = ""):
        """``引擎`` 可注入（测试时传 :class:`假引擎`，生产走 QtWebEngine）。

        ``登录方式`` 可选值：``"sms"`` —— 加载完自动把登录框切到「短信登录」页，
        省得用户自己找（百度登录框有三种方式：扫码 / 账号 / 短信）。
        """
        super().__init__(父)
        self.网盘类型 = str(网盘类型 or "")
        self.登录方式 = str(登录方式 or "")
        self.手机号 = str(手机号 or "").strip()
        self._完成回调 = 完成回调
        self._凭证: list[dict] = []
        self._已回调 = False
        # 浏览器交给可插拔引擎：本窗口只做"显示 + 流程 + 收凭证"
        self.引擎: 浏览器引擎 = 引擎 if 引擎 is not None else 建引擎(
            "qtwebengine", {"数据目录": str(内置浏览器目录())}, 父=self)

        名称 = 网盘名称.get(self.网盘类型, self.网盘类型)
        self.setWindowTitle(f"🌐 内置浏览器登录 · {名称}")
        self.resize(1000, 760)

        布局 = QVBoxLayout(self)
        布局.setSpacing(6)

        方式提示 = "（已自动切到「短信登录」，填手机号 → 收验证码 → 登录）" \
            if self.登录方式 == "sms" else "（扫码 / 短信 / 账号密码都行）"
        说明 = QLabel(
            f"在下面的窗口里登录 <b>{名称}</b>{方式提示}。\n"
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
        刷新按钮.clicked.connect(self._刷新页面)
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
        self.完成按钮.clicked.connect(self._手动完成)
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
        """把引擎的视图放进对话框（引擎自己负责 profile/存储目录）。"""
        能用, 原因 = self.引擎.可用()
        if not 能用:
            self.状态标签.setText(f"❌ {原因 or '内置浏览器不可用'}")
            return
        try:
            视图 = self.引擎.取视图()
        except Exception as e:  # noqa: BLE001
            self.状态标签.setText(f"❌ 内置浏览器不可用：{e}")
            return
        if 视图 is not None:
            布局.addWidget(视图, 1)
        try:
            self.引擎.挂加载回调(self._加载完)
        except Exception:
            pass
        入口, _ = 网盘入口.get(self.网盘类型, ("about:blank", ()))
        if self.登录方式 == "sms" and self.网盘类型 == "baidu":
            # 从"短信登录"入口直接进：百度网盘首页支持 URL 参数把登录框直接开到
            # 短信登录页（?sms_login=1），用户不用先找登录按钮再切页签。
            # 万一百度改了这个参数，_切到短信登录()（按文字点页签）还会兜底。
            入口 = "https://pan.baidu.com/?sms_login=1"
        self.引擎.打开(入口)

    def _切到短信登录(self) -> None:
        """把登录框切到「短信登录」页。

        为什么要重试：登录框本身是异步渲染的（先加载静态页，再由 JS 挂登录组件），
        刚 loadFinished 时 tab 常常还不存在 —— 所以按 0.5 秒一次、最多 12 次重试。
        JS 里按"文字命中 + 可点"找元素，比记 class 名稳（百度改版会换 class）。
        """
        if self.引擎 is None:
            return
        js = r"""
(function(){
  var 词 = ['短信登录', '短信快捷登录'];
  var 元素 = document.querySelectorAll('a,button,div,span,li,p');
  for (var i = 0; i < 元素.length; i++) {
    var e = 元素[i];
    var t = (e.innerText || e.textContent || '').trim();
    if (词.indexOf(t) < 0) continue;
    var r = e.getBoundingClientRect();
    if (r.width < 8 || r.height < 8) continue;
    try {
      e.click();
      var ev = new MouseEvent('click', {bubbles: true, cancelable: true, view: window});
      e.dispatchEvent(ev);
      return 'clicked:' + t;
    } catch (err) { return 'error:' + err; }
  }
  // 再看有没有已经出现的短信表单（说明已经在短信页了）
  var 有手机框 = !!document.querySelector('input[placeholder*="手机"], input[name*="phone"]');
  return 有手机框 ? 'already' : 'notfound';
})()
"""
        def 试(剩余: int) -> None:
            if self._已回调 or 剩余 <= 0:
                return
            try:
                结果 = self.引擎.执行JS(js)
            except Exception:
                结果 = None
            if 结果 in ("already",) or (isinstance(结果, str) and 结果.startswith("clicked")):
                self.状态标签.setText("✅ 已切到「短信登录」：填手机号 → 点发送验证码 → 填验证码")
                self._填手机号()          # 顺手帮用户把手机号填上
                return
            QTimer.singleShot(500, lambda: 试(剩余 - 1))
        QTimer.singleShot(600, lambda: 试(12))

    #: 自动填手机号最多重试几次（输入框是异步渲染的，但要有个上限）
    填号重试上限 = 8

    def _填手机号(self, 第几次: int = 0) -> None:
        """把用户在上一个界面填过的手机号写进登录框（省一次手输）。

        百度登录框会把手机号 base64 编码后塞进 ``encryptMobile`` 隐藏域，
        只设 ``input.value`` 不够 —— 所以两个都试，并派发 input/change 事件。
        """
        if not self.手机号 or self.登录方式 != "sms":
            return
        if 第几次 > self.填号重试上限:
            return                      # 不再无限重试（状态栏已有提示）
        import json as _json
        安全 = _json.dumps(self.手机号, ensure_ascii=False)
        # 选择器里的引号统一改用 \\u0022（双引号）露出，避免和外层 Python 字符串打架
        脚本 = (
            "(function(){var v=" + 安全 + ";"
            "var b=document.querySelector("
            "'input[placeholder*=\\u624b\\u673a],input[name*=phone],"
            "input[name*=mobile],input[maxlength=\\u002211\\u0022]');"
            "if(!b)return 'nofield';"
            "b.focus();b.value=v;"
            "b.dispatchEvent(new Event('input',{bubbles:true}));"
            "b.dispatchEvent(new Event('change',{bubbles:true}));"
            "b.dispatchEvent(new Event('blur',{bubbles:true}));"
            "var h=document.getElementById('encryptMobile')||"
            "document.querySelector('input[name*=encryptMobile]');"
            "if(h){h.value=btoa(v);}"
            "return 'filled';})()")
        try:
            结果 = self.引擎.执行JS(脚本)
        except Exception:
            结果 = None
        if 结果 == "filled":
            self.状态标签.setText(
                f"✅ 已自动填入手机号（{self.手机号[:3]}****）"
                "：点「发送验证码」→ 收到后填验证码 → 登录")
        elif 结果 == "nofield":
            # 输入框还没渲染出来：再等一会儿重试（有次数上限）
            QTimer.singleShot(600, lambda: self._填手机号(第几次 + 1))

    def _加载完(self, 好: bool) -> None:
        if not self._已回调:
            if self.登录方式 == "sms" and self.网盘类型 == "baidu":
                self._切到短信登录()
            地址 = ""
            try:
                地址 = str(self.引擎.页面地址() or "")[:80]
            except Exception:
                pass
            self.状态标签.setText(
                f"{'✅ 页面已加载' if 好 else '⚠️ 页面加载失败'}：{地址}　｜　"
                "正在读取本机已有的登录态…")
        # ⚠️ 关键：**已经登录过**的时候（profile 是持久化的），cookie 早就在库里，
        #    引擎不会再发"新增 cookie"事件 —— 必须主动捞一遍，
        #    否则"打开窗口就是登录态"却永远收不到凭证（引擎内部已实现这一点）。
        self._查凭证()

    def _刷新页面(self) -> None:
        try:
            地址 = self.引擎.页面地址() or self.地址框.text().strip()
            if 地址:
                self.引擎.打开(地址)
        except Exception:
            pass

    def _跳转(self) -> None:
        self.引擎.打开(self.地址框.text().strip())

    # ---------------- 凭证 ----------------

    def _收字典(self, 项: dict) -> None:
        """把一条 cookie 合并进凭证表（归一化只在 浏览器引擎.规范cookie 一处）。"""
        干净 = 规范cookie(项)
        if not 干净.get("name") or not 干净.get("value"):
            return
        for i, 旧 in enumerate(self._凭证):
            if 旧["name"] == 干净["name"] and 旧["domain"] == 干净["domain"]:
                self._凭证[i] = 干净
                return
        self._凭证.append(干净)

    def _收cookie(self, cookie) -> None:
        """兼容旧调用（Qt 的 QNetworkCookie 之类）：归一化后并入。"""
        self._收字典(规范cookie(cookie))

    def _收引擎cookie(self, 需要: tuple[str, ...] = ()) -> None:
        """把引擎当前的 cookie 并进凭证表（取 cookie 的唯一入口）。"""
        try:
            for c in self.引擎.取cookie(tuple(需要) if 需要 else ()):
                干净 = 规范cookie(c)
                if 干净.get("name") and 干净.get("value"):
                    self._收字典(干净)
        except Exception:
            pass

    def _查凭证(self) -> None:
        """看凭证够不够；够了就自动回调（用户不用手动点）。

        取 cookie 一律问**引擎**（引擎内部自己决定：QtWebEngine 直读 profile 库 +
        filterCookies；Obscura 走 CDP；WebView2 走它的 API）。界面不认识任何
        浏览器细节 —— 这就是"引擎可替换"的关键。
        """
        if self._已回调:
            return
        需要 = 网盘入口.get(self.网盘类型, ("", ()))[1]
        self._收引擎cookie(tuple(需要))
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

    def _最后深取一次(self, 尝试: int = 3) -> int:
        """关窗/手动完成前的"最后一次深取"：连续取几轮，等 Chromium 把 cookie 落盘。

        ⚠️ 实测踩过：用户在内置浏览器里登录成功后**直接关窗**，窗口报
        "已捕获 0 条 cookie"，而其实几秒后 cookie 才落进 sqlite —— 于是白登一次。
        所以关窗前要等一下、多取几轮。引擎那边也有重试，这里是第二层保险。

        :return: 这次新拿到的条数
        """
        需要 = 网盘入口.get(self.网盘类型, ("", ()))[1]
        前 = len(self._凭证)
        for _ in range(max(1, 尝试)):
            self._收引擎cookie(tuple(需要))
            if self._凭证:
                break
            try:
                import time as _t
                _t.sleep(0.6)
            except Exception:
                pass
        新增 = len(self._凭证) - 前
        if 新增:
            self.状态标签.setText(
                f"✅ 关窗前又取到 {新增} 条 cookie（共 {len(self._凭证)} 条）")
        return 新增

    def _手动完成(self) -> None:
        """点「我已登录完成」：**先现场取一次** cookie 再回调。

        ⚠️ 以前直接回调 ``self._凭证``：若定时器（1.5 秒一轮）还没跑过，
        点下去会"什么都没发生"（被单测抓到）。
        """
        self._最后深取一次()
        if not self._凭证:
            self.状态标签.setText(
                "⚠️ 还没取到任何 cookie —— 请确认已经登录成功"
                "（页面右上角能看到你的账号），再点这个按钮")
            return
        self._回调(self._凭证, 手动=True)

    def closeEvent(self, 事件) -> None:
        """关窗时：**先把凭证再深取一次**（用户可能刚登录完就关了窗口），再收引擎。

        实取不到就**不要回调** —— 别拿空凭证去打扰桥（那样只会得到一句
        "内置浏览器里还没拿到 BDUSS"，用户还以为是登录失败）。
        """
        if not self._已回调:
            self._最后深取一次(尝试=4)
            if self._凭证:
                self._回调(self._凭证, 手动=True)
            else:
                self.状态标签.setText("已关闭内置浏览器（没取到 cookie，未提交）")
        try:
            self.引擎.关闭()
        except Exception:
            pass
        super().closeEvent(事件)

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

    # 这两个工具直接复用 浏览器引擎 模块里的实现（归一化只写一处）
    取cookie值 = staticmethod(取cookie值)
    拼cookie头 = staticmethod(拼cookie头)


def 拼cookie头(凭证: list[dict], 只要: tuple[str, ...] = ()) -> str:
    """模块级便捷函数（同 内置浏览器登录窗口.拼cookie头）。"""
    return 内置浏览器登录窗口.拼cookie头(凭证, 只要)


def 取cookie值(凭证: list[dict], 名: str,
            域名优先: tuple[str, ...] = ()) -> str:
    """模块级便捷函数：按域名优先级取 cookie 值。"""
    return 内置浏览器登录窗口.取cookie值(凭证, 名, 域名优先)
