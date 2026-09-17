# 光鸭云盘适配器/核心/接口/云添加接口.py
"""
云添加（离线下载）接口
参考深度分析文档（2026-09-13）：

流程：
  直链：
    POST /cloudcollection/v1/resolve_res      解析元信息
    POST /userres/v1/file/create_dir          幂等创建「来自：云添加」目录
    POST /cloudcollection/v1/create_task      创建异步任务
    POST /cloudcollection/v1/list_task        轮询任务状态
  种子：
    POST /cloudcollection/v1/resolve_torrent  上传 .torrent（multipart）解析 infoHash
    后续与直链相同（用 magnet 创建任务，附 newName）

关键业务码：
  159 = 目录已存在（携带已有 fileId，前端复用）

任务状态：
  0 = 等待中
  1 = 下载中
  2 = 已完成（有 fileId）
  3 = 失败
  4 = 暂停
  5 = 已删除
"""
import logging
import time
from pathlib import Path
from typing import Iterable, Optional

from ..网络.网络客户端 import 网络客户端

logger = logging.getLogger("光鸭云盘.云添加")

# 与光鸭前端保持一致的目录名（含全角冒号「：」）
默认目录名 = "来自：云添加"

# 目录已存在业务码
目录已存在码 = 159

# 任务状态
任务状态映射 = {
    0: "等待中",
    1: "下载中",
    2: "已完成",
    3: "失败",
    4: "暂停",
    5: "已删除",
}

# 进行中状态 / 完成状态
进行中状态 = [0, 1, 3, 4]
完成状态 = [2, 5]
全部状态 = [0, 1, 2, 3, 4, 5]


class 云添加接口:
    """云添加接口"""

    def __init__(self, 网络: 网络客户端):
        self.网络 = 网络

    # ==================== 解析 ====================

    def 解析直链(self, url: str) -> dict:
        """POST /cloudcollection/v1/resolve_res

        响应 data：
            {"urlResInfo":{"url","fileName","fileSize"}, "url":"..."}
        返回 urlResInfo（含 fileName / fileSize / url）。
        """
        响应 = self.网络.请求(
            "POST",
            "/cloudcollection/v1/resolve_res",
            json数据={"url": url},
        )
        数据 = 响应.get("data", 响应) if isinstance(响应, dict) else 响应
        if not isinstance(数据, dict):
            raise RuntimeError(f"解析直链响应异常：{响应}")
        信息 = 数据.get("urlResInfo")
        if not isinstance(信息, dict) or not 信息.get("fileName"):
            # 兼容 data 直接携带 fileName 的场景
            信息 = {
                "url": 数据.get("url", url),
                "fileName": 数据.get("fileName", ""),
                "fileSize": 数据.get("fileSize", 0),
            }
        if not 信息.get("fileName"):
            raise RuntimeError(f"解析直链失败，未获取 fileName：{响应}")
        return 信息

    def 解析种子(self, 本地路径: str | Path) -> dict:
        """POST /cloudcollection/v1/resolve_torrent（multipart/form-data）

        表单字段名：torrent
        响应 data：
            {"resType": 2,
             "btResInfo": {"infoHash","fileName","fileSize"}}
        返回 btResInfo。
        """
        路径 = Path(本地路径)
        if not 路径.is_file():
            raise FileNotFoundError(f"种子文件不存在：{本地路径}")

        响应 = self.网络.表单文件请求(
            "/cloudcollection/v1/resolve_torrent",
            文件字段名="torrent",
            文件路径=路径,
        )
        数据 = 响应.get("data", 响应) if isinstance(响应, dict) else 响应
        if not isinstance(数据, dict):
            raise RuntimeError(f"解析种子响应异常：{响应}")
        信息 = 数据.get("btResInfo")
        if not isinstance(信息, dict) or not 信息.get("infoHash"):
            raise RuntimeError(f"解析种子失败，未获取 infoHash：{响应}")
        return 信息

    # ==================== 默认目录（幂等） ====================

    def 确保默认目录(self, parentId: str = "") -> str:
        """幂等创建「来自：云添加」目录

        - 目录不存在 → 创建成功，返回新 fileId
        - 目录已存在 → 服务端返回 code=159 + data.fileId，复用

        :param parentId: 父目录 ID，""=根目录
        :return: 目录 fileId
        """
        响应 = self.网络.请求(
            "POST",
            "/userres/v1/file/create_dir",
            json数据={
                "dirName": 默认目录名,
                "parentId": parentId or "",
                "failIfNameExist": True,
            },
            # 159 属于预期内（目录已存在），不当作错误
            允许业务码=(目录已存在码,),
        )
        数据 = 响应.get("data", 响应) if isinstance(响应, dict) else 响应
        if not isinstance(数据, dict) or "fileId" not in 数据:
            raise RuntimeError(f"确保默认目录失败：{响应}")
        code = 响应.get("code") if isinstance(响应, dict) else None
        if code == 目录已存在码:
            logger.info(
                f"[云添加] 默认目录已存在，复用 fileId={数据['fileId']}")
        else:
            logger.info(f"[云添加] 默认目录已创建 fileId={数据['fileId']}")
        return str(数据["fileId"])

    # ==================== 创建任务 ====================

    def 创建直链任务(self, url: str, parentId: str) -> str:
        """POST /cloudcollection/v1/create_task（直链）→ taskId"""
        响应 = self.网络.请求(
            "POST",
            "/cloudcollection/v1/create_task",
            json数据={"url": url, "parentId": parentId},
        )
        数据 = 响应.get("data", 响应) if isinstance(响应, dict) else 响应
        if not isinstance(数据, dict) or "taskId" not in 数据:
            raise RuntimeError(f"创建直链任务失败：{响应}")
        return str(数据["taskId"])

    def 创建BT任务(
        self,
        infoHash: str,
        parentId: str,
        newName: str = "",
    ) -> str:
        """POST /cloudcollection/v1/create_task（BT，用 magnet 链接）"""
        magnet = f"magnet:?xt=urn:btih:{infoHash}"
        请求体: dict = {"url": magnet, "parentId": parentId}
        if newName:
            请求体["newName"] = newName
        响应 = self.网络.请求(
            "POST",
            "/cloudcollection/v1/create_task",
            json数据=请求体,
        )
        数据 = 响应.get("data", 响应) if isinstance(响应, dict) else 响应
        if not isinstance(数据, dict) or "taskId" not in 数据:
            raise RuntimeError(f"创建 BT 任务失败：{响应}")
        return str(数据["taskId"])

    # ==================== 查询任务 ====================

    def 查询任务(
        self,
        taskIds: Optional[Iterable[str]] = None,
        status: Optional[Iterable[int]] = None,
        pageSize: int = 100,
        cursor: str = "",
    ) -> dict:
        """POST /cloudcollection/v1/list_task

        三种调用方式（与 HAR 一致）：
          - taskIds=[...]        精确查询
          - status=[0,1,3,4]     进行中任务
          - status=[2,5]         已完成/其它
        响应 data：{"statusCounts","list","cursor","total"}
        """
        if taskIds:
            请求体: dict = {"taskIds": list(taskIds)}
        else:
            请求体 = {"pageSize": pageSize}
            if status:
                请求体["status"] = list(status)
            if cursor:
                请求体["cursor"] = cursor

        响应 = self.网络.请求(
            "POST",
            "/cloudcollection/v1/list_task",
            json数据=请求体,
        )
        return 响应.get("data", 响应) if isinstance(响应, dict) else 响应

    def 查询全部任务(
        self,
        pageSize: int = 100,
        status: Optional[Iterable[int]] = None,
    ) -> list[dict]:
        """便捷方法：拉取任务列表（返回 list 数组）"""
        数据 = self.查询任务(
            status=status or 全部状态, pageSize=pageSize)
        return 数据.get("list", []) if isinstance(数据, dict) else []

    def 取任务详情(self, taskId: str) -> Optional[dict]:
        """查询单个任务详情"""
        数据 = self.查询任务(taskIds=[taskId])
        列表 = 数据.get("list", []) if isinstance(数据, dict) else []
        if not 列表:
            return None
        return 列表[0]

    def 等待任务完成(
        self,
        taskId: str,
        超时秒: float = 3600.0,
        轮询间隔秒: float = 3.0,
        进度回调=None,
    ) -> dict:
        """轮询直到任务完成（status=2）

        :param 进度回调: 回调(任务详情: dict)
        :return: 完成后的任务详情（含 fileId）
        """
        开始 = time.time()
        while time.time() - 开始 < 超时秒:
            详情 = self.取任务详情(taskId)
            if 详情 and 进度回调:
                进度回调(详情)
            状态 = 详情.get("status") if 详情 else None
            if 状态 == 2:
                return 详情
            if 状态 in (3, 4, 5):
                raise RuntimeError(
                    f"云添加任务 {taskId} 状态异常："
                    f"{任务状态映射.get(状态, 状态)}")
            time.sleep(轮询间隔秒)
        raise TimeoutError(f"云添加任务 {taskId} 等待超时（{超时秒}s）")