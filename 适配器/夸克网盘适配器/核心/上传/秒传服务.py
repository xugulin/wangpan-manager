# 夸克网盘适配器/核心/上传/秒传服务.py
"""
秒传服务（阶段二 2.7）

原适配器用的是 `get_res_center_token` + `check_can_flash_upload` 两个接口，
夸克没有这一对。夸克的「预上传」本身就是一次服务端查重：

    POST file/upload/pre
    → data.task_id / auth_info / upload_id / obj_key / bucket / callback

若服务端已存在同一文件（同 md5/sha1 + 同名），预上传响应会直接带上结果标志
（`finish` / `fid`）。因此本模块的职责收敛为：
  1. 调 上传接口.预上传，把结果封装成 `上传令牌`
  2. 顺带判定「秒传命中」，命中则调用方可以跳过全部上传步骤

⚠️ 秒传判定目前是**防御式**实现：只有响应里真的出现 `finish` / `fid` 才认命中，
   命中不了就走正常上传（不会误判导致文件丢失）。夸克的秒传字段尚未实测确认，
   待有 dup 文件时补充验证（见 PLAN.md §6.2）。
"""
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from ..网络.网络客户端 import 网络客户端
from ..接口.上传接口 import 上传接口

logger = logging.getLogger("夸克网盘.上传.秒传")


@dataclass
class 上传令牌:
    """一次上传会话的服务端凭证"""

    task_id: str = ""
    auth_info: str = ""
    upload_id: str = ""
    obj_key: str = ""
    bucket: str = "ul-zb"
    callback: Any = None
    秒传命中: bool = False
    已存在fid: str = ""
    原始: dict = field(default_factory=dict)

    @classmethod
    def 从字典(cls, 数据: dict) -> "上传令牌":
        数据 = 数据 or {}
        fid = str(数据.get("fid") or "")
        # 命中判据：显式 finish=True，或直接给了 fid（且没有 upload_id 才是真秒传）
        命中 = bool(fid) and (
            数据.get("finish") is True
            or not 数据.get("upload_id")
        )
        return cls(
            task_id=str(数据.get("task_id") or ""),
            auth_info=str(数据.get("auth_info") or ""),
            upload_id=str(数据.get("upload_id") or ""),
            obj_key=str(数据.get("obj_key") or ""),
            bucket=str(数据.get("bucket") or "ul-zb"),
            callback=数据.get("callback"),
            秒传命中=命中,
            已存在fid=fid,
            原始=数据,
        )


class 秒传服务:
    """预上传 + 秒传判定"""

    def __init__(self, 网络: 网络客户端):
        self.网络 = 网络
        self.接口 = 上传接口(网络)

    def 预上传(self, 名称: str, 大小: int, 父目录ID: str = "0",
               mime: Optional[str] = None) -> 上传令牌:
        """调预上传并封装成上传令牌"""
        数据 = self.接口.预上传(名称, 大小, 父目录ID, mime)
        令牌 = 上传令牌.从字典(数据)
        if 令牌.秒传命中:
            logger.info(
                f"[秒传] 命中！跳过实际上传 fid={令牌.已存在fid} ({名称})")
        return 令牌

    # 兼容旧命名
    取上传令牌 = 预上传
