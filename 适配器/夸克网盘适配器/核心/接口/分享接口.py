# 夸克网盘适配器/核心/接口/分享接口.py
"""
分享接口（阶段二 2.9）

端点（对齐 QuarkPan `services/share_service.py`）：

    创建分享（三步）
      ① POST share                    body {fid_list,title,url_type,expired_type,[expired_at],[passcode]}
                                      → data.task_id
      ② GET  task?task_id=…           → data.status==2 时含 data.share_id
      ③ POST share/password           body {share_id} → data{share_url,pwd_id,passcode…}

    我的分享    GET  share/mypage/detail
    取消分享    POST share/delete      body {share_id}

    转存（三步，**必须用分享域名** drive.quark.cn）
      ① POST share/sharepage/token    body {pwd_id,passcode,…} → data.stoken
      ② GET  share/sharepage/detail   params {pwd_id,stoken,pdir_fid,…} → data.list
      ③ POST share/sharepage/save     body {fid_list,to_pdir_fid,pwd_id,stoken,…}
                                      → data.task_id → 轮询 GET task

字段约定：
    ⚠️ 夸克有**两套分享 ID**，不要混用（2026-09-14 实测）：
        - `share_id` / `pwd_id`（32 位十六进制，如 `61fdd052e98b42d9933e3a011dfdf4f8`）
          → **管理用**：`share/mypage/detail` 列表、`share/delete` 取消
        - URL slug（12 位，如 `292d917b145c`，出现在 `https://pan.quark.cn/s/<slug>`）
          → **访问用**：`share/sharepage/token` 的 `pwd_id`、转存
        两者**不相等**，`解析分享链接()` 返回的是 slug（供转存），
        `链接ID()` 可从 share_url 提取 slug。

    `url_type`     1=公开链接  2=私密链接（有提取码）
    `expired_type` 1=永久      2=有期限（配 `expired_at` 毫秒时间戳）
    `stoken`       分享页的一次性访问令牌
"""
import logging
import random
import re
import string
import time
from typing import Any, Optional

from ..网络.网络客户端 import 网络客户端, 接口错误, 共享基础地址
from .任务接口 import 任务接口

logger = logging.getLogger("夸克网盘.分享")

分享链接正则 = [
    re.compile(r"https?://pan\.quark\.cn/s/([0-9a-zA-Z]+)"),
    re.compile(r"https?://pan\.quark\.cn/s/([0-9a-zA-Z]+).*?密码[：: ]?\s*([0-9a-zA-Z]+)"),
    re.compile(r"quark://share/([0-9a-zA-Z]+)"),
]
提取码正则 = [
    re.compile(r"(?:密码|提取码|访问码|code)[：: ]?\s*([0-9a-zA-Z]{4})"),
]


def 随机提取码(长度: int = 4) -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits,
                                  k=长度))


class 分享接口:
    """夸克分享：创建 / 列表 / 取消 / 解析 / 转存"""

    def __init__(self, 网络: 网络客户端):
        self.网络 = 网络
        self.任务 = 任务接口(网络)

    # ==================== 创建 ====================

    def 创建分享(
        self,
        fileIds: str | list[str] | None = None,
        title: str = "",
        有效期天: int = 0,
        密码: Optional[str] = None,
        等待: bool = True,
        最长等待秒: float = 120.0,
        **兼容参数: Any,
    ) -> dict:
        """创建分享链接

        :param fileIds: 文件/目录 fid 列表
        :param 有效期天: 0 = 永久
        :param 密码: 提取码；None 表示公开分享
        :return: 夸克原生详情 dict，含 `share_id` / `share_url` / `passcode` / `pwd_id`

        兼容旧调用方（原适配器的关键字）：
          `validateDuration`(秒) → 有效期天；`shareType`/`code` → 密码；
          `downloadType`/`trafficLimit`/`maxRestoreCount`/`autoFillCode` 会被忽略
          （夸克无对应能力，见 PLAN.md §4.5-D，阶段三会从对话框移除）。
        """
        if not fileIds:
            fileIds = 兼容参数.get("file_ids") or []
        标识 = [fileIds] if isinstance(fileIds, str) else list(fileIds)
        if not 标识:
            raise 接口错误("创建分享失败：fileIds 不能为空")

        # ---- 兼容旧参数 ----
        分享类型 = 兼容参数.get("shareType")
        秒数 = 兼容参数.get("validateDuration")
        if 秒数 is not None:
            try:
                有效期天 = int(秒数) // 86400
            except (TypeError, ValueError):
                pass
        if 密码 is None:
            if 分享类型 == 2:
                密码 = 兼容参数.get("code") or None
            elif 分享类型 == 1:
                密码 = 随机提取码()      # 随机密码：本地生成后当私密分享
        for 忽略 in ("downloadType", "trafficLimit",
                     "maxRestoreCount", "autoFillCode", "file_ids"):
            if 忽略 in 兼容参数:
                logger.debug(f"[分享] 忽略夸克不支持的参数：{忽略}")

        请求体: dict = {
            "fid_list": [str(x) for x in 标识],
            "title": title or "",
            "url_type": 2 if 密码 else 1,
            "expired_type": 1 if not 有效期天 else 2,
        }
        if 有效期天:
            请求体["expired_at"] = int(
                (time.time() + int(有效期天) * 86400) * 1000)
        if 密码:
            请求体["passcode"] = 密码

        logger.info(
            f"[分享] 创建分享 files={len(标识)} 标题={title!r} "
            f"url_type={请求体['url_type']} 有效={有效期天}天")
        响应 = self.网络.请求("POST", "share", json数据=请求体)
        数据 = 响应.get("data", 响应) if isinstance(响应, dict) else {}
        taskId = str((数据 or {}).get("task_id") or "")
        if not taskId:
            raise 接口错误(f"创建分享未返回 task_id：{数据}")

        if not 等待:
            return {"task_id": taskId}

        完成 = self.任务.等待任务完成(
            taskId, 最长等待秒=最长等待秒, 描述="创建分享")
        shareId = str(完成.get("share_id") or "")
        if not shareId:
            raise 接口错误(f"分享任务完成但未返回 share_id：{完成}")

        return self.取分享详情(shareId)

    def 取分享详情(self, shareId: str) -> dict:
        """POST share/password —— 取分享详情（含最终 share_url / passcode）"""
        响应 = self.网络.请求(
            "POST", "share/password", json数据={"share_id": str(shareId)})
        数据 = 响应.get("data", 响应) if isinstance(响应, dict) else {}
        if not isinstance(数据, dict):
            数据 = {}
        数据.setdefault("share_id", str(shareId))
        logger.info(
            f"[分享] 详情 share_id={数据.get('share_id')} "
            f"url={数据.get('share_url')}")
        return 数据

    # ==================== 列表 / 取消 ====================

    def 取分享列表(self, page: int = 1, pageSize: int = 50) -> dict:
        """GET share/mypage/detail

        :param page: 页码，**从 1 开始**（传 0 会被当作 1）
        :return: {"list": [...], "total": N, "metadata": {...}}
        """
        响应 = self.网络.请求(
            "GET", "share/mypage/detail",
            params={
                "_page": max(1, int(page or 1)),
                "_size": int(pageSize),
                "_order_field": "created_at",
                "_order_type": "desc",
                "_fetch_total": 1,
                "_fetch_notify_follow": 1,
            })
        if not isinstance(响应, dict):
            return {"list": [], "total": 0, "metadata": {}}
        数据 = 响应.get("data") or {}
        元数据 = 响应.get("metadata") or {}
        列表 = 数据.get("list") or []
        if not isinstance(列表, list):
            列表 = []
        总数 = 元数据.get("_total")
        if 总数 is None:
            总数 = 数据.get("total") or len(列表)
        return {"list": 列表, "total": int(总数 or 0), "metadata": 元数据}

    def _存活的分享ID(self, 页数: int = 3) -> set[str]:
        """拉取我的分享列表，返回仍然存在的 share_id 集合（用于校验删除）"""
        存活: set[str] = set()
        for 页 in range(max(1, 页数)):
            try:
                数据 = self.取分享列表(page=页, pageSize=100)
            except Exception:
                break
            列表 = 数据.get("list") or []
            if not 列表:
                break
            for 项 in 列表:
                if isinstance(项, dict) and 项.get("share_id"):
                    存活.add(str(项["share_id"]))
            if len(列表) < 100:
                break
        return 存活

    def 取消分享(self, shareIds: str | list[str],
                 等待: bool = False,
                 校验: bool = True) -> list[dict]:
        """POST share/delete —— 取消分享

        ⚠️⚠️ **实测坑（2026-09-14，A/B 对照实验查明）**：
        `share/delete` 的字段名必须是 **`share_ids`（数组）**。

            正确：{"share_ids": ["<32位hex>"]}      → 真正删除 ✅
            错误：{"share_id":  "<32位hex>"}        → 返回
                   {"status":200,"code":0,"message":"ok"} 但**什么都没删**，
                   是个**静默 no-op**（旧实现就是踩了这个）
            错误：{"share_id": ["<32位hex>"]}       → 500 inner error

        正因为这个接口会「假成功」，本方法默认 `校验=True`：
        删除后回查分享列表，把其实没删掉的如实标成 `已删除=False`，
        而不是无脑 log 一句「已取消」骗自己。

        :param 等待: 兼容旧签名，当前未使用
        :param 校验: 是否回查列表确认（列表有滞后，会重试几次）
        """
        _ = 等待
        标识 = [shareIds] if isinstance(shareIds, str) else list(shareIds)
        标识 = [str(s) for s in 标识 if s]
        if not 标识:
            return []

        logger.info(f"[分享] 请求取消 {len(标识)} 条分享")
        响应 = self.网络.请求(
            "POST", "share/delete",
            json数据={"share_ids": 标识})
        数据 = 响应.get("data", 响应) if isinstance(响应, dict) else {}
        logger.info(f"[分享] share/delete 返回：{数据}")

        if not 校验:
            return [{"share_id": s, "结果": 数据, "已删除": None} for s in 标识]

        # 列表有滞后，重试几次再下结论
        未删 = list(标识)
        for 尝试 in range(4):
            time.sleep(1.5)
            存活 = self._存活的分享ID()
            未删 = [s for s in 标识 if s in 存活]
            if not 未删:
                break

        if 未删:
            logger.error(f"[分享] ❌ 服务端返回成功，但这些分享仍然存在：{未删}")
        else:
            logger.info(f"[分享] ✅ 已确认取消 {len(标识)} 条分享")

        return [{"share_id": s, "结果": 数据, "已删除": s not in 未删}
                for s in 标识]

    # ==================== 解析 ====================

    @staticmethod
    def 解析分享链接(链接: str) -> tuple[str, Optional[str]]:
        """从分享链接/分享文案里解析出 (share_id, 提取码)

        支持：
            https://pan.quark.cn/s/abcdef123456
            https://pan.quark.cn/s/abcdef123456 密码：1234
            quark://share/abcdef123456
        """
        文本 = (链接 or "").strip()
        if not 文本:
            raise 接口错误("分享链接为空")

        shareId = ""
        for 模式 in 分享链接正则:
            匹配 = 模式.search(文本)
            if 匹配:
                shareId = 匹配.group(1)
                if 匹配.lastindex and 匹配.lastindex >= 2 and 匹配.group(2):
                    return shareId, 匹配.group(2)
                break
        if not shareId:
            # 兜底：直接给了 share_id
            if re.fullmatch(r"[0-9a-zA-Z]{8,}", 文本):
                return 文本, None
            raise 接口错误(f"无法识别的分享链接：{文本[:120]}")

        提取码 = None
        for 模式 in 提取码正则:
            匹配 = 模式.search(文本)
            if 匹配:
                提取码 = 匹配.group(1)
                break
        return shareId, 提取码

    @staticmethod
    def 生成分享URL(shareId: str) -> str:
        """夸克分享链接格式：https://pan.quark.cn/s/<share_id>"""
        return f"https://pan.quark.cn/s/{shareId}"

    @staticmethod
    def 链接ID(shareUrl: str) -> str:
        """从 share_url 里提取公开 ID（slug，即转存用的 pwd_id）

        ⚠️ 与 `share_id`（32 位十六进制，管理用）**不是同一个值**。
        """
        路径 = str(shareUrl or "").split("?")[0].rstrip("/")
        return 路径.split("/")[-1] if 路径 else ""

    # ==================== 转存 ====================

    def 取分享Token(self, shareId: str,
                    密码: Optional[str] = None) -> str:
        """POST share/sharepage/token（分享域名） → stoken"""
        响应 = self.网络.请求(
            "POST", "share/sharepage/token",
            json数据={
                "pwd_id": str(shareId),
                "passcode": 密码 or "",
                "support_visit_limit_private_share": True,
            },
            基础地址=共享基础地址)
        数据 = 响应.get("data", 响应) if isinstance(响应, dict) else {}
        stoken = str((数据 or {}).get("stoken") or "")
        if not stoken:
            raise 接口错误(
                f"未取到 stoken（提取码可能不正确）：{数据}")
        logger.info(f"[分享] 已取到 stoken（share_id={shareId}）")
        return stoken

    def 取分享内容(
        self,
        shareId: str,
        token: str,
        pdirFid: str = "0",
        page: int = 1,
        pageSize: int = 50,
    ) -> dict:
        """GET share/sharepage/detail（分享域名） → 分享内的文件列表"""
        响应 = self.网络.请求(
            "GET", "share/sharepage/detail",
            params={
                "pwd_id": str(shareId),
                "stoken": token,
                "pdir_fid": str(pdirFid or "0"),
                "force": 0,
                "_page": max(1, int(page or 1)),
                "_size": int(pageSize),
                "_fetch_banner": 1,
                "_fetch_share": 1,
                "_fetch_total": 1,
                "_sort": "file_type:asc,file_name:asc",
            },
            基础地址=共享基础地址)
        数据 = 响应.get("data") or {}
        列表 = 数据.get("list") or []
        return {"list": 列表 if isinstance(列表, list) else [],
                "total": int(数据.get("total") or len(列表)),
                "share": 数据.get("share") or {}}

    def 转存分享(
        self,
        shareId: str,
        token: str,
        fileIds: Optional[list[str]] = None,
        目标目录ID: str = "0",
        全部: bool = False,
        等待: bool = True,
        最长等待秒: float = 120.0,
    ) -> dict:
        """POST share/sharepage/save（分享域名） → task_id → 轮询完成

        :param 全部: True 时转存分享内全部内容（pdir_save_all）
        :return: {"task_id":…, "task_result":…}
        """
        标识 = [str(x) for x in (fileIds or [])]
        if not 标识 and not 全部:
            raise 接口错误("转存失败：fileIds 为空且未指定「全部转存」")

        请求体 = {
            "fid_list": 标识,
            "fid_token_list": [],
            "to_pdir_fid": str(目标目录ID or "0"),
            "pwd_id": str(shareId),
            "stoken": token,
            "pdir_fid": "0",
            "pdir_save_all": bool(全部),
            "exclude_fids": [],
            "scene": "link",
        }
        响应 = self.网络.请求(
            "POST", "share/sharepage/save",
            json数据=请求体, 基础地址=共享基础地址)
        数据 = 响应.get("data", 响应) if isinstance(响应, dict) else {}
        taskId = str((数据 or {}).get("task_id") or "")
        if not taskId:
            raise 接口错误(f"转存未返回 task_id：{数据}")
        logger.info(f"[分享] 转存任务已创建 task_id={taskId}")

        if not 等待:
            return {"task_id": taskId}
        完成 = self.任务.等待任务完成(
            taskId, 最长等待秒=最长等待秒, 描述="转存",
            不可重试关键字=("permission denied", "access denied",
                            "forbidden", "unauthorized",
                            "file not found", "share expired",
                            "share not found"))
        return {"task_id": taskId, "task_result": 完成}

    def 一键转存(
        self,
        分享链接: str,
        目标目录ID: str = "0",
        密码: Optional[str] = None,
        等待: bool = True,
    ) -> dict:
        """从分享链接一路转存（解析 → token → 全部转存）"""
        shareId, 链接提取码 = self.解析分享链接(分享链接)
        token = self.取分享Token(shareId, 密码 or 链接提取码)
        结果 = self.转存分享(shareId, token, 目标目录ID=目标目录ID,
                             全部=True, 等待=等待)
        结果["share_id"] = shareId
        return 结果

    # ==================== 条目辅助 ====================

    @staticmethod
    def 规范分享(项: dict) -> dict:
        """在夸克分享条目上补充派生字段（不改动原始键）

        派生：`分享链接` / `提取码` / `创建时间戳` / `是否有效`
        """
        if not isinstance(项, dict):
            return {}
        shareId = str(项.get("share_id") or 项.get("pwd_id") or "")
        return {
            **项,
            "分享链接": 项.get("share_url")
                        or (分享接口.生成分享URL(shareId) if shareId else ""),
            "提取码": 项.get("passcode") or "",
            "创建时间戳": 项.get("created_at"),
            "是否有效": 项.get("status") == 1,
        }
