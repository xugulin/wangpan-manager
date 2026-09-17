# v8_3/敏感词/上传守卫.py
"""上传守卫：敏感词预检改名 + 失败后的探测式绕过。

来源：只读学习 网盘管理_V8 / 适配器层/敏感词绕过器.py 后，在本项目内独立落地。

V8 里绕过器挂在 Alist 适配器上，靠 Alist 返回 405 触发；V8_3 是原生直连，
所以改成"上传守卫"：
  1. **预检改名**（不读文件内容）：上传前用敏感词库扫描文件名，命中且已有
     安全词就直接换名上传，并写改名记录；
  2. **失败绕过**：上传报错且错误信息像"敏感词/违规/禁止/405/403"时，如果
     文件不大（默认 ≤32MiB），把内容读进内存跑 P1–P4 探测式绕过（改名字再传、
     瘦身定位真正触发的最小词、全中文化保底），成功后把学到的词与安全词写库；
  3. **学习**：预检改名成功后确认安全词有效，失败则记失效，下次自动换候选。

对照 V8 的差异（都是修复/优化）：
  * 日志走回调（进"日志"页）而不是 print；
  * 探测文件走本地临时文件中转，避免大文件常驻内存（并设大小上限）；
  * 预检成功后会把安全词标记为"有效"，让学习闭环更快收敛；
  * 全部按**网盘实例标识**分库，同一家网盘的多账号各学各的。
"""

from __future__ import annotations

import os
import re
import tempfile
import time
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from .敏感词数据库 import 全部转拼音, 生成安全词, 是中文字


class 敏感词绕过器:
    """探测式绕过（与传输层完全解耦，沿用 V8 的策略编号 P1–P4）。"""

    P2每段最多候选 = 2
    P3最大组合长度 = 4
    P3每组合最多候选 = 1

    瘦身最大PUT次数 = 30
    瘦身窗口最小长度 = 2
    瘦身窗口最大长度 = 6

    高频敏感词 = [
        "总统", "选举", "政府", "人民", "中央", "人大", "政协",
        "书记", "主席", "总理", "领导",
        "任天堂", "索尼", "世嘉", "育碧", "暴雪",
        "迪士尼", "皮克斯", "梦工厂",
        "成人", "情色", "暴力", "恐怖", "血腥",
        "辛普森", "南方公园", "海绵宝宝", "猫和老鼠",
        "超级马里奥", "塞尔达", "宝可梦",
        "特朗普", "拜登", "普京", "奥巴马",
    ]

    def __init__(self,
                 网盘名称: str,
                 数据库,
                 写函数: Callable[[str, bytes], Tuple[int, bool]],
                 属性函数: Callable[[str], Optional[dict]],
                 高频词: Optional[List[str]] = None,
                 日志: Optional[Callable[[str], None]] = None):
        self.网盘名称 = 网盘名称
        self.数据库 = 数据库
        self._写 = 写函数
        self._属性 = 属性函数
        self._高频词 = 高频词 or self.高频敏感词
        self._日志 = 日志 or (lambda 消息: None)

    # ============================================================
    # 对外主入口
    # ============================================================

    def 尝试绕过(self, 目标路径: str, 数据: bytes) -> Tuple[bool, str]:
        """探测式绕过主入口。返回 (是否成功, 新路径)。"""
        if not self.数据库:
            return False, 目标路径

        文件名 = os.path.basename(目标路径)
        目录 = os.path.dirname(目标路径)

        已知命中 = self.数据库.扫描所有敏感词(self.网盘名称, 文件名)
        已知词集合 = {h["敏感词"] for h in 已知命中}
        段列表 = self.数据库.切中文段(文件名)

        # ---------- P1：已知词批量替换 ----------
        P1候选 = 文件名
        if 已知命中:
            P1候选 = self._批量替换(文件名, 已知命中)
            if P1候选 != 文件名:
                P1路径 = self._拼接(目录, P1候选)
                self._日志(f"[绕过] 🎯 P1 已知词替换：{P1候选}")
                _status, ok = self._写(P1路径, 数据)
                if ok:
                    self._日志("[绕过] ✅ P1 成功")
                    self._记录成功(P1路径, 已知命中)
                    # V8 的 P1 分支漏了改名记录，导致下载还原不了原名——这里补上
                    self._记改名批量(目标路径, P1路径, 已知命中, "P1已知词")
                    return True, P1路径

        # ---------- P2：单段探测 + 瘦身 ----------
        未知段列表 = self._收集未知段(段列表, 已知词集合)
        for 段 in 未知段列表:
            r = self._P2单段(目标路径, 数据, 文件名, 目录,
                           P1候选, 段, 已知词集合)
            if r:
                return r

        # ---------- P3：组合探测 + 瘦身 ----------
        词序列 = [s["段"] for s in 段列表 if len(s["段"]) >= 2]
        r = self._P3组合(目标路径, 数据, 文件名, 目录,
                       P1候选, 词序列, 已知词集合)
        if r:
            return r

        # ---------- P4：全中文化保底 ----------
        return self._P4保底(目标路径, 数据, 文件名, 目录)

    # ============================================================
    # 上传前预检（不改数据，仅改文件名）
    # ============================================================

    def 预检(self, 目标路径: str, 大小: int = 0) -> Tuple[str, bool]:
        """上传前预检：扫描已知敏感词并批量替换文件名。

        返回 (新路径, 是否改过名)。
        """
        if not self.数据库:
            return 目标路径, False
        文件名 = os.path.basename(目标路径)
        目录 = os.path.dirname(目标路径)
        命中列表 = self.数据库.扫描所有敏感词(self.网盘名称, 文件名)
        if not 命中列表:
            return 目标路径, False

        新文件名 = self._批量替换(文件名, 命中列表)
        if 新文件名 == 文件名:
            return 目标路径, False

        新路径 = self._拼接(目录, 新文件名)
        for h in 命中列表:
            敏感词 = h["敏感词"]
            信息 = self.数据库.获取敏感词(self.网盘名称, 敏感词)
            安全词 = (信息.get("最后一次成功安全词") if 信息 else None) \
                or self._获取已有安全词(敏感词)
            if 安全词:
                try:
                    self.数据库.记录改名(
                        网盘名称=self.网盘名称,
                        原云端路径=目标路径,
                        云端实际路径=新路径,
                        命中的敏感词=敏感词,
                        当前安全词=安全词,
                        改名策略="预检",
                        改名原因=f"上传前预检命中'{敏感词}'",
                        文件大小=大小,
                    )
                except Exception:
                    pass
                self.数据库.增加统计("预检命中")
        self._日志(f"[敏感词] 预检改名：{文件名} → {新文件名}")
        return 新路径, True

    def 确认预检成功(self, 原路径: str = "") -> None:
        """预检改名后上传成功 → 记一次成功统计（学习闭环优化的口径）。

        原名命中已经在 :meth:`预检` 里通过"改名记录 + 安全词"落库，这里不
        再重复扫词（改后的名字已不含敏感词，扫也扫不到）。
        """
        if not self.数据库:
            return
        try:
            self.数据库.增加统计("预检成功")
        except Exception:
            pass

    # ============================================================
    # P2 单段 / P3 组合 / P4 保底
    # ============================================================

    def _P2单段(self, 原路径, 数据, 文件名, 目录,
                P1候选, 段, 已知词集合) -> Optional[Tuple[bool, str]]:
        候选安全 = self.数据库.生成候选安全词列表(self.网盘名称, 段)
        if not 候选安全:
            return None
        for 安全词 in 候选安全[:self.P2每段最多候选]:
            if not 安全词 or 安全词 == 段:
                continue
            新文件名 = P1候选.replace(段, 安全词)
            if 新文件名 == P1候选:
                continue
            新路径 = self._拼接(目录, 新文件名)
            self._日志(f"[绕过] 🕵️ P2 单段：'{段}' → '{安全词}'")
            _status, ok = self._写(新路径, 数据)
            self._记录探测("P2单段", 段, ok)
            if ok:
                self._日志(f"[绕过] 🎓 P2 学到：'{段}'")
                最短词 = self._瘦身(原路径, 数据, 段, 已知词集合)
                最终路径 = self._确认敏感词(原路径, 数据, 文件名, 目录,
                                       最短词, 新路径, "P2单段瘦身")
                self.数据库.增加统计("P2单段成功")
                return True, 最终路径
        return None

    def _P3组合(self, 原路径, 数据, 文件名, 目录,
                P1候选, 词序列, 已知词集合) -> Optional[Tuple[bool, str]]:
        if len(词序列) < 2:
            return None
        for 长度 in range(2, min(len(词序列) + 1, self.P3最大组合长度 + 1)):
            for 起 in range(len(词序列) - 长度 + 1):
                子 = 词序列[起:起 + 长度]
                if all(w in 已知词集合 for w in 子):
                    continue
                组合词 = "".join(子)
                候选安全 = 全部转拼音(组合词)
                if not 候选安全 or 候选安全 == 组合词:
                    continue
                新文件名 = P1候选.replace(组合词, 候选安全)
                if 新文件名 == P1候选:
                    continue
                新路径 = self._拼接(目录, 新文件名)
                self._日志(f"[绕过] 🕵️ P3 组合({长度})：'{组合词}' → '{候选安全}'")
                _status, ok = self._写(新路径, 数据)
                self._记录探测(f"P3组合{长度}", 组合词, ok)
                if ok:
                    self._日志(f"[绕过] 🎓 P3 学到：'{组合词}'")
                    最短词 = self._瘦身(原路径, 数据, 组合词, 已知词集合)
                    最终路径 = self._确认敏感词(原路径, 数据, 文件名, 目录,
                                           最短词, 新路径, f"P3组合瘦身{长度}")
                    self.数据库.增加统计("P3组合成功")
                    return True, 最终路径
        return None

    def _P4保底(self, 原路径, 数据, 文件名, 目录) -> Tuple[bool, str]:
        try:
            def 替换段(m):
                return 全部转拼音(m.group(0))
            P4候选 = re.sub(r"[\u4e00-\u9fff]+", 替换段, 文件名)
        except Exception:
            return False, 原路径
        if P4候选 == 文件名:
            return False, 原路径
        P4路径 = self._拼接(目录, P4候选)
        self._日志(f"[绕过] 💥 P4 全中文化：{P4候选}")
        _status, ok = self._写(P4路径, 数据)
        if ok:
            self._日志("[绕过] ✅ P4 成功")
            self.数据库.增加统计("P4保底成功")
            self._记录探测("P4全中文化", P4候选, True)
            self._记改名(原路径, P4路径, "全中文化", P4候选, "P4暴力")
            return True, P4路径
        return False, 原路径

    # ============================================================
    # 瘦身
    # ============================================================

    def _瘦身(self, 原路径: str, 数据: bytes,
              误判词: str, 已知词集合: set) -> str:
        if not 误判词 or len(误判词) <= 1:
            return 误判词

        文件名 = os.path.basename(原路径)
        目录 = os.path.dirname(原路径)
        put计数 = [0]

        def 测试(子串: str) -> bool:
            if not 子串 or 子串 not in 文件名:
                return False
            if put计数[0] >= self.瘦身最大PUT次数:
                return False
            安全词 = self._获取已有安全词(子串) or f"__t{len(子串)}__"
            试探名 = 文件名.replace(子串, 安全词, 1)
            if 试探名 == 文件名:
                return False
            试探路径 = self._拼接(目录, 试探名)
            put计数[0] += 1
            _status, ok = self._写(试探路径, 数据)
            self._记录探测("瘦身测试", 子串, ok)
            return ok

        self._日志(f"[瘦身] 🔬 开始 '{误判词}'")

        for 高频 in self._高频词:
            if 高频 == 误判词:
                return 误判词
            if 高频 in 误判词 and 高频 not in 已知词集合:
                self._日志(f"[瘦身] 🎯 优先测试：'{高频}'")
                if 测试(高频):
                    self._日志(f"[瘦身] ✅ 高频命中：'{高频}'")
                    return 高频

        左, 右 = 0, len(误判词)
        while 右 - 左 > 4:
            中点 = (左 + 右) // 2
            左半 = 误判词[左:中点]
            if len(左半) >= 2 and 测试(左半):
                右 = 中点
                continue
            右半 = 误判词[中点:右]
            if len(右半) >= 2 and 测试(右半):
                左 = 中点
                continue
            break

        区间词 = 误判词[左:右]
        if len(区间词) <= 2:
            return 区间词 if 测试(区间词) else 误判词

        最短命中 = None
        最大窗口 = min(len(区间词), self.瘦身窗口最大长度)
        for L in range(self.瘦身窗口最小长度, 最大窗口 + 1):
            if 最短命中:
                break
            for i in range(len(区间词) - L + 1):
                子 = 区间词[i:i + L]
                if 子 in 已知词集合:
                    continue
                if 测试(子):
                    最短命中 = 子
                    break
        if not 最短命中:
            return 误判词

        精简 = 最短命中
        while len(精简) > self.瘦身窗口最小长度:
            去尾 = 精简[:-1]
            if 测试(去尾):
                精简 = 去尾
            else:
                break
        while len(精简) > self.瘦身窗口最小长度:
            去首 = 精简[1:]
            if 测试(去首):
                精简 = 去首
            else:
                break

        self._日志(f"[瘦身] ✅ '{误判词}' → '{精简}'")
        return 精简

    # ============================================================
    # 辅助
    # ============================================================

    def _收集未知段(self, 段列表, 已知词集合) -> List[str]:
        结果 = []
        已见 = set()
        for s in 段列表:
            段 = s["段"]
            if 段 in 已知词集合 or len(段) < 2 or 段 in 已见:
                continue
            已见.add(段)
            结果.append(段)
        结果.sort(key=len, reverse=True)
        return 结果

    def _批量替换(self, 文件名: str, 命中列表: List[dict]) -> str:
        命中列表 = sorted(命中列表, key=lambda h: h["起"], reverse=True)
        结果 = 文件名
        for h in 命中列表:
            安全词 = self._获取已有安全词(h["敏感词"])
            if not 安全词:
                continue
            结果 = 结果[:h["起"]] + 安全词 + 结果[h["止"]:]
        return 结果

    def _获取已有安全词(self, 敏感词: str) -> Optional[str]:
        信息 = self.数据库.获取敏感词(self.网盘名称, 敏感词)
        if 信息 and 信息.get("最后一次成功安全词"):
            return 信息["最后一次成功安全词"]
        候选 = self.数据库.生成候选安全词列表(self.网盘名称, 敏感词)
        return 候选[0] if 候选 else None

    def _确认敏感词(self, 原路径, 数据, 文件名, 目录,
                    最短词, 之前的路径, 策略) -> str:
        安全词 = self._获取已有安全词(最短词) or f"{最短词}_safe"
        最终名 = 文件名.replace(最短词, 安全词, 1)
        最终路径 = self._拼接(目录, 最终名)

        if 最终路径 != 之前的路径:
            _status, ok = self._写(最终路径, 数据)
            if not ok:
                最终路径 = 之前的路径

        try:
            self.数据库.记录发现(self.网盘名称, 最短词, 来源=策略)
            self.数据库.记录安全词成功(self.网盘名称, 最短词, 安全词)
        except Exception:
            pass
        self._记改名(原路径, 最终路径, 最短词, 安全词, 策略)
        return 最终路径

    def _记改名批量(self, 原路径: str, 新路径: str,
                    命中列表: List[dict], 策略: str) -> None:
        if 原路径 == 新路径:
            return
        敏感词们 = "、".join(h.get("敏感词", "") for h in 命中列表) or "未知"
        安全词们 = "、".join(
            (self._获取已有安全词(h.get("敏感词", "")) or "")
            for h in 命中列表 if h.get("敏感词")) or ""
        self._记改名(原路径, 新路径, 敏感词们, 安全词们, 策略)

    def _记录成功(self, 新路径: str, 命中列表: List[dict]):
        for h in 命中列表:
            信息 = self.数据库.获取敏感词(self.网盘名称, h["敏感词"])
            安全词 = 信息.get("最后一次成功安全词") if 信息 else None
            if 安全词:
                self.数据库.记录安全词成功(self.网盘名称, h["敏感词"], 安全词)

    def _记录探测(self, 类型: str, 内容: str, 成功: bool):
        try:
            self.数据库.记录探测(self.网盘名称, "", 类型, 内容,
                            "成功" if 成功 else "405")
        except Exception:
            pass

    def _记改名(self, 原路径, 新路径, 敏感词, 安全词, 策略):
        try:
            self.数据库.记录改名(
                网盘名称=self.网盘名称,
                原云端路径=原路径,
                云端实际路径=新路径,
                命中的敏感词=敏感词,
                当前安全词=安全词,
                改名策略=策略,
                改名原因=f"绕过触发（{策略}）",
            )
        except Exception:
            pass

    @staticmethod
    def _拼接(目录: str, 文件名: str) -> str:
        if not 目录 or 目录 == "/":
            return f"/{文件名}"
        return f"{目录.rstrip('/')}/{文件名}"


class 上传守卫:
    """把"绕过器"接到 V8_3 的云盘适配器上：预检改名 + 失败后探测式绕过。

    用法::

        守卫 = 上传守卫(适配器, "quark", 敏感词库, 日志=print)
        结果 = 守卫.上传("/本地/xxx.mp4", "/电影", 名称="xxx.mp4",
                          进度=回调, 任务ID="t1")
    """

    默认最大绕过字节 = 32 * 1024 * 1024

    #: 错误信息里出现这些字样时认为可能是敏感词/名称拦截。
    #:
    #: ⚠️ 2026-09-16 实测补齐：光鸭对敏感文件名返回的是
    #: 「名称不可用，请更换后重试」——既没有"敏感"也没有 405/403，
    #: 之前这张表漏了它，导致预检与 P1–P4 绕过都没触发（明明是敏感词却直接失败）。
    拦截关键词 = (
        # 通用/风控类
        "405", "403", "451", "敏感", "违规", "禁止", "非法", "审核", "拦截",
        "sensitive", "forbidden", "blocked", "risk", "banned",
        # 各家"文件名不可用"类文案（光鸭实测、其他家常见说法一并覆盖）
        "名称不可用", "名字不可用", "文件名不可用", "名称非法", "名称无效",
        "请更换后重试", "更换后重试", "请更换名字", "请修改名称",
        "名称包含", "文件名包含", "命名不符合", "命名规范",
        "invalid name", "name is not available", "not allowed",
    )

    def __init__(self, 适配器, 网盘标识: str, 数据库, *,
                 启用: bool = True, 自动改名: bool = True,
                 最大绕过字节: int = 默认最大绕过字节,
                 中转目录: str = "",
                 日志=None):
        self.适配器 = 适配器
        self.网盘标识 = 网盘标识
        self.数据库 = 数据库
        self.启用 = bool(启用)
        self.自动改名 = bool(自动改名)
        self.最大绕过字节 = int(最大绕过字节 or self.默认最大绕过字节)
        self.中转目录 = 中转目录 or str(Path(tempfile.gettempdir()) / "v8_3_绕过")
        self._日志回调 = 日志 or (lambda 消息: None)
        self._统计 = {
            "预检改名": 0, "预检成功": 0, "绕过尝试": 0,
            "绕过成功": 0, "绕过跳过_过大": 0, "绕过失败": 0,
        }
        self.绕过器 = 敏感词绕过器(
            网盘标识, 数据库, self._写字节, self._属性,
            日志=self._日志回调) if (self.启用 and 数据库 is not None) else None

    # ---------------- 基本信息 ----------------

    @property
    def 生效(self) -> bool:
        return bool(self.启用 and self.数据库 is not None and self.绕过器)

    def 统计(self) -> dict:
        return dict(self._统计)

    def _日志(self, 消息: str) -> None:
        try:
            self._日志回调(f"[{self.网盘标识}] {消息}")
        except Exception:
            pass

    @staticmethod
    def 是拦截错误(错误文本: str) -> bool:
        文本 = str(错误文本 or "").lower()
        return any(词.lower() in 文本 for 词 in 上传守卫.拦截关键词)

    # ---------------- 注入给绕过器的两个函数 ----------------

    @staticmethod
    def 拼接(目录: str, 名称: str) -> str:
        if not 目录 or 目录 == "/":
            return f"/{名称}"
        return f"{目录.rstrip('/')}/{名称}"

    def _写字节(self, 路径: str, 数据: bytes) -> Tuple[int, bool]:
        """把 bytes 写到网盘的指定路径（先落临时文件，再走适配器上传）。"""
        目录 = os.path.dirname(路径) or "/"
        名称 = os.path.basename(路径)
        os.makedirs(self.中转目录, exist_ok=True)
        临时 = Path(self.中转目录) / f"probe_{int(time.time()*1000)}_{名称}"
        try:
            临时.write_bytes(数据)
            try:
                self.适配器.上传(str(临时), 目录, 名称=名称,
                              任务ID=f"probe_{名称}")
            except Exception as e:
                self._日志(f"[绕过] 探测上传失败：{e}")
                return 500, False
            return 200, True
        except Exception as e:
            self._日志(f"[绕过] 探测写盘失败：{e}")
            return 500, False
        finally:
            try:
                临时.unlink(missing_ok=True)
            except Exception:
                pass

    def _属性(self, 路径: str) -> Optional[dict]:
        try:
            信息 = self.适配器.文件信息(路径)
        except Exception:
            return None
        if 信息 is None:
            return None
        try:
            return 信息.to_dict()
        except Exception:
            return None

    # ---------------- 对外：带守卫的上传 ----------------

    def 预检名称(self, 远端目录: str, 名称: str, 大小: int = 0
                ) -> Tuple[str, bool, str]:
        """返回 (新名称, 是否改名, 原名称)。"""
        if not (self.生效 and self.自动改名) or not 名称:
            return 名称, False, 名称
        目标 = self.拼接(远端目录, 名称)
        try:
            新路径, 改过 = self.绕过器.预检(目标, 大小)
        except Exception as e:
            self._日志(f"[敏感词] 预检异常（按原名上传）：{e}")
            return 名称, False, 名称
        if not 改过:
            return 名称, False, 名称
        self._统计["预检改名"] += 1
        return os.path.basename(新路径), True, 名称

    def 上传(self, 本地路径: str, 远端目录: str, *,
             名称: Optional[str] = None, 进度=None, 任务ID: str = "") -> dict:
        """上传一个本地文件；自动做敏感词预检与失败绕过。"""
        名称 = 名称 or Path(本地路径).name
        try:
            大小 = int(Path(本地路径).stat().st_size)
        except OSError:
            大小 = 0

        新名称, 改过名, 原名称 = self.预检名称(远端目录, 名称, 大小)
        try:
            结果 = self.适配器.上传(本地路径, 远端目录, 名称=新名称,
                                 任务ID=任务ID, 进度=进度)
            # 后端可能因为**目标盘命名限制**又改了一次名（如百度不允许 " 与 emoji）：
            # 这里把它的改名信息落进改名记录，保证"下载可还原原名"依然成立。
            try:
                信息 = (结果 or {}).get("改名信息") if isinstance(结果, dict) else None
                if isinstance(信息, dict) and 信息.get("原名") and 信息.get("新名"):
                    self._记改名(信息.get("原路径") or self.拼接(远端目录,
                                                          信息["原名"]),
                             信息.get("新路径") or self.拼接(远端目录,
                                                          信息["新名"]),
                             "（目标盘命名限制）", 信息["新名"],
                             信息.get("策略") or "命名规避")
                    self._统计["预检改名"] += 1
                    self._日志(f"[命名规避] {信息.get('说明')}")
            except Exception as e:  # noqa: BLE001
                self._日志(f"[命名规避] 记录改名失败（不影响上传）：{e}")
        except Exception as e:
            if not (self.生效 and self.是拦截错误(str(e))):
                raise
            self._统计["绕过尝试"] += 1
            if 大小 > self.最大绕过字节:
                self._统计["绕过跳过_过大"] += 1
                self._日志(f"[绕过] 文件 {大小} 字节超过上限 "
                         f"{self.最大绕过字节}，不再尝试绕过")
                raise
            try:
                数据 = Path(本地路径).read_bytes()
            except Exception as 读错误:
                raise e from 读错误
            目标路径 = self.拼接(远端目录, 新名称)
            self._日志(f"[绕过] 🔎 {名称} 上传被拦，开始探测式绕过…")
            成功, 新路径 = self.绕过器.尝试绕过(目标路径, 数据)
            if not 成功:
                self._统计["绕过失败"] += 1
                raise
            self._统计["绕过成功"] += 1
            最终名 = os.path.basename(新路径)
            self._日志(f"[绕过] ✅ 已改用 {最终名} 上传成功")
            return {
                "name": 最终名,
                "path": 新路径,
                "size": 大小,
                "bytes": 大小,
                "绕过": True,
                "原名称": 原名称,
            }
        if 改过名:
            self._统计["预检成功"] += 1
            try:
                self.绕过器.确认预检成功(self.拼接(远端目录, 新名称))
            except Exception:
                pass
            self._日志(f"[敏感词] ✅ 预检改名后上传成功：{原名称} → {新名称}")
        return dict(结果 or {})


__all__ = ["敏感词绕过器", "上传守卫", "生成安全词", "是中文字", "全部转拼音"]
