# 光鸭云盘适配器/核心/接口/分享接口.py
"""
分享接口
参考 HAR 分析（2026-09-13）：

创建分享：
  POST /userres/v1/share_file
  body: {
    "fileIds": ["..."],               # 必填，文件/目录 ID 列表
    "title": "文件名",                 # 分享标题
    "validateDuration": 86400,        # 0=永久, 86400=1天, 2592000=30天
    "shareType": 0,                   # 0=无密码, 1=随机密码, 2=自定义密码
    "trafficLimit": "0",              # 字符串字节数, "0"=不限
    "maxRestoreCount": 0,             # 0=不限
    "downloadType": 1,                # 0=直链, 1=非直链
    "enableShareCode": false,         # 旧字段（HAR 中恒为 false）
    "shareCode": "",                  # 旧字段（HAR 中恒为空）
    # 可选:
    "autoFillCode": true,             # shareType=1 时是否自动在 URL 带 code
    "code": "8888",                   # shareType=2 时自定义密码
  }
  resp: {
    "msg": "success",
    "data": {
      "shareUrl": "https://www.guangyapan.com/s/{shareId}_{userId}?code=xxx",
      "shareId": "1946224319800246312_aedZqpwocgzRgIb2",
      "id":      "1946224319800246312",
      "createTime": "2026-09-13 21:20:36",
      "code": "cdrs",                 # 有密码时返回
    }
  }

分享模板：
  POST /misc/v1/get_share_template
  resp: {"msg":"success","data":{}}

分享列表：
  POST /userres/v1/get_share_list
  body: {"page": 0, "pageSize": 50}
  resp data.list[i] 关键字段（HAR 确认 2026-09-13）：
    {
      "id": "1946224319800246312",              # ★ 纯数字，取消分享要用这个
      "shareId": "1946224319800246312_aedZqpwocgzRgIb2",
      "createTime": 1789305637,
      "shareStatus": 1,
      "leftTime": 85177,
      "title": "项目配置.toml",
      "shareUrl": "https://www.guangyapan.com/s/...",
      "resType": 1, "fileType": 11,
      "downloadType": 1, "validateDuration": 86400,
      "code": "8888",                           # 有密码时返回
      "shareType": 2
    }

取消分享（HAR 确认 2026-09-13）：
  POST /userres/v1/delete_share                  # ★ 不是 cancel_share
  body: {"ids": ["1946224749737402373"]}         # ★ 字段名是 ids，用纯数字 id
  resp: {"msg":"success"}                        # 无 data
"""
import logging

from ..网络.网络客户端 import 网络客户端

logger = logging.getLogger("光鸭云盘.分享")

# ---------- 枚举常量（与 HAR 一致） ----------

# 分享类型
分享类型_无密码 = 0
分享类型_随机密码 = 1
分享类型_自定义密码 = 2

# 下载类型
下载类型_直链 = 0
下载类型_非直链 = 1

# 有效期（秒）
有效期_永久 = 0
有效期_1天 = 86400
有效期_7天 = 86400 * 7
有效期_30天 = 86400 * 30

# 展示映射
分享类型映射 = {
    0: "无密码",
    1: "随机密码",
    2: "自定义密码",
}
下载类型映射 = {
    0: "直链",
    1: "非直链",
}

# 有效期映射（秒 -> 展示文本）
有效期映射 = {
    0: "永久",
    86400: "1 天",
    86400 * 7: "7 天",
    86400 * 30: "30 天",
    86400 * 90: "90 天",
    86400 * 365: "1 年",
}


class 分享接口:
    """分享相关接口"""

    def __init__(self, 网络: 网络客户端):
        self.网络 = 网络

    # ==================== 模板 ====================

    def 取分享模板(self) -> dict:
        """POST /misc/v1/get_share_template

        返回分享弹窗的动态配置（当前 HAR 返回空对象，使用前端默认）。
        """
        响应 = self.网络.请求(
            "POST",
            "/misc/v1/get_share_template",
            json数据=None,
        )
        return 响应.get("data", {}) if isinstance(响应, dict) else {}

    # ==================== 创建 ====================

    def 创建分享(
        self,
        fileIds: list[str],
        title: str = "",
        shareType: int = 分享类型_无密码,
        validateDuration: int = 有效期_永久,
        downloadType: int = 下载类型_非直链,
        trafficLimit: str = "0",
        maxRestoreCount: int = 0,
        code: str = "",
        autoFillCode: bool = False,
    ) -> dict:
        """创建分享，返回服务端 data 字段

        :param fileIds: 文件/目录 ID 列表
        :param title: 分享标题（一般传文件名）
        :param shareType: 0=无密码, 1=随机密码, 2=自定义密码
        :param validateDuration: 有效期秒数，0=永久
        :param downloadType: 0=直链, 1=非直链
        :param trafficLimit: 字符串字节数，"0"=不限
        :param maxRestoreCount: 最大转存次数，0=不限
        :param code: 自定义密码（仅 shareType=2 时使用）
        :param autoFillCode: 随机密码时是否自动填充到 URL（仅 shareType=1 时使用）
        """
        if not fileIds:
            raise ValueError("fileIds 不能为空")

        请求体: dict = {
            "fileIds": list(fileIds),
            "title": title or "",
            "validateDuration": int(validateDuration),
            "shareType": int(shareType),
            "trafficLimit": str(trafficLimit or "0"),
            "maxRestoreCount": int(maxRestoreCount),
            "downloadType": int(downloadType),
            "enableShareCode": False,
            "shareCode": "",
        }

        if shareType == 分享类型_随机密码:
            请求体["autoFillCode"] = bool(autoFillCode)
        elif shareType == 分享类型_自定义密码:
            请求体["code"] = code or ""
            请求体["autoFillCode"] = False
        # 无密码时不带 code/autoFillCode，与 HAR 保持一致

        logger.info(
            f"[分享] 创建 shareType={shareType} "
            f"downloadType={downloadType} "
            f"validateDuration={validateDuration} "
            f"trafficLimit={trafficLimit} "
            f"files={len(fileIds)}")

        响应 = self.网络.请求(
            "POST",
            "/userres/v1/share_file",
            json数据=请求体,
        )
        数据 = 响应.get("data", 响应) if isinstance(响应, dict) else 响应
        if not isinstance(数据, dict) or "shareUrl" not in 数据:
            raise RuntimeError(f"创建分享失败：{响应}")

        logger.info(
            f"[分享] ✅ 创建成功 shareId={数据.get('shareId')} "
            f"id={数据.get('id')} "
            f"url={数据.get('shareUrl')}")
        return 数据

    # ==================== 列表 ====================

    def 取分享列表(
        self,
        page: int = 0,
        pageSize: int = 50,
    ) -> dict:
        """POST /userres/v1/get_share_list

        响应 data 通常含：
          {"list": [...], "total": N}
        每条记录关键字段：
          id             纯数字 ID —— 取消分享时使用
          shareId        带后缀的分享 ID（用于拼 URL）
          shareUrl / title / shareType / downloadType / code / createTime
        """
        响应 = self.网络.请求(
            "POST",
            "/userres/v1/get_share_list",
            json数据={"page": int(page), "pageSize": int(pageSize)},
        )
        return 响应.get("data", 响应) if isinstance(响应, dict) else 响应

    # ==================== 取消（删除分享）====================

    def 取消分享(self, ids: list[str] | str) -> dict:
        """取消（删除）分享

        参考 HAR（2026-09-13）：
          POST /userres/v1/delete_share          ← 注意是 delete_share
          body: {"ids": ["1946224749737402373"]} ← 字段名是 ids
          resp: {"msg":"success"}                 ← 无 data

        ⚠️ 关键点：
          传入的 **必须是纯数字 id**（列表中每条记录的 `id` 字段），
          而不是带后缀的 `shareId`（如 1946224319800246312_xxxxx）。
          服务端返回 404 Not Found 一般就是因为传了带后缀的 shareId，
          或使用了不存在的接口名 cancel_share。

        :param ids: 分享记录的纯数字 id，单个字符串或字符串列表
        :return: 服务端响应（通常只有 {"msg":"success"}）
        """
        if isinstance(ids, str):
            ids = [ids]
        if not ids:
            raise ValueError("ids 不能为空")

        # 只保留纯数字部分，防止误传带后缀 shareId
        清洗后: list[str] = []
        for 项 in ids:
            字符串 = str(项).strip()
            if not 字符串:
                continue
            # 若形如 "xxx_yyy"，取前缀数字部分
            纯 = 字符串.split("_", 1)[0]
            if 纯:
                清洗后.append(纯)

        if not 清洗后:
            raise ValueError("ids 不能为空")

        logger.info(f"[分享] 取消 ids={清洗后}")

        响应 = self.网络.请求(
            "POST",
            "/userres/v1/delete_share",
            json数据={"ids": 清洗后},
        )
        return 响应.get("data", 响应) if isinstance(响应, dict) else 响应

    # ==================== 便捷方法 ====================

    @staticmethod
    def 生成分享URL(shareId: str, userId: str, code: str = "") -> str:
        """按 HAR 规则拼装分享 URL"""
        url = f"https://www.guangyapan.com/s/{shareId}_{userId}"
        if code:
            url += f"?code={code}"
        return url