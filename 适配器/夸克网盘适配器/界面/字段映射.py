# 夸克网盘适配器/界面/字段映射.py
"""字段中文映射、状态映射、值格式化（阶段三 3.4 按 PLAN.md §4.4 重写）

夸克字段与展示的对应关系（**这是 GUI 显示的核心**）：

| 概念 | 夸克字段 | 说明 |
|---|---|---|
| 父目录 / 文件 ID | `pdir_fid` / `fid` | 不再是 `parentId` / `fileId` |
| 文件名 / 大小 | `file_name` / `size` | |
| **是否目录** | `file_type == 0`（0 是**目录**，非 0 是文件），或 `dir`(bool) | ⚠️ 最易错的一点 |
| 时间戳 | `created_at` / `updated_at`（**毫秒**） | |
| 分页 | `_page` / `_size` / `_sort` | `_page` 从 1 开始 |
| **总数** | `metadata._total`（**不在 data 里**） | |
| 列表 | `data.list` | |
| 下载直链 | `download_url` | 不再是 `signedURL` |
| 分享 | `share_id` / `share_url` / `passcode` | `share_id`(32位hex,管理) ≠ URL slug(转存用) |
| 容量/会员 | `total_capacity` / `use_capacity` / `member_type` | 来自 `GET member` |
| 任务状态 | `status`：0 排队 / 1 进行中 / 2 完成 / 3 失败 | |

⚠️ 关于 `status`：它在不同响应里含义不同（HTTP 语义、任务状态、分享状态），
   因此**不对裸 `status` 做自动翻译**，避免误译；需要时用 `task_status` /
   `share_status` 这类明确键名。
"""

from .格式化工具 import 格式化大小, 格式化时长, 格式化时间戳

# ==================== 分享相关展示常量 ====================
# 阶段二 2.9：原定义在 核心/接口/分享接口.py，属表现层，已迁到此处。
# 夸克并无「分享类型 / 下载类型」概念（只有 url_type 公开/私密 + passcode），
# 这些只服务于界面下拉项与列表展示。阶段三 3.8 会移除对话框里的
# 「下载方式 / 流量限制 / 转存次数」三行。
分享类型_无密码 = 0
分享类型_随机密码 = 1
分享类型_自定义密码 = 2

下载类型_直链 = 0
下载类型_非直链 = 1

有效期_永久 = 0
有效期_1天 = 86400
有效期_7天 = 86400 * 7
有效期_30天 = 86400 * 30

分享类型映射 = {0: "无密码", 1: "随机密码", 2: "自定义密码"}
下载类型映射 = {0: "直链", 1: "非直链"}
有效期映射 = {0: "永久", 86400: "1 天",
           86400 * 7: "7 天", 86400 * 30: "30 天"}


# ==================== 字段集合 ====================

时间戳字段 = {
    # —— 夸克原生（毫秒）——
    "created_at", "updated_at", "expired_at",
    "l_created_at", "l_updated_at", "delete_time",
    "saved_at", "expires_at", "exp_at",
    # GET member 实测返回的毫秒到期时间（2026-09-14）
    "super_vip_exp_at", "z_vip_exp_at",
    # —— 旧命名（保留兼容）——
    "签发时间戳", "过期时间戳",
    "createTime", "updateTime", "modifyTime",
    "createAt", "updateAt", "gmtCreate", "gmtModified",
    "createdAt", "updatedAt",
    "ctime", "utime", "dtime",
}

字节字段 = {
    # —— 夸克原生 ——
    "size", "total_capacity", "use_capacity", "secret_total_capacity",
    "secret_use_capacity", "free_capacity", "sign_reward",
    "sign_daily_reward",
    # —— 旧命名（保留兼容）——
    "totalSpaceSize", "usedSpaceSize", "freeSpaceSize",
    "bigFileSize", "maxFileSize", "fileSize", "file_size",
    "usedSpace", "totalSpace", "freeSpace",
    "totalSize", "fileSizeBytes",
}

秒数字段 = {
    "leftTime", "remainTime", "expiresIn",
}

# ⚠️ 天数字段 —— 名字里带 `_days` 的**绝不能用秒数字段**！
# 历史 bug（2026-09-14 实测）：`recycle_normal_serve_days = 10` 被 秒数字段
# 渲染成 “10 秒”，而它的真实语义是「普通用户回收站保留 10 天」。
天数字段 = {
    "recycle_normal_serve_days", "recycle_svip_serve_days",
    "recycle_zvip_serve_days", "recycle_vip_serve_days",
    "recycle_pay_serve_days", "deep_recycle_serve_days",
    "expired_days",
}


# ==================== 值映射 ====================

# ⚠️ 夸克的 `file_type`：**0 = 目录**，非 0 = 文件
文件类型映射 = {0: "目录", 1: "文件", 2: "文件", 3: "文件", 4: "文件"}

任务状态映射 = {0: "排队中", 1: "进行中", 2: "已完成", 3: "失败"}
任务状态映射_文件 = 任务状态映射          # 旧名兼容

分享状态映射 = {1: "正常", 2: "已过期", 3: "已取消"}

会员类型映射 = {
    "NORMAL": "普通用户", "MINI_VIP": "迷你会员",
    "VIP": "VIP", "SUPER_VIP": "超级会员",
    "Z_VIP": "Z 会员", "EXP_SVIP": "体验超级会员",
}

# 会员状态取值（GET member → member_status 的 value）
会员订阅状态映射 = {
    "UNPAID": "未开通",
    "PAID": "已开通",
    "SUBSCRIBE": "订阅中",
    "YEAR_SUBSCRIBE_CANCEL_IN_MEM": "年费订阅（期内已取消，到期不续）",
    "MONTH_SUBSCRIBE_CANCEL": "月费订阅（已取消，到期不续）",
    "TERMINATED": "已终止",
    "EXPIRED": "已过期",
}

# 订阅状态（数值）
订阅状态映射 = {0: "未订阅", 1: "已订阅", 2: "订阅中", 3: "已终止"}

# 账号状态（数值）
账号状态映射 = {0: "正常", 1: "受限", 2: "冻结"}

# 开关类（1/0）
开关映射 = {0: "未开启", 1: "已开启"}

# -1 表示「不限次数」
不限映射 = {-1: "不限"}

# 支付渠道
支付渠道映射 = {
    "ALIPAY": "支付宝", "WECHAT": "微信支付",
    "WECHATPAY": "微信支付", "UNIONPAY": "银联",
    "APPLE": "Apple 内购", "NONE": "无",
}

# 不展示的内部字段（无中文含义 / 对用户无意义）
隐藏字段 = {
    "uhx6", "identity", "member_type_map",
    "subscribe_status_map", "subscribe_pay_channel_map",
    "secret_total_capacity", "secret_use_capacity",
    "meta", "metadata",
}


def 应展示(键) -> bool:
    """账号信息树里是否应展示该字段

    用户要求：**不要展示原始英文字段名**。
    规则：
      1. 命中 `隐藏字段` → 不展示
      2. 有中文映射 → 展示
      3. 键里含 ASCII 字母/数字/下划线（说明是未解析的英文标识符）→ 不展示
      4. 其余（纯中文键、`[0]` 这类下标）→ 展示
    """
    键 = str(键)
    if 键 in 隐藏字段:
        return False
    if 键 in 字段中文名:
        return True
    if 键.startswith("[") and 键.endswith("]"):
        return True
    return not any(c.isascii() and (c.isalnum() or c == "_") for c in 键)

VIP状态映射 = {0: "未开通", 1: "未开通", 2: "已开通", 3: "已过期", 4: "已过期"}
# 旧字段 resType（原适配器）：2 = 目录；保留仅为兼容
资源类型映射 = {0: "文件", 1: "文件", 2: "目录", 3: "目录"}


# ==================== 中文名 ====================

字段中文名 = {
    # ---------- 夸克文件/目录 ----------
    "fid": "文件 ID", "pdir_fid": "父目录 ID",
    "file_name": "文件名", "file_type": "类型", "dir": "是否目录",
    "size": "大小", "format_type": "MIME 类型",
    "category": "分类", "obj_category": "对象分类",
    "updated_at": "修改时间", "created_at": "创建时间",
    "delete_time": "删除时间", "file_path": "路径",
    "share_fid_token": "分享文件令牌",
    "thumbnail": "缩略图", "preview_url": "预览地址",
    "obj_key": "对象键", "bucket": "存储桶",

    # ---------- 夸克分页/响应 ----------
    "metadata": "元数据", "_total": "总数", "_count": "本页数量",
    "_page": "页码", "_size": "每页数量", "_sort": "排序",
    "list": "列表", "status": "状态", "message": "消息",
    "code": "返回码", "req_id": "请求 ID", "data": "数据",
    "total": "总数", "error": "错误", "path": "路径",
    "tq_gap": "轮询间隔(ms)",

    # ---------- 夸克上传 ----------
    "task_id": "任务 ID", "auth_info": "授权信息",
    "auth_key": "授权密钥", "upload_id": "分片上传 ID",
    "md5": "MD5", "sha1": "SHA1", "finish": "已完成",

    # ---------- 夸克分享 ----------
    "share_id": "分享 ID", "pwd_id": "分享 ID(公开)",
    "share_url": "分享链接", "passcode": "提取码",
    "title": "标题", "file_num": "文件数",
    "expired_at": "过期时间", "first_fid": "首个文件 ID",
    "stoken": "分享令牌", "url_type": "链接类型",
    "expired_type": "有效期类型", "pdir_save_all": "全部转存",
    "scene": "场景", "fid_list": "文件 ID 列表",
    "fid_token_list": "文件令牌列表", "to_pdir_fid": "目标目录 ID",
    "exclude_fids": "排除的文件 ID",

    # ---------- 夸克账号/容量（GET member 实测结构，2026-09-14）----------
    "member_type": "会员类型", "subscribe_status": "订阅状态",
    "member_status": "会员状态", "member_info": "会员权益明细",
    "total_capacity": "总空间", "use_capacity": "已用空间",
    "free_capacity": "剩余空间",
    "secret_total_capacity": "隐藏空间总量",
    "secret_use_capacity": "隐藏空间已用",
    "exp_at": "会员到期时间",
    "super_vip_exp_at": "超级会员到期时间",
    "z_vip_exp_at": "Z 会员到期时间",
    "acc_status": "账号状态",
    "is_new_user": "是否新用户",
    "image_backup": "图片备份",
    "video_backup": "视频备份",
    "subscribe_pay_channel": "订阅支付渠道",
    "deep_recycle_stat": "回收站保留策略",
    "cap_composition": "容量构成",
    "extend_capacity_composition": "扩容构成",
    "sign_reward": "签到获得空间",
    "recycle_normal_serve_days": "普通用户回收站保留(天)",
    "recycle_vip_serve_days": "VIP 回收站保留(天)",
    "recycle_svip_serve_days": "超级会员回收站保留(天)",
    "recycle_zvip_serve_days": "Z 会员回收站保留(天)",
    "recycle_pay_serve_days": "付费回收站保留(天)",
    "deep_recycle_serve_days": "深度回收站保留(天)",
    "video_save_to_uses": "视频转存已用次数",
    "video_save_to_remains": "视频转存剩余次数",
    "file_save_to_remains": "文件转存剩余次数",
    "offline_download_remains": "离线下载剩余次数",
    "video_save_to_total": "视频转存总次数",
    # 分享记录的其余可见字段（`GET share/mypage/detail` 实测键名）
    # ⚠️ 该接口**不返回** `passcode`，提取码只能由 `POST share/password`
    #    单独查询，所以分享页的「密码」列需要按需拉取。
    "audit_status": "审核状态", "is_owner": "是否本人分享",
    "expired_left": "剩余有效期",
    # member_status / subscribe_*_map 的子键是会员类型名
    "MINI_VIP": "迷你会员", "VIP": "VIP 会员",
    "SUPER_VIP": "超级会员", "Z_VIP": "Z 会员",
    "NORMAL": "普通用户", "EXP_SVIP": "体验超级会员",

    # ---------- 夸克回收站 ----------
    "record_id": "回收站项 ID", "record_list": "回收站项列表",
    "select_mode": "选择模式", "filelist": "文件列表",

    # ---------- 夸克任务/操作 ----------
    "task_status": "任务状态", "share_status": "分享状态",
    "action_type": "操作类型", "to_pdir_name": "目标目录名",

    # ---------- 旧命名（保留兼容，避免历史数据显示为英文）----------
    "fileName": "文件名", "fileId": "文件 ID", "fileSize": "文件大小",
    "parentId": "父目录 ID", "parentName": "父目录名",
    "resType": "资源类型", "fileType": "文件类型", "ext": "扩展名",
    "gcid": "内容 ID", "depth": "深度", "dirType": "目录类型",
    "ctime": "创建时间", "utime": "修改时间", "dtime": "删除时间",
    "signedURL": "下载链接", "download_url": "下载链接",
    "urlDuration": "链接有效期", "url": "链接",
    "shareId": "分享 ID", "shareName": "分享名称",
    "shareUrl": "分享链接", "shareType": "分享类型",
    "downloadType": "下载方式", "validateDuration": "有效期",
    "msg": "消息", "cursor": "游标",
    "hasMore": "是否还有更多", "leftTime": "剩余时间",
    "vipStatus": "VIP 状态", "svipStatus": "SVIP 状态",
    "vipExpireTime": "VIP 到期时间", "svipExpireTime": "SVIP 到期时间",
    "vipLeftTime": "VIP 剩余时间", "svipLeftTime": "SVIP 剩余时间",
    "totalSpaceSize": "总空间", "usedSpaceSize": "已用空间",
    "freeSpaceSize": "剩余空间",
    "bigFileSize": "单文件大小上限", "maxFileSize": "单文件大小上限",
    "canDownload": "允许下载", "canShare": "允许分享",
    "batchUploadFiles": "单批上传文件数上限",
    "playbackSpeed": "播放倍速上限",
    "directLinkFeature": "支持直链功能",
    "infoHash": "种子哈希",
    "progress": "进度", "exist": "文件已存在",
    "source": "数据来源", "meta": "元数据",
}


def 翻译字段名(键) -> str:
    键 = str(键)
    return 字段中文名.get(键, 键)


def 格式化值(键: str, 值):
    """把值格式化成便于阅读的形式

    :param 键: 字段名（决定用什么格式）
    :param 值: 原始值
    """
    键 = str(键)
    try:
        # ---- 时间戳（夸克是毫秒，也兼容秒）----
        if 键 in 时间戳字段:
            if isinstance(值, (int, float)) and 值 > 1_000_000_000:
                if 值 > 10_000_000_000:      # 毫秒
                    return f"{格式化时间戳(值 / 1000)} ({int(值)})"
                return f"{格式化时间戳(值)} ({int(值)})"

        # ---- 字节 ----
        if 键 in 字节字段:
            if isinstance(值, (int, float)) and 值 > 0:
                return f"{格式化大小(值)} ({int(值)} 字节)"

        # ---- 秒数 ----
        if 键 in 秒数字段:
            if isinstance(值, (int, float)) and 值 > 0:
                return f"{格式化时长(值)} ({int(值)} 秒)"

        # ---- 天数（⚠️ 不能当作秒，见 天数字段 注释）----
        if 键 in 天数字段:
            if isinstance(值, (int, float)) and 值 > 0:
                return f"{int(值)} 天"

        # ---- 毫秒级「剩余时长」----
        # `expired_left` 是**剩余毫秒数**而非时间戳（实测 2313093926161），
        # 按时间戳格式化会错显成 2043 年，必须当 duration 处理。
        if 键 == "expired_left" and isinstance(值, (int, float)) and 值 > 0:
            return f"剩余 {格式化时长(值 / 1000)}"

        # ---- 枚举 ----
        # ⚠️ file_type: 0 = 目录
        if 键 == "file_type":
            return f"{文件类型映射.get(值, '文件')} (原值 {值})"
        if 键 == "dir" or isinstance(值, bool):
            return "是" if 值 else "否"
        if 键 in ("task_status", "taskStatus"):
            return f"{任务状态映射.get(值, 值)} (原值 {值})"
        if 键 == "share_status":
            return f"{分享状态映射.get(值, 值)} (原值 {值})"
        # ⚠️ 夸克分享列表用的是**下划线** `share_type`，不是旧适配器的
        # camelCase `shareType` —— 之前只判了后者，导致分享页类型列显示裸数字 0。
        if 键 in ("share_type", "shareType"):
            return f"{分享类型映射.get(值, 值)} (原值 {值})"
        if 键 == "status":
            # 说明：账号信息页「分享列表」里的裸 `status` 就是分享状态
            # （`GET share/mypage/detail` 实测 status=1）。
            # 任务状态走的是 `task_status` 键，不会落到这个分支。
            return f"{分享状态映射.get(值, 值)} (原值 {值})"
        if 键 == "member_type" and isinstance(值, str):
            return 会员类型映射.get(值, 值)
        # ---- 会员等级作为「键」出现时（member_status / subscribe_*_map 的子键）----
        # 实测（2026-09-14）这几棵子树的值词汇表互不重叠：
        #   member_status.SUPER_VIP            = "YEAR_SUBSCRIBE_CANCEL_IN_MEM"
        #   subscribe_status_map.SUPER_VIP     = "TERMINATED"
        #   subscribe_pay_channel_map.SUPER_VIP = "ALIPAY"
        # `_填节点` 只把**叶子键名**传进来（不带父路径），所以按顺序在几张表里
        # 查一次即可，无需知道父节点是谁。不这样做的话，界面上会直接漏出
        # `YEAR_SUBSCRIBE_CANCEL_IN_MEM` 这种原始英文枚举。
        if 键 in 会员类型映射:
            if isinstance(值, bool):
                return "是" if 值 else "否"
            文本值 = str(值)
            for 表 in (会员订阅状态映射, 订阅状态映射,
                       支付渠道映射, 会员类型映射):
                if 文本值 in 表:
                    return 表[文本值]
            return 值
        if 键 == "member_status":
            return 会员订阅状态映射.get(str(值), 值)
        if 键 == "subscribe_status" and isinstance(值, int):
            return f"{订阅状态映射.get(值, 值)} (原值 {值})"
        if 键 == "acc_status" and isinstance(值, int):
            return f"{账号状态映射.get(值, 值)} (原值 {值})"
        if 键 in ("image_backup", "video_backup"):
            return f"{开关映射.get(值, 值)} (原值 {值})"
        if 键 == "subscribe_pay_channel":
            return 支付渠道映射.get(str(值), 值)
        if 键.endswith("_remains") and isinstance(值, int) and 值 < 0:
            return "不限"
        if 键 in ("vipStatus", "svipStatus", "vip_status", "svip_status"):
            return f"{VIP状态映射.get(值, 值)} (原值 {值})"
        if 键 == "resType":                      # 旧字段兼容
            return f"{资源类型映射.get(值, 值)} (原值 {值})"
        if 键 == "downloadType":
            return f"{下载类型映射.get(值, 值)} (原值 {值})"
        if 键 in ("url_type",):
            return {1: "公开链接", 2: "私密链接（有提取码）"}.get(值, 值)
        if 键 in ("expired_type",):
            return {1: "永久", 2: "有期限"}.get(值, 值)
    except Exception:
        pass
    return 值
