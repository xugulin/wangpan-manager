# 夸克网盘适配器/界面/主界面.py
"""
夸克网盘管理工具 - 主窗口

职责：
  - 账户区（登录 / 短信登录 / 重新登录 / 全局刷新 / 退出）
  - Tab 组装
  - 二维码弹窗调度
  - 生命周期 / 线程清理 / 分享对话框调度

具体页面逻辑见 界面/页面/ 下的子模块。
"""

import logging
import os
import sys

项目根目录 = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if 项目根目录 not in sys.path:
    sys.path.insert(0, 项目根目录)

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QGroupBox, QMessageBox, QTabWidget,
    QDialog,
)
from PySide6.QtCore import QThread

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("夸克网盘.界面")

from 核心.认证.认证服务 import 认证服务
from 核心.认证.凭证仓库 import 全局凭证仓库
from 核心.网络.网络客户端 import 网络客户端

from .样式 import 应用全局样式
from .公共线程 import 扫码登录线程
from .弹窗.二维码弹窗 import 二维码弹窗
from .弹窗.Cookie登录对话框 import Cookie登录对话框
from .弹窗.分享对话框 import 分享对话框
from .页面.文件浏览页 import 文件浏览页
# 阶段一清理：已移除 上传记录页 / 云添加页 的 import（两页已删除，PLAN.md §6.1-Q1/Q2）
from .页面.分享页 import 分享页
from .页面.账号信息页 import 账号信息页
from .页面.日志页 import 日志页


class 主界面(QMainWindow):
    def __init__(self):
        super().__init__()
        # ----- 运行时状态 -----
        self.网络: 网络客户端 | None = None
        self.认证 = 认证服务()
        self.活动线程: list[QThread] = []
        self.扫码线程: 扫码登录线程 | None = None
        self.二维码弹窗实例: 二维码弹窗 | None = None

        # ----- 界面 -----
        self.设置窗口()
        应用全局样式(self)
        self.日志页.添加日志处理器()
        self.检查已有登录()

    # ---------------- 界面搭建 ----------------

    def 设置窗口(self):
        self.setWindowTitle("夸克网盘管理工具")
        self.setGeometry(100, 100, 1200, 880)

        中央部件 = QWidget()
        self.setCentralWidget(中央部件)
        主布局 = QVBoxLayout(中央部件)

        主布局.addWidget(self._构建登录区())

        self.Tab = QTabWidget()
        self.文件浏览页 = 文件浏览页(self)
        self.分享页 = 分享页(self)
        self.账号信息页 = 账号信息页(self)
        self.日志页 = 日志页(self)

        self.Tab.addTab(self.文件浏览页, "📁 文件浏览")
        self.Tab.addTab(self.分享页, "🔗 分享")
        self.Tab.addTab(self.账号信息页, "👤 账号信息")
        self.Tab.addTab(self.日志页, "📋 调试日志")
        self.Tab.currentChanged.connect(self._标签页切换)

        主布局.addWidget(self.Tab, stretch=1)

        # 文件浏览页发出分享请求 → 主窗口弹对话框
        self.文件浏览页.分享文件请求.connect(self._处理分享请求)

    def _构建登录区(self) -> QGroupBox:
        登录组 = QGroupBox("账户")
        登录布局 = QVBoxLayout()

        状态行 = QHBoxLayout()
        self.状态标签 = QLabel("⚪ 未登录")
        self.状态标签.setStyleSheet(
            "font-size: 14px; font-weight: bold; color: gray;")
        状态行.addWidget(self.状态标签)
        状态行.addStretch()

        self.令牌路径标签 = QLabel(f"凭证文件：{全局凭证仓库.文件路径}")
        self.令牌路径标签.setStyleSheet("color: #999; font-size: 11px;")
        状态行.addWidget(self.令牌路径标签)
        登录布局.addLayout(状态行)

        按钮行 = QHBoxLayout()

        self.登录按钮 = QPushButton("🔐 扫码登录")
        self.登录按钮.setStyleSheet(
            "background: #4CAF50; color: white; padding: 8px 16px; "
            "border-radius: 4px; font-weight: bold;")
        self.登录按钮.setToolTip("使用夸克 App 扫描二维码授权登录")
        self.登录按钮.clicked.connect(self.开始扫码登录)
        按钮行.addWidget(self.登录按钮)

        self.短信登录按钮 = QPushButton("🍪 手动 Cookie")
        self.短信登录按钮.setStyleSheet(
            "background: #9C27B0; color: white; padding: 8px 16px; "
            "border-radius: 4px; font-weight: bold;")
        self.短信登录按钮.setToolTip(
            "粘贴浏览器中复制的夸克 Cookie 登录（阶段三将换成专用对话框）")
        self.短信登录按钮.clicked.connect(self.开始手动Cookie登录)
        按钮行.addWidget(self.短信登录按钮)

        self.重新登录按钮 = QPushButton("🔄 重新登录")
        self.重新登录按钮.setStyleSheet(
            "background: #FF9800; color: white; padding: 8px 16px; "
            "border-radius: 4px; font-weight: bold;")
        self.重新登录按钮.clicked.connect(self.重新登录)
        self.重新登录按钮.setEnabled(False)
        按钮行.addWidget(self.重新登录按钮)

        self.全局刷新按钮 = QPushButton("🔄 全局刷新")
        self.全局刷新按钮.setStyleSheet(
            "background: #2196F3; color: white; padding: 8px 16px; "
            "border-radius: 4px; font-weight: bold;")
        self.全局刷新按钮.setToolTip("刷新当前标签页数据")
        self.全局刷新按钮.clicked.connect(self.全局刷新)
        self.全局刷新按钮.setEnabled(False)
        按钮行.addWidget(self.全局刷新按钮)

        self.退出登录按钮 = QPushButton("🚪 退出登录")
        self.退出登录按钮.setStyleSheet(
            "background: #F44336; color: white; padding: 8px 16px; "
            "border-radius: 4px; font-weight: bold;")
        self.退出登录按钮.clicked.connect(self.退出登录)
        self.退出登录按钮.setEnabled(False)
        按钮行.addWidget(self.退出登录按钮)

        按钮行.addStretch()
        登录布局.addLayout(按钮行)
        登录组.setLayout(登录布局)
        return 登录组

    # ---------------- 登录状态 ----------------

    def 检查已有登录(self):
        cookie = 全局凭证仓库.取Cookie(安全余量=60.0)
        if cookie:
            logger.info("发现已保存的有效凭证（Cookie）")
            self.状态标签.setText("🟢 已登录（凭证有效）")
            self.状态标签.setStyleSheet(
                "font-size: 14px; font-weight: bold; color: green;")
            self.初始化网络客户端(cookie)
            self.切换登录状态(True)
            self.文件浏览页.加载目录("", "/")
        else:
            logger.info("没有有效凭证，需要登录")
            self.切换登录状态(False)

    def 切换登录状态(self, 已登录: bool):
        self.登录按钮.setEnabled(not 已登录)
        self.短信登录按钮.setEnabled(not 已登录)
        self.重新登录按钮.setEnabled(已登录)
        self.退出登录按钮.setEnabled(已登录)
        self.全局刷新按钮.setEnabled(已登录)

        self.文件浏览页.设置启用(已登录)
        self.分享页.设置启用(已登录)
        self.账号信息页.设置启用(已登录)

    def 初始化网络客户端(self, cookie: str):
        self.网络 = 网络客户端(cookie=cookie)
        logger.debug(
            f"网络客户端已初始化，凭证长度：{len(cookie or '')} 字节")

    # ---------------- 扫码登录 ----------------

    def 开始扫码登录(self):
        if 全局凭证仓库.取Cookie():
            QMessageBox.information(
                self, "提示", "当前已登录，无需重复登录")
            return

        if self.扫码线程 is not None:
            for 信号名 in ("二维码", "状态", "成功", "失败"):
                try:
                    getattr(self.扫码线程, 信号名).disconnect()
                except Exception:
                    pass
            self.扫码线程 = None

        self.登录按钮.setEnabled(False)
        self.短信登录按钮.setEnabled(False)
        self.状态标签.setText("🟡 正在获取二维码…")
        self.状态标签.setStyleSheet(
            "font-size: 14px; font-weight: bold; color: #FF9800;")

        self.扫码线程 = 扫码登录线程()
        self.扫码线程.二维码.connect(self.显示二维码弹窗)
        self.扫码线程.状态.connect(self._扫码状态更新)
        self.扫码线程.成功.connect(self.登录成功处理)
        self.扫码线程.失败.connect(self.登录失败处理)
        self.扫码线程.finished.connect(self._线程完成清理)
        self.活动线程.append(self.扫码线程)
        self.扫码线程.start()

    def _扫码状态更新(self, 文本: str):
        """扫码轮询进度 → 状态栏 + 二维码弹窗"""
        self.状态标签.setText(f"🟡 {文本}")
        if self.二维码弹窗实例 is not None:
            try:
                self.二维码弹窗实例.设置状态(文本)
            except Exception:
                pass

    def 显示二维码弹窗(self, 链接: str, 用户码: str):
        logger.info(f"授权链接：{链接}")
        self.状态标签.setText("🟡 等待扫码…")
        self.二维码弹窗实例 = 二维码弹窗(链接, 用户码, 父窗口=self)
        self.二维码弹窗实例.finished.connect(self._二维码弹窗关闭)
        self.二维码弹窗实例.show()

    def _二维码弹窗关闭(self, 结果: int):
        logger.info(f"[界面] 二维码弹窗已关闭（result={结果}）")
        self.二维码弹窗实例 = None

        if self.扫码线程 is not None:
            for 信号名 in ("二维码", "状态", "成功", "失败"):
                try:
                    getattr(self.扫码线程, 信号名).disconnect()
                except Exception:
                    pass
            self.扫码线程 = None

        if not 全局凭证仓库.取Cookie():
            self.登录按钮.setEnabled(True)
            self.短信登录按钮.setEnabled(True)
            self.状态标签.setText("⚪ 未登录（可重新登录）")
            self.状态标签.setStyleSheet(
                "font-size: 14px; font-weight: bold; color: gray;")

    def 登录成功处理(self, 令牌数据: dict):
        发送者 = self.sender()
        if 发送者 is not None and self.扫码线程 is not None \
                and 发送者 is not self.扫码线程:
            logger.info("[界面] 忽略过期扫码线程的成功回调")
            return

        logger.info("[界面] 扫码登录成功")
        self.状态标签.setText("🟢 已登录（扫码登录）")
        self.状态标签.setStyleSheet(
            "font-size: 14px; font-weight: bold; color: green;")
        self.切换登录状态(True)

        if self.二维码弹窗实例 is not None:
            try:
                self.二维码弹窗实例.accept()
            except Exception:
                pass
            self.二维码弹窗实例 = None

        cookie = 全局凭证仓库.取Cookie()
        self.初始化网络客户端(cookie)
        QMessageBox.information(self, "成功", "登录成功！")
        self.文件浏览页.加载目录("", "/")

    def 登录失败处理(self, 错误: str):
        发送者 = self.sender()
        if 发送者 is not None and self.扫码线程 is not None \
                and 发送者 is not self.扫码线程:
            logger.info("[界面] 忽略过期扫码线程的失败回调")
            return

        logger.error(f"[界面] 扫码登录失败：{错误}")
        self.状态标签.setText(f"🔴 登录失败：{错误}")
        self.状态标签.setStyleSheet(
            "font-size: 14px; font-weight: bold; color: red;")
        self.登录按钮.setEnabled(True)
        self.短信登录按钮.setEnabled(True)
        if self.二维码弹窗实例 is not None:
            try:
                self.二维码弹窗实例.reject()
            except Exception:
                pass
            self.二维码弹窗实例 = None

    # ---------------- 手动 Cookie 登录（3.2b 专用对话框）----------------

    def 开始手动Cookie登录(self):
        if 全局凭证仓库.取Cookie():
            QMessageBox.information(
                self, "提示", "当前已登录，无需重复登录")
            return

        对话框 = Cookie登录对话框(父窗口=self)
        if 对话框.exec() == QDialog.Accepted and 对话框.登录结果:
            self._Cookie登录成功(对话框.登录结果)

    def _Cookie登录成功(self, 结果: dict):
        logger.info("[界面] 手动 Cookie 登录成功")
        self.状态标签.setText("🟢 已登录（手动 Cookie）")
        self.状态标签.setStyleSheet(
            "font-size: 14px; font-weight: bold; color: green;")
        self.切换登录状态(True)

        cookie = 全局凭证仓库.取Cookie()
        self.初始化网络客户端(cookie)
        QMessageBox.information(self, "登录成功", "已通过手动 Cookie 登录")
        self.文件浏览页.加载目录("", "/")

    # ---------------- 重新 / 退出 ----------------

    def 重新登录(self):
        应答 = QMessageBox.question(
            self, "重新登录",
            "将清除当前登录状态并重新登录，是否继续？")
        if 应答 != QMessageBox.Yes:
            return

        self._执行退出()

        选择框 = QMessageBox(self)
        选择框.setWindowTitle("选择登录方式")
        选择框.setText("请选择登录方式：")
        选择框.setIcon(QMessageBox.Question)
        扫码按钮 = 选择框.addButton("🔐 扫码登录", QMessageBox.YesRole)
        Cookie按钮 = 选择框.addButton(
            "🍪 手动 Cookie 登录", QMessageBox.NoRole)
        取消按钮 = 选择框.addButton("取消", QMessageBox.RejectRole)
        选择框.setDefaultButton(扫码按钮)
        选择框.exec()

        点击 = 选择框.clickedButton()
        if 点击 is None or 点击 is 取消按钮:
            return
        if 点击 is 扫码按钮:
            self.开始扫码登录()
        elif 点击 is Cookie按钮:
            self.开始手动Cookie登录()

    def 退出登录(self):
        应答 = QMessageBox.question(
            self, "退出登录",
            "确定要退出登录吗？\n\n本地保存的凭证文件将被删除，"
            "下次需重新登录。")
        if 应答 != QMessageBox.Yes:
            return
        self._执行退出()
        QMessageBox.information(self, "提示", "已退出登录")

    def _执行退出(self):
        logger.info("[界面] 用户退出登录")
        全局凭证仓库.清空()
        if self.网络 is not None:
            try:
                self.网络.设置Cookie(None)
            except Exception:
                pass
        self.网络 = None

        self.状态标签.setText("⚪ 未登录")
        self.状态标签.setStyleSheet(
            "font-size: 14px; font-weight: bold; color: gray;")
        self.切换登录状态(False)

        self.文件浏览页.重置()
        self.分享页.重置()
        self.账号信息页.重置()

        if self.二维码弹窗实例 is not None:
            try:
                self.二维码弹窗实例.reject()
            except Exception:
                pass
            self.二维码弹窗实例 = None

        logger.info("[界面] 已退出登录")

    # ---------------- 刷新分发 ----------------

    def _标签页切换(self, 索引: int):
        if not self.网络:
            return
        页面 = self.Tab.widget(索引)
        if hasattr(页面, "首次进入"):
            页面.首次进入()

    def 全局刷新(self):
        if not self.网络:
            QMessageBox.warning(self, "提示", "请先登录")
            return

        当前索引 = self.Tab.currentIndex()
        if 当前索引 == 0:
            self.文件浏览页.刷新当前视图()
        elif 当前索引 == 1:
            self.分享页.上次刷新时间 = 0.0
            self.分享页.加载分享列表()
        elif 当前索引 == 2:
            self.账号信息页.刷新全部()
        else:
            logger.info("日志页无需刷新")

    # ---------------- 分享 ----------------

    def _处理分享请求(self, 选中: list):
        """文件浏览页发出分享请求：弹分享对话框 → 调分享页创建"""
        if not self.网络:
            QMessageBox.warning(self, "提示", "请先登录")
            return

        对话框 = 分享对话框(选中, 父窗口=self)
        if 对话框.exec() != QDialog.Accepted:
            return
        配置 = 对话框.取配置()
        if not 配置:
            return
        self.分享页.执行创建分享(配置)

    # ---------------- 生命周期 ----------------

    def _线程完成清理(self):
        """线程结束后，从活动列表移除并交给 Qt 延迟删除

        ⚠️ 不能直接 del；必须用 deleteLater()，让 Qt 在事件循环里
        安全地销毁 QThread 的 C++ 对象（此时线程已完全退出）。
        """
        线程 = self.sender()
        if 线程 is None:
            return
        try:
            if 线程 in self.活动线程:
                self.活动线程.remove(线程)
        except Exception:
            pass
        try:
            if isinstance(线程, QThread):
                线程.deleteLater()
        except Exception:
            pass

    def closeEvent(self, 事件):
        logger.info("[界面] 开始关闭窗口，停止线程...")

        for 线程 in list(self.活动线程):
            try:
                if 线程.isRunning():
                    线程.quit()
                    线程.wait(2000)
            except Exception:
                pass
        self.活动线程.clear()

        try:
            if self.网络:
                self.网络.关闭()
        except Exception:
            pass
        try:
            if self.认证:
                self.认证.关闭()
        except Exception:
            pass

        logger.info("[界面] 窗口关闭完成")
        事件.accept()


def 运行界面():
    app = QApplication(sys.argv)
    窗口 = 主界面()
    窗口.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    运行界面()