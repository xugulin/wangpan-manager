"""
上传总调度
作用：对外统一入口，串起
  算MD5 → 取token → 秒传判断 → 分片上传 → 拿文件信息
全流程
"""

from dataclasses import dataclass
from pathlib import Path

from .哈希计算器 import 计算文件MD5
from .秒传服务 import 秒传服务
from .分片上传 import 分片上传器
from ..网络.网络客户端 import 网络客户端


@dataclass
class 上传结果:
    """上传结果"""
    fileId: str
    fileName: str
    fileSize: int
    是否秒传: bool


class 上传总调度:
    """上传总调度"""

    def __init__(self, 网络: 网络客户端):
        self.网络 = 网络
        self.秒传 = 秒传服务(网络)
        self.分片 = 分片上传器(网络)

    def 上传文件(
        self,
        文件路径: str | Path,
        父目录ID: str,
        通道数: int = 30,
        进度回调=None,
    ) -> 上传结果:
        """
        上传单个文件（对外主入口）
        :param 文件路径: 本地文件路径
        :param 父目录ID: 上传到的目录ID（抓包里是 1945546119831085105 这样的数字字符串）
        :param 通道数: 默认 30（抓包中 capacity=30）
        :param 进度回调: 可选，形如 回调(阶段:str, 进度:float)
        """
        路径 = Path(文件路径)
        文件名 = 路径.name
        文件大小 = 路径.stat().st_size

        # 第一步：计算 MD5
        if 进度回调:
            进度回调("计算MD5", 0.0)

        def MD5进度(已读: int, 总: int):
            if 进度回调:
                进度回调("计算MD5", 已读 / 总)

        MD5值 = 计算文件MD5(路径, MD5进度)

        # 第二步：取上传 token（可能秒传）
        if 进度回调:
            进度回调("取上传令牌", 0.0)

        取结果 = self.秒传.取上传令牌(
            文件名=文件名,
            文件大小=文件大小,
            MD5值=MD5值,
            父目录ID=父目录ID,
            通道数=通道数,
        )

        # 第三步：秒传命中直接返回
        if 取结果.是否秒传:
            if 进度回调:
                进度回调("秒传命中", 1.0)
            信息 = self.分片.按任务ID取文件信息(取结果.令牌.taskId)
            return 上传结果(
                fileId=信息["fileId"],
                fileName=信息["fileName"],
                fileSize=信息["fileSize"],
                是否秒传=True,
            )

        # 第四步：可闪电上传判断（抓包中有这个判断，这里不单独实现）
        self.秒传.检查是否可闪电上传(
            taskId=取结果.令牌.taskId,
            gcid=取结果.令牌.gcid,
            cid=取结果.令牌.gcid,
        )

        # 第五步：分片上传
        if 进度回调:
            进度回调("分片上传", 0.0)

        def 分片进度(已上传: int, 总: int):
            if 进度回调:
                进度回调("分片上传", 已上传 / 总)

        self.分片.上传(
            文件路径=路径,
            令牌=取结果.令牌,
            进度回调=分片进度,
        )

        # 第六步：拿最终文件信息
        最终信息 = self.分片.按任务ID取文件信息(取结果.令牌.taskId)

        if 进度回调:
            进度回调("完成", 1.0)

        return 上传结果(
            fileId=最终信息["fileId"],
            fileName=最终信息["fileName"],
            fileSize=最终信息["fileSize"],
            是否秒传=False,
        )