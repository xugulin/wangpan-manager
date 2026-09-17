"""终端日志：像 网盘管理_V8 那样把运行信息打到终端，方便调试和维护。

V8 用 ``print`` 到处打日志；V8_3 里日志有三条去处：

1. **终端**（本模块）：默认开启，格式 ``[HH:MM:SS] 消息``；
2. **日志页**（界面）：同一份消息进 ``数据/界面日志.txt``；
3. **标准库 logging**（AI 层等）：由 :func:`设置终端日志` 统一接到终端。

用法::

    from v8_3.日志 import 设置终端日志, 终端输出, 设置级别, 开启, 关闭
    设置终端日志(级别="信息")      # 启动时调用一次
    终端输出("开始传输 xxx")       # 任何线程都可调用

级别用中文：``调试 < 信息 < 警告 < 错误``；默认“信息”，``--调试`` 可放开。
"""

from __future__ import annotations

import logging
import re
import sys
import threading
import time

级别顺序 = {"调试": 10, "信息": 20, "警告": 30, "错误": 40}
级别图标 = {"调试": "🔍", "信息": "", "警告": "⚠️", "错误": "❌"}

#: 关键字 → 图标（顺序即优先级：先判成败，再判类别；消息自带 emoji 时不赘加）
关键字图标 = (
    # —— 成败/警告类由正则先行判断（见 图标()），这里只放类别词 ——
    ("超时", "⏰"), ("时间", "🕐"),
    ("恢复", "♻️"), ("重试", "🔁"),
    # —— 类别 ——
    ("启动自检", "🚀"), ("启动", "🚀"), ("主题", "🎨"),
    ("价格", "💰"), ("余额", "💳"), ("预算", "💳"), ("时段", "🕐"),
    ("敏感词", "🔒"), ("预检", "🎯"), ("绕过", "🛡️"), ("改名", "✏️"),
    ("瘦身", "🪶"), ("全中文化", "💥"),
    ("学习库", "📚"), ("回填", "📈"), ("诊断", "🩺"), ("调优", "⚙️"),
    ("策略", "🧭"), ("优先级", "🎯"), ("AI", "🤖"),
    ("连通性", "🔌"), ("适配器", "🧩"), ("网盘", "🧩"), ("桥", "🌉"),
    ("扫描", "🔍"), ("并发", "🧮"), ("批次", "📦"), ("队列", "📋"),
    ("上传", "📤"), ("下载", "📥"), ("传输", "🔀"),
    ("跳过", "⏭️"), ("覆盖", "🔁"), ("删除", "🗑️"),
    ("缓存", "🧹"), ("中转", "🧳"), ("清理", "🧹"),
)

#: 常见 emoji 的 Unicode 区间（判"这行已经自带图标了"）
_emoji正则 = re.compile(
    "[\u2190-\u21ff\u2300-\u23ff\u2600-\u27bf\u2b00-\u2bff\ufe0f"
    "\U0001f000-\U0001faff]")


def 含图标(文本: str) -> bool:
    return bool(_emoji正则.search(str(文本 or "")))


#: 真正的异常词（"失败率" 这种统计口径不算）
_异常正则 = re.compile(r"(失败(?!率)|错误|异常|Traceback)")
#: "失败 0 / 错误：0" 这类统计口径，不算异常
_零失败正则 = re.compile(r"(失败|错误)[^\d]{0,3}0(?![.\d%])")
_成功正则 = re.compile(r"(完成|成功|就绪|通过)")


def 图标(消息: str, 级别: str | int | None = "信息") -> str:
    """给一行日志挑一个 emoji：级别优先，其次成败，再看类别词。

    消息里已经有 emoji（✅/🌐/🅱…）就不再赘加；
    "失败率 0%""失败 0" 这类统计口径不会被当成错误。
    """
    原文 = str(消息 or "")
    文本 = 原文.strip()
    if 原文[:1].isspace():
        return ""              # 缩进的是明细行（"      适配器 : …"），不配图标
    if 含图标(文本):
        return ""
    规范 = 规范级别(级别)
    if 规范 == "错误":
        return "❌"
    if 规范 == "警告":
        return "⚠️"
    if _异常正则.search(文本) and not _零失败正则.search(文本):
        return "❌"
    if "警告" in 文本 or "warn" in 文本.lower():
        return "⚠️"
    if _成功正则.search(文本):
        return "✅"
    for 词, 图 in 关键字图标:
        if 词 in 文本:
            return 图
    return 级别图标[规范]
英文别名 = {
    "debug": "调试", "info": "信息", "warning": "警告", "warn": "警告",
    "error": "错误", "critical": "错误", "fatal": "错误",
}

_锁 = threading.Lock()
_已配置 = False
_终端开启 = True
_终端级别 = "信息"
_强制关闭 = False        # --静默：优先级高于配置里的 界面.终端日志
# 连续重复行折叠：适配器偶发会连打多行相同报错（例如百度 bdstoken 刷新失败），
# 终端只留一行 + "（同上 ×N）"，日志页仍是全量。
_上次行 = {"文本": None, "次数": 0}


def 规范级别(级别: str | int | None) -> str:
    if 级别 is None:
        return "信息"
    if isinstance(级别, int):
        for 名称, 值 in 级别顺序.items():
            if 值 >= 级别:
                return 名称
        return "错误"
    文本 = str(级别).strip().lower()
    if 文本 in 英文别名:
        return 英文别名[文本]
    for 名称 in 级别顺序:
        if 文本 == 名称 or 文本 == 名称[0]:
            return 名称
    return "信息"


def 设置级别(级别: str | int | None) -> str:
    global _终端级别
    _终端级别 = 规范级别(级别)
    return _终端级别


def 获取级别() -> str:
    return _终端级别


def 开启() -> None:
    global _终端开启
    if _强制关闭:
        return
    _终端开启 = True


def 强制关闭(值: bool = True) -> None:
    """启动参数 --静默 用：此后 开启() 也不生效。"""
    global _强制关闭, _终端开启
    _强制关闭 = bool(值)
    if _强制关闭:
        _终端开启 = False


def 关闭() -> None:
    global _终端开启
    _终端开启 = False


def 是否开启() -> bool:
    return _终端开启


def 应该输出(级别: str | int | None = "信息") -> bool:
    if not _终端开启:
        return False
    return 级别顺序[规范级别(级别)] >= 级别顺序[_终端级别]


def 终端输出(消息: str, 级别: str | int | None = "信息",
             来源: str = "") -> None:
    """往终端打一行（带时间戳）；级别低于当前设置就丢弃。

    写的是 **stderr**：终端里照样看得见，同时不会污染 ``--cli`` 的
    ``stdout``（方便 ``启动.py --cli 列目录 > 文件`` 这类用法）。
    """
    if not 应该输出(级别):
        return
    文本 = str(消息 or "")
    前缀 = f"[{来源}] " if 来源 else ""
    图 = 图标(文本, 级别)
    图 = f"{图} " if 图 else ""
    行 = f"[{time.strftime('%H:%M:%S')}] {前缀}{图}{文本}"
    指纹 = f"{前缀}{图}{文本}"       # 时间戳不参与比较
    with _锁:
        try:
            if 指纹 == _上次行["文本"]:
                _上次行["次数"] += 1
                return
            折叠 = ""
            if _上次行["次数"] > 1:
                折叠 = (f"[{time.strftime('%H:%M:%S')}] "
                      f"… 上一行重复了 {_上次行['次数']} 次（日志页有全量）")
            _上次行["文本"] = 指纹
            _上次行["次数"] = 1
            if 折叠:
                print(折叠, file=sys.stderr, flush=True)
            print(行, file=sys.stderr, flush=True)
        except Exception:
            pass


class _终端处理器(logging.Handler):
    """把标准库 logging 接到终端输出（AI 层等用的是 logging）。"""

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D102
        try:
            级别 = 规范级别(record.levelname)
            来源 = record.name or ""
            # 只保留最后一级名字，避免 "v8_3.AI.AI调度器" 这种长前缀
            if "." in 来源:
                来源 = 来源.rsplit(".", 1)[-1]
            消息原文 = record.getMessage()
            # AI 层自己已经带 [DeepSeek价格] / [时段管理器] 这类前缀，避免叠两层
            if 消息原文.lstrip().startswith("["):
                来源 = ""
            消息 = record.getMessage()
            if record.exc_info:
                消息 = f"{消息}（{record.exc_info[0].__name__}）"
            终端输出(消息, 级别=级别, 来源=来源)
        except Exception:
            pass


def 设置终端日志(级别: str | int | None = "信息", 强制: bool = False,
                 接管logging: bool = True) -> None:
    """启动时调用一次：设定级别，并把标准库 logging 接到终端。

    ``强制=False`` 时重复调用只会更新级别（不会重复添加 handler）。
    """
    global _已配置
    设置级别(级别)
    with _锁:
        if _已配置 and not 强制:
            return
        if not 接管logging:
            _已配置 = True
            return
        根 = logging.getLogger()
        已有 = [h for h in 根.handlers if isinstance(h, _终端处理器)]
        if not 已有:
            处理器 = _终端处理器()
            处理器.setLevel(logging.DEBUG)
            根.addHandler(处理器)
        根.setLevel(logging.DEBUG)      # 具体显示由 _终端处理器 自己过滤
        # 适配器的 httpx 等第三方库太啰嗦，默认压到警告
        for 名字 in ("httpx", "httpcore", "urllib3", "asyncio", "qrcode"):
            logging.getLogger(名字).setLevel(logging.WARNING)
        _已配置 = True


def 打印启动横幅(配置: dict | None = None, 附加: str = "") -> None:
    """打印一条 V8 风格的启动信息（网盘/主题/AI/敏感词）。"""
    行 = ["=" * 62, "网盘管理 V8_3 · 去 Alist 直连直传"]
    try:
        from .配置 import (AI配置, 敏感词配置, 界面配置, 网盘实例列表,
                        项目根)
        行.append(f"项目根 : {项目根}")
        实例们 = 网盘实例列表(配置)
        启用 = [x for x in 实例们 if x["启用"]]
        行.append(f"网盘   : {len(启用)} 个启用 / {len(实例们)} 个已配置 "
                 f"（{'、'.join(x['标识'] for x in 启用) or '无'}）")
        界 = 界面配置(配置)
        行.append(f"界面   : 主题 {界.get('主题') or '默认'}，"
                 f"上次网盘 {界.get('上次网盘') or '-'}")
        ai = AI配置(配置)
        行.append(f"AI     : {'开启' if ai['启用'] else '关闭'}，"
                 f"模型 {ai['模型']}，密钥 {'已配置' if ai['api密钥'] else '未配置'}，"
                 f"高峰(不调AI) {ai['高峰时段']['开始']}–{ai['高峰时段']['结束']}")
        词 = 敏感词配置(配置)
        行.append(f"敏感词 : {'开启' if 词['启用'] else '关闭'}，"
                 f"自动改名 {'开' if 词['自动改名上传'] else '关'}，"
                 f"词库 {词['数据库路径']}")
    except Exception as e:  # noqa: BLE001 - 横幅不能影响启动
        行.append(f"（读取配置摘要失败：{e}）")
    if 附加:
        行.append(附加)
    行.append("=" * 62)
    for 文本 in 行:
        终端输出(文本)
