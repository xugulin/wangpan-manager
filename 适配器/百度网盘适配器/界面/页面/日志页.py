# 百度网盘适配器/界面/页面/日志页.py
"""调试日志页

🔴 线程安全要点（2026-09-14 段错误事故根因）
--------------------------------------------
`logging` 是**全局且跨线程**的：root logger 上加的 Handler 会被
**任何线程**的日志调用触发 —— 上传/下载跑在 `网络查询线程`（QThread）里，
它们的 `logger.info(...)` 会在**工作线程**里执行 `Handler.emit()`。

因此 `emit()` 里**绝对不能碰任何 QWidget**。
之前的实现直接在 emit 里做了::

    自身.文本编辑.append(样式消息)
    自身.文本编辑.moveCursor(QTextCursor.End)

后果（gdb 从 coredump 拿到的真实栈）::

    TID 279601 (网络查询线程)  SEGV_MAPERR
    #0 QTextLayout::lineCount()
    #4 QTextCursor::movePosition()
    #5 QWidgetTextControl::moveCursor()
    #6 QtWidgets.abi3.so          ← 从 Python 调进 QtWidgets

即：工作线程在改 QTextEdit 的文档，主线程同时在为同一个文档做
FreeType 字体布局（主线程栈就是 libfreetype/libpng16），
QTextLayout 内部指针被并发访问 → 段错误。

✅ 正确做法：Handler 只负责把消息通过 **Qt 信号**发出去。
   跨线程信号是队列投递（QueuedConnection），槽函数一定在主线程执行，
   Qt 会自己保证线程亲和性 —— 不需要我们手动 processEvents。
"""
from __future__ import annotations

import logging

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont, QTextCursor
from PySide6.QtWidgets import QTextEdit, QVBoxLayout

# 界面最多保留多少行（避免长时间运行后文档无限增长拖慢布局）
最大行数 = 5000


class 日志页(QTextEdit):
    """调试日志页：挂一个 logging.Handler 到 root logger。"""

    # 信号参数必须是可跨线程投递的类型：字符串 + 颜色都用 str
    收到日志 = Signal(str, str)

    def __init__(self, 主窗口, parent=None):
        super().__init__(parent)
        self.主窗口 = 主窗口

        self.setReadOnly(True)
        self.setFont(QFont("Consolas, 微软雅黑", 9))
        self.setStyleSheet(
            "background: #1e1e1e; color: #d4d4d4; border: 1px solid #333;")

        # 用 QVBoxLayout 包裹一层，便于以后扩展
        外框 = QVBoxLayout(self)
        外框.setContentsMargins(0, 0, 0, 0)

        # ✅ 队列投递：无论哪个线程 emit，槽都在主线程执行
        self.收到日志.connect(self._追加日志, Qt.QueuedConnection)

    def 设置启用(self, 已登录: bool):
        pass

    def 添加日志处理器(self):
        self._安装处理器()

    # ---------------- 主线程侧：真正碰控件的地方 ----------------

    def _追加日志(self, 样式消息: str, 颜色: str):
        """只在主线程执行（由 收到日志 信号保证）。"""
        try:
            self.append(样式消息)
            self.moveCursor(QTextCursor.End)
            # 超长时裁掉最老的一半，避免文档无限增长
            if self.document().blockCount() > 最大行数:
                self.document().setMaximumBlockCount(最大行数)
        except RuntimeError:
            # 窗口已销毁（退出过程中）——忽略即可
            pass

    # ---------------- Handler ----------------

    def _安装处理器(self):
        页面 = self

        class 界面日志处理器(logging.Handler):
            """只做「格式化 + 发信号」，**不碰任何 QWidget**。"""

            def __init__(自身):
                super().__init__()
                自身.setFormatter(logging.Formatter(
                    "%(asctime)s [%(levelname)s] %(name)s: %(message)s"))

            def emit(自身, 记录):
                # ⚠️ 这里可能跑在任意线程，只能做纯 Python 操作
                try:
                    消息 = 自身.format(记录)
                except Exception:
                    return
                颜色 = {
                    logging.DEBUG: "#808080",
                    logging.INFO: "#d4d4d4",
                    logging.WARNING: "#DCDCAA",
                    logging.ERROR: "#F48771",
                    logging.CRITICAL: "#C586C0",
                }.get(记录.levelno, "#d4d4d4")
                转义 = (消息.replace("&", "&amp;")
                            .replace("<", "&lt;")
                            .replace(">", "&gt;"))
                样式消息 = f'<span style="color: {颜色};">{转义}</span>'
                try:
                    页面.收到日志.emit(样式消息, 颜色)
                except RuntimeError:
                    # 页面已销毁（退出中）
                    pass

        处理器 = 界面日志处理器()
        logging.getLogger().addHandler(处理器)
