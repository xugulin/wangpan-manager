# 夸克网盘适配器/核心/接口/路径解析.py
"""
路径解析（阶段二 2.10）

等价于 QuarkPan 的 `services/name_resolver.py`：把人类可读的路径
（如 `/文档/2026/报告.pdf`）解析成夸克的 `fid`。

规则：
  - 以 `/` 开头 → **从根目录**解析
  - 否则         → **相对当前目录**解析
  - `/` 或 `""`  → 根目录 `"0"`
  - 支持 `.`（当前）与 `..`（上级）

缓存：按目录缓存一次列表结果（`fid -> {名称: 条目}`），
      避免逐级解析时对同一目录重复请求。

⚠️ 夸克允许同名文件/目录，此时取**第一个**匹配项（与参考实现一致）。
"""
import logging
import time
from typing import Any, Optional

from ..网络.网络客户端 import 网络客户端, 接口错误
from .文件接口 import 文件接口

logger = logging.getLogger("夸克网盘.路径")


class 路径解析器:
    """路径 → fid 解析器（带目录缓存）"""

    def __init__(self, 文件接口实例: Optional[文件接口] = None,
                 网络: Optional[网络客户端] = None):
        if 文件接口实例 is None:
            if 网络 is None:
                raise ValueError("必须提供 文件接口实例 或 网络客户端")
            文件接口实例 = 文件接口(网络)
        self.文件 = 文件接口实例
        self._缓存: dict[str, dict[str, dict]] = {}

    # ---------- 缓存 ----------

    def 清空缓存(self, 目录ID: Optional[str] = None) -> None:
        if 目录ID is None:
            self._缓存.clear()
        else:
            self._缓存.pop(str(目录ID), None)

    def 命中缓存目录数(self) -> int:
        return len(self._缓存)

    def _列目录(self, 目录ID: str) -> dict[str, dict]:
        """取目录内容并按名称建索引（带缓存）

        ⚠️ `file/sort` 有秒级延迟，且**在有删除任务刚完成时可能瞬时返回空页**
        （2026-09-14 实测）。空结果重试一次再缓存，避免把「路径不存在」误报。
        """
        键 = str(目录ID or "0")
        if 键 not in self._缓存:
            索引 = self._取名字索引(键)
            if not 索引:
                time.sleep(1.5)
                索引 = self._取名字索引(键)
            self._缓存[键] = 索引
            logger.debug(f"[路径] 缓存目录 {键}，{len(索引)} 项")
        return self._缓存[键]

    def _取名字索引(self, 目录ID: str) -> dict[str, dict]:
        结果 = self.文件.取文件列表(目录ID, pageSize=200)
        索引: dict[str, dict] = {}
        for 条目 in 结果.get("list", []):
            名称 = str(条目.get("file_name") or "")
            if 名称 and 名称 not in 索引:
                索引[名称] = 条目
        return 索引

    # ---------- 解析 ----------

    def 解析(self, 路径: str, 当前目录ID: str = "0") -> tuple[str, bool]:
        """把路径解析成 (fid, 是否目录)

        :raises 接口错误: 路径中某一段不存在
        """
        原文 = (路径 or "").strip()
        if 原文 in ("", "/", "."):
            return str(当前目录ID or "0"), True

        绝对 = 原文.startswith("/")
        当前 = "0" if 绝对 else str(当前目录ID or "0")
        当前是否目录 = True
        当前路径 = "/" if 绝对 else "."

        for 段 in [s for s in 原文.split("/") if s]:
            if 段 == ".":
                continue
            if 段 == "..":
                当前 = str(当前目录ID or "0") if 绝对 else "0"
                当前路径 = "/" if 绝对 else "."
                continue

            索引 = self._列目录(当前)
            条目 = 索引.get(段)
            if 条目 is None:
                raise 接口错误(
                    f"路径不存在：{当前路径}/{段}（在 fid={当前} 下未找到）")
            当前 = str(条目.get("fid") or "")
            当前是否目录 = self.文件.是否目录(条目)
            当前路径 = f"{当前路径.rstrip('/')}/{段}"

            if not 当前是否目录:
                # 已经到文件，但路径还有后续段
                return 当前, False

        return 当前, 当前是否目录

    def 取目录ID(self, 路径: str, 当前目录ID: str = "0") -> str:
        """只要目录 fid（非目录则报错）"""
        fid, 是目录 = self.解析(路径, 当前目录ID)
        if not 是目录:
            raise 接口错误(f"不是目录：{路径}")
        return fid

    def 确保目录ID(self, 路径: str, 当前目录ID: str = "0") -> str:
        """解析目录；不存在则逐级创建（幂等）"""
        原文 = (路径 or "").strip()
        if 原文 in ("", "/", "."):
            return str(当前目录ID or "0")

        绝对 = 原文.startswith("/")
        当前 = "0" if 绝对 else str(当前目录ID or "0")
        for 段 in [s for s in 原文.split("/") if s and s != "."]:
            索引 = self._列目录(当前)
            条目 = 索引.get(段)
            if 条目 is not None and self.文件.是否目录(条目):
                当前 = str(条目.get("fid") or "")
                continue
            if 条目 is not None and not self.文件.是否目录(条目):
                raise 接口错误(f"路径中 '{段}' 是文件，不能作为目录")
            新建 = self.文件.创建文件夹(当前, 段)
            if not isinstance(新建, dict) or not 新建.get("fid"):
                raise 接口错误(f"创建目录 '{段}' 未返回 fid：{新建}")
            当前 = str(新建["fid"])
            self.清空缓存(当前)
        # 目录结构变了，清掉沿途缓存以免读到旧列表
        self.清空缓存()
        return 当前

    # ---------- 反向查询 ----------

    def 取真实名称(self, fid: str) -> Optional[str]:
        """由 fid 反查文件名（遍历根目录，限额 2000 项）"""
        目标 = str(fid)
        结果 = self.文件.取文件列表("0", pageSize=2000)
        for 条目 in 结果.get("list", []):
            if str(条目.get("fid")) == 目标:
                return str(条目.get("file_name") or "")
        return None

    def 列名称(self, 目录ID: str = "0") -> list[str]:
        """列出目录下的名称（走缓存）"""
        return list(self._列目录(目录ID).keys())


def 解析路径(网络: 网络客户端, 路径: str,
             当前目录ID: str = "0") -> tuple[str, bool]:
    """一次性解析的便捷函数（不带缓存复用）"""
    return 路径解析器(网络=网络).解析(路径, 当前目录ID)
