"""网盘路径选择对话框（V8_3 版：直接用适配器实例列目录，不经过 Alist）。

界面沿用 网盘管理_V8 的 路径选择对话框 的交互：
路径输入框 + 进入/上一级 + 目录列表双击进入 + 可选「新建文件夹」。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMessageBox, QPushButton, QVBoxLayout,
)

from ..核心.模型 import 规范路径, 拼路径
from .后台线程 import 列目录线程


class 路径选择对话框(QDialog):
    """在某个网盘实例里挑一个路径。"""

    def __init__(self, 适配器, 标识: str = "", 初始路径: str = "/",
                 允许选择文件: bool = True, 允许新建: bool = False,
                 标题: str = "选择网盘路径", 说明: str = "", 父窗口=None,
                 只要文件: bool = False, 名字过滤=None, 过滤提示: str = "",
                 网盘列表=None, 取适配器=None):
        """
        :param 只要文件: True = **必须选中一个文件**才能确定（播放视频用；
            传输页选目录时保持 False）。选中目录时会被明确挡下来，而不是
            把一个目录路径交回调用方 —— 实测那样会让播放器去播目录并报错。
        :param 名字过滤: ``callable(名字) -> bool``，返回假的名字会**灰掉不可选**，
            并在状态栏给出 :param 过滤提示:（例如"不是视频文件"）。
        :param 网盘列表: ``{标识: 规格}``（一般是 ``动作.规格表``）。给了就在顶部
            显示一个**网盘下拉框**，可以在对话框里直接换网盘 —— 用户反馈"打开网盘后
            不能切换网盘"，就是这个缺口。
        :param 取适配器: ``callable(标识) -> 适配器``，换网盘时用它拿新适配器。
        """
        super().__init__(父窗口)
        self.适配器 = 适配器
        self.标识 = 标识
        self.允许选择文件 = 允许选择文件
        self.允许新建 = 允许新建
        self.只要文件 = bool(只要文件)
        self.名字过滤 = 名字过滤
        self.过滤提示 = str(过滤提示 or "不符合要求的文件")
        self.选中是文件 = False
        #: 路径框是不是**用户手输**的（选中条目会自动改路径框，必须区分开，
        #: 否则"只选目录 + 只要文件"会被误当成手输文件路径而放行）
        self._手输过 = False
        self._退过一次 = False
        #: 对话框是不是已经关了 —— 后台列目录线程可能还在跑，回调必须忽略，
        #: 否则信号打进已销毁的对话框会**直接段错误**（实测崩溃）
        self._关闭 = False
        self.当前路径 = 规范路径(初始路径)
        self.选中路径 = ""
        self.选中条目 = None
        self._线程: list = []
        self.网盘列表 = dict(网盘列表 or {})
        self._取适配器 = 取适配器
        self.选中网盘标识 = 标识

        self.setWindowTitle(标题)
        self.resize(760, 520)
        布局 = QVBoxLayout(self)

        if self.网盘列表 and callable(self._取适配器):
            网盘行 = QHBoxLayout()
            网盘行.addWidget(QLabel("网盘："))
            self.网盘框 = QComboBox()
            self.网盘框.setMinimumWidth(200)
            for 网标识, 规格 in self.网盘列表.items():
                名称 = getattr(规格, "显示名", None) or str(规格 or 网标识)
                图标 = getattr(规格, "图标", "") or ""
                self.网盘框.addItem(f"{图标} {名称}".strip(), 网标识)
            idx = self.网盘框.findData(self.标识)
            self.网盘框.setCurrentIndex(idx if idx >= 0 else 0)
            self.选中网盘标识 = str(self.网盘框.currentData() or self.标识)
            self.网盘框.currentIndexChanged.connect(self._换网盘)
            网盘行.addWidget(self.网盘框, 1)
            布局.addLayout(网盘行)

        if 说明:
            提示 = QLabel(说明)
            提示.setWordWrap(True)
            布局.addWidget(提示)

        顶部 = QHBoxLayout()
        顶部.addWidget(QLabel("路径："))
        self.路径框 = QLineEdit(self.当前路径)
        self.路径框.returnPressed.connect(self._进入输入路径)
        # textEdited 只在**用户输入**时触发（setText 不会触发），正好用来区分
        self.路径框.textEdited.connect(self._路径框被手输)
        顶部.addWidget(self.路径框, 1)
        进入按钮 = QPushButton("进入")
        进入按钮.clicked.connect(self._进入输入路径)
        顶部.addWidget(进入按钮)
        self.上级按钮 = QPushButton("⬆ 上一级")
        self.上级按钮.clicked.connect(self._上一级)
        顶部.addWidget(self.上级按钮)
        布局.addLayout(顶部)

        self.列表 = QListWidget()
        self.列表.itemDoubleClicked.connect(self._双击进入)
        self.列表.currentItemChanged.connect(self._选中变化)
        布局.addWidget(self.列表, 1)

        底部 = QHBoxLayout()
        self.状态标签 = QLabel("加载中…")
        底部.addWidget(self.状态标签, 1)
        self.确定按钮 = QPushButton("选择此目录" if 允许新建 else "选择")
        self.确定按钮.clicked.connect(self._确定选择)
        底部.addWidget(self.确定按钮)
        if 允许新建:
            新建按钮 = QPushButton("📁 新建文件夹")
            新建按钮.clicked.connect(self._新建文件夹)
            底部.addWidget(新建按钮)
        取消按钮 = QPushButton("取消")
        取消按钮.clicked.connect(self.reject)
        底部.addWidget(取消按钮)
        布局.addLayout(底部)

        self._刷新()

    # ---------------- 列表 ----------------

    def _路径框被手输(self, _文本: str):
        self._手输过 = True

    def _断开后台(self, 等待毫秒: int = 800) -> None:
        """关闭时收尾后台列目录线程：**先断信号，再等它跑完**。

        为什么必须等：线程是挂在对话框上的（父对象），对话框一销毁、线程还在跑，
        回调就会打进已销毁的控件 —— 实测**直接段错误**（点取消/换网盘时都可能触发）。
        断开信号只解决"回调"，等待才解决"线程还活着"。
        """
        for 线程 in list(self._线程):
            for 信号名 in ("成功", "失败", "finished"):
                try:
                    getattr(线程, 信号名).disconnect()
                except Exception:  # noqa: BLE001
                    pass
        for 线程 in list(self._线程):
            try:
                if 线程.isRunning():
                    线程.wait(int(等待毫秒))
            except Exception:  # noqa: BLE001
                pass
        self._线程.clear()

    def done(self, 结果: int) -> None:  # noqa: N802
        self._关闭 = True
        self._断开后台()
        super().done(结果)

    def closeEvent(self, 事件):  # noqa: N802
        self._关闭 = True
        self._断开后台()
        super().closeEvent(事件)

    def _刷新(self):
        if self._关闭:
            return
        self._手输过 = False
        self.路径框.setText(self.当前路径)
        self.列表.clear()
        self.状态标签.setText(f"正在读取 {self.当前路径} …")
        self.确定按钮.setEnabled(False)
        线程 = 列目录线程(self.标识, self.适配器, self.当前路径, self)
        线程.成功.connect(self._加载成功)
        线程.失败.connect(self._加载失败)
        self._线程.append(线程)
        线程.finished.connect(lambda t=线程: self._清理(t))
        线程.start()

    def _清理(self, 线程):
        try:
            self._线程.remove(线程)
        except ValueError:
            pass
        线程.deleteLater()

    def _加载成功(self, 标识, 路径, 条目):
        if self._关闭 or 路径 != self.当前路径:
            return
        self.列表.clear()
        可用数 = 0
        for 项 in sorted(条目, key=lambda x: (not x.is_dir, x.name.lower())):
            if not 项.is_dir and not self.允许选择文件:
                continue
            item = QListWidgetItem(("📁 " if 项.is_dir else "📄 ") + 项.name)
            可选 = True
            if (not 项.is_dir) and callable(self.名字过滤):
                可选 = bool(self.名字过滤(项.name))
            if not 可选:
                # 灰掉 + 不可选：看得见但选不了（比"藏起来"更不容易让人困惑）
                item.setFlags(item.flags() & ~Qt.ItemIsSelectable
                              & ~Qt.ItemIsEnabled)
                item.setToolTip(self.过滤提示)
                item.setForeground(Qt.gray)
            elif not 项.is_dir:
                可用数 += 1
            item.setData(Qt.UserRole, {
                "name": 项.name, "path": 项.path,
                "is_dir": bool(项.is_dir), "size": int(项.size or 0),
                "可选": 可选,
            })
            self.列表.addItem(item)
        提示 = f"共 {self.列表.count()} 项"
        if callable(self.名字过滤) and 可用数 == 0:
            提示 += f"（本目录没有可选的：{self.过滤提示}）"
        self.状态标签.setText(提示)
        # 只要文件时：必须真的选中一个文件，才允许确定
        self.确定按钮.setEnabled(not self.只要文件)
        self.上级按钮.setEnabled(self.当前路径 != "/")

    def _加载失败(self, 标识, 路径, 错误):
        if self._关闭 or 路径 != self.当前路径:
            return
        # 传进来的"初始路径"很可能是个**文件**（例如播放器把正在播的视频路径填了进来）
        # —— 那就自动退到它的父目录重新列，而不是弹一个"不是目录"的错。
        文本 = str(错误 or "")
        像文件 = ("不是目录" in 文本 or "NotADirectory" in 文本
               or "not a directory" in 文本.lower())
        if 像文件 and self.当前路径 not in ("", "/") and not getattr(self, "_退过一次", False):
            self._退过一次 = True
            上级 = 规范路径(self.当前路径.rsplit("/", 1)[0] or "/")
            self.状态标签.setText(f"这是一条文件路径，改到它的目录：{上级}")
            self.当前路径 = 上级
            self._刷新()
            return
        self.状态标签.setText(f"读取失败：{文本[:120]}（可点「⬆ 上一级」返回）")
        self.确定按钮.setEnabled(True)
        # 不再弹模态框：模态框会挡住操作、也会把自动化/自检挂死（本项目踩过）

    def _选中变化(self, 当前, 之前):
        if 当前 is None:
            self.选中是文件 = False
            if self.只要文件:
                self.确定按钮.setEnabled(False)
            return
        数据 = 当前.data(Qt.UserRole) or {}
        if 数据.get("is_dir"):
            self.选中是文件 = False
            self._手输过 = False          # 是选中项自动填的，不算手输
            self.路径框.setText(数据.get("path") or self.当前路径)
            if self.只要文件:
                self.确定按钮.setEnabled(False)
                self.状态标签.setText(
                    f"📁 {数据.get('name')} 是目录：双击进入，再选里面的文件")
        else:
            self.选中是文件 = True
            大小 = self._格式化大小(数据.get("size") or 0)
            self.状态标签.setText(f"文件：{数据.get('name')}（{大小}）")
            if self.只要文件:
                self.确定按钮.setEnabled(bool(数据.get("可选", True)))

    def _双击进入(self, item):
        数据 = item.data(Qt.UserRole) or {}
        if 数据.get("is_dir"):
            self.当前路径 = 规范路径(数据.get("path"))
            self._刷新()
            return
        if self.只要文件 and 数据.get("可选", True) and 数据.get("path"):
            # 双击文件 = 直接选中并确定（跟资源管理器/VLC 的习惯一致）
            self.选中条目 = 数据
            self.选中路径 = str(数据.get("path") or "")
            self.accept()

    def _换网盘(self, _索引: int = 0):
        """对话框里切换网盘：换适配器、回到根目录、重新列目录。"""
        新标识 = str(self.网盘框.currentData() or "")
        if not 新标识 or 新标识 == self.标识:
            return
        try:
            新适配器 = self._取适配器(新标识)
        except Exception as e:  # noqa: BLE001
            self.状态标签.setText(f"切换网盘失败：{e}")
            return
        self.标识 = 新标识
        self.选中网盘标识 = 新标识
        self.适配器 = 新适配器
        self.当前路径 = "/"
        self._退过一次 = False
        self.选中路径 = ""
        self.选中条目 = None
        if hasattr(self, "路径框"):
            self.路径框.setText("/")
        self.状态标签.setText(f"已切换到 {self.网盘框.currentText()}，正在读取 / …")
        self._刷新()

    def _进入输入路径(self):
        self.当前路径 = 规范路径(self.路径框.text())
        self._刷新()

    def _上一级(self):
        if self.当前路径 == "/":
            return
        self.当前路径 = 规范路径(self.当前路径.rsplit("/", 1)[0] or "/")
        self._刷新()

    def _确定选择(self):
        """确定：传输页语义选"目录"，播放器语义（只要文件）选"文件"。

        ⚠️ 这里踩过坑：以前要求"选中项路径 == 路径框文本"才算选中文件，可是
        路径框显示的是**当前目录**，于是"选中文件 → 点确定"永远不生效（只闪一行
        小提示），用户的感觉就是"选不中视频文件"。
        """
        当前项 = self.列表.currentItem()
        数据 = (当前项.data(Qt.UserRole) or {}) if 当前项 is not None else {}
        文本 = 规范路径(self.路径框.text())
        选中的是文件 = bool(当前项 is not None
                      and not 数据.get("is_dir")
                      and 数据.get("path")
                      and 数据.get("可选", True))
        if 选中的是文件:
            self.选中条目 = 数据
            self.选中路径 = str(数据.get("path"))
            self.accept()
            return
        if self.只要文件:
            # 也允许"手输文件路径"：路径框里不是当前目录、且名字看着是目标类型
            手输 = (self._手输过 and bool(文本)
                  and 文本 != 规范路径(self.当前路径))
            if 手输 and (not callable(self.名字过滤)
                       or self.名字过滤(Path(文本).name)):
                self.选中条目 = None
                self.选中路径 = 文本
                self.accept()
                return
            # 其余情况**绝不**把目录路径交回去（否则调用方会去播目录）
            if 当前项 is not None and 数据.get("is_dir"):
                self.状态标签.setText(
                    f"📁 {数据.get('name')} 是目录：请双击进入后选中具体文件")
            else:
                self.状态标签.setText("请先选中一个文件（灰色的是不可选类型）")
            return
        self.选中条目 = None
        self.选中路径 = 文本
        self.accept()

    def _新建文件夹(self):
        名称, ok = QInputDialog.getText(self, "新建文件夹", "文件夹名：")
        if not ok or not 名称.strip():
            return
        新路径 = 拼路径(self.当前路径, 名称.strip())
        try:
            self.适配器.确保目录(新路径)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "新建失败", str(e))
            return
        self.当前路径 = 新路径      # 建好就进去，便于继续操作
        self._刷新()

    @staticmethod
    def _格式化大小(n) -> str:
        try:
            n = int(n)
        except (TypeError, ValueError):
            return "0 B"
        if n < 1024:
            return f"{n} B"
        n /= 1024
        if n < 1024:
            return f"{n:.1f} KB"
        n /= 1024
        if n < 1024:
            return f"{n:.1f} MB"
        return f"{n / 1024:.2f} GB"
