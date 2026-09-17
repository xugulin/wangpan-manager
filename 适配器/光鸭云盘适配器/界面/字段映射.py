# 光鸭云盘适配器/界面/字段映射.py
"""字段中文映射、状态映射、值格式化"""

from 核心.接口.云添加接口 import 任务状态映射 as 云添加任务状态
from 核心.接口.分享接口 import 分享类型映射, 下载类型映射
from .格式化工具 import 格式化大小, 格式化时长, 格式化时间戳


时间戳字段 = {
    "签发时间戳", "过期时间戳",
    "vipExpireTime", "svipExpireTime",
    "expireTime", "expireAt",
    "createTime", "updateTime", "modifyTime",
    "systemTime", "serverTime", "lastLoginTime",
    "createAt", "updateAt", "gmtCreate", "gmtModified",
    "createdAt", "updatedAt", "lastLoginAt",
    "deleteTime", "shareTime", "actionTime",
    "ctime", "utime", "dtime",
}

字节字段 = {
    "totalSpaceSize", "usedSpaceSize", "freeSpaceSize",
    "totalDirectLinkTraffic", "freeDirectLinkTraffic",
    "usedDirectLinkTraffic",
    "totalShareGuestTraffic", "freeShareGuestTraffic",
    "usedShareGuestTraffic",
    "bigFileSize", "maxFileSize", "fileSize",
    "usedSpace", "totalSpace", "freeSpace",
    "traffic", "totalTraffic", "usedTraffic",
    "size", "fileSizeBytes", "totalSize",
}

秒数字段 = {
    "vipLeftTime", "svipLeftTime", "vipRemainTime",
    "leftTime", "remainTime", "expiresIn",
    "urlDuration",
}

字段中文名 = {
    "sub": "用户 ID", "用户ID": "用户 ID",
    "name": "昵称", "nickName": "昵称", "nickname": "昵称",
    "userName": "用户名", "username": "用户名",
    "phone_number": "手机号", "phone": "手机号",
    "email": "邮箱", "avatar": "头像", "avatar_url": "头像地址",
    "created_at": "注册时间", "createdAt": "注册时间",
    "updated_at": "更新时间", "updatedAt": "更新时间",
    "password_updated_at": "密码修改时间",
    "last_login_at": "最后登录时间",
    "last_login_time": "最后登录时间",
    "lastLoginTime": "最后登录时间",
    "status": "状态",
    "vip_status": "VIP 状态", "vip_expire_time": "VIP 到期时间",
    "svip_status": "SVIP 状态", "svip_expire_time": "SVIP 到期时间",
    "project_id": "项目 ID", "项目ID": "项目 ID",
    "aud": "应用 ID", "应用ID": "应用 ID",
    "iss": "令牌签发方", "签发方": "令牌签发方",
    "scope": "授权范围", "授权范围": "授权范围",
    "iat": "签发时间", "签发时间戳": "签发时间",
    "exp": "过期时间", "过期时间戳": "过期时间",
    "来源": "数据来源", "source": "数据来源", "meta": "元数据",
    "totalSpaceSize": "总空间", "usedSpaceSize": "已用空间",
    "freeSpaceSize": "剩余空间",
    "totalDirectLinkTraffic": "直链总流量",
    "freeDirectLinkTraffic": "直链剩余流量",
    "usedDirectLinkTraffic": "直链已用流量",
    "totalShareGuestTraffic": "分享访客总流量",
    "freeShareGuestTraffic": "分享访客剩余流量",
    "usedShareGuestTraffic": "分享访客已用流量",
    "vipStatus": "VIP 状态", "vipLeftTime": "VIP 剩余时间",
    "vipExpireTime": "VIP 到期时间",
    "svipStatus": "SVIP 状态", "svipLeftTime": "SVIP 剩余时间",
    "svipExpireTime": "SVIP 到期时间",
    "systemTime": "服务器时间", "serverTime": "服务器时间",
    "batchUploadFiles": "单批上传文件数上限",
    "bigFileSize": "单文件大小上限", "maxFileSize": "单文件大小上限",
    "playbackSpeed": "播放倍速上限",
    "directLinkFeature": "支持直链功能",
    "canDownload": "允许下载", "canShare": "允许分享",
    "createTime": "创建时间", "updateTime": "更新时间",
    "modifyTime": "修改时间",
    "fileName": "文件名", "fileId": "文件 ID",
    "fileSize": "文件大小", "parentId": "父目录 ID",
    "parentName": "父目录名",
    "resType": "资源类型",
    "url": "链接", "signedURL": "下载链接",
    "urlDuration": "链接有效期",
    "code": "返回码", "msg": "消息", "data": "数据",
    "list": "列表", "total": "总数",
    "page": "页码", "pageSize": "每页数量", "cursor": "游标",
    "hasMore": "是否还有更多",
    "ctime": "创建时间", "utime": "修改时间", "dtime": "删除时间",
    "gcid": "内容 ID", "depth": "深度",
    "mineType": "MIME 类型", "fileType": "文件类型",
    "dirType": "目录类型", "ext": "扩展名",
    "fullParentIds": "完整父路径 ID",
    "auditStatus": "审核状态", "leftTime": "剩余时间",
    "thumbnail": "缩略图",
    "shareId": "分享 ID", "shareName": "分享名称",
    "shareUrl": "分享链接", "shareTime": "分享时间",
    "title": "标题",
    "expireTime": "过期时间",
    "visitCount": "访问次数", "downloadCount": "下载次数",
    "taskId": "任务 ID", "taskName": "任务名称",
    "taskStatus": "任务状态",
    "restoreId": "回收站项 ID", "deleteTime": "删除时间",
    "originalPath": "原始路径",
    "actionType": "操作类型", "actionTime": "操作时间",
    "actionDesc": "操作描述",
    "actionDetails": "操作明细",
    "collectionId": "批次 ID",
    "totalCount": "总数量",
    "targetName": "目标名称", "targetId": "目标 ID",
    "targetPath": "目标路径",
    "infoHash": "种子哈希",
    "btResInfo": "BT 资源信息",
    "urlResInfo": "直链资源信息",
    "progress": "进度",
    "res": "资源地址",
    "exist": "文件已存在",
    "parentDirType": "父目录类型",
    "verification_id": "验证会话 ID",
    "verification_token": "验证令牌",
    "is_user": "是否已注册用户",
    "selected_channel": "验证通道",
    "captcha_token": "盾令牌",
}

VIP状态映射 = {0: "未开通", 1: "未开通", 2: "已开通", 3: "已过期", 4: "已过期"}
资源类型映射 = {0: "文件", 1: "文件", 2: "目录", 3: "目录"}
任务状态映射_文件 = {0: "等待中", 1: "处理中", 2: "已完成", 3: "失败", 4: "已取消"}
操作类型映射 = {0: "未知", 1: "浏览", 2: "下载", 3: "上传", 4: "删除", 5: "分享"}


def 翻译字段名(键) -> str:
    键 = str(键)
    return 字段中文名.get(键, 键)


def 格式化值(键: str, 值):
    键 = str(键)
    try:
        if 键 in 时间戳字段:
            if isinstance(值, (int, float)) and 值 > 1_000_000_000:
                return f"{格式化时间戳(值)} ({值})"
        if 键 in 字节字段:
            if isinstance(值, (int, float)) and 值 > 0:
                return f"{格式化大小(值)} ({值})"
        if 键 in 秒数字段:
            if isinstance(值, (int, float)) and 值 > 0:
                return f"{格式化时长(值)} ({值} 秒)"
        if 键 in ("vipStatus", "svipStatus", "vip_status", "svip_status"):
            return f"{VIP状态映射.get(值, 值)} (原值 {值})"
        if 键 == "resType":
            return f"{资源类型映射.get(值, 值)} (原值 {值})"
        if 键 in ("taskStatus", "task_status"):
            return f"{任务状态映射_文件.get(值, 值)} (原值 {值})"
        if 键 == "actionType":
            return f"{操作类型映射.get(值, 值)} (原值 {值})"
        if 键 == "status":
            return f"{云添加任务状态.get(值, 值)} (原值 {值})"
        if 键 == "shareType":
            return f"{分享类型映射.get(值, 值)} (原值 {值})"
        if 键 == "downloadType":
            return f"{下载类型映射.get(值, 值)} (原值 {值})"
        if isinstance(值, bool):
            return "是" if 值 else "否"
    except Exception:
        pass
    return 值