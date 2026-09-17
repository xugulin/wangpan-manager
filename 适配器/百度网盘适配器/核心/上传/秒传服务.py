# 百度网盘适配器/核心/上传/秒传服务.py
"""
秒传服务
作用：调用 `/api/rapidupload` 尝试「秒传」——服务端已有该文件时直接生成副本，无需真正上传

官方构造逻辑（从上传器 JS 提取，见 PLAN.md §5.2.1）
--------------------------------------------------
    o = { uploadid, path, "content-length": size,
          "content-md5": _encryptMd5(contentMD5),   // ← 必须传加密态！
          "slice-md5":   _encryptMd5(sliceMD5) }    // ← 必须传加密态！
    a = Math.floor(Date.now()/1000)                  // data_time = 秒级时间戳
    c = parseInt(makeMD5(uk + o["content-md5"] + a).substr(0,8), 16)
            % (size - 262144 + 1)                    // data_offset 确定性公式
    o.data_content = base64(file.slice(c, c + 262144))   // 取 256KB 并 base64

实测结论（Phase 1.6）：
  - `errno:0`   → 命中，文件已在网盘生成
  - `errno:404` → 未命中，属**正常分支**，调用方应回落分片上传
  - 缺 `data_time`/`data_offset`/`data_content` → `errno:2`（参数错误）
  - `data_content` 内容不匹配真实文件 → `errno:2`（服务端**确实校验**内容）
  - `uploadid` 非必需
  - 判定依据是 `content-md5`，**与 `path` 无关** → 可用于同文件换名/换目录
"""

from __future__ import annotations

import base64
import hashlib
import logging
import time
from pathlib import Path

from ..网络.网络客户端 import 网络客户端, 接口错误, 秒传未命中
from .哈希计算器 import 加密md5, 首片大小, 读偏移数据

logger = logging.getLogger("百度网盘.上传.秒传")


class 秒传服务:
    """`/api/rapidupload` 封装。"""

    def __init__(self, 网络: 网络客户端):
        self.网络 = 网络

    # ---------------- 内部 ----------------

    def _取uk(self) -> str:
        """data_offset 的公式需要 uk（`makeMD5(uk + contentMd5 + time)`）。

        会话里通常没有 uk（它不是 Cookie），因此直接调一次
        `/api/gettemplatevariable` 取回——**不经过认证服务**，
        以免把结果写进另一个会话仓库导致不一致。
        """
        会话uk = self.网络.取会话().get("uk")
        if 会话uk:
            return str(会话uk)
        try:
            数据 = self.网络.请求(
                "GET", "/api/gettemplatevariable",
                params={"fields": '["uk"]'}, 带渠道=False)
            uk = (数据.get("result") or {}).get("uk")
            if uk:
                logger.debug(f"[秒传] 补取到 uk={uk}")
                return str(uk)
        except Exception as e:
            logger.warning(f"[秒传] 取 uk 失败，data_offset 将退化为 0：{e}")
        return ""

    @staticmethod
    def 计算偏移(uk: str, content_md5_加密: str, data_time: int, 大小: int) -> int:
        """官方 `_getDataOffset`：`md5(uk+md5+time)[:8] % (size - 256KB + 1)`。

        ⚠️ 文件小于 256 KB 时无法取满窗口，退化为 0（服务端会按实际长度校验）。
        """
        窗口 = 大小 - 首片大小 + 1
        if 窗口 <= 1 or not uk:
            return 0
        摘要 = hashlib.md5(
            f"{uk}{content_md5_加密}{data_time}".encode()).hexdigest()
        return int(摘要[:8], 16) % 窗口

    # ---------------- 对外 ----------------

    def 试秒传(
        self,
        目标路径: str,
        本地文件路径: str | Path,
        大小: int,
        整文件md5: str,
        首片md5: str,
        目标目录: str = "/",
        uploadid: str | None = None,
    ) -> bool:
        """尝试秒传。

        :param 整文件md5: **纯净** MD5（内部会加密）
        :param 首片md5:   首 256 KB 的**纯净** MD5（内部会加密）
        :return: True = 命中；False = 未命中（errno:404）或**文件太小无法秒传**，
                 调用方应回落分片上传
        :raises 接口错误: 其它错误码（如 errno:2 参数错误、errno:132 风控）
        """
        # ⚠️ 小于 256 KB 的文件无法秒传（实测 errno:2）
        #    原因：持有证明 data_content 固定取 256 KB，
        #    文件比这还小时 `size - 256KB + 1` 为负，无法构造有效偏移与片段。
        if 大小 < 首片大小:
            logger.info(f"[秒传] 文件仅 {大小}B（< {首片大小}B），"
                        f"跳过秒传直接分片上传：{目标路径}")
            return False

        内容md5 = 加密md5(整文件md5)
        片md5 = 加密md5(首片md5)
        data_time = int(time.time())
        偏移 = self.计算偏移(self._取uk(), 内容md5, data_time, 大小)
        片段 = 读偏移数据(本地文件路径, 偏移, 首片大小)
        data_content = base64.b64encode(片段).decode("ascii")

        表单 = {
            "path": 目标路径,
            "content-length": str(大小),
            "content-md5": 内容md5,
            "slice-md5": 片md5,
            "target_path": 目标目录 or "/",
            "local_mtime": str(int(time.time())),
            "data_time": str(data_time),
            "data_offset": str(偏移),
            "data_content": data_content,
        }
        if uploadid:
            表单["uploadid"] = uploadid

        logger.debug(
            f"[秒传] 尝试 {目标路径} 大小={大小} "
            f"content-md5={内容md5} offset={偏移} 片段={len(片段)}B")

        try:
            数据 = self.网络.请求(
                "POST", "/api/rapidupload",
                params={"rtype": "1"},
                表单数据=表单,
                需要bdstoken=True,
                # 404 = 未命中，属正常分支，不抛异常
                允许errno=(0, 秒传未命中),
            )
        except 接口错误 as e:
            if e.errno == 秒传未命中:
                logger.info(f"[秒传] 未命中（errno:404），需回落分片上传：{目标路径}")
                return False
            raise

        errno = 数据.get("errno") if isinstance(数据, dict) else None
        if errno == 0:
            logger.info(f"[秒传] ✅ 命中：{目标路径}")
            return True
        logger.info(f"[秒传] 未命中（errno={errno}）：{目标路径}")
        return False
