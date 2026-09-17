# 夸克网盘适配器/界面/页面/日志页.py
"""调试日志页

⚠️⚠️ 线程安全 —— 2026-09-14 段错误事故的修复，改动前务必读这段
================================================================
本页把一个 `logging.Handler` 挂在 **root logger** 上，意味着**任意线程**
的任何一条日志都会进入它的 `emit()`。

关键在于：**logging 是同步调用** —— 谁打的日志，就在谁的线程里跑 `emit()`。
所以 `emit()` 里**绝对不能碰任何 Qt 控件**。

事故现场（core dump 实证，崩溃线程 = 批量上传线程）：

    #0 QTextLayout::lineCount()               libQt6Gui
    #1 QTextCursorPrivate::blockLayout()
    #4 QTextCursor::movePosition()
    #5 QWidgetTextControl::moveCursor()
    #7 libpython3.14                ← 从 Python 调进来

上传线程打一条日志，就在上传线程里 `QTextEdit.append()` + `moveCursor()`，
与主线程的重绘竞争，把 `QTextLayout` 踩坏 → SIGSEGV。
（Qt 自己也会报 `QBasicTimer::start: Timers cannot be started from
another thread` —— 连光标闪烁定时器都被跨线程启动了。）

正确做法：`emit()` 只做一件事 —— `宿主.新日志.emit(消息)`。
`新日志` 是 `日志页`（QObject，住在 GUI 线程）的信号；跨线程 emit 时
Qt **自动排队**到 GUI 线程执行 `_追加日志`。零锁、零竞争、零阻塞。

为什么离屏测试测不出来：跨线程改 QWidget 是**数据竞争**，只有主线程
**同时在重绘同一控件**时才踩坏。offscreen 下主线程基本空闲，
竞争窗口极小 —— 所以必须用 `验证/界面线程安全回归.py` 那种
「主线程持续重绘 + 工作线程狂打日志」的方式才能覆盖。
"""
import logging

from PySide6.QtCore import Signal
from PySide6.QtGui import QFont, QTextCursor
from PySide6.QtWidgets import QTextEdit, QVBoxLayout

# 长时间批量上传会刷几千行，限制文档块数，避免内存无限增长
日志页最多保留行数 = 5000


class 界面日志过滤器(logging.Filter):
    """决定哪些日志该进界面

    控制台仍保留完整 DEBUG（排障要看 httpx 的请求明细）；
    界面只收：

      · 本项目（`夸克网盘.*`）的 INFO 及以上
      · **任何**来源的 WARNING 及以上（别人的报错也要看得见）

    这样 httpx 每个 HTTP 请求的 INFO 就不会再往日志页里刷 ——
    实测每个小文件共 12 条记录，其中 7 条是 httpx 的请求日志，
    占了 58%，全是没有阅读价值的噪音。
    """

    def filter(self, 记录) -> bool:
        if 记录.levelno >= logging.WARNING:
            return True
        return 记录.name.startswith("夸克网盘")


class 日志页(QTextEdit):
    """调试日志页：挂一个 logging.Handler 到 root logger（线程安全）"""

    # 跨线程投递通道：工作线程 emit → GUI 线程 append
    新日志 = Signal(str)

    def __init__(self, 主窗口, parent=None):
        super().__init__(parent)
        self.主窗口 = 主窗口
        self._处理器: logging.Handler | None = None

        self.setReadOnly(True)
        self.setFont(QFont("Consolas, 微软雅黑", 9))
        self.setStyleSheet(
            "background: #1e1e1e; color: #d4d4d4; border: 1px solid #333;")
        self.document().setMaximumBlockCount(日志页最多保留行数)

        # 用 QVBoxLayout 包裹一层，便于以后扩展
        外框 = QVBoxLayout(self)
        外框.setContentsMargins(0, 0, 0, 0)

        # 本对象住在 GUI 线程 → 跨线程 emit 自动排队，槽一定在 GUI 线程执行
        self.新日志.connect(self._追加日志)
        # 控件销毁时摘掉 handler，避免工作线程往已销毁的控件投递
        self.destroyed.connect(self._控件销毁)

    def 设置启用(self, 已登录: bool):
        pass

    # ---------------- GUI 线程侧 ----------------

    def _追加日志(self, 样式消息: str):
        """**只在 GUI 线程执行**（由 `新日志` 排队投递而来）"""
        self.append(样式消息)
        self.moveCursor(QTextCursor.End)

    def _控件销毁(self, *_):
        self._移除处理器()

    def _移除处理器(self):
        if self._处理器 is not None:
            try:
                logging.getLogger().removeHandler(self._处理器)
            except Exception:
                pass
            self._处理器 = None

    # ---------------- 安装 ----------------

    def 添加日志处理器(self):
        self._安装处理器()

    def _安装处理器(self):
        if self._处理器 is not None:
            return
        宿主 = self

        class 界面日志处理器(logging.Handler):
            """`emit()` 可能跑在**任意工作线程**上

            ⚠️ 这里唯一允许的动作就是 `新日志.emit()` —— 投递到 GUI 线程。
            不许 append、不许 moveCursor、不许碰任何 QWidget。
            """

            def emit(自身, 记录):
                try:
                    消息 = 自身.format(记录)
                    颜色 = {
                        logging.DEBUG: "#808080",
                        logging.INFO: "#d4d4d4",
                        logging.WARNING: "#DCDCAA",
                        logging.ERROR: "#F48771",
                        logging.CRITICAL: "#C586C0",
                    }.get(记录.levelno, "#d4d4d4")
                    宿主.新日志.emit(
                        f'<span style="color: {颜色};">{消息}</span>')
                except Exception:
                    # logging 明确要求 handler 不得抛异常（会反噬调用方，
                    # 在 except 块里打日志时尤其危险）。此处可能还持有 GIL
                    # 干活，静默吞掉是唯一安全的选择。
                    pass

        处理器 = 界面日志处理器()
        处理器.setLevel(logging.INFO)          # DEBUG 不进界面
        处理器.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
        处理器.addFilter(界面日志过滤器())
        logging.getLogger().addHandler(处理器)
        self._处理器 = 处理器
