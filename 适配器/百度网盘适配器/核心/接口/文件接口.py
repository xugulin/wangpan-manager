# 百度网盘适配器/核心/接口/文件接口.py
"""
文件浏览接口（只读）
参考抓包：完整操作 / 补充操作 / 上传操作（2026-09-13 ~ 09-14），详见 PLAN.md §6.1

三个端点：

  1. GET /api/list?dir=/&order=time&desc=1&num=100&page=1
     → { errno:0, list:[文件对象...], guid_info, guid, request_id }
     ⚠️ 目录对象也有 category（恒为 6）且 size 恒为 0；
        **判目录必须用 `isdir == 1`**（原骨架是 resType==2，本项目已改）。

  2. GET  /api/filemetas?target=["/path"]&dlink=0&text=1
     POST /api/filemetas?channel=chunlei&version=<ms>   body: target=["/path"]
     → { errno:0, info:[文件对象...], request_id }
     ⚠️ 返回键是 **info**，不是 list。

  3. GET /api/categorylist?...&category=6&order=time&desc=1&num=100&page=1
     → { errno:0, info:[...], guid_info, guid, request_id }
     ⚠️ 返回键同样是 **info**（实测确认，易与 /api/list 的 list 混淆）。

文件对象字段语义见 PLAN.md §3.1 与 Q10。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Iterator

from ..网络.网络客户端 import 网络客户端

logger = logging.getLogger("百度网盘.文件")

# ---------------- category 取值（实测确认，见 PLAN.md Q10） ----------------
分类_视频 = 1
分类_音频 = 2      # 本次样本未覆盖，按百度公开约定
分类_图片 = 3
分类_文档 = 4
分类_应用 = 5
分类_其它 = 6

分类中文名 = {
    1: "视频",
    2: "音频",
    3: "图片",
    4: "文档",
    5: "应用",
    6: "其它",
}

# 单页上限（服务端实测可接受 num=1000）
单页上限 = 1000


# ==========================================================================
# 文件对象读写辅助（统一定义「什么算目录」）
# ==========================================================================
def 是目录(条目: dict) -> bool:
    """✅ 百度用 `isdir == 1` 判目录（原骨架原来是 resType==2，已废弃）。"""
    try:
        return int(条目.get("isdir") or 0) == 1
    except (TypeError, ValueError):
        return False


def 取文件名(条目: dict) -> str:
    return str(条目.get("server_filename") or "")


def 取路径(条目: dict) -> str:
    return str(条目.get("path") or "")


def 取fs_id(条目: dict) -> int | None:
    v = 条目.get("fs_id")
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def 分类名(条目: dict) -> str:
    if 是目录(条目):
        return "目录"
    return 分类中文名.get(条目.get("category"), "其它")


def 父目录(路径: str) -> str:
    """返回父目录路径，根目录返回 "/"。"""
    p = (路径 or "/").rstrip("/")
    if not p:
        return "/"
    i = p.rfind("/")
    return p[:i] or "/"


class 文件接口:
    """文件浏览接口（只读）。

    本阶段（Phase 2）只实现浏览相关；
    创建/删除/移动/分享/回收站属 Phase 3~6，暂以 NotImplementedError 占位。
    """

    def __init__(self, 网络: 网络客户端):
        self.网络 = 网络

    # ==================== 1. 列目录 ====================

    def 取文件列表(
        self,
        dir: str = "/",
        page: int = 1,
        num: int = 100,
        order: str = "time",
        desc: int = 1,
    ) -> dict:
        """GET /api/list —— 单页目录列表。

        :param dir:  目录路径（根为 "/"）
        :param page: 页码，从 1 开始
        :param num:  每页数量（服务端实测可到 1000）
        :param order: 排序字段，实测 `time` / `name`
        :param desc:  1=降序，0=升序
        :return: 原始响应 dict（含 `list`）
        """
        目录 = dir or "/"
        if not 目录.startswith("/"):
            目录 = "/" + 目录
        参数 = {
            "dir": 目录,
            "order": order,
            "desc": str(int(desc)),
            "num": str(max(1, min(int(num), 单页上限))),
            "page": str(max(1, int(page))),
        }
        logger.debug(f"[文件] 列目录 {目录} page={参数['page']} num={参数['num']}")
        # ⚠️ /api/list 实测不带 channel，故 带渠道=False
        return self.网络.请求("GET", "/api/list", params=参数, 带渠道=False)

    def 迭代文件(
        self,
        dir: str = "/",
        num: int = 100,
        order: str = "time",
        desc: int = 1,
        最多页数: int = 200,
    ) -> Iterator[dict]:
        """自动翻页迭代目录下的全部文件对象。"""
        page = 1
        while page <= 最多页数:
            结果 = self.取文件列表(dir, page=page, num=num,
                                    order=order, desc=desc)
            条目们 = 结果.get("list") or []
            if not 条目们:
                return
            for 条目 in 条目们:
                yield 条目
            if len(条目们) < num:
                return            # 已到最后一页
            page += 1

    def 取全部文件(self, dir: str = "/", num: int = 100,
                   order: str = "time", desc: int = 1,
                   上限: int = 20000) -> list[dict]:
        """一次性取回目录下全部文件对象（受 `上限` 保护）。"""
        结果: list[dict] = []
        for 条目 in self.迭代文件(dir, num=num, order=order, desc=desc):
            结果.append(条目)
            if len(结果) >= 上限:
                logger.warning(f"[文件] {dir} 条目数达到上限 {上限}，提前截断")
                break
        return 结果

    # ==================== 2. 文件元信息 ====================

    def 取文件元信息(self, target: str | list[str],
                    dlink: bool = False, text: bool = True) -> dict:
        """GET /api/filemetas —— 取指定路径的元信息。

        :param target: 路径或路径列表（内部转成 JSON 数组字符串）
        :param dlink:  True 时请求下载直链（⚠️ 实测两次 HAR 均为 0，未验证）
        :return: 原始响应 dict（含 **`info`**，不是 `list`）
        """
        列表 = [target] if isinstance(target, str) else list(target)
        import json as _json
        参数 = {
            "target": _json.dumps(列表, ensure_ascii=False, separators=(",", ":")),
            "dlink": "1" if dlink else "0",
            "text": "1" if text else "0",
        }
        logger.debug(f"[文件] 取元信息 {列表}")
        return self.网络.请求("GET", "/api/filemetas", params=参数, 带渠道=False)

    def 批量取文件元信息(self, target: list[str]) -> dict:
        """POST /api/filemetas —— 批量形态（HAR 实测，需 channel + version）。"""
        import json as _json
        logger.debug(f"[文件] 批量元信息 {len(target)} 项")
        return self.网络.请求(
            "POST", "/api/filemetas",
            params={"channel": "chunlei", "version": str(int(time.time() * 1000))},
            表单数据={"target": _json.dumps(target, ensure_ascii=False,
                                          separators=(",", ":"))},
            带渠道=False,
        )

    # ==================== 3. 分类浏览 ====================

    def 取分类列表(
        self,
        category: int = 分类_其它,
        page: int = 1,
        num: int = 100,
        order: str = "time",
        desc: int = 1,
    ) -> dict:
        """GET /api/categorylist —— 按分类浏览。

        ⚠️ 返回键是 **`info`**（实测确认），不是 `/api/list` 的 `list`。
        """
        参数 = {
            "category": str(int(category)),
            "order": order,
            "desc": str(int(desc)),
            "num": str(max(1, min(int(num), 单页上限))),
            "page": str(max(1, int(page))),
        }
        logger.debug(f"[文件] 分类浏览 category={category} page={参数['page']}")
        return self.网络.请求("GET", "/api/categorylist", params=参数, 带渠道=False)

    # ==================== 缩略图 ====================

    def 取缩略图URL(self, 条目: dict) -> str | None:
        """从文件对象的 `thumbs` 字典取缩略图地址。

        实测结构：`thumbs = {"icon": "https://thumbnail0.baidupcs.com/thumbnail/..."}`
        仅部分文件（图片/视频等）带该字段。
        """
        缩略图 = 条目.get("thumbs")
        if isinstance(缩略图, dict):
            for 键 in ("icon", "url3", "url2", "url1", "small", "middle"):
                if 缩略图.get(键):
                    return str(缩略图[键])
            # 兜底：取第一个非空值
            for v in 缩略图.values():
                if v:
                    return str(v)
        return None

    # ==================== 后续 Phase 占位 ====================
    # 以下方法被子代理页面（上传记录页 / 账号信息页）引用。
    # 保留签名并抛出明确异常，避免 AttributeError 式的隐晦崩溃。

    def 取上传记录(self, pageSize: int = 20, cursor: str = "") -> dict:
        raise NotImplementedError(
            "上传记录将在 Phase 4（上传）实现，当前为只读浏览阶段")

    def 取用户操作(self, pageSize: int = 20, cursor: str = "",
                  fileTypes: list | None = None) -> dict:
        raise NotImplementedError(
            "用户操作记录将在 Phase 3（文件管理）实现")

    def 取回收站列表(self, pageSize: int = 100, page: int = 1) -> dict:
        raise NotImplementedError(
            "回收站将在 Phase 3（文件管理）实现")
