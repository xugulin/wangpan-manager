# 百度网盘适配器/核心/上传/上传总调度.py
"""
上传总调度
作用：百度网盘上传全流程的唯一编排入口，串起

    算哈希 → 秒传尝试 → precreate → superfile2 分片 → create

`核心/接口/上传接口.py` 只是面向界面的薄封装，实际逻辑都在这里。

【已修复的遗留缺陷 - 2026-09-14】
  原骨架的 `上传总调度.py:90` 有一处赋值错误：
      self.秒传.检查是否可闪电上传(taskId=..., gcid=..., cid=取结果.令牌.gcid)
                                                        ^^^ 应为 cid
  即把 `cid` 误赋成了 `gcid` 的副本。该缺陷连同整个 OSS/gcid 体系
  在本次百度化改造中一并移除（百度上传协议里没有 gcid / cid 概念）。

【三个实测坑（见 PLAN.md §5.2）】
  1. `create.block_list` 用 **superfile2 返回的 md5**，
     不是 precreate 响应里的 `[0,1,2…]` 占位序号。
  2. 单分片文件的 `block_list[0]` 是 `md5(整文件)`，不是 `md5(首256KB)`。
  3. 上传域名动态，必须走 locateupload（见 `分片上传.py`）。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..网络.网络客户端 import 网络客户端
from ..接口.文件接口 import 文件接口, 是目录
from .哈希计算器 import 计算全部哈希, 分片大小
from .分片上传 import 分片上传器, 上传域名解析器
from .秒传服务 import 秒传服务

logger = logging.getLogger("百度网盘.上传.调度")

# 进度阶段名（界面直接显示）
阶段_算哈希 = "计算哈希"
阶段_秒传 = "秒传检查"
阶段_预创建 = "预创建"
阶段_上传 = "上传分片"
阶段_创建 = "创建文件"

# 各阶段在总进度里的占比
_占比 = {
    阶段_算哈希: (0.00, 0.15),
    阶段_秒传: (0.15, 0.20),
    阶段_预创建: (0.20, 0.25),
    阶段_上传: (0.25, 0.90),
    阶段_创建: (0.90, 1.00),
}

# 批量上传时每个文件之间的让位时间。
# 实测每文件 0.05s 即可显著降低主线程事件队列压力，而对总耗时影响 <5%。
# 诊断结论：这不是崩溃的修复项（批量上传本身不崩），只为界面流畅度。
默认批间隔秒 = 0.05


# ==========================================================================
# 本地上传记录
# --------------------------------------------------------------------------
# 百度侧未抓到「上传记录」查询接口，因此改为**本地记录**：
# 每次成功上传后追加一条到 `数据/上传记录.json`，供界面「上传记录页」展示。
# ==========================================================================
记录文件 = Path(__file__).resolve().parents[2] / "数据" / "上传记录.json"
记录上限 = 500


def 追加上传记录(记录: dict) -> None:
    """把一条上传记录追加到本地文件（失败不影响上传本身）。"""
    try:
        记录文件.parent.mkdir(parents=True, exist_ok=True)
        现有: list = []
        if 记录文件.is_file():
            try:
                现有 = json.loads(记录文件.read_text(encoding="utf-8")) or []
                if not isinstance(现有, list):
                    现有 = []
            except Exception:
                现有 = []
        现有.insert(0, 记录)
        del 现有[记录上限:]
        记录文件.write_text(
            json.dumps(现有, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning(f"[调度] 写上传记录失败（不影响上传）：{e}")


def 读取上传记录() -> list[dict]:
    """读取本地上传记录（最新在前）。"""
    try:
        if 记录文件.is_file():
            数据 = json.loads(记录文件.read_text(encoding="utf-8"))
            return 数据 if isinstance(数据, list) else []
    except Exception as e:
        logger.warning(f"[调度] 读上传记录失败：{e}")
    return []


@dataclass
class 上传结果:
    """单个文件的上传结果。"""
    文件路径: str
    文件名: str
    大小: int
    md5: str
    fs_id: int | None = None
    是否秒传: bool = False
    原始: dict = field(default_factory=dict)


class 上传总调度:
    """上传全流程编排。"""

    def __init__(self, 网络: 网络客户端):
        self.网络 = 网络
        self.秒传 = 秒传服务(网络)
        self.域名解析器 = 上传域名解析器(网络)
        self.分片 = 分片上传器(网络, self.域名解析器)
        # 已确认存在的远端目录（避免递归上传时反复查同一层）
        self._已建目录: set[str] = set()
        # 当前正在处理的文件名（仅用于进度文案，见 _设置进度标签）
        self._进度标签 = ""

    # ==================== 远端目录 ====================

    def 远端路径存在(self, 目标路径: str) -> dict | None:
        """查路径是否存在：返回条目 dict，不存在返回 None。

        走 `/api/filemetas`，**结果在 `info[0]`**（不是顶层）。
        路径不存在时 `info` 为空列表、`errno` 仍为 0，所以要靠列表长度判断。
        """
        try:
            数据 = self.网络.请求(
                "GET", "/api/filemetas",
                params={
                    "target": json.dumps([目标路径], ensure_ascii=False,
                                         separators=(",", ":")),
                    "dlink": "0", "text": "1",
                },
                带渠道=False,
            )
        except Exception as e:
            logger.debug(f"[调度] 路径存在性检查失败 {目标路径}：{e}")
            return None
        信息 = 数据.get("info") if isinstance(数据, dict) else None
        if isinstance(信息, list) and 信息:
            return 信息[0]
        return None

    def 存在且是目录(self, 目标路径: str) -> bool:
        条目 = self.远端路径存在(目标路径)
        return bool(条目) and 是目录(条目)

    def 确保远端目录(self, 目标目录: str) -> bool:
        """确保远端目录存在，逐级补齐（上传前调用）。

        用 `filemetas` 先探存在性再按需 `create`，而不是「先建、失败再说」——
        因为**新建已存在的目录时百度返回 `errno:0` 且不会报错**，
        而是**悄悄建出一个带时间戳后缀的新目录**（如
        `/名字` → `/名字_20260914_045549`），这会造成满地垃圾目录。

        :return: 目录已可写用（True）
        """
        目录 = "/" + (目标目录 or "/").strip("/")
        if not 目录.strip("/"):
            return True
        # 🛡️ 防御：`.`/`..` 是路径拼接出 bug 的信号。
        #    百度对 `/测试/.` 这种路径**不报错**，而是默默建出垃圾目录，
        #    会把文件全丢进去（2026-09-14 真实事故）。这里宁可报错也不要静默损坏。
        垃圾段 = [段 for 段 in 目录.split("/") if 段 in (".", "..")]
        if 垃圾段:
            raise ValueError(
                f"远端目录含非法路径段 {垃圾段}：{目标目录!r}"
                f"（上游路径拼接有误，请检查调用方的 relative_to/parent 逻辑）")
        if 目录 in self._已建目录:
            return True

        # 先补齐所有祖先目录（从浅到深），否则父目录不存在时 create 会失败
        父 = 目录.rsplit("/", 1)[0] or "/"
        累积 = ""
        for 段 in [s for s in 父.split("/") if s]:
            累积 = f"{累积}/{段}"
            if 累积 in self._已建目录:
                continue
            if self.存在且是目录(累积):
                self._已建目录.add(累积)
                continue
            self._创建目录(累积)
            self._已建目录.add(累积)

        # 再建目标目录本身
        if not self.存在且是目录(目录):
            self._创建目录(目录)
            self._已建目录.add(目录)
        return True

    def _创建目录(self, 路径: str) -> dict:
        """POST /api/create?isdir=1（父目录必须已存在）。"""
        父 = 路径.rsplit("/", 1)[0] or "/"
        logger.info(f"[调度] 新建远端目录 {路径}")
        return self.网络.请求(
            "POST", "/api/create",
            params={"isdir": "1", "rtype": "1"},
            表单数据={
                "path": 路径,
                "isdir": "1",
                "size": "0",
                "block_list": "[]",
                "target_path": 父,
            },
            需要bdstoken=True,
        )

    def 清空目录缓存(self) -> None:
        """清掉「已建目录」缓存（换账号或目录被外部删掉时调）。"""
        self._已建目录.clear()

    # ==================== 底层三段（真实 HTTP） ====================

    def 预创建(self, 目标路径: str, 分片md5列表: list[str],
              目标目录: str = "/") -> dict:
        """① POST /api/precreate。"""
        if not 分片md5列表:
            raise ValueError("分片 md5 列表不能为空")
        数据 = self.网络.请求(
            "POST", "/api/precreate",
            params={"rtype": "1"},
            表单数据={
                "path": 目标路径,
                "autoinit": "1",
                "block_list": json.dumps(分片md5列表),
                "target_path": 目标目录 or "/",
                "local_mtime": str(int(time.time())),
            },
            需要bdstoken=True,
        )
        if not 数据.get("uploadid"):
            raise RuntimeError(f"precreate 未返回 uploadid：{数据}")
        logger.info(
            f"[调度] precreate ✅ {目标路径} 分片数={len(分片md5列表)} "
            f"占位block_list={数据.get('block_list')} "
            f"uploadid={str(数据['uploadid'])[:12]}…")
        return 数据

    def 创建文件(self, 目标路径: str, 大小: int, uploadid: str,
                服务端md5列表: list[str], 目标目录: str = "/") -> dict:
        """③ POST /api/create（`block_list` 必须是 superfile2 返回的 md5）。"""
        if not 服务端md5列表:
            raise ValueError(
                "block_list 不能为空：必须用 superfile2 返回的 md5 列表，"
                "不能用 precreate 响应里的占位序号 [0,1,2…]")
        数据 = self.网络.请求(
            "POST", "/api/create",
            params={"isdir": "0", "rtype": "1"},
            表单数据={
                "path": 目标路径,
                "size": str(大小),
                "uploadid": uploadid,
                "block_list": json.dumps(服务端md5列表),
                "target_path": 目标目录 or "/",
                "local_mtime": str(int(time.time())),
            },
            需要bdstoken=True,
        )
        logger.info(f"[调度] create ✅ {目标路径} fs_id={数据.get('fs_id')} "
                    f"md5={数据.get('md5')}")
        return 数据

    # ==================== 编排 ====================

    @staticmethod
    def _进度器(进度回调, 标签: str = ""):
        """包装进度回调：把「阶段内 0~1」映射成「整文件 0~1」，并去抖。

        去抖规则：**连续重复**的 (阶段, 百分比) 不再上报。
        实测（2026-09-14）：9MB 文件约 13 次回调、20MB 约 20 次、2KB 约 7 次
        —— 量级很小，去抖只削掉约 7%，**不是**性能瓶颈，纯粹是顺手降噪。
        """
        上次 = [None, None]

        def 报(阶段: str, 阶段内进度: float):
            if not 进度回调:
                return
            起, 止 = _占比.get(阶段, (0.0, 1.0))
            总 = 起 + (止 - 起) * max(0.0, min(1.0, 阶段内进度))
            百分比 = int(总 * 100)
            if 阶段 == 上次[0] and 百分比 == 上次[1]:
                return                      # 连续重复 → 丢弃
            上次[0], 上次[1] = 阶段, 百分比
            try:
                进度回调(f"{阶段} · {标签}" if 标签 else 阶段, 总)
            except Exception:
                pass
        return 报

    def _设置进度标签(self, 标签: str) -> None:
        """设置「当前正在处理哪个文件」，只影响进度回调里的显示文案。"""
        self._进度标签 = 标签 or ""

    def 上传单文件(
        self,
        本地路径: str | Path,
        目标目录: str = "/",
        进度回调: Callable[[str, float], None] | None = None,
        允许秒传: bool = True,
        确保目录: bool = False,
    ) -> 上传结果:
        """上传单个文件：算哈希 → 秒传 → precreate → 分片 → create。

        :param 确保目录: 上传前先确认远端目标目录存在，缺则逐级补齐。
            递归上传文件夹时必须开，否则子目录不存在会让 precreate 失败。
        """
        路径 = Path(本地路径)
        if not 路径.is_file():
            raise FileNotFoundError(f"文件不存在：{本地路径}")

        目录 = (目标目录 or "/").rstrip("/")
        文件名 = 路径.name
        远端路径 = f"{目录}/{文件名}"
        报 = self._进度器(进度回调, self._进度标签)

        # ---- 0. 远端目录（仅在需要时查一次，结果有缓存）----
        if 确保目录:
            self.确保远端目录(目录)

        # ---- 1. 哈希（单次读盘算出全部）----
        报(阶段_算哈希, 0.0)
        哈希 = 计算全部哈希(
            路径, 进度回调=lambda 已读, 总: 报(阶段_算哈希, 已读 / max(总, 1)))
        logger.info(
            f"[调度] {文件名} {哈希['大小']}B 分片数={哈希['分片数']} "
            f"整文件md5={哈希['整文件md5']}")

        # ---- 2. 秒传 ----
        if 允许秒传 and 哈希["大小"] > 0:
            报(阶段_秒传, 0.5)
            try:
                if self.秒传.试秒传(
                        远端路径, 路径, 哈希["大小"],
                        哈希["整文件md5"], 哈希["首片md5"], 目录):
                    报(阶段_创建, 1.0)
                    秒传结果 = 上传结果(
                        文件路径=远端路径, 文件名=文件名, 大小=哈希["大小"],
                        md5=哈希["整文件md5"], 是否秒传=True,
                        原始={"errno": 0, "path": 远端路径})
                    追加上传记录({
                        "时间": int(time.time()),
                        "文件名": 文件名,
                        "远端路径": 远端路径,
                        "大小": 哈希["大小"],
                        "md5": 哈希["整文件md5"],
                        "fs_id": None,
                        "是否秒传": True,
                        "分片数": 0,
                    })
                    return 秒传结果
            except Exception as e:
                logger.warning(f"[调度] 秒传检查异常，回落分片上传：{e}")

        # ---- 3. 预创建（block_list = 本地纯净分片 md5）----
        报(阶段_预创建, 0.5)
        预创建结果 = self.预创建(远端路径, 哈希["分片md5列表"], 目录)
        uploadid = 预创建结果["uploadid"]

        # ---- 4. 分片上传 ----
        服务端md5列表 = self.分片.上传全部分片(
            路径, 远端路径, uploadid, 分片大小,
            进度回调=lambda 已传, 总, 片: 报(
                阶段_上传, 已传 / max(总, 1)))

        # ---- 5. 创建（block_list = superfile2 返回的 md5）----
        报(阶段_创建, 0.3)
        原始 = self.创建文件(
            远端路径, 哈希["大小"], uploadid, 服务端md5列表, 目录)
        报(阶段_创建, 1.0)

        结果 = 上传结果(
            文件路径=远端路径, 文件名=文件名, 大小=哈希["大小"],
            md5=哈希["整文件md5"],
            fs_id=原始.get("fs_id"), 是否秒传=False, 原始=原始)
        追加上传记录({
            "时间": int(time.time()),
            "文件名": 文件名,
            "远端路径": 远端路径,
            "大小": 哈希["大小"],
            "md5": 哈希["整文件md5"],
            "fs_id": 原始.get("fs_id"),
            "是否秒传": False,
            "分片数": 哈希["分片数"],
        })
        return 结果

    def 上传文件夹(
        self,
        本地目录: str | Path,
        目标目录: str = "/",
        进度回调: Callable[[str, float], None] | None = None,
        单文件回调: Callable[[str, 上传结果], None] | None = None,
        允许秒传: bool = True,
        批间隔秒: float = 默认批间隔秒,
    ) -> list[上传结果]:
        """递归上传整个文件夹（保留目录结构，并把文件夹本身建到远端）。

        远端布局（实测修正后的行为）::

            上传 本地 /home/x/Screenshots/（内含 照片/a.png、b.png）
            目标目录 /测试
            → /测试/Screenshots/b.png
            → /测试/Screenshots/照片/a.png

        ⚠️ 曾经有两个 bug（2026-09-14 真机暴露并修复）：
          1. 文件**直接位于所选文件夹内**时，`Path("a.txt").parent` 是 `"."`，
             天真的 `f"{目录}/{相对.parent}"` 会拼出 `/测试/.` —— 目录名整个丢失，
             于是文件全被丢进了一个叫 `.` 的错目录（百度居然还接受了）。
          2. 文件夹自身的名字没有拼进远端路径，导致内容被平铺进目标目录。
        """
        根 = Path(本地目录)
        if not 根.is_dir():
            raise NotADirectoryError(f"目录不存在：{本地目录}")

        父目录 = (目标目录 or "/").rstrip("/")
        # ✅ 把「所选文件夹本身」建到目标目录下：
        #    /测试 + Screenshots → /测试/Screenshots
        #    ⚠️ 若远端已存在同名目录，百度**不会合并**，而是建出
        #       `Screenshots_20260914_xxxxxx` 副本（所以别重复传同一文件夹）
        根目录 = f"{父目录}/{根.name}".replace("//", "/")

        文件们 = sorted(p for p in 根.rglob("*") if p.is_file())
        总数 = max(len(文件们), 1)
        logger.info(f"[调度] 文件夹 {根.name}：共 {len(文件们)} 个文件 → {根目录}")

        结果列表: list[上传结果] = []
        for i, 文件 in enumerate(文件们):
            # ✅ 用 parts 过滤而不是直接把 Path.parent 拼进字符串：
            #    直接放在根下的文件，其 parent 是 "."，必须丢掉这一层
            相对 = 文件.relative_to(根)
            子层 = [段 for 段 in 相对.parent.parts if 段 not in (".", "..", "")]
            子目录 = "/".join([根目录, *子层]) or "/"
            子目录 = 子目录.replace("//", "/")

            基, 范围 = i / 总数, 1 / 总数

            def 子进度(阶段: str, p: float, _b=基, _r=范围):
                # 阶段名保持「纯净」（不带文件名），否则上传单文件 里的
                # _占比 查不到该阶段、进度会退化成 0~1 乱跳；
                # 文件名通过 _设置进度标签 注入，只在回调文案里追加。
                if 进度回调:
                    进度回调(阶段, _b + p * _r)

            self._设置进度标签(文件.name)
            try:
                结果 = self.上传单文件(文件, 子目录, 进度回调=子进度,
                                      允许秒传=允许秒传, 确保目录=True)
                结果列表.append(结果)
                if 单文件回调:
                    单文件回调(文件.name, 结果)
            except Exception as e:
                # ✅ 逐文件隔离：单个文件失败只记日志 + 回调，绝不中断整批
                logger.error(f"[调度] {文件} 上传失败：{type(e).__name__}: {e}")
                if 单文件回调:
                    单文件回调(文件.name, 上传结果(
                        文件路径=str(文件), 文件名=文件.name, 大小=0,
                        md5="", 原始={"errno": -1, "错误": str(e)}))
            finally:
                self._设置进度标签("")

            # 可选让位：给 Qt 事件循环/GC 一点喘息时间（对本地磁盘/网络吞吐
            # 影响可忽略）。诊断表明批量上传本身不崩，这一项主要是界面流畅度。
            if 批间隔秒 and i + 1 < len(文件们):
                time.sleep(批间隔秒)
        return 结果列表

    # ==================== 辅助 ====================

    def 取上传域名(self) -> str:
        """当前上传通道域名（动态解析，60s 缓存）。"""
        return self.域名解析器.取域名()
