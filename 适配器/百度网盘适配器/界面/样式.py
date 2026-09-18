# 百度网盘适配器/界面/样式.py
"""全局 QSS 样式"""

全局样式表 = """
QGroupBox {
    border: 1px solid #ddd; border-radius: 6px;
    margin-top: 10px; padding-top: 8px; font-weight: bold;
}
QGroupBox::title {
    subcontrol-origin: margin; left: 10px;
    padding: 0 5px; color: #333;
}
QTabBar::tab { padding: 8px 16px; font-weight: bold; }
QTabBar::tab:selected {
    background: #2196F3; color: white; border-radius: 4px;
}
QTableWidget { gridline-color: #e0e0e0; }
QTableWidget::item:selected { background: #BBDEFB; color: #000; }
QHeaderView::section {
    background: #f5f5f5; padding: 6px; border: none;
    border-right: 1px solid #e0e0e0;
    border-bottom: 1px solid #e0e0e0; font-weight: bold;
}
QTreeWidget { background: #fafafa; border: 1px solid #ddd; }
"""


def 应用全局样式(窗口):
    窗口.setStyleSheet(全局样式表)
    适配高DPI表格(窗口)


def 适配高DPI表格(窗口):
    """让表格的表头/行高跟着字号走，避免高 DPI 下表头文字被压扁、串行。

    用户反馈过"表头和序号显示异常"：Windows 上 125%/150% 缩放时，Qt 会把字体放大，
    但 QSS 里写死的 padding / 固定表头高度不会跟着变，于是表头文字挤在一起、
    看着像乱码。这里按当前字体度量重新算表头高度和行高。
    """
    try:
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QTableWidget, QTreeWidget
    except Exception:      # pragma: no cover - 没有 Qt 时直接跳过
        return
    度量 = 窗口.fontMetrics()
    表头高 = max(28, 度量.height() + 16)
    行高 = max(26, 度量.height() + 14)
    for 表 in 窗口.findChildren(QTableWidget):
        横向 = 表.horizontalHeader()
        横向.setMinimumHeight(表头高)
        横向.setSectionResizeMode(横向.ResizeMode.Interactive)
        横向.setStretchLastSection(False)
        表.verticalHeader().setDefaultSectionSize(行高)
        表.verticalHeader().setMinimumWidth(max(28, 度量.horizontalAdvance("000") + 8))
        表.setWordWrap(False)
        表.setTextElideMode(Qt.TextElideMode.ElideRight)
    for 树 in 窗口.findChildren(QTreeWidget):
        树.header().setMinimumHeight(表头高)