"""V8_3 命令行入口。

网盘参数一律使用**实例标识**（``baidu`` / ``baidu_2``…），也接受类型名
（``百度``）或实例名称（``百度小号``）——只要不含歧义。
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from .配置 import (
    动作, 加载配置, 保存配置, 缓存参数, 传输参数, 数据库路径,
    网盘实例列表, 解析标识, 新增网盘实例, 更新网盘实例, 删除网盘实例,
    复制适配器项目, 建议副本目录, 默认适配器路径, 适配器规格表,
)
from .核心.传输引擎 import 传输请求
from .核心.适配器 import 适配器规格, 类型图标
from .核心.模型 import 网盘类型, 规范路径


def _拆分目标(文本: str) -> tuple[str, str]:
    """解析 ``baidu:/目录`` / ``百度小号:/目录`` 形式，返回 (网盘文本, 路径)。"""
    文本 = str(文本 or "").strip()
    if ":" not in 文本:
        raise argparse.ArgumentTypeError(
            f"路径格式应为 网盘:路径，例如 baidu:/电影；收到：{文本!r}")
    网盘文本, 路径 = 文本.split(":", 1)
    return 网盘文本.strip(), 规范路径(路径)


class _解析目标Action(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        setattr(namespace, self.dest, _拆分目标(values))


def 终端日志回调():
    """给 动作/引擎 用的日志出口：按级别打到终端（V8 风格 [日志] 前缀）。"""
    from .日志 import 终端输出, 应该输出

    def 回调(消息, 级别="信息"):
        if 应该输出(级别):
            终端输出(消息, 级别=级别)
    return 回调


def 构建解析器() -> argparse.ArgumentParser:
    解析器 = argparse.ArgumentParser(
        prog="网盘管理_V8_3",
        description="去 Alist 的跨网盘直连/直传管理器（支持同一家网盘多账号）",
    )
    解析器.add_argument("--配置", help="配置文件路径（默认 配置.json）")
    sub = 解析器.add_subparsers(dest="命令")

    p状态 = sub.add_parser("状态", help="查看各网盘登录状态")
    p状态.add_argument("--网盘", help="只查一个网盘")

    p列表 = sub.add_parser("列目录", help="列出网盘目录")
    p列表.add_argument("--网盘", required=True)
    p列表.add_argument("--路径", default="/")
    p列表.add_argument("--详细", action="store_true")

    p上传 = sub.add_parser("上传", help="把本地文件/目录上传到某个网盘")
    p上传.add_argument("--网盘", required=True)
    p上传.add_argument("--本地", required=True)
    p上传.add_argument("--远端", default="/")
    p上传.add_argument("--覆盖", action="store_true")
    p上传.add_argument("--并发", type=int, default=8)

    p下载 = sub.add_parser("下载", help="从某个网盘下载文件到本地")
    p下载.add_argument("--网盘", required=True)
    p下载.add_argument("--远端", required=True)
    p下载.add_argument("--本地", required=True)

    p删除 = sub.add_parser("删除", help="删除网盘上的文件或目录")
    p删除.add_argument("--网盘", required=True)
    p删除.add_argument("--路径", required=True)

    p传输 = sub.add_parser("传输", help="跨网盘传输文件或目录")
    p传输.add_argument("--源", required=True, action=_解析目标Action,
                      metavar="网盘:路径")
    p传输.add_argument("--目标", required=True, action=_解析目标Action,
                      metavar="网盘:路径")
    p传输.add_argument("--覆盖", action="store_true", help="覆盖目标已存在文件")
    p传输.add_argument("--并发", type=int, default=0, help="显式指定总并发")
    p传输.add_argument("--重试", type=int, default=None)
    p传输.add_argument("--小文件并发", type=int, default=None)
    p传输.add_argument("--大文件并发", type=int, default=None)
    p传输.add_argument("--缓存目录", default=None)
    p传输.add_argument("--保留失败中转", action="store_true")
    p传输.add_argument("--关闭自适应", action="store_true")

    p缓存 = sub.add_parser("清理缓存", help="清空本地中转缓存")
    p缓存.add_argument("--目录", default=None)

    p批次 = sub.add_parser("批次列表", help="列出未完成/失败的传输批次")
    p批次.add_argument("--限制", type=int, default=50)

    p恢复 = sub.add_parser("恢复", help="恢复一个未完成的传输批次")
    p恢复.add_argument("--批次", required=True)

    # ---- 本地 DeepSeek 模型（V8_3 新增：免费、离线）----
    p本地 = sub.add_parser("本地模型", help="本地 DeepSeek 模型：检测/启动/拉取/测速/对话")
    p本地.add_argument("--检测", action="store_true", help="探测本机是否有可用服务（默认）")
    p本地.add_argument("--启动", action="store_true", help="启动 ollama serve（用户目录安装）")
    p本地.add_argument("--拉取", metavar="模型", nargs="?", const="",
                    help="拉取模型（默认配置里的模型，如 deepseek-r1:1.5b）")
    p本地.add_argument("--测速", action="store_true", help="跑一次短提示测延迟与 tokens/s")
    p本地.add_argument("--对话", metavar="文本", help="直接和本地模型说一句话")
    p本地.add_argument("--开启", action="store_true", help="在配置里启用本地模型")
    p本地.add_argument("--关闭", action="store_true", help="在配置里关闭本地模型")
    p本地.add_argument("--模型", help="指定模型名（配合 --拉取/--对话）")
    p本地.add_argument("--安装指引", action="store_true", help="打印安装命令")

    # ---- 网盘实例管理（与界面上的 新增/编辑/删除 等价）----
    sub.add_parser("网盘列表", help="列出已配置的网盘实例")

    p详情 = sub.add_parser("网盘详情",
                         help="列出每个网盘的核心信息（目录/凭证/账号/容量）")
    p详情.add_argument("--网盘", help="只看一个网盘")
    p详情.add_argument("--离线", action="store_true",
                     help="不联网：只打目录/凭证/线程等静态信息")

    p新增 = sub.add_parser("网盘新增", help="新增一个网盘实例（可同类型多账号）")
    p新增.add_argument("--类型", required=True,
                       help="baidu / guangya / quark / fake（也接受中文名）")
    p新增.add_argument("--名称", default="", help="显示名，默认用类型中文名")
    p新增.add_argument("--标识", default="", help="实例标识，默认自动生成")
    p新增.add_argument("--路径", default="", help="适配器项目目录（--副本 时是复制目标）")
    p新增.add_argument("--线程数", type=int, default=12)
    p新增.add_argument("--副本", action="store_true",
                       help="复制一份适配器项目（凭证独立），用于同家网盘第二账号")
    p新增.add_argument("--源路径", default="",
                       help="--副本 时的源适配器目录，默认按类型自动查找")
    p新增.add_argument("--停用", action="store_true", help="先加入配置但不启用")

    p编辑 = sub.add_parser("网盘编辑", help="修改一个网盘实例")
    p编辑.add_argument("--网盘", required=True, help="标识/名称/类型")
    p编辑.add_argument("--标识", default=None)
    p编辑.add_argument("--名称", default=None)
    p编辑.add_argument("--路径", default=None)
    p编辑.add_argument("--线程数", type=int, default=None)
    p编辑.add_argument("--启用", dest="启用", action="store_true", default=None)
    p编辑.add_argument("--停用", dest="启用", action="store_false")

    p删网盘 = sub.add_parser("网盘删除", help="从配置里删除一个网盘实例")
    p删网盘.add_argument("--网盘", required=True)

    p复制 = sub.add_parser("网盘复制", help="复制适配器项目（做同家网盘第二账号）")
    p复制.add_argument("--网盘", required=True)
    p复制.add_argument("--目标", default="", help="目标目录，默认 适配器实例/<类型>_<标识>")

    return 解析器


# ==================== 网盘实例管理 ====================


def 命令本地模型(参数, 配置: dict, 配置路径: Optional[Path] = None) -> int:
    """本地 DeepSeek 模型：检测 / 启动 / 拉取 / 测速 / 对话 / 开关。"""
    from .AI.本地模型 import 本地模型客户端, 取本地模型配置
    from .配置 import AI配置, 加载配置, 保存配置, 配置文件 as 默认配置路径

    ai = AI配置(配置)
    本地配置 = 取本地模型配置(ai)
    if getattr(参数, "模型", None):
        本地配置.模型 = str(参数.模型)
    if getattr(参数, "开启", False) or getattr(参数, "关闭", False):
        路径 = 配置路径 or 默认配置路径
        整份 = 加载配置(路径)
        ai段 = dict(整份.get("AI") or {})
        段 = dict(ai段.get("本地模型") or {})
        段["启用"] = bool(getattr(参数, "开启", False))
        if getattr(参数, "模型", None):
            段["模型"] = str(参数.模型)
        ai段["本地模型"] = 段
        整份["AI"] = ai段
        保存配置(整份, 路径)
        print(f"{'✅ 已启用' if 段['启用'] else '⚪ 已关闭'}本地模型"
              f"（{路径} 的 AI.本地模型）")
        if not (getattr(参数, "检测", False) or getattr(参数, "启动", False)
                or getattr(参数, "拉取", None) is not None
                or getattr(参数, "测速", False)
                or getattr(参数, "对话", None)):
            return 0

    客户端 = 本地模型客户端(本地配置, 日志回调=print)

    if getattr(参数, "安装指引", False):
        print(客户端.安装指引())
        return 0
    if getattr(参数, "启动", False):
        成功, 消息 = 客户端.启动服务()
        print(("✅ " if 成功 else "❌ ") + 消息)
        if not 成功:
            return 1
    if getattr(参数, "拉取", None) is not None:
        模型 = str(getattr(参数, "拉取") or "") or 本地配置.模型
        print(f"⏳ 正在拉取 {模型}（首次可能要下载 1GB 左右）…")
        成功, 消息 = 客户端.拉取模型(模型)
        print(("✅ " if 成功 else "❌ ") + 消息)
        if not 成功:
            return 1
    if getattr(参数, "对话", None):
        结果 = 客户端.对话(str(参数.对话), "回答尽量简短。")
        print(结果.内容 or 结果.推理内容 or f"（失败：{结果.错误}）")
        print(f"—— {结果.模型} · {结果.用时秒:.1f}s · "
              f"{结果.输出tokens} tokens · 免费")
        return 0 if 结果.成功 else 1
    if getattr(参数, "测速", False):
        结果 = 客户端.测速()
        if not 结果.get("成功"):
            print(f"❌ 测速失败：{结果.get('错误')}")
            print(客户端.安装指引())
            return 1
        print(f"✅ {结果.get('模型')} · 用时 {结果.get('用时秒')}s · "
              f"{结果.get('每秒tokens')} tokens/s · 服务 {结果.get('地址')}")
        return 0

    # 默认：检测
    状态 = 客户端.检测()
    print(状态.一行())
    if 状态.可用:
        print(f"   模型列表：{状态.模型列表 or '（还没拉取模型）'}")
        print(f"   可执行文件：{状态.可执行文件}")
        if not 状态.模型列表:
            print(f"   下一步：python 启动.py --cli 本地模型 --拉取 "
                  f"{本地配置.模型}")
        else:
            print(f"   当前选用：{状态.模型}"
                  f"　（测速：--cli 本地模型 --测速）")
    else:
        print(客户端.安装指引())
        return 1
    return 0


def 命令网盘列表(参数, 配置: dict, 配置路径: Optional[Path]) -> int:
    实例们 = 网盘实例列表(配置)
    if not 实例们:
        print("尚未配置任何网盘，用 `网盘新增 --类型 baidu` 添加")
        return 0
    规格表 = 适配器规格表(配置)
    print(f"{'标识':<12}{'类型':<10}{'名称':<14}{'线程':<6}{'状态':<6}路径")
    for 项 in 实例们:
        规格 = 规格表.get(项["标识"])
        状态 = "启用" if 项["启用"] else "停用"
        if 规格 is not None:
            可用, _ = 适配器规格.校验目录(规格.路径)
            状态 += "/目录OK" if 可用 else "/目录缺失"
        print(f"{项['标识']:<12}{项['类型']:<10}{项['名称']:<14}"
              f"{项['线程数']:<6}{状态:<14}{项['路径']}")
    return 0


def 命令网盘详情(参数, 配置: dict, 配置路径=None) -> int:
    """打印每个网盘的核心信息（CLI 版的"启动自检核心信息"）。"""
    from .网盘信息 import 网盘信息
    from .日志 import 终端输出
    信息表 = 网盘信息(配置)
    记录 = 信息表.收集()
    if 参数.网盘:
        标识 = 解析标识(配置, 参数.网盘)
        记录 = [x for x in 记录 if x["标识"] == 标识] or 记录
    打 = lambda 文本, 级别="信息": print(文本, flush=True)
    if 参数.离线:
        信息表.打印(记录, 打, 含账号=False,
                  标题="🧩 网盘核心信息（离线；不含账号状态）")
        return 0
    print("🧩 网盘核心信息（含账号状态；首次读账号可能要几秒）", flush=True)
    with 动作(配置, 日志回调=终端日志回调()) as 动作对象:
        for i, 条目 in enumerate(记录):
            if 条目.get("目录可用") and 条目.get("启用"):
                try:
                    条目 = 信息表.拉账号(条目, 动作对象.适配器(条目["标识"]))
                except Exception as e:  # noqa: BLE001
                    条目["错误"] = str(e)[:160]
            记录[i] = 条目                      # 回写，后面统计才拿得到结果
            信息表.打印一个(条目, 打, 含账号=True)
        启用 = [x for x in 记录 if x.get("启用")]
        可用 = [x for x in 启用 if x.get("登录")]
        print(f"✅ 可用网盘：{len(可用)}/{len(启用)}"
              + ("（" + "、".join(f"{x['图标']} {x['名称']}" for x in 可用) + "）"
                 if 可用 else ""), flush=True)
    return 0


def 命令网盘新增(参数, 配置: dict, 配置路径: Optional[Path]) -> int:
    try:
        类型 = 网盘类型.解析(参数.类型)
    except ValueError as e:
        print(f"类型无法识别：{e}")
        return 2
    路径 = str(参数.路径 or "").strip()
    if 参数.副本:
        源 = str(参数.源路径 or "").strip() or str(默认适配器路径(类型))
        目标 = 路径 or str(建议副本目录(配置, 类型))
        try:
            复制适配器项目(源, 目标)
            print(f"已复制适配器项目：{源} → {目标}（登录数据目录已清空）")
        except Exception as e:
            print(f"复制失败：{e}")
            return 2
        路径 = 目标
    if not 路径:
        路径 = str(默认适配器路径(类型))
    try:
        实例 = 新增网盘实例(
            配置, 类型, 名称=参数.名称, 路径=路径,
            线程数=参数.线程数, 启用=not 参数.停用, 标识=参数.标识)
    except Exception as e:
        print(f"新增失败：{e}")
        return 2
    保存配置(配置, 配置路径)
    print(f"已新增网盘：{实例['标识']}（{实例['名称']}）→ {实例['路径']}")
    return 0


def 命令网盘编辑(参数, 配置: dict, 配置路径: Optional[Path]) -> int:
    try:
        实例 = 更新网盘实例(
            配置, 参数.网盘,
            标识=参数.标识, 名称=参数.名称, 路径=参数.路径,
            线程数=参数.线程数, 启用=参数.启用)
    except Exception as e:
        print(f"编辑失败：{e}")
        return 2
    if 实例 is None:
        print(f"未找到网盘：{参数.网盘}")
        return 2
    保存配置(配置, 配置路径)
    print(f"已更新网盘：{json.dumps(实例, ensure_ascii=False)}")
    return 0


def 命令网盘删除(参数, 配置: dict, 配置路径: Optional[Path]) -> int:
    try:
        被删 = 删除网盘实例(配置, 参数.网盘)
    except Exception as e:
        print(f"删除失败：{e}")
        return 2
    if 被删 is None:
        print(f"未找到网盘：{参数.网盘}")
        return 2
    保存配置(配置, 配置路径)
    print(f"已删除网盘：{被删['标识']}（{被删['名称']}）；适配器目录与其登录数据未改动")
    return 0


def 命令网盘复制(参数, 配置: dict, 配置路径: Optional[Path]) -> int:
    标识 = 解析标识(配置, 参数.网盘)
    实例 = next(x for x in 网盘实例列表(配置) if x["标识"] == 标识)
    源 = Path(实例["路径"]).expanduser()
    if not 源.is_absolute():
        源 = 默认适配器路径(实例["类型"]).parent / 实例["路径"]
    目标 = Path(参数.目标).expanduser() if 参数.目标 \
        else 建议副本目录(配置, 实例["类型"])
    try:
        结果 = 复制适配器项目(源, 目标)
    except Exception as e:
        print(f"复制失败：{e}")
        return 2
    print(f"已复制：{源} → {结果}")
    print("提示：用 `网盘新增 --类型 {0} --路径 {1}` 把它加成一个新网盘，"
          "然后在界面里点「登录 / 管理」登录另一个账号。".format(
              实例["类型"], 结果))
    return 0


# ==================== 原有命令 ====================


def 命令状态(参数, 配置: dict, 配置路径=None) -> int:
    """各网盘登录状态（敏感字段脱敏后打印；要更详细用 `网盘详情`）。"""
    from .网盘信息 import 网盘信息
    信息表 = 网盘信息(配置)
    记录 = 信息表.收集()
    if 参数.网盘:
        标识 = 解析标识(配置, 参数.网盘)
        记录 = [x for x in 记录 if x["标识"] == 标识] or 记录
    with 动作(配置, 日志回调=终端日志回调()) as 动作对象:
        for 条目 in 记录:
            if not 条目.get("启用"):
                print(f"{条目['图标']} {条目['名称']:<10} 已停用", flush=True)
                continue
            try:
                条目 = 信息表.拉账号(条目, 动作对象.适配器(条目["标识"]))
            except Exception as e:  # noqa: BLE001
                条目["错误"] = str(e)[:160]
            信息表.打印一个(条目, lambda t, 级别="信息": print(t, flush=True),
                        含账号=True, 只账号=True)
    return 0


def 命令列目录(参数, 配置: dict, 配置路径=None) -> int:
    标识 = 解析标识(配置, 参数.网盘)
    with 动作(配置, 日志回调=终端日志回调()) as 动作对象:
        适配器 = 动作对象.适配器(标识)
        列表 = 适配器.列目录(参数.路径)
        for 项 in 列表:
            if 参数.详细:
                print(f"{'D' if 项.is_dir else 'F'} {项.size:>12} "
                      f"{项.modified:<20} {项.path}")
            else:
                print(项.name)
    return 0


def _进度打印(前缀: str):
    import time as _time
    状态 = {"时间": 0.0}

    def 回调(阶段, 当前, 总量):
        现在 = _time.monotonic()
        if 现在 - 状态["时间"] < 1.0 and (总量 or 0) > 0 and 当前 < 总量:
            return
        状态["时间"] = 现在
        百分比 = f"{(当前 / 总量 * 100):5.1f}%" if (总量 or 0) > 0 else "  ?  "
        print(f"\r{前缀} {阶段} {百分比} {当前}/{总量}", end="", flush=True)

    return 回调


def 命令上传(参数, 配置: dict, 配置路径=None) -> int:
    标识 = 解析标识(配置, 参数.网盘)
    本地 = Path(参数.本地).expanduser()
    if not 本地.exists():
        print(f"本地路径不存在：{本地}")
        return 2
    远端目录 = 规范路径(参数.远端)
    with 动作(配置, 日志回调=终端日志回调()) as 动作对象:
        适配器 = 动作对象.适配器(标识)
        if 本地.is_file():
            结果 = 适配器.上传(str(本地), 远端目录, 任务ID="cli-upload",
                                进度=_进度打印(f"上传 {本地.name}"))
            print()
            print(f"完成：{结果}")
            return 0
        # 目录递归：本地文件夹名作为远端顶层目录；文件级并发上传。
        顶层 = 远端目录.rstrip("/") + "/" + 本地.name
        文件列表 = sorted(p for p in 本地.rglob("*") if p.is_file())
        总数 = len(文件列表)
        print(f"目录上传：{本地} → {顶层}，共 {总数} 个文件，"
              f"并发 {max(1, 参数.并发)}")
        # 目录先串行建好，避免并发建目录的竞态
        目录集 = sorted({
            顶层 + ("" if (文件.parent.relative_to(本地).as_posix() in ("", "."))
                    else "/" + 文件.parent.relative_to(本地).as_posix())
            for 文件 in 文件列表
        }, key=len)
        for 子目录 in 目录集:
            适配器.确保目录(子目录)

        from concurrent.futures import ThreadPoolExecutor, as_completed
        完成 = [0]
        失败: list[str] = []

        def 上传一个(序号文件):
            序号, 文件 = 序号文件
            相对目录 = 文件.parent.relative_to(本地).as_posix()
            远端子目录 = 顶层 + ("" if 相对目录 in ("", ".") else "/" + 相对目录)
            try:
                适配器.上传(str(文件), 远端子目录,
                            任务ID=f"cli-upload-{序号}", 进度=None)
                return 文件, None
            except Exception as e:
                return 文件, str(e)

        with ThreadPoolExecutor(max_workers=max(1, 参数.并发)) as 池:
            未来 = {池.submit(上传一个, (i + 1, 文件)): 文件
                   for i, 文件 in enumerate(文件列表)}
            for 未 in as_completed(未来):
                文件, 错误 = 未.result()
                if 错误:
                    失败.append(f"{文件}: {错误}")
                    print(f"❌ {文件.name}: {错误}", flush=True)
                else:
                    完成[0] += 1
                    print(f"[{完成[0]}/{总数}] ✅ {文件.name}", flush=True)
        print(f"目录上传完成：成功 {完成[0]}/{总数}，失败 {len(失败)}")
        return 0 if not 失败 else 1


def 命令下载(参数, 配置: dict, 配置路径=None) -> int:
    标识 = 解析标识(配置, 参数.网盘)
    远端 = 规范路径(参数.远端)
    本地 = Path(参数.本地).expanduser()
    with 动作(配置, 日志回调=终端日志回调()) as 动作对象:
        适配器 = 动作对象.适配器(标识)
        信息 = 适配器.文件信息(远端)
        if 信息 is None:
            print(f"远端不存在：{远端}")
            return 2
        目标 = 本地
        if 信息.is_dir or 本地.is_dir():
            目标 = 本地 / 信息.name
        目标.parent.mkdir(parents=True, exist_ok=True)
        结果 = 适配器.下载(远端, str(目标), 任务ID="cli-download",
                            进度=_进度打印(f"下载 {信息.name}"))
        print()
        print(f"完成：{结果}")
        return 0


def 命令删除(参数, 配置: dict, 配置路径=None) -> int:
    标识 = 解析标识(配置, 参数.网盘)
    路径 = 规范路径(参数.路径)
    with 动作(配置, 日志回调=终端日志回调()) as 动作对象:
        结果 = 动作对象.适配器(标识).删除(路径)
        print(f"已提交删除：{结果}")
    return 0


def _事件打印器():
    已开始: set[str] = set()

    def 事件回调(事件):
        if 事件.类型 == "任务开始" and 事件.任务:
            任务 = 事件.任务
            if 任务.任务ID not in 已开始:
                已开始.add(任务.任务ID)
                print(f"▶ {任务.源路径} → {任务.目标路径} ({任务.状态.中文名})",
                      flush=True)
        elif 事件.类型 in ("任务完成", "任务跳过", "任务失败", "任务取消"):
            任务 = 事件.任务
            时间 = (任务.结束时间 or time.time()) - (任务.开始时间 or time.time())
            if 事件.类型 == "任务完成":
                print(f"✅ {任务.源路径} ({任务.大小} B, {时间:.1f}s)")
            elif 事件.类型 == "任务跳过":
                print(f"⏭ {任务.源路径}：{任务.跳过原因}")
            else:
                print(f"❌ {任务.源路径}：{任务.错误}", flush=True)
        elif 事件.类型 == "批次完成" and 事件.统计:
            print(f"—— {事件.消息}")

    return 事件回调


def 命令传输(参数, 配置: dict, 配置路径=None) -> int:
    源文本, 源路径 = 参数.源
    目标文本, 目标路径 = 参数.目标
    try:
        源标识 = 解析标识(配置, 源文本)
        目标标识 = 解析标识(配置, 目标文本)
    except ValueError as e:
        print(str(e))
        return 2
    传输 = 传输参数(配置)
    if 参数.重试 is not None:
        传输["重试次数"] = 参数.重试
    if 参数.小文件并发 is not None:
        传输["小文件并发"] = 参数.小文件并发
    if 参数.大文件并发 is not None:
        传输["大文件并发"] = 参数.大文件并发
    缓存 = 缓存参数(配置)
    if 参数.缓存目录:
        缓存.目录 = 参数.缓存目录
    if 参数.保留失败中转:
        缓存.保留失败文件 = True
    请求 = 传输请求(
        源网盘=源标识, 源路径=源路径,
        目标网盘=目标标识, 目标路径=目标路径,
        覆盖=参数.覆盖,
        并发=参数.并发,
        重试次数=传输["重试次数"],
        小文件阈值=传输["小文件阈值"],
        小文件并发=传输["小文件并发"],
        大文件并发=传输["大文件并发"],
        自适应并发=(not 参数.关闭自适应) and 传输.get("自适应并发", True),
        断点续传=bool(传输.get("断点续传", True)),
        失败兜底=bool(传输.get("失败兜底", True)) and not getattr(参数, "不兜底", False),
        兜底串行=int(传输.get("兜底串行") or 1),
        缓存=缓存,
    )

    取消 = threading.Event()
    事件回调 = _事件打印器()

    try:
        with 动作(配置, 日志回调=终端日志回调()) as 动作对象:
            引擎 = 动作对象.构建引擎([源标识, 目标标识])
            统计 = 引擎.传输(请求, 事件回调, 取消)
            print(f"统计：{统计.to_dict()}")
            return 0 if 统计.失败 == 0 else 1
    except KeyboardInterrupt:
        取消.set()
        print("\n已请求取消（等待当前文件结束）")
        return 130


def 命令批次列表(参数, 配置: dict, 配置路径=None) -> int:
    from .核心.任务数据库 import 任务数据库
    db = 任务数据库(数据库路径(配置))
    try:
        批次 = db.未完成批次(int(参数.限制 or 50))
        if not 批次:
            print("没有未完成批次")
            return 0
        for 项 in 批次:
            print(f"{项['batch_id']}  {项['source_cloud']}:{项['source_path']}"
                  f" → {项['target_cloud']}:{项['target_path']}"
                  f"  状态={项['status']}")
    finally:
        db.关闭()
    return 0


def 命令恢复(参数, 配置: dict, 配置路径=None) -> int:
    from .核心.任务数据库 import 任务数据库
    db = 任务数据库(数据库路径(配置))
    try:
        记录 = db.读取批次(参数.批次)
    finally:
        db.关闭()
    if 记录 is None:
        print(f"批次不存在：{参数.批次}")
        return 2
    配置记录 = 记录.get("config") or {}
    源标识 = 解析标识(配置, 配置记录.get("source_cloud") or "")
    目标标识 = 解析标识(配置, 配置记录.get("target_cloud") or "")
    取消 = threading.Event()
    事件回调 = _事件打印器()
    try:
        with 动作(配置, 日志回调=终端日志回调()) as 动作对象:
            引擎 = 动作对象.构建引擎([源标识, 目标标识])
            统计 = 引擎.恢复批次(参数.批次, 事件回调, 取消)
            print(f"统计：{统计.to_dict()}")
            return 0 if 统计.失败 == 0 else 1
    except KeyboardInterrupt:
        取消.set()
        print("\n已请求取消（等待当前文件结束）")
        return 130


def 命令清理缓存(参数, 配置: dict, 配置路径=None) -> int:
    import shutil
    缓存 = 缓存参数(配置)
    目录 = Path(参数.目录 or 缓存.解析目录())
    if 目录.is_dir():
        shutil.rmtree(目录, ignore_errors=True)
        print(f"已清理 {目录}")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    from .日志 import 设置终端日志
    设置终端日志(级别="信息")
    argv = list(sys.argv[1:] if argv is None else argv)
    解析器 = 构建解析器()
    参数 = 解析器.parse_args(argv)
    if not 参数.命令:
        解析器.print_help()
        return 2
    配置路径 = Path(参数.配置) if 参数.配置 else None
    配置 = 加载配置(配置路径)
    处理器 = {
        "状态": 命令状态,
        "列目录": 命令列目录,
        "上传": 命令上传,
        "下载": 命令下载,
        "删除": 命令删除,
        "传输": 命令传输,
        "批次列表": 命令批次列表,
        "恢复": 命令恢复,
        "清理缓存": 命令清理缓存,
        "网盘列表": 命令网盘列表,
        "网盘详情": 命令网盘详情,
        "网盘新增": 命令网盘新增,
        "网盘编辑": 命令网盘编辑,
        "网盘删除": 命令网盘删除,
        "网盘复制": 命令网盘复制,
        "本地模型": 命令本地模型,
    }[参数.命令]
    返回 = 处理器(参数, 配置, 配置路径)
    return int(返回 or 0)


if __name__ == "__main__":
    raise SystemExit(main())
