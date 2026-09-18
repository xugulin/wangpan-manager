"""统一登录对话框：一个入口，6 种登录方式。

| 方式 | 说明 | 依赖适配器核心提供 |
|---|---|---|
| 🔳 扫码登录 | 图片二维码（百度/夸克）或设备码（光鸭：验证地址 + 用户码） | 各家的登录服务 |
| 🍪 导入 Cookie | 粘贴 Cookie / BDUSS+STOKEN（百度、夸克） | 手工导入接口 |
| 📱 短信登录 | 手机号 + 验证码（光鸭） | 短信登录接口 |
| 📧 邮箱登录 | 目前三家适配器都未提供 | 无 → 面板给出说明与兜底 |
| 🔑 令牌登录 | 直接填 access/refresh 令牌（光鸭） | 令牌仓库写入 |
| 👤 账号密码 | 目前三家适配器都未提供 | 无 → 面板给出说明与兜底 |

* 能力来自桥命令 ``auth_caps``（各适配器按其核心真实支持的能力申报），
  不支持的页签会禁用并显示原因，**不谎报支持**；
* 不支持的两种方式面板里给「🖥 打开适配器原 GUI」兜底按钮；
* 所有网络调用都走 :class:`文件操作线程`，界面不卡；成功后自动刷新网盘页状态。
"""

from __future__ import annotations

import base64
import time

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QPixmap
from PySide6.QtWidgets import (
    QApplication, QDialog, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QPlainTextEdit, QPushButton, QStackedWidget, QVBoxLayout, QWidget,
)

from .后台线程 import 账号状态线程, 文件操作线程

#: 方式键 → (按钮文字, 图标)
方式标题 = {
    "qrcode": ("🔳 扫码登录", "🔳"),
    "cookie": ("🍪 导入Cookie", "🍪"),
    "sms": ("📱 短信登录", "📱"),
    "email": ("📧 邮箱登录", "📧"),
    "token": ("🔑 令牌登录", "🔑"),
    "password": ("👤 账号密码", "👤"),
}

#: 各网盘 Cookie 需要的关键字段（提示用）
Cookie提示 = {
    "baidu": ("必填 BDUSS 与 STOKEN（浏览器里 pan.baidu.com 的 Cookie）。\n"
              "示例：BDUSS=xxxxxx; STOKEN=yyyyyy; BAIDUID=zzzz",
              "BDUSS=你的值; STOKEN=你的值"),
    "quark": ("粘贴 pan.quark.cn 的完整 Cookie（需含 __pus、__kps、__uid）。\n"
              "示例：__pus=xxx; __kps=yyy; __uid=zzz",
              "__pus=你的值; __kps=你的值; __uid=你的值"),
    "guangya": ("光鸭是令牌型网盘，不支持 Cookie 登录，请用「🔑 令牌登录」。",
              ""),
    "fake": ("假网盘：随便粘一段含 '=' 的文本即可。", "BDUSS=demo"),
}


class 登录对话框(QDialog):
    """一个网盘实例的统一登录入口。"""

    扫码默认超时 = 180.0

    def __init__(self, 主窗口, 实例: dict, 父=None):
        super().__init__(父 or 主窗口)
        self.主窗口 = 主窗口
        self.动作 = 主窗口.动作
        self.实例 = dict(实例)
        self.标识 = self.实例["标识"]
        self.成功 = False
        self.能力: dict = {}
        self._线程: list = []
        self._扫码会话 = ""
        self._短信会话 = ""
        self._短信倒计时 = 0
        self.扫码重试次数 = 0
        self.扫码重试提示 = ""

        self.setWindowTitle(f"登录网盘 · {self.实例.get('名称', self.标识)}")
        self.resize(760, 640)
        self._构建()
        self._加载能力()

    # ==================== 基础 ====================

    @property
    def 适配器(self):
        return self.动作.适配器(self.标识)

    def _日志(self, 文本: str, 级别: str = "信息"):
        self.日志框.appendPlainText(str(文本))
        try:
            self.主窗口.追加日志(f"[登录·{self.实例.get('名称', self.标识)}] {文本}",
                            级别)
        except Exception:
            pass

    def _跑(self, 描述: str, 动作, 处理器=None):
        """把阻塞的网络动作放到后台线程跑；处理器默认按"登录结果"处理。"""
        处理 = 处理器 or self._处理结果
        线程 = 文件操作线程(描述, 动作, self)
        线程.完成.connect(lambda d, r: 处理(r))
        线程.失败.connect(lambda d, e: self._处理异常(e))
        self._线程.append(线程)
        线程.finished.connect(lambda t=线程: self._清理(t))
        线程.start()
        return 线程

    def _清理(self, 线程):
        try:
            self._线程.remove(线程)
        except ValueError:
            pass
        线程.deleteLater()

    # ==================== 界面 ====================

    def _构建(self):
        布局 = QVBoxLayout(self)
        布局.setSpacing(8)

        标题 = QLabel(f"🔐 {self.实例.get('名称', self.标识)}"
                    f"（{self.标识}）· 统一登录")
        标题.setStyleSheet("font-size: 16px; font-weight: bold;")
        布局.addWidget(标题)

        self.当前状态标签 = QLabel("正在读取该网盘支持的登录方式…")
        self.当前状态标签.setWordWrap(True)
        self.当前状态标签.setStyleSheet("font-size: 12px; color: #95a5a6;")
        布局.addWidget(self.当前状态标签)

        方式行 = QHBoxLayout()
        方式行.setSpacing(4)
        self.方式按钮: dict[str, QPushButton] = {}
        for 键, (文字, _图标) in 方式标题.items():
            按钮 = QPushButton(文字)
            按钮.setCheckable(True)
            按钮.setChecked(键 == "qrcode")
            按钮.clicked.connect(lambda _=False, k=键: self._切方式(k))
            方式行.addWidget(按钮)
            self.方式按钮[键] = 按钮
        方式行.addStretch(1)
        布局.addLayout(方式行)

        self.堆叠 = QStackedWidget()
        self.面板: dict[str, QWidget] = {}
        for 键 in 方式标题:
            面板, 容器 = self._建面板(键)
            self.面板[键] = 面板
            self.堆叠.addWidget(容器)
        布局.addWidget(self.堆叠, 1)

        self.状态标签 = QLabel("就绪")
        self.状态标签.setWordWrap(True)
        self.状态标签.setStyleSheet(
            "padding: 8px; border-radius: 4px; background: #34495e;"
            "color: #ecf0f1; font-size: 12px;")
        布局.addWidget(self.状态标签)

        self.日志框 = QPlainTextEdit()
        self.日志框.setReadOnly(True)
        self.日志框.setMaximumHeight(110)
        self.日志框.setPlaceholderText("登录过程日志…")
        布局.addWidget(self.日志框)

        底部 = QHBoxLayout()
        底部.addStretch(1)
        self.原GUI按钮 = QPushButton("🖥 打开适配器原 GUI")
        self.原GUI按钮.setToolTip("需要适配器自带的完整界面时用（独立进程）")
        self.原GUI按钮.clicked.connect(self._打开原GUI)
        底部.addWidget(self.原GUI按钮)
        关闭按钮 = QPushButton("关闭")
        关闭按钮.clicked.connect(self.reject)
        底部.addWidget(关闭按钮)
        布局.addLayout(底部)

    def _建面板(self, 键: str):
        容器 = QWidget()
        容器布局 = QVBoxLayout(容器)
        容器布局.setContentsMargins(0, 0, 0, 0)

        if 键 == "qrcode":
            面板 = self._建扫码面板()
        elif 键 == "cookie":
            面板 = self._建Cookie面板()
        elif 键 == "sms":
            面板 = self._建短信面板()
        elif 键 == "token":
            面板 = self._建令牌面板()
        else:
            面板 = self._建不支持面板(键)
        容器布局.addWidget(面板)
        # 返回 (面板本体, 装它的容器控件)：QStackedWidget.addWidget 只收 QWidget，
        # 早先这里返回的是布局，导致点击「登录 / 管理」直接报
        # "Internal C++ object (QVBoxLayout) already deleted"。
        return 面板, 容器

    # ---------- 扫码 ----------

    def _建扫码面板(self) -> QWidget:
        面板 = QWidget()
        布局 = QVBoxLayout(面板)
        布局.setContentsMargins(0, 0, 0, 0)

        行 = QHBoxLayout()
        self.二维码标签 = QLabel("点「开始扫码」获取二维码")
        self.二维码标签.setFixedSize(240, 240)
        self.二维码标签.setAlignment(Qt.AlignCenter)
        self.二维码标签.setStyleSheet(
            "border: 1px dashed #7f8c8d; border-radius: 6px; color: #95a5a6;")
        行.addWidget(self.二维码标签)

        右侧 = QVBoxLayout()
        self.扫码提示标签 = QLabel(
            "支持两种形态：\n"
            "· 图片二维码：用手机 App 直接扫；\n"
            "· 设备码：在浏览器打开验证地址并输入用户码。")
        self.扫码提示标签.setWordWrap(True)
        右侧.addWidget(self.扫码提示标签)

        self.设备码标签 = QLabel("")
        self.设备码标签.setStyleSheet(
            "font-size: 18px; font-weight: bold; letter-spacing: 2px;")
        self.设备码标签.setTextInteractionFlags(Qt.TextSelectableByMouse)
        右侧.addWidget(self.设备码标签)

        self.验证地址标签 = QLabel("")
        self.验证地址标签.setWordWrap(True)
        self.验证地址标签.setTextInteractionFlags(Qt.TextSelectableByMouse)
        右侧.addWidget(self.验证地址标签)

        按钮行 = QHBoxLayout()
        self.开始扫码按钮 = QPushButton("🔳 开始扫码")
        self.开始扫码按钮.setObjectName("PrimaryButton")
        self.开始扫码按钮.clicked.connect(self._开始扫码)
        按钮行.addWidget(self.开始扫码按钮)
        复制按钮 = QPushButton("📋 复制用户码")
        复制按钮.clicked.connect(lambda: self._复制(self.设备码标签.text()))
        按钮行.addWidget(复制按钮)
        打开按钮 = QPushButton("🌐 打开验证地址")
        打开按钮.clicked.connect(self._打开验证地址)
        按钮行.addWidget(打开按钮)
        self.取消扫码按钮 = QPushButton("⏹ 取消等待")
        self.取消扫码按钮.setEnabled(False)
        self.取消扫码按钮.clicked.connect(self._取消扫码)
        按钮行.addWidget(self.取消扫码按钮)
        右侧.addLayout(按钮行)
        右侧.addStretch(1)
        行.addLayout(右侧, 1)
        布局.addLayout(行)
        return 面板

    # ---------- Cookie ----------

    def _建Cookie面板(self) -> QWidget:
        面板 = QWidget()
        布局 = QVBoxLayout(面板)
        布局.setContentsMargins(0, 0, 0, 0)
        类型 = str(self.实例.get("类型") or "")
        说明, 占位 = Cookie提示.get(类型, Cookie提示["fake"])
        self.Cookie说明标签 = QLabel(说明)
        self.Cookie说明标签.setWordWrap(True)
        布局.addWidget(self.Cookie说明标签)
        self.Cookie输入框 = QPlainTextEdit()
        self.Cookie输入框.setPlaceholderText(占位 or "粘贴 Cookie 文本…")
        self.Cookie输入框.setMinimumHeight(150)
        布局.addWidget(self.Cookie输入框, 1)
        行 = QHBoxLayout()
        粘贴按钮 = QPushButton("📋 从剪贴板粘贴")
        粘贴按钮.clicked.connect(self._粘贴剪贴板)
        行.addWidget(粘贴按钮)
        清空按钮 = QPushButton("🧹 清空")
        清空按钮.clicked.connect(self.Cookie输入框.clear)
        行.addWidget(清空按钮)
        行.addStretch(1)
        self.Cookie登录按钮 = QPushButton("✅ 验证并登录")
        self.Cookie登录按钮.setObjectName("PrimaryButton")
        self.Cookie登录按钮.clicked.connect(self._Cookie登录)
        行.addWidget(self.Cookie登录按钮)
        布局.addLayout(行)

        # 百度专用：从本机浏览器直接取**完整**会话（含 HttpOnly 的 pan 域 STOKEN）
        if 类型 == "baidu":
            浏览器行 = QHBoxLayout()
            self.浏览器导入按钮 = QPushButton("🌐 从浏览器导入完整会话")
            self.浏览器导入按钮.setObjectName("PrimaryButton")
            self.浏览器导入按钮.setToolTip(
                "从带调试端口启动的本机浏览器（本项目的浏览器面板默认已开）读取\n"
                "BDUSS 与 **pan 域 STOKEN** —— 写操作（上传/改名/删除）只认这份。\n"
                "扫码登录拿到的会话常常缺这份 STOKEN，表现就是\n"
                "「已登录、能列目录，但上传/改名/删除一律 errno:-6」。")
            self.浏览器导入按钮.clicked.connect(self._从浏览器导入)
            浏览器行.addWidget(self.浏览器导入按钮)
            说明2 = QLabel(
                "扫码登录后如果提示「写操作不可用（errno:-6）」，用左边这个按钮："
                "先在浏览器里登录 pan.baidu.com，再点它。")
            说明2.setWordWrap(True)
            说明2.setStyleSheet("color: #95a5a6; font-size: 11px;")
            浏览器行.addWidget(说明2, 1)
            布局.addLayout(浏览器行)
        return 面板

    def _从浏览器导入(self):
        """从本机浏览器取完整会话（含 pan 域 STOKEN），解决写权限不完整。"""
        self._设置状态("正在从浏览器读取登录会话（调试端口）…")
        self._跑("从浏览器导入会话",
               lambda 进度: self.适配器.Cookie登录("__取浏览器会话__"))

    # ---------- 短信 ----------

    def _建短信面板(self) -> QWidget:
        面板 = QWidget()
        布局 = QVBoxLayout(面板)
        布局.setContentsMargins(0, 0, 0, 0)
        布局.addWidget(QLabel("手机号（含国家码，例如 +86 13800000000）："))
        行 = QHBoxLayout()
        self.手机号框 = QLineEdit()
        self.手机号框.setPlaceholderText("+86 13800000000")
        行.addWidget(self.手机号框, 1)
        self.发送验证码按钮 = QPushButton("📨 发送验证码")
        self.发送验证码按钮.clicked.connect(self._发送验证码)
        行.addWidget(self.发送验证码按钮)
        布局.addLayout(行)
        布局.addWidget(QLabel("短信验证码："))
        行2 = QHBoxLayout()
        self.验证码框 = QLineEdit()
        self.验证码框.setPlaceholderText("6 位数字")
        self.验证码框.returnPressed.connect(self._短信登录)
        行2.addWidget(self.验证码框, 1)
        self.短信登录按钮 = QPushButton("✅ 登录")
        self.短信登录按钮.setObjectName("PrimaryButton")
        self.短信登录按钮.clicked.connect(self._短信登录)
        行2.addWidget(self.短信登录按钮)
        布局.addLayout(行2)
        self.短信提示标签 = QLabel("")
        self.短信提示标签.setWordWrap(True)
        self.短信提示标签.setStyleSheet("color: #95a5a6; font-size: 12px;")
        布局.addWidget(self.短信提示标签)
        布局.addStretch(1)
        return 面板

    # ---------- 令牌 ----------

    def _建令牌面板(self) -> QWidget:
        面板 = QWidget()
        布局 = QVBoxLayout(面板)
        布局.setContentsMargins(0, 0, 0, 0)
        提示 = QLabel(
            "直接写入令牌（适合从别处拿到 access_token / refresh_token 的情况）。\n"
            "· access_token：必填；\n"
            "· refresh_token：可选，填了就能自动续期。")
        提示.setWordWrap(True)
        布局.addWidget(提示)
        布局.addWidget(QLabel("access_token："))
        self.访问令牌框 = QLineEdit()
        self.访问令牌框.setPlaceholderText("粘贴 access_token")
        self.访问令牌框.setEchoMode(QLineEdit.Password)
        布局.addWidget(self.访问令牌框)
        布局.addWidget(QLabel("refresh_token（可选）："))
        self.刷新令牌框 = QLineEdit()
        self.刷新令牌框.setPlaceholderText("粘贴 refresh_token")
        self.刷新令牌框.setEchoMode(QLineEdit.Password)
        布局.addWidget(self.刷新令牌框)
        显示 = QPushButton("👁 显示/隐藏")
        显示.setCheckable(True)
        显示.toggled.connect(
            lambda 开: [框.setEchoMode(QLineEdit.Normal if 开 else QLineEdit.Password)
                     for 框 in (self.访问令牌框, self.刷新令牌框)])
        行 = QHBoxLayout()
        行.addWidget(显示)
        行.addStretch(1)
        self.令牌登录按钮 = QPushButton("✅ 校验并登录")
        self.令牌登录按钮.setObjectName("PrimaryButton")
        self.令牌登录按钮.clicked.connect(self._令牌登录)
        行.addWidget(self.令牌登录按钮)
        布局.addLayout(行)
        布局.addStretch(1)
        return 面板

    # ---------- 不支持的方式 ----------

    def _建不支持面板(self, 键: str) -> QWidget:
        面板 = QWidget()
        布局 = QVBoxLayout(面板)
        布局.setContentsMargins(0, 0, 0, 0)
        名称 = 方式标题[键][0]
        标题 = QLabel(f"{名称}：该网盘适配器未提供")
        标题.setStyleSheet("font-size: 14px; font-weight: bold; color: #e67e22;")
        布局.addWidget(标题)
        说明 = QLabel("")
        说明.setWordWrap(True)
        布局.addWidget(说明)
        兜底 = QLabel(
            "可以改用该网盘支持的其它方式（见上方页签的可用状态），"
            "或点下面的「🖥 打开适配器原 GUI」用它自带界面登录——"
            "V8_3 只读取登录结果，不会改动适配器源码。")
        兜底.setWordWrap(True)
        兜底.setStyleSheet("color: #95a5a6;")
        布局.addWidget(兜底)
        按钮 = QPushButton("🖥 打开适配器原 GUI")
        按钮.clicked.connect(self._打开原GUI)
        行 = QHBoxLayout()
        行.addWidget(按钮)
        行.addStretch(1)
        布局.addLayout(行)
        布局.addStretch(1)
        面板._说明标签 = 说明          # 能力加载后填原因
        return 面板

    # ==================== 能力 ====================

    def _加载能力(self):
        self._跑("读取登录方式", lambda 进度: self.适配器.登录方式表(),
               处理器=lambda r: self._应用能力(r))

    def _应用能力(self, 能力):
        if not isinstance(能力, dict) or not 能力:
            self.当前状态标签.setText("⚠️ 没读到登录能力，默认只显示扫码/Cookie 可用")
            能力 = {键: {"支持": 键 in ("qrcode", "cookie"),
                       "名称": 方式标题[键][0].split(" ", 1)[-1],
                       "说明": "未能读取适配器能力，按最小集合启用"}
                  for 键 in 方式标题}
        self.能力 = dict(能力 or {})
        支持 = [k for k, v in self.能力.items() if v.get("支持")]
        名称 = [self.能力[k]["名称"] for k in 支持]
        self.当前状态标签.setText(
            f"该网盘支持：{'、'.join(名称) if 名称 else '（无）'}"
            f"　|　不适用的方式已置灰并说明原因")
        for 键, 按钮 in self.方式按钮.items():
            项 = self.能力.get(键, {})
            可用 = bool(项.get("支持"))
            按钮.setEnabled(可用)
            按钮.setToolTip(项.get("说明", ""))
            if not 可用:
                按钮.setText(f"{方式标题[键][1]} {项.get('名称', 键)}（不支持）")
            面板 = self.面板.get(键)
            if not 可用 and hasattr(面板, "_说明标签"):
                面板._说明标签.setText(str(项.get("说明", "该适配器未提供此登录方式")))
        # 默认选中第一个可用的方式
        for 键 in 方式标题:
            if self.能力.get(键, {}).get("支持"):
                self._切方式(键)
                break
        self._日志(f"登录方式：{self.当前状态标签.text()}")

    def _切方式(self, 键: str):
        for 其他键, 按钮 in self.方式按钮.items():
            按钮.setChecked(其他键 == 键)
        顺序 = list(方式标题)
        if 键 in 顺序:
            self.堆叠.setCurrentIndex(顺序.index(键))
        self._设置状态(f"当前方式：{方式标题[键][0]}")

    # ==================== 通用 ====================

    def _设置状态(self, 文本: str, 颜色: str = "#34495e",
                  文字色: str = "#ecf0f1"):
        self.状态标签.setText(str(文本))
        self.状态标签.setStyleSheet(
            f"padding: 8px; border-radius: 4px; background: {颜色};"
            f"color: {文字色}; font-size: 12px;")

    def _处理结果(self, 结果):
        if not isinstance(结果, dict):
            self._设置状态("返回结构异常（已忽略）", "#c0392b")
            return
        状态 = str(结果.get("状态") or "")
        if 状态 == "成功":
            self.成功 = True
            self._设置状态(f"✅ {结果.get('消息', '登录成功')}"
                       + (f"　{结果['提示']}" if 结果.get("提示") else ""),
                      "#27ae60")
            self._日志("✅ " + str(结果.get("消息", "登录成功")))
            账号 = 结果.get("账号") or {}
            if 账号:
                self._日志(f"账号：{账号.get('user') or '-'}"
                        f"（{账号.get('data_dir') or ''}）")
                try:
                    self.主窗口.设置网盘状态(self.标识, bool(账号.get("logged_in")))
                except Exception:
                    pass
            self._通知成功()
        elif 状态 == "不支持":
            self._设置状态(f"⚠️ {结果.get('消息', '该方式不被支持')}", "#e67e22")
        elif 状态 in ("超时", "已取消"):
            self._设置状态(f"⏹ {结果.get('消息', 状态)}", "#e67e22")
            self.取消扫码按钮.setEnabled(False)
        elif 状态 == "失败" and _像超时(结果):
            # 有的后端把"等待超时"报成 失败（带或不带 超时 字段），
            # 按超时处理：不吓人、提示可重试，会话多半还活着。
            self._设置状态(
                f"⏳ {结果.get('消息', '等待超时')}　"
                "（可再点「🔳 开始扫码」重试；关闭对话框会取消本次会话）",
                "#e67e22")
            self.取消扫码按钮.setEnabled(False)
        else:
            self._设置状态(f"❌ {结果.get('消息', '登录失败')}"
                       + (f"　{结果['提示']}" if 结果.get("提示") else ""),
                      "#c0392b")
            self._日志("❌ " + str(结果.get("消息", "登录失败")))
            self.取消扫码按钮.setEnabled(False)

    def _处理异常(self, 错误):
        self._设置状态(f"❌ 操作失败：{error短(错误)}", "#c0392b")
        self._日志(f"异常：{错误}", "警告")
        self.取消扫码按钮.setEnabled(False)
        self.开始扫码按钮.setEnabled(True)
        self.发送验证码按钮.setEnabled(True)
        self.Cookie登录按钮.setEnabled(True)
        self.令牌登录按钮.setEnabled(True)

    def _通知成功(self):
        self._停止核对()
        """登录成功后让网盘页**立刻列一次目录**，状态栏并行刷新。

        顺序很关键：以前只调 `刷新网盘状态()`，而列目录要等账号状态查询
        （取模板变量 + 取用户信息）回来才触发 —— 用户感觉就是"登录完半天不出文件"。
        现在先起列目录，再起状态查询，两条链路并行。
        """
        页 = None
        try:
            页 = self.主窗口._网盘页面.get(self.标识)
        except Exception:
            页 = None
        if 页 is not None:
            try:
                页.登录后立即加载()
            except Exception as e:  # noqa: BLE001
                self._日志(f"登录后立即列目录失败（不影响登录）：{e}", "警告")
        try:
            self.主窗口.刷新网盘状态(self.标识)
        except Exception:
            pass
        self.关闭计时 = QTimer.singleShot(1500, self.accept)

    def _打开原GUI(self):
        规格 = self.动作.规格(self.标识)
        if 规格 is None:
            QMessageBox.warning(self, "不可用", "该网盘未启用")
            return
        try:
            规格.启动适配器GUI()
            self._日志(f"已启动 {规格.显示名} 原 GUI（独立进程）")
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "启动失败", str(e))

    @staticmethod
    def _复制(文本: str):
        应用 = QApplication.instance()
        if 应用 is not None and 文本:
            应用.clipboard().setText(文本)

    def _粘贴剪贴板(self):
        应用 = QApplication.instance()
        if 应用 is None:
            return
        文本 = 应用.clipboard().text()
        if 文本:
            self.Cookie输入框.setPlainText(文本)

    def _打开验证地址(self):
        地址 = self.验证地址标签.text().strip()
        if 地址.startswith("http"):
            QDesktopServices.openUrl(QUrl(地址))
        else:
            QMessageBox.information(self, "提示", "还没有验证地址，先点「开始扫码」")

    # ==================== 各方式实现 ====================

    def _开始扫码(self):
        self.开始扫码按钮.setEnabled(False)
        self.扫码重试次数 = 0
        self._设置状态("正在获取二维码…")
        self._跑("获取二维码", lambda 进度: self._扫码开始一次(首次=True),
               处理器=lambda r: self._扫码已开始(r))

    #: 「扫码开始」是一次**短**请求（取一张码），超时多为冷启动/网络抖动。
    #: 实测：桥进程冷启动 + 首次 TLS 建连偶尔会顶到 60s 客户端超时，而重试
    #: 一次就秒回（用户现场那条 `[guangya/login_qr_start] 适配器调用超时（60.0s）`
    #: 之后紧接着登录就成功了，就是这个形态）。所以这里自己兜一次重试。
    扫码开始最多尝试 = 3

    def _扫码开始一次(self, 首次: bool = False) -> dict:
        适配器 = self.适配器
        try:
            return dict(适配器.扫码开始() or {})
        except Exception as 异常:  # noqa: BLE001 - 网络抖动/超时都要能兜住
            文本 = f"{type(异常).__name__}: {异常}"
            if self.扫码重试次数 + 1 >= self.扫码开始最多尝试:
                raise
            self.扫码重试次数 += 1
            self.扫码重试提示 = (
                f"获取二维码超时（{error短(文本, 80)}），"
                f"正在自动重试第 {self.扫码重试次数} 次…")
            try:
                self._日志(f"获取二维码失败，自动重试第 {self.扫码重试次数} 次：{文本}",
                        "警告")
            except Exception:
                pass
            import time as _time
            _time.sleep(0.8)
            return self._扫码开始一次(首次=False)

    def _扫码已开始(self, 信息):
        self.开始扫码按钮.setEnabled(True)
        if not isinstance(信息, dict):
            self._设置状态("二维码获取失败（返回结构异常）", "#c0392b")
            return
        # 协议规定 login_qr_start 失败时也返回 {"状态": "失败"} 而不是抛异常，
        # 所以这里必须先看状态，否则会先显示"等待扫码"再立刻收到失败。
        状态 = str(信息.get("状态") or "")
        if 状态 in ("失败", "不支持"):
            self._设置状态(f"❌ 获取二维码失败：{信息.get('消息', 状态)}"
                       + (f"　{信息['提示']}" if 信息.get("提示") else ""),
                      "#c0392b")
            self._日志(f"获取二维码失败：{信息.get('消息', 状态)}", "警告")
            return
        类型 = str(信息.get("类型") or "图片")
        self._扫码会话 = str(信息.get("会话") or "")
        提示 = str(信息.get("提示") or "")
        地址 = str(信息.get("验证地址") or "")
        用户码 = str(信息.get("用户码") or "")
        图片 = str(信息.get("图片base64") or "")
        链接 = str(信息.get("二维码链接") or 信息.get("链接") or "")
        # ⚠️ 设备授权码型（光鸭）：后端只给「验证地址」这一条 URL，界面以前既不画码
        #    也不当链接，用户看到的是一段说明文字 —— 这就是"二维码显示不出来"。
        #    适配器原 GUI 的做法就是把这条地址本身画成二维码，这里保持一致。
        if not (图片 or 链接) and 地址.startswith("http"):
            链接 = 地址
        if 图片:
            try:
                数据 = base64.b64decode(图片)
                图 = QPixmap()
                if 图.loadFromData(数据):
                    self.二维码标签.setPixmap(
                        图.scaled(230, 230, Qt.KeepAspectRatio,
                                Qt.SmoothTransformation))
                else:
                    self.二维码标签.setText("二维码图片解析失败")
            except Exception as e:  # noqa: BLE001
                self.二维码标签.setText(f"二维码解析失败：{e}")
        elif 链接 and self._本地渲染二维码(链接):
            pass                      # 有些网盘只给链接，界面这边自己画二维码
        elif 地址:
            self.二维码标签.setText(f"{类型}\n请在浏览器打开右侧地址")
        self.设备码标签.setText(f"用户码：{用户码}" if 用户码 else "")
        self.验证地址标签.setText(地址)
        默认提示 = ("请用手机扫描二维码完成授权"
                 if 链接 else "请用手机扫码 / 打开验证地址完成授权")
        self.扫码提示标签.setText(提示 or 默认提示)
        self._日志(f"二维码已就绪（{类型}）"
                 + (f" 验证地址：{地址}" if 地址 else "")
                 + (f" 用户码：{用户码}" if 用户码 else ""))
        超时 = float(信息.get("有效期秒") or self.扫码默认超时)
        if self.扫码重试提示:
            self._日志(self.扫码重试提示)
            self.扫码重试提示 = ""
        self._设置状态("等待扫码/授权…（扫码完成后会自动登录）", "#f39c12")
        self.取消扫码按钮.setEnabled(True)
        self._跑("等待扫码", lambda 进度: self.适配器.扫码等待(
            self._扫码会话, min(超时, self.扫码默认超时)))
        self._开始核对登录()

    # ---------------- 扫码期间主动核对（防止"已登录却一直显示等待中"） ----------------

    def _开始核对登录(self) -> None:
        """每隔几秒查一次账号状态。

        用户反馈：扫码后界面一直停在"等待中"，重启才发现其实已经登录成功 ——
        适配器内部的扫码轮询有时就是不上报成功。与其等它，不如直接问账号状态，
        一旦已登录就当成功处理（顺带立刻加载文件列表）。

        间隔 6 秒（原来是 3 秒）：核对期间扫码轮询本身也在打同一个桥进程，
        频次太高会让"扫码 → 换令牌"这两步互相抢连接，反而更容易失败。
        """
        if getattr(self, "_核对计时", None) is None:
            self._核对计时 = QTimer(self)
            self._核对计时.setInterval(6000)
            self._核对计时.timeout.connect(self._核对一次)
        self._核对中 = False
        self._核对计时.start()

    def _停止核对(self) -> None:
        计时 = getattr(self, "_核对计时", None)
        if 计时 is not None:
            计时.stop()

    def _核对一次(self) -> None:
        if self.成功 or getattr(self, "_核对中", False):
            return
        适配器 = getattr(self, "适配器", None)
        if 适配器 is None:
            return
        self._核对中 = True
        线程 = 账号状态线程(self.标识, lambda _标识: 适配器, self)
        def 好(_标识: str, 信息: dict):
            self._核对中 = False
            if self.成功:
                return
            if 信息.get("logged_in"):
                self._停止核对()
                self._处理结果({"状态": "成功",
                            "消息": "扫码登录成功（已核对账号状态）",
                            "账号": 信息})
        def 坏(_标识: str, _错误: str):
            self._核对中 = False
        线程.成功.connect(好)
        线程.失败.connect(坏)
        # ⚠️ 必须"跑完就从列表里摘掉再 deleteLater"：deleteLater 会把 C++ 对象
        #    销毁，而 Python 包装还在列表里 → 之后任何 isRunning()/wait() 都会
        #    抛 "libshiboken: Internal C++ object already deleted"，
        #    关窗时也就没法正确等待它们（现场 abort 的一部分原因）。
        线程.finished.connect(lambda t=线程: self._核对线程收工(t))
        if not hasattr(self, "_核对线程们"):
            self._核对线程们 = []
        self._核对线程们.append(线程)
        线程.start()

    def _核对线程收工(self, 线程) -> None:
        try:
            if 线程 in self._核对线程们:
                self._核对线程们.remove(线程)
        except Exception:
            pass
        try:
            线程.deleteLater()
        except Exception:
            pass

    def _本地渲染二维码(self, 内容: str) -> bool:
        """有些网盘只给一条链接/验证地址（夸克、光鸭），界面这边自己画二维码。

        渲染参数与适配器原 GUI 对齐（纠错 M、box_size 10、border 2）——
        光鸭的验证地址有 139 字符，纠错等级调低一点码点更疏、更好扫。
        """
        try:
            import io

            import qrcode
            码 = qrcode.QRCode(
                version=None,
                error_correction=qrcode.constants.ERROR_CORRECT_M,
                box_size=10, border=2,
            )
            码.add_data(内容)
            码.make(fit=True)
            图 = 码.make_image(fill_color="black", back_color="white")
            缓冲 = io.BytesIO()
            图.save(缓冲, format="PNG")
            像素 = QPixmap()
            if not 像素.loadFromData(缓冲.getvalue()) or 像素.isNull():
                raise RuntimeError("QPixmap 解码失败")
            self.二维码标签.setPixmap(
                像素.scaled(230, 230, Qt.KeepAspectRatio,
                          Qt.SmoothTransformation))
            self.扫码提示标签.setText(
                "用手机扫描二维码；也可以直接打开右侧验证地址。")
            return True
        except Exception as e:  # noqa: BLE001
            self.二维码标签.setText(
                "只拿到二维码链接\n（本地渲染不可用："
                f"{str(e)[:60]}）\n请用手机浏览器打开右侧地址")
            return False

    def _取消扫码(self):
        self._设置状态("正在取消等待…", "#e67e22")
        try:
            self.适配器.扫码取消(self._扫码会话)
        except Exception:
            pass
        self.取消扫码按钮.setEnabled(False)
        self._设置状态("已取消扫码登录", "#e67e22")

    def _Cookie登录(self):
        文本 = self.Cookie输入框.toPlainText().strip()
        if not 文本:
            QMessageBox.information(self, "提示", "请先粘贴 Cookie 文本")
            return
        self._设置状态("正在验证 Cookie 并登录…")
        self._跑("Cookie 登录", lambda 进度: self.适配器.Cookie登录(文本))

    def _发送验证码(self):
        手机号 = self.手机号框.text().strip()
        if len(手机号) < 6:
            QMessageBox.information(self, "提示", "请填写含国家码的手机号")
            return
        self.发送验证码按钮.setEnabled(False)
        self._设置状态("正在发送验证码…")

        def 动作(进度):
            结果 = self.适配器.短信发送(手机号)
            self._短信会话 = str((结果 or {}).get("会话") or "")
            return 结果

        self._跑("发送验证码", 动作,
               处理器=lambda r: self._验证码已发送(r))

    def _验证码已发送(self, 结果):
        结果 = dict(结果 or {})
        if str(结果.get("状态")) == "成功":
            self._短信倒计时 = 60
            self.短信提示标签.setText(str(结果.get("提示") or "验证码已发送"))
            self._设置状态("📨 " + str(结果.get("消息", "验证码已发送")), "#27ae60")
            self._日志("验证码已发送")
            self._滴答倒计时()
        else:
            self._设置状态(f"❌ {结果.get('消息', '发送失败')}", "#c0392b")
            self.发送验证码按钮.setEnabled(True)

    def _滴答倒计时(self):
        if self._短信倒计时 <= 0:
            self.发送验证码按钮.setEnabled(True)
            self.发送验证码按钮.setText("📨 发送验证码")
            return
        self.发送验证码按钮.setEnabled(False)
        self.发送验证码按钮.setText(f"⏳ {self._短信倒计时}s")
        self._短信倒计时 -= 1
        QTimer.singleShot(1000, self._滴答倒计时)

    def _发送失败(self, 错误):
        self.发送验证码按钮.setEnabled(True)
        self._设置状态(f"❌ 发送失败：{error短(错误)}", "#c0392b")

    def _短信登录(self):
        验证码 = self.验证码框.text().strip()
        if not 验证码:
            QMessageBox.information(self, "提示", "请填写短信验证码")
            return
        手机号 = self.手机号框.text().strip()
        会话 = self._短信会话
        self._设置状态("正在校验验证码…")
        self._跑("短信登录", lambda 进度: self.适配器.短信校验(
            会话, 验证码, 手机号))

    def _令牌登录(self):
        访问 = self.访问令牌框.text().strip()
        if not 访问:
            QMessageBox.information(self, "提示", "access_token 不能为空")
            return
        刷新 = self.刷新令牌框.text().strip()
        self._设置状态("正在校验令牌…")
        self._跑("令牌登录", lambda 进度: self.适配器.令牌登录(访问, 刷新))

    # ==================== 关闭 ====================

    def closeEvent(self, 事件):
        self._停止核对()
        try:
            if self._扫码会话:
                self.适配器.扫码取消(self._扫码会话)
        except Exception:
            pass
        for 线程 in list(self._线程):
            try:
                if 线程.isRunning():
                    线程.wait(1500)
            except Exception:
                pass
        # ⚠️ 现场崩溃（QThread: Destroyed while thread '' is still running →
        #    Fatal Python error: Aborted）就是漏了这一组：
        #    「扫码期间每 6 秒核对一次账号状态」造出来的线程只存在 _核对线程们 里，
        #    关对话框时没人等它们；线程还卡在适配器调用里，Qt 就把它的 C++ 对象
        #    连同父对象一起删了 → Qt 直接 abort（不是 Python 异常，抓不住）。
        self._等核对线程(4000)
        super().closeEvent(事件)

    def _等核对线程(self, 毫秒总预算: int = 4000) -> None:
        """等"账号状态核对"线程收工；超预算就再给一次机会，但不留着跑。"""
        import time as _time
        截止 = _time.time() + max(0.5, 毫秒总预算 / 1000.0)
        for 线程 in list(getattr(self, "_核对线程们", []) or []):
            try:
                if 线程.isRunning():
                    剩余 = max(0.2, 截止 - _time.time())
                    线程.wait(int(剩余 * 1000))
            except Exception:
                pass
        # 停掉计时器并清空列表：下次打开对话框不会拿到已经失效的对象
        try:
            self._停止核对()
        except Exception:
            pass
        try:
            self._核对线程们 = []
        except Exception:
            pass


def error短(文本, 长度: int = 200) -> str:
    return str(文本 or "")[:长度]


def _像超时(结果: dict) -> bool:
    """把"等待超时"类失败识别出来。

    协议里超时可以是 ``状态="超时"``，也可以是 ``状态="失败"`` + 消息说明
    （百度后端就是后者：`扫码登录超时：30 秒内没有在手机上确认`）。
    识别出来的话界面按"可重试"处理，不用红色报错吓人——会话通常还活着。
    """
    if not isinstance(结果, dict):
        return False
    if 结果.get("超时"):
        return True
    文本 = f"{结果.get('消息', '')} {结果.get('提示', '')}".lower()
    return any(词 in 文本 for 词 in
              ("超时", "timeout", "过期", "没有在手机上确认",
               "未在", "已失效", "expired"))
