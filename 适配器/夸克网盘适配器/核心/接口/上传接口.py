# 夸克网盘适配器/核心/接口/上传接口.py
"""
上传接口（阶段二 2.7）—— 夸克上传协议层

本模块只负责「与夸克/OSS 对话」，不含编排逻辑（编排在 核心/上传/上传总调度.py）。

完整流程（2026-09-14 实测通过，10MB 三分为 4MB/4MB/2MB）：

  ① POST file/upload/pre        预上传 → task_id / auth_info / upload_id / obj_key / bucket / callback
  ② POST file/update/hash       提交 md5 + sha1
  ③ 逐片：
       POST file/upload/auth    取分片授权（auth_meta 里**必须**含 x-oss-hash-ctx）
       PUT  https://{bucket}.pds.quark.cn/{obj_key}?partNumber=n&uploadId=…   ← x-oss-hash-ctx 头
  ④ POST file/upload/auth       取合并授权（Content-MD5 + x-oss-callback）
     POST https://{bucket}.pds.quark.cn/{obj_key}?uploadId=…   提交 XML 合并（200 / 203 均算成功）
  ⑤ POST file/upload/finish     完成 → 返回 fid

⚠️ 三个易错点（都实测踩过）：
  1. `X-Oss-Hash-Ctx` 是**强制**的。不带它分片 2 会被 OSS 拒绝：
     `<Code>NoHashContext</Code> / <EC>0042-00000106</EC>`。
  2. auth_meta 的签名串必须**逐字节**与实际发出的请求头一致
     （含 x-oss-date / x-oss-hash-ctx / x-oss-user-agent 三行，顺序不能变），
     否则签名校验失败。
  3. 合并接受 **203**（合并成功但 callback 失败），不能只认 200。
"""
import base64
import hashlib
import json
import logging
import mimetypes
import time
from email.utils import formatdate
from pathlib import Path
from typing import Any, Optional

from ..网络.网络客户端 import 网络客户端, 接口错误

logger = logging.getLogger("夸克网盘.上传")

# ---- 协议常量 ----
分片大小 = 4 * 1024 * 1024        # 4MB（实测通过）
单分片阈值 = 5 * 1024 * 1024      # < 5MB 走单分片
默认桶 = "ul-zb"                  # 预上传没给 bucket 时的兜底
OSS域名模板 = "https://{bucket}.pds.quark.cn/{obj_key}"
OSS_UA = ("aliyun-sdk-js/1.0.0 Chrome Mobile 139.0.0.0 "
          "on Google Nexus 5 (Android 6.0)")


def 推断MIME(文件名: str | Path) -> str:
    mime, _ = mimetypes.guess_type(str(文件名))
    return mime or "application/octet-stream"


def _OSS日期() -> str:
    """RFC 1123 GMT 日期，例如 `Fri, 13 Sep 2026 17:40:16 GMT`

    ⚠️⚠️ **必须使用 locale 无关的方式生成**，绝不能用
    `datetime.strftime("%a, %d %b %Y %H:%M:%S GMT")`！

    原因（2026-09-14 GUI 实测踩坑）：**Qt 在 QApplication 初始化时会调用
    `setlocale(LC_ALL, "")`**，把 `LC_TIME` 从默认的 `C` 改成 `zh_CN.UTF-8`；
    之后 `%a`/`%b` 会输出中文星期与月份，得到
    `日, 13 9月 2026 17:50:58 GMT`，OSS 直接拒绝：

        The input parameter date is not valid. date should be GMT format

    为什么纯脚本测试发现不了：CPython 启动时只设置 `LC_CTYPE`，**不动 `LC_TIME`**，
    所以命令行下 `strftime` 仍是英文。只有起了 QApplication 才会变成中文。

    `email.utils.formatdate(usegmt=True)` 内部用硬编码的英文星期/月份表，
    与 locale 完全无关，是这里唯一正确的选择。
    """
    return formatdate(usegmt=True)


def 构建合并XML(分片列表: list[tuple[int, str]]) -> str:
    """[(分片号, etag), …] → CompleteMultipartUpload XML（无换行，实测形态）"""
    部分 = "\n".join(
        f'<Part>\n<PartNumber>{号}</PartNumber>\n<ETag>"{etag}"</ETag>\n</Part>'
        for 号, etag in 分片列表
    )
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<CompleteMultipartUpload>\n' + 部分
            + '\n</CompleteMultipartUpload>')


def 规划分片(总大小: int, 每片: int = 分片大小) -> list[tuple[int, int]]:
    """[(分片号, 本片字节数), …]"""
    结果: list[tuple[int, int]] = []
    剩余, 号 = 总大小, 1
    while 剩余 > 0:
        本片 = min(每片, 剩余)
        结果.append((号, 本片))
        剩余 -= 本片
        号 += 1
    return 结果 or [(1, 0)]


class 上传接口:
    """夸克上传协议层（不含编排）"""

    def __init__(self, 网络: 网络客户端):
        self.网络 = 网络

    # ==================== ① 预上传 ====================

    def 预上传(self, 名称: str, 大小: int, 父目录ID: str = "0",
               mime: str | None = None) -> dict:
        """POST file/upload/pre

        :return: data 字典（task_id / auth_info / upload_id / obj_key /
                 bucket / callback）
        """
        现在 = int(time.time() * 1000)
        请求体 = {
            "ccp_hash_update": True,
            "parallel_upload": True,
            "pdir_fid": str(父目录ID or "0"),
            "dir_name": "",
            "size": int(大小),
            "file_name": 名称,
            "format_type": mime or 推断MIME(名称),
            "l_updated_at": 现在,
            "l_created_at": 现在,
        }
        logger.debug(f"[上传] 预上传 {名称} ({大小} 字节)")
        结果 = self.网络.请求("POST", "file/upload/pre", json数据=请求体)
        数据 = 结果.get("data") if isinstance(结果, dict) else None
        if not isinstance(数据, dict) or not 数据.get("task_id"):
            raise 接口错误(
                f"预上传失败，未取到 task_id：{结果}", 状态码=None)
        logger.info(
            f"[上传] 预上传成功 task_id={数据.get('task_id')} "
            f"bucket={数据.get('bucket')}")
        return 数据

    # ==================== ② 提交哈希 ====================

    def 提交哈希(self, task_id: str, md5: str, sha1: str) -> dict:
        """POST file/update/hash"""
        logger.debug(f"[上传] 提交哈希 task_id={task_id}")
        结果 = self.网络.请求(
            "POST", "file/update/hash",
            json数据={"task_id": task_id, "md5": md5, "sha1": sha1})
        return 结果.get("data", 结果) if isinstance(结果, dict) else {}

    # ==================== ③ 分片授权 + 直传 ====================

    def 取分片授权(
        self,
        task_id: str,
        auth_info: str,
        upload_id: str,
        obj_key: str,
        bucket: str,
        分片号: int,
        mime: str,
        增量上下文: str = "",
    ) -> dict:
        """POST file/upload/auth → {url, headers}

        auth_meta 必须包含且仅包含实际会发出的 x-oss-* 头（顺序固定）。
        """
        oss_date = _OSS日期()
        头部行 = [f"x-oss-date:{oss_date}"]
        if 增量上下文:
            头部行.append(f"x-oss-hash-ctx:{增量上下文}")
        头部行.append(f"x-oss-user-agent:{OSS_UA}")

        auth_meta = ("PUT\n\n" + mime + "\n" + oss_date + "\n"
                     + "\n".join(头部行)
                     + f"\n/{bucket}/{obj_key}"
                       f"?partNumber={分片号}&uploadId={upload_id}")

        结果 = self.网络.请求(
            "POST", "file/upload/auth",
            json数据={"task_id": task_id, "auth_info": auth_info,
                      "auth_meta": auth_meta})
        auth_key = ((结果.get("data") or {}).get("auth_key")
                    if isinstance(结果, dict) else "") or ""
        if not auth_key:
            raise 接口错误(f"分片 {分片号} 未取到 auth_key：{结果}")

        请求头 = {
            "Content-Type": mime,
            "x-oss-date": oss_date,
            "x-oss-user-agent": OSS_UA,
            "authorization": auth_key,
        }
        if 增量上下文:
            请求头["X-Oss-Hash-Ctx"] = 增量上下文

        return {
            "url": (OSS域名模板.format(bucket=bucket, obj_key=obj_key)
                    + f"?partNumber={分片号}&uploadId={upload_id}"),
            "headers": 请求头,
        }

    def OSS直传(self, url: str, 请求头: dict, 数据: bytes) -> str:
        """PUT 一个分片到 OSS，返回 ETag（已去引号）"""
        响应 = self.网络.OSS请求("PUT", url, 内容=数据, 请求头=请求头)
        if 响应.status_code != 200:
            raise 接口错误(
                f"OSS 分片上传失败：HTTP {响应.status_code} {响应.text[:400]}")
        etag = (响应.headers.get("etag") or "").strip('"')
        if not etag:
            raise 接口错误("OSS 返回 200 但没有 ETag")
        return etag

    # ==================== ④ 合并 ====================

    def 取合并授权(
        self,
        task_id: str,
        auth_info: str,
        upload_id: str,
        obj_key: str,
        bucket: str,
        xml: str,
        回调信息: Any = None,
    ) -> dict:
        """POST file/upload/auth（POST 语义） → {url, headers}"""
        oss_date = _OSS日期()
        xml_md5 = base64.b64encode(
            hashlib.md5(xml.encode("utf-8")).digest()).decode("ascii")

        头部行: list[str] = []
        回调b64 = ""
        if 回调信息:
            回调b64 = base64.b64encode(json.dumps(
                回调信息, separators=(",", ":")).encode("utf-8")).decode("ascii")
            头部行.append(f"x-oss-callback:{回调b64}")
        头部行.append(f"x-oss-date:{oss_date}")
        头部行.append(f"x-oss-user-agent:{OSS_UA}")

        auth_meta = ("POST\n" + xml_md5 + "\napplication/xml\n" + oss_date
                     + "\n" + "\n".join(头部行)
                     + f"\n/{bucket}/{obj_key}?uploadId={upload_id}")

        结果 = self.网络.请求(
            "POST", "file/upload/auth",
            json数据={"task_id": task_id, "auth_info": auth_info,
                      "auth_meta": auth_meta})
        auth_key = ((结果.get("data") or {}).get("auth_key")
                    if isinstance(结果, dict) else "") or ""
        if not auth_key:
            raise 接口错误(f"合并授权未取到 auth_key：{结果}")

        请求头 = {
            "Content-Type": "application/xml",
            "x-oss-date": oss_date,
            "x-oss-user-agent": OSS_UA,
            "Content-MD5": xml_md5,
            "authorization": auth_key,
        }
        if 回调b64:
            请求头["x-oss-callback"] = 回调b64

        return {
            "url": (OSS域名模板.format(bucket=bucket, obj_key=obj_key)
                    + f"?uploadId={upload_id}"),
            "headers": 请求头,
        }

    def OSS合并(self, url: str, 请求头: dict, xml: str) -> int:
        """POST 合并请求，返回 HTTP 状态码（200 或 203 均为成功）"""
        响应 = self.网络.OSS请求("POST", url, 内容=xml, 请求头=请求头)
        if 响应.status_code not in (200, 203):
            raise 接口错误(
                f"OSS 合并失败：HTTP {响应.status_code} {响应.text[:400]}")
        if 响应.status_code == 203:
            logger.warning("[上传] OSS 合并返回 203（合并成功但 callback 失败）")
        return 响应.status_code

    # ==================== ⑤ 完成 ====================

    def 完成上传(self, task_id: str, obj_key: str) -> dict:
        """POST file/upload/finish → data（含 fid / finish / md5 / sha1）"""
        结果 = self.网络.请求(
            "POST", "file/upload/finish",
            json数据={"task_id": task_id, "obj_key": obj_key})
        数据 = 结果.get("data") if isinstance(结果, dict) else None
        return 数据 if isinstance(数据, dict) else {}

    # ==================== 便捷：交给 上传总调度 用的低层组合 ====================

    def 上传分片(
        self,
        文件路径: str | Path,
        分片号: int,
        片大小: int,
        增量上下文: str,
        令牌: Any,
        mime: str,
    ) -> str:
        """取授权 + OSS 直传，返回 ETag（一个分片的完整动作）"""
        授权 = self.取分片授权(
            task_id=令牌.task_id,
            auth_info=令牌.auth_info,
            upload_id=令牌.upload_id,
            obj_key=令牌.obj_key,
            bucket=令牌.bucket,
            分片号=分片号,
            mime=mime,
            增量上下文=增量上下文,
        )

        路径 = Path(文件路径)
        with 路径.open("rb") as f:
            f.seek((分片号 - 1) * 分片大小)
            数据 = f.read(片大小)

        return self.OSS直传(授权["url"], 授权["headers"], 数据)
