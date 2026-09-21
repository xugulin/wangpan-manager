# v8_3/播放/播放出口.py
"""播放出口：**画面往哪里画**的唯一实现（所有播放路径都走这里）。

用户要求"播放视频归口为统一接口"，也是四次游离窗口 bug 的教训：
以前"把画面交出去"散在播放页、独立窗口、画面自愈、AI 重载四五个地方，每处各写一套，
同一类 bug 换条路径就复发一次。

真机矩阵实测（用户这台机器：KDE Wayland + XWayland + Intel vaapi，光鸭 4K60 HEVC 直链）
============================================================================
====================================================  ==================  ========
做法                                                   vout window 模块    游离窗口
====================================================  ==================  ========
``set_xwindow(视频控件自己的 X11 窗口)``                "embed-xid,any" ✓   无 ✓
``set_xwindow(…)`` + 软件解码                          "embed-xid,any" ✓   无 ✓
``set_xwindow(…)`` + VLC 自选 vout（gl + vaapi 互操作）  "embed-xid,any" ✓   无 ✓
实例级 ``--drawable-xid`` + ``--embedded-video``        "any" ✗            **有** ✗
媒体级 ``:vout=xxx``                                   不生效（VLC 报 matching "any"）
====================================================  ==================  ========

所以规矩只有三条：

1. **必须**用 ``libvlc_media_player_set_xwindow()`` —— 这是 libvlc 唯一会触发嵌入的 API
   （实例级 drawable 不会，它会退化成"自己开一个顶层窗口"）；
2. 交给它的窗口要**由 Qt 自己管**：把视频控件设成 ``WA_NativeWindow``，它就有了真正的
   X11 子窗口，尺寸/位置/映射全由 Qt 跟着布局走。**不要自己另建窗口再去算偏移** ——
   非原生控件的 ``winId()`` 是顶层窗口号，自己算出来的偏移会差一个控件位置，
   画面就会歪到右下角还被裁掉（用户实测反馈过）；
3. **永远不要销毁**这个窗口（它是 Qt 的，而且 VLC 可能还在往里画）。停播时只要
   ``停止并等待``，不要把 drawable 拆掉 —— 拆了 VLC 会另开一个窗口（用户实测过）。

交接（页面 ⇄ 独立窗口）也归口在这里：换窗口必须"**先停干净 → 换绑 → 重开 → 跳回原位置**"，
并且**先把会话的出口换成目标窗口的出口**再起播；一边起播一边用着另一个窗口的出口，
正是"关掉独立窗口又冒出 VLC 窗口"的原因。
"""

from __future__ import annotations

from typing import Callable, Optional

__all__ = ["播放出口"]


class 播放出口:
    """播放画面的唯一出口。

    用法（所有播放路径都该这么写）::

        出口 = 播放出口(控件=视频控件, 日志回调=self._写日志)
        self.会话.出口 = 出口            # ★ 交出去之前先换出口（交接纪律）
        self.会话.起播(出口.句柄())
        ...
        出口.销毁()                      # 只清自己的引用，**不拆窗口**

    句柄说明：X11 上返回**视频控件自己的 X11 窗口号**（把它设成原生窗口）；
    不可嵌入的平台（Wayland 原生 / 离屏）返回 0 —— 那时 libvlc 走无窗口解码，
    绝不把无效句柄递给它（会段错误闪退）。
    """

    def __init__(self, 控件=None, 日志回调: Optional[Callable] = None):
        self.控件 = 控件
        self._日志 = 日志回调 or (lambda *_a, **_k: None)
        self._原生过 = False
        self._已绑句柄 = 0
        #: 人类可读的一句话（日志/自检用）：画面到底画在哪个窗口上
        self.说明 = "还没取窗口"

    # ---------------- 句柄 ----------------

    def 可以嵌入(self) -> bool:
        """当前平台能不能把画面嵌进本程序的窗口（X11/xcb 才行）。"""
        try:
            from PySide6.QtGui import QGuiApplication
            from .显示环境 import 可嵌入窗口
            return bool(可嵌入窗口(QGuiApplication.platformName()))
        except Exception:  # noqa: BLE001
            return False

    def 就绪(self) -> bool:
        """窗口建好了、而且**真的在屏幕上**吗（交给 libvlc 之前必须为真）。

        为什么必须看 X 的映射状态：Qt 说"可见"不等于 X 已经 map；给一个还没上屏的窗口，
        VLC 的嵌入尝试会失败，然后**自己开一个顶层窗口**（用户实测过的游离窗口）。
        """
        if not self.可以嵌入():
            return True                    # 不嵌入就没有"上屏"这回事
        try:
            from ..界面.窗口就绪 import 窗口就绪
            return bool(窗口就绪(self.控件))
        except Exception:  # noqa: BLE001
            return True

    def 句柄(self) -> int:
        """交给 libvlc 的窗口号（0 = 无窗口解码；绝不给无效句柄）。"""
        if not self.可以嵌入() or self.控件 is None:
            self.说明 = ("当前平台没有 X11 窗口号：按无窗口模式解码（看不到画面）"
                      if not self.可以嵌入() else "没有视频控件")
            return 0
        # ⚠️ 绝不能在后台线程里创建窗口：Qt 的控件操作只能在界面线程做
        #    （真机崩溃现场之一就是非界面线程走到 Qt 里）。
        import threading
        if threading.current_thread() is not threading.main_thread():
            self._日志(f"[显示] ⚠️ 非界面线程（{threading.current_thread().name}）"
                     f"请求播放窗口号，已拒绝（避免崩溃）")
            return 0
        try:
            from PySide6.QtCore import Qt
            # ★ 关键：让**这个控件自己**有一个真正的 X11 子窗口。
            #   非原生控件的 winId() 是顶层窗口号 —— 那样 VLC 会把画面画到
            #   顶层窗口的 (0,0)，既歪又盖住左边的功能栏（用户实测的"没对齐"）。
            #
            # ⚠️ 顺序与重入（真机 SEGV 的第二个现场，coredump 里是
            #    setAttribute → createWinId 递归把自己压爆）：
            #    `setAttribute(WA_NativeWindow)` 会**立即创建原生窗口**，而这个过程
            #    会派发事件（WinIdChange/Show/Resize），我们的回调可能再次走到
            #    `句柄()` —— 如果标志位是"调用之后"才置位，就会无限递归。
            #    所以：先置位、再看 testAttribute，两个条件都挡住重入。
            if not self._原生过 and not self.控件.testAttribute(
                    Qt.WidgetAttribute.WA_NativeWindow):
                self._原生过 = True                  # 先置位（防重入）
                self.控件.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
            else:
                self._原生过 = True
            号 = int(self.控件.winId())       # 触发生成 + 取号
            if 号 != self._已绑句柄:
                self._已绑句柄 = 号
                self.说明 = f"视频控件自己的 X11 窗口 {号}（Qt 管尺寸/位置）"
                self._日志(f"[显示] 播放窗口号 {号}（视频控件自己的 X11 子窗口，"
                         f"尺寸/位置由 Qt 跟着布局走）")
            return 号
        except Exception as e:  # noqa: BLE001
            self.说明 = f"取窗口号失败：{e}"
            self._日志(f"[显示] 取播放窗口号失败：{e}")
            return 0

    # ---------------- 建播放器 ----------------

    def 建播放器(self, 日志回调: Optional[Callable] = None,
              实例视频输出: str = ""):
        """按统一规矩建一个 libvlc 播放器（唯一入口）。"""
        from .vlc绑定 import VLC
        号 = self.句柄()
        # 没有 drawable（无窗口解码）时才允许钉 vout：嵌入路径上 set_xwindow 会把
        # 实例级 --vout 重置成 "any"，钉了也没用（矩阵已证）。
        钉 = str(实例视频输出 or "") if not 号 else ""
        播放器 = VLC(窗口句柄=号, 日志回调=日志回调 or self._日志,
                   实例视频输出=钉)
        if 号:
            # 再绑一次（幂等）：起播瞬间那次有时会被 VLC 忽略，补一次最稳。
            播放器.绑定窗口(号)
        return 播放器

    # ---------------- 交接 ----------------

    def 交接(self, 会话, 位置: float = -1.0, 理由: str = "") -> bool:
        """把**正在播的**会话接到本出口：先停干净 → 换出口 → 换绑 → 重开 → 跳回位置。

        为什么必须"重开"而不是只 ``set_xwindow``：VLC 3 里**播放中**改 drawable
        不会把已有 vout 搬过去（句柄记下了、画面还留在原处），于是两边的画面/声音
        就对不上；而"不重开"还会让旧 vout 与新 vout 抢窗口 —— 正是关掉独立窗口后
        又冒出 VLC 自己窗口的原因。

        交接纪律：**先把会话的出口换成自己**（``会话.出口 = self``）再起播，
        否则起播会用着旧出口的句柄（一边起播一边指向即将销毁的窗口）。
        """
        if 会话 is None:
            return False
        if 位置 is None or float(位置) < 0:
            try:
                位置 = float(会话.播放器.进度秒()) if 会话.播放器 else 0.0
            except Exception:  # noqa: BLE001
                位置 = 0.0
        位置 = float(位置 or 0.0)
        try:                                  # ★ 交接纪律：先换出口
            会话.出口 = self
        except Exception:  # noqa: BLE001
            pass
        try:
            会话.播放器.停止并等待(3.0)        # 先停干净（旧 vout 必须释放）
            # ⚠️ 还要等 vout **真的没了**：不然旧 drawable（即将关闭的独立窗口）
            #    会被 vout 线程踩到 —— 实测会卡死或让 VLC 另开一个窗口。
            会话.播放器.等vout消失(3.0)
        except Exception:  # noqa: BLE001
            pass
        号 = self.句柄()
        if not 号:
            self._日志(f"[显示] {理由 or '交接'}失败：没有可嵌入的窗口号")
            return False
        成功 = bool(会话.起播(号))
        if 成功 and 位置 > 1.0:
            会话.跳转(位置)
        self._日志(f"[显示] {理由 or '画面交接'}"
                 + ("成功" if 成功 else "失败")
                 + (f"（从 {位置:.0f}s 继续）" if 成功 and 位置 > 1.0 else ""))
        return 成功

    # ---------------- 生命周期 ----------------

    def 同步(self) -> None:
        """不需要做任何事：窗口是 Qt 的，尺寸/位置由 Qt 跟着布局走。

        以前这里要把自建子窗口挪到控件位置 —— 那正是"画面没对齐"的来源
        （非原生控件的 ``winId()`` 是顶层窗口号，自己算偏移必然差一个控件位置）。
        """
        return

    def 销毁(self) -> None:
        """只清引用，**不拆窗口**。

        窗口是 Qt 的（WA_NativeWindow 那个），拆掉它等于把 VLC 正在画的 drawable
        从底下抽走 —— VLC 会另开一个顶层窗口放画面（用户实测：关掉独立窗口后又
        冒出 "VLC media player"）。真要收画面就 ``停止并等待``，别拆窗口。
        """
        self._已绑句柄 = 0
        return
