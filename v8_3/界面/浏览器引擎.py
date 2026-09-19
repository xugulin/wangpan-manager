"""浏览器引擎抽象层：把"浏览器"变成可替换、可测试的一块。

为什么要有它
============
「内置浏览器登录」是这个项目里唯一的**图形化登录**通道（百度写权限那条路
只有真正的网页版登录才拿得到 pan 域 STOKEN）。但我们和浏览器的耦合**只有
十几个调用点**，逻辑本体（截图显示 / 二维码刷新 / 输入 / 凭证收割 / 自愈）
全都是我们自己写的 —— 所以正确的做法是：

* **界面层与凭证层只依赖本文件定义的接口**（那是我们 100% 可控的部分）；
* **引擎（渲染 + 执行 JS 的那块黑盒）是可插拔的实现**：现在只有 QtWebEngine，
  以后可以是 WebView2（Windows 系统自带、0 体积）、CDP（用户自己的浏览器）、
  Obscura 或将来任何自研引擎 —— 换引擎只改一个适配器文件。

接口很小（九件事）
==================
``可用()``、``打开(url)``、``取视图()``、``截图()``、``点(x,y)``、``输入文本(s)``、
``按键(k)``、``取cookie(域们)``、``清cookie()``、``设置配置(...)``、``关闭()``。

约定
====
* cookie 一律用**统一字典**：``{"name","value","domain","path","httpOnly","secure"}``
  —— 与 :func:`适配器.百度网盘适配器.核心.认证.浏览器会话导入.会话字段` 的输入一致；
* 引擎**不碰业务逻辑**：不判断"该不该自愈"、不认识 BDUSS/STOKEN；
* 引擎在**主线程**里用（Qt 控件），耗时动作由调用方丢后台。

自检与单测
==========
``假引擎`` 让"登录流程"能在**没有任何浏览器**的情况下被测试：把要出现的
cookie、要显示的截图都预先塞进去，界面流程照跑。``工具/界面自检.py`` 用它
验证对话框的取凭证/回调路径。
"""

from __future__ import annotations

from typing import Any

#: 统一 cookie 字典的字段（顺序即书写顺序，方便对比）
COOKIE字段 = ("name", "value", "domain", "path", "httpOnly", "secure")


def 规范cookie(项: dict | Any) -> dict:
    """把任意来源的 cookie 归一成统一字典（bytes 也吃）。

    实测踩过：PySide6 的 ``QNetworkCookie.name()/domain()/value()`` 返回 **bytes**
    （``b'BDUSS'``），只对 value 解码会让"缺不缺凭证"永远判缺 —— 这里统一处理。
    """
    def _文本(值) -> str:
        if 值 is None:
            return ""
        if isinstance(值, (bytes, bytearray)):
            try:
                return bytes(值).decode("utf-8")
            except Exception:
                return bytes(值).decode("latin-1", "replace")
        return str(值)

    if not isinstance(项, dict):
        try:                       # Qt 的 QNetworkCookie 之类
            项 = {
                "name": 项.name(), "value": 项.value(), "domain": 项.domain(),
                "path": 项.path(), "httpOnly": 项.isHttpOnly(),
                "secure": 项.isSecure(),
            }
        except Exception:
            return {}
    名字 = _文本(项.get("name"))
    if not 名字:
        return {}
    return {
        "name": 名字,
        "value": _文本(项.get("value")),
        "domain": _文本(项.get("domain")),
        "path": _文本(项.get("path")) or "/",
        "httpOnly": bool(项.get("httpOnly")),
        "secure": bool(项.get("secure")),
    }


def 取cookie值(凭证: list[dict], 名: str,
            域名优先: tuple[str, ...] = ()) -> str:
    """按域名优先级取某个 cookie 的值（同名 cookie 不同域值可能不同）。"""
    候选 = [c for c in (凭证 or []) if 规范cookie(c).get("name") == 名]
    for 域 in 域名优先:
        命中 = [c for c in 候选 if 规范cookie(c).get("domain") == 域]
        if 命中:
            return 规范cookie(命中[0]).get("value", "")
    return 规范cookie(候选[0]).get("value", "") if 候选 else ""


def 拼cookie头(凭证: list[dict], 只要: tuple[str, ...] = ()) -> str:
    """把 cookie 拼成 ``Cookie:`` 头（同名取第一个；给了白名单就只拼白名单）。"""
    对: list[str] = []
    见过: set[str] = set()
    for c in (凭证 or []):
        干净 = 规范cookie(c)
        名 = 干净.get("name", "")
        if not 名 or 名 in 见过 or (只要 and 名 not in 只要):
            continue
        见过.add(名)
        对.append(f"{名}={干净.get('value', '')}")
    return "; ".join(对)


class 浏览器引擎:
    """浏览器引擎接口（基类只提供默认实现与文档，子类按需覆盖）。

    子类至少要覆盖：``可用`` / ``打开`` / ``取视图`` / ``截图`` / ``取cookie``。
    "点/输入/按键"这类交互在**只做显示**的引擎里可以返回 False（不支持）。
    """

    #: 引擎名（写进日志/界面提示，便于排查"到底用的哪个引擎"）
    名字 = "抽象引擎"

    def __init__(self, 配置: dict | None = None, 父=None):
        self.配置 = dict(配置 or {})
        self.父 = 父
        self._已关闭 = False

    # ---------------- 生命周期 ----------------

    @staticmethod
    def 可用() -> tuple[bool, str]:
        """这个引擎在当前机器/当前包里能不能用。返回 ``(可用, 原因)``。"""
        return True, ""

    def 打开(self, url: str) -> None:
        """加载一个地址。"""
        raise NotImplementedError

    def 关闭(self) -> None:
        """释放资源（窗口销毁前调用；重复调用要安全）。"""
        self._已关闭 = True

    # ---------------- 显示 ----------------

    def 取视图(self):
        """返回可放进 Qt 布局的控件（引擎自己造的 QWidget）。"""
        raise NotImplementedError

    def 截图(self):
        """当前页面截图（``QImage``）。不支持时返回 None。"""
        return None

    def 页面地址(self) -> str:
        """当前 URL（用于状态提示）。"""
        return ""

    def 页面标题(self) -> str:
        return ""

    # ---------------- 人机交互 ----------------

    def 点(self, x: int, y: int) -> bool:
        """在页面坐标系点击。不支持返回 False。"""
        return False

    def 输入文本(self, 文本: str) -> bool:
        """往当前焦点控件输入文本（账号/密码/验证码都是 ASCII，不涉及输入法）。"""
        return False

    def 按键(self, 键: str) -> bool:
        """发送一个按键（如 ``Enter``/``Tab``/``Backspace``）。"""
        return False

    def 滚动(self, 增量: int) -> bool:
        return False

    # ---------------- 凭证 ----------------

    def 取cookie(self, 只要名字: tuple[str, ...] = (),
              域们: tuple[str, ...] = ()) -> list[dict]:
        """取出当前会话的 cookie（**统一字典**，含 HttpOnly）。

        :param 只要名字: 只取这些名字（如 ("BDUSS", "STOKEN")）；空 = 全部
        :param 域们: 只取这些域（子串匹配，如 ("baidu",)）；空 = 不限
        """
        return []

    def 清cookie(self) -> None:
        """清掉会话（退出登录/切换账号时用）。"""

    # ---------------- 便利 ----------------

    def 说明(self) -> str:
        return f"{self.名字}（{'可用' if self.可用()[0] else '不可用'}）"


# ============================================================================
#  假引擎：让登录流程在没有浏览器的情况下也能被测试
# ============================================================================


class 假引擎(浏览器引擎):
    """脚本化的假引擎：预置截图/cookie，记录收到的操作。

    用法::

        引擎 = 假引擎()
        引擎.预置cookie([{"name": "BDUSS", "value": "x", "domain": ".baidu.com"}])
        对话 = 内置浏览器登录窗口("baidu", 引擎=引擎, 完成回调=...)
        引擎.触发加载完成(True)          # 模拟页面加载完
        引擎.预置cookie([...STOKEN...])  # 模拟"用户登录成功，服务端下发新 cookie"
        对话._查凭证()                   # 界面流程照跑
    """

    名字 = "假引擎（测试用）"

    def __init__(self, 配置: dict | None = None, 父=None):
        super().__init__(配置, 父)
        self.cookie表: list[dict] = []
        self.操作记录: list[tuple] = []
        self._加载回调 = None
        self._截图 = None
        self._地址 = ""
        self._标题 = ""
        self._可用 = (True, "")
        self.视图占位 = None

    # 测试用：预置与触发

    def 设可用(self, 可用: bool, 原因: str = "") -> None:
        self._可用 = (可用, 原因)

    def 预置cookie(self, cookie们: list[dict]) -> None:
        for c in cookie们 or []:
            干净 = 规范cookie(c)
            if not 干净:
                continue
            self.cookie表 = [x for x in self.cookie表
                          if not (x["name"] == 干净["name"]
                                and x["domain"] == 干净["domain"])]
            self.cookie表.append(干净)

    def 预置截图(self, 图) -> None:
        self._截图 = 图

    def 挂加载回调(self, 回调) -> None:
        self._加载回调 = 回调

    def 触发加载完成(self, 成功: bool = True) -> None:
        if self._加载回调 is not None:
            self._加载回调(bool(成功))

    # 接口实现

    def 可用(self) -> tuple[bool, str]:        # type: ignore[override]
        return self._可用

    def 打开(self, url: str) -> None:
        self._地址 = str(url)
        self.操作记录.append(("打开", str(url)))

    def 取视图(self):
        return self.视图占位

    def 截图(self):
        return self._截图

    def 页面地址(self) -> str:
        return self._地址

    def 页面标题(self) -> str:
        return self._标题

    def 点(self, x: int, y: int) -> bool:
        self.操作记录.append(("点", int(x), int(y)))
        return True

    def 输入文本(self, 文本: str) -> bool:
        self.操作记录.append(("输入文本", 文本))
        return True

    def 按键(self, 键: str) -> bool:
        self.操作记录.append(("按键", 键))
        return True

    def 取cookie(self, 只要名字: tuple[str, ...] = (),
              域们: tuple[str, ...] = ()) -> list[dict]:
        self.操作记录.append(("取cookie", tuple(只要名字), tuple(域们)))
        out = list(self.cookie表)
        if 只要名字:
            out = [c for c in out if c["name"] in 只要名字]
        if 域们:
            out = [c for c in out if any(域 in c.get("domain", "") for 域 in 域们)]
        return out

    def 清cookie(self) -> None:
        self.操作记录.append(("清cookie",))
        self.cookie表 = []


# ============================================================================
#  引擎工厂
# ============================================================================

#: 可用的引擎类型 → 中文说明（界面/配置里选）
引擎类型 = {
    "qtwebengine": "QtWebEngine（随 PySide6，自带 Chromium）",
    "假": "假引擎（仅测试用，不联网）",
}


def 建引擎(类型名: str = "", 配置: dict | None = None, 父=None) -> 浏览器引擎:
    """按名字建一个引擎；未知/不可用就回退（调用方用 :meth:`浏览器引擎.可用` 判断）。

    :param 类型名: 显式指定（空则看 ``配置["引擎"]``，再空则默认 qtwebengine）
    """
    名 = (类型名 or "").strip()
    if not 名 and isinstance(配置, dict):
        名 = str(配置.get("引擎") or "").strip()
    if not 名:
        名 = "qtwebengine"
    if 名 in ("假", "fake", "test"):
        return 假引擎(配置, 父)
    try:
        from .引擎_QtWebEngine import QtWebEngine引擎
        return QtWebEngine引擎(配置, 父)
    except Exception as e:  # noqa: BLE001
        # 引擎建不出来时返回"不可用"的假引擎，界面据此显示提示（不崩）
        引擎 = 假引擎(配置, 父)
        引擎.设可用(False, f"没有可用的浏览器引擎：{e}")
        return 引擎
