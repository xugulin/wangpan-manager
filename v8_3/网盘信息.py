"""网盘核心信息：有哪些网盘可用、每个网盘的关键细节。

用途：
* 启动自检时打印（``启动自检`` 调用）；
* CLI ``网盘详情`` / ``状态`` 子命令；
* 界面要展示时也能直接复用（纯文本，无 Qt 依赖）。

**安全约定**：只打印"凭证文件在哪、多大、什么时候写的、是否已配置"，
绝不打印凭证内容；``详情`` 里的字段统一走 :func:`脱敏`，
带 token/cookie/bduss 之类字样的字段只显示"有/无"，长串一律打码。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

#: 这些字样出现在字段名里 → 只显示"有/无"，绝不显示值
敏感字段词 = ("token", "cookie", "bduss", "stoken", "secret", "key",
          "password", "passwd", "credential", "凭据", "密钥", "凭证",
          "pus", "kps", "uid", "sub", "session", "会话")

#: 展示时更友好的字段名
字段中文 = {
    "member_type": "会员类型", "total_capacity": "总容量",
    "use_capacity": "已用容量", "phone_number": "手机号",
    "created_at": "注册时间", "password_updated_at": "改密时间",
    "name": "昵称", "user_name": "用户名", "vip": "会员",
    "note": "备注",
}

容量字段 = ("total_capacity", "use_capacity", "free_capacity", "capacity",
        "quota", "used", "size")

容量单位 = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")


def 格式化字节(字节) -> str:
    try:
        值 = float(字节 or 0)
    except (TypeError, ValueError):
        return str(字节)
    for 单位 in 容量单位:
        if 值 < 1024 or 单位 == 容量单位[-1]:
            if 单位 == "B":
                return f"{int(值)} B"
            return f"{值:.1f} {单位}"
        值 /= 1024
    return str(字节)


def 格式化时间(时间戳) -> str:
    try:
        值 = float(时间戳)
    except (TypeError, ValueError):
        return str(时间戳 or "-")
    if 值 <= 0:
        return "-"
    return datetime.fromtimestamp(值).strftime("%Y-%m-%d %H:%M")


def 美化值(值) -> str:
    """把 ISO 时间戳、"None" 之类的值变得好读。"""
    文本 = str(值)
    if len(文本) >= 16 and 文本[:4].isdigit() and 文本[4] == "-" \
            and "T" in 文本:
        return 文本[:16].replace("T", " ")      # 2026-04-21T11:04:10.6Z → 2026-04-21 11:04
    if 文本 in ("None", "null", ""):
        return "-"
    return 文本


def _像密钥(值) -> bool:
    文本 = str(值 or "")
    if len(文本) < 16:
        return False
    import re
    if re.fullmatch(r"[0-9a-fA-F]{16,}", 文本):        # hex 串
        return True
    if re.fullmatch(r"[0-9a-zA-Z_\-+/=]{16,}", 文本):   # base64/token 形态
        return True
    return False


def 脱敏(详情: dict | None, 最多字段: int = 12) -> list[str]:
    """把适配器返回的 ``详情`` 变成可安全打印的片段列表。"""
    if not isinstance(详情, dict) or not 详情:
        return []
    片段: list[str] = []
    for 键, 值 in list(详情.items())[:最多字段]:
        名 = 字段中文.get(键, str(键))
        小写 = str(键).lower()
        if any(词 in 小写 for 词 in 敏感字段词):
            if isinstance(值, bool):
                片段.append(f"{名} {'✅' if 值 else '❌'}")
            else:
                片段.append(f"{名} {'已配置' if 值 else '无'}")
            continue
        if isinstance(值, dict):
            子 = []
            for 子键, 子值 in 值.items():
                子名 = 字段中文.get(子键, str(子键))
                if 子键 in 容量字段 or "capacity" in str(子键).lower():
                    子.append(f"{子名} {格式化字节(子值)}")
                elif isinstance(子值, bool):
                    子.append(f"{子名} {'✅' if 子值 else '❌'}")
                elif _像密钥(子值):
                    子.append(f"{子名} 已配置")
                else:
                    子.append(f"{子名} {美化值(子值)}")
            片段.append("（" + " · ".join(子) + "）")
            continue
        if isinstance(值, bool):
            片段.append(f"{名} {'✅' if 值 else '❌'}")
            continue
        if 键 in 容量字段 or "capacity" in 小写:
            片段.append(f"{名} {格式化字节(值)}")
            continue
        if _像密钥(值):
            片段.append(f"{名} 已配置（{len(str(值))} 字符，已打码）")
            continue
        文本 = 美化值(值)
        if len(文本) > 60:
            文本 = 文本[:57] + "…"
        片段.append(f"{名} {文本}")
    return 片段


def 账号警告(详情: dict | None) -> list[str]:
    """从详情里提炼"要注意什么"（例如百度写操作需要重新登录）。"""
    if not isinstance(详情, dict):
        return []
    警告: list[str] = []
    if 详情.get("refresh_error") or 详情.get("user_error"):
        原因 = str(详情.get("refresh_error") or 详情.get("user_error"))
        警告.append(f"读正常，写操作需要重新登录（{原因[:60]}）")
    if 详情.get("logged_in") is False:
        警告.append("未登录：点网盘页「登录 / 管理」登录")
    for 键 in ("vip_expired", "expired", "need_relogin"):
        if 详情.get(键):
            警告.append(f"{字段中文.get(键, 键)}")
    return 警告


class 网盘信息:
    """收集 / 格式化每个网盘的核心信息。"""

    def __init__(self, 配置: dict | None = None, 词库=None):
        from .配置 import 加载配置
        self.配置 = 配置 if isinstance(配置, dict) else 加载配置()
        self._外部词库 = 词库          # 复用已经打开的敏感词库，避免重复初始化

    # ---------------- 收集（不需要联网） ----------------

    def 收集(self) -> list[dict]:
        from .配置 import 适配器规格表, 敏感词配置, 网盘实例列表
        from .核心.适配器 import 适配器规格, 类型图标
        from .核心.模型 import 网盘类型

        规格表 = 适配器规格表(self.配置)
        词配置 = 敏感词配置(self.配置)
        词库 = self._外部词库
        自建词库 = False
        if 词库 is None:
            try:
                from .敏感词.敏感词数据库 import 敏感词数据库
                if 词配置["启用"]:
                    路径 = Path(词配置["数据库路径"])
                    if 路径.is_file():
                        词库 = 敏感词数据库(str(路径))
                        自建词库 = True
            except Exception:
                词库 = None

        记录: list[dict] = []
        实例们 = 网盘实例列表(self.配置)
        类型计数: dict[str, int] = {}
        for 项 in 实例们:
            类型计数[项["类型"]] = 类型计数.get(项["类型"], 0) + 1

        for 项 in 实例们:
            类型 = 网盘类型.解析(项["类型"])
            规格 = 规格表.get(项["标识"])
            条目 = {
                "标识": 项["标识"], "类型": 项["类型"],
                "类型中文": 类型.中文名, "名称": 项["名称"],
                "图标": 类型图标.get(类型, "☁"), "启用": bool(项["启用"]),
                "线程数": int(项["线程数"]), "源码路径": 项["路径"],
                "同类型实例数": 类型计数.get(项["类型"], 1),
            }
            if 规格 is None:
                条目.update({"目录可用": False, "目录提示": "已停用",
                         "路径": str(项["路径"])})
                记录.append(条目)
                continue
            可用, 提示 = 适配器规格.校验目录(规格.路径)
            凭证 = 规格.凭证文件
            条目.update({
                "目录可用": 可用, "目录提示": 提示,
                "路径": str(规格.路径),
                "Python": str(规格.Python解释器),
                "数据目录": str(规格.数据目录),
                "凭证": str(凭证),
                "凭证相对": f"数据/{凭证.name}",
                "凭证有": 凭证.is_file(),
                "凭证大小": 格式化字节(凭证.stat().st_size) if 凭证.is_file() else "-",
                "凭证时间": (格式化时间(凭证.stat().st_mtime)
                         if 凭证.is_file() else "-"),
                "守卫": ("开" if (词配置["启用"] and
                              词配置["按网盘启用"].get(项["标识"], True))
                      else "关"),
                "词库词数": None,
            })
            记录.append(条目)

        if 词库 is not None:
            for 条目 in 记录:
                try:
                    条目["词库词数"] = len(词库.列出敏感词(条目["标识"]))
                except Exception:
                    条目["词库词数"] = None
            if 自建词库:                     # 只关自己开的那个
                try:
                    词库.关闭()
                except Exception:
                    pass
        return 记录

    # ---------------- 收集（需要联网：账号状态） ----------------

    @staticmethod
    def 拉账号(记录: dict, 适配器) -> dict:
        """把某个网盘的账号信息补进记录（含桥进程耗时与 ping）。"""
        import time
        条目 = dict(记录)
        开始 = time.time()
        try:
            信息 = 适配器.账号状态()
            条目["桥耗时"] = time.time() - 开始
            条目["登录"] = bool(信息.已登录)
            条目["用户"] = 信息.用户名 or "-"
            条目["数据目录"] = 信息.数据目录 or 条目.get("数据目录", "")
            详情 = dict(信息.详情 or {})
            条目["详情片段"] = 脱敏(详情)
            条目["警告"] = 账号警告(详情)
            会员 = 详情.get("member") if isinstance(详情.get("member"), dict) else {}
            if 会员:
                总 = 会员.get("total_capacity")
                用 = 会员.get("use_capacity")
                类型 = 会员.get("member_type")
                文本 = []
                if 总:
                    文本.append(f"总容量 {格式化字节(总)}")
                if 用:
                    文本.append(f"已用 {格式化字节(用)}")
                if 类型:
                    文本.append(str(类型))
                条目["容量"] = " · ".join(文本)
        except Exception as e:  # noqa: BLE001
            条目["桥耗时"] = time.time() - 开始
            条目["登录"] = None
            条目["错误"] = str(e)[:160]
        适配器对象 = 适配器
        try:
            开始 = time.time()
            适配器对象.调用("ping", {}, 超时=10.0)
            条目["ping毫秒"] = (time.time() - 开始) * 1000
        except Exception:
            条目["ping毫秒"] = None
        return 条目

    # ---------------- 打印 ----------------

    def 打印(self, 记录列表: list[dict], 打, *, 含账号: bool = False,
             标题: str = "") -> None:
        启用 = [x for x in 记录列表 if x.get("启用")]
        停用 = [x for x in 记录列表 if not x.get("启用")]
        可用 = [x for x in 启用 if x.get("目录可用")]
        标题 = 标题 or (f"🧩 网盘核心信息（启用 {len(启用)} / 配置 "
                    f"{len(记录列表)}）")
        打(标题)
        if not 记录列表:
            打("   （还没有配置网盘：左下角「➕ 新增网盘」或 CLI 网盘新增）")
            return
        for 条目 in 记录列表:
            self._打印一个(条目, 打, 含账号=含账号)
        if 停用:
            打(f"⏸ 停用 {len(停用)} 个："
              + "、".join(f"{x['名称']}（{x['标识']}）" for x in 停用)
              + " —— 在「编辑网盘」里可启用")
        打(f"✅ 可用网盘：{len(可用)}/{len(启用)}"
          + ("（" + "、".join(f"{x['图标']} {x['名称']}" for x in 可用) + "）"
             if 可用 else ""))

    def 打印一个(self, 条目: dict, 打, *, 含账号: bool = False,
             只账号: bool = False) -> None:
        self._打印一个(条目, 打, 含账号=含账号, 只账号=只账号)

    def _打印一个(self, 条目: dict, 打, *, 含账号: bool,
                 只账号: bool = False) -> None:
        if 只账号:
            # 核心信息已经打过了，这里只补"账号/详情/注意"，避免整块重复
            图标 = 条目.get("图标", "☁")
            打(f"   {图标} {条目.get('名称', '?')}（{条目.get('标识', '?')}）")
            self._打印账号(条目, 打)
            return
        图标 = 条目.get("图标", "☁")
        名称 = 条目.get("名称", "?")
        标识 = 条目.get("标识", "?")
        if not 条目.get("启用"):
            打(f"   ⏸ {图标} {名称}（{标识}）· 已停用")
            return
        状态 = "✅ 可用" if 条目.get("目录可用") else "❌ 不可用"
        打(f"   {图标} {名称}（{标识}）· {状态} · 线程 {条目.get('线程数', '?')} · "
          f"类型 {条目.get('类型中文', 条目.get('类型'))}")
        打(f"      适配器  : {条目.get('路径', '-')}")
        if not 条目.get("目录可用"):
            打(f"      ⚠️ 提示  : {条目.get('目录提示', '目录不可用')}")
        if 条目.get("类型") == "fake":
            打("      凭证    : 不需要（本地假网盘，直接读写本地目录）")
        elif 条目.get("凭证") is not None:
            凭证 = (f"{条目.get('凭证相对', '数据/…')} "
                  + ("✅" if 条目.get("凭证有") else "❌ 无")
                  + f" · {条目.get('凭证大小', '-')} · {条目.get('凭证时间', '-')}")
            打(f"      凭证    : {凭证}")
        if 条目.get("Python"):
            打(f"      解释器  : {条目['Python']}")
        守卫 = 条目.get("守卫")
        if 守卫 is not None:
            词数 = 条目.get("词库词数")
            打(f"      敏感词  : {守卫}"
              + (f"（该盘词库 {词数} 词）" if 词数 is not None else ""))
        if 条目.get("同类型实例数", 1) > 1:
            打(f"      ℹ️ 提示  : 同一家网盘配了 {条目['同类型实例数']} 个实例，"
              f"各自需要独立的适配器目录（凭证互相隔离）")
        if 含账号:
            self._打印账号(条目, 打)

    @staticmethod
    def _打印账号(条目: dict, 打) -> None:
        耗时 = 条目.get("桥耗时")
        时间文本 = f"{耗时:.1f}s" if isinstance(耗时, (int, float)) else "-"
        ping = 条目.get("ping毫秒")
        if isinstance(ping, (int, float)):
            ping文本 = (f" · ping {ping:.1f}ms" if ping < 10
                      else f" · ping {ping:.0f}ms")
        else:
            ping文本 = ""
        if 条目.get("错误"):
            打(f"      账号    : ❌ 读取失败 — {条目['错误']}（{时间文本}）")
            return
        登录 = 条目.get("登录")
        灯 = "🟢 已登录" if 登录 else ("⚪ 未登录" if 登录 is False else "❔ 未知")
        片段 = [灯, f"用户 {条目.get('用户', '-')}"]
        if 条目.get("容量"):
            片段.append(条目["容量"])
        打(f"      账号    : " + " · ".join(片段)
          + f"（{时间文本}{ping文本}）")
        if 条目.get("详情片段"):
            打(f"      详情    : " + " · ".join(条目["详情片段"]))
        for 警告 in 条目.get("警告", []) or []:
            打(f"      ⚠️ 注意  : {警告}")


# ============================================================
# 便捷函数
# ============================================================

def 收集网盘信息(配置: dict | None = None) -> list[dict]:
    return 网盘信息(配置).收集()


def 打印网盘信息(记录列表: list[dict], 打, *, 含账号: bool = False,
            配置: dict | None = None) -> None:
    网盘信息(配置).打印(记录列表, 打, 含账号=含账号)
