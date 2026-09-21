r"""目标盘命名限制与规避（2026-09-16 新增）。

**为什么需要**：各家网盘对文件名的合法字符集不同，而且限制只在**真实上传路径**
上暴露——实测百度：

| 名字 | 结果（百度 /api/create） |
|---|---|
| `引号'单"双.txt` | ❌ `文件名中不能包含?\|"><:*等特殊字符` |
| `换行\\n名字.txt` | ❌ `文件名中不能包含不可见字符` |
| `emoji🎬🦆.mp4` | ❌ `接口返回异常`（星平面字符直接让接口报错） |
| `末尾空格 .txt`、`一字节.bin` | ✅ 正常 |

> 注意：**秒传（内容已存在）会绕过这层校验**，所以"同一个文件重传一次居然成功"
> 是假象——第一次真实上传才是真相。

规避策略（尽量保留可读性，并写进改名记录以便追溯/还原）：
  * 非法字符 → 全角等价字符（`"`→`＂`、`?`→`？`、`*`→`＊`、`:`→`：`、
    `<`→`＜`、`>`→`＞`、`|`→`｜`、`\\`→`＼`、`/`→`／`）；
  * 不可见字符（控制字符/换行/制表）→ 单个空格（连续折叠）；
  * 星平面字符（emoji 等 > U+FFFF）→ 移除；若名字被清空则回退为
    `未命名_<4位哈希>`（保留扩展名）；
  * 其它家网盘（夸克/光鸭）实测支持 emoji 与这些字符 → 不做任何改动。
"""

from __future__ import annotations

import hashlib
import re
from pathlib import PurePosixPath

#: 百度明确禁止的字符（服务端原文：`文件名中不能包含?|"><:*等特殊字符`）
百度非法字符 = '\\/:*?"<>|'
#: 非法字符 → 全角等价（保持肉眼可读，且是全角合法字符）
全角映射 = {
    "\\": "＼", "/": "／", ":": "：", "*": "＊", "?": "？",
    '"': "＂", "<": "＜", ">": "＞", "|": "｜",
}
#: 控制字符（含换行/制表/删除符）
控制字符正则 = re.compile(r"[\x00-\x1f\x7f-\x9f]+")

#: 各网盘的命名限制（只写实测确认过的）
命名限制表: dict[str, dict] = {
    "baidu": {
        "非法字符": 百度非法字符,
        "禁止不可见": True,
        "禁止星平面": True,          # emoji → 服务端"接口返回异常"
        "说明": "百度网盘不允许 \\ / : * ? \" < > | 与不可见字符，且不支持 emoji",
    },
    "quark": {"非法字符": "", "禁止不可见": False, "禁止星平面": False,
              "说明": ""},
    "guangya": {"非法字符": "", "禁止不可见": False, "禁止星平面": False,
                "说明": ""},
}


def 取限制(网盘: str) -> dict:
    """按网盘标识取限制；未知网盘按"无限制"处理（不擅自改名）。"""
    键 = str(网盘 or "").strip().lower()
    if 键 in 命名限制表:
        return 命名限制表[键]
    # 支持"百度网盘"/"baidu_2"这类写法
    for k, v in 命名限制表.items():
        if 键.startswith(k) or k in 键:
            return v
    return {"非法字符": "", "禁止不可见": False, "禁止星平面": False, "说明": ""}


def 需要规避(网盘: str, 名称: str) -> tuple[bool, str]:
    """返回 (是否必须改名, 原因)。"""
    名称 = str(名称 or "")
    if not 名称:
        return False, ""
    # 只看文件名部分：路径里的 "/" 是目录分隔，不属于"名字非法"
    名称 = PurePosixPath(名称).name      # 同 规避名称：网盘路径永远是 "/" 分隔
    限制 = 取限制(网盘)
    非法 = str(限制.get("非法字符") or "")
    if 非法 and any(ch in 非法 for ch in 名称):
        命中 = "".join(sorted({ch for ch in 名称 if ch in 非法}))
        return True, f"包含网盘不允许的字符：{命中}"
    if 限制.get("禁止不可见") and 控制字符正则.search(名称):
        return True, "包含不可见字符（换行/制表等）"
    if 限制.get("禁止星平面") and any(ord(ch) > 0xFFFF for ch in 名称):
        return True, "包含 emoji 等星平面字符（该网盘接口会直接报错）"
    return False, ""


def 规避名称(网盘: str, 名称: str) -> tuple[str, str]:
    """把名称改成该网盘能接受的形态。

    返回 ``(新名称, 说明)``；不需要改动时原样返回且说明为空。
    """
    名称 = str(名称 or "")
    需要, 原因 = 需要规避(网盘, 名称)
    if not 需要:
        return 名称, ""
    限制 = 取限制(网盘)
    # ⚠️ 必须用 **PurePosixPath**（网盘路径永远是 "/" 分隔），不能用 Path：
    #    Windows 上 Path("a:b.txt") 会把 "a:" 当成盘符（name 只剩 "b.txt"）、
    #    Path("a\b.txt") 把 "\" 当目录分隔符 —— 这两个非法字符就逃过改名了。
    #    真机 Windows CI 实测到了（改名后名字里仍有 ":"，上传必然失败）。
    主干 = PurePosixPath(名称)
    目录 = 名称[:len(名称) - len(主干.name)]
    基名 = 主干.name
    后缀 = 主干.suffix

    原名 = 基名
    非法 = str(限制.get("非法字符") or "")
    if 非法:
        基名 = "".join(全角映射.get(ch, ch) if ch in 非法 else ch
                     for ch in 基名)
    if 限制.get("禁止不可见"):
        基名 = 控制字符正则.sub(" ", 基名)
        基名 = re.sub(r" {2,}", " ", 基名).strip()
    if 限制.get("禁止星平面"):
        基名 = "".join(ch for ch in 基名 if ord(ch) <= 0xFFFF)

    新名 = 基名
    if not 新名.strip() or 新名.strip() == 后缀.strip():
        短哈希 = hashlib.md5(原名.encode("utf-8")).hexdigest()[:4]
        新名 = f"未命名_{短哈希}"
        if 后缀 and not 新名.lower().endswith(后缀.lower()):
            新名 += 后缀
    新路径 = 目录 + 新名
    说明 = f"{原因} → 改名为 {新名}"
    return 新路径, 说明
