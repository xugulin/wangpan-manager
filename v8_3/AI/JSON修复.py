"""从模型输出里抠 JSON —— 专门对付"本地小模型输出不规矩"。

为什么需要这个模块
==================
本地 ``deepseek-r1:1.5b`` 在 CPU 上跑一次要几十秒，但它经常这样交作业：

* 包在 Markdown 代码围栏里：`` ```json {…} ``` ``；
* 前面先写一大段推理，里面夹着花括号；
* **被 token 上限截断**（实测诊断 JSON 停在 ``"起播等待秒":`` 就没了）——
  这时 ``json.loads`` 必然失败，如果直接放弃，那 40~50 秒的推理就白烧了。

所以这里做四级兜底：直接解析 → 剥围栏/全角括号 → 花括号配对扫片段 →
**把截断的对象补完整**（补引号、补括号，丢掉最后那个不完整的键值对）。
补出来的结果可能少一两个字段，但对"给个建议/给个摘要"这类用途完全够用，
调用方本来也都有默认值。
"""

from __future__ import annotations

import json
import re
from typing import Optional

__all__ = ["去代码块", "修补截断JSON", "提取JSON对象"]

#: 匹配 ```lang ... ``` 或 ``` ... ``` 代码块
_围栏 = re.compile(r"```[a-zA-Z0-9_+-]*\s*(.*?)```", re.DOTALL)


def 去代码块(文本: str) -> str:
    """把 Markdown 代码围栏剥掉（多层就多剥几次），返回最像 JSON 的那段。"""
    文本 = str(文本 or "")
    段们 = [m.group(1).strip() for m in _围栏.finditer(文本)]
    if 段们:
        # 优先返回包含花括号的那段（模型有时先给一段说明代码块）
        for 段 in 段们:
            if "{" in 段:
                return 段
        return 段们[0]
    # 有开头没结尾（被截断）的围栏
    if "```" in 文本:
        头, _, 尾 = 文本.partition("```")
        尾 = 尾.split("\n", 1)[-1] if "\n" in 尾 else 尾
        if "{" in 尾:
            return 尾.strip()
    return 文本


def _尝试解析(文本: str):
    try:
        return json.loads(文本)
    except Exception:
        return None


def 修补截断JSON(文本: str) -> Optional[dict]:
    r"""把**被截断**的 JSON 对象尽量补完整；补不出来返回 ``None``。

    做法：从某个 ``{`` 开始扫，跟踪字符串状态与括号深度，记下最后一个
    "安全的截断点"（即刚读完一个完整值的位置：逗号/闭括号之后）。截断发生在
    半个键值对上时，就退回到那个安全点再补齐 ``}``。
    """
    原文 = str(文本 or "")
    # 从头到尾每个 "{" 都试一次：模型常先写一段带花括号的推理（``{随便}``），
    # 只从第一个 "{" 开始修就会修出个废物，真正的对象在后面。
    起点们 = [i for i, 字 in enumerate(原文) if 字 == "{"]
    if not 起点们:
        return None
    for 起点 in 起点们:
        数据 = _修补从(原文[起点:])
        if 数据:
            return 数据
    return None


def _修补从(片段: str) -> Optional[dict]:
    r"""从某个 ``{`` 开始扫，尽量把被截断的对象补完整。

    用**括号栈**（跳过字符串里的括号）记下最后一个"安全截断点"对应的栈，
    截断发生在半个键值对上时就退回到那个安全点，再按栈把括号补齐。
    注意：不能拿 ``str.count("{")`` 去凑 —— 字符串里的花括号会被算进去，
    补出来的东西多一个 ``}`` 反而解析不了（实测踩过）。
    """
    栈: list[str] = []
    在字符串 = False
    转义 = False
    安全点 = -1
    安全栈: list[str] = []
    for i, 字 in enumerate(片段):
        if 在字符串:
            if 转义:
                转义 = False
            elif 字 == "\\":
                转义 = True
            elif 字 == '"':
                在字符串 = False
            continue
        if 字 == '"':
            在字符串 = True
        elif 字 in "{[":
            栈.append(字)
        elif 字 in "}]":
            if 栈:
                栈.pop()
            if not 栈:
                # 对象已经完整
                数据 = _尝试解析(片段[:i + 1])
                return 数据 if isinstance(数据, dict) else None
            if len(栈) == 1:
                安全点, 安全栈 = i + 1, list(栈)
        elif 字 == "," and len(栈) == 1:
            安全点, 安全栈 = i, list(栈)
    if 安全点 <= 0:
        return None
    裁剪 = 片段[:安全点].rstrip().rstrip(",")
    候选 = 裁剪 + "".join("}" if 开 == "{" else "]" for 开 in reversed(安全栈))
    数据 = _尝试解析(候选)
    if isinstance(数据, dict):
        return 数据
    # 再退一步：只把最外层对象补上（丢掉数组尾巴这种难补的情况）
    候选2 = 裁剪
    缺 = 候选2.count("{") - 候选2.count("}")
    while 缺 > 0 and 候选2:
        候选2 += "}"
        缺 -= 1
    数据 = _尝试解析(候选2)
    return 数据 if isinstance(数据, dict) else None


def 提取JSON对象(文本, 期望键: tuple = ()) -> Optional[dict]:
    """从模型输出里抠出一个 JSON 对象；抠不到返回 ``None``。

    ``期望键``：一段文字里有多个对象时，优先返回**含其中任一键**的那个
    （模型常先写推理、最后才写真正的参数对象）。
    """
    原文 = str(文本 or "").strip()
    if not 原文:
        return None
    候选: list[str] = []
    for 项 in (原文, 去代码块(原文),
              原文.replace("｛", "{").replace("｝", "}")):
        if 项 and 项 not in 候选:
            候选.append(项)
    找到: list[dict] = []
    for 项 in 候选:
        数据 = _尝试解析(项)
        if isinstance(数据, dict) and 数据 and 数据 not in 找到:
            找到.append(数据)
    if not 找到:
        for 段 in re.findall(r"\{.*?\}", 原文, re.DOTALL):
            数据 = _尝试解析(段)
            if isinstance(数据, dict) and 数据 and 数据 not in 找到:
                找到.append(数据)
    if not 找到:
        修补 = 修补截断JSON(去代码块(原文))
        if 修补:
            找到.append(修补)
    if not 找到:
        return None
    if 期望键:
        for 数据 in 找到:
            if any(键 in 数据 for 键 in 期望键):
                return 数据
    # 没给期望键（或都没命中）时，取字段最多的那个，通常才是真参数
    return max(找到, key=len)
