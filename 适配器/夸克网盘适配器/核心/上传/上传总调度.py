# 夸克网盘适配器/核心/上传/上传总调度.py
"""
上传总调度（阶段二 2.7）—— 对外统一入口

串起完整流程（实测通过，2026-09-14）：

    算 MD5+SHA1
      → 预上传（顺带秒传判定）
      → 提交哈希
      → 小文件单分片 / 大文件 4MB 多分片（每片带 X-Oss-Hash-Ctx）
      → OSS 合并（200 / 203）
      → finish（拿到 fid）

进度回调形如 `回调(阶段: str, 进度: float)`，进度 0.0~1.0。

对外接口（GUI 的 批量上传线程 使用）：
    上传文件(路径, 父目录ID, 进度回调)                  -> dict
    上传文件夹(目录, 父目录ID, 进度回调, 单文件回调)     -> list[dict]
"""
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from ..网络.网络客户端 import 网络客户端
from ..接口.上传接口 import 上传接口, 分片大小, 构建合并XML, 推断MIME
from ..接口.文件接口 import 文件接口
from .哈希计算器 import 计算MD5和SHA1
from .秒传服务 import 秒传服务, 上传令牌
from .分片上传 import 分片上传器

logger = logging.getLogger("夸克网盘.上传.调度")


@dataclass
class 上传结果:
    """单个文件的上传结果"""

    文件名: str = ""
    大小: int = 0
    md5: str = ""
    sha1: str = ""
    fid: str = ""
    task_id: str = ""
    分片数: int = 0
    秒传: bool = False
    服务器返回: dict = field(default_factory=dict)

    def 转字典(self) -> dict:
        return {
            "fileName": self.文件名,
            "fileId": self.fid,          # 兼容旧 GUI 的 fileId 取值
            "fid": self.fid,
            "fileSize": self.大小,
            "size": self.大小,
            "md5": self.md5,
            "sha1": self.sha1,
            "taskId": self.task_id,
            "分片数": self.分片数,
            "秒传": self.秒传,
        }


class 上传总调度:
    """夸克上传的统一入口"""

    def __init__(self, 网络: 网络客户端):
        self.网络 = 网络
        self.接口 = 上传接口(网络)
        self.文件 = 文件接口(网络)
        self.秒传 = 秒传服务(网络)
        self.分片 = 分片上传器(网络)

    # ==================== 单文件 ====================

    def 上传文件(
        self,
        文件路径: str | Path,
        父目录ID: str = "0",
        进度回调: Optional[Callable] = None,
    ) -> dict:
        """上传单个文件

        :param 父目录ID: 目标目录 fid，"0" 表示根目录
        :param 进度回调: `回调(阶段: str, 进度: float)`
        :return: 上传结果字典（含 fid / md5 / sha1 / 分片数 / 秒传）
        """
        路径 = Path(文件路径)
        if not 路径.is_file():
            raise FileNotFoundError(f"文件不存在：{文件路径}")

        大小 = 路径.stat().st_size
        名称 = 路径.name
        mime = 推断MIME(名称)
        logger.info(f"[上传] 开始 {名称} ({大小} 字节, {mime}) → fid={父目录ID}")

        def 报告(阶段: str, 进度: float):
            if 进度回调:
                try:
                    进度回调(阶段, max(0.0, min(1.0, 进度)))
                except Exception:
                    pass

        # ---- 1. 哈希（0 ~ 0.10）----
        报告("计算哈希", 0.0)
        md5, sha1 = 计算MD5和SHA1(
            路径, lambda 阶段, p: 报告(阶段, p * 0.10))
        logger.debug(f"[上传] MD5={md5} SHA1={sha1}")

        # ---- 2. 预上传 + 秒传判定（0.10）----
        报告("预上传", 0.10)
        令牌: 上传令牌 = self.秒传.预上传(名称, 大小, 父目录ID, mime)

        if 令牌.秒传命中:
            报告("秒传命中", 1.0)
            结果 = 上传结果(
                文件名=名称, 大小=大小, md5=md5, sha1=sha1,
                fid=令牌.已存在fid, task_id=令牌.task_id, 秒传=True,
                服务器返回=令牌.原始)
            logger.info(f"[上传] 秒传命中，跳过上传 {名称} fid={令牌.已存在fid}")
            return 结果.转字典()

        # ---- 3. 提交哈希（0.12）----
        报告("提交哈希", 0.12)
        self.接口.提交哈希(令牌.task_id, md5, sha1)

        # ---- 4. 分片上传（0.15 ~ 0.90）----
        # 单分片 / 多分片的决策与重试都在 分片上传器 内部
        def 分片进度(阶段: str, p: float):
            报告(阶段, 0.15 + p * 0.75)

        上传分片: list[tuple[int, str]] = self.分片.上传(
            路径, 令牌, mime, 进度回调=分片进度, 每片=分片大小)

        # ---- 5. 合并（0.90）----
        报告("合并分片", 0.90)
        xml = 构建合并XML(上传分片)
        授权 = self.接口.取合并授权(
            task_id=令牌.task_id, auth_info=令牌.auth_info,
            upload_id=令牌.upload_id, obj_key=令牌.obj_key,
            bucket=令牌.bucket, xml=xml, 回调信息=令牌.callback)
        self.接口.OSS合并(授权["url"], 授权["headers"], xml)

        # ---- 6. finish（0.97）----
        报告("完成上传", 0.97)
        fin = self.接口.完成上传(令牌.task_id, 令牌.obj_key)
        报告("上传完成", 1.0)

        结果 = 上传结果(
            文件名=名称, 大小=大小, md5=md5, sha1=sha1,
            fid=str(fin.get("fid") or ""), task_id=令牌.task_id,
            分片数=len(上传分片), 秒传=False, 服务器返回=fin)
        logger.info(
            f"[上传] 完成 {名称} fid={结果.fid} 分片={结果.分片数}")
        return 结果.转字典()

    # ==================== 文件夹 ====================

    def 上传文件夹(
        self,
        目录路径: str | Path,
        父目录ID: str = "0",
        进度回调: Optional[Callable] = None,
        单文件回调: Optional[Callable] = None,
    ) -> list[dict]:
        """递归上传整个目录

        语义（沿用原实现）：先在 `父目录ID` 下**创建同名顶层目录**，
        再把它下面的内容按相对结构放进去。

        :param 单文件回调: `回调(文件名, 结果字典)`
        :return: 每个文件的结果字典列表
        """
        根 = Path(目录路径).resolve()
        if not 根.is_dir():
            raise NotADirectoryError(f"目录不存在：{目录路径}")

        顶层名 = 根.name
        顶层ID = self._创建目录(str(父目录ID or "0"), 顶层名)
        logger.info(f"[上传] 创建顶层目录 '{顶层名}' fid={顶层ID}")

        文件列表 = sorted(p for p in 根.rglob("*") if p.is_file())
        总数 = len(文件列表)
        if 总数 == 0:
            logger.info(f"[上传] 文件夹 {顶层名} 内没有文件")
            return []

        # 相对目录字符串（"" 表示顶层） → fid
        目录缓存: dict[str, str] = {"": 顶层ID}
        结果列表: list[dict] = []

        for i, 文件 in enumerate(文件列表):
            相对 = str(文件.parent.relative_to(根)).replace("\\", "/")
            if 相对 == ".":
                相对 = ""

            if 相对 not in 目录缓存:
                父ID = 目录缓存[""]
                累积 = ""
                for 段 in 相对.split("/"):
                    累积 = f"{累积}/{段}" if 累积 else 段
                    if 累积 in 目录缓存:
                        父ID = 目录缓存[累积]
                        continue
                    子ID = self._创建目录(父ID, 段)
                    目录缓存[累积] = 子ID
                    父ID = 子ID

            文件父ID = 目录缓存[相对]
            基 = i / 总数
            范围 = 1 / 总数
            文件名 = 文件.name

            def 子进度(阶段: str, p: float,
                       _b=基, _r=范围, _n=文件名):
                if 进度回调:
                    try:
                        进度回调(f"{_n} - {阶段}", _b + p * _r)
                    except Exception:
                        pass

            try:
                信息 = self.上传文件(str(文件), 文件父ID, 进度回调=子进度)
            except Exception as e:
                logger.error(f"[上传] {文件} 失败：{e}")
                raise

            结果列表.append(信息)
            if 单文件回调:
                try:
                    单文件回调(文件名, 信息)
                except Exception:
                    pass

        logger.info(f"[上传] 文件夹 {顶层名} 完成，共 {len(结果列表)} 个文件")
        return 结果列表

    def _创建目录(self, 父目录ID: str, 名称: str) -> str:
        """在云盘创建目录并返回其 fid"""
        响应 = self.文件.创建文件夹(父目录ID, 名称)
        fid = ""
        if isinstance(响应, dict):
            fid = str(响应.get("fid")
                      or (响应.get("data") or {}).get("fid") or "")
        if not fid:
            raise RuntimeError(f"创建目录 '{名称}' 未返回 fid：{响应}")
        return fid
