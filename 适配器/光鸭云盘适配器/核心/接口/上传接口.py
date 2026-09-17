# 光鸭云盘适配器/核心/接口/上传接口.py
"""
上传接口
参考真实抓包（2026-09-13，www.guangyapan.com）：

完整流程：
  1) POST /userres/v2/get_res_center_token
        body: {"capacity":30,"name":"文件名","res":{"fileSize":N,"md5":"..."},"parentId":"..."}
        resp（普通上传）：
            {"msg":"success","data":{
                "gcid","provider",
                "creds":{"accessKeyID","secretAccessKey","sessionToken","expiration"},
                "endPoint","bucketName","objectPath","region",
                "taskId","fullEndPoint","callbackVar"}}
        resp（秒传命中）：
            {"code":156,"msg":"上传已完成","data":{"taskId":"..."}}
            ← 注意：此时没有 creds / bucketName / objectPath，
              因为服务端已经缓存了相同 MD5 的文件，无需客户端再上传
  2) PUT https://<fullEndPoint>/<objectPath>
        headers: x-oss-date, x-oss-security-token, x-oss-user-agent,
                 Authorization（OSS V1 签名）
        resp: ETag
        【秒传命中时跳过此步】
  3) POST /userres/v1/check_can_flash_upload
        body: {"taskId":"...","gcid":"...","cid":"..."}
        resp: {"msg":"success","data":{"taskId":"..."}}
        【秒传命中时跳过此步】
  4) POST /userres/v1/file/get_info_by_task_id
        body: {"taskId":"..."}
        resp: {"msg":"success","data":{fileId,fileName,...}}
             或 {"code":147,"msg":"文件上传中"}  ← 需要轮询
        【两种路径都要走此步】

  目录创建：
  POST /userres/v1/file/create_dir
        body: {"parentId":"...","dirName":"...","failIfNameExist":true}
        resp: {"msg":"success","data":{"fileId":"..."}}

OSS V1 签名规则（关键）：
  StringToSign =
      HTTP-Verb + "\n"
      + Content-MD5 + "\n"
      + Content-Type + "\n"
      + Date + "\n"        ← 若用 x-oss-date，则 Date 位置填 x-oss-date 的值
      + CanonicalizedOSSHeaders
      + CanonicalizedResource

  其中：
    - CanonicalizedOSSHeaders：所有 x-oss-* 头，名字小写、字典序、value 去首尾空格，
      每行以 \n 结尾（包含 x-oss-date 本身）
    - CanonicalizedResource：/<bucketName>/<objectPath>
    - Authorization = "OSS " + AccessKeyId + ":" + base64(HMAC-SHA1(AKSecret, StringToSign))
"""
import base64
import hashlib
import hmac
import logging
import mimetypes
import os
import time
from datetime import datetime, timezone
from email.utils import format_datetime
from hashlib import sha1
from pathlib import Path
from typing import Callable, Optional

import httpx

from ..网络.网络客户端 import 网络客户端

logger = logging.getLogger("光鸭云盘.上传")

# 与 HAR 中 get_res_center_token 请求体保持一致
CAPACITY = 30
OSS_USER_AGENT = (
    "aliyun-sdk-js/6.23.0 Chrome 151.0.0.0 on Linux 64-bit"
)

# HAR 中的浏览器 UA
BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)

轮询间隔秒 = 1.0
单文件最长等待秒 = 300.0

# 分片上传阈值（大于此大小才使用分片）
分片阈值 = 100 * 1024 * 1024  # 100MB
默认分片大小 = 10 * 1024 * 1024  # 10MB

# 光鸭秒传命中的业务码
秒传命中码 = 156


def _生成OSS日期() -> str:
    """生成 OSS 需要的 HTTP 日期（RFC 1123 格式，强制英文）

    不能用 time.strftime("%a, %d %b %Y %H:%M:%S GMT")，
    因为在中文 locale 下 %a / %b 会输出「日」「9月」等中文，
    导致 httpx 抛 UnicodeEncodeError: 'ascii' codec。
    """
    return format_datetime(datetime.now(timezone.utc), usegmt=True)


def _推断MIME类型(文件名: str) -> str:
    """按扩展名推断 MIME 类型（HAR 中 Content-Type: application/toml）"""
    类型, _编码 = mimetypes.guess_type(文件名)
    return 类型 or "application/octet-stream"


def _md5_文件(路径: Path, 进度回调=None) -> str:
    """分块计算文件MD5"""
    m = hashlib.md5()
    总大小 = 路径.stat().st_size
    已读 = 0
    with 路径.open("rb") as f:
        for 块 in iter(lambda: f.read(2 * 1024 * 1024), b""):
            m.update(块)
            已读 += len(块)
            if 进度回调 and 总大小 > 0:
                进度回调(已读, 总大小)
    return m.hexdigest()


def _计算OSS签名(
    method: str,
    content_md5: str,
    content_type: str,
    date: str,
    oss_headers: dict,
    bucket: str,
    object_path: str,
    access_key_id: str,
    access_key_secret: str,
) -> str:
    """阿里云 OSS V1 签名（STS 场景）

    StringToSign =
        HTTP-Verb + "\\n" +
        Content-MD5 + "\\n" +
        Content-Type + "\\n" +
        Date + "\\n" +
        CanonicalizedOSSHeaders +
        CanonicalizedResource

    ⚠️ 关键点：
      - Date 位置填 **x-oss-date 的值**（不是留空！）
      - CanonicalizedOSSHeaders：所有 x-oss-* 头，按名字小写、字典序，
        value 去首尾空格，每行以 \\n 结尾（包含 x-oss-date 本身）
      - CanonicalizedResource = "/<bucket>/<objectPath>"

    返回："OSS <AccessKeyId>:<Signature>"
    """
    # 1. CanonicalizedOSSHeaders
    排序后 = sorted(
        (str(k).lower(), str(v).strip())
        for k, v in oss_headers.items()
        if str(k).lower().startswith("x-oss-")
    )
    canonical_oss_headers = "".join(
        f"{k}:{v}\n" for k, v in 排序后
    )

    # 2. CanonicalizedResource
    canonical_resource = f"/{bucket}/{object_path.lstrip('/')}"

    # 3. StringToSign
    string_to_sign = (
        f"{method}\n"
        f"{content_md5}\n"
        f"{content_type}\n"
        f"{date}\n"
        f"{canonical_oss_headers}"
        f"{canonical_resource}"
    )

    # 4. HMAC-SHA1 签名
    h = hmac.new(
        access_key_secret.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        sha1,
    )
    signature = base64.b64encode(h.digest()).decode("ascii")

    logger.debug(
        f"[OSS] StringToSign >>>\n{string_to_sign}\n<<<")
    logger.debug(f"[OSS] Signature = {signature}")

    return f"OSS {access_key_id}:{signature}"


class 上传接口:
    """光鸭云盘上传接口"""

    def __init__(self, 网络: 网络客户端):
        self.网络 = 网络

    # ==================== 目录 ====================

    def 创建目录(self, parentId: str, dirName: str,
                 failIfNameExist: bool = True) -> str:
        """创建目录，返回 fileId

        HAR 真实请求体（2026-09-13）：
            {"parentId":"...","dirName":"...","failIfNameExist":true}
        注意：使用 dirName 字段，不是 fileName！
        """
        响应 = self.网络.请求(
            "POST",
            "/userres/v1/file/create_dir",
            json数据={
                "parentId": parentId or "",
                "dirName": dirName,
                "failIfNameExist": failIfNameExist,
            },
        )
        数据 = 响应.get("data", 响应) if isinstance(响应, dict) else 响应
        if not isinstance(数据, dict) or "fileId" not in 数据:
            raise RuntimeError(f"创建目录失败：{响应}")
        return str(数据["fileId"])

    def 确保目录(self, 父目录ID: str, 相对路径: str) -> str:
        """
        逐级创建目录，返回最终目录 fileId。
        相对路径形如 'a'、'a/b'、'a/b/c'。
        空串或 '.' 或 '/' 表示返回父目录本身。
        """
        if not 相对路径 or 相对路径 in (".", "/"):
            return 父目录ID or ""

        当前父ID = 父目录ID or ""
        for 段 in 相对路径.replace("\\", "/").split("/"):
            段 = 段.strip()
            if not 段:
                continue
            当前父ID = self.创建目录(当前父ID, 段)
        return 当前父ID

    # ==================== 上传令牌 ====================

    def 取上传令牌(self, 名称: str, 大小: int, md5: str,
                   父目录ID: str) -> dict:
        """POST /userres/v2/get_res_center_token

        返回：服务端 data 字段，并注入 _秒传 标记。

        两种情况：
          A. 普通上传：data 含 creds / bucketName / objectPath / taskId ...
          B. 秒传命中（code=156, msg='上传已完成'）：data 只有 taskId

        为避免上层误判，返回 dict 里注入：
            _秒传: bool
        """
        响应 = self.网络.请求(
            "POST",
            "/userres/v2/get_res_center_token",
            json数据={
                "capacity": CAPACITY,
                "name": 名称,
                "res": {"fileSize": 大小, "md5": md5},
                "parentId": 父目录ID or "",
            },
        )
        数据 = 响应.get("data", 响应) if isinstance(响应, dict) else 响应
        code = 响应.get("code") if isinstance(响应, dict) else None
        msg = 响应.get("msg", "") if isinstance(响应, dict) else ""

        if not isinstance(数据, dict):
            raise RuntimeError(f"取上传令牌响应异常：{响应}")

        # ⚡ 秒传命中：只返回 taskId，无 creds
        if code == 秒传命中码 or msg == "上传已完成":
            if "taskId" not in 数据:
                raise RuntimeError(f"秒传响应缺少 taskId：{响应}")
            数据 = dict(数据)
            数据["_秒传"] = True
            logger.info(
                f"[上传] ⚡ 秒传命中 md5={md5} taskId={数据['taskId']}")
            return 数据

        # 普通上传：必须带 creds
        if "creds" not in 数据:
            raise RuntimeError(f"取上传令牌失败：{响应}")

        数据 = dict(数据)
        数据["_秒传"] = False
        return 数据

    # ==================== OSS 直传 ====================

    def _构造OSS请求头(
        self,
        令牌: dict,
        文件名: str = "",
        content_length: int | None = None,
        content_type: str = "",
    ) -> dict:
        """构造 OSS PUT 请求头，含 V1 签名（Authorization）
        """
        # 1. 先准备所有 x-oss-* 头（要参与签名）
        oss_headers = {
            "x-oss-date": _生成OSS日期(),
            "x-oss-security-token": 令牌["creds"]["sessionToken"],
            "x-oss-user-agent": OSS_USER_AGENT,
        }

        # 2. 决定 Content-Type
        final_content_type = content_type
        if not final_content_type and 文件名:
            final_content_type = _推断MIME类型(文件名)
        if not final_content_type:
            final_content_type = "application/octet-stream"

        # 3. 计算签名（Date 位置填 x-oss-date 的值）
        authorization = _计算OSS签名(
            method="PUT",
            content_md5="",
            content_type=final_content_type,
            date=oss_headers["x-oss-date"],
            oss_headers=oss_headers,
            bucket=令牌["bucketName"],
            object_path=令牌["objectPath"],
            access_key_id=令牌["creds"]["accessKeyID"],
            access_key_secret=令牌["creds"]["secretAccessKey"],
        )

        # 4. 组装最终请求头
        请求头 = {
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.8",
            "Origin": "https://www.guangyapan.com",
            "Referer": "https://www.guangyapan.com/",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-site",
            "Sec-GPC": "1",
            "sec-ch-ua": (
                '"Not=A?Brand";v="99", "Brave";v="151", '
                '"Chromium";v="151"'
            ),
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Linux"',
            "User-Agent": BROWSER_UA,
            "Content-Type": final_content_type,
            **oss_headers,
            "Authorization": authorization,
        }

        if content_length is not None:
            请求头["Content-Length"] = str(content_length)

        return 请求头

    def OSS直传(
        self,
        令牌: dict,
        数据: bytes,
        文件名: str = "",
        content_type: str = "",
    ) -> str:
        """PUT 到 OSS，返回 ETag。"""
        完整地址 = f"{令牌['fullEndPoint']}/{令牌['objectPath']}"

        请求头 = self._构造OSS请求头(
            令牌,
            文件名=文件名,
            content_length=len(数据),
            content_type=content_type,
        )

        logger.info(f"[上传] PUT {完整地址} size={len(数据)}B")

        响应 = self.网络.原始PUT(完整地址, 数据, 请求头)

        if 响应.status_code >= 400:
            try:
                body = 响应.text[:2000]
            except Exception:
                body = "<无法读取>"
            logger.error(
                f"[上传] OSS {响应.status_code} {响应.reason_phrase}\n"
                f"  响应头：{dict(响应.headers)}\n"
                f"  响应体：{body}")
            响应.raise_for_status()

        etag = 响应.headers.get("ETag", "")
        logger.info(f"[上传] OSS 完成 ETag={etag}")
        return etag

    def OSS直传流式(
        self,
        令牌: dict,
        文件路径: Path,
        content_type: str = "",
        进度回调=None,
    ) -> str:
        """流式 PUT 到 OSS（适合大文件），返回 ETag。"""
        完整地址 = f"{令牌['fullEndPoint']}/{令牌['objectPath']}"
        文件大小 = 文件路径.stat().st_size

        请求头 = self._构造OSS请求头(
            令牌,
            文件名=文件路径.name,
            content_length=文件大小,
            content_type=content_type,
        )

        logger.info(f"[上传] PUT {完整地址} size={文件大小}B (流式)")

        已发送 = 0

        def 生成器():
            nonlocal 已发送
            with 文件路径.open("rb") as f:
                while True:
                    块 = f.read(1024 * 1024)
                    if not 块:
                        break
                    已发送 += len(块)
                    if 进度回调:
                        进度回调(已发送, 文件大小)
                    yield 块

        with httpx.Client(
            timeout=httpx.Timeout(
                connect=30.0, read=3600.0,
                write=3600.0, pool=30.0),
            http2=False,
        ) as 客户端:
            响应 = 客户端.put(
                完整地址,
                content=生成器(),
                headers=请求头,
            )

        if 响应.status_code >= 400:
            try:
                body = 响应.text[:2000]
            except Exception:
                body = "<无法读取>"
            logger.error(
                f"[上传] OSS {响应.status_code} {响应.reason_phrase}\n"
                f"  响应体：{body}")
            响应.raise_for_status()

        etag = 响应.headers.get("ETag", "")
        logger.info(f"[上传] OSS 完成 ETag={etag}")
        return etag

    # ==================== 秒传检查 ====================

    def 检查秒传(self, taskId: str,
                 gcid: str = "", cid: str = "") -> str:
        """POST /userres/v1/check_can_flash_upload，返回服务端确认的 taskId"""
        响应 = self.网络.请求(
            "POST",
            "/userres/v1/check_can_flash_upload",
            json数据={"taskId": taskId, "gcid": gcid, "cid": cid},
        )
        数据 = 响应.get("data", 响应) if isinstance(响应, dict) else 响应
        if not isinstance(数据, dict) or "taskId" not in 数据:
            raise RuntimeError(f"秒传检查失败：{响应}")
        return str(数据["taskId"])

    # ==================== 轮询 ====================

    def 等待任务完成(self, taskId: str,
                     超时秒: float = 单文件最长等待秒,
                     间隔秒: float = 轮询间隔秒) -> dict:
        """
        POST /userres/v1/file/get_info_by_task_id
        code=147 表示还在上传中；msg=success 且带 data 表示成功。
        """
        开始 = time.time()
        路径 = "/userres/v1/file/get_info_by_task_id"
        while True:
            响应 = self.网络.请求(
                "POST", 路径, json数据={"taskId": taskId})
            if isinstance(响应, dict) and 响应.get("msg") == "success" \
                    and 响应.get("data"):
                return 响应["data"]
            if isinstance(响应, dict) and 响应.get("code") == 147:
                if time.time() - 开始 >= 超时秒:
                    raise TimeoutError(
                        f"上传任务 {taskId} 等待超时（{超时秒}s）")
                time.sleep(间隔秒)
                continue
            raise RuntimeError(f"上传任务 {taskId} 异常：{响应}")

    # ==================== 单文件上传 ====================

    def 上传文件(
        self,
        本地路径: str,
        父目录ID: str,
        进度回调: Optional[Callable[[str, float], None]] = None,
    ) -> dict:
        """
        上传单个文件，返回服务端 file_info。
        进度回调(阶段, 0~1)

        流程：
          1. 计算 MD5
          2. 取上传令牌
             - 若服务端返回 code=156（秒传命中）：
               跳过步骤 3、4，直接进步骤 5
             - 否则继续
          3. PUT 直传到 OSS（带 OSS V1 签名）
          4. 检查秒传
          5. 轮询等待任务完成
        """
        路径 = Path(本地路径)
        if not 路径.is_file():
            raise FileNotFoundError(f"不是文件：{本地路径}")
        文件名 = 路径.name
        大小 = 路径.stat().st_size

        def 报告(阶段: str, p: float):
            if 进度回调:
                进度回调(阶段, max(0.0, min(1.0, p)))

        # 1. 计算 md5
        报告("计算 MD5", 0.0)
        md5 = _md5_文件(路径, 进度回调=lambda 已读, 总: 报告(
            "计算 MD5", 0.05 * (已读 / 总) if 总 > 0 else 0.05))

        # 2. 取上传令牌
        报告("取上传令牌", 0.1)
        令牌 = self.取上传令牌(文件名, 大小, md5, 父目录ID)
        taskId = str(令牌["taskId"])

        # ============ 秒传命中：跳过 OSS 上传 ============
        if 令牌.get("_秒传"):
            logger.info(
                f"[上传] ⚡ {文件名} 秒传命中 taskId={taskId}，"
                f"跳过 OSS PUT，直接领取文件信息")
            报告("秒传命中", 0.9)
            信息 = self.等待任务完成(taskId)
            报告("完成", 1.0)
            logger.info(
                f"[上传] ✅ {文件名} fileId={信息.get('fileId')} (秒传)")
            return 信息

        # ============ 普通上传 ============
        logger.info(f"[上传] {文件名} taskId={taskId} "
                    f"gcid={令牌.get('gcid')} bucket={令牌.get('bucketName')} "
                    f"endPoint={令牌.get('fullEndPoint')}")

        # 3. OSS 直传
        报告("直传 OSS", 0.15)
        if 大小 > 分片阈值:
            etag = self.OSS直传流式(
                令牌, 路径,
                进度回调=lambda 已发, 总: 报告(
                    "直传 OSS",
                    0.15 + 0.7 * (已发 / 总) if 总 > 0 else 0.85))
        else:
            数据 = 路径.read_bytes()
            etag = self.OSS直传(令牌, 数据, 文件名=文件名)

        # 4. 秒传检查（失败忽略）
        报告("服务端校验", 0.9)
        try:
            gcid = etag.strip('"') if etag else 令牌.get("gcid", "")
            taskId = self.检查秒传(taskId, gcid=gcid, cid="")
        except Exception as e:
            logger.warning(f"[上传] 秒传检查失败，忽略：{e}")

        # 5. 轮询等待任务完成
        报告("等待服务端确认", 0.95)
        信息 = self.等待任务完成(taskId)
        报告("完成", 1.0)
        logger.info(f"[上传] ✅ {文件名} fileId={信息.get('fileId')}")
        return 信息

    # ==================== 文件夹上传 ====================

    def 上传文件夹(
        self,
        本地目录: str,
        父目录ID: str,
        进度回调: Optional[Callable[[str, float], None]] = None,
        单文件回调: Optional[Callable[[str, dict], None]] = None,
    ) -> list[dict]:
        """递归上传整个文件夹。"""
        根 = Path(本地目录).resolve()
        if not 根.is_dir():
            raise NotADirectoryError(f"不是目录：{本地目录}")

        顶层名 = 根.name
        顶层ID = self.创建目录(父目录ID, 顶层名)
        logger.info(f"[上传] 创建顶层目录 '{顶层名}' fileId={顶层ID}")

        所有文件: list[tuple[Path, str]] = []
        for 当前目录, _子目录, 文件列表 in os.walk(根):
            相对 = Path(当前目录).relative_to(根)
            相对字符串 = (
                "" if str(相对) == "."
                else str(相对).replace("\\", "/")
            )
            for f in 文件列表:
                所有文件.append((Path(当前目录) / f, 相对字符串))

        总数 = len(所有文件)
        if 总数 == 0:
            return []

        目录缓存: dict[str, str] = {"": 顶层ID}
        结果: list[dict] = []

        for 序号, (文件路径, 相对目录) in enumerate(所有文件):
            if 相对目录 not in 目录缓存:
                父ID = 目录缓存[""]
                当前段累积 = ""
                for 段 in 相对目录.split("/"):
                    当前段累积 = (
                        f"{当前段累积}/{段}" if 当前段累积 else 段
                    )
                    if 当前段累积 in 目录缓存:
                        父ID = 目录缓存[当前段累积]
                        continue
                    子ID = self.创建目录(父ID, 段)
                    目录缓存[当前段累积] = 子ID
                    父ID = 子ID
            文件父ID = 目录缓存[相对目录]

            基 = 序号 / 总数
            范围 = 1 / 总数
            文件名 = 文件路径.name

            def 子进度(阶段: str, p: float,
                       _b=基, _r=范围, _n=文件名):
                if 进度回调:
                    进度回调(f"{_n} - {阶段}", _b + p * _r)

            信息 = self.上传文件(
                str(文件路径), 文件父ID, 进度回调=子进度)
            结果.append(信息)
            if 单文件回调:
                单文件回调(文件名, 信息)

        return 结果