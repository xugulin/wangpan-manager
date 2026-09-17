# 百度网盘适配器/界面/主界面.py
"""
百度网盘管理工具 - 主窗口

职责：
  - 账户区（扫码登录 / 导入 Cookie / 重新登录 / 全局刷新 / 退出）
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
    QDialog, QInputDialog,
)
from PySide6.QtCore import QThread

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)

# 🔴 必须压掉传输层的 DEBUG：httpcore 会把**完整响应头**打进日志，
#    其中包含服务端下发的 `Set-Cookie: BDUSS=...; STOKEN=...` ——
#    等于把账号凭证明文写进控制台和「调试日志」面板，一旦被复制粘贴
#    外发就等于泄露账号。业务日志（百度网盘.*）保持 DEBUG 不变。
#    确需排查 TLS/连接层时：DSH_HTTP_DEBUG=1 启动即可放开。
if os.environ.get("DSH_HTTP_DEBUG") == "1":
    print("[启动] ⚠️ DSH_HTTP_DEBUG=1：已开启 httpx/httpcore DEBUG，"
          "日志会包含明文 Cookie，请勿外发")
else:
    for _库名 in ("httpx", "httpcore", "hpack", "h11"):
        logging.getLogger(_库名).setLevel(logging.WARNING)

logger = logging.getLogger("百度网盘.界面")

from 核心.认证.认证服务 import 认证服务
from 核心.认证.会话仓库 import 全局会话仓库
from 核心.网络.网络客户端 import 网络客户端

from .样式 import 应用全局样式
from .公共线程 import 扫码登录线程
from .弹窗.二维码弹窗 import 二维码弹窗
from .弹窗.分享对话框 import 分享对话框
from .页面.文件浏览页 import 文件浏览页
from .页面.上传记录页 import 上传记录页
from .页面.分享页 import 分享页
from .页面.账号信息页 import 账号信息页
from .页面.日志页 import 日志页


# ==========================================================================
# Tab 索引常量（✅ 替代原先散落的硬编码 `索引 == 2` / `当前索引 == 0`）
# 顺序必须与 `_构建界面` 里的 addTab 调用顺序保持一致。
# ==========================================================================
Tab_文件浏览 = 0
Tab_上传记录 = 1
Tab_分享 = 2
Tab_账号信息 = 3
Tab_日志 = 4
Tab_总数 = 5


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
        # ⛔ 不要在这里加「定时 gc.collect()」——见 文件头 GC 说明。
        #    实测代价：整堆一次 27~30ms；实测风险：跨线程/与 Qt 对象并存时
        #    整堆回收会段错误。上传是流式的，内存不会无限涨。

    # ---------------- 界面搭建 ----------------

    def 设置窗口(self):
        self.setWindowTitle("百度网盘管理工具")
        self.setGeometry(100, 100, 1200, 880)

        中央部件 = QWidget()
        self.setCentralWidget(中央部件)
        主布局 = QVBoxLayout(中央部件)

        主布局.addWidget(self._构建登录区())

        self.Tab = QTabWidget()
        self.文件浏览页 = 文件浏览页(self)
        self.上传记录页 = 上传记录页(self)
        self.分享页 = 分享页(self)
        self.账号信息页 = 账号信息页(self)
        self.日志页 = 日志页(self)

        self.Tab.addTab(self.文件浏览页, "📁 文件浏览")
        self.Tab.addTab(self.上传记录页, "📤 上传记录")
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

        self.令牌路径标签 = QLabel(f"令牌文件：{全局会话仓库.文件路径}")
        self.令牌路径标签.setStyleSheet("color: #999; font-size: 11px;")
        状态行.addWidget(self.令牌路径标签)
        登录布局.addLayout(状态行)

        按钮行 = QHBoxLayout()

        self.登录按钮 = QPushButton("🔐 扫码登录")
        self.登录按钮.setStyleSheet(
            "background: #4CAF50; color: white; padding: 8px 16px; "
            "border-radius: 4px; font-weight: bold;")
        self.登录按钮.setToolTip("使用百度网盘 App 扫描二维码授权登录")
        self.登录按钮.clicked.connect(self.开始扫码登录)
        按钮行.addWidget(self.登录按钮)

        self.导入Cookie按钮 = QPushButton("🔑 导入 Cookie")
        self.导入Cookie按钮.setStyleSheet(
            "background: #9C27B0; color: white; padding: 8px 16px; "
            "border-radius: 4px; font-weight: bold;")
        self.导入Cookie按钮.setToolTip(
            "百度网盘无短信登录流程，改为直接粘贴浏览器 Cookie 导入会话")
        self.导入Cookie按钮.clicked.connect(self.开始导入Cookie)
        按钮行.addWidget(self.导入Cookie按钮)

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
        令牌 = 全局会话仓库.获取bduss()
        if 令牌:
            logger.info("发现已保存的有效令牌")
            self.状态标签.setText("🟢 已登录（令牌有效）")
            self.状态标签.setStyleSheet(
                "font-size: 14px; font-weight: bold; color: green;")
            self.初始化网络客户端(令牌)
            self.切换登录状态(True)
            self.文件浏览页.加载目录("", "/")
        else:
            logger.info("没有有效令牌，需要登录")
            self.切换登录状态(False)

    def 切换登录状态(self, 已登录: bool):
        self.登录按钮.setEnabled(not 已登录)
        self.导入Cookie按钮.setEnabled(not 已登录)
        self.重新登录按钮.setEnabled(已登录)
        self.退出登录按钮.setEnabled(已登录)
        self.全局刷新按钮.setEnabled(已登录)

        self.文件浏览页.设置启用(已登录)
        self.上传记录页.设置启用(已登录)
        self.分享页.设置启用(已登录)
        self.账号信息页.设置启用(已登录)

    def 初始化网络客户端(self, 令牌: str):
        self.网络 = 网络客户端()
        self.网络.设置访问令牌(令牌)
        logger.debug(f"网络客户端已初始化，令牌前8位：{令牌[:8]}...")

    # ---------------- 扫码登录 ----------------

    def 开始扫码登录(self):
        if 全局会话仓库.获取bduss():
            QMessageBox.information(
                self, "提示", "当前已登录，无需重复登录")
            return

        if self.扫码线程 is not None:
            for 信号名 in ("二维码", "扫码状态", "成功", "失败"):
                try:
                    getattr(self.扫码线程, 信号名).disconnect()
                except Exception:
                    pass
            self.扫码线程 = None

        self.登录按钮.setEnabled(False)
        self.导入Cookie按钮.setEnabled(False)
        self.状态标签.setText("🟡 正在获取授权码...")
        self.状态标签.setStyleSheet(
            "font-size: 14px; font-weight: bold; color: #FF9800;")

        self.扫码线程 = 扫码登录线程()
        self.扫码线程.二维码.connect(self.显示二维码弹窗)
        self.扫码线程.扫码状态.connect(self.更新扫码状态)
        self.扫码线程.成功.connect(self.登录成功处理)
        self.扫码线程.失败.connect(self.登录失败处理)
        self.扫码线程.finished.connect(self._线程完成清理)
        self.活动线程.append(self.扫码线程)
        self.扫码线程.start()

    def 显示二维码弹窗(self, 二维码内容: str, 标识: str):
        """百度侧「二维码内容」是服务端下发的 base64 PNG，「标识」是 sign。"""
        logger.info(f"已获取二维码（内容长度 {len(二维码内容)}，标识 {标识[:12]}）")
        self.状态标签.setText("🟡 等待扫码…")
        self.二维码弹窗实例 = 二维码弹窗(二维码内容, 标识, 父窗口=self)
        self.二维码弹窗实例.finished.connect(self._二维码弹窗关闭)
        self.二维码弹窗实例.show()

    def 更新扫码状态(self, 状态: int):
        """四态：0 等待扫码 / 1 已扫描 / 2 已确认。"""
        文案 = {0: "🟡 等待扫码…", 1: "🔵 已扫描，请在手机上确认",
                2: "🟢 已确认，正在登录…"}
        self.状态标签.setText(文案.get(int(状态), "🟡 等待扫码…"))
        dialog = getattr(self, "二维码弹窗实例", None)
        if dialog is not None:
            try:
                dialog.更新状态(int(状态))
            except Exception:
                pass

    def _二维码弹窗关闭(self, 结果: int):
        """二维码弹窗关闭 —— **只关窗，不拆信号**。

        🔴 曾经的严重缺陷：这里把 二维码/扫码状态/**成功**/失败 四个信号
        全部 disconnect 掉。而登录是在**后台线程**里跑完的，用户完全可能
        在「手机上确认」之后、`成功` 信号投递到主线程之前就关掉弹窗
        （实测就是这个时序）。一旦断开，`成功` 就**永久丢失**：
        会话其实已经写进仓库、登录已经成功，但界面永远等不到回调，
        停在「等待扫码」看起来就像卡死，只能重启程序才发现已登录。

        正确做法：弹窗关闭只是 UI 行为，**不能**影响后台登录流程的信号；
        线程与信号的清理统一交给 `_线程完成清理`（线程真正结束后触发）。
        """
        logger.info(f"[界面] 二维码弹窗已关闭（result={结果}）")
        self.二维码弹窗实例 = None
        # 兜底：若弹窗关闭时登录其实已经完成（会话已落库），立刻补一次刷新，
        # 避免用户看不到已登录状态。
        if 全局会话仓库.获取bduss() and self.网络 is None:
            try:
                self.登录成功处理({})
            except Exception as e:
                logger.warning(f"[界面] 弹窗关闭后补刷新失败：{e}")

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

        令牌 = 全局会话仓库.获取bduss()
        self.初始化网络客户端(令牌)
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
        self.导入Cookie按钮.setEnabled(True)
        if self.二维码弹窗实例 is not None:
            try:
                self.二维码弹窗实例.reject()
            except Exception:
                pass
            self.二维码弹窗实例 = None

    # ---------------- 手工导入 Cookie（替代短信登录） ----------------

    def 开始导入Cookie(self):
        """百度网盘无短信登录流程；这里改为直接粘贴浏览器 Cookie 导入会话。

        实测最小必需集为 `BDUSS + STOKEN`，推荐连 `BDUSS_BFESS` 一起带
        （见 PLAN.md §4.6）。
        """
        if 全局会话仓库.获取bduss():
            QMessageBox.information(self, "提示", "当前已登录，无需重复登录")
            return

        文本, 确定 = QInputDialog.getMultiLineText(
            self, "导入 Cookie",
            "请从浏览器（已登录 pan.baidu.com）复制任意请求的 Cookie 头粘贴到下方：\n"
            "必需：BDUSS、STOKEN（推荐再加 BDUSS_BFESS）",
            "")
        if not 确定 or not (文本 or "").strip():
            return

        try:
            命中 = 全局会话仓库.从cookie文本导入(文本)
        except Exception as e:
            QMessageBox.warning(self, "导入失败", f"Cookie 解析失败：\n{e}")
            return

        if not 全局会话仓库.是否有效():
            QMessageBox.warning(
                self, "导入失败",
                f"未识别到必需凭证。\n识别到的 Cookie：{命中 or '无'}\n\n"
                f"至少需要 BDUSS 与 STOKEN。")
            return

        self._导入Cookie成功(命中)

    def _导入Cookie成功(self, 命中: list):
        logger.info(f"[界面] Cookie 导入成功：{命中}")
        self.状态标签.setText("🟢 已登录（Cookie 导入）")
        self.状态标签.setStyleSheet(
            "font-size: 14px; font-weight: bold; color: green;")
        self.切换登录状态(True)
        self.初始化网络客户端(全局会话仓库.获取bduss())
        QMessageBox.information(
            self, "登录成功",
            f"已导入 {len(命中)} 项 Cookie：{', '.join(命中)}\n"
            f"用户 ID：{全局会话仓库.获取uk() or '（登录后自动获取）'}")
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
        导入按钮 = 选择框.addButton("🔑 导入 Cookie", QMessageBox.NoRole)
        取消按钮 = 选择框.addButton("取消", QMessageBox.RejectRole)
        选择框.setDefaultButton(扫码按钮)
        选择框.exec()

        点击 = 选择框.clickedButton()
        if 点击 is None or 点击 is 取消按钮:
            return
        if 点击 is 扫码按钮:
            self.开始扫码登录()
        elif 点击 is 导入按钮:
            self.开始导入Cookie()

    def 退出登录(self):
        应答 = QMessageBox.question(
            self, "退出登录",
            "确定要退出登录吗？\n\n本地保存的令牌文件将被删除，"
            "下次需重新登录。")
        if 应答 != QMessageBox.Yes:
            return
        self._执行退出()
        QMessageBox.information(self, "提示", "已退出登录")

    def _执行退出(self):
        logger.info("[界面] 用户退出登录")
        全局会话仓库.清空()
        if self.网络 is not None:
            try:
                self.网络.设置访问令牌(None)
            except Exception:
                pass
        self.网络 = None

        self.状态标签.setText("⚪ 未登录")
        self.状态标签.setStyleSheet(
            "font-size: 14px; font-weight: bold; color: gray;")
        self.切换登录状态(False)

        self.文件浏览页.重置()
        self.上传记录页.重置()
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
        if 当前索引 == Tab_文件浏览:
            self.文件浏览页.刷新当前视图()
        elif 当前索引 == Tab_上传记录:
            self.上传记录页.加载上传记录()
        elif 当前索引 == Tab_分享:
            self.分享页.刷新当前视图()
        elif 当前索引 == Tab_账号信息:
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

        ✅ 扫码线程的**信号断开**也放在这里：只有线程真正结束、成功/失败
        信号都已经投递完毕之后才断开，才不会丢回调
        （详见 `_二维码弹窗关闭` 的说明）。
        """
        线程 = self.sender()
        if 线程 is None:
            return
        # 扫码线程：收尾 —— 先断开信号，再清引用
        if 线程 is getattr(self, "扫码线程", None):
            for 信号名 in ("二维码", "扫码状态", "成功", "失败"):
                try:
                    getattr(线程, 信号名).disconnect()
                except Exception:
                    pass
            self.扫码线程 = None
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