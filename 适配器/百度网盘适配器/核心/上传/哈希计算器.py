# 百度网盘适配器/核心/上传/哈希计算器.py
"""
哈希计算器
作用：计算百度网盘上传所需的各类 MD5，并实现官方的「展示态 MD5」编解码

⚠️ 百度对 MD5 使用**两套表示**（详见 PLAN.md §5.4）：

  | 表示 | 用途 | 出现在 |
  |---|---|---|
  | **纯净 MD5** | 上传协议 | `precreate.block_list`、`superfile2` 响应、`create.block_list` |
  | **加密 MD5**（展示态） | 用户可见 | `/api/list`、`/api/create` 响应、`rapidupload` 的 `content-md5`/`slice-md5` |

`加密md5()` 是官方 `_encryptMd5` 的 Python 实现（从上传器 JS 提取，
并用真实 ground truth 逐位验证通过，见 PLAN.md §5.4.2）：

    _encryptMd5 = function(e) {
        if (e.length != 32) return e;
        for (...) if (!(hex)) return e;                                 // 非纯小写hex原样返回
        i = e.substr(8,8)+e.substr(0,8)+e.substr(24,8)+e.substr(16,8);  // ① 4×8 重排
        r = ""; for (o=0;o<32;o++) r += (parseInt(i[o],16) ^ 15 & o).toString(16);  // ② XOR (o%16)
        a = String.fromCharCode("g".charCodeAt() + parseInt(r[9],16));  // ③ 第9位 → chr('g'+值)
        return r.substr(0,9) + a + r.substr(10);
    }

这解释了为什么展示态 md5 的**第 9 位总是 `g`..`v` 之间的字母**。
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

# 分片大小：4 MB（两次独立实测确认，见 PLAN.md §5.2）
分片大小 = 4 * 1024 * 1024
# 秒传的 slice-md5 / data_content 取样长度：256 KB
首片大小 = 256 * 1024
# 读盘块大小
读块大小 = 2 * 1024 * 1024

_纯HEX32 = re.compile(r"[0-9a-f]{32}")


# ==========================================================================
# 官方 _encryptMd5 及其逆运算
# ==========================================================================
def 加密md5(e: str) -> str:
    """纯净 MD5 → 展示态 MD5（官方 `_encryptMd5`）。

    非「32 位纯小写 hex」的输入**原样返回**（与官方行为一致）。
    """
    if not isinstance(e, str) or len(e) != 32 or not _纯HEX32.fullmatch(e):
        return e
    # ① 4 组 8 字符重排：8-16, 0-8, 24-32, 16-24
    i = e[8:16] + e[0:8] + e[24:32] + e[16:24]
    # ② 逐位 XOR (index % 16)
    r = "".join(format(int(i[o], 16) ^ (15 & o), "x") for o in range(32))
    # ③ 第 9 位替换为 chr('g' + 值)
    return r[:9] + chr(ord("g") + int(r[9], 16)) + r[10:]


def 解密md5(c: str) -> str:
    """展示态 MD5 → 纯净 MD5（`加密md5` 的逆运算）。

    ①② 两步均为自逆运算，只有第 9 位需要还原。
    """
    if not isinstance(c, str) or len(c) != 32:
        raise ValueError(f"不是 32 位字符串：{c!r}")
    ch = c[9]
    if "g" <= ch <= "v":
        第九位 = format(ord(ch) - ord("g"), "x")
    elif ch in "0123456789abcdef":
        第九位 = ch            # 输入本身可能就是纯净值
    else:
        raise ValueError(f"第 9 位字符非法：{ch!r}")
    s = c[:9] + 第九位 + c[10:]
    i = "".join(format(int(s[o], 16) ^ (15 & o), "x") for o in range(32))
    return i[8:16] + i[0:8] + i[24:32] + i[16:24]


# ==========================================================================
# 各类 MD5 计算
# ==========================================================================
def 计算文件MD5(文件路径: str | Path, 进度回调=None) -> str:
    """计算整文件**纯净** MD5（分块读，避免大文件撑爆内存）。

    :param 进度回调: 形如 `回调(已读字节, 总字节)`
    """
    路径 = Path(文件路径)
    总大小 = 路径.stat().st_size
    哈希 = hashlib.md5()
    已读 = 0
    with 路径.open("rb") as f:
        while True:
            块 = f.read(读块大小)
            if not 块:
                break
            哈希.update(块)
            已读 += len(块)
            if 进度回调:
                进度回调(已读, 总大小)
    return 哈希.hexdigest()


def 计算首片MD5(文件路径: str | Path) -> str:
    """计算**首 256 KB** 的纯净 MD5（即秒传 `slice-md5` 的原始值）。"""
    with Path(文件路径).open("rb") as f:
        数据 = f.read(首片大小)
    return hashlib.md5(数据).hexdigest()


def 计算分片MD5(文件路径: str | Path, 每片字节: int = 分片大小,
                进度回调=None) -> list[str]:
    """按固定大小切分并计算每片的**纯净** MD5（用于 `block_list`）。

    ⚠️ 空文件返回 `[md5("")]`（1 片），不会返回空列表。
    """
    路径 = Path(文件路径)
    总大小 = 路径.stat().st_size
    结果: list[str] = []
    当前 = hashlib.md5()
    当前长度 = 0
    已读 = 0

    with 路径.open("rb") as f:
        while True:
            块 = f.read(读块大小)
            if not 块:
                break
            偏移 = 0
            while 偏移 < len(块):
                取 = min(每片字节 - 当前长度, len(块) - 偏移)
                当前.update(块[偏移:偏移 + 取])
                当前长度 += 取
                偏移 += 取
                if 当前长度 >= 每片字节:
                    结果.append(当前.hexdigest())
                    当前 = hashlib.md5()
                    当前长度 = 0
            已读 += len(块)
            if 进度回调:
                进度回调(已读, 总大小)

    if 当前长度 > 0:
        结果.append(当前.hexdigest())
    if not 结果:
        结果.append(hashlib.md5(b"").hexdigest())      # 空文件
    return 结果


def 计算全部哈希(文件路径: str | Path, 进度回调=None,
                每片字节: int = 分片大小) -> dict:
    """**单次读盘**同时算出上传所需的全部哈希。

    :return: {大小, 整文件md5, 首片md5, 分片md5列表, 分片数,
              整文件md5_加密, 首片md5_加密}
              后两项即秒传直接可用的 `content-md5` / `slice-md5`
    """
    路径 = Path(文件路径)
    总大小 = 路径.stat().st_size

    整 = hashlib.md5()
    首 = hashlib.md5()
    首已读 = 0
    分片列表: list[str] = []
    当前 = hashlib.md5()
    当前长度 = 0
    已读 = 0

    with 路径.open("rb") as f:
        while True:
            块 = f.read(读块大小)
            if not 块:
                break
            整.update(块)

            if 首已读 < 首片大小:
                取 = min(首片大小 - 首已读, len(块))
                首.update(块[:取])
                首已读 += 取

            偏移 = 0
            while 偏移 < len(块):
                取 = min(每片字节 - 当前长度, len(块) - 偏移)
                当前.update(块[偏移:偏移 + 取])
                当前长度 += 取
                偏移 += 取
                if 当前长度 >= 每片字节:
                    分片列表.append(当前.hexdigest())
                    当前 = hashlib.md5()
                    当前长度 = 0

            已读 += len(块)
            if 进度回调:
                进度回调(已读, 总大小)

    if 当前长度 > 0:
        分片列表.append(当前.hexdigest())
    if not 分片列表:
        分片列表.append(hashlib.md5(b"").hexdigest())

    整md5 = 整.hexdigest()
    首md5 = 首.hexdigest()
    return {
        "大小": 总大小,
        "整文件md5": 整md5,
        "首片md5": 首md5,
        "分片md5列表": 分片列表,
        "分片数": len(分片列表),
        # 秒传的 content-md5 / slice-md5 必须用**加密态**
        "整文件md5_加密": 加密md5(整md5),
        "首片md5_加密": 加密md5(首md5),
    }


def 读偏移数据(文件路径: str | Path, 偏移: int, 长度: int) -> bytes:
    """读取 `[偏移, 偏移+长度)` 区间（超出部分自动截断）。"""
    with Path(文件路径).open("rb") as f:
        f.seek(偏移)
        return f.read(长度)
