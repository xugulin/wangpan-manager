# 光鸭云盘适配器/界面/页面/日志页.py
"""调试日志页"""

import logging

from PySide6.QtWidgets import QVBoxLayout, QTextEdit
from PySide6.QtGui import QFont, QTextCursor


class 日志页(QTextEdit):
    """调试日志页：挂一个 logging.Handler 到 root logger"""

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

    def 设置启用(self, 已登录: bool):
        pass

    def 添加日志处理器(self):
        self._安装处理器()

    def _安装处理器(self):
        class 界面日志处理器(logging.Handler):
            def __init__(自身, 文本编辑):
                super().__init__()
                自身.文本编辑 = 文本编辑

            def emit(自身, 记录):
                消息 = 自身.format(记录)
                颜色 = {
                    logging.DEBUG: "#808080",
                    logging.INFO: "#d4d4d4",
                    logging.WARNING: "#DCDCAA",
                    logging.ERROR: "#F48771",
                    logging.CRITICAL: "#C586C0",
                }.get(记录.levelno, "#d4d4d4")
                样式消息 = f'<span style="color: {颜色};">{消息}</span>'
                自身.文本编辑.append(样式消息)
                自身.文本编辑.moveCursor(QTextCursor.End)

        处理器 = 界面日志处理器(self)
        处理器.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
        logging.getLogger().addHandler(处理器)