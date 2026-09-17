# 光鸭云盘适配器/界面/页面/基类.py
"""页面基类：持有主窗口引用、统一启动后台任务"""

import logging

from PySide6.QtWidgets import QWidget

from ..公共线程 import 网络查询线程

logger = logging.getLogger("光鸭云盘.界面.页面")


class 页面基类(QWidget):
    """所有 Tab 页面的基类

    - 通过 self.主窗口 访问网络客户端、认证服务、活动线程列表
    - 通过 self.启动网络任务() 提交后台请求，自动连接清理逻辑
    """

    def __init__(self, 主窗口, parent=None):
        super().__init__(parent)
        self.主窗口 = 主窗口

    @property
    def 网络(self):
        return self.主窗口.网络

    @property
    def 认证(self):
        return self.主窗口.认证

    def 启动网络任务(self, 任务名: str, 函数,
                     成功回调, 失败回调=None):
        线程 = 网络查询线程(任务名, 函数)
        线程.成功.connect(成功回调)
        if 失败回调 is not None:
            线程.失败.connect(失败回调)
        else:
            线程.失败.connect(self._默认失败)
        线程.finished.connect(self.主窗口._线程完成清理)
        self.主窗口.活动线程.append(线程)
        线程.start()

    def _默认失败(self, 任务名: str, 错误: str):
        logger.error(f"任务 {任务名} 失败：{错误}")

    def 设置启用(self, 已登录: bool):
        """子类覆盖：根据登录态启用/禁用按钮"""
        pass

    def 首次进入(self):
        """子类覆盖：Tab 首次激活时自动加载数据"""
        pass

    def 重置(self):
        """子类覆盖：退出登录时清空状态"""
        pass