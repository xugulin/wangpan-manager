# 百度网盘适配器/核心/接口/回收站接口.py
"""
回收站接口
作用：列表与还原

端点（HAR 实测，见 PLAN.md §6.3）
--------------------------------
    ① 列表
       GET /api/recycle/list/?num=100&page=1&clienttype=0&app_id=250528&web=1&dp-logid=...
       → { errno:0, list:[...], timestamp:1789327529, request_id }

    ② 还原
       POST /api/recycle/restore?channel=chunlei&async=1&bdstoken=...
       body: fidlist=[584299975796218]
       → { errno:0, faillist:[], taskid:0, request_id }
       ⚠️ 返回的是 **`faillist`**（失败列表），不是 `info`
       ⚠️ `taskid:0` 表示同步完成，**无需轮询**

    ③ 彻底删除
       POST /api/recycle/delete?channel=chunlei&async=1&bdstoken=...
       body: fidlist=[566873365733941]
       → 首次: { errno:132, authwidget:{...}, verify_scene:2 }   ← 风控拦截
       → 验证后: { errno:0 }

🔴 **本模块刻意不提供「彻底删除」的自动化方法。**
   实测首次调用必然返回 `errno:132` 并强制短信二次验证
   （`passport.baidu.com/v2/sapi/authwidgetverify`，`ppAuthName:'recyclebin'`），
   这是**服务端风控**，不应也不能被客户端绕过（见 PLAN.md R4）。

   界面层的正确做法：
     - 提供「彻底删除」入口，但点击后提示用户前往网页端完成验证；
     - 若直接调用返回 `errno:132`，捕获并给出同样的提示，**不要重试**。

   如需在代码中判断该错误：`接口错误.是风控验证` 属性即 `errno == 132`。
"""

from __future__ import annotations

import json
import logging

from ..网络.网络客户端 import 网络客户端, 接口错误, 风控验证

logger = logging.getLogger("百度网盘.回收站")

# 彻底删除受限的提示文案（界面层直接复用）
彻底删除提示 = (
    "百度网盘对「彻底删除」启用了短信二次验证（errno:132），\n"
    "客户端无法自动完成。请前往网页版回收站操作：\n"
    "https://pan.baidu.com/disk/main#/recycle"
)


class 回收站接口:
    """回收站（列表 / 还原）。

    彻底删除见模块文档说明 —— 不提供自动化实现。
    """

    def __init__(self, 网络: 网络客户端):
        self.网络 = 网络

    # ---------------- 列表 ----------------

    def 取列表(self, page: int = 1, num: int = 100) -> dict:
        """GET /api/recycle/list/ —— 返回含 `list` 的原始响应。"""
        数据 = self.网络.请求(
            "GET", "/api/recycle/list/",
            params={"num": str(int(num)), "page": str(int(page))},
            带渠道=False,
        )
        条目数 = len(数据.get("list") or [])
        logger.info(f"[回收站] 列表 page={page} num={num} → {条目数} 项")
        return 数据

    def 取全部(self, num: int = 100, 最多页数: int = 50) -> list[dict]:
        """翻页取回回收站全部条目（按「本页是否满」判断是否继续）。"""
        结果: list[dict] = []
        page = 1
        while page <= 最多页数:
            数据 = self.取列表(page=page, num=num)
            条目们 = 数据.get("list") or []
            结果.extend(条目们)
            if len(条目们) < num:
                break
            page += 1
        return 结果

    # ---------------- 还原 ----------------

    def 还原(self, fid列表: list[int | str]) -> dict:
        """POST /api/recycle/restore —— 还原回原目录。

        :param fid列表: 回收站条目的 `fs_id` 列表
        :return: 原始响应；失败项在 **`faillist`** 里
        """
        列表 = [f for f in fid列表 if f not in (None, "")]
        if not 列表:
            raise ValueError("fid 列表不能为空")
        数据 = self.网络.请求(
            "POST", "/api/recycle/restore",
            params={"channel": "chunlei", "async": "1"},
            表单数据={"fidlist": json.dumps(列表,
                                          separators=(",", ":"))},
            需要bdstoken=True,
        )
        失败 = 数据.get("faillist") or []
        logger.info(f"[回收站] 还原 {len(列表)} 项 → "
                    f"失败 {len(失败)} 项（taskid={数据.get('taskid')}）")
        if 失败:
            logger.warning(f"[回收站] 还原失败项：{失败}")
        return 数据

    def 还原单个(self, fid: int | str) -> dict:
        return self.还原([fid])

    # ---------------- 彻底删除（仅提供错误判定，不自动调用） ----------------

    @staticmethod
    def 是风控错误(异常) -> bool:
        """判断某个异常是否为「彻底删除被风控拦截」。"""
        return isinstance(异常, 接口错误) and 异常.errno == 风控验证

    @staticmethod
    def 彻底删除说明() -> str:
        """返回给界面显示的提示文案。"""
        return 彻底删除提示
