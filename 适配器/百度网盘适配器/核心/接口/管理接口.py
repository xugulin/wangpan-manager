# 百度网盘适配器/核心/接口/管理接口.py
"""
文件管理接口
作用：`/api/filemanager` 的四个操作 —— 删除 / 重命名 / 移动 / 复制

端点（HAR 实测，见 PLAN.md §6.2）
--------------------------------
    POST /api/filemanager?opera=<操作>&async=2&onnest=fail&bdstoken=...
         &clienttype=0&app_id=250528&web=1&dp-logid=...
    Content-Type: application/x-www-form-urlencoded
    body: filelist=<JSON>

    → { errno:0, info:[], taskid:<数字>, request_id }

⚠️ **`filelist` 的结构随 `opera` 变化**（实测确认，最容易踩的坑）：

    | opera  | filelist 结构 |
    |--------|---------------|
    | delete | `["/path/a", "/path/b"]`                        ← **纯字符串数组** |
    | rename | `[{"id":fs_id,"path":"/a","newname":"b"}]`      ← 带 id |
    | move   | `[{"path":"/a","dest":"/dir","newname":"a"}]`   ← **不带 id** |
    | copy   | `[{"path":"/a","dest":"/dir","newname":"a"}]`   ← 与 move 同构 |

其余实测细节：
  - `async=2` 异步，返回 `taskid`，需用 `任务接口` 轮询
  - 删除额外带 `newVerify=1`
  - `onnest=fail`（嵌套操作失败即报错）
"""

from __future__ import annotations

import json
import logging
from typing import Iterable

from ..网络.网络客户端 import 网络客户端

logger = logging.getLogger("百度网盘.管理")

# opera 取值（实测确认这 4 个）
OPERA_删除 = "delete"
OPERA_重命名 = "rename"
OPERA_移动 = "move"
OPERA_复制 = "copy"

# 各 opera 在 filelist 里允许的键
_字段_删除 = ("path",)
_字段_重命名 = ("id", "path", "newname")
_字段_移动复制 = ("path", "dest", "newname")


def _去空(条目: dict, 允许: tuple[str, ...]) -> dict:
    """只保留该 opera 允许的键，并丢弃空值。"""
    结果 = {}
    for k in 允许:
        v = 条目.get(k)
        if v not in (None, ""):
            结果[k] = v
    return 结果


class 管理接口:
    """文件管理（删除 / 重命名 / 移动 / 复制）。"""

    def __init__(self, 网络: 网络客户端):
        self.网络 = 网络

    # ---------------- 内部 ----------------

    def _提交(self, opera: str, filelist: list,
              额外参数: dict | None = None) -> dict:
        参数 = {"opera": opera, "async": "2", "onnest": "fail"}
        参数.update(额外参数 or {})
        数据 = self.网络.请求(
            "POST", "/api/filemanager",
            params=参数,
            表单数据={
                "filelist": json.dumps(filelist, ensure_ascii=False,
                                       separators=(",", ":"))
            },
            需要bdstoken=True,
        )
        logger.info(f"[管理] {opera} ✅ {len(filelist)} 项 "
                    f"taskid={数据.get('taskid')}")
        return 数据

    # ---------------- 删除 ----------------

    def 删除(self, 路径们: Iterable[str]) -> dict:
        """删除若干路径（进回收站）。

        ⚠️ `delete` 的 filelist 是**纯字符串数组**，不是对象数组。
        :return: 含 `taskid` 的响应
        """
        列表 = [str(p) for p in 路径们 if p]
        if not 列表:
            raise ValueError("路径列表不能为空")
        return self._提交(OPERA_删除, 列表, {"newVerify": "1"})

    # ---------------- 重命名 ----------------

    def 重命名(self, 条目们: Iterable[dict]) -> dict:
        """重命名。

        :param 条目们: 每项形如 `{"id": fs_id 或 "path": 原路径, "newname": "新名"}`
                       `newname` 只需**文件名**，不含路径。
        """
        列表 = [_去空(dict(e), _字段_重命名) for e in 条目们]
        for d in 列表:
            if not d.get("newname"):
                raise ValueError(f"重命名条目缺少 newname：{d}")
            if not d.get("id") and not d.get("path"):
                raise ValueError(f"重命名条目至少需要 id 或 path：{d}")
        if not 列表:
            raise ValueError("条目列表不能为空")
        return self._提交(OPERA_重命名, 列表)

    # ---------------- 移动 ----------------

    def 移动(self, 条目们: Iterable[dict]) -> dict:
        """移动到目标目录。

        :param 条目们: 每项形如 `{"path": 原路径, "dest": 目标目录, "newname": 文件名}`
                       ⚠️ `move` **不带 `id`**。
        """
        列表 = [_去空(dict(e), _字段_移动复制) for e in 条目们]
        for d in 列表:
            if not d.get("path") or not d.get("dest"):
                raise ValueError(f"移动条目需要 path 与 dest：{d}")
        if not 列表:
            raise ValueError("条目列表不能为空")
        return self._提交(OPERA_移动, 列表)

    # ---------------- 复制 ----------------

    def 复制(self, 条目们: Iterable[dict]) -> dict:
        """复制到目标目录（结构与 `移动` 完全一致）。"""
        列表 = [_去空(dict(e), _字段_移动复制) for e in 条目们]
        for d in 列表:
            if not d.get("path") or not d.get("dest"):
                raise ValueError(f"复制条目需要 path 与 dest：{d}")
        if not 列表:
            raise ValueError("条目列表不能为空")
        return self._提交(OPERA_复制, 列表)

    # ---------------- 新建文件夹 ----------------

    def 新建文件夹(self, 目标路径: str, 目标目录: str = "/") -> dict:
        """新建文件夹（复用 `/api/create`，`isdir=1`）。

        :param 目标路径: 新文件夹的完整路径，如 `/新建文件夹`
        :param 目标目录: 其父目录，如 `/`
        """
        路径 = 目标路径 if 目标路径.startswith("/") else "/" + 目标路径
        父 = (目标目录 or "/").rstrip("/") or "/"
        数据 = self.网络.请求(
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
        logger.info(f"[管理] 新建文件夹 ✅ {路径} fs_id={数据.get('fs_id')}")
        return 数据

    # ---------------- 便捷包装（单文件） ----------------

    def 删除单个(self, 路径: str) -> dict:
        return self.删除([路径])

    def 重命名单个(self, 原路径: str, 新名: str,
                  fs_id: int | str | None = None) -> dict:
        条目 = {"path": 原路径, "newname": 新名}
        if fs_id is not None:
            条目["id"] = fs_id
        return self.重命名([条目])

    def 移动单个(self, 原路径: str, 目标目录: str,
                 文件名: str | None = None) -> dict:
        名字 = 文件名 or 原路径.rstrip("/").rsplit("/", 1)[-1]
        return self.移动([{"path": 原路径, "dest": 目标目录,
                          "newname": 名字}])

    def 复制单个(self, 原路径: str, 目标目录: str,
                 文件名: str | None = None) -> dict:
        名字 = 文件名 or 原路径.rstrip("/").rsplit("/", 1)[-1]
        return self.复制([{"path": 原路径, "dest": 目标目录,
                          "newname": 名字}])
