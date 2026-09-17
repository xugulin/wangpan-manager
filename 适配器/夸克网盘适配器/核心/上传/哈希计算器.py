# 夸克网盘适配器/核心/上传/哈希计算器.py
"""
文件哈希计算（阶段二 2.7）

夸克上传需要三样东西：

  1. **MD5**    —— `POST file/update/hash`
  2. **SHA1**   —— 同上
  3. **增量 SHA1 上下文**（`X-Oss-Hash-Ctx`）—— 分片号 ≥ 2 时**强制要求**

第 3 项是本模块的核心。省略它的后果是决定性的（2026-09-14 实测）：

    HTTP 400
    <Error>
      <Code>NoHashContext</Code>
      <Message>Has No Hash Context in Parallel Multipart file.</Message>
      <EC>0042-00000106</EC>
      <PartNumber>2</PartNumber>
    </Error>

上下文格式 = `base64(JSON)`：

    {"hash_type":"sha1","h0":"…","h1":"…","h2":"…","h3":"…","h4":"…",
     "Nl":"<已处理位数>","Nh":"0","data":"","num":"0"}

⚠️ **为什么不需要 padding**：
   `已处理字节 = (分片号 - 1) × 4MB`，而 4MB = 65536 × 64 字节，
   恒为完整的 SHA1 块，因此永远不会出现「不完整末块」。
   离线验证：用本模块算出的中间状态 + 标准 SHA1 padding 收尾，
   可精确还原 `hashlib.sha1()`（4/4 用例通过，2026-09-14 spike）。
"""
import base64
import hashlib
import json
import logging
import struct
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger("夸克网盘.上传.哈希")

# SHA1 初始状态
_SHA1_INIT = (0x67452301, 0xEFCDAB89, 0x98BADCFE, 0x10325476, 0xC3D2E1F0)


def 计算文件MD5(文件路径: str | Path,
                进度回调: Optional[Callable] = None) -> str:
    """只算 MD5（保留旧函数名，内部转调 计算MD5和SHA1）

    :param 进度回调: 兼容旧签名 `回调(已读字节, 总字节)`
    """
    路径 = Path(文件路径)
    总大小 = 路径.stat().st_size or 1

    def 桥接(阶段: str, 进度: float):
        if 进度回调:
            try:
                进度回调(int(进度 * 总大小), 总大小)
            except Exception:
                pass

    return 计算MD5和SHA1(文件路径, 桥接)[0]


def 计算MD5和SHA1(
    文件路径: str | Path,
    进度回调: Optional[Callable] = None,
) -> tuple[str, str]:
    """一次遍历同时算出 MD5 与 SHA1

    :param 进度回调: `回调(阶段: str, 进度: float)`，进度范围 0.0~1.0
    :return: (md5 十六进制, sha1 十六进制)
    """
    路径 = Path(文件路径)
    总大小 = 路径.stat().st_size
    md5 = hashlib.md5()
    sha1 = hashlib.sha1()
    已读 = 0
    # 1MB 块：比 8KB 快得多，也不会占用太多内存
    块大小 = 1024 * 1024

    with 路径.open("rb") as f:
        while True:
            块 = f.read(块大小)
            if not 块:
                break
            md5.update(块)
            sha1.update(块)
            已读 += len(块)
            if 进度回调 and 总大小 > 0:
                try:
                    进度回调("计算哈希", 已读 / 总大小)
                except Exception:
                    pass

    return md5.hexdigest(), sha1.hexdigest()


# ==================== 增量 SHA1 上下文 ====================

def 压缩SHA1块(数据: bytes) -> tuple[int, int, int, int, int]:
    """对 `数据` 的**完整 64 字节块**做 SHA1 压缩，返回中间状态 (h0..h4)

    与 QuarkPan `file_upload_service.py:707-769` 的循环等价，
    已用 `hashlib` 离线交叉验证（见模块 docstring）。
    """
    h0, h1, h2, h3, h4 = _SHA1_INIT

    可处理长度 = len(数据) - (len(数据) % 64)
    for i in range(0, 可处理长度, 64):
        块 = 数据[i:i + 64]
        w = [struct.unpack(">I", 块[j:j + 4])[0] for j in range(0, 64, 4)]
        for t in range(16, 80):
            x = w[t - 3] ^ w[t - 8] ^ w[t - 14] ^ w[t - 16]
            w.append(((x << 1) | (x >> 31)) & 0xFFFFFFFF)

        a, b, c, d, e = h0, h1, h2, h3, h4
        for t in range(80):
            if t < 20:
                f = (b & c) | ((~b) & d)
                k = 0x5A827999
            elif t < 40:
                f = b ^ c ^ d
                k = 0x6ED9EBA1
            elif t < 60:
                f = (b & c) | (b & d) | (c & d)
                k = 0x8F1BBCDC
            else:
                f = b ^ c ^ d
                k = 0xCA62C1D6
            temp = (((a << 5) | (a >> 27)) + f + e + k + w[t]) & 0xFFFFFFFF
            e = d
            d = c
            c = ((b << 30) | (b >> 2)) & 0xFFFFFFFF
            b = a
            a = temp

        h0 = (h0 + a) & 0xFFFFFFFF
        h1 = (h1 + b) & 0xFFFFFFFF
        h2 = (h2 + c) & 0xFFFFFFFF
        h3 = (h3 + d) & 0xFFFFFFFF
        h4 = (h4 + e) & 0xFFFFFFFF

    return h0, h1, h2, h3, h4


def 计算增量SHA1上下文(
    文件路径: str | Path,
    分片号: int,
    分片大小: int,
) -> str:
    """算出上传第 `分片号` 片时所需的 `X-Oss-Hash-Ctx`

    语义：该片**之前**所有字节的 SHA1 中间状态。

    :param 分片号: 从 1 开始；分片 1 不需要上下文（调用方应跳过）
    :param 分片大小: 每个分片的字节数（当前为 4MB）
    :return: base64 编码的 JSON 上下文
    """
    if 分片号 < 2:
        return ""

    已处理字节 = (分片号 - 1) * 分片大小
    已处理位 = 已处理字节 * 8

    路径 = Path(文件路径)
    with 路径.open("rb") as f:
        前面数据 = f.read(已处理字节)

    if len(前面数据) != 已处理字节:
        logger.warning(
            f"[哈希] 期望读取 {已处理字节} 字节，实际 {len(前面数据)} 字节")

    h0, h1, h2, h3, h4 = 压缩SHA1块(前面数据)

    上下文 = {
        "hash_type": "sha1",
        "h0": str(h0), "h1": str(h1), "h2": str(h2),
        "h3": str(h3), "h4": str(h4),
        "Nl": str(已处理位), "Nh": "0",
        "data": "", "num": "0",
    }
    return base64.b64encode(
        json.dumps(上下文, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
