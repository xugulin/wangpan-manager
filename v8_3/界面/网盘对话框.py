"""新增 / 编辑网盘对话框。

对应左下角管理面板的「新增网盘」「编辑网盘」两个按钮。

要点
====
* **新增只问两件事**：网盘类型 + 显示名称。实例标识、适配器目录、线程数、启用
  全部自动生成/套用默认值 —— 用户不需要知道"适配器项目"这回事。
* 一个网盘 = 一个**适配器实例**：类型决定用哪个桥后端，标识是全局唯一键；
  凭证存在各自目录的 ``数据/`` 下，所以同一家网盘挂第二个账号时必须有一份
  独立目录 —— 新增第二个账号时，对话框会**自动**把项目自带的适配器复制到
  ``适配器实例/<标识>/``，不再让用户先点「复制适配器项目…」再回来（以前
  那样会先报一句"目录不存在"，很劝退）。
* 编辑时才暴露「高级设置」（适配器目录 / 线程数 / 启用），默认收起，
  需要改再展开；实例标识是主键，只展示、不提供修改。
* 「本地假网盘」不需要网络，V8_3 会自动准备最小目录，方便自测。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout, QGroupBox,
    QHBoxLayout, QInputDialog, QLabel, QLineEdit, QMessageBox, QPushButton,
    QSpinBox, QVBoxLayout, QWidget,
)

from ..配置 import (
    复制适配器项目, 建议副本目录, 默认适配器路径,
    准备假网盘目录, 规范化实例, 生成唯一标识,
)
from ..核心.适配器 import 适配器规格, 类型图标, 规范化标识
from ..核心.模型 import 网盘类型

#: 凭证文件名（只用来在提示里告诉用户登录数据落在哪）
凭证文件 = {
    网盘类型.百度: "数据/会话.json",
    网盘类型.光鸭: "数据/令牌.json",
    网盘类型.夸克: "数据/凭证.json",
}

#: 新实例默认线程数
默认线程数 = 12


class 网盘编辑对话框(QDialog):
    """返回 :attr:`结果` —— 标准化后的实例字典。

    新增模式下用户只填类型和名称，其余由对话框自动配好（含自动复制适配器目录）。
    """

    def __init__(self, 配置: dict, 实例: dict | None = None, 父窗口=None):
        super().__init__(父窗口)
        self.配置 = 配置
        self.原实例 = dict(实例) if 实例 else None
        self.结果: dict | None = None
        self.是否新增 = 实例 is None

        self.setWindowTitle("新增网盘" if self.是否新增 else "编辑网盘")
        self.resize(640, 300 if self.是否新增 else 420)
        self._构建()
        if self.是否新增:
            self._类型变化()
        else:
            self._填充原实例()
            self._刷新提示()

    # ---------------- 构建 ----------------

    def _构建(self):
        布局 = QVBoxLayout(self)

        表单组 = QGroupBox("网盘信息")
        表单 = QFormLayout(表单组)

        self.类型框 = QComboBox()
        for 类型 in (网盘类型.百度, 网盘类型.光鸭, 网盘类型.夸克, 网盘类型.假):
            self.类型框.addItem(f"{类型图标.get(类型, '☁')} {类型.中文名}", 类型.value)
        self.类型框.currentIndexChanged.connect(self._类型变化)
        表单.addRow("网盘类型：", self.类型框)

        self.名称框 = QLineEdit()
        self.名称框.setPlaceholderText("例如：百度网盘 / 百度小号")
        self.名称框.textChanged.connect(self._刷新提示)
        表单.addRow("显示名称：", self.名称框)
        布局.addWidget(表单组)

        # ---- 高级设置：只有「编辑」才挂到界面上，新增时压根不显示 ----
        self.高级组 = self._建高级组()
        self.高级组.setVisible(False)
        self.高级开关 = QCheckBox("显示高级设置（适配器目录 / 线程数 / 启用）")
        self.高级开关.setToolTip(
            "平时不用管：新增时这些都会自动配好。\n"
            "想改目录、并发线程数，或临时停用某个网盘，再展开这里。")
        self.高级开关.toggled.connect(self.高级组.setVisible)
        # 新增模式：高级项**整块不出现**（连开关一起藏），只留类型+名称；
        # 编辑模式：露出开关，但默认仍是收起的。
        self.高级开关.setVisible(not self.是否新增)
        布局.addWidget(self.高级开关)
        布局.addWidget(self.高级组)

        self.提示标签 = QLabel()
        self.提示标签.setWordWrap(True)
        self.提示标签.setTextInteractionFlags(Qt.TextSelectableByMouse)
        布局.addWidget(self.提示标签, 1)

        按钮行 = QHBoxLayout()
        按钮行.addStretch(1)
        确定 = QPushButton("确定")
        确定.setObjectName("PrimaryButton")
        确定.clicked.connect(self._确定)
        取消 = QPushButton("取消")
        取消.clicked.connect(self.reject)
        按钮行.addWidget(确定)
        按钮行.addWidget(取消)
        布局.addLayout(按钮行)

        if not self.是否新增:
            # 编辑模式才允许改类型以外的细节，且默认收起
            self.类型框.setEnabled(False)      # 类型决定后端，编辑时不允许改
            self.高级开关.setChecked(False)

    def _建高级组(self) -> QWidget:
        组 = QGroupBox("高级设置")
        表单 = QFormLayout(组)

        目录行 = QWidget()
        目录布局 = QHBoxLayout(目录行)
        目录布局.setContentsMargins(0, 0, 0, 0)
        self.目录框 = QLineEdit()
        self.目录框.textChanged.connect(self._刷新提示)
        目录布局.addWidget(self.目录框, 1)
        浏览 = QPushButton("浏览…")
        浏览.clicked.connect(self._浏览目录)
        目录布局.addWidget(浏览)
        复制 = QPushButton("复制适配器项目…")
        复制.setToolTip("同一家网盘挂第二个账号时，复制一份独立目录（凭证互不干扰）")
        复制.clicked.connect(self._复制项目)
        目录布局.addWidget(复制)
        表单.addRow("适配器目录：", 目录行)

        self.线程框 = QSpinBox()
        self.线程框.setRange(1, 64)
        self.线程框.setValue(默认线程数)
        self.线程框.setToolTip("该网盘桥进程里的并发工作线程数")
        表单.addRow("线程数：", self.线程框)

        self.启用框 = QCheckBox("启用（出现在左侧导航栏）")
        self.启用框.setChecked(True)
        表单.addRow("", self.启用框)
        return 组

    def _填充原实例(self):
        实例 = 规范化实例(self.原实例)
        idx = self.类型框.findData(实例["类型"])
        self.类型框.setCurrentIndex(max(0, idx))
        self.名称框.setText(实例["名称"])
        self.目录框.setText(实例["路径"])
        self.线程框.setValue(int(实例["线程数"]))
        self.启用框.setChecked(bool(实例["启用"]))

    # ---------------- 交互 ----------------

    def _当前类型(self) -> 网盘类型:
        return 网盘类型.解析(self.类型框.currentData())

    def _类型变化(self):
        if self.是否新增:
            类型 = self._当前类型()
            self.名称框.setText(类型.中文名)
            self._标识 = self._建议标识(类型)
        self._刷新提示()

    def _其他实例(self) -> list[dict]:
        from ..配置 import 网盘实例列表
        全部 = 网盘实例列表(self.配置)
        if self.原实例 is None:
            return 全部
        原标识 = 规范化实例(self.原实例)["标识"]
        return [x for x in 全部 if x["标识"] != 原标识]

    def _建议标识(self, 类型: 网盘类型) -> str:
        """新实例的标识：``baidu`` → ``baidu_2`` → ``baidu_3``…（与配置模块同一套规则）"""
        return 生成唯一标识(self.配置, 类型)

    def _目录计划(self, 类型: 网盘类型, 标识: str) -> tuple[Path, Path | None, str]:
        """新增时该用哪个适配器目录。

        返回 ``(目标目录, 需要复制的源目录 or None, 给用户看的一句话)``。

        * 这家网盘**第一个**实例 → 直接用项目自带的 ``适配器/<名>``；
        * **第二个**起 → 复制一份到 ``适配器实例/<标识>``，凭证互不干扰；
        * 本地假网盘 → 保存时现场准备最小目录。
        """
        自带 = 默认适配器路径(类型)
        if 类型 == 网盘类型.假:
            return 自带, None, "本地假网盘：保存时自动准备一个最小可用目录"
        同类型 = [x for x in self._其他实例() if x["类型"] == 类型.value]
        if not 同类型:
            return 自带, None, f"使用项目自带的适配器目录：{自带}"
        目标 = 建议副本目录(self.配置, 类型).parent / 标识
        源 = Path(同类型[0]["路径"]).expanduser()
        if not 源.is_dir():
            源 = 自带
        if 目标.is_dir():
            return 目标, None, f"复用已有实例目录：{目标}"
        return 目标, 源, f"保存时自动从 {源} 复制一份独立目录到 {目标}（凭证互不干扰）"

    def _浏览目录(self):
        起始 = self.目录框.text().strip() or str(默认适配器路径(self._当前类型()))
        目录 = QFileDialog.getExistingDirectory(self, "选择适配器项目目录", 起始)
        if 目录:
            self.目录框.setText(目录)

    def _复制项目(self):
        类型 = self._当前类型()
        同类型 = [x for x in self._其他实例() if x["类型"] == 类型.value]
        候选源 = str(同类型[0]["路径"]) if 同类型 else str(默认适配器路径(类型))
        源 = QFileDialog.getExistingDirectory(
            self, "选择要复制的源适配器目录", 候选源)
        if not 源:
            return
        建议 = str(建议副本目录(self.配置, 类型))
        名称, ok = QInputDialog.getText(
            self, "复制适配器项目", "新目录（绝对路径或相对项目根）：",
            text=建议)
        if not ok or not 名称.strip():
            return
        目标 = Path(名称.strip()).expanduser()
        try:
            结果 = 复制适配器项目(源, 目标)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "复制失败", str(e))
            return
        self.目录框.setText(str(结果))
        QMessageBox.information(
            self, "复制完成",
            f"已复制到：\n{结果}\n\n登录数据目录是空的，"
            "保存后点「登录 / 管理」登录另一个账号即可。")

    # ---------------- 提示 ----------------

    def _刷新提示(self):
        类型 = self._当前类型()
        名称 = self.名称框.text().strip() or 类型.中文名
        行: list[str] = []

        if self.是否新增:
            标识 = getattr(self, "_标识", "") or self._建议标识(类型)
            目录, 源, 说明 = self._目录计划(类型, 标识)
            行.append(f"ℹ️ 显示名称：{名称}")
            行.append(f"ℹ️ 实例标识：{标识}（自动生成，全局唯一）")
            行.append(f"ℹ️ 适配器目录：{目录}")
            行.append(f"　　{说明}")
            行.append(f"ℹ️ 线程数 {默认线程数} ｜ 启用：是（保存后出现在左侧导航栏）")
        else:
            实例 = 规范化实例(self.原实例)
            标识 = 实例["标识"]
            目录 = Path(self.目录框.text().strip() or 实例["路径"]).expanduser()
            行.append(f"ℹ️ 实例标识：{标识}（主键，不可修改）")
            同类型 = [x for x in self._其他实例() if x["类型"] == 类型.value
                    and Path(x["路径"]).expanduser() == 目录]
            if 同类型:
                行.append(f"⚠️ 目录与「{同类型[0]['名称']}」相同，两个网盘会共用同一份登录数据")
            行.append("ℹ️ 要改目录/线程数/启用，勾上「显示高级设置」")

        可用, 提示 = 适配器规格.校验目录(目录)
        if not 可用:
            if self.是否新增 and 类型 != 网盘类型.假:
                行.append("ℹ️ 该目录还不存在：保存时会自动创建（复制一份适配器）")
            elif 类型 == 网盘类型.假:
                行.append("ℹ️ 本地假网盘：保存时会自动准备最小目录")
            else:
                行.append(f"❌ {提示}")
        凭证 = 凭证文件.get(类型)
        if 凭证 and not self.是否新增:
            行.append(f"ℹ️ 登录凭证位置：{目录 / 凭证}")
        self.提示标签.setText("\n".join(行))

    # ---------------- 确定 ----------------

    def _确定(self):
        类型 = self._当前类型()
        名称 = self.名称框.text().strip() or 类型.中文名

        if self.是否新增:
            标识 = getattr(self, "_标识", "") or self._建议标识(类型)
            冲突 = [x for x in self._其他实例() if x["标识"] == 标识]
            if 冲突:
                QMessageBox.warning(self, "标识冲突",
                                    f"标识「{标识}」已被「{冲突[0]['名称']}」使用")
                return
            计划目录, 源, _ = self._目录计划(类型, 标识)
            try:
                路径 = self._准备目录(类型, 计划目录, 源)
            except Exception as e:  # noqa: BLE001
                QMessageBox.warning(self, "准备适配器目录失败",
                                    f"{e}\n\n目标目录：{计划目录}")
                return
            线程数 = 默认线程数
            启用 = True
        else:
            实例 = 规范化实例(self.原实例)
            标识 = 实例["标识"]
            目录 = self.目录框.text().strip()
            if not 目录:
                QMessageBox.warning(self, "缺少目录", "请填写适配器目录")
                return
            路径 = Path(目录).expanduser()
            可用, 提示 = 适配器规格.校验目录(路径)
            if not 可用:
                if 类型 == 网盘类型.假:
                    路径 = 准备假网盘目录(路径)
                    可用, _ = 适配器规格.校验目录(路径)
                if not 可用:
                    答案 = QMessageBox.question(
                        self, "目录校验未通过",
                        f"{提示}\n\n仍然保存吗？（保存后该网盘会显示为不可用，"
                        "修好目录再点「编辑网盘」即可）",
                        QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
                    if 答案 != QMessageBox.Yes:
                        return
            线程数 = int(self.线程框.value())
            启用 = bool(self.启用框.isChecked())

        self.结果 = 规范化实例({
            "标识": 标识,
            "类型": 类型.value,
            "名称": 名称,
            "路径": str(路径),
            "线程数": 线程数,
            "启用": 启用,
        })
        self.accept()

    @staticmethod
    def _准备目录(类型: 网盘类型, 计划目录: Path, 源: Path | None) -> Path:
        """把目录真正准备好：假网盘现场建、第二账号自动复制、其余要求已存在。"""
        if 类型 == 网盘类型.假:
            return 准备假网盘目录(计划目录)
        if 计划目录.is_dir():
            return 计划目录
        if 源 is not None:
            return 复制适配器项目(源, 计划目录)
        return 计划目录
