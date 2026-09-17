# 夸克网盘适配器/核心/接口/文件接口.py
"""
文件接口（阶段二 2.5）—— 全部改为夸克端点

端点对照（原适配器 → 夸克）：

    列表    POST /userres/v1/file/get_file_list   → GET  file/sort
    建目录  POST /userres/v1/file/create_dir      → POST file
    删除    POST /userres/v1/file/delete_file     → POST file/delete
    重命名  —                                     → POST file/rename
    移动    —                                     → POST file/move
    搜索    —                                     → GET  file/search
    目录树  —                                     → GET  file/tree
    文件信息—                                     → GET  file?fids=
    下载    POST /userres/v1/get_res_download_url → POST file/download

⚠️ 字段与分页约定（2026-09-14 实测）：

| 概念 | 夸克字段 |
|---|---|
| 父目录 / 文件 ID | `pdir_fid` / `fid` |
| 文件名 / 大小 | `file_name` / `size` |
| 是否目录 | `dir`(bool) 或 `file_type == 0` |
| 分页 | `_page`(从 **1** 开始) / `_size` / `_sort="字段:asc"` |
| 总数 | `metadata._total`（**不在 data 里**） |
| 列表 | `data.list` |

⚠️ `POST file/delete` 必须带 `fid_list` + `current_dir_fid`；
   只给 `fid_list` 会报 `14001 Bad Parameter: [current_dir_fid,filelist 不能同时为空]`。
"""
import logging
from pathlib import Path
from typing import Any, Optional

from ..网络.网络客户端 import 网络客户端

logger = logging.getLogger("夸克网盘.文件")

# Quark `file_type` 中 0 表示文件夹
文件类型_目录 = 0

默认排序 = "file_name:asc"
默认每页 = 50


def 解析列表响应(响应: Any) -> dict:
    """把 file/sort 的响应统一成 {"list": [...], "total": N, "metadata": {...}}

    ⚠️ 总数在 `metadata._total`，不在 `data` 里。
    """
    if not isinstance(响应, dict):
        return {"list": [], "total": 0, "metadata": {}}
    数据 = 响应.get("data") or {}
    元数据 = 响应.get("metadata") or {}
    列表 = 数据.get("list") or []
    if not isinstance(列表, list):
        列表 = []
    总数 = 元数据.get("_total")
    if 总数 is None:
        总数 = 数据.get("total") or len(列表)
    return {"list": 列表, "total": int(总数 or 0), "metadata": 元数据}


class 文件接口:
    """夸克文件相关接口"""

    def __init__(self, 网络: 网络客户端):
        self.网络 = 网络

    # ==================== 列表 / 搜索 / 目录树 ====================

    def 取文件列表(
        self,
        parentId: str = "0",
        page: int = 1,
        pageSize: int = 默认每页,
        排序字段: str = "file_name",
        排序方向: str = "asc",
        取总数: bool = True,
    ) -> dict:
        """GET file/sort —— 列目录

        :param parentId: 父目录 fid，"0" 表示根目录
        :param page: 页码，**从 1 开始**（传 0 会被当作 1）
        :return: {"list": [...原始条目...], "total": N, "metadata": {...}}
        """
        页码 = max(1, int(page or 1))
        响应 = self.网络.请求(
            "GET", "file/sort",
            params={
                "pdir_fid": str(parentId or "0"),
                "_page": 页码,
                "_size": int(pageSize),
                "_sort": f"{排序字段}:{排序方向}",
                "_fetch_total": 1 if 取总数 else 0,
            })
        return 解析列表响应(响应)

    def 搜索文件(self, 关键字: str, page: int = 1,
                 pageSize: int = 默认每页) -> dict:
        """GET file/search

        ⚠️ 关键词参数名是 **`q`**，不是 `keyword`（用 `keyword` 会被静默忽略，
        服务端返回一堆无关结果 —— 2026-09-14 实测踩过）。
        """
        响应 = self.网络.请求(
            "GET", "file/search",
            params={
                "q": 关键字 or "",
                "_page": max(1, int(page or 1)),
                "_size": int(pageSize),
                "_fetch_total": 1,
                "_is_hl": 1,
            })
        return 解析列表响应(响应)

    def 取文件夹树(self, 父目录ID: str = "0", 最大深度: int = 3) -> dict:
        """GET file/tree（深度参数名是 `max_depth`）"""
        响应 = self.网络.请求(
            "GET", "file/tree",
            params={"pdir_fid": str(父目录ID or "0"),
                    "max_depth": int(最大深度)})
        return 响应.get("data", 响应) if isinstance(响应, dict) else {}

    def 取文件信息(self, fileIds: str | list[str]) -> dict:
        """GET file?fids= —— 批量取文件详情

        :return: {"list": [...]}
        """
        标识 = fileIds if isinstance(fileIds, str) else ",".join(map(str, fileIds))
        响应 = self.网络.请求("GET", "file", params={"fids": 标识})
        return 解析列表响应(响应)

    # ==================== 目录 ====================

    def 创建文件夹(self, parentId: str, dirName: str,
                   failIfNameExist: bool | None = None) -> dict:
        """POST file —— 创建文件夹

        响应 data 含 `fid`（目录 ID），上传子文件时作为 pdir_fid。

        :param failIfNameExist: 夸克无此字段，保留入参只为兼容旧调用方
        """
        _ = failIfNameExist
        响应 = self.网络.请求(
            "POST", "file",
            json数据={
                "pdir_fid": str(parentId or "0"),
                "file_name": dirName,
                "dir_init_lock": False,
                "dir_path": "",
            })
        return 响应.get("data", 响应)

    # 兼容旧名称
    创建目录 = 创建文件夹

    def 确保目录(self, 父目录ID: str, 名称: str) -> str:
        """幂等创建：已存在同名目录则直接返回其 fid（不新建）"""
        现有 = self.取文件列表(父目录ID, pageSize=200, 取总数=False)
        for 条目 in 现有.get("list", []):
            if 条目.get("file_name") == 名称 and self.是否目录(条目):
                return str(条目.get("fid") or "")
        数据 = self.创建文件夹(父目录ID, 名称)
        return str((数据 or {}).get("fid") or "")

    # ==================== 删除 / 重命名 / 移动 ====================

    def 删除文件(self, fileIds: str | list[str],
                 当前目录ID: str | None = None, 等待: bool = False,
                 最长等待秒: float = 60.0) -> dict:
        """POST file/delete —— 删除指定文件/目录（移入回收站）

        ⚠️⚠️ **绝不要传 `current_dir_fid`！**（2026-09-14 血泪事故）

        服务端把删除分成**两种互斥模式**：
            模式 A：`{"action_type":2, "filelist":[fid...], "exclude_fids":[]}`
                    → 删除**列表里的那些**文件（我们要的就是这个）
            模式 B：`{"current_dir_fid": <fid>}`
                    → **删除整个该目录**（会连同其全部内容递归删除！）

        两者同时出现会报 `14001 不能同时存在值`；而
        「`fid_list` + `current_dir_fid`」会被服务端按**模式 B** 处理 ——
        传 `current_dir_fid="0"` 就等于**清空根目录**。
        本适配器早期版本正是这样写的，导致根目录被清空（可从回收站恢复）。

        :param 当前目录ID: **已废弃，仅为兼容旧调用方保留，会被忽略**
        """
        from .任务接口 import 任务接口

        if 当前目录ID not in (None, "", "0"):
            logger.warning(
                "[文件] 删除文件() 的 current_dir_fid 参数已废弃并被忽略——"
                "传它会导致整目录递归删除（详见本方法 docstring）")

        标识 = [fileIds] if isinstance(fileIds, str) else list(fileIds)
        标识 = [str(x) for x in 标识 if x]
        if not 标识:
            raise 接口错误("删除失败：fileIds 为空")

        响应 = self.网络.请求(
            "POST", "file/delete",
            json数据={
                "action_type": 2,          # 2 = 删除
                "filelist": 标识,
                "exclude_fids": [],
            })
        数据 = 响应.get("data", 响应) if isinstance(响应, dict) else {}
        taskId = str((数据 or {}).get("task_id") or "")
        logger.info(f"[文件] 已提交删除 {len(标识)} 项 task_id={taskId}")
        if 等待 and taskId:
            任务接口(self.网络).等待任务完成(
                taskId, 最长等待秒=最长等待秒, 描述="删除")
        return 数据

    # ==================== 回收站 ====================

    def 取回收站列表(self, page: int = 1, pageSize: int = 50) -> dict:
        """GET file/recycle/list —— 回收站条目（含 record_id，用于恢复/彻底删除）"""
        响应 = self.网络.请求(
            "GET", "file/recycle/list",
            params={"_page": max(1, int(page or 1)), "_size": int(pageSize)})
        return 解析列表响应(响应)

    def 恢复回收站(self, recordIds: str | list[str]) -> dict:
        """POST file/recycle/recover —— 从回收站恢复

        ⚠️ 端点名是 `recover` 而不是 `restore`（后者 404），
        且必须带 `select_mode`，否则报 `14001 select_mode should not be null`。
        """
        标识 = [recordIds] if isinstance(recordIds, str) else list(recordIds)
        响应 = self.网络.请求(
            "POST", "file/recycle/recover",
            json数据={"select_mode": 2,
                     "record_list": [str(x) for x in 标识]})
        return 响应.get("data", 响应) if isinstance(响应, dict) else {}

    def 彻底删除回收站(self, recordIds: str | list[str]) -> dict:
        """POST file/recycle/remove —— 从回收站彻底删除（不可恢复）"""
        标识 = [recordIds] if isinstance(recordIds, str) else list(recordIds)
        响应 = self.网络.请求(
            "POST", "file/recycle/remove",
            json数据={"select_mode": 2,
                     "record_list": [str(x) for x in 标识]})
        return 响应.get("data", 响应) if isinstance(响应, dict) else {}

    def 重命名(self, fileId: str, 新名称: str,
               等待: bool = False) -> dict:
        """POST file/rename"""
        from .任务接口 import 任务接口

        响应 = self.网络.请求(
            "POST", "file/rename",
            json数据={"fid": str(fileId), "file_name": 新名称})
        数据 = 响应.get("data", 响应) if isinstance(响应, dict) else {}
        taskId = str((数据 or {}).get("task_id") or "")
        if 等待 and taskId:
            任务接口(self.网络).等待任务完成(taskId, 描述="重命名")
        return 数据

    def 移动文件(self, fileIds: str | list[str], 目标目录ID: str,
                 等待: bool = False, 最长等待秒: float = 60.0) -> dict:
        """POST file/move"""
        from .任务接口 import 任务接口

        标识 = [fileIds] if isinstance(fileIds, str) else list(fileIds)
        响应 = self.网络.请求(
            "POST", "file/move",
            json数据={
                "filelist": [str(x) for x in 标识],
                "to_pdir_fid": str(目标目录ID or "0"),
                "exclude_fids": [],
            })
        数据 = 响应.get("data", 响应) if isinstance(响应, dict) else {}
        taskId = str((数据 or {}).get("task_id") or "")
        if 等待 and taskId:
            任务接口(self.网络).等待任务完成(
                taskId, 最长等待秒=最长等待秒, 描述="移动")
        return 数据

    # ==================== 下载地址 ====================

    def 取下载地址列表(self, fileIds: str | list[str]) -> list[dict]:
        """POST file/download —— 批量取直链

        :return: 原始列表，每项含 `fid` / `file_name` / `size` / `download_url`
        """
        标识 = [fileIds] if isinstance(fileIds, str) else list(fileIds)
        响应 = self.网络.请求(
            "POST", "file/download",
            json数据={"fids": [str(x) for x in 标识]})
        数据 = 响应.get("data", 响应) if isinstance(响应, dict) else []
        if isinstance(数据, dict):
            数据 = 数据.get("list") or []
        return 数据 if isinstance(数据, list) else []

    def 取下载地址(self, fileId: str) -> dict:
        """单个文件的下载信息

        :return: {"fid","file_name","size","download_url"}
        :raises 接口错误: 服务端未返回 download_url
        """
        from ..网络.网络客户端 import 接口错误

        列表 = self.取下载地址列表(fileId)
        for 项 in 列表:
            if str(项.get("fid")) == str(fileId) and 项.get("download_url"):
                return 项
        if 列表 and 列表[0].get("download_url"):
            return 列表[0]
        raise 接口错误(
            f"未取到下载直链（fid={fileId}）：{列表}")

    # ==================== 条目辅助 ====================

    @staticmethod
    def 是否目录(条目: dict) -> bool:
        """夸克用 `dir`(bool) 或 `file_type == 0` 表示目录"""
        if not isinstance(条目, dict):
            return False
        if 条目.get("dir") is True:
            return True
        return 条目.get("file_type") == 文件类型_目录

    @staticmethod
    def 规范条目(条目: dict) -> dict:
        """在原始条目基础上补充派生字段（**不改动/不覆盖原始键**）

        供界面层消费，派生键用中文以免与夸克字段混淆：
            `是否目录` / `名称` / `大小` / `修改时间戳`
        """
        if not isinstance(条目, dict):
            return {}
        return {
            **条目,
            "是否目录": 文件接口.是否目录(条目),
            "名称": 条目.get("file_name", ""),
            "大小": 条目.get("size", 0) or 0,
            "修改时间戳": (条目.get("updated_at")
                         or 条目.get("created_at")),
        }

    @staticmethod
    def 取缩略图URL(条目: dict) -> Optional[str]:
        """夸克条目里的缩略图字段（`thumbnail` / `preview_url`）"""
        if not isinstance(条目, dict):
            return None
        for 键 in ("thumbnail", "preview_url", "thumb_url"):
            值 = 条目.get(键)
            if isinstance(值, str) and 值:
                return 值.replace("\\u0026", "&")
        return None
