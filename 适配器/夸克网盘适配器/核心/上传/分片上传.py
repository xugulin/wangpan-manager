# 夸克网盘适配器/核心/上传/分片上传.py
"""
上传执行器（阶段二 2.7）

对外只有一条入口 `上传()`，内部按大小分流成两条路径：

    小文件（< 5MB）  → `_单次上传()`：1 次 OSS PUT 传完整个文件
    大文件（≥ 5MB）  → `_分片上传()`：4MB 切片，逐片 PUT

⚠️ 关于「简单 PUT」的实测结论（2026-09-14，探针验证）
--------------------------------------------------------
技术上不存在「完全不带 multipart 参数的简单 PUT」这条路径：

    POST file/upload/auth
      auth_meta 里不带 uploadId
      → 接口错误: Bad Parameter: [uploadId is null]

`file/upload/auth` 的 `auth_meta` **必须**携带 `partNumber` 与 `uploadId`，
二者由 `file/update/hash` 返回，也就是说夸克只提供 multipart 语义的授权。

所以本文件里的「单次上传」= **1 次 PUT + 1 次单分片合并**，仅此而已；
区别只在「不切片、不算增量哈希、不循环」，PUT 本身仍然只有一次。
这正是 ≤5MB 能达到的最优形态，日志因此按「单次上传」措辞，
不再写成容易误读的「分片 1/1」。

其余实测通过的形态（没有用 oss2，也没有用 STS）：

    `POST file/upload/auth` 拿 `auth_key`，然后裸 httpx PUT 到
    `https://{bucket}.pds.quark.cn/{obj_key}?partNumber=n&uploadId=…`，
    由 `X-Oss-Hash-Ctx` 承载增量哈希（见 核心/上传/哈希计算器.py），
    该头对 片号 ≥ 2 是**强制**的。

分片路径为什么串行（不并发）
--------------------------------------------------------
OSS 的增量哈希上下文依赖前序全部字节，并发分片会让上下文快照难以保证；
实测串行 3 片 10MB 约 2 秒，带宽远未打满，没有必要为并发承担出错面。
"""
import logging
import time
from pathlib import Path
from typing import Callable, Optional

from ..网络.网络客户端 import 网络客户端
from ..接口.上传接口 import 上传接口, 分片大小, 单分片阈值, 规划分片
from .哈希计算器 import 计算增量SHA1上下文
from .秒传服务 import 上传令牌

logger = logging.getLogger("夸克网盘.上传.分片")

默认每片重试次数 = 3
重试退避基数秒 = 2.0


class 分片上传器:
    """上传执行器：小文件单次 PUT，大文件 4MB 分片直传 OSS"""

    def __init__(self, 网络: 网络客户端):
        self.网络 = 网络
        self.接口 = 上传接口(网络)

    # ── 统一入口 ─────────────────────────────────────────────
    def 上传(
        self,
        文件路径: str | Path,
        令牌: 上传令牌,
        mime: str,
        进度回调: Optional[Callable] = None,
        每片: int = 分片大小,
    ) -> list[tuple[int, str]]:
        """上传整个文件，按大小自动分流

        :param 进度回调: `回调(阶段: str, 进度: float)`，本阶段进度 0.0~1.0
        :return: [(分片号, etag), …]；单次上传时即 `[(1, etag)]`
        """
        路径 = Path(文件路径)
        总大小 = 路径.stat().st_size

        if 总大小 < 单分片阈值:
            return self._单次上传(路径, 令牌, mime, 总大小, 进度回调)
        return self._分片上传(路径, 令牌, mime, 总大小, 每片, 进度回调)

    # ── 路径一：小文件，一次 PUT 打满 ────────────────────────
    def _单次上传(
        self,
        路径: Path,
        令牌: 上传令牌,
        mime: str,
        总大小: int,
        进度回调: Optional[Callable] = None,
    ) -> list[tuple[int, str]]:
        """< 5MB：1 次 PUT 传完整个文件，不切片、不算增量哈希

        仍然带 `partNumber=1&uploadId=…`——这不是冗余，是夸克的硬性要求
        （见模块文档头的探针结论），少了 uploadId 授权接口直接拒绝。
        """
        logger.info(
            f"[上传] {路径.name} 共 {总大小} 字节 < {单分片阈值 // 1024 // 1024}MB"
            f" → 单次上传（1 次 PUT，不分片）")

        if 进度回调:
            try:
                进度回调("上传中", 0.0)
            except Exception:
                pass

        开始 = time.time()
        etag = self._传一片(路径, 令牌, mime, 1, 总大小, "")

        if 进度回调:
            try:
                进度回调("上传完成", 1.0)
            except Exception:
                pass

        耗时 = time.time() - 开始
        logger.info(
            f"[上传] {路径.name} 单次上传完成 ETag={etag[:16]}…，"
            f"{总大小} 字节，耗时 {耗时:.1f}s "
            f"({总大小 / 1024 / 1024 / max(耗时, 0.001):.1f} MB/s)")
        return [(1, etag)]

    # ── 路径二：大文件，4MB 切片串行 ─────────────────────────
    def _分片上传(
        self,
        路径: Path,
        令牌: 上传令牌,
        mime: str,
        总大小: int,
        每片: int,
        进度回调: Optional[Callable] = None,
    ) -> list[tuple[int, str]]:
        """≥ 5MB：按 每片 字节切片，逐片 PUT（片号 ≥2 带增量哈希上下文）"""
        切片 = 规划分片(总大小, 每片)
        总数 = len(切片)
        logger.info(
            f"[分片] {路径.name} 共 {总大小} 字节 → {总数} 片"
            f"（每片 {每片 // 1024 // 1024}MB）")

        结果: list[tuple[int, str]] = []
        已传字节 = 0
        开始 = time.time()

        for 序号, (片号, 片大小) in enumerate(切片):
            if 进度回调:
                try:
                    进度回调(f"上传分片 {片号}/{总数}", 已传字节 / max(1, 总大小))
                except Exception:
                    pass

            上下文 = ""
            if 片号 > 1:
                上下文 = 计算增量SHA1上下文(路径, 片号, 每片)
                logger.debug(f"[分片] {片号} 上下文 {len(上下文)} 字符")

            etag = self._传一片(路径, 令牌, mime, 片号, 片大小, 上下文)
            结果.append((片号, etag))
            已传字节 += 片大小
            logger.info(
                f"[分片] {片号}/{总数} 完成 ETag={etag[:16]}… "
                f"({已传字节}/{总大小})")

        if 进度回调:
            try:
                进度回调("分片完成", 1.0)
            except Exception:
                pass

        耗时 = time.time() - 开始
        logger.info(
            f"[分片] 全部完成，{总数} 片 / {总大小} 字节，耗时 {耗时:.1f}s "
            f"({总大小 / 1024 / 1024 / max(耗时, 0.001):.1f} MB/s)")
        return 结果

    # ── 单片 PUT（两条路径共用）──────────────────────────────
    def _传一片(self, 路径: Path, 令牌: 上传令牌, mime: str,
                片号: int, 片大小: int, 上下文: str) -> str:
        """单分片上传，带指数退避重试"""
        最后错误: Exception | None = None
        for 尝试 in range(默认每片重试次数 + 1):
            try:
                return self.接口.上传分片(
                    文件路径=路径,
                    分片号=片号,
                    片大小=片大小,
                    增量上下文=上下文,
                    令牌=令牌,
                    mime=mime,
                )
            except Exception as e:
                最后错误 = e
                if 尝试 >= 默认每片重试次数:
                    break
                延迟 = min(重试退避基数秒 ** (尝试 + 1), 10.0)
                logger.warning(
                    f"[分片] {片号} 第 {尝试 + 1} 次失败：{e}；{延迟:.1f}s 后重试")
                time.sleep(延迟)
        raise RuntimeError(f"分片 {片号} 上传失败：{最后错误}") from 最后错误
