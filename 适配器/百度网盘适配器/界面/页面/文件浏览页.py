# 百度网盘适配器/界面/页面/文件浏览页.py
"""文件浏览页

已实现：
  - 列目录（GET /api/list）
  - 双击进入子目录
  - 面包屑导航 + 返回上级 + 回到根目录
  - 分页加载（上一页/下一页/每页数量）
  - **下载选中/双击文件下载**（Phase 5，走 `核心/下载/下载服务.py`）

⚠️ 上传 / 删除 / 新建文件夹 / 分享 / 回收站 属 Phase 7，
   目前只放按钮并明确提示，不调用未实现的接口。
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QGroupBox, QMessageBox, QTableWidget, QTableWidgetItem,
    QHeaderView, QComboBox, QAbstractItemView, QFileDialog,
    QInputDialog, QProgressDialog,
)
from PySide6.QtCore import Qt, Signal

from 核心.接口.文件接口 import (
    文件接口, 是目录, 取文件名, 取路径, 父目录, 分类名,
)
from 核心.下载.下载服务 import 下载服务
from 核心.接口.管理接口 import 管理接口
from 核心.接口.上传接口 import 上传接口
from 核心.接口.回收站接口 import 回收站接口, 彻底删除提示
from 核心.接口.任务接口 import 任务接口

from ..格式化工具 import 格式化大小, 格式化时间戳
from .基类 import 页面基类

logger = logging.getLogger("百度网盘.界面.文件浏览")

每页选项 = [100, 200, 500, 1000]
默认每页 = 100

# 表格列
列_类型, 列_名称, 列_大小, 列_修改时间 = range(4)


class 文件浏览页(页面基类):
    """文件浏览页"""

    # 保持与主界面的既有契约：选中项分享请求
    分享文件请求 = Signal(list)
    # 下载进度（文件名, 已下载字节, 总字节）
    下载进度 = Signal(str, int, int)
    # 单个文件下载结束（文件名, 是否成功, 提示）
    下载结束 = Signal(str, bool, str)
    # 上传进度（阶段, 百分比）
    上传进度 = Signal(str, int)

    def __init__(self, 主窗口, parent=None):
        super().__init__(主窗口, parent)

        self.当前目录 = "/"
        self.当前页 = 1
        self.每页 = 默认每页
        self.还有下一页 = False
        self.当前条目: list[dict] = []
        self.下载中 = False
        # ⚠️ Phase 7 接线上传按钮时 _上传() 会读这个标志，
        #    但 Phase 2 重写本文件时漏了初始化 → 真机点「上传」直接
        #    AttributeError。所有「上传中」相关属性一律在此集中声明。
        self.上传中 = False

        self._构建界面()
        # 跨线程进度回主线程更新
        self.下载进度.connect(self._下载进度更新)
        self.下载结束.connect(self._下载结束)
        self.上传进度.connect(self._上传进度更新)

    # ==================== UI ====================

    def _构建界面(self):
        布局 = QVBoxLayout(self)

        # ---- 路径栏 ----
        路径栏 = QHBoxLayout()

        self.根目录按钮 = QPushButton("🏠 根目录")
        self.根目录按钮.clicked.connect(lambda: self.加载目录("/"))
        路径栏.addWidget(self.根目录按钮)

        self.上级按钮 = QPushButton("⬆ 上级")
        self.上级按钮.clicked.connect(self.返回上级)
        路径栏.addWidget(self.上级按钮)

        # 面包屑容器（动态重建）
        self.面包屑容器 = QWidget()
        self.面包屑布局 = QHBoxLayout(self.面包屑容器)
        self.面包屑布局.setContentsMargins(0, 0, 0, 0)
        self.面包屑布局.setSpacing(2)
        self.面包屑容器.setStyleSheet(
            "background: #f5f5f5; border: 1px solid #ddd; border-radius: 4px;")
        路径栏.addWidget(self.面包屑容器, 1)

        self.刷新按钮 = QPushButton("🔄 刷新")
        self.刷新按钮.clicked.connect(self.刷新当前视图)
        路径栏.addWidget(self.刷新按钮)
        布局.addLayout(路径栏)

        # ---- 操作栏 ----
        操作组 = QGroupBox("⚡ 操作")
        操作布局 = QHBoxLayout()

        self.新建文件夹按钮 = QPushButton("📁+ 新建文件夹")
        self.新建文件夹按钮.setToolTip("在当前目录下创建新文件夹（/api/create isdir=1）")
        self.新建文件夹按钮.clicked.connect(self.新建文件夹)
        操作布局.addWidget(self.新建文件夹按钮)

        self.上传文件按钮 = QPushButton("📤 上传文件")
        self.上传文件按钮.setStyleSheet(
            "background: #4CAF50; color: white; padding: 6px 14px; "
            "border-radius: 4px; font-weight: bold;")
        self.上传文件按钮.clicked.connect(self.选择并上传文件)
        操作布局.addWidget(self.上传文件按钮)

        self.上传文件夹按钮 = QPushButton("📁 上传文件夹")
        self.上传文件夹按钮.clicked.connect(self.选择并上传文件夹)
        操作布局.addWidget(self.上传文件夹按钮)

        # ✅ 下载已可用（Phase 5 完成）
        self.下载文件按钮 = QPushButton("📥 下载选中")
        self.下载文件按钮.setStyleSheet(
            "background: #2196F3; color: white; padding: 6px 14px; "
            "border-radius: 4px; font-weight: bold;")
        self.下载文件按钮.setToolTip("把选中的文件下载到本地（自动去重，不覆盖）")
        self.下载文件按钮.clicked.connect(self.下载选中项)
        操作布局.addWidget(self.下载文件按钮)

        self.分享选中按钮 = QPushButton("🔗 分享选中")
        self.分享选中按钮.setStyleSheet(
            "background: #9C27B0; color: white; padding: 6px 14px; "
            "border-radius: 4px; font-weight: bold;")
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

        self.清空回收站按钮 = QPushButton("🔥 彻底删除说明")
        self.清空回收站按钮.setToolTip(
            "百度对「彻底删除」启用了短信二次验证，客户端无法自动完成")
        self.清空回收站按钮.clicked.connect(self.说明彻底删除)
        操作布局.addWidget(self.清空回收站按钮)

        self.状态提示 = QLabel("就绪")
        self.状态提示.setStyleSheet("color: #666; padding-left: 10px;")
        操作布局.addWidget(self.状态提示)

        操作组.setLayout(操作布局)
        布局.addWidget(操作组)

        # ---- 表格 ----
        self.文件表格 = QTableWidget()
        self.文件表格.setColumnCount(4)
        self.文件表格.setHorizontalHeaderLabels(["类型", "名称", "大小", "修改时间"])
        self.文件表格.horizontalHeader().setSectionResizeMode(
            列_名称, QHeaderView.Stretch)
        self.文件表格.setColumnWidth(列_类型, 80)
        self.文件表格.setColumnWidth(列_大小, 120)
        self.文件表格.setColumnWidth(列_修改时间, 180)
        self.文件表格.setAlternatingRowColors(True)
        self.文件表格.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.文件表格.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.文件表格.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.文件表格.setSortingEnabled(False)
        self.文件表格.doubleClicked.connect(self.双击文件行)
        布局.addWidget(self.文件表格, 1)

        # ---- 分页栏 ----
        分页栏 = QHBoxLayout()
        分页栏.addStretch()

        self.上一页按钮 = QPushButton("◀ 上一页")
        self.上一页按钮.clicked.connect(lambda: self._翻页(self.当前页 - 1))
        分页栏.addWidget(self.上一页按钮)

        self.页码标签 = QLabel("第 1 页")
        self.页码标签.setStyleSheet("padding: 0 10px; font-weight: bold;")
        分页栏.addWidget(self.页码标签)

        self.下一页按钮 = QPushButton("下一页 ▶")
        self.下一页按钮.clicked.connect(lambda: self._翻页(self.当前页 + 1))
        分页栏.addWidget(self.下一页按钮)

        分页栏.addSpacing(20)
        分页栏.addWidget(QLabel("每页："))
        self.每页下拉 = QComboBox()
        for n in 每页选项:
            self.每页下拉.addItem(str(n), n)
        self.每页下拉.setCurrentIndex(每页选项.index(默认每页))
        self.每页下拉.currentIndexChanged.connect(self._改变每页)
        分页栏.addWidget(self.每页下拉)

        分页栏.addStretch()
        布局.addLayout(分页栏)

        self._更新面包屑()
        self._更新分页控件()

    # ==================== 登录态 ====================

    def 设置启用(self, 已登录: bool):
        for 按钮 in (self.根目录按钮, self.上级按钮, self.刷新按钮,
                    self.上传文件按钮, self.上传文件夹按钮,
                    self.新建文件夹按钮, self.下载文件按钮,
                    self.分享选中按钮, self.删除文件按钮,
                    self.查看回收站按钮, self.清空回收站按钮,
                    self.每页下拉):
            按钮.setEnabled(已登录)

    def 重置(self):
        self.当前目录 = "/"
        self.当前页 = 1
        self.当前条目 = []
        self.还有下一页 = False
        self.文件表格.setRowCount(0)
        self.状态提示.setText("就绪")
        self._更新面包屑()
        self._更新分页控件()

    def 首次进入(self):
        if not self.当前条目:
            self.加载目录(self.当前目录)

    # ==================== 导航 ====================

    def 加载目录(self, 目录路径: str = "", 显示路径: str | None = None):
        """加载指定目录的第一页。

        ⚠️ 兼容主界面既有调用 `加载目录("", "/")`：
        第一个参数即目录路径（空串视作根目录）。
        """
        路径 = (目录路径 or "/").strip() or "/"
        # 丢掉 `.` / `..` 这类无意义段：百度对 `/测试/.` 是会当真的，
        # 不清理的话面包屑和「列目录」都会带上这个丑陋后缀
        段 = [s for s in 路径.split("/") if s not in ("", ".", "..")]
        路径 = "/" + "/".join(段) if 段 else "/"
        self.当前目录 = 路径
        self.当前页 = 1
        self._更新面包屑()
        self._拉取当前页()

    def _拉取当前页(self):
        if not self.网络:
            self.状态提示.setText("未登录")
            return
        目录, 页码, 每页 = self.当前目录, self.当前页, self.每页
        self.状态提示.setText(f"加载中… {目录}")

        def 任务():
            文件 = 文件接口(self.网络)
            return 文件.取文件列表(dir=目录, page=页码, num=每页)

        self.启动网络任务("列目录", 任务, self._列目录成功, self._列目录失败)

    def _列目录成功(self, 任务名: str, 数据: dict):
        条目们 = (数据 or {}).get("list") or []
        # ⚠️ /api/list 不返回总数，只能按「本页是否满」判断有无下一页
        self.还有下一页 = len(条目们) >= self.每页
        self._渲染条目(条目们)
        目录说明 = "根目录" if self.当前目录 == "/" else self.当前目录
        self.状态提示.setText(
            f"{目录说明} · 本页 {len(条目们)} 项"
            + ("（还有下一页）" if self.还有下一页 else "（已到末页）"))
        self._更新分页控件()

    def _列目录失败(self, 任务名: str, 错误: str):
        logger.error(f"列目录失败：{错误}")
        self.状态提示.setText(f"加载失败：{错误}")
        # 未登录是**预期状态**（刚启动/刚退出登录），不要弹模态框打断用户；
        # 而且这里跑在主线程，模态框会阻塞事件循环，观感上就是「界面卡死」。
        if not self.网络 or not self.网络.取会话().get("bduss"):
            return
        QMessageBox.warning(self, "加载失败", f"无法加载目录：\n{错误}")

    def 刷新当前视图(self):
        self._拉取当前页()

    def 返回上级(self):
        if self.当前目录 in ("", "/"):
            return
        self.加载目录(父目录(self.当前目录))

    def _导航到路径(self, 目标路径: str):
        self.加载目录(目标路径)

    def 双击文件行(self, 索引):
        行 = 索引.row()
        if 行 < 0 or 行 >= len(self.当前条目):
            return
        条目 = self.当前条目[行]
        if 是目录(条目):
            self.加载目录(取路径(条目))
        else:
            # ✅ Phase 5：双击文件直接下载
            self._下载条目们([条目])

    # ==================== 下载（Phase 5） ====================

    def _选中条目们(self) -> list[dict]:
        """取表格里当前选中的条目（保持行序）。"""
        行号 = sorted({i.row() for i in self.文件表格.selectedIndexes()})
        return [self.当前条目[r] for r in 行号
                if 0 <= r < len(self.当前条目)]

    def 下载选中项(self):
        """工具栏「下载选中」：把选中的文件下载到用户选择的目录。"""
        if self.下载中:
            QMessageBox.information(self, "提示", "已有下载任务在进行中")
            return
        选中 = self._选中条目们()
        文件们 = [e for e in 选中 if not 是目录(e)]
        if not 文件们:
            QMessageBox.information(
                self, "提示", "请先选中要下载的文件（目录暂不支持打包下载）")
            return
        self._下载条目们(文件们)

    def _下载条目们(self, 条目们: list[dict]):
        if not 条目们:
            return
        目录 = QFileDialog.getExistingDirectory(self, "选择保存目录")
        if not 目录:
            return
        self.下载中 = True
        self.下载文件按钮.setEnabled(False)
        文件列表 = [(取路径(e), 取文件名(e)) for e in 条目们]

        def 任务():
            服务 = 下载服务(self.网络)
            成功 = 0
            详情 = []
            for 远端路径, 名字 in 文件列表:
                def 进度(已下, 总, _n=名字):
                    self.下载进度.emit(_n, int(已下), int(总))

                try:
                    # 下载到目录 → 自动去重，不覆盖已有文件
                    实际 = 服务.下载到目录(远端路径, 目录, 进度回调=进度)
                    成功 += 1
                    详情.append(f"{名字} → {实际.name}")
                    self.下载结束.emit(名字, True, str(实际))
                except Exception as e:
                    logger.error(f"下载 {远端路径} 失败：{e}")
                    self.下载结束.emit(名字, False, str(e))
            return (成功, len(文件列表), 详情)

        self.启动网络任务("下载", 任务, self._下载全部完成, self._下载全部失败)

    def _下载进度更新(self, 名字: str, 已下载: int, 总大小: int):
        if 总大小 > 0:
            百分比 = 已下载 * 100 // 总大小
            self.状态提示.setText(
                f"下载中 {名字}：{百分比}% "
                f"（{格式化大小(已下载)} / {格式化大小(总大小)}）")
        else:
            self.状态提示.setText(f"下载中 {名字}：{格式化大小(已下载)}")

    def _下载结束(self, 名字: str, 成功: bool, 提示: str):
        if not 成功:
            logger.warning(f"[下载] {名字} 失败：{提示}")

    def _下载全部完成(self, 任务名: str, 结果):
        成功, 总数, 详情 = 结果 if isinstance(结果, tuple) else (0, 0, [])
        self.下载中 = False
        self.下载文件按钮.setEnabled(True)
        self.状态提示.setText(f"下载完成：成功 {成功}/{总数}")
        if 成功:
            正文 = "\n".join(详情[:15])
            if len(详情) > 15:
                正文 += f"\n… 其余 {len(详情) - 15} 个"
            QMessageBox.information(
                self, "下载完成",
                f"成功下载 {成功}/{总数} 个文件。\n\n{正文}")
        else:
            QMessageBox.warning(self, "下载失败", "全部文件下载失败，详见日志页。")

    def _下载全部失败(self, 任务名: str, 错误: str):
        self.下载中 = False
        self.下载文件按钮.setEnabled(True)
        self.状态提示.setText(f"下载失败：{错误}")
        QMessageBox.warning(self, "下载失败", f"下载任务异常：\n{错误}")

    # ==================== 新建文件夹（Phase 3 接口） ====================

    def 新建文件夹(self):
        if not self.网络:
            QMessageBox.warning(self, "提示", "请先登录")
            return
        名字, 确定 = QInputDialog.getText(
            self, "新建文件夹", "文件夹名称：", text="新建文件夹")
        名字 = (名字 or "").strip()
        if not 确定 or not 名字:
            return
        if any(c in 名字 for c in '\\/:*?"<>|'):
            QMessageBox.warning(self, "名称非法", '文件夹名不能包含 \\ / : * ? " < > |')
            return

        目录 = self.当前目录.rstrip("/")
        目标路径 = f"{目录}/{名字}"
        logger.info(f"[文件浏览] 新建文件夹 {目标路径}")

        def 任务():
            return 管理接口(self.网络).新建文件夹(目标路径, 目录 or "/")

        self.启动网络任务("新建文件夹", 任务, self._操作成功, self._操作失败)

    # ==================== 上传（Phase 4 接口） ====================

    def 选择并上传文件(self):
        路径们, _ = QFileDialog.getOpenFileNames(self, "选择要上传的文件")
        if 路径们:
            self._上传(路径们, 是文件夹=False)

    def 选择并上传文件夹(self):
        目录 = QFileDialog.getExistingDirectory(self, "选择要上传的文件夹")
        if 目录:
            self._上传([目录], 是文件夹=True)

    def _上传(self, 路径们: list, 是文件夹: bool):
        if not self.网络:
            QMessageBox.warning(self, "提示", "请先登录")
            return
        if self.上传中:
            QMessageBox.information(self, "提示", "已有上传任务在进行中")
            return
        if not 路径们:
            return

        目标目录 = self.当前目录 or "/"
        任务名 = "上传文件夹" if 是文件夹 else "上传文件"
        logger.info(f"[文件浏览] {任务名} → {目标目录}，共 {len(路径们)} 项")

        # 去抖：连续相同的 (阶段, 百分比) 不再跨线程投递。
        # 实测每次回调只发射 ~7~20 次/文件（见 上传总调度._进度器），量级很小，
        # 这里只是顺手减少重复信号，**不是**崩溃的修复项。
        上次进度 = [None, None]

        def 进度(阶段, 比例):
            百分比 = int(float(比例) * 100)
            if 阶段 == 上次进度[0] and 百分比 == 上次进度[1]:
                return
            上次进度[0], 上次进度[1] = 阶段, 百分比
            # 后台线程 emit，Qt 自动以队列方式投递到主线程
            self.上传进度.emit(str(阶段), 百分比)

        def 任务():
            # ✅ 真正走 上传接口 → 上传总调度 → precreate/superfile2/create
            接口 = 上传接口(self.网络)
            成功 = 0
            失败: list[str] = []
            for p in 路径们:
                try:
                    if 是文件夹:
                        结果们 = 接口.上传文件夹(p, 目标目录, 进度回调=进度)
                        成功 += len(结果们)
                    else:
                        接口.上传文件(p, 目标目录, 进度回调=进度)
                        成功 += 1
                except Exception as e:
                    logger.error(f"[文件浏览] 上传 {p} 失败：{type(e).__name__}: {e}")
                    失败.append(f"{Path(p).name}: {e}")
            return (成功, len(路径们), 失败)

        self._设置上传状态(True)
        try:
            self.启动网络任务(任务名, 任务, self._上传完成, self._上传失败)
        except Exception:
            # 注意：启动网络任务() 是**异步**的，正常返回时任务还在跑，
            # 所以这里不能无条件复位（否则守卫立刻失效、按钮可重复点击）。
            # 只有「连线程都没起来」这种同步异常才需要回滚状态。
            self._设置上传状态(False)
            raise

    def _设置上传状态(self, 进行中: bool):
        """统一管理上传中的界面状态（按钮禁用 + 标志位）。"""
        self.上传中 = bool(进行中)
        启用 = not 进行中
        self.上传文件按钮.setEnabled(启用)
        self.上传文件夹按钮.setEnabled(启用)

    def _上传进度更新(self, 阶段: str, 百分比: int):
        self.状态提示.setText(f"上传中 · {阶段} {百分比}%")

    def _上传完成(self, 任务名: str, 结果):
        成功, 总数, 失败 = 结果 if isinstance(结果, tuple) else (0, 0, [])
        try:
            self.状态提示.setText(f"上传完成：成功 {成功}/{总数}")
            if 失败:
                QMessageBox.warning(
                    self, "部分失败",
                    f"成功 {成功}/{总数}。\n\n失败详情：\n" + "\n".join(失败[:10]))
            else:
                QMessageBox.information(self, "上传完成", f"成功上传 {成功} 项。")
            self.刷新当前视图()
        except Exception as e:
            # 弹窗/刷新失败也不能让按钮永远禁用
            logger.error(f"[文件浏览] 上传完成收尾异常（已忽略）：{e}")
        finally:
            self._设置上传状态(False)

    def _上传失败(self, 任务名: str, 错误: str):
        try:
            self.状态提示.setText(f"上传失败：{错误}")
            QMessageBox.warning(self, "上传失败", f"上传任务异常：\n{错误}")
        except Exception as e:
            logger.error(f"[文件浏览] 上传失败收尾异常（已忽略）：{e}")
        finally:
            self._设置上传状态(False)

    # ==================== 删除（Phase 3 接口） ====================

    def 删除选中项(self):
        if not self.网络:
            QMessageBox.warning(self, "提示", "请先登录")
            return
        选中 = self._选中条目们()
        if not 选中:
            QMessageBox.information(self, "提示", "请先选中要删除的文件或目录")
            return

        名称 = [取文件名(e) for e in 选中]
        预览 = "\n".join(f"  • {n}" for n in 名称[:8])
        if len(名称) > 8:
            预览 += f"\n  … 其余 {len(名称) - 8} 项"
        应答 = QMessageBox.question(
            self, "确认删除",
            f"将删除以下 {len(选中)} 项（会进入回收站，可还原）：\n\n{预览}\n\n是否继续？")
        if 应答 != QMessageBox.Yes:
            return

        路径们 = [取路径(e) for e in 选中 if 取路径(e)]

        def 任务():
            结果 = 管理接口(self.网络).删除(路径们)
            taskid = 结果.get("taskid")
            if taskid:
                任务接口(self.网络).等待任务(taskid, 间隔秒=2, 超时秒=120)
            return 结果

        self.启动网络任务("删除", 任务, self._删除完成, self._操作失败)

    def _删除完成(self, 任务名: str, 结果):
        self.状态提示.setText("删除完成")
        QMessageBox.information(
            self, "已删除", "删除完成，文件已移入回收站（可在网页版回收站还原）。")
        self.刷新当前视图()

    # ==================== 分享（Phase 6a 接口） ====================

    def 分享选中项(self):
        选中 = self._选中条目们()
        if not 选中:
            QMessageBox.information(self, "提示", "请先选中要分享的文件或目录")
            return
        # 交给主界面弹「创建分享」对话框 → 分享页执行
        self.分享文件请求.emit(选中)

    # ==================== 回收站（Phase 3 接口） ====================

    def 查看回收站(self):
        if not self.网络:
            QMessageBox.warning(self, "提示", "请先登录")
            return

        def 任务():
            return 回收站接口(self.网络).取全部(num=100)

        self.启动网络任务("回收站", 任务, self._回收站成功, self._操作失败)

    def _回收站成功(self, 任务名: str, 条目们: list):
        条目们 = list(条目们 or [])
        if not 条目们:
            QMessageBox.information(self, "回收站", "回收站是空的。")
            return

        名称 = [取文件名(e) for e in 条目们]
        预览 = "\n".join(f"  • {n}" for n in 名称[:15])
        if len(名称) > 15:
            预览 += f"\n  … 其余 {len(名称) - 15} 项"

        框 = QMessageBox(self)
        框.setWindowTitle(f"回收站（{len(条目们)} 项）")
        框.setText(预览)
        框.setIcon(QMessageBox.Information)
        还原按钮 = 框.addButton("♻️ 全部还原", QMessageBox.AcceptRole)
        框.addButton("关闭", QMessageBox.RejectRole)
        框.exec()
        if 框.clickedButton() is 还原按钮:
            self._还原全部([e.get("fs_id") for e in 条目们
                          if e.get("fs_id") is not None])

    def _还原全部(self, fid列表: list):
        if not fid列表:
            return

        def 任务():
            return 回收站接口(self.网络).还原(fid列表)

        def 成功(任务名, 结果):
            失败 = 结果.get("faillist") or []
            if 失败:
                QMessageBox.warning(
                    self, "部分失败", f"有 {len(失败)} 项还原失败：{失败}")
            else:
                QMessageBox.information(self, "完成", "已全部还原。")
            self.刷新当前视图()

        self.启动网络任务("还原", 任务, 成功, self._操作失败)

    def 说明彻底删除(self):
        """🔴 彻底删除不提供自动化 —— 会触发 errno:132 短信风控。

        见 PLAN.md R4 与 `核心/接口/回收站接口.py` 的模块文档。
        """
        QMessageBox.information(self, "关于「彻底删除」", 彻底删除提示)

    # ==================== 通用回调 ====================

    def _操作成功(self, 任务名: str, 结果):
        self.状态提示.setText(f"{任务名}完成")
        self.刷新当前视图()

    def _操作失败(self, 任务名: str, 错误: str):
        logger.error(f"[文件浏览] {任务名} 失败：{错误}")
        self.状态提示.setText(f"{任务名}失败：{错误}")
        QMessageBox.warning(self, f"{任务名}失败", str(错误))

    # ==================== 分页 ====================

    def _翻页(self, 页码: int):
        if 页码 < 1:
            return
        self.当前页 = 页码
        self._拉取当前页()

    def _改变每页(self):
        self.每页 = int(self.每页下拉.currentData() or 默认每页)
        self.当前页 = 1
        self._拉取当前页()

    def _更新分页控件(self):
        self.页码标签.setText(f"第 {self.当前页} 页")
        self.上一页按钮.setEnabled(self.当前页 > 1)
        self.下一页按钮.setEnabled(self.还有下一页)

    # ==================== 渲染 ====================

    def _渲染条目(self, 条目们: list[dict]):
        self.当前条目 = list(条目们)
        self.文件表格.setRowCount(0)
        self.文件表格.setRowCount(len(条目们))

        for 行, 条目 in enumerate(条目们):
            目录 = 是目录(条目)

            名称 = ("📁 " if 目录 else "📄 ") + 取文件名(条目)
            单元格们 = [
                分类名(条目),
                名称,
                "—" if 目录 else 格式化大小(条目.get("size") or 0),
                格式化时间戳(条目.get("server_mtime") or 0),
            ]
            for 列, 文本 in enumerate(单元格们):
                格 = QTableWidgetItem(str(文本))
                if 列 == 列_名称:
                    # 完整路径放 tooltip，便于确认
                    格.setToolTip(取路径(条目))
                    # 目录加粗，视觉上区分
                    if 目录:
                        字体 = 格.font()
                        字体.setBold(True)
                        格.setFont(字体)
                格.setData(Qt.UserRole, 条目)
                self.文件表格.setItem(行, 列, 格)

    def _更新面包屑(self):
        # 清空旧按钮
        while self.面包屑布局.count():
            项 = self.面包屑布局.takeAt(0)
            控件 = 项.widget()
            if 控件:
                控件.deleteLater()

        def 加按钮(文字: str, 路径: str, 加粗: bool = False):
            钮 = QPushButton(文字)
            钮.setFlat(True)
            钮.setCursor(Qt.PointingHandCursor)
            钮.setStyleSheet(
                "QPushButton { border: none; padding: 4px 8px; color: #1976D2; "
                f"{'font-weight: bold;' if 加粗 else ''} }}"
                "QPushButton:hover { background: #e3f2fd; border-radius: 3px; }")
            钮.clicked.connect(lambda _=False, p=路径: self._导航到路径(p))
            return 钮

        段 = [s for s in (self.当前目录 or "/").split("/") if s]

        if not 段:
            self.面包屑布局.addWidget(加按钮("🏠 根目录", "/", 加粗=True))
        else:
            self.面包屑布局.addWidget(加按钮("🏠", "/"))
            for i, 名 in enumerate(段):
                路径 = "/" + "/".join(段[:i + 1])
                self.面包屑布局.addWidget(QLabel("›"))
                self.面包屑布局.addWidget(
                    加按钮(名, 路径, 加粗=(i == len(段) - 1)))
        self.面包屑布局.addStretch()

    # ==================== 未实现占位 ====================

