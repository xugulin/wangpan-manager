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