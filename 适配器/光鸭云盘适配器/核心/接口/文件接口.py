# 光鸭云盘适配器/核心/接口/文件接口.py
"""
文件相关接口
参考真实抓包（2026-09-13）：
  - 所有接口 POST + JSON body
  - 下载：POST /userres/v1/get_res_download_url  请求体 {"fileId": "..."}
  - 下载响应字段：signedURL / urlDuration / speedupSignature / requestId

  - 回收站（HAR 确认 2026-09-13）：
      列表：POST /userres/v1/file/get_file_list
            body: {"parentId":"","pageSize":100,"dirType":4,"orderBy":12,"sortType":1}
            注意 dirType=4 表示回收站
      清空：POST /userres/v1/file/clear_recycle_bin
            body: 空
            响应：{"msg":"success","data":{"taskId":"..."}}
            之后通过 /userres/v1/get_task_status 轮询 taskId

  - 创建文件夹（HAR 确认 2026-09-13）：
      POST /userres/v1/file/create_dir
      body: {"parentId":"1945546119831085105",
             "dirName":"新建文件夹-20260913185516014"}
      注意：
        1) 使用 dirName 字段，不是 fileName！
        2) HAR 里没有 failIfNameExist 字段，说明可省略（可选）

  - 用户操作 / 上传记录（HAR 确认 2026-09-13）：
      POST /userres/v1/get_user_action
      body: {"pageSize":N,"cursor":"","excludeFileTypes":[2]}   查上传文件
      body: {"pageSize":N,"cursor":"","fileTypes":[2]}           查目录
      响应 data.list 是按 collectionId 分组的批次，
      每个批次内含 actionDetails[]，即真实的上传记录条目。
      字段：fileId / fileName / thumbnail / fileSize / utime
            parentId / parentName / fileType / resType / ext
      actionType: 3 = 上传

  - 上传任务统计（HAR 确认）：
      POST /userres/v1/query_uploading_tasks_stat
      响应：{"msg":"success","data":{"totalCount":328,"totalSize":71819875}}

  - 列表响应（HAR 确认）：
      data.list[i] 字段：
        fileId, fileName, fileSize, resType, dirType,
        ctime, utime, gcid, mineType, fileType, ext,
        thumbnail (图片文件特有，128x128 缩略图 URL),
        auditStatus, depth
"""

import logging
from typing import Iterator

from ..网络.网络客户端 import 网络客户端

logger = logging.getLogger("光鸭云盘.文件")

# 回收站专用参数（HAR 确认）
回收站_dirType = 4
回收站_orderBy = 12
回收站_sortType = 1

# 用户操作类型（HAR 观察）
操作类型_上传 = 3


class 文件接口:
    """文件接口"""

    def __init__(self, 网络: 网络客户端):
        self.网络 = 网络

    def _带客户端标识(self, 请求体: dict | None = None) -> dict:
        请求体 = dict(请求体 or {})
        请求体.setdefault("clientId", self.网络.客户端标识)
        return 请求体

    # ==================== 列目录 ====================

    def 取文件列表(
        self,
        parentId: str = "",
        page: int = 0,
        pageSize: int = 50,
        orderBy: int = 3,
        sortType: int = 1,
        dirType: int | None = None,
    ) -> dict:
        """POST /userres/v1/file/get_file_list

        HAR 真实请求（2026-09-13 刷新按钮）：
            {"pageSize":50,"orderBy":3,"sortType":1,"parentId":"","page":0}
        """
        请求体: dict = {
            "pageSize": pageSize,
            "orderBy": orderBy,
            "sortType": sortType,
            "parentId": parentId,
            "page": page,
        }
        if dirType is not None:
            请求体["dirType"] = dirType

        响应数据 = self.网络.请求(
            "POST",
            "/userres/v1/file/get_file_list",
            json数据=self._带客户端标识(请求体),
        )
        return 响应数据.get("data", 响应数据)

    # ==================== 文件夹 / 删除 ====================

    def 创建文件夹(
        self,
        parentId: str,
        dirName: str,
        failIfNameExist: bool | None = None,
    ) -> dict:
        """POST /userres/v1/file/create_dir

        HAR 真实 body（2026-09-13 新建文件夹）：
            {"parentId":"1945546119831085105",
             "dirName":"新建文件夹-20260913185516014"}
        注意：**没有 failIfNameExist 字段**。
              该字段为可选，如需开启重名检测再传 true。

        响应 data：
            {"fileId","fileName","parentId","depth",
             "dirType":1,"resType":2,"fullParentIds","ctime","utime"}
        """
        请求体: dict = {
            "parentId": parentId or "",
            "dirName": dirName,
        }
        if failIfNameExist is not None:
            请求体["failIfNameExist"] = bool(failIfNameExist)

        响应数据 = self.网络.请求(
            "POST",
            "/userres/v1/file/create_dir",
            json数据=请求体,
        )
        return 响应数据.get("data", 响应数据)

    # 兼容旧名称
    def 创建目录(
        self,
        parentId: str,
        dirName: str,
        failIfNameExist: bool | None = None,
    ) -> dict:
        """创建文件夹的别名（与上传接口保持一致的命名）"""
        return self.创建文件夹(parentId, dirName, failIfNameExist)

    def 删除文件(self, fileIds: list[str]) -> dict:
        """POST /userres/v1/file/delete_file（移入回收站）"""
        响应数据 = self.网络.请求(
            "POST",
            "/userres/v1/file/delete_file",
            json数据=self._带客户端标识({"fileIds": fileIds}),
        )
        return 响应数据.get("data", 响应数据)

    # ==================== 下载 ====================

    def 取下载地址(self, fileId: str) -> dict:
        """POST /userres/v1/get_res_download_url

        抓包确认（2026-09-13）：
          请求体：{"fileId": "1946176957014851618"}
          响应字段：signedURL / urlDuration / speedupSignature / requestId
        """
        响应数据 = self.网络.请求(
            "POST",
            "/userres/v1/get_res_download_url",
            json数据={"fileId": fileId},
        )
        return 响应数据.get("data", 响应数据)

    # ==================== 用户行为 / 上传记录 ====================

    def 取用户操作(
        self,
        pageSize: int = 50,
        cursor: str = "",
        fileTypes: list[int] | None = None,
        excludeFileTypes: list[int] | None = None,
    ) -> dict:
        """POST /userres/v1/get_user_action"""
        请求体: dict = {"pageSize": pageSize, "cursor": cursor or ""}
        if fileTypes is not None:
            请求体["fileTypes"] = fileTypes
        if excludeFileTypes is not None:
            请求体["excludeFileTypes"] = excludeFileTypes

        响应数据 = self.网络.请求(
            "POST",
            "/userres/v1/get_user_action",
            json数据=self._带客户端标识(请求体),
        )
        return 响应数据.get("data", 响应数据)

    def 取上传记录(
        self,
        pageSize: int = 20,
        cursor: str = "",
    ) -> dict:
        """便捷方法：取上传记录（排除目录）"""
        return self.取用户操作(
            pageSize=pageSize, cursor=cursor,
            excludeFileTypes=[2],
        )

    def 迭代上传记录(
        self,
        每页: int = 50,
        最多页: int = 10,
    ) -> Iterator[dict]:
        """迭代所有上传记录（扁平化 actionDetails）"""
        游标 = ""
        for _ in range(最多页):
            数据 = self.取上传记录(pageSize=每页, cursor=游标)
            列表 = 数据.get("list", []) if isinstance(数据, dict) else []
            for 批次 in 列表:
                for 详情 in (批次.get("actionDetails") or []):
                    详情 = dict(详情)
                    详情["_批次ID"] = 批次.get("collectionId")
                    详情["_批次时间"] = 批次.get("ctime")
                    详情["_批次总数"] = 批次.get("totalCount")
                    详情["_操作类型"] = 批次.get("actionType")
                    yield 详情
            if not 数据.get("hasMore"):
                break
            游标 = 数据.get("cursor", "")
            if not 游标:
                break

    def 取上传任务统计(self) -> dict:
        """POST /userres/v1/query_uploading_tasks_stat"""
        响应数据 = self.网络.请求(
            "POST",
            "/userres/v1/query_uploading_tasks_stat",
            json数据=self._带客户端标识({}),
        )
        return 响应数据.get("data", 响应数据)

    # ==================== 回收站（HAR 修复） ====================

    def 取回收站列表(
        self,
        pageSize: int = 100,
        page: int = 0,
        orderBy: int = 回收站_orderBy,
        sortType: int = 回收站_sortType,
        dirType: int = 回收站_dirType,
    ) -> dict:
        """POST /userres/v1/file/get_file_list（dirType=4 即回收站）"""
        请求体 = {
            "parentId": "",
            "pageSize": pageSize,
            "dirType": dirType,
            "orderBy": orderBy,
            "sortType": sortType,
        }
        if page > 0:
            请求体["page"] = page

        响应数据 = self.网络.请求(
            "POST",
            "/userres/v1/file/get_file_list",
            json数据=self._带客户端标识(请求体),
        )
        return 响应数据.get("data", 响应数据)

    def 清空回收站(self) -> dict:
        """POST /userres/v1/file/clear_recycle_bin（不带 body）"""
        响应数据 = self.网络.请求(
            "POST",
            "/userres/v1/file/clear_recycle_bin",
            json数据=None,
        )
        return (
            响应数据.get("data", 响应数据)
            if isinstance(响应数据, dict)
            else 响应数据
        )

    # ==================== 缩略图 ====================

    def 取缩略图URL(self, 条目: dict) -> str | None:
        """从文件列表条目中提取缩略图 URL（HAR 确认字段名 thumbnail）"""
        if not isinstance(条目, dict):
            return None
        缩略图 = 条目.get("thumbnail")
        if isinstance(缩略图, str) and 缩略图:
            # HAR 中 thumbnail 含 \u0026 转义，需还原为 &
            return 缩略图.replace("\\u0026", "&")
        return None