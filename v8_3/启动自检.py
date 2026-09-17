"""启动自检：像 网盘管理_V8 的 main.py 那样，在终端分步打印启动信息。

V8 的启动输出长这样（用户要求 V8_3 保持一致观感）::

    ============================================================
    🚀 光鸭云盘管理器 V8 - 启动自检
    🎨 主题: 🌙 Nord
    💰 初始化价格抓取器...
    [DeepSeek价格] 🌐 web | 2026-09-15 18:58:06 | 2 模型
    [时段管理器] 🕐 2026-09-15 23:30:08（周二）| 当前: 空闲 | AI: ✅
    🔒 初始化敏感词数据库...
    📚 初始化 AI 学习库...
    🤖 AI 助手初始化中...
    ✅ 可用模型: ['deepseek-flash', 'deepseek-v4-pro']
    ✅ AI 调度器就绪
    🔌 连通性测试...
    ✅ 连通性测试通过

V8_3 的差异（都是因为架构不同）：
* 没有 Alist，所以"挂载存储适配器"换成**列出网盘实例**（目录/凭证/Python），
  "连通性测试"换成**逐个网盘实例拉一次账号状态**；
* 连通性测试放后台线程，先把横幅打完再逐条报结果，不拖慢启动；
* 全部走 :mod:`v8_3.日志`，所以终端/日志页/日志文件三处都能看到。
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from datetime import datetime
from pathlib import Path

from .日志 import 终端输出

分隔线 = "=" * 62
星期 = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


_当前输出: list[str] = []


def _打(消息: str, 级别: str = "信息") -> None:
    终端输出(消息, 级别=级别)
    _当前输出.append(str(消息))


def _行级(消息, 级别: str = "信息") -> None:
    """只记录、不打印（给"已经由别处打过"的行用）。"""
    _当前输出.append(str(消息))


class 启动自检:
    """按 V8 的顺序做一遍启动自检，并返回装配好的 AI 运行时。"""

    def __init__(self, 配置: dict | None = None, 配置路径: str | Path | None = None,
                 *, 主题显示名: str = "", 禁用网络: bool | None = None,
                 自动刷新价格: bool = True, 连通性: bool = True,
                 后台连通性: bool = True, 日志回调=None):
        from .配置 import 加载配置
        self.配置 = 配置 if isinstance(配置, dict) else 加载配置()
        self.配置路径 = 配置路径
        self.主题显示名 = 主题显示名
        self.禁用网络 = 禁用网络
        self.自动刷新价格 = 自动刷新价格
        self.要连通性 = 连通性
        self.后台连通性 = 后台连通性
        self.日志回调 = 日志回调
        self.运行时 = None
        self.行: list[str] = []          # 横幅原文（界面日志页可以接着显示）
        self.结果: dict = {"网盘": [], "可用": 0, "总数": 0}
        self.网盘记录: list[dict] = []

    # ==================== 主流程 ====================

    def 运行(self) -> "启动自检":
        _当前输出.clear()
        _打(分隔线)
        _打("🚀 网盘管理 V8_3 · 启动自检")
        self._主题()
        self._价格()
        self._时段与预算()
        self._敏感词库()
        self._AI()
        self._网盘实例()
        _打(分隔线)
        if self.要连通性:
            self._连通性()
        self.行 = list(_当前输出)
        return self

    # ==================== 各步骤 ====================

    def _主题(self):
        try:
            from .界面.主题管理器 import 主题管理器
            from .配置 import 界面配置
            名称 = (界面配置(self.配置).get("主题")
                  or 主题管理器.获取默认主题())
            if 名称 not in 主题管理器.获取所有主题名():
                名称 = 主题管理器.获取默认主题()
            self.结果["主题"] = 名称
            _打(f"🎨 主题: {主题管理器.获取主题显示名(名称)}")
        except Exception as e:  # noqa: BLE001
            _打(f"⚠️ 主题读取失败，用默认：{e}", "警告")

    def _价格(self):
        _打("💰 初始化价格抓取器...")
        try:
            from .AI.DeepSeek价格抓取 import DeepSeek价格抓取器
            from .配置 import AI配置
            ai = AI配置(self.配置)
            抓取器 = DeepSeek价格抓取器(
                配置=ai, 自动刷新=bool(self.自动刷新价格),
                禁用网络=self.禁用网络, 打印状态=False)
            信息 = 抓取器.获取元信息() or {}
            _打(f"[DeepSeek价格] 🌐 {信息.get('来源', 'builtin')} | "
                f"{信息.get('抓取时间', '?')} | {信息.get('模型数', 0)} 模型")
            try:
                抓取器.启动定时刷新()      # 它自己会打 "[DeepSeek价格] ⏰ 定时刷新已启动"
            except Exception:
                pass
            self.价格抓取器 = 抓取器
        except Exception as e:  # noqa: BLE001
            self.价格抓取器 = None
            _打(f"⚠️ 价格抓取器不可用（AI 页仍可手动刷新）：{e}", "警告")

    def _时段与预算(self):
        try:
            from .AI.时段预算 import 时段管理器, 预算管理器
            from .配置 import AI配置
            ai = AI配置(self.配置)
            self.时段管理器 = 时段管理器(
                价格抓取器=getattr(self, "价格抓取器", None),
                配置=ai, 打印启动状态=False)
            self.预算管理器 = 预算管理器(配置=self.配置)
            现在 = datetime.now()
            时段 = self.时段管理器.获取当前时段类型()
            启用 = self.时段管理器.是否启用AI()
            _打(f"[时段管理器] 🕐 {现在.strftime('%Y-%m-%d %H:%M:%S')}"
                f"（{星期[现在.weekday()]}）| 当前: {时段} | "
                f"AI: {'✅' if 启用 else '❌'}")
            if not 启用 and ai["高峰时段"]["启用"]:
                _打(f"[时段管理器] ℹ️ 高峰时段不调用 AI（{ai['高峰时段']['开始']}"
                    f"–{ai['高峰时段']['结束']}），AI 页可「切换模式」强制开启")
            _打(f"[预算管理器] {self.预算管理器.获取预算摘要()}")
        except Exception as e:  # noqa: BLE001
            self.时段管理器 = None
            self.预算管理器 = None
            _打(f"⚠️ 时段/预算管理器不可用：{e}", "警告")

    def _敏感词库(self):
        _打("🔒 初始化敏感词数据库...")
        try:
            from .配置 import 敏感词配置
            from .敏感词.敏感词数据库 import 敏感词数据库
            参数 = 敏感词配置(self.配置)
            if not 参数["启用"]:
                self.敏感词库 = None
                _打("[敏感词数据库] ⏸ 已在配置里关闭（上传不做预检改名）")
                return
            库 = 敏感词数据库(参数["数据库路径"])   # 它自己会打 初始化/缓存加载
            列表 = 库.列出敏感词()
            self.敏感词库 = 库
            self.结果["敏感词数"] = len(列表)
            _打(f"[敏感词数据库] 🔧 自动改名 "
                f"{'开（上传前预检替换安全词）' if 参数['自动改名上传'] else '关'}，"
                f"按网盘启用："
                f"{参数['按网盘启用'] or '全部'}")
        except Exception as e:  # noqa: BLE001
            self.敏感词库 = None
            _打(f"[敏感词数据库] ⚠️ 不可用，已降级为不改名：{e}", "警告")

    @contextlib.contextmanager
    def _安静构建AI(self):
        """构建 AI 运行时期间压掉 AI 层自己的 INFO 日志。

        下面几行已经把它们的关键信息打印出来了，重复一遍只会更难读；
        WARNING 及以上（真正的失败）照样会打。
        """
        记录器 = [logging.getLogger(n) for n in
               ("v8_3.AI", "v8_3.AI.AI智能助手", "v8_3.AI.DeepSeek价格抓取",
                "v8_3.AI.时段预算", "v8_3.AI.AI学习库", "v8_3.AI.运行时",
                "v8_3.AI.AI调度器")]
        旧级别 = [(r, r.level) for r in 记录器]
        try:
            for r in 记录器:
                r.setLevel(logging.WARNING)
            yield
        finally:
            for r, 级别 in 旧级别:
                r.setLevel(级别)

    def _AI(self):
        _打("📚 初始化 AI 学习库 / 🤖 AI 助手...")
        try:
            from .AI.运行时 import AI运行时
            from .配置 import AI配置
            ai = AI配置(self.配置)
            禁用网络 = self.禁用网络
            if 禁用网络 is None:
                禁用网络 = not bool(ai["api密钥"])       # 没密钥就别联网
            with self._安静构建AI():
                self.运行时 = AI运行时(
                    self.配置, self.配置路径, 日志回调=self.日志回调,
                    自动刷新价格=False, 禁用网络=bool(禁用网络))
            库 = getattr(self.运行时, "学习库", None)
            if 库 is not None:
                try:
                    统计 = 库.获取统计() or {}
                except Exception:
                    统计 = {}
                _打(f"[AI学习库] ✅ 初始化: {getattr(库, '数据库路径', '?')}")
                _打(f"[AI学习库] ⚡ 预加载 {统计.get('总记录', 0)} 条")
            else:
                _打("[AI学习库] ⏸ 未启用（AI 关闭或无密钥时不落盘）")
            # ---- 本地 DeepSeek 模型（V8_3 新增）----
            本地摘要 = {}
            try:
                # 启动时显式探测一次：即使用户没启用，也告诉他"这台机器能白嫖"
                self.运行时.获取本地模型状态(重新检测=True)
                本地摘要 = self.运行时.获取本地模型摘要()
                本地行 = self.运行时.获取本地模型一行()
                if 本地摘要.get("启用") and 本地摘要.get("可用"):
                    _打(f"🏠 {本地行}")
                    _打(f"[本地模型] 🆓 免费离线推理 · "
                        f"{本地摘要.get('模型')} · 来源：{本地摘要.get('提供方')}"
                        f" · 延迟 {本地摘要.get('延迟毫秒', 0):.0f}ms")
                elif 本地摘要.get("启用"):
                    _打(f"🏠 {本地行}", "警告")
                    for 行 in str(本地摘要.get("说明") or "").splitlines()[:4]:
                        if 行.strip():
                            _行级(f"[本地模型] {行}")
                elif 本地摘要.get("可用"):
                    _打("🏠 本地模型：⚪ 已关闭，但检测到可用服务"
                        f"（{本地摘要.get('地址')} · {本地摘要.get('模型')}）"
                        "→ AI 页一键开启，免费用")
                else:
                    _打("🏠 本地模型：⚪ 未启用（可离线免费用，见 docs/本地模型.md）")
                self.结果["本地模型"] = bool(本地摘要.get("可用"))
            except Exception as e:  # noqa: BLE001
                _打(f"⚠️ 本地模型检测失败：{e}", "警告")
                self.结果["本地模型"] = False
            if not ai["api密钥"] and not 本地摘要.get("可用"):
                _打("⚠️ 未配置 DeepSeek 密钥，本地模型也不可用：AI 只做规则调度"
                    "（可在 AI 页开启本地模型，或导入云端密钥）", "警告")
            elif not ai["api密钥"]:
                _打("ℹ️ 未配置云端密钥，但本地模型可用：AI 决策走本地（免费）")
            else:
                try:
                    模型列表 = list(self.运行时.获取模型列表() or [])
                except Exception:
                    模型列表 = []
                _打(f"✅ 可用模型: {模型列表}")
                _打("[AI助手] 🔧 价格抓取器已注入")
            if getattr(self.运行时, "调度器", None) is not None:
                _打("✅ AI 调度器就绪")
            else:
                _打("⚠️ AI 调度器不可用，传输按规则跑", "警告")
            self.结果["AI"] = bool(self.运行时.是否可用())
            self.结果["本地模型"] = bool(
                (self.运行时.获取本地模型摘要() or {}).get("可用"))
        except Exception as e:  # noqa: BLE001
            self.运行时 = None
            self.结果["AI"] = False
            _打(f"⚠️ AI 运行时初始化失败，传输按规则跑：{e}", "警告")

    def _网盘实例(self):
        """打印每个网盘的核心信息（目录/凭证/解释器/线程/敏感词/同类型提示）。"""
        from .配置 import 适配器规格表, 网盘实例列表
        from .网盘信息 import 网盘信息
        _打("🧩 读取网盘核心信息...")
        self.信息表 = 网盘信息(self.配置, 词库=getattr(self, "敏感词库", None))
        try:
            self.网盘记录 = self.信息表.收集()
        except Exception as e:  # noqa: BLE001
            self.网盘记录 = []
            _打(f"⚠️ 网盘信息收集失败：{e}", "警告")
            return
        self.信息表.打印(self.网盘记录, _打, 含账号=False)
        规格表 = 适配器规格表(self.配置)
        启用 = [(规格表[x["标识"]], x.get("目录可用", True))
              for x in self.网盘记录
              if x.get("启用") and x["标识"] in 规格表]
        self.结果["网盘"] = [规格.标识 for 规格, _ in 启用]
        self.结果["总数"] = len(启用)
        self.结果["规格表"] = {规格.标识: 规格 for 规格, _ in 启用}
        self.结果["网盘记录"] = self.网盘记录

    # ==================== 连通性 ====================

    def _连通性(self):
        from .配置 import 动作
        规格表 = self.结果.get("规格表") or {}
        if not 规格表:
            _打("🔌 连通性测试: 跳过（没有启用的网盘）")
            return
        _打("🔌 连通性测试...（后台逐个网盘检查，界面可以先用）")
        if not self.后台连通性:
            self._连通性一轮(规格表)
            return
        线程 = threading.Thread(
            target=self._连通性一轮, args=(规格表,),
            name="启动连通性自检", daemon=True)
        线程.start()

    def _连通性一轮(self, 规格表: dict):
        """逐个网盘拉账号状态，并把"账号/容量/详情/注意"补进核心信息一起打印。"""
        from .配置 import 动作
        from .网盘信息 import 网盘信息
        信息表 = getattr(self, "信息表", None) or 网盘信息(self.配置)
        记录表 = {x["标识"]: x for x in (self.网盘记录 or [])}
        可用 = 0
        动作对象 = 动作(self.配置, 日志回调=self.日志回调 or (lambda m, l="信息": None))
        try:
            for 标识, 规格 in 规格表.items():
                try:
                    适配器 = 动作对象.适配器(标识)
                except Exception as e:  # noqa: BLE001
                    _打(f"❌ {规格.图标} {规格.显示名}：桥进程起不来 — "
                        f"{str(e)[:140]}", "错误")
                    continue
                条目 = 信息表.拉账号(记录表.get(标识, {"标识": 标识,
                                                  "名称": 规格.显示名,
                                                  "图标": 规格.图标,
                                                  "启用": True}), 适配器)
                记录表[标识] = 条目
                信息表.打印一个(条目, _打, 含账号=True, 只账号=True)
                if 条目.get("登录"):
                    可用 += 1
                elif 条目.get("错误"):
                    _打(f"      ↳ {规格.显示名} 读取失败，"
                        f"页面上会显示同样原因", "警告")
        finally:
            try:
                动作对象.关闭()
            except Exception:
                pass
        self.网盘记录 = [记录表.get(x["标识"], x) for x in (self.网盘记录 or [])]
        self.结果["可用"] = 可用
        if 可用 == len(规格表):
            _打(f"✅ 连通性测试通过（{可用}/{len(规格表)} 个网盘可用）")
        elif 可用:
            _打(f"⚠️ 连通性测试：{可用}/{len(规格表)} 个网盘可用，"
                f"其余在页面上会显示失败原因", "警告")
        else:
            _打(f"❌ 连通性测试失败：{len(规格表)} 个网盘都不可用"
                f"（检查适配器目录与登录数据）", "错误")


def 运行启动自检(配置: dict | None = None, 配置路径=None, **参数) -> 启动自检:
    """便捷入口：跑一遍启动自检并返回结果（``.运行时`` 可直接交给主窗口）。"""
    return 启动自检(配置, 配置路径, **参数).运行()
