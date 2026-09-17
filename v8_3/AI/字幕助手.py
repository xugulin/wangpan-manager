# v8_3/AI/字幕助手.py
"""AI 字幕助手 —— SRT 解析/生成、AI 翻译、AI 总结、语音识别结果转字幕。

给播放页用的**纯逻辑**模块：不 import Qt、不开线程池之外的后台线程、
不读写任何 GUI 全局状态。界面只需要把 :class:`字幕助手` 装配好，
在后台线程里调 :meth:`字幕助手.翻译` / :meth:`字幕助手.总结`，
再把结果丢回主线程刷新即可。

设计原则（每一条都对应一个真实的坑）
====================================
1. **时间戳零漂移**：翻译只换 ``文本`` 字段，``开始秒`` / ``结束秒`` 原样复制。
   时间轴一旦被 AI 改写，字幕会和画面整体错位（几分钟后差出好几秒），
   而且这种错位是"看起来能播但完全对不上"的隐性故障，比直接报错更难查。
   所以这里连"顺手对齐一下"都不做，并在最后再做一次自检回填（见 :meth:`字幕助手._修复时间戳`）。
2. **绝不抛到调用方**：AI 会返回坏 JSON、少几条、空字符串、超时、断连……
   播放页在跑视频，任何异常冒到 UI 线程都会变成弹窗甚至崩溃。
   因此所有 AI 交互都在内部兜住，失败一律**保留原文**并记入 ``失败批``，
   条目数量与顺序永远和输入一致（宁可译文是原文，也不能丢条目或错位）。
3. **分批 + 重试**：一次把 1000 条字幕塞给模型，输出一定会被 ``max_tokens`` 截断，
   截断后的 JSON 解析必失败 → 整批白干。分批（默认 40 行 / 约 1200 字）能把
   "截断"的爆炸半径限制在一批；单批失败重试（默认 2 次）再兜不住才保留原文。
   真机实测本地 1.5B 写总结 JSON 会顶到默认的 512 tokens 被切断，所以本地回答
   "括号没配平"（疑似截断）时会**把 token 预算翻倍重问一次**（见
   :meth:`字幕助手._尝试本地`），这比直接降级到规则总结有用得多。
4. **本地优先**：本地 DeepSeek（Ollama）免费、离线、不上传视频内容；
   传了 ``本地模型`` 且可用就优先用它，失败/没传才用 ``AI助手``（云端），
   两者都没有就退化成规则结果（``来源="规则"``），功能降级但不报错。
5. **顺序即真相**：并发批处理（``最大并发``）只影响"谁先算完"，
   回填严格按批次下标进行，并发与串行的结果完全一致。

公开接口
========
``字幕条目``、``字幕助手``、以及模块级的 ``解析SRT`` / ``生成SRT`` /
``读取字幕文件`` / ``写出字幕文件`` / ``时间戳转秒`` / ``秒转时间戳`` / ``时钟文本``。

用法（伪代码，界面装配）::

    from v8_3.AI.字幕助手 import 字幕助手
    助手 = 字幕助手(AI助手=AI.助手, 本地模型=AI.助手.本地模型, 日志回调=追加日志)
    条目们 = 助手.读取字幕文件("/path/movie.srt")
    译文, 统计 = 助手.翻译(条目们, "中文", 进度回调=进度条.更新)
    助手.写出字幕文件("/path/movie.中文.srt", 译文)
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Optional, Sequence

logger = logging.getLogger(__name__)

__all__ = [
    "字幕条目", "字幕助手",
    "解析SRT", "生成SRT", "读取字幕文件", "写出字幕文件",
    "时间戳转秒", "秒转时间戳", "时钟文本",
]

# ============================================================ 常量 / 正则

#: SRT 时间戳：``HH:MM:SS,mmm`` / ``HH:MM:SS.mmm`` / ``MM:SS,mmm`` / ``HH:MM:SS`` 全收。
#: 小时段可选 + 小数分隔符逗号或点 + 毫秒可选，是为了兼容各路工具导出的野生 SRT。
_时间戳模式 = re.compile(
    r"(?:(\d{1,4})\s*[:：]\s*)?(\d{1,2})\s*[:：]\s*(\d{1,2})"
    r"(?:\s*[,.]\s*(\d{1,4}))?")

#: 纯序号行（允许 ``1`` / ``12.`` / ``3、`` 这类写法）
_序号模式 = re.compile(r"^\s*(\d{1,6})\s*[.、)）]?\s*$")

#: 箭头（SRT 的时间行分隔符）
_箭头 = "-->"

#: 音效/噪声标记：``[音乐]``、``（掌声）`` 之类整块去掉后再判断有没有内容
_括号噪声 = re.compile(r"[\[【(（][^\]】)）]*[\]】)）]")

#: 判"等于没字幕"时要忽略的字符（标点、空白、音符、破折号等）
_噪声字符 = set(
    " \t\u3000.,;:!?，。；：！？、…—－-~～·\"'“”‘’()（）[]【】<>《》"
    "♪♫♬♩■□●○◆◇★☆※|/\\_+=*&#@$%^`")

#: 句末标点（合并短句时，"上一条已经说完了"就不该再往下粘）
_句末标点 = "。！？!?…；;."


#: 摘要里出现这些词，基本就是模型把**提示词格式示例**抄回来了（实测：
#: 本地 1.5B 给出的"摘要"就是提示词里的占位文字"整体内容，150 字以内"）
_提示词回声词 = (
    "字以内", "输出格式", "严格 JSON", "严格JSON", "不要解释", "不要 Markdown",
    "关键帧", "小标题", "这一段讲了什么", "字幕如下", "你是视频内容分析助手",
    "章节 2~6 个", "标签 3~6 个", "时间必须取自字幕", "不许编造", "按时间升序",
)


def _摘要不可用(文本: str) -> str:
    """判断 AI 给的摘要能不能用；返回""表示可用，否则返回原因。

    实测两种垃圾摘要（都会直接显示给用户）：
      * **提示词回声**：模型把格式示例原样抄回来 —— "整体内容，150 字以内"；
      * **JSON 片段**：模型把整个对象塞进摘要字段 —— ``"摘要": "视频讲的是…"``。
    """
    文本 = str(文本 or "").strip()
    if len(文本) < 4:
        return "太短"
    if any(词 in 文本 for 词 in _提示词回声词):
        return "像是把提示词格式示例抄回来了"
    if 文本.startswith(("{", "[")) or '"摘要"' in 文本 or "'摘要'" in 文本:
        return "看起来是 JSON 片段"
    紧凑 = 文本.replace(" ", "").replace("\u3000", "")
    if (紧凑.count('":"') >= 2 or 紧凑.count('","') >= 2
            or 紧凑.count('":') >= 2):
        return "看起来是 JSON 片段"
    if len(文本) > 600:
        return "太长"
    return ""


def _取浮(值: Any, 默认: float = 0.0) -> float:
    """宽容地取 float：非数字（None/""/乱码）一律给默认值，绝不抛。"""
    if isinstance(值, bool):
        return float(默认)
    try:
        return float(值)
    except Exception:
        return 默认


def _取整(值: Any, 默认: int = 0) -> int:
    try:
        return int(值)
    except Exception:
        return 默认


def _截断(文本: str, 上限: int) -> str:
    """按字符数截断（超长补省略号）。用于规则降级的章节标题/要点。"""
    文本 = str(文本 or "").strip()
    if 上限 <= 0 or len(文本) <= 上限:
        return 文本
    return 文本[:上限].rstrip() + "……"


# ============================================================ 时间戳

def 时间戳转秒(文本: Any) -> float:
    """把 ``HH:MM:SS,mmm`` 之类的字符串解析成秒。

    解析不出来返回 ``-1.0``（哨兵值而不是抛异常：调用方多是"跳过这一条"，
    用返回值判断比 try/except 更省事）。
    """
    if 文本 is None:
        return -1.0
    匹配 = _时间戳模式.search(str(文本))
    if not 匹配:
        return -1.0
    时, 分, 秒, 毫 = 匹配.groups()
    毫秒 = int((毫 or "0").ljust(3, "0")[:3]) if 毫 else 0
    return (int(时 or 0) * 3600 + int(分 or 0) * 60 + int(秒 or 0)
            + 毫秒 / 1000.0)


def 秒转时间戳(秒: Any, 毫秒: bool = True) -> str:
    """把秒数格式化成 SRT 时间戳；``毫秒=False`` 时输出 ``HH:MM:SS``。

    负数/非数字按 0 处理——播放器拿到 ``-00:00:01`` 这类时间是直接罢工的。
    """
    总毫秒 = int(round(max(0.0, _取浮(秒, 0.0)) * 1000))
    时, 余 = divmod(总毫秒, 3600_000)
    分, 余 = divmod(余, 60_000)
    整秒, 毫 = divmod(余, 1000)
    if 毫秒:
        return f"{时:02d}:{分:02d}:{整秒:02d},{毫:03d}"
    return f"{时:02d}:{分:02d}:{整秒:02d}"


def 时钟文本(秒: Any) -> str:
    """``HH:MM:SS``（章节时间点、给 AI 看的时间戳都用这个，不带毫秒更省 token）。"""
    return 秒转时间戳(秒, 毫秒=False)


# ============================================================ 数据模型

@dataclass
class 字幕条目:
    """一条字幕。

    ``开始秒`` / ``结束秒`` 是 **秒(float)** 而不是字符串：播放器 seek 需要数值，
    字符串时间戳每次都要重新解析，容易在来回转换里丢精度。
    """

    序号: int = 0
    开始秒: float = 0.0
    结束秒: float = 0.0
    文本: str = ""

    def 追加(self, 文本: str) -> None:
        """在已有文本后追加一段（多行字幕、合并短句时用）。

        空串直接忽略：语音识别和 AI 偶尔会回空行，追加空行只会让字幕中间
        多出一个空洞。
        """
        片段 = str(文本 or "").strip()
        if not 片段:
            return
        现有 = str(self.文本 or "").strip()
        self.文本 = f"{现有}\n{片段}" if 现有 else 片段

    def 复制(self) -> "字幕条目":
        """深拷贝一份。

        翻译/合并都基于副本操作：调用方手里的原始条目要能继续当"原文"用
        （播放页常同时保留原文与译文），也避免 UI 线程读到写了一半的对象。
        """
        return 字幕条目(序号=self.序号, 开始秒=self.开始秒,
                        结束秒=self.结束秒, 文本=self.文本)

    def 时长秒(self) -> float:
        """持续时长（负数按 0 算，脏数据不至于把后续合并逻辑带偏）。"""
        return max(0.0, _取浮(self.结束秒) - _取浮(self.开始秒))

    def 是否为空(self) -> bool:
        """文本是否为空（只有空白也算空）。"""
        return not str(self.文本 or "").strip()


# ============================================================ SRT 解析 / 生成

def _是时间行(行: str) -> bool:
    return _箭头 in 行 and _时间戳模式.search(行) is not None


def _拆时间行(行: str) -> Optional[tuple[float, float]]:
    """拆一行 ``开始 --> 结束``；返回 None 表示这一块不可信，整块跳过。

    区分两种情况：

    * 右边**一个数字都没有**（``00:00:05,000 -->``）→ 只是缺结束时间，
      用 ``开始 + 0.5s`` 兜底（避免零时长条目在播放器里"一闪而过点不到"）；
    * 右边有数字但格式不认识（``--> 乱码123``）→ 多半是坏行/串行，
      跳过比猜时间安全：猜错会让后面所有字幕和画面对不上。
    """
    左, _, 右 = str(行).partition(_箭头)
    开始 = 时间戳转秒(左)
    if 开始 < 0:
        return None
    结束 = 时间戳转秒(右)
    if 结束 < 0:
        if str(右).strip():
            return None     # 右边有东西但不是时间 → 坏行，跳过整块
        结束 = 开始 + 0.5   # 右边空的 → 只是缺结束时间，兜底 0.5s
    if 结束 < 开始:
        # 时间倒挂：能解析出来说明两个时间本身是真的，只是写反了，按开始时间兜底
        结束 = 开始 + 0.5
    return 开始, 结束


def 解析SRT(文本: str) -> list[字幕条目]:
    """解析 SRT 文本（**宽容模式**，尽量别把整段字幕判死）。

    字幕来源太杂（人人/机翻/ASR 导出/手改），各家 SRT 的"标准"程度天差地别。
    播放页宁可多解析出几条，也不该因为一个小瑕疵就整个字幕不显示，所以容忍：

    * CRLF / 单独 CR / 开头 BOM；
    * 缺序号（用"上一条 +1"补），序号乱或重复（保留文件里的显式序号）；
    * 毫秒分隔符是逗号或点，毫秒可缺省，时间是 ``MM:SS`` 也能认；
    * 一条字幕多行文本（原样用 ``\\n`` 连起来，播放器自己会折行）；
    * 空行混乱：正文中间的空行被当成噪声跳过，只有"后面跟着新块"的空行才算块边界；
    * 时间行尾部带 ``X1:100 X2:200`` 之类的样式参数（解析时只看时间部分）。

    解析不出时间戳的行一律忽略——猜时间比丢一条更危险（会错位）。
    """
    if not 文本 or not isinstance(文本, str):
        return []
    行们 = (文本.replace("\r\n", "\n").replace("\r", "\n")
            .lstrip("\ufeff").split("\n"))
    总行数 = len(行们)
    条目们: list[字幕条目] = []
    待用序号: Optional[int] = None
    i = 0
    while i < 总行数:
        行 = 行们[i]
        if _是时间行(行):
            时间 = _拆时间行(行)
            if 时间 is None:
                i += 1
                continue
            开始, 结束 = 时间
            i += 1
            文本行: list[str] = []
            while i < 总行数:
                当前 = 行们[i]
                if _是时间行(当前):
                    break
                if not 当前.strip():
                    # 空行有两种身份：①块结束 ②正文里多打了一个回车。看后面跟着什么
                    j = i
                    while j < 总行数 and not 行们[j].strip():
                        j += 1
                    if j >= 总行数:
                        i = j
                        break
                    后续 = 行们[j]
                    if _是时间行(后续) or (
                            _序号模式.match(后续) and j + 1 < 总行数
                            and _是时间行(行们[j + 1])):
                        break       # 真正的块边界：i 停在空行上，外层会跳过它
                    i = j           # 空行夹在正文中间 → 当噪声跳过，继续收文本
                    continue
                序号匹配 = _序号模式.match(当前)
                if 序号匹配 and i + 1 < 总行数 and _是时间行(行们[i + 1]):
                    # 有些 SRT 两块之间没有空行：这一行是下一块的序号，不是正文
                    break
                文本行.append(当前.strip())
                i += 1
            if 待用序号 is not None:
                序号, 待用序号 = 待用序号, None
            else:
                序号 = (条目们[-1].序号 + 1) if 条目们 else 1
            条目们.append(字幕条目(
                序号=序号, 开始秒=开始, 结束秒=结束,
                文本="\n".join([t for t in 文本行 if t])))
            continue
        序号匹配 = _序号模式.match(行)
        if 序号匹配:
            待用序号 = int(序号匹配.group(1))
            i += 1
            continue
        i += 1
    return 条目们


def 生成SRT(条目们: Sequence[字幕条目]) -> str:
    """生成 SRT 文本。

    **序号一律重排成 1..N**：翻译/合并/识别之后，原序号可能出现空洞或重复，
    而 SRT 规范要求序号连续递增，播放器（VLC / PotPlayer / ffmpeg）对乱序号的
    容忍度并不一致。时间戳与文本则原样输出——这个函数不做任何"纠正"。
    """
    块们: list[str] = []
    序号 = 0
    for 条目 in (条目们 or []):
        if 条目 is None:
            continue
        序号 += 1
        开始 = _取浮(getattr(条目, "开始秒", 0.0), 0.0)
        结束 = _取浮(getattr(条目, "结束秒", 开始), 开始)
        if 结束 < 开始:
            结束 = 开始
        文本 = (str(getattr(条目, "文本", "") or "")
                .replace("\r\n", "\n").replace("\r", "\n").strip("\n"))
        块们.append(f"{序号}\n{秒转时间戳(开始)} --> {秒转时间戳(结束)}\n{文本}")
    return "\n\n".join(块们) + ("\n" if 块们 else "")


def _解码字幕字节(数据: bytes) -> tuple[str, str]:
    """按 UTF-8(BOM) → UTF-8 → GB18030 → Big5 的顺序试解码，返回 (文本, 编码)。

    先试 ``utf-8-sig``：它同时覆盖"带 BOM"和"不带 BOM 的纯 UTF-8"，
    而且 GBK 中文（如 ``D6 D0``）几乎不可能通过 UTF-8 的严格校验，
    所以这个顺序不会把 GBK 字幕误判成 UTF-8。
    """
    for 编码 in ("utf-8-sig", "utf-8", "gb18030", "big5"):
        try:
            return 数据.decode(编码), 编码
        except UnicodeDecodeError:
            continue
    return 数据.decode("utf-8", errors="replace"), "utf-8(容错)"


def 读取字幕文件(路径: str) -> list[字幕条目]:
    """读字幕文件并解析（自动识别 UTF-8 / UTF-8-BOM / GBK 等编码）。

    **读宽容**：文件不存在、权限不足、编码全试失败 → 返回空列表并记日志。
    对播放页来说"没字幕"和"字幕文件坏了"是同一个体验（继续放视频），
    为了这个弹异常不值得；调用方可以用 :meth:`字幕助手.无字幕判定` 区分。
    """
    路径 = str(路径 or "").strip()
    if not 路径:
        return []
    try:
        with open(路径, "rb") as f:
            数据 = f.read()
    except Exception as e:  # noqa: BLE001 - 见 docstring：读失败就是"没字幕"
        logger.warning("[字幕] 读取字幕文件失败 %s：%s", 路径, e)
        return []
    文本, 编码 = _解码字幕字节(数据)
    logger.debug("[字幕] %s 以 %s 解码，%d 字节", 路径, 编码, len(数据))
    return 解析SRT(文本)


def 写出字幕文件(路径: str, 条目们: Sequence[字幕条目]) -> str:
    """写出 UTF-8（无 BOM）SRT 文件，返回实际写入的路径。

    **写严格**：写盘失败（目录不可建、磁盘满、只读）直接抛 ``OSError``。
    读失败顶多没字幕，写失败必须让用户知道——否则用户以为"翻译好了"，
    结果文件根本没落盘。:meth:`字幕助手.翻译文件` 会把异常转成 ``成功=False``。
    """
    路径 = str(路径 or "").strip()
    if not 路径:
        raise ValueError("写出字幕文件需要非空路径")
    目录 = os.path.dirname(os.path.abspath(路径))
    if 目录:
        os.makedirs(目录, exist_ok=True)
    # 显式 newline="\n"：SRT 用 LF，别让 Windows 上写成 CRLF（虽然也能播，
    # 但同一份字幕在不同机器上 diff 会满屏差异）。
    with open(路径, "w", encoding="utf-8", newline="\n") as f:
        f.write(生成SRT(条目们))
    return 路径


def _解析载荷(文本: Any) -> Any:
    """把模型输出解析成 dict / list；解析不出来返回 None。

    小模型（尤其本地 1.5B）很爱把 JSON 包在 ```json 围栏里，或者前后加一句
    "好的，以下是翻译："。所以三级兜底：整体 → 去围栏 → 掐头去尾取最外层括号。
    """
    if 文本 is None:
        return None
    if isinstance(文本, (dict, list)):
        return 文本
    文本 = str(文本).strip()
    if not 文本:
        return None
    候选 = [文本]
    围栏 = re.search(r"```(?:json)?\s*(.+?)```", 文本, re.DOTALL)
    if 围栏:
        候选.append(围栏.group(1).strip())
    for 起, 止 in (("{", "}"), ("[", "]")):
        i, j = 文本.find(起), 文本.rfind(止)
        if 0 <= i < j:
            候选.append(文本[i:j + 1])
    for 段 in 候选:
        try:
            return json.loads(段)
        except Exception:
            continue
    return None


def _像被截断(文本: str) -> bool:
    """粗略判断模型输出是否被 ``max_tokens`` 截断。

    判据（任一命中即算）：括号/方括号没配平，或结尾停在一个"话没说完"的符号上。
    小模型写长 JSON（尤其带章节数组的总结）很容易顶到上限被切断，
    这时把 token 预算翻倍重问一次，往往就能拿到完整 JSON。
    """
    文本 = str(文本 or "").strip()
    if not 文本:
        return False
    if 文本.count("{") > 文本.count("}") or 文本.count("[") > 文本.count("]"):
        return True
    return 文本.endswith(("\"", ",", ":", "\\", "，", "：", "…"))


# ============================================================ 主类

def _对象转字典(对象) -> dict:
    """把任意对象（dataclass/普通类）按公开属性转成 dict。

    只读属性名，不调用方法；识别器返回 dataclass 时用它兜底取值。
    """
    结果: dict = {}
    try:
        for 名 in dir(对象):
            if 名.startswith("_"):
                continue
            值 = getattr(对象, 名, None)
            if callable(值):
                continue
            结果[名] = 值
    except Exception:
        return {}
    # dataclass 的属性在 __dict__ 里更准
    try:
        结果.update({k: v for k, v in vars(对象).items()
                  if not str(k).startswith("_")})
    except Exception:
        pass
    return 结果


class 字幕助手:
    """AI 字幕助手（纯逻辑，无 Qt 依赖）。

    构造
    ====
    ``字幕助手(AI助手=None, 日志回调=None, 本地模型=None, *,
                分批行数=40, 最大并发=1, 每批重试=2)``

    * ``AI助手``：``AI智能助手``（或任何有 ``是否可用()`` /
      ``请求结构化策略(系统提示, 用户消息)`` 的对象），走云端；
    * ``本地模型``：``本地模型客户端``（鸭子类型：``检测()`` / ``对话(...)``），
      **优先本地**——免费、离线、内容不出本机；
    * ``日志回调``：``回调(文本: str)``，用于把进度写进界面日志；
    * ``分批行数`` / ``最大并发`` / ``每批重试``：见模块 docstring 的设计原则 3、5。
    """

    #: 一批最多多少字符。行数没超但每行都是长句时，光看行数会把本地小模型撑爆
    #: （1.5B 的 num_ctx 通常 4096），所以行数 + 字符数双重限流。
    每批最大字数 = 1200
    #: 总结时送给 AI 的字幕文本上限（字符）。超长视频靠抽样而不是截断，
    #: 否则 AI 只看到开头几分钟，章节会全挤在片头。
    总结文本上限 = 6000
    #: 本地模型单次输出预算；0 = 不干预，用 ``本地模型客户端`` 自己的配置。
    本地最大tokens = 0
    #: 本地回答疑似被 max_tokens 截断时，token 预算翻倍重试的上限（0 = 不重试）
    截断重试上限 = 2048
    #: 规则降级时按时间均匀分章的段数上下限
    规则章节上限 = 5
    规则章节下限 = 2
    #: 合并短句：两条之间最大允许间隙，超过就是换镜头/换话题，不该粘一起
    合并最大间隔秒 = 1.5
    #: 规则降级摘要的目标字数
    规则摘要字数 = 180
    #: 标签最多保留几个
    标签上限 = 8

    def __init__(self, AI助手=None, 日志回调=None, 本地模型=None, *,
                 分批行数: int = 40, 最大并发: int = 1, 每批重试: int = 2):
        self.AI助手 = AI助手
        self.日志回调 = 日志回调
        self.本地模型 = 本地模型
        self.分批行数 = max(1, _取整(分批行数, 40) or 1)
        self.最大并发 = max(1, _取整(最大并发, 1) or 1)
        self.每批重试 = max(0, _取整(每批重试, 2))
        self._锁 = threading.RLock()
        #: 本地模型可用性的进程内缓存（None = 还没探测过）
        self._本地可用缓存: Optional[bool] = None

    # ---------------------------------------------------- 日志 / 进度

    def _日志(self, 文本: str) -> None:
        回调 = self.日志回调
        if callable(回调):
            try:
                回调(f"[字幕] {文本}")
            except Exception:      # 日志回调是界面代码，绝不能因为它炸了业务
                pass
        logger.debug("[字幕] %s", 文本)

    def _报进度(self, 回调, 阶段: str, 已完成: int, 总数: int) -> None:
        """调进度回调，兼容三种签名（项目里主流的 ``(阶段, 已完成, 总数)`` 优先）。

        为什么容忍三种：播放页/引擎/子进程桥各自传进来的回调签名不完全统一，
        与其让调用方包一层适配，不如这里多试两次；回调内部抛错一律吞掉，
        进度条坏掉不该中断翻译。
        """
        if 回调 is None:
            return
        已完成, 总数 = int(已完成), int(总数)
        try:
            回调(阶段, 已完成, 总数)
            return
        except TypeError:
            pass
        except Exception:
            return
        try:
            回调(已完成, 总数)
            return
        except Exception:
            pass
        try:
            回调(已完成 / float(总数 or 1))
        except Exception:
            pass

    # ---------------------------------------------------- SRT 解析 / 生成

    def 解析SRT(self, 文本: str) -> list[字幕条目]:
        """解析 SRT 文本（= 模块级 :func:`解析SRT`，播放页只持有助手对象也能用）。"""
        return 解析SRT(文本)

    def 生成SRT(self, 条目们: Sequence[字幕条目]) -> str:
        """生成 SRT 文本（= 模块级 :func:`生成SRT`；序号一律重排为 1..N）。"""
        return 生成SRT(条目们)

    def 读取字幕文件(self, 路径: str) -> list[字幕条目]:
        """读字幕文件（自动识别 UTF-8 / UTF-8-BOM / GBK；失败返回空列表）。"""
        条目们 = 读取字幕文件(路径)
        self._日志(f"读取字幕 {路径 or '(空路径)'}：{len(条目们)} 条")
        return 条目们

    def 写出字幕文件(self, 路径: str, 条目们: Sequence[字幕条目]) -> str:
        """写出 UTF-8 SRT 文件并返回实际路径；写失败抛 ``OSError``（调用方自己提示）。"""
        列表 = list(条目们 or [])
        实际路径 = 写出字幕文件(路径, 列表)
        self._日志(f"写出字幕 {实际路径}：{len(列表)} 条")
        return 实际路径

    # ---------------------------------------------------- AI 来源判定

    def 重置本地状态(self) -> bool:
        """丢掉本地模型可用性缓存并重新探测（界面"启动本地模型"后调一次）。"""
        with self._锁:
            self._本地可用缓存 = None
        return self.本地是否可用()

    def 本地是否可用(self) -> bool:
        """本地模型是否可用（结果缓存；调用失败时会被置为不可用，避免连环超时）。"""
        with self._锁:
            if self._本地可用缓存 is None:
                self._本地可用缓存 = self._探测本地可用()
            return bool(self._本地可用缓存)

    def _探测本地可用(self) -> bool:
        """按鸭子类型探测本地模型，顺序：显式配置 → ``检测()`` → ``可用()`` → 乐观。

        最后一档"乐观"是给测试用的一次性假客户端留的口子（它只有 ``对话``，
        没法提前自证可用）；真调用失败会立刻把缓存打成不可用，
        所以乐观不会造成"每批都撞一次墙"。
        """
        客户端 = self.本地模型
        if 客户端 is None:
            return False
        配置 = getattr(客户端, "配置", None)
        启用 = 配置.get("启用") if isinstance(配置, dict) \
            else getattr(配置, "启用", None)
        if 启用 is False:
            self._日志("本地模型未启用（配置.启用=False），本次不走本地推理")
            return False
        检测 = getattr(客户端, "检测", None)
        if callable(检测):
            try:
                状态 = 检测()
            except Exception as e:  # noqa: BLE001
                self._日志(f"本地模型检测异常：{e}")
                return False
            if isinstance(状态, bool):
                return 状态
            可用 = getattr(状态, "可用", None)
            if 可用 is None:
                return True         # 检测返回了说不清的东西：乐观，失败会自愈
            if not 可用:
                原因 = (getattr(状态, "错误", "") or getattr(状态, "说明", "")
                      or "本地模型不可用")
                self._日志(str(原因).splitlines()[0][:120])
            return bool(可用)
        可用方法 = getattr(客户端, "可用", None)
        if callable(可用方法):
            try:
                值 = 可用方法()
            except Exception:       # noqa: BLE001
                return False
            if isinstance(值, tuple):
                值 = 值[0] if 值 else False
            return bool(值)
        return True

    def _标记本地不可用(self, 原因: str = "") -> None:
        """本地调用失败后立刻熔断到"本次会话不再试"。

        为什么：本地服务没起来时，一次调用要等 TCP 超时（秒级）。25 批 × 3 次重试
        能把"翻译"卡成几十分钟，用户只会觉得软件死了。
        """
        with self._锁:
            self._本地可用缓存 = False
        if 原因:
            self._日志(f"本地模型调用失败，本次改用云端/规则：{原因}")

    def 有AI来源(self) -> bool:
        """本地或云端至少有一个可用（没有就别空转重试了）。"""
        if self.本地是否可用():
            return True
        return self._云端可用()

    def _云端可用(self) -> bool:
        助手 = self.AI助手
        if 助手 is None:
            return False
        是否可用 = getattr(助手, "是否可用", None)
        if callable(是否可用):
            try:
                return bool(是否可用())
            except Exception:       # noqa: BLE001
                return False
        return callable(getattr(助手, "请求结构化策略", None))

    # ---------------------------------------------------- AI 调用

    def _请求结构化(self, 系统提示: str, 用户消息: str) -> tuple[Any, str]:
        """一次结构化请求：本地优先 → 云端兜底。

        返回 ``(载荷, 来源)``；载荷为 ``None`` 表示两个来源都没给出可解析的结果，
        来源为 ``"规则"``。本方法**不抛异常**。
        """
        载荷 = self._尝试本地(系统提示, 用户消息)
        if 载荷 is not None:
            return 载荷, "本地模型"
        载荷 = self._尝试云端(系统提示, 用户消息)
        if 载荷 is not None:
            return 载荷, "云端"
        return None, "规则"

    def _尝试本地(self, 系统提示: str, 用户消息: str) -> Any:
        if not self.本地是否可用():
            return None
        预算 = self._本地token预算()
        for 第次 in (1, 2):
            结果 = self._本地对话(用户消息, 系统提示, 预算)
            if 结果 is None:
                return None                 # 调用失败，_本地对话 已经熔断
            成功, 文本 = self._取本地结果(结果)
            if not 成功 or not str(文本 or "").strip():
                原因 = (结果.get("错误") if isinstance(结果, dict)
                      else getattr(结果, "错误", "")) or "本地模型返回空内容"
                self._标记本地不可用(str(原因))
                return None
            载荷 = _解析载荷(文本)
            if 载荷 is not None:
                return 载荷
            更大 = self._更大预算(预算)
            if 第次 == 1 and 更大 > 预算 and _像被截断(文本):
                # 真机实测：deepseek-r1:1.5b 的总结 JSON 会顶到 max_tokens（默认 512）
                # 被切断，JSON 缺右括号必然解析失败。这时"预算翻倍再来一次"通常就能
                # 拿到完整 JSON，比直接降级到规则总结有价值得多；只有第一次尝试且
                # 疑似截断才会重试，不会让每批都白等一倍时间。
                self._日志(f"本地模型回答疑似被 max_tokens 截断，"
                         f"改用 {更大} tokens 重试一次")
                预算 = 更大
                continue
            # 服务是好的，只是这次回答不是 JSON —— 不熔断（下一批还有机会），
            # 但这一批交给云端/规则，别拿半截文本硬凑译文。
            self._日志(f"本地模型回答不是 JSON：{str(文本).strip()[:60]}")
            return None
        return None

    def _本地对话(self, 用户消息: str, 系统提示: str, 预算: int) -> Any:
        """调一次本地模型，返回原始结果；失败返回 None 并标记本地不可用。"""
        对话 = getattr(self.本地模型, "对话", None)
        if not callable(对话):
            self._标记本地不可用("本地模型没有 对话() 方法")
            return None
        参数: dict = {"最大tokens": int(预算)} if 预算 else {}
        try:
            return 对话(用户消息, 系统提示, **参数)
        except TypeError:
            # 兼容只实现"最小签名"的老客户端/假客户端（不认识 最大tokens 关键字）
            try:
                return 对话(用户消息, 系统提示)
            except Exception as e:  # noqa: BLE001
                self._标记本地不可用(f"{type(e).__name__}: {e}")
                return None
        except Exception as e:  # noqa: BLE001 - 超时/断连/任何异常都不许冒泡
            self._标记本地不可用(f"{type(e).__name__}: {e}")
            return None

    def _本地token预算(self) -> int:
        """本次请求的输出预算：优先助手自己的设置，其次读本地客户端的配置。"""
        if self.本地最大tokens:
            return max(0, _取整(self.本地最大tokens, 0))
        配置 = getattr(self.本地模型, "配置", None)
        值 = 配置.get("最大tokens") if isinstance(配置, dict) \
            else getattr(配置, "最大tokens", 0)
        return max(0, _取整(值, 0))

    def _更大预算(self, 预算: int) -> int:
        """截断重试的新预算（预算翻倍，封顶 ``截断重试上限``）。"""
        上限 = max(0, _取整(self.截断重试上限, 0))
        if 上限 <= 0 or 预算 >= 上限:
            return 预算
        基础 = 预算 if 预算 > 0 else 512     # 问不到客户端预算时按 Ollama 默认 512 起步
        return min(上限, 基础 * 2)

    @staticmethod
    def _取本地结果(结果) -> tuple[bool, str]:
        """从 ``对话结果``（或同构 dict / 裸字符串）里取 (成功, 文本)。"""
        if isinstance(结果, dict):
            文本 = str(结果.get("内容") or 结果.get("推理内容") or "")
            return bool(结果.get("成功", bool(文本.strip()))), 文本
        if isinstance(结果, str):
            return bool(结果.strip()), 结果
        if 结果 is None:
            return False, ""
        文本 = str(getattr(结果, "内容", "") or "")
        if not 文本.strip():
            文本 = str(getattr(结果, "推理内容", "") or "")
        return bool(getattr(结果, "成功", bool(文本.strip()))), 文本

    def _尝试云端(self, 系统提示: str, 用户消息: str) -> Any:
        助手 = self.AI助手
        if 助手 is None or not self._云端可用():
            return None
        请求 = getattr(助手, "请求结构化策略", None)
        if not callable(请求):
            return None
        try:
            载荷 = 请求(系统提示, 用户消息)
        except Exception as e:  # noqa: BLE001
            self._日志(f"云端请求失败：{type(e).__name__}: {e}")
            return None
        if isinstance(载荷, str):
            载荷 = _解析载荷(载荷)
        if not 载荷:                # {} / [] / None / "" 都算"没拿到结果"
            return None
        return 载荷

    # ---------------------------------------------------- 分批

    def _分批(self, 条目们: Sequence[字幕条目]) -> list[list[字幕条目]]:
        """按"行数 + 字符数"切批。

        为什么要切：一次几百条会被 ``max_tokens`` 截断，截断的 JSON 一定解析失败，
        整批白干；切小之后最坏情况只损失一批（还有重试兜底）。
        """
        批次们: list[list[字幕条目]] = []
        当前: list[字幕条目] = []
        当前字数 = 0
        for 条目 in 条目们:
            字数 = len(str(条目.文本 or ""))
            if 当前 and (len(当前) >= self.分批行数
                      or 当前字数 + 字数 > self.每批最大字数):
                批次们.append(当前)
                当前, 当前字数 = [], 0
            当前.append(条目)
            当前字数 += 字数
        if 当前:
            批次们.append(当前)
        return 批次们

    # ---------------------------------------------------- 翻译

    def 翻译(self, 条目们: Sequence[字幕条目], 目标语言: str = "中文",
             进度回调=None) -> tuple[list[字幕条目], dict]:
        """把字幕逐条翻译成 ``目标语言``，返回 ``(译文条目, 统计)``。

        * **时间戳零漂移**：译文条目是输入条目的副本，只有 ``文本`` 被替换；
          结束前还会做一次自检回填（``_修复时间戳``），任何意外漂移都会被纠正。
        * **分批 + 重试**：单批失败重试 ``每批重试`` 次，仍失败则整批**保留原文**
          并计入 ``失败批``——条目数、顺序、时间轴与输入完全一致。
        * **并发不改顺序**：``最大并发 > 1`` 时批次并行执行，但回填严格按批次下标，
          结果与串行完全一致。
        * 输入为空时返回 ``([], 统计)``，不发任何 AI 请求。
        """
        原条目们 = [e for e in (条目们 or []) if e is not None]
        开始时刻 = time.time()
        统计 = {"批次": 0, "成功批": 0, "失败批": 0,
                "原文保留行数": 0, "用时秒": 0.0, "来源": "规则"}
        if not 原条目们:
            统计["用时秒"] = 0.0
            return [], 统计

        结果 = [e.复制() for e in 原条目们]        # 副本：绝不改调用方的数据
        批次们 = self._分批(结果)
        统计["批次"] = len(批次们)
        起点们: list[int] = []
        游标 = 0
        for 批次 in 批次们:
            起点们.append(游标)
            游标 += len(批次)

        # ---- 执行（串行 or 并发；并发只影响完成顺序，不影响回填顺序）----
        产出: list[tuple[int, Optional[list[str]], str, int]] = []
        if self.最大并发 <= 1 or len(批次们) <= 1:
            for 下标, 批次 in enumerate(批次们):
                产出.append((下标, *self._翻译一批(批次, 目标语言)))
        else:
            with ThreadPoolExecutor(
                    max_workers=min(self.最大并发, len(批次们))) as 池:
                期货 = {池.submit(self._翻译一批, 批次, 目标语言): 下标
                      for 下标, 批次 in enumerate(批次们)}
                for 期货项 in as_completed(期货):
                    下标 = 期货[期货项]
                    try:
                        结果项 = 期货项.result()
                    except Exception as e:  # noqa: BLE001 - 双保险
                        logger.warning("[字幕] 批 %s 线程异常：%s", 下标, e)
                        结果项 = (None, "规则", len(批次们[下标]))
                    产出.append((下标, *结果项))
        产出.sort(key=lambda 项: 项[0])

        # ---- 回填（严格按原顺序，进度也按顺序报）----
        来源计数 = {"本地模型": 0, "云端": 0}
        已完成 = 0
        for 下标, 译文, 来源, 保留行数 in 产出:
            批次 = 批次们[下标]
            if 译文 is None:
                统计["失败批"] += 1
            else:
                统计["成功批"] += 1
                if 来源 in 来源计数:
                    来源计数[来源] += 1
                for 偏移, 文本 in enumerate(译文):
                    条 = 结果[起点们[下标] + 偏移]
                    if 文本:
                        条.文本 = 文本
            # 「原文保留行数」= 整批失败（该批全部行）或成功批里的空译文行，
            # 两种情况都由 _翻译一批 数好放进 保留行数：宁可让用户看到原文，
            # 也不能出现空白字幕（空白一闪比没翻译更让人困惑）
            统计["原文保留行数"] += 保留行数
            已完成 += len(批次)
            self._报进度(进度回调, "翻译", 已完成, len(结果))

        统计["来源"] = ("本地模型" if 来源计数["本地模型"] else
                    "云端" if 来源计数["云端"] else "规则")
        统计["用时秒"] = round(time.time() - 开始时刻, 3)
        self._修复时间戳(结果, 原条目们)
        self._日志(f"翻译完成：{len(结果)} 条 / {统计['批次']} 批，"
                 f"成功 {统计['成功批']}、失败 {统计['失败批']}，"
                 f"来源 {统计['来源']}，用时 {统计['用时秒']}s")
        return 结果, 统计

    def _翻译一批(self, 批次: Sequence[字幕条目],
                 目标语言: str) -> tuple[Optional[list[str]], str, int]:
        """翻译一批，返回 ``(译文列表 or None, 来源, 该批保留原文的行数)``。"""
        系统提示, 用户消息 = self._翻译提示(批次, 目标语言)
        保留 = len(批次)
        最近来源 = "规则"
        for 次 in range(1, self.每批重试 + 2):
            try:
                载荷, 来源 = self._请求结构化(系统提示, 用户消息)
            except Exception as e:  # noqa: BLE001 - "绝不抛到调用方"的最后一道闸
                logger.warning("[字幕] 第 %s 次请求异常：%s", 次, e)
                载荷, 来源 = None, "规则"
            if 载荷 is None:
                if not self.有AI来源():
                    break           # 本地/云端都没有，重试只是空转
                continue
            最近来源 = 来源
            译文 = self._抽取译文(载荷, len(批次))
            if 译文 is not None:
                return 译文, 来源, sum(1 for t in 译文 if not t)
            self._日志(f"第 {次} 次返回条数对不上（期望 {len(批次)} 条），重试或保留原文")
        return None, 最近来源, 保留

    def _翻译提示(self, 批次: Sequence[字幕条目],
                 目标语言: str) -> tuple[str, str]:
        """翻译用的系统提示 + 用户消息。

        用户消息用 JSON 数组传"纯文本"（不带序号、不带时间戳）：序号会让模型
        自作聪明重排，时间戳会被它顺手改写——两个都是错位的源头。
        """
        条数 = len(批次)
        # 真机实测：本地 1.5B 小模型在没有例子时经常"原文照抄"（把英文原样塞进译文），
        # 给一对中英示例能明显改善；但内置示例只有"中文"这一对，目标语言不是中文时
        # 就不给例子，免得把模型带到中英互译的套路上。
        示例 = ""
        if "中" in str(目标语言):
            示例 = ('示例：输入 {"条数": 2, "字幕": ["Hello everyone.", '
                  '"Let\'s get started."]}\n'
                  '     输出 {"译文": ["大家好。", "我们开始吧。"]}\n')
        系统提示 = (
            f"你是字幕翻译引擎，把用户给的字幕逐条翻译成{目标语言}。\n"
            "【硬性要求】\n"
            f"1. 逐条翻译，译文条数必须严格等于输入条数（{条数} 条），"
            "不许合并、拆分、漏译或补译；\n"
            "2. 只翻译文本内容，人名/术语/数字照原样保留；\n"
            "3. 不要输出时间戳、序号、原文或任何解释；\n"
            f'4. 只输出 JSON：{{"译文": ["第1条译文", "第2条译文", ...]}}，'
            f"数组长度必须是 {条数}。\n"
            + 示例
        )
        用户消息 = json.dumps(
            {"目标语言": 目标语言, "条数": 条数,
             "字幕": [str(e.文本 or "") for e in 批次]},
            ensure_ascii=False)
        return 系统提示, 用户消息

    @staticmethod
    def _抽取译文(载荷: Any, 条数: int) -> Optional[list[str]]:
        """从模型返回里抽出译文列表；条数对不上就返回 None（整批保留原文）。

        为什么"少一条"就整批放弃：少一条意味着模型可能合并了两句，
        后面所有行都会前移一位——**错位比不翻译糟糕得多**（字幕会张冠李戴）。
        多出来的（模型爱在末尾复述一句）只截断，前 N 条仍然是对齐的。
        """
        列表 = _找列表(载荷)
        if 列表 is None:
            return None
        文本们 = [_取条目文本(项).strip() for 项 in 列表]
        if len(文本们) < 条数:
            return None
        return 文本们[:条数]

    @staticmethod
    def _修复时间戳(结果: Sequence[字幕条目],
                   原条目们: Sequence[字幕条目]) -> int:
        """自检并回填时间戳（正常路径下修正数恒为 0）。

        这只是"防手抖"：将来若有人在回填时顺手改了时间，这里会把漂移摁回去，
        保证"时间戳零漂移"是结构性保证而不是"记得别改"。
        """
        修正 = 0
        for 新, 原 in zip(结果, 原条目们):
            if 新.开始秒 != 原.开始秒 or 新.结束秒 != 原.结束秒:
                新.开始秒, 新.结束秒 = 原.开始秒, 原.结束秒
                修正 += 1
        if 修正:
            logger.warning("[字幕] 检测到 %d 条时间戳漂移，已按原文回填", 修正)
        return 修正

    # ---------------------------------------------------- 翻译文件

    def 翻译文件(self, 源路径: str, 目标路径: str = "",
                 目标语言: str = "中文", 进度回调=None) -> dict:
        """读源字幕 → 翻译 → 写目标字幕，返回
        ``{成功, 输出路径, 条目数, 统计, 错误}``。

        ``目标路径`` 省略时输出到源文件旁边：``movie.srt`` → ``movie.中文.srt``。
        """
        结果 = {"成功": False, "输出路径": "", "条目数": 0, "统计": {
            "批次": 0, "成功批": 0, "失败批": 0, "原文保留行数": 0,
            "用时秒": 0.0, "来源": "规则"}, "错误": ""}
        条目们 = self.读取字幕文件(源路径)
        if not 条目们:
            结果["错误"] = f"没有从“{源路径 or '(空路径)'}”解析出字幕内容"
            return 结果
        译文, 统计 = self.翻译(条目们, 目标语言, 进度回调)
        结果["统计"] = 统计
        结果["条目数"] = len(译文)
        输出路径 = str(目标路径 or "").strip() or _默认输出路径(源路径, 目标语言)
        try:
            写出字幕文件(输出路径, 译文)
        except Exception as e:  # noqa: BLE001
            结果["错误"] = f"写出字幕失败：{type(e).__name__}: {e}"
            return 结果
        结果["成功"] = True
        结果["输出路径"] = 输出路径
        self._日志(f"已生成 {输出路径}（{len(译文)} 条，来源 {统计.get('来源')}）")
        return 结果

    # ---------------------------------------------------- 总结

    def 总结(self, 条目们: Sequence[字幕条目], 标题: str = "",
             进度回调=None) -> dict:
        """AI 总结视频内容，返回
        ``{成功, 摘要, 章节:[{时间, 秒, 标题, 要点}], 标签, 来源, 错误}``。

        章节保证**按时间升序**，且 ``时间`` 由 ``秒`` 重新格式化（两者永远自洽），
        播放器拿 ``秒``（float）直接 ``setPosition`` 即可跳转。
        AI 不可用或返回不可用时按规则降级：截断摘要 + 按时间均匀分章，
        此时 ``来源="规则"``、``成功=True``（降级结果依然可用，不该报错）。
        """
        结果: dict = {"成功": False, "摘要": "", "章节": [], "标签": [],
                    "来源": "规则", "错误": ""}
        有序 = sorted([e for e in (条目们 or []) if e is not None],
                    key=lambda e: _取浮(e.开始秒, 0.0))
        if self.无字幕判定(有序):
            结果["错误"] = "没有可总结的字幕内容"
            return 结果

        self._报进度(进度回调, "总结", 0, 1)
        系统提示, 用户消息 = self._总结提示(有序, 标题)
        载荷, 来源 = None, "规则"
        try:
            载荷, 来源 = self._请求结构化(系统提示, 用户消息)
        except Exception as e:  # noqa: BLE001
            logger.warning("[字幕] 总结请求异常：%s", e)
        解析结果 = self._解析总结载荷(载荷, 有序) if 载荷 is not None else None
        self._报进度(进度回调, "总结", 1, 1)
        if 解析结果 is None:
            self._日志("AI 总结不可用，按规则降级（截断摘要 + 均匀分章）")
            规则 = self._规则总结(有序)
            规则["说明"] = "AI 不可用或返回不可用，已按规则降级"
            return 规则
        结果.update(解析结果)
        结果["成功"] = True
        结果["来源"] = 来源 if 来源 in ("本地模型", "云端") else "规则"
        if not 结果["章节"]:
            # AI 给了摘要但没给出可用章节：摘要用 AI 的，章节用规则的，别让时间轴空着
            结果["章节"] = self._规则章节(有序)
        self._日志(f"总结完成：来源 {结果['来源']}，"
                 f"{len(结果['章节'])} 个章节，{len(结果['标签'])} 个标签")
        return 结果

    def _总结提示(self, 条目们: Sequence[字幕条目],
                 标题: str = "") -> tuple[str, str]:
        前提 = f"视频标题：{标题}\n" if str(标题 or "").strip() else ""
        系统提示 = (
            "你是视频内容分析助手。根据带时间戳的字幕，输出结构化总结。\n"
            "【输出格式 - 严格 JSON】\n"
            '{"摘要": "整体内容，150 字以内", '
            '"章节": [{"时间": "00:12:34", "标题": "小标题", "要点": "这一段讲了什么"}], '
            '"标签": ["关键词", "关键词"]}\n'
            "【硬性要求】\n"
            "1. 章节按时间升序，时间必须取自字幕里出现过的时间点，不许编造；\n"
            "2. 章节 2~6 个，标签 3~6 个；\n"
            "3. 只输出 JSON，不要解释、不要 Markdown 代码块。"
        )
        用户消息 = 前提 + "字幕如下，每行格式为 [时间] 正文：\n" + self._总结用文本(条目们)
        return 系统提示, 用户消息

    def _总结用文本(self, 条目们: Sequence[字幕条目]) -> str:
        """给 AI 的正文：超长时**均匀抽样**而不是截断。

        为什么抽样：一部电影的完整字幕有几万字，本地 1.5B 模型上下文只有 4096，
        直接塞进去会被静默截断，AI 只能看到片头，章节就全挤在前几分钟。
        均匀抽样（保留首尾）能让章节覆盖全片，时间戳还在，跳转依然准。
        """
        文本 = self.纯文本(条目们)
        if len(文本) <= self.总结文本上限:
            return 文本
        行们 = 文本.split("\n")
        平均行长 = max(1, len(文本) // max(1, len(行们)))
        保留行数 = max(2, self.总结文本上限 // 平均行长)
        步长 = max(1, math.ceil(len(行们) / 保留行数))
        抽样 = 行们[::步长]
        while len(抽样) > 2 and len("\n".join(抽样)) > self.总结文本上限:
            抽样 = 抽样[::2]
        if 行们[-1] not in 抽样:
            抽样 = 抽样 + [行们[-1]]
        return "\n".join(抽样)

    def _解析总结载荷(self, 载荷: Any,
                     条目们: Sequence[字幕条目]) -> Optional[dict]:
        """校验并归一化 AI 总结；完全不可用时返回 None（交给规则降级）。"""
        if isinstance(载荷, list) and 载荷 and isinstance(载荷[0], dict):
            载荷 = 载荷[0]
        if not isinstance(载荷, dict):
            return None

        摘要 = ""
        for 键 in ("摘要", "总结", "summary", "概述", "简介"):
            值 = 载荷.get(键)
            if 值:
                摘要 = str(值).strip()
                break

        原章节 = None
        for 键 in ("章节", "chapters", "分段", "时间线"):
            值 = 载荷.get(键)
            if 值:
                原章节 = 值
                break
        if isinstance(原章节, dict):
            # {"00:12:34": "标题"} 这种简写也认
            原章节 = [{"时间": k, "标题": v} if not isinstance(v, dict)
                    else {"时间": k, **v} for k, v in 原章节.items()]
        时长 = max([_取浮(e.结束秒, 0.0) for e in 条目们] or [0.0])
        章节: list[dict] = []
        已用秒: set[float] = set()
        for 项 in (原章节 if isinstance(原章节, list) else []):
            秒 = _章节秒(项)
            if 秒 is None or 秒 < 0:
                continue
            # 夹在 [0, 视频时长] 内：AI 常把"00:00:00"写成负数或编出超过片长的时间，
            # 越界的时间点在播放器里跳不动
            秒 = max(0.0, min(float(秒), 时长)) if 时长 > 0 else max(0.0, float(秒))
            键 = round(秒, 3)
            if 键 in 已用秒:
                continue
            已用秒.add(键)
            标题 = 要点 = ""
            if isinstance(项, dict):
                标题 = str(项.get("标题") or 项.get("title") or
                         项.get("名称") or "").strip()
                要点 = str(项.get("要点") or 项.get("摘要") or 项.get("内容") or
                         项.get("description") or 项.get("summary") or "").strip()
            elif isinstance(项, str):
                # "00:12:34 开场介绍" 这种一行式章节：把时间戳从标题里摘掉
                标题 = (_时间戳模式.sub("", 项, count=1).strip(" -—–:：、,，")
                      or 项.strip())
            if not 标题 and not 要点:
                continue
            章节.append({"时间": 时钟文本(秒), "秒": float(秒),
                       "标题": 标题 or _截断(要点, 20), "要点": 要点})
        章节.sort(key=lambda 章: 章["秒"])     # 章节必须按时间升序才能给播放器做跳转
        标签 = self._取标签(载荷)
        说明 = ""
        原因 = _摘要不可用(摘要) if 摘要 else ""
        if 原因:
            # AI 摘要不能用（提示词回声/JSON 片段/太长太短）→ 换成规则摘要，
            # 章节和标签只要是好的就留着，别把整次 AI 结果都扔掉。
            self._日志(f"AI 摘要不可用（{原因}），改用规则摘要：{摘要[:40]!r}")
            规则摘要 = self._规则总结(条目们).get("摘要", "")
            摘要 = 规则摘要
            说明 = f"AI 摘要不可用（{原因}），已用规则摘要替代"
        if len(摘要) > 600:
            摘要 = 摘要[:600].rstrip() + "…"
        if not 摘要 and not 章节:
            return None
        return {"摘要": 摘要, "章节": 章节, "标签": 标签, "错误": "",
                **({"说明": 说明} if 说明 else {})}

    def _取标签(self, 载荷: dict) -> list[str]:
        原 = None
        for 键 in ("标签", "tags", "关键词", "keywords"):
            值 = 载荷.get(键)
            if 值:
                原 = 值
                break
        if isinstance(原, str):
            原 = re.split(r"[,，、;；/\s]+", 原)
        标签: list[str] = []
        for 项 in (原 if isinstance(原, list) else []):
            文本 = str(项 or "").strip().strip("#").strip()
            if 文本 and 文本 not in 标签:
                标签.append(文本)
        return 标签[:self.标签上限]

    def _规则总结(self, 条目们: Sequence[字幕条目]) -> dict:
        """规则降级总结：摘要 = 开头截断；章节 = 按时间均匀切段。"""
        全文 = " ".join(str(e.文本 or "").replace("\n", " ").strip()
                      for e in 条目们).strip()
        摘要 = _截断(全文, self.规则摘要字数)
        return {"成功": True, "摘要": 摘要, "章节": self._规则章节(条目们),
                "标签": [], "来源": "规则", "错误": ""}

    def _规则章节(self, 条目们: Sequence[字幕条目]) -> list[dict]:
        """按时间均匀分章（纯规则，不依赖 AI）。"""
        条数 = len(条目们)
        if 条数 == 0:
            return []
        段数 = max(1, min(self.规则章节上限, max(self.规则章节下限, 条数)))
        段数 = min(段数, 条数)       # 条目比段数还少时，一段一条，别造空章节
        章节: list[dict] = []
        for 段 in range(段数):
            起点 = 条数 * 段 // 段数
            终点 = 条数 * (段 + 1) // 段数
            段条目 = 条目们[起点:终点]
            if not 段条目:
                continue
            正文 = " ".join(str(e.文本 or "").replace("\n", " ").strip()
                          for e in 段条目).strip()
            开始 = _取浮(段条目[0].开始秒, 0.0)
            章节.append({"时间": 时钟文本(开始), "秒": float(开始),
                       "标题": _截断(段条目[0].文本.replace("\n", " "), 20),
                       "要点": _截断(正文, 80)})
        return 章节

    # ---------------------------------------------------- 语音识别 → 字幕

    def 识别结果转条目(self, 段落们: Sequence[dict]) -> list[字幕条目]:
        """把语音识别结果转成字幕条目。

        识别器各家字段不统一（``开始秒`` / ``start`` / ``begin``、秒或毫秒、
        float 或 ``"00:00:12,000"`` 字符串），这里一次性归一化；
        结束时间缺失或倒挂时兜底 ``开始+0.5s``，避免零时长条目在播放器里点不到。
        空文本段直接丢弃（ASR 的静音段很常见），最后按时间升序排好并重排序号。
        """
        结果: list[字幕条目] = []
        for 段落 in (段落们 or []):
            if isinstance(段落, dict):
                开始 = _取时间值(段落, ("开始秒", "开始", "start", "起始秒",
                                    "begin", "from", "start_time", "开始时间"))
                结束 = _取时间值(段落, ("结束秒", "结束", "end", "终止秒",
                                    "stop", "to", "end_time", "结束时间"))
                if 开始 is None:
                    开始 = _取时间值(段落, ("开始毫秒", "start_ms", "开始ms"))
                    开始 = None if 开始 is None else 开始 / 1000.0
                if 结束 is None:
                    结束 = _取时间值(段落, ("结束毫秒", "end_ms", "结束ms"))
                    结束 = None if 结束 is None else 结束 / 1000.0
                文本 = _取文本值(段落, ("文本", "内容", "text", "sentence",
                                    "译文", "transcript"))
            elif isinstance(段落, (list, tuple)) and len(段落) >= 3:
                开始 = _取时间值({"值": 段落[0]}, ("值",))
                结束 = _取时间值({"值": 段落[1]}, ("值",))
                文本 = 段落[2]
            elif isinstance(段落, object) and any(
                    hasattr(段落, 名) for 名 in ("开始秒", "start", "start_time")):
                # 兜底：语音识别器可能返回 **dataclass/普通对象**而不是 dict
                # （V8_3 的 语音识别.识别段落 就是这种）。早先这里直接 continue
                # 会把整条识别结果静默丢光 —— 症状是"识别成功但一条字幕都没有"。
                开始 = _取时间值(_对象转字典(段落),
                             ("开始秒", "开始", "start", "begin", "start_time"))
                结束 = _取时间值(_对象转字典(段落),
                             ("结束秒", "结束", "end", "stop", "end_time"))
                文本 = _取文本值(_对象转字典(段落),
                             ("文本", "内容", "text", "sentence"))
            else:
                continue
            文本 = " ".join(str(文本 or "").split())
            if not 文本:
                continue
            开始 = max(0.0, _取浮(开始, 0.0))
            结束 = _取浮(结束, 开始)
            if 结束 <= 开始:
                结束 = 开始 + 0.5
            结果.append(字幕条目(序号=0, 开始秒=开始, 结束秒=结束, 文本=文本))
        结果.sort(key=lambda e: e.开始秒)     # ASR 偶尔乱序；SRT 必须单调递增
        for 序号, 条目 in enumerate(结果, start=1):
            条目.序号 = 序号
        return 结果

    def 生成字幕(self, 媒体来源: str, 语音识别器, 语言: str = "",
                 进度回调=None) -> dict:
        """调语音识别器生成字幕，返回 ``{成功, 条目们, 统计, 错误}``。

        识别器是**鸭子类型**：``可用() -> (bool, 说明)``（可选）、
        ``识别(媒体来源, 语言=..., 进度回调=...) -> 段落 list``。
        识别器抛异常/返回空/不可用都只是 ``成功=False``，绝不冒泡到播放页。
        """
        统计 = {"段落数": 0, "条目数": 0, "用时秒": 0.0,
                "语言": str(语言 or "").strip() or "自动", "时长秒": 0.0}
        结果 = {"成功": False, "条目们": [], "统计": 统计, "错误": ""}
        开始时刻 = time.time()
        if 语音识别器 is None:
            结果["错误"] = "未提供语音识别器"
            return 结果
        可用, 说明 = self._识别器可用(语音识别器)
        if not 可用:
            结果["错误"] = str(说明 or "语音识别器不可用")
            return 结果
        if not str(媒体来源 or "").strip():
            结果["错误"] = "媒体来源为空，无法识别"
            return 结果

        段落们 = self._调识别(语音识别器, 媒体来源, 语言, 进度回调)
        if 段落们 is None:
            统计["用时秒"] = round(time.time() - 开始时刻, 3)
            结果["错误"] = "语音识别失败（异常、超时或返回不可用）"
            return 结果
        if isinstance(段落们, dict):
            段落们 = (段落们.get("段落") or 段落们.get("segments")
                    or 段落们.get("结果") or 段落们.get("data") or [])
        if not isinstance(段落们, (list, tuple)):
            段落们 = []
        条目们 = self.识别结果转条目(段落们)
        统计["段落数"] = len(段落们)
        统计["条目数"] = len(条目们)
        统计["用时秒"] = round(time.time() - 开始时刻, 3)
        统计["时长秒"] = round(max([e.结束秒 for e in 条目们] or [0.0]), 3)
        if not 条目们:
            结果["错误"] = "识别结果里没有可用文本"
            return 结果
        结果["成功"] = True
        结果["条目们"] = 条目们
        self._报进度(进度回调, "识别", len(条目们), len(条目们))
        self._日志(f"字幕生成完成：{len(条目们)} 条，"
                 f"时长 {统计['时长秒']}s，用时 {统计['用时秒']}s")
        return 结果

    @staticmethod
    def _识别器可用(识别器) -> tuple[bool, str]:
        可用方法 = getattr(识别器, "可用", None)
        if not callable(可用方法):
            return True, ""             # 没有自检接口就交给 识别() 自己报错
        try:
            值 = 可用方法()
        except Exception as e:  # noqa: BLE001
            return False, f"识别器可用性检查异常：{type(e).__name__}: {e}"
        if isinstance(值, tuple):
            return bool(值[0] if 值 else False), \
                (str(值[1]) if len(值) > 1 and 值[1] else "")
        return bool(值), ""

    @staticmethod
    def _调识别(识别器, 媒体来源: str, 语言: str, 进度回调):
        """调 ``识别()``，按"参数从多到少"退让，兼容只实现最小签名的识别器。"""
        识别 = getattr(识别器, "识别", None)
        if not callable(识别):
            return None
        for 参数 in ({"语言": 语言, "进度回调": 进度回调},
                    {"语言": 语言}, {}):
            try:
                return 识别(媒体来源, **参数)
            except TypeError:
                continue                # 签名不匹配 → 少传点再试
            except Exception as e:  # noqa: BLE001
                logger.warning("[字幕] 语音识别失败：%s", e)
                return None
        return None

    # ---------------------------------------------------- 工具

    def 纯文本(self, 条目们: Sequence[字幕条目]) -> str:
        """压成"带时间戳的纯文本"，每行 ``[HH:MM:SS] 正文``。

        给 AI 当输入用（也让日志可读）。多行文本折成一行：时间戳本身占了行首，
        再换行会让 AI 把一条字幕当成两条，翻译/总结都会串位。
        """
        行们: list[str] = []
        for 条目 in (条目们 or []):
            if 条目 is None:
                continue
            文本 = " ".join(str(getattr(条目, "文本", "") or "").split())
            if not 文本:
                continue
            行们.append(f"[{时钟文本(getattr(条目, '开始秒', 0.0))}] {文本}")
        return "\n".join(行们)

    def 合并短句(self, 条目们: Sequence[字幕条目], 最小时长秒: float = 1.0,
                 最大字数: int = 42) -> list[字幕条目]:
        """把过短/半句的相邻字幕粘成一条。

        为什么要合并：一条只显示 0.4 秒的字幕人眼根本看不清；ASR 的断句也常把
        一句话切成三五个字一条。合并时**保留首条起点、末条终点**（时间范围只会
        变大不会缩小），并且只粘"间隔很小 + 上一条没说完/太短 + 合并后不超字数"
        的邻居——跨镜头的两条粘一起会变成"牛头不对马嘴"的字幕。
        """
        有序 = [e for e in (条目们 or []) if e is not None]
        最小时长 = max(0.0, _取浮(最小时长秒, 1.0))
        上限 = max(1, _取整(最大字数, 42) or 1)
        结果: list[字幕条目] = []
        for 条目 in 有序:
            本条 = 条目.复制()
            if 结果:
                上一条 = 结果[-1]
                间隔 = _取浮(本条.开始秒, 0.0) - _取浮(上一条.结束秒, 0.0)
                合并文本 = _拼接文本(上一条.文本, 本条.文本)
                if (间隔 <= self.合并最大间隔秒
                        and (上一条.时长秒() < 最小时长
                             or not _以句末标点结尾(上一条.文本))
                        and len(合并文本) <= 上限):
                    上一条.文本 = 合并文本
                    上一条.结束秒 = max(_取浮(上一条.结束秒, 0.0),
                                    _取浮(本条.结束秒, 0.0))
                    continue
            结果.append(本条)
        for 序号, 条目 in enumerate(结果, start=1):
            条目.序号 = 序号
        return 结果

    def 无字幕判定(self, 条目们: Sequence[字幕条目]) -> bool:
        """判断这堆条目是不是"等于没有字幕"。

        为什么要这个：视频可能压根没有字幕轨、语音识别可能什么都听不出来、
        SRT 里可能只有 ``[音乐]`` / ``♪♪`` 这类音效标记。界面这时候应该显示
        "未检测到字幕"，而不是让用户对着一堆空条目点"翻译"然后什么也没发生。
        判定口径：去掉标点/空白/括号噪声/音符后，全部条目加起来不足 2 个字符。
        """
        有效字数 = 0
        for 条目 in (条目们 or []):
            if 条目 is None:
                continue
            有效字数 += len(_去噪声(getattr(条目, "文本", "")))
            if 有效字数 >= 2:
                return False
        return True


# ============================================================ 模块级小工具

def _默认输出路径(源路径: str, 目标语言: str) -> str:
    """``movie.srt`` + ``中文`` → ``movie.中文.srt``（与源文件同目录，不覆盖原文）。"""
    根, 扩展 = os.path.splitext(str(源路径 or "").strip())
    语言 = re.sub(r"[\\/:*?\"<>|\s]+", "", str(目标语言 or "").strip()) or "译文"
    return f"{根}.{语言}{扩展 or '.srt'}"


def _找列表(载荷: Any) -> Optional[list]:
    """从各种可能形状里找出"译文列表"。

    模型（尤其小模型）返回的键名/结构完全不可控：``{"译文": [...]}``、
    直接一个数组、``{"1": "a", "2": "b"}``、甚至嵌一层 ``{"data": {...}}``。
    这里按常见键名递归找，找不到就返回 None（由调用方按"失败"处理）。
    """
    if isinstance(载荷, (list, tuple)):
        return list(载荷)
    if isinstance(载荷, dict):
        for 键 in ("译文", "翻译", "结果", "字幕", "文本", "内容",
                  "lines", "data", "items", "translations"):
            if 键 in 载荷:
                找到 = _找列表(载荷[键])
                if 找到 is not None:
                    return 找到
        if 载荷 and all(str(k).strip().isdigit() for k in 载荷):
            return [载荷[k] for k in sorted(载荷, key=lambda x: int(str(x)))]
        return None
    if isinstance(载荷, str):
        return _找列表(_解析载荷(载荷))
    return None


def _取条目文本(项: Any) -> str:
    """把列表里的一项取成字符串（可能是 str / {"译文": ...} / 数字）。"""
    if isinstance(项, str):
        return 项
    if isinstance(项, dict):
        for 键 in ("译文", "翻译", "文本", "内容", "text", "translation",
                  "value", "target"):
            if 项.get(键) is not None:
                return str(项[键])
        return ""
    if isinstance(项, (int, float)) and not isinstance(项, bool):
        return str(项)
    return ""


def _去噪声(文本: Any) -> str:
    """去掉括号噪声、标点、音符等"看着有字其实没信息"的字符。"""
    文本 = _括号噪声.sub("", str(文本 or ""))
    return "".join(ch for ch in 文本 if ch not in _噪声字符).strip()


def _拼接文本(左: str, 右: str) -> str:
    """拼接两条字幕文本：中文直接接，英文之间补空格。"""
    左 = str(左 or "").strip()
    右 = str(右 or "").strip()
    if not 左:
        return 右
    if not 右:
        return 左
    需要空格 = 左[-1].isascii() and not 左[-1].isspace() and 右[0].isascii()
    return f"{左} {右}" if 需要空格 else f"{左}{右}"


def _以句末标点结尾(文本: str) -> bool:
    文本 = str(文本 or "").strip()
    return bool(文本) and 文本[-1] in _句末标点


def _取时间值(段落: dict, 键名们: Sequence[str]) -> Optional[float]:
    """按候选键名取时间值：数字直接用，字符串走"纯数字 → 时间戳"两级解析。"""
    for 键 in 键名们:
        if 键 not in 段落:
            continue
        值 = _取数值(段落.get(键))
        if 值 is not None:
            return 值
    return None


def _取数值(值: Any) -> Optional[float]:
    """把"12.3" / 12.3 / "00:00:12,300" 都解析成秒；解析不出来返回 None。

    AI / ASR 的 JSON 里数字经常是字符串（``{"秒": "754.0"}``），
    只认 float 会把好好的章节/段落整条丢掉。
    """
    if isinstance(值, bool) or 值 is None:
        return None
    if isinstance(值, (int, float)):
        return float(值)
    if isinstance(值, str):
        文本 = 值.strip()
        if not 文本:
            return None
        try:
            return float(文本)
        except ValueError:
            pass
        转 = 时间戳转秒(文本)
        return 转 if 转 >= 0 else None
    return None


def _取文本值(段落: dict, 键名们: Sequence[str]) -> str:
    """按候选键名取文本；值是列表（某些 ASR 返回 token 数组）时用空格连起来。"""
    for 键 in 键名们:
        值 = 段落.get(键)
        if 值 is None:
            continue
        if isinstance(值, (list, tuple)):
            return " ".join(str(x) for x in 值 if x is not None)
        return str(值)
    return ""


def _章节秒(项: Any) -> Optional[float]:
    """从一个章节项里取出"第几秒"：优先数值字段，其次时间戳字符串。"""
    if isinstance(项, dict):
        for 键 in ("秒", "开始秒", "start", "start秒", "start_time", "开始"):
            秒 = _取数值(项.get(键))
            if 秒 is not None:
                return 秒
        for 键 in ("时间", "时间戳", "time", "timestamp", "timecode"):
            秒 = _取数值(项.get(键))
            if 秒 is not None:
                return 秒
        return None
    return _取数值(项)
