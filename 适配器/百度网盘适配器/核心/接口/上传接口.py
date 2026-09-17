# 百度网盘适配器/核心/接口/上传接口.py
"""
上传接口
作用：面向界面层的薄封装，实际编排逻辑在 `核心/上传/上传总调度.py`

    UI（文件浏览页 / 公共线程）
        └─ 上传接口.上传文件() / 上传文件夹()
              └─ 上传总调度.上传单文件()
                    ├─ 哈希计算器  算 整文件MD5 / 首256K MD5 / 各分片MD5
                    ├─ 秒传服务    POST /api/rapidupload（errno:404 = 未命中，正常回落）
                    ├─ 预创建      POST /api/precreate
                    ├─ 分片上传    POST <locateupload给的server[0]>/superfile2
                    └─ 创建文件    POST /api/create

⚠️ 三个实测坑（详见 PLAN.md §5.2，已在总调度中规避）
    1. `create.block_list` = **superfile2 返回的 md5 列表**，
       不是 precreate 响应里的 `[0,1,2…]` 占位序号。
    2. 单分片文件的 `block_list[0]` = `md5(整文件)`，不是 `md5(首256KB)`。
    3. 上传域名动态，必须走 `locateupload`，**不要硬编码 `bddwd-cm01`**。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

from ..网络.网络客户端 import 网络客户端
from ..上传.上传总调度 import 上传总调度, 上传结果

logger = logging.getLogger("百度网盘.上传")

# 复用总调度的阶段常量，便于界面统一显示
阶段_算哈希 = "计算哈希"
阶段_秒传 = "秒传检查"
阶段_预创建 = "预创建"
阶段_上传 = "上传分片"
阶段_创建 = "创建文件"


class 上传接口:
    """上传对外入口。"""

    def __init__(self, 网络: 网络客户端):
        self.网络 = 网络
        self.调度 = 上传总调度(网络)

    # ==================== 单文件 ====================

    def 上传文件(
        self,
        本地路径: str | Path,
        父目录ID: str = "/",
        进度回调: Callable[[str, float], None] | None = None,
        允许秒传: bool = True,
        目标目录: str | None = None,
        确保目录: bool = False,
    ) -> dict:
        """上传单个文件。

        :param 父目录ID: 目标目录路径（保留旧参数名以兼容界面层调用）
        :param 进度回调: `回调(阶段名, 0~1)`
        :param 确保目录: 目标目录不存在时自动逐级创建（默认不建，
            保持「传错路径应报错」的语义；递归上传文件夹时内部恒为 True）
        :return: 兼容旧返回的 dict（含 `fs_id` / `md5` / `是否秒传` / `path` / `size`）
        """
        目录 = 目标目录 if 目标目录 is not None else 父目录ID
        结果: 上传结果 = self.调度.上传单文件(
            本地路径, 目录 or "/", 进度回调=进度回调,
            允许秒传=允许秒传, 确保目录=确保目录)
        return {
            "errno": 0 if 结果.原始.get("errno") in (0, None) else 结果.原始.get("errno"),
            "fs_id": 结果.fs_id,
            "md5": 结果.md5,
            "path": 结果.文件路径,
            "server_filename": 结果.文件名,
            "size": 结果.大小,
            "是否秒传": 结果.是否秒传,
        }

    # ==================== 文件夹 ====================

    def 上传文件夹(
        self,
        本地目录: str | Path,
        父目录ID: str = "/",
        进度回调: Callable[[str, float], None] | None = None,
        单文件回调: Callable[[str, dict], None] | None = None,
    ) -> list[dict]:
        """递归上传文件夹，返回每个成功文件的 dict 列表。

        远端目录结构由总调度逐级创建（实测 precreate 不会自动建父目录）。
        """

        def 包装回调(文件名: str, 结果: 上传结果):
            if not 单文件回调:
                return
            单文件回调(文件名, {
                "errno": 0 if 结果.原始.get("errno") in (0, None)
                         else 结果.原始.get("errno"),
                "fs_id": 结果.fs_id,
                "md5": 结果.md5,
                "path": 结果.文件路径,
                "server_filename": 结果.文件名,
                "size": 结果.大小,
                "是否秒传": 结果.是否秒传,
            })

        结果们 = self.调度.上传文件夹(
            本地目录, 父目录ID or "/",
            进度回调=进度回调, 单文件回调=包装回调)
        return [
            {
                "errno": 0 if r.原始.get("errno") in (0, None) else r.原始.get("errno"),
                "fs_id": r.fs_id,
                "md5": r.md5,
                "path": r.文件路径,
                "server_filename": r.文件名,
                "size": r.大小,
                "是否秒传": r.是否秒传,
            }
            for r in 结果们
        ]

    # ==================== 底层直通（供调试/测试） ====================

    def 预创建(self, 目标路径: str, 分片md5列表: list[str],
              目标目录: str = "/") -> dict:
        return self.调度.预创建(目标路径, 分片md5列表, 目标目录)

    def 创建文件(self, 目标路径: str, 大小: int, uploadid: str,
                服务端md5列表: list[str], 目标目录: str = "/") -> dict:
        return self.调度.创建文件(目标路径, 大小, uploadid,
                                 服务端md5列表, 目标目录)

    def 取上传域名(self) -> str:
        """当前上传通道域名（动态解析，见 §5.2.2）。"""
        return self.调度.取上传域名()

    def 确保远端目录(self, 目标目录: str) -> bool:
        """确保远端目录存在（逐级创建），供界面/脚本预建目录用。"""
        return self.调度.确保远端目录(目标目录)

    def 清空目录缓存(self) -> None:
        """清掉「已建目录」缓存（目录被外部删除后需要重查时调）。"""
        self.调度.清空目录缓存()
