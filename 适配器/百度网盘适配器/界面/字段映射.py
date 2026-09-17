# 百度网盘适配器/界面/字段映射.py
"""字段中文映射、状态映射、值格式化

字段来源：PLAN.md §3.1（`/api/list` 实测 schema）与 §3.1 补充的 Q10 语义表。
平台差异要点：
  - 判目录用 **`isdir == 1`**（原骨架是 `resType == 2`，已废弃）
  - 文件类型用 **`category`**（原骨架把它拼错成 `mineType`）
  - 时间字段是 `server_mtime` / `server_ctime`，还有一个客户端侧的 `local_mtime`
"""

from 核心.接口.文件接口 import 分类中文名, 是目录

from .格式化工具 import 格式化大小, 格式化时长, 格式化时间戳

# ==========================================================================
# 值格式化用的字段分类
# ==========================================================================
时间戳字段 = {
    # 百度文件对象
    "server_mtime", "server_ctime", "server_atime",
    "local_mtime", "local_ctime",
    # 其它接口
    "ctime", "mtime", "atime", "utime", "dtime",
    "servertime", "server_time", "更新时间", "expiretime",
}

字节字段 = {
    "size", "total", "used", "free",
    "sbox_total", "sbox_used",
    "content-length", "content_length",
}

秒数字段 = {
    "expire", "expires_in", "period", "leftTime", "remainTime",
}

# ==========================================================================
# 字段中文名
# ==========================================================================
字段中文名 = {
    # ---------------- 文件对象（实测 32 个字段，见 Q10） ----------------
    "fs_id": "文件 ID",
    "path": "路径",
    "server_filename": "文件名",
    "isdir": "是否目录",
    "size": "大小",
    "md5": "MD5（展示态）",
    "category": "分类",
    "real_category": "真实类型",
    "server_mtime": "修改时间",
    "server_ctime": "创建时间",
    "server_atime": "访问时间",
    "local_mtime": "本地修改时间",
    "local_ctime": "本地创建时间",
    "thumbs": "缩略图",
    "docpreview": "文档预览",
    "lodocpreview": "低清文档预览",
    "is_root": "是否根目录项",
    # Q10 语义已确认的字段
    "unlist": "是否隐藏",
    "wpfile": "是否 WPS 文档",
    "oper_id": "最后操作者",
    "owner_id": "所有者 ID",
    "owner_type": "所有者类型",
    "share": "是否已分享",
    "is_scene": "是否场景文件",
    "dir_empty": "目录是否为空",
    "empty": "是否为空",
    "from_type": "来源类型",
    "pl": "权限/层级标记",
    "tkbind_id": "图库绑定 ID",
    "black_tag": "黑名单标记",
    "extent_int2": "扩展字段 int2",
    "extent_int8": "扩展字段 int8",
    "extent_tinyint7": "扩展字段 tinyint7",
    # ---------------- 用户 / 会话 ----------------
    "uk": "用户 ID",
    "uname": "用户名",
    "username": "用户名",
    "avatar_url": "头像地址",
    "photo_url": "头像地址",
    "vip_type": "会员类型",
    "vip_level": "会员等级",
    "vip_identity": "会员身份",
    "priority_name": "脱敏账号",
    "sec_mobile": "绑定手机",
    "svip10_id": "SVIP10 ID",
    "uk_str": "用户 ID（字符串）",
    "bdstoken": "会话令牌",
    "isdocuser": "是否文档用户",
    "login_info": "登录信息",
    "records": "记录列表",
    # ---------------- 容量 ----------------
    "total": "总空间",
    "used": "已用空间",
    "free": "剩余空间",
    "recyclestatus": "回收站状态",
    "expire": "是否过期",
    # ---------------- 通用响应 ----------------
    "errno": "错误码",
    "errmsg": "错误信息",
    "err_msg": "错误信息",
    "show_msg": "提示信息",
    "newno": "新编号",
    "request_id": "请求 ID",
    "guid": "GUID",
    "guid_info": "GUID 信息",
    "info": "信息列表",
    "list": "列表",
    "count": "总数",
    "total_count": "总数",
    "nextpage": "下一页",
    "has_more": "是否还有更多",
    "display_count": "显示数量",
    "need_ai_search": "需要 AI 搜索",
    # ---------------- 任务 / 分享 ----------------
    "taskid": "任务 ID",
    "task_id": "任务 ID",
    "task_errno": "任务错误码",
    "status": "状态",
    "progress": "进度",
    "faillist": "失败列表",
    "shareid": "分享 ID",
    "share_id": "分享 ID",
    "shorturl": "短链",
    "link": "分享链接",
    "pwd": "提取码",
    "period": "有效期（天）",
    "expiredType": "过期类型",
    "expiretime": "过期时间",
    "qrcodeurl": "二维码地址",
    "fsidlist": "文件 ID 列表",
    "fidlist": "文件 ID 列表",
    "filelist": "文件列表",
    # ---------------- 展示辅助 ----------------
    "文件名": "文件名",
    "文件ID": "文件 ID",
    "类型": "类型",
    "大小": "大小",
    "修改时间": "修改时间",
}

# ==========================================================================
# 状态映射
# ==========================================================================
# 是否类字段：原值 → 中文
是否映射 = {0: "否", 1: "是"}

# ✅ 判目录：百度用 isdir
资源类型映射 = {0: "文件", 1: "目录"}

# 会员类型（实测 vip_type=2 为超级会员）
VIP类型映射 = {0: "普通用户", 1: "会员", 2: "超级会员"}

# 任务状态（/share/taskquery 实测）
任务状态映射_文件 = {
    "running": "处理中",
    "success": "已完成",
    "failed": "失败",
    "pending": "等待中",
}

# 操作类型（文件管理 opera，Phase 3 会用到）
操作类型映射 = {
    "delete": "删除",
    "rename": "重命名",
    "move": "移动",
    "copy": "复制",
    "restore": "还原",
    "deleteforever": "彻底删除",
    "upload": "上传",
    "download": "下载",
    "share": "分享",
    "transfer": "转存",
    "未知": "未知",
}


# ==========================================================================
# 对外函数
# ==========================================================================
def 翻译字段名(键) -> str:
    """字段名 → 中文名；未知字段原样返回。"""
    键 = str(键)
    return 字段中文名.get(键, 键)


def 分类中文(条目: dict) -> str:
    """文件对象 → 分类中文名（目录优先）。"""
    if 是目录(条目):
        return "目录"
    return 分类中文名.get(条目.get("category"), "其它")


def 格式化值(键: str, 值):
    """按字段语义格式化值（时间戳/字节/秒数/枚举），无法识别时原样返回。"""
    键 = str(键)
    try:
        if 键 in 时间戳字段:
            if isinstance(值, (int, float)) and not isinstance(值, bool) and 值 > 1_000_000_000:
                return f"{格式化时间戳(值)} ({值})"
        if 键 in 字节字段:
            if isinstance(值, (int, float)) and not isinstance(值, bool) and 值 > 0:
                return f"{格式化大小(值)} ({值})"
        if 键 in 秒数字段:
            if isinstance(值, (int, float)) and not isinstance(值, bool) and 值 > 0:
                return f"{格式化时长(值)} ({值} 秒)"
        if 键 in ("isdir", "unlist", "wpfile", "share", "is_scene",
                 "dir_empty", "empty", "is_root"):
            if 值 in 是否映射:
                return f"{是否映射[值]} (原值 {值})"
        if 键 == "category":
            return f"{分类中文名.get(值, '其它')} (原值 {值})"
        if 键 == "vip_type":
            return f"{VIP类型映射.get(值, 值)} (原值 {值})"
        if 键 in ("status", "task_status"):
            return f"{任务状态映射_文件.get(值, 值)} (原值 {值})"
        if isinstance(值, bool):
            return "是" if 值 else "否"
    except Exception:
        pass
    return 值
