# 光鸭云盘适配器/界面/页面/文件浏览页.py
"""文件浏览页（含上传/下载/删除/新建文件夹/回收站）"""

import os
import time
import logging

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QGroupBox, QMessageBox, QTableWidget, QTableWidgetItem,
    QHeaderView, QFileDialog, QProgressDialog, QInputDialog,
    QLineEdit,
)
from PySide6.QtCore import Qt, Signal

from 核心.接口.文件接口 import 文件接口
from 核心.接口.任务接口 import 任务接口

from ..格式化工具 import (
    格式化大小, 格式化时间戳, 格式化时长,
    默认新文件夹名, 非法字符集,
)
from ..字段映射 import 资源类型映射, 操作类型映射
from ..公共线程 import 批量上传线程
from .基类 import 页面基类

logger = logging.getLogger("光鸭云盘.界面.文件浏览")


class 文件浏览页(页面基类):
    """文件浏览页"""

    分享文件请求 = Signal(list)   # 参数：[{"文件ID","名称","是目录"}]

    def __init__(self, 主窗口, parent=None):
        super().__init__(主窗口, parent)

        self.当前父ID = ""
        self.当前路径 = "/"
        self.当前视图 = "文件"

        self.上传线程实例 = None
        self.上传进度弹窗: QProgressDialog | None = None

        self._构建界面()

    # ---------------- UI ----------------

    def _构建界面(self):
        布局 = QVBoxLayout(self)

        路径栏 = QHBoxLayout()
        self.根目录按钮 = QPushButton("🏠 根目录")
        self.根目录按钮.clicked.connect(lambda: self.加载目录("", "/"))
        路径栏.addWidget(self.根目录按钮)

        self.上级按钮 = QPushButton("⬆ 上级")
        self.上级按钮.clicked.connect(self.返回上级)
        路径栏.addWidget(self.上级按钮)

        self.路径标签 = QLabel("/")
        self.路径标签.setStyleSheet(
            "background: #f5f5f5; padding: 6px; "
            "border: 1px solid #ddd; border-radius: 4px;")
        路径栏.addWidget(self.路径标签, 1)

        self.刷新按钮 = QPushButton("🔄 刷新")
        self.刷新按钮.setToolTip("刷新当前目录/回收站")
        self.刷新按钮.clicked.connect(self.刷新当前视图)
        路径栏.addWidget(self.刷新按钮)
        布局.addLayout(路径栏)

        操作组 = QGroupBox("⚡ 操作")
        操作布局 = QHBoxLayout()

        self.新建文件夹按钮 = QPushButton("📁+ 新建文件夹")
        self.新建文件夹按钮.setStyleSheet(
            "background: #FFC107; color: #333; padding: 6px 14px; "
            "border-radius: 4px; font-weight: bold;")
        self.新建文件夹按钮.setToolTip("在当前目录下创建一个新的文件夹")
        self.新建文件夹按钮.clicked.connect(self.新建文件夹)
        操作布局.addWidget(self.新建文件夹按钮)

        self.上传文件按钮 = QPushButton("📤 上传文件")
        self.上传文件按钮.setStyleSheet(
            "background: #4CAF50; color: white; padding: 6px 14px; "
            "border-radius: 4px; font-weight: bold;")
        self.上传文件按钮.clicked.connect(self.选择并上传文件)
        操作布局.addWidget(self.上传文件按钮)

        self.上传文件夹按钮 = QPushButton("📁 上传文件夹")
        self.上传文件夹按钮.setStyleSheet(
            "background: #388E3C; color: white; padding: 6px 14px; "
            "border-radius: 4px; font-weight: bold;")
        self.上传文件夹按钮.clicked.connect(self.选择并上传文件夹)
        操作布局.addWidget(self.上传文件夹按钮)

        self.下载文件按钮 = QPushButton("📥 下载选中")
        self.下载文件按钮.setStyleSheet(
            "background: #2196F3; color: white; padding: 6px 14px; "
            "border-radius: 4px; font-weight: bold;")
        self.下载文件按钮.clicked.connect(self.下载选中文件)
        操作布局.addWidget(self.下载文件按钮)

        self.分享选中按钮 = QPushButton("🔗 分享选中")
        self.分享选中按钮.setStyleSheet(
            "background: #9C27B0; color: white; padding: 6px 14px; "
            "border-radius: 4px; font-weight: bold;")
        self.分享选中按钮.setToolTip("把选中的文件/目录创建为分享链接")
        self.分享选中按钮.clicked.connect(self.分享选中项)
        操作布局.addWidget(self.分享选中按钮)

        self.删除文件按钮 = QPushButton("🗑 删除选中")
        self.删除文件按钮.setStyleSheet(
            "background: #F44336; color: white; padding: 6px 14px; "
            "border-radius: 4px; font-weight: bold;")
        self.删除文件按钮.clicked.connect(self.删除选中项)
        操作布局.addWidget(self.删除文件按钮)

        操作布局.addStretch()

        self.查看回收站按钮 = QPushButton("♻️ 回收站")
        self.查看回收站按钮.clicked.connect(self.查看回收站)
        操作布局.addWidget(self.查看回收站按钮)

        self.清空回收站按钮 = QPushButton("🔥 清空回收站")
        self.清空回收站按钮.setStyleSheet(
            "background: #F44336; color: white; padding: 6px 14px; "
            "border-radius: 4px; font-weight: bold;")
        self.清空回收站按钮.clicked.connect(self.清空回收站)
        操作布局.addWidget(self.清空回收站按钮)

        self.状态提示 = QLabel("就绪")
        self.状态提示.setStyleSheet("color: #666; padding-left: 10px;")
        操作布局.addWidget(self.状态提示)

        操作组.setLayout(操作布局)
        布局.addWidget(操作组)

        self.文件表格 = QTableWidget()
        self.文件表格.setColumnCount(4)
        self.文件表格.setHorizontalHeaderLabels(
            ["类型", "名称", "大小", "修改时间"])
        self.文件表格.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.Stretch)
        self.文件表格.setColumnWidth(0, 70)
        self.文件表格.setColumnWidth(2, 120)
        self.文件表格.setColumnWidth(3, 180)
        self.文件表格.setAlternatingRowColors(True)
        self.文件表格.setSelectionBehavior(QTableWidget.SelectRows)
        self.文件表格.setSelectionMode(QTableWidget.ExtendedSelection)
        self.文件表格.setEditTriggers(QTableWidget.NoEditTriggers)
        self.文件表格.doubleClicked.connect(self.双击文件行)
        布局.addWidget(self.文件表格, 1)

    # ---------------- 登录态 ----------------

    def 设置启用(self, 已登录: bool):
        for 按钮 in (
            self.新建文件夹按钮, self.上传文件按钮,
            self.上传文件夹按钮, self.下载文件按钮,
            self.分享选中按钮, self.删除文件按钮,
            self.查看回收站按钮, self.清空回收站按钮,
            self.刷新按钮,
        ):
            按钮.setEnabled(已登录)

    def 重置(self):
        self.文件表格.setRowCount(0)
        self.当前父ID = ""
        self.当前路径 = "/"
        self.当前视图 = "文件"
        self.路径标签.setText("/")
        self.状态提示.setText("就绪")

    # ---------------- 目录浏览 ----------------

    def 加载目录(self, 父ID: str, 路径: str = "/"):
        if not self.网络:
            QMessageBox.warning(self, "提示", "请先登录")
            return
        self.当前父ID = 父ID
        self.当前路径 = 路径
        self.当前视图 = "文件"
        self.路径标签.setText(路径)
        self.状态提示.setText("加载中...")
        self.状态提示.setStyleSheet("color: #2196F3; padding-left: 10px;")

        def 任务():
            文件 = 文件接口(self.网络)
            return 文件.取文件列表(parentId=父ID)

        self.启动网络任务("列目录", 任务, self.列目录成功, self.列目录失败)

    def 列目录成功(self, 任务名: str, 数据: dict):
        self.文件表格.setRowCount(0)
        条目列表 = 数据.get("list", []) if isinstance(数据, dict) else []
        if 条目列表 is None:
            条目列表 = []

        文件接口实例 = 文件接口(self.网络)

        for 条目 in 条目列表:
            行 = self.文件表格.rowCount()
            self.文件表格.insertRow(行)

            是目录 = 条目.get("resType") == 2
            类型 = "📁 目录" if 是目录 else "📄 文件"
            名称 = 条目.get("fileName", "")
            文件ID = str(条目.get("fileId", ""))
            大小 = 条目.get("fileSize", 0) or 0
            修改时间原始 = (条目.get("updateTime")
                          or 条目.get("utime")
                          or 条目.get("modifyTime"))
            if 修改时间原始 and isinstance(修改时间原始, (int, float)):
                修改时间 = 格式化时间戳(修改时间原始)
            else:
                修改时间 = str(修改时间原始 or "—")

            类型项 = QTableWidgetItem(类型)
            类型项.setData(Qt.UserRole, {
                "是目录": 是目录, "名称": 名称,
                "文件ID": 文件ID,
            })
            缩略图 = 文件接口实例.取缩略图URL(条目)
            类型项.setData(Qt.UserRole + 1, 缩略图)
            self.文件表格.setItem(行, 0, 类型项)

            名称项 = QTableWidgetItem(名称)
            名称项.setToolTip(名称)
            self.文件表格.setItem(行, 1, 名称项)

            self.文件表格.setItem(
                行, 2,
                QTableWidgetItem("—" if 是目录 else 格式化大小(大小)))
            self.文件表格.setItem(行, 3, QTableWidgetItem(修改时间))

        self.状态提示.setText(f"共 {len(条目列表)} 项")
        self.状态提示.setStyleSheet("color: #4CAF50; padding-left: 10px;")
        logger.info(f"列目录成功，共 {len(条目列表)} 项")

    def 列目录失败(self, 任务名: str, 错误: str):
        logger.error(f"列目录失败：{错误}")
        self.状态提示.setText(f"列目录失败：{错误}")
        self.状态提示.setStyleSheet("color: #F44336; padding-left: 10px;")

    def 刷新当前视图(self):
        if not self.网络:
            QMessageBox.warning(self, "提示", "请先登录")
            return
        if self.当前视图 == "回收站":
            self.查看回收站()
        else:
            self.加载目录(self.当前父ID, self.当前路径)

    def 双击文件行(self, 索引):
        行 = 索引.row()
        项 = self.文件表格.item(行, 0)
        if 项 is None:
            return
        数据 = 项.data(Qt.UserRole)
        if 数据 and 数据.get("是目录") and self.当前视图 == "文件":
            新路径 = (self.当前路径.rstrip("/") + "/" + 数据["名称"])
            self.加载目录(数据["文件ID"], 新路径)

    def 返回上级(self):
        if self.当前路径 == "/" or self.当前视图 == "回收站":
            return
        段 = self.当前路径.rstrip("/").split("/")[:-1]
        新路径 = "/".join(段) or "/"
        self._导航到路径(新路径)

    def _导航到路径(self, 目标路径: str):
        目标路径 = ("/" + 目标路径.strip("/")
                  if 目标路径 != "/" else "/")
        段列表 = [段 for 段 in 目标路径.split("/") if 段]

        def 任务():
            文件 = 文件接口(self.网络)
            父ID = ""
            for 段 in 段列表:
                数据 = 文件.取文件列表(parentId=父ID)
                找到 = None
                for 条目 in 数据.get("list", []) or []:
                    if (条目.get("fileName") == 段
                            and 条目.get("resType") == 2):
                        找到 = str(条目.get("fileId"))
                        break
                if 找到 is None:
                    raise RuntimeError(f"目录不存在：{段}")
                父ID = 找到
            return 父ID

        def 成功(任务名, 父ID):
            self.加载目录(父ID, 目标路径)

        def 失败(任务名, 错误):
            logger.error(f"导航失败：{错误}")

        self.启动网络任务("导航", 任务, 成功, 失败)

    # ---------------- 新建文件夹 ----------------

    def 新建文件夹(self):
        if not self.网络:
            QMessageBox.warning(self, "提示", "请先登录")
            return
        if self.当前视图 != "文件":
            QMessageBox.warning(
                self, "提示", "请先进入普通目录再新建文件夹")
            return

        名字, 确认 = QInputDialog.getText(
            self, "新建文件夹", "请输入文件夹名称：",
            QLineEdit.Normal, 默认新文件夹名(),
        )
        if not 确认:
            return

        名字 = (名字 or "").strip()
        if not 名字:
            QMessageBox.warning(self, "提示", "文件夹名不能为空")
            return

        非法 = [ch for ch in 名字 if ch in 非法字符集]
        if 非法:
            QMessageBox.warning(
                self, "提示",
                f"文件夹名不能包含以下字符：\\ / : * ? \" < > |\n"
                f"检测到：{''.join(非法)}")
            return
        if len(名字) > 200:
            QMessageBox.warning(self, "提示", "文件夹名过长（>200 字符）")
            return

        父ID = self.当前父ID
        self.新建文件夹按钮.setEnabled(False)
        self.状态提示.setText("正在创建文件夹...")
        self.状态提示.setStyleSheet(
            "color: #2196F3; padding-left: 10px;")
        logger.info(f"[界面] 新建文件夹：parentId={父ID} dirName={名字}")

        def 任务():
            文件 = 文件接口(self.网络)
            return 文件.创建文件夹(父ID, 名字)

        def 成功(任务名, 结果):
            self.新建文件夹按钮.setEnabled(True)
            fileId = 结果.get("fileId") if isinstance(结果, dict) else None
            logger.info(f"[界面] ✅ 新建文件夹 '{名字}' fileId={fileId}")
            self.状态提示.setText(f"✅ 已创建文件夹：{名字}")
            self.状态提示.setStyleSheet(
                "color: #4CAF50; padding-left: 10px;")
            self.加载目录(self.当前父ID, self.当前路径)

        def 失败(任务名, 错误):
            self.新建文件夹按钮.setEnabled(True)
            logger.error(f"[界面] ❌ 新建文件夹失败：{错误}")
            self.状态提示.setText(f"❌ 新建失败：{错误}")
            self.状态提示.setStyleSheet(
                "color: #F44336; padding-left: 10px;")
            QMessageBox.warning(self, "新建失败", 错误)

        self.启动网络任务("新建文件夹", 任务, 成功, 失败)

    # ---------------- 上传 ----------------

    def 选择并上传文件(self):
        if not self.网络:
            QMessageBox.warning(self, "提示", "请先登录")
            return
        if self.当前视图 != "文件":
            QMessageBox.warning(self, "提示", "请先进入普通目录再上传")
            return

        路径列表, _ = QFileDialog.getOpenFileNames(
            self, "选择要上传的文件")
        if not 路径列表:
            return
        self._启动批量上传(路径列表, 是文件夹=False)

    def 选择并上传文件夹(self):
        if not self.网络:
            QMessageBox.warning(self, "提示", "请先登录")
            return
        if self.当前视图 != "文件":
            QMessageBox.warning(self, "提示", "请先进入普通目录再上传")
            return

        目录 = QFileDialog.getExistingDirectory(
            self, "选择要上传的文件夹")
        if not 目录:
            return
        self._启动批量上传([目录], 是文件夹=True)

    def _启动批量上传(self, 路径列表: list, 是文件夹: bool):
        总数 = len(路径列表)
        if 总数 == 0:
            return

        self.上传进度弹窗 = QProgressDialog(
            f"准备上传 {总数} 个{'文件夹' if 是文件夹 else '文件'}...",
            "取消", 0, 100, self,
        )
        self.上传进度弹窗.setWindowTitle("上传")
        self.上传进度弹窗.setModal(True)
        self.上传进度弹窗.setMinimumDuration(0)
        self.上传进度弹窗.setValue(0)

        线程 = 批量上传线程(
            self.网络, 路径列表, self.当前父ID, 是文件夹)
        线程.总进度.connect(self._上传进度)
        线程.单文件成功.connect(self._单文件上传成功)
        线程.全部成功.connect(self._全部上传成功)
        线程.失败.connect(self._上传失败)
        线程.finished.connect(self.主窗口._线程完成清理)
        self.主窗口.活动线程.append(线程)
        self.上传线程实例 = 线程
        线程.start()

    def _上传进度(self, 阶段: str, 进度: float):
        if self.上传进度弹窗 is not None:
            百分比 = max(0, min(100, int(进度 * 100)))
            self.上传进度弹窗.setLabelText(f"{阶段}  {百分比}%")
            self.上传进度弹窗.setValue(百分比)

    def _单文件上传成功(self, 文件名: str, 信息: dict):
        fileId = 信息.get("fileId") if isinstance(信息, dict) else None
        logger.info(f"[界面] ✅ {文件名} 上传成功 fileId={fileId}")

    def _全部上传成功(self, 成功数: int, 总数: int):
        if self.上传进度弹窗 is not None:
            self.上传进度弹窗.setValue(100)
            self.上传进度弹窗.close()
            self.上传进度弹窗 = None

        self.状态提示.setText(f"✅ 上传完成：{成功数}/{总数}")
        self.状态提示.setStyleSheet("color: #4CAF50; padding-left: 10px;")
        logger.info(f"[界面] ✅ 上传完成：{成功数}/{总数}")

        self.加载目录(self.当前父ID, self.当前路径)
        self.主窗口.上传记录页.加载上传记录(仅刷新=True)
        QMessageBox.information(
            self, "上传完成", f"成功 {成功数} / 共 {总数}")

    def _上传失败(self, 错误: str):
        if self.上传进度弹窗 is not None:
            self.上传进度弹窗.close()
            self.上传进度弹窗 = None

        logger.error(f"[界面] ❌ 上传失败：{错误}")
        self.状态提示.setText(f"❌ 上传失败：{错误}")
        self.状态提示.setStyleSheet("color: #F44336; padding-left: 10px;")
        QMessageBox.warning(self, "上传失败", 错误)

    # ---------------- 下载 ----------------

    def 下载选中文件(self):
        if not self.网络:
            QMessageBox.warning(self, "提示", "请先登录")
            return
        选中 = self._获取选中项()
        文件项 = [项 for 项 in 选中 if not 项.get("是目录")]
        if not 文件项:
            QMessageBox.warning(self, "提示", "请先选中至少一个文件")
            return

        保存目录 = QFileDialog.getExistingDirectory(
            self, "选择保存目录")
        if not 保存目录:
            return

        for 项 in 文件项:
            本地路径 = os.path.join(保存目录, 项["名称"])
            self._下载一个文件(项["文件ID"], 本地路径)

    def _下载一个文件(self, 文件ID: str, 本地路径: str):
        logger.info(f"[界面] 下载 fileId={文件ID} → {本地路径}")

        def 任务():
            文件 = 文件接口(self.网络)
            下载信息 = 文件.取下载地址(文件ID)
            signedURL = 下载信息.get("signedURL")
            if not signedURL:
                raise RuntimeError(
                    f"未获取到 signedURL：{list(下载信息.keys())}")

            import httpx
            with httpx.Client(
                timeout=httpx.Timeout(
                    connect=30.0, read=3600.0,
                    write=3600.0, pool=30.0),
                http2=False,
                follow_redirects=True,
            ) as 客户端:
                with 客户端.stream("GET", signedURL) as 响应:
                    响应.raise_for_status()
                    with open(本地路径, "wb") as f:
                        for 数据块 in 响应.iter_bytes(
                                chunk_size=64 * 1024):
                            f.write(数据块)
            return 本地路径

        def 成功(任务名, 结果):
            try:
                大小 = os.path.getsize(结果)
            except Exception:
                大小 = 0
            logger.info(f"[界面] ✅ 下载完成：{结果} ({大小}B)")
            self.状态提示.setText(
                f"✅ 下载完成：{os.path.basename(结果)} "
                f"({格式化大小(大小)})")
            self.状态提示.setStyleSheet(
                "color: #4CAF50; padding-left: 10px;")

        def 失败(任务名, 错误):
            logger.error(f"[界面] ❌ 下载失败：{错误}")
            self.状态提示.setText(f"❌ 下载失败：{错误}")
            self.状态提示.setStyleSheet(
                "color: #F44336; padding-left: 10px;")
            QMessageBox.warning(self, "下载失败", 错误)

        self.启动网络任务("下载", 任务, 成功, 失败)

    # ---------------- 删除 ----------------

    def 删除选中项(self):
        if not self.网络:
            QMessageBox.warning(self, "提示", "请先登录")
            return
        选中 = self._获取选中项()
        if not 选中:
            QMessageBox.warning(self, "提示", "请先选中至少一项")
            return

        应答 = QMessageBox.question(
            self, "确认删除",
            f"确定要删除选中的 {len(选中)} 项吗？\n"
            "（文件会移入回收站，可恢复）")
        if 应答 != QMessageBox.Yes:
            return

        fileIds = [项["文件ID"] for 项 in 选中]
        logger.info(f"[界面] 删除 {fileIds}")

        def 任务():
            文件 = 文件接口(self.网络)
            return 文件.删除文件(fileIds)

        def 成功(任务名, 结果):
            logger.info("[界面] ✅ 删除成功")
            self.状态提示.setText(f"✅ 已删除 {len(fileIds)} 项")
            self.状态提示.setStyleSheet(
                "color: #4CAF50; padding-left: 10px;")
            self.刷新当前视图()

        def 失败(任务名, 错误):
            logger.error(f"[界面] ❌ 删除失败：{错误}")
            QMessageBox.warning(self, "删除失败", 错误)

        self.启动网络任务("删除", 任务, 成功, 失败)

    def _获取选中项(self) -> list:
        选中行 = self.文件表格.selectionModel().selectedRows()
        结果 = []
        for 索引 in 选中行:
            项 = self.文件表格.item(索引.row(), 0)
            if 项 is None:
                continue
            数据 = 项.data(Qt.UserRole)
            if 数据:
                结果.append(数据)
        return 结果

    # ---------------- 分享 ----------------

    def 分享选中项(self):
        if not self.网络:
            QMessageBox.warning(self, "提示", "请先登录")
            return
        选中 = self._获取选中项()
        if not 选中:
            QMessageBox.warning(self, "提示", "请先选中至少一项")
            return
        # 交由主窗口弹出分享对话框并执行创建
        self.分享文件请求.emit(选中)

    # ---------------- 回收站 ----------------

    def 查看回收站(self):
        if not self.网络:
            QMessageBox.warning(self, "提示", "请先登录")
            return
        self.当前视图 = "回收站"
        self.当前父ID = ""
        self.当前路径 = "/♻️ 回收站"
        self.路径标签.setText("♻️ 回收站")
        self.状态提示.setText("加载中...")
        self.状态提示.setStyleSheet("color: #2196F3; padding-left: 10px;")

        def 任务():
            文件 = 文件接口(self.网络)
            return 文件.取回收站列表(pageSize=100)

        self.启动网络任务(
            "回收站列表", 任务, self._回收站列表成功, self._回收站列表失败)

    def _回收站列表成功(self, 任务名: str, 数据: dict):
        self.文件表格.setRowCount(0)
        条目列表 = (数据.get("list", []) if isinstance(数据, dict) else [])
        if 条目列表 is None:
            条目列表 = []

        for 条目 in 条目列表:
            行 = self.文件表格.rowCount()
            self.文件表格.insertRow(行)

            是目录 = 条目.get("resType") == 2
            类型 = "📁 目录" if 是目录 else "📄 文件"
            名称 = 条目.get("fileName", "")
            文件ID = str(条目.get("fileId", ""))
            大小 = 条目.get("fileSize", 0) or 0
            删除时间原始 = (条目.get("dtime")
                          or 条目.get("deleteTime")
                          or 条目.get("utime"))
            if 删除时间原始 and isinstance(删除时间原始, (int, float)):
                删除时间 = 格式化时间戳(删除时间原始)
            else:
                删除时间 = str(删除时间原始 or "—")

            剩余 = 条目.get("leftTime")
            if 剩余 and isinstance(剩余, (int, float)) and 剩余 > 0:
                删除时间 = f"{删除时间}（剩 {格式化时长(剩余)}）"

            类型项 = QTableWidgetItem(类型)
            类型项.setData(Qt.UserRole, {
                "是目录": 是目录, "名称": 名称,
                "文件ID": 文件ID,
            })
            self.文件表格.setItem(行, 0, 类型项)

            名称项 = QTableWidgetItem(名称)
            名称项.setToolTip(名称)
            self.文件表格.setItem(行, 1, 名称项)

            self.文件表格.setItem(
                行, 2,
                QTableWidgetItem("—" if 是目录 else 格式化大小(大小)))
            self.文件表格.setItem(行, 3, QTableWidgetItem(删除时间))

        self.状态提示.setText(f"回收站共 {len(条目列表)} 项")
        self.状态提示.setStyleSheet("color: #4CAF50; padding-left: 10px;")
        logger.info(f"回收站列表加载成功，共 {len(条目列表)} 项")

    def _回收站列表失败(self, 任务名: str, 错误: str):
        logger.error(f"回收站列表失败：{错误}")
        self.状态提示.setText(f"加载失败：{错误}")
        self.状态提示.setStyleSheet("color: #F44336; padding-left: 10px;")

    def 清空回收站(self):
        if not self.网络:
            QMessageBox.warning(self, "提示", "请先登录")
            return

        应答 = QMessageBox.question(
            self, "清空回收站",
            "⚠️ 确定要清空回收站吗？\n\n"
            "回收站中的所有文件将被永久删除，此操作不可撤销！")
        if 应答 != QMessageBox.Yes:
            return

        self.清空回收站按钮.setEnabled(False)
        self.状态提示.setText("正在清空...")
        self.状态提示.setStyleSheet("color: #FF9800; padding-left: 10px;")
        logger.info("[界面] 开始清空回收站")

        def 任务():
            文件 = 文件接口(self.网络)
            结果 = 文件.清空回收站()
            taskId = None
            if isinstance(结果, dict):
                taskId = 结果.get("taskId")

            if taskId:
                logger.info(f"[界面] 清空任务已创建：taskId={taskId}")
                任务接口实例 = 任务接口(self.网络)
                任务接口实例.等待任务完成(
                    taskId, 最长等待秒=30.0, 轮询间隔秒=1.0)
            return {"taskId": taskId}

        def 成功(任务名, 结果):
            self.清空回收站按钮.setEnabled(True)
            taskId = 结果.get("taskId") if isinstance(结果, dict) else None
            if taskId:
                self.状态提示.setText(f"✅ 清空完成（taskId={taskId}）")
            else:
                self.状态提示.setText("✅ 清空完成")
            self.状态提示.setStyleSheet(
                "color: #4CAF50; padding-left: 10px;")
            QMessageBox.information(self, "成功", "回收站已清空")
            if self.当前视图 == "回收站":
                self.查看回收站()

        def 失败(任务名, 错误):
            self.清空回收站按钮.setEnabled(True)
            logger.error(f"清空回收站失败：{错误}")
            self.状态提示.setText(f"清空失败：{错误}")
            self.状态提示.setStyleSheet(
                "color: #F44336; padding-left: 10px;")
            QMessageBox.warning(self, "清空失败", 错误)

        self.启动网络任务("清空回收站", 任务, 成功, 失败)