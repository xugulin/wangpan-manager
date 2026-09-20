"""百度网盘后端：桥接工作区里的 百度网盘适配器/核心。

除了文件/目录/传输能力，本文件还实现了 V8_3 的**统一登录协议**
（见 后端_基类.py 的「统一登录协议」段）：

* 扫码登录：`login_qr_start()` 取二维码 → `login_qr_wait()` 阻塞轮询到确认，
  成功后自动换取 BDUSS/STOKEN 并补齐 bdstoken；全程只调用适配器
  `核心/认证/登录服务.py` 自己的接口；
* Cookie 登录：`login_cookie()` 把粘贴的 Cookie 交给适配器落盘，再用
  `/api/gettemplatevariable` 向服务端验证（失败会回滚，不弄坏原有会话）；
* 短信 / 邮箱 / 令牌 / 账号密码：百度适配器没有对应端点，能力表如实报
  `支持=False`（界面据此置灰页签），相关方法也返回「不支持」而不是抛异常；
* 凭证落盘一律由适配器自己的 `核心/认证/会话仓库.py` 完成
  （数据/会话.json），本文件不直接读写它的凭证文件。
"""

from __future__ import annotations

import base64
import json
import os
import threading
import time
import uuid
from pathlib import Path

from 后端_基类 import (后端基类, 规范, 多段下载, 读取下载分段, 规范命名, 确认可见)

#: 「自动从浏览器补全会话」的状态（时间节流 + 浏览器是否可用）
_最近补会话: dict = {"时间": 0.0, "可用": True}

#: 退出登录后的"静默期"：这段时间内不做任何自愈（免得刚退出又被自动登回来）
_退出后静默: dict = {"到": 0.0}

#: 静默多久（秒）：10 分钟。用户想立刻重登就点登录按钮，那条路不受影响。
退出后静默秒 = 600.0

#: 「补种 pan 域会话」的节流状态（这一步能把只读会话变成可写会话）
_最近补种: dict = {"时间": 0.0}

#: 写权限结论缓存：带"会话指纹"，令牌一变就作废。
#: ⚠️ 关键教训（现场踩到）：**有 bdstoken 不等于能写**。扫码登录那份会话
#:    bdstoken 拿得到（loginStatus 会下发），但它的 STOKEN 是 passport 域那份，
#:    写接口照样 errno:-6。所以判定必须绑定"当时那份凭证"的指纹。
_写状态缓存: dict = {"指纹": "", "状态": "", "时间": 0.0}


def _会话指纹(仓库) -> str:
    try:
        会话 = 仓库.取会话() or {}
        return f"{str(会话.get('bduss') or '')[:16]}|{str(会话.get('stoken') or '')[:16]}"
    except Exception:
        return ""

#: 写权限探针的结论缓存：网盘页每次刷新状态都会调 account()，
#: 不该每次都多发一条写请求、多刷一条同样的提示。
_写探针缓存: dict = {"时间": 0.0, "提示": "", "窗口": 0.0}


def _写探针缓存写入(提示: str, 现在: float) -> None:
    #: 通过时缓存 5 分钟（正常状态不用反复探），不通过时 2 分钟（好得快一点）
    _写探针缓存["时间"] = 现在
    _写探针缓存["提示"] = 提示
    _写探针缓存["窗口"] = 300.0 if not 提示 else 120.0

_项目根 = None  # 由工作进程导入前插入 sys.path；这里保持模块可独立导入


def _导入():
    # 延迟导入，确保工作进程已经把适配器项目根加入 sys.path
    from 核心.认证.认证服务 import 认证服务
    from 核心.认证.会话仓库 import 全局会话仓库
    from 核心.认证.登录服务 import (
        登录服务, 轮询最长等待秒, 状态_已扫码,
    )
    from 核心.接口.文件接口 import (
        是目录, 取文件名, 取路径, 取fs_id, 文件接口,
    )
    from 核心.接口.管理接口 import 管理接口
    from 核心.下载.下载服务 import 下载服务
    from 核心.接口.上传接口 import 上传接口
    from 核心.网络.网络客户端 import 网络客户端
    return locals()


def _时间文本(值) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(值)))
    except Exception:
        return ""


class _上下文:
    def __init__(self):
        m = _导入()
        self.认证 = m["认证服务"]()
        self.网络 = self.认证.网络
        self.会话仓库 = m["全局会话仓库"]
        self.文件 = m["文件接口"](self.网络)
        self.下载 = m["下载服务"](self.网络)
        self.上传 = m["上传接口"](self.网络)
        self.管理 = m["管理接口"](self.网络)
        self.是目录 = m["是目录"]
        self.取文件名 = m["取文件名"]
        self.取路径 = m["取路径"]

    def 条目(self, 原始: dict, 目录路径: str = "/") -> dict:
        名字 = self.取文件名(原始)
        路径 = self.取路径(原始) or 规范((目录路径.rstrip("/") or "") + "/" + 名字)
        return {
            "name": 名字,
            "path": 规范(路径),
            "is_dir": self.是目录(原始),
            "size": int(原始.get("size") or 0),
            "modified": _时间文本(原始.get("server_mtime") or 原始.get("local_mtime")),
            "id": str(原始.get("fs_id") or ""),
            "raw": 原始,
        }


class 后端(后端基类):
    def __init__(self, 项目根, 发事件=None, 日志=None):
        super().__init__(项目根, 发事件, 日志)
        self._本地 = threading.local()
        self._会话状态 = None
        # 目录解析锁（并发建目录串行化）
        self._解析锁表: dict[str, threading.RLock] = {}
        self._解析锁表锁 = threading.Lock()
        self._锁 = threading.RLock()
        # 扫码会话台账：会话句柄 → {服务, 轮询标识, sign, 有效期秒, 创建, 取消, 使用中}
        # 「扫码开始」在一个工作线程、「扫码等待」在另一个线程是常态，
        # 所以这里按句柄交给 login_qr_wait，而不是把状态放在线程局部里。
        self._扫码会话: dict[str, dict] = {}
        self._最近扫码会话 = ""

    def _ctx(self) -> _上下文:
        上下文 = getattr(self._本地, "ctx", None)
        if 上下文 is None:
            上下文 = _上下文()
            self._本地.ctx = 上下文
        return 上下文

    def _确保可写(self, ctx: _上下文) -> None:
        """写接口前确认 bdstoken 可用；拿不到时给出**可执行**的中文指引。

        ✅ 真机实测（2026-09-19，用户那份"读全通、写全废"的会话）：
             /api/list、/rest/2.0/xpan/nas?method=uinfo → errno:0（BDUSS 有效）
             /api/gettemplatevariable                    → errno:-6
             /api/loginStatus                            → errno:0 + bdstoken
             /api/create（带上面那个 bdstoken）           → errno:-6
        两条结论都必须让用户看见：
          1. bdstoken 不是取不到，是以前**只走了一条路**（模板变量接口）。
             现在回退到 /api/loginStatus，同样拿得到；
          2. 但写操作照样 -6，那就不是 bdstoken 的问题，而是**STOKEN 失效**：
             百度读接口只认 BDUSS，写接口认 BDUSS + STOKEN。
        """
        # ① 先看**实测**结论：有 bdstoken 不等于能写（扫码会话正是如此）
        状态 = self._写权限状态_带缓存()
        if 状态 == "可写":
            return
        # ② 只读/未知 → 自愈：用户点上传的那一刻就该修好，而不是等他去点登录按钮
        if 状态.startswith("只读") or 状态.startswith("未知") or not self._有bdstoken(ctx):
            if self._试从浏览器补全会话():
                if self._写权限状态_带缓存() == "可写":
                    return
        令牌 = ""
        try:
            令牌 = str(ctx.认证.从登录态补bdstoken() or "")
        except Exception:
            try:
                令牌 = str((ctx.认证.取模板变量() or {}).get("bdstoken") or "")
            except Exception as e:  # noqa: BLE001
                raise RuntimeError(
                    "百度写权限不可用：bdstoken 取不到"
                    "（/api/loginStatus 与 /api/gettemplatevariable 都失败）。"
                    "会话多半已失效，请重新登录。") from e
        if not 令牌:
            raise RuntimeError(
                "百度写权限不可用：服务端没有下发 bdstoken。请重新登录。")

    def _登录成功后的收尾(self) -> None:
        """任何一条登录路径成功后都要做的三件事。

        ⚠️ 实测踩坑（2026-09-19）：退出登录会设 10 分钟"静默期"（防止自愈把凭证
        偷偷补回来），但**重新登录成功后没解除它** —— 于是用户重新登录百度后，
        自愈被静默期挡着，界面一直显示
        "⚠️ 写操作不可用…已尝试自动从浏览器补全但失败：刚退出登录，563 秒内不自动恢复"。
        登录是用户的明确意图，成功就该：
          ① 解除退出后的静默期；
          ② 清掉写权限结论缓存（让下一次判定如实反映刚登进来的会话）；
          ③ 复位自愈的节流时间（允许立刻自愈）。
        """
        之前 = float(_退出后静默.get("到") or 0.0)
        还剩 = max(0, int(之前 - time.time()))
        _退出后静默["到"] = 0.0
        _写状态缓存.update({"指纹": "", "状态": "", "时间": 0.0})
        _最近补会话["时间"] = 0.0
        _最近补会话["失败原因"] = ""
        self.记录("[百度] 登录成功：已解除退出后的静默期"
                 + (f"（原剩 {还剩} 秒）" if 还剩 else "（当时没有静默期）"))

    def write_probe(self) -> dict:
        """重新实测写权限（含自愈）—— 界面在"刚登录完/刚重启"时调它。

        为什么需要：`account()` 里的判定**带 60 秒缓存**，而且扫码刚成功时
        自愈可能还没生效；界面若拿着那一刻的旧结论，就会一直显示
        "只能读"，直到用户手动「🔄 刷新状态」或重启程序才消失
        （用户实测反馈：重启后提示没了、上传也能用）。
        这里强制清缓存再判一次，并把结论返回给界面。
        """
        _写状态缓存.update({"指纹": "", "状态": "", "时间": 0.0})
        try:
            状态 = self._写权限状态()
        except Exception as e:  # noqa: BLE001
            return {"状态": "未知", "消息": f"写权限复测失败：{e}"}
        可写 = 状态 == "可写"
        if not 可写:
            # 再给一次自愈机会（内置浏览器 → 外部调试端口）
            _最近补会话["时间"] = 0.0
            if self._试从浏览器补全会话():
                _写状态缓存.update({"指纹": "", "状态": "", "时间": 0.0})
                状态 = self._写权限状态()
                可写 = 状态 == "可写"
        return {
            "状态": 状态,
            "可写": 可写,
            "消息": ("写操作可用" if 可写 else
                   f"这份会话只能读（{状态}）：上传/改名/删除需要 pan 域 STOKEN"),
        }

    @staticmethod
    def _写权限提示(ctx: _上下文) -> str:
        """无副作用的写权限探针：说清"读得到、写不了"是哪一环坏了。

        拿一个**非法路径**去 CREATE：合法写会话回参数/路径错误，失效会话回
        errno:-6 —— 不在网盘上留下任何东西。

        结论缓存 5 分钟（失败结论 2 分钟）：网盘页每次刷新状态都会调 account()，
        不该每次都多发一条写请求、多刷一条同样的提示。
        """
        缓存 = _写探针缓存
        现在 = time.time()
        if 缓存.get("时间") and 现在 - float(缓存["时间"]) < float(缓存["窗口"]):
            return str(缓存.get("提示") or "")
        try:
            if not ctx.会话仓库.获取stoken():
                提示 = "会话里没有 STOKEN：百度的写操作必须带 STOKEN"
                _写探针缓存写入(提示, 现在)
                return 提示
        except Exception:
            pass
        try:
            ctx.网络.请求(
                "POST", "/api/create",
                params={"path": "/V8_3_写权限探针_请忽略", "isdir": "1",
                        "block_list": "[]"},
                需要bdstoken=True,
            )
            提示 = ""
        except Exception as e:  # noqa: BLE001
            errno = getattr(e, "errno", None)
            if errno == -6:
                提示 = ("写操作被服务端拒绝（errno:-6）：会话里的 STOKEN 已失效"
                        "（读接口只认 BDUSS，写接口认 BDUSS + STOKEN）。"
                        "请在浏览器打开 pan.baidu.com 确认仍登录，"
                        "然后在登录对话框点「🌐 从浏览器导入完整会话」"
                        "（或复制整段 Cookie 用「🍪 导入Cookie」）。")
            elif errno == 2:
                # errno 2 = 文件已存在（服务端真的去建了才可能回这个）
                # → 说明写权限是通的，只是探针路径已经存在
                提示 = ""
            else:
                提示 = f"写权限探针异常：{type(e).__name__}: {str(e)[:120]}"
        _写探针缓存写入(提示, 现在)
        return 提示

    def _补种pan域会话(self) -> bool:
        """跑一遍"建立网盘会话 + 种植子域"：**把扫码得到的 STOKEN 换成 pan 域那份**。

        ✅ 真机实测（2026-09-19，诊断脚本 逆向/诊断STOKEN来源.py）：
            扫码登录后的会话： stoken=9dfe29b5…（passport 域那份）
                              取 bdstoken → errno:-6、写接口 → errno:-6（只读）
            跑完这两步之后：   stoken=a1c9d58d…（**与浏览器里那份完全一致**）
                              取 bdstoken → errno:0、写接口 → errno:2（可写！）
        ⇒ pan 域的 STOKEN 不是登录响应里下发的，而是**访问 pan 域时换发**的。
          我们只在"手工导入 Cookie"路径里跑过这两步，扫码路径漏了，
          于是扫码登录永远是"只读会话"——用户看到的就是
          "已登录、能列目录，上传/改名/删除全报 errno:-6"。
        节流 60 秒：一次没成功就别反复打。
        """
        现在 = time.time()
        # 刚退出登录的静默期内不许补种：补种会访问 pan 域并重建会话文件，
        # 把用户刚清掉的凭证又写回来（实测：退出后仍显示已登录、文件又出现）。
        if 现在 < float(_退出后静默.get("到") or 0.0):
            return False
        if 现在 - float(_最近补种.get("时间") or 0.0) < 60.0:
            return False
        _最近补种["时间"] = 现在
        try:
            import sys as _sys
            根 = str(self.项目根)
            if 根 not in _sys.path:
                _sys.path.insert(0, 根)
            服务 = self._新登录服务()
        except Exception as e:  # noqa: BLE001
            self.记录(f"[百度] 补种 pan 域会话：登录服务不可用（{e}）", "warning")
            return False
        try:
            之前 = 服务.仓库.获取stoken()
            # 优先用适配器里那条"最多 4 次、间隔 1.2 秒"的换发逻辑
            换到了 = False
            try:
                换到了 = bool(服务.建立pan域会话_换发stoken())
            except Exception as e:  # noqa: BLE001
                self.记录(f"[百度] 换发 pan 域 STOKEN 异常：{e}", "warning")
            if not 换到了:
                # 兜底：老路径（建立会话 + 种植子域）
                try:
                    服务.建立网盘会话()
                    子域 = 服务.种植子域会话()
                    self.记录("[百度] 已补种 pan 域会话"
                             f"（子域：{'、'.join(子域) if 子域 else '无'}）")
                except Exception as e:  # noqa: BLE001
                    self.记录(f"[百度] 补种 pan 域会话失败：{type(e).__name__}: {e}",
                             "warning")
            之后 = 服务.仓库.获取stoken()
            return bool(之后 and 之后 != 之前) or 换到了
        except Exception as e:  # noqa: BLE001
            self.记录(f"[百度] 补种 pan 域会话异常：{type(e).__name__}: {e}", "warning")
            return False
        finally:
            try:
                服务.关闭()
            except Exception:
                pass

    @staticmethod
    def _打一次写探针(网络, ctx) -> str:
        """用给定网络客户端打一次写探针：返回 可写 / 只读 / 未知。"""
        try:
            网络.请求("POST", "/api/create",
                    params={"path": "/V8_3_写权限探针_请忽略", "isdir": "1",
                            "block_list": "[]"},
                    需要bdstoken=True)
            return "可写"
        except Exception as e:  # noqa: BLE001
            errno = getattr(e, "errno", None)
            if errno == 2:          # 路径已存在 = 服务端真的去建了
                return "可写"
            if errno == -6:
                try:
                    有stoken = bool(ctx.会话仓库.获取stoken())
                except Exception:
                    有stoken = False
                return "只读" if 有stoken else "只读（没有 STOKEN）"
            return f"未知（errno={errno}）"

    def _写权限状态(self) -> str:
        """真打一次写接口，判断这份会话到底是"可写 / 只读 / 未登录"。

        为什么要真打：会话里**有** STOKEN 不等于那个 STOKEN 有用 ——
        扫码登录写进来的常常是 passport 域那份，读接口照样通、写接口恒 -6。
        以前只看"字段在不在"，于是界面显示"已登录"，用户一上传就失败。

        判定为"只读"时会**先自己救一次**（补种 pan 域会话，实测能把
        passport 域那份换成 pan 域那份），救不回来才如实报只读。
        """
        try:
            m = _导入()
            仓库 = m["全局会话仓库"]
            网络 = m["网络客户端"](会话提供者=仓库.取会话, 连接超时秒=15.0)
        except Exception:
            return "未知"

        class _盒子:              # _打一次写探针 只用得到 .会话仓库
            会话仓库 = 仓库

        try:
            try:
                模板 = m["认证服务"](网络=网络, 仓库=仓库).取模板变量()
                if 模板.get("bdstoken"):
                    仓库.更新bdstoken(模板["bdstoken"], 模板.get("uk"))
            except Exception as e:  # noqa: BLE001
                if getattr(e, "errno", None) != -6:
                    return f"未知（{type(e).__name__}）"
            if not 仓库.获取bdstoken():
                try:
                    m["认证服务"](网络=网络, 仓库=仓库).从登录态补bdstoken()
                except Exception:
                    pass
            结果 = self._打一次写探针(网络, _盒子)
            if 结果 == "可写":
                return 结果
            # 只读 → 自己救一次：补种 pan 域会话（换发 pan 域 STOKEN）后再打一次
            if 仓库.获取bduss() and self._补种pan域会话():
                try:
                    网络2 = m["网络客户端"](会话提供者=仓库.取会话,
                                        连接超时秒=15.0)
                except Exception:
                    网络2 = None
                if 网络2 is not None:
                    try:
                        try:
                            m["认证服务"](网络=网络2, 仓库=仓库).取模板变量()
                        except Exception:
                            pass
                        二次 = self._打一次写探针(网络2, _盒子)
                        self.记录(f"[百度] 补种 pan 域会话后复测写权限：{二次}")
                        if 二次 == "可写":
                            return "可写"
                        结果 = 二次 or 结果
                    finally:
                        try:
                            网络2.关闭()
                        except Exception:
                            pass
            return 结果
        finally:
            try:
                网络.关闭()
            except Exception:
                pass

    def _写权限状态_带缓存(self) -> str:
        """带指纹的写权限判定：同一份凭证 + 60 秒内复用结论，别每次都真打。"""
        try:
            m = _导入()
            仓库 = m["全局会话仓库"]
        except Exception:
            return "未知"
        指纹 = _会话指纹(仓库)
        现在 = time.time()
        if (_写状态缓存["指纹"] == 指纹 and _写状态缓存["状态"]
                and 现在 - float(_写状态缓存["时间"] or 0.0) < 60.0):
            return str(_写状态缓存["状态"])
        状态 = self._写权限状态()
        _写状态缓存.update({"指纹": 指纹, "状态": 状态, "时间": 现在})
        return 状态

    @staticmethod
    def _有bdstoken(ctx) -> bool:
        try:
            return bool(ctx.会话仓库.获取bdstoken())
        except Exception:
            return False

    def _试从内置浏览器补全(self) -> bool:
        """从**内置浏览器 profile** 的 cookie 库直接补一份完整会话。

        ✅ 这是最省事的一条自愈路（实测 2026-09-19）：
        用户只要用过程序里的「🌐 用内置浏览器登录」，项目内
        ``数据/内置浏览器/storage/Cookies`` 就有一份**完整网页版会话**
        （含 pan 域 STOKEN）。它不依赖调试端口、不依赖外部浏览器，
        所以"扫码登录后又变只读"这种情况能被它自动治好。
        """
        try:
            import sys as _sys
            根 = str(self.项目根)
            if 根 not in _sys.path:
                _sys.path.insert(0, 根)
            from 核心.认证.浏览器会话导入 import 会话字段
            from 界面 import 内置浏览器登录 as _内置   # 同项目下的界面模块
        except Exception:
            try:
                # 桥进程的 sys.path 只有适配器根，界面模块要按项目根找
                import sys as _sys
                from pathlib import Path as _P
                项目根 = str(_P(__file__).resolve().parents[2])
                if 项目根 not in _sys.path:
                    _sys.path.insert(0, 项目根)
                from v8_3.界面 import 内置浏览器登录 as _内置
                from 核心.认证.浏览器会话导入 import 会话字段
            except Exception as e:  # noqa: BLE001
                self.记录(f"[百度] 内置浏览器模块不可用（{type(e).__name__}）：{e}",
                         "warning")
                return False
        库 = _内置.内置浏览器目录() / "storage" / "Cookies"
        if not 库.is_file():
            return False
        饼 = _内置.读取当前离线cookie(("BDUSS", "BDUSS_BFESS", "STOKEN", "BAIDUID",
                                   "BAIDUID_BFESS"))
        if not 饼:
            return False
        字段 = 会话字段(饼)
        if not 字段.get("bduss") or not 字段.get("stoken"):
            self.记录("[百度] 内置浏览器的登录态不完整（缺 BDUSS 或 pan 域 STOKEN）",
                     "warning")
            return False
        m = _导入()
        仓库 = m["全局会话仓库"]
        try:
            for 键, 值 in 字段.items():
                if 值:
                    仓库.保存会话(**{键: 值})
            self.记录("[百度] 已从内置浏览器 profile 补全会话："
                     + "、".join(f"{k}({len(v)})" for k, v in 字段.items()))
            return True
        except Exception as e:  # noqa: BLE001
            self.记录(f"[百度] 从内置浏览器补全会话失败：{type(e).__name__}: {e}",
                     "warning")
            return False

    def _试从浏览器补全会话(self) -> bool:
        """写权限缺失时自动补一份完整会话（节流 2 分钟）。

        顺序（实测后定的）：
          ① **内置浏览器 profile**：零依赖、本机读库，最省事；
          ② 外部浏览器调试端口：需要用户开调试端口（拿不到就如实说）。
        失败就静默返回 False，界面照常给出可执行的手动指引。
        """
        现在 = time.time()
        # ⚠️ 用户刚点过「退出登录」：一段时间内**不要**自愈，
        #    否则会立刻把凭证又补回来 —— 用户看到的就是"退出登录没生效"
        #    （实测：百度退出后账号状态仍是已登录，就是自愈干的）。
        静默到 = float(_退出后静默.get("到") or 0.0)
        if 现在 < 静默到:
            _最近补会话["失败原因"] = (
                f"刚退出登录，{int(静默到 - 现在)} 秒内不自动恢复；"
                "需要重新登录请点「🔐 登录 / 管理」")
            return False
        上次 = float(_最近补会话.get("时间") or 0.0)
        if 现在 - 上次 < 120.0:
            return False
        _最近补会话["时间"] = 现在
        # ① 先用内置浏览器的登录态（用户用过程内登录就有）
        if self._试从内置浏览器补全():
            _最近补会话["失败原因"] = ""
            return True
        if not _最近补会话.get("可用"):
            # 先探一次浏览器在不在，不在就不再反复试
            try:
                import sys as _sys
                根 = str(self.项目根)
                if 根 not in _sys.path:
                    _sys.path.insert(0, 根)
                from 核心.认证.浏览器会话导入 import 找调试端口
                端口, _说明 = 找调试端口()
            except Exception:
                端口 = 0
            if not 端口:
                _最近补会话["可用"] = False
                _最近补会话["失败原因"] = (
                    "内置浏览器里还没有登录态，也没有找到开着调试端口的浏览器")
                self.记录("[百度] 本机没有可用的浏览器调试端口，"
                         "写权限需要手动导入会话", "warning")
                return False
            _最近补会话["可用"] = True
        结果 = self._从浏览器导入会话()
        if str(结果.get("状态")) == "成功":
            _最近补会话["失败原因"] = ""
            return True
        # 失败就把原因记下来，界面直接显示，用户按提示做就行
        _最近补会话["失败原因"] = str(结果.get("消息") or "从浏览器补全会话失败")[:200]
        self.记录(f"[百度] 自动从浏览器补全会话失败：{_最近补会话['失败原因']}",
                 "warning")
        return False

    # ---------------- 账号 ----------------

    def account(self) -> dict:
        m = _导入()
        会话 = m["全局会话仓库"]
        try:
            已登录 = bool(会话.是否有效())
        except Exception:
            已登录 = False
        详情 = {"stoken": bool(会话.获取stoken()), "bduss": bool(会话.获取bduss())}
        用户名 = ""
        try:
            ctx = self._ctx()
            # 刷新 bdstoken 并尽量取用户信息；失败不影响“已登录”的本地判断
            try:
                ctx.认证.取模板变量()
            except Exception as e:
                详情["refresh_error"] = str(e)[:200]
                # ✅ 回退路径：模板变量接口回 -6 的会话，loginStatus 仍会给 bdstoken
                try:
                    if ctx.认证.从登录态补bdstoken():
                        详情.pop("refresh_error", None)
                        详情["bdstoken_from"] = "loginStatus"
                except Exception:
                    pass
            try:
                u = ctx.认证.取当前用户()
                用户名 = str(u.get("uname") or "")
                详情.update({
                    "vip_type": u.get("vip_type"),
                    "uk": u.get("uk"),
                })
            except Exception as e:
                详情["user_error"] = str(e)[:200]
            # 容量（总/已用/可用）：/api/quota 一次调用就能拿全
            try:
                容量 = ctx.认证.取容量() or {}
                总 = 容量.get("total")
                已用 = 容量.get("used")
                可用 = 容量.get("free")
                try:
                    # ⚠️ 实测：百度 /api/quota 的 free 可能是 0（与 used/total 明显不符），
                    # 这时用 总-已用 现算，别给用户显示"可用 0"。
                    if (not 可用) and 总 and 已用:
                        可用 = max(0, int(总) - int(已用))
                except Exception:
                    可用 = 容量.get("free")
                详情["member"] = {
                    "total_capacity": 总,
                    "use_capacity": 已用,
                    "free_capacity": 可用,
                }
            except Exception as e:  # noqa: BLE001
                详情["容量错误"] = str(e)[:120]
            # 写权限实测（真打一次写接口）：
            #   有 bdstoken / 有 STOKEN 都**不等于**能写 —— 百度把读/写授权
            #   分开判定，实测"只读会话"（passport 域 STOKEN 或失效 STOKEN）
            #   下写接口恒返回 errno:-6。
            状态 = self._写权限状态()
            详情["写权限"] = 状态
            if 状态.startswith("只读"):
                # 自愈：用户浏览器里多半有份完整会话，自动补一次再复核
                if self._试从浏览器补全会话():
                    状态 = self._写权限状态()
                    详情["写权限"] = 状态 + "（已自动从浏览器补全会话）"
                    详情.pop("write_error", None)
            if 状态.startswith("只读"):
                原因 = str(_最近补会话.get("失败原因") or "")
                详情["write_error"] = (
                    "这份会话只能读：上传/改名/删除需要 pan 域 STOKEN。"
                    + (f"已尝试自动从浏览器补全但失败：{原因}。"
                       if 原因 else
                       ("浏览器里有完整会话，可在登录对话框点"
                        "「🌐 从浏览器导入完整会话」一键修好。"
                        if _最近补会话.get("可用") else
                        "① 在程序里点「🌐 用内置浏览器登录（推荐）」，"
                        "登录一次即可（之后扫码也能自动用它补全）；"
                        "② 或用带调试端口的浏览器登录 pan.baidu.com 后点"
                        "「🌐 从浏览器导入完整会话」（可设 "
                        "V8_3_浏览器调试端口=<端口> 指定端口）。")))
        except Exception as e:
            详情["error"] = str(e)[:200]
        return {
            "logged_in": 已登录,
            "user": 用户名,
            "data_dir": str(self.项目根 / "数据"),
            "detail": 详情,
        }

    # ---------------- 目录/文件 ----------------

    def list(self, path: str) -> list[dict]:
        ctx = self._ctx()
        路径 = 规范(path)
        结果: list[dict] = []
        页 = 1
        while 页 <= 1000:
            数据 = ctx.文件.取文件列表(dir=路径, page=页, num=1000)
            原始列表 = 数据.get("list") or []
            for 原始 in 原始列表:
                结果.append(ctx.条目(原始, 路径))
            if len(原始列表) < 1000:
                break
            页 += 1
        return 结果

    def stat(self, path: str) -> dict | None:
        ctx = self._ctx()
        路径 = 规范(path)
        if 路径 == "/":
            return {
                "name": "/", "path": "/", "is_dir": True, "size": 0,
                "modified": "", "id": "root",
            }
        数据 = ctx.文件.取文件元信息(target=路径)
        信息 = 数据.get("info") or []
        if not 信息:
            return None
        return ctx.条目(信息[0], 路径.rsplit("/", 1)[0] or "/")

    #: 服务端"暂时不可用"类错误（可重试）——与夸克/光鸭保持同一套判定
    瞬时错误关键词 = (
        "doloading", "同名冲突", "正在处理", "处理中", "稍后", "重试",
        "try again", "timeout", "超时", "timed out", "connection",
        "reset", "busy", "系统繁忙", "internal", "内部错误",
        "502", "503", "504", "429", "too many", "rate limit", "限流",
    )

    @classmethod
    def _是瞬时错误(cls, 错误) -> bool:
        文本 = str(错误 or "").lower()
        for 词 in ("未登录", "认证", "权限", "无权限", "参数", "不存在",
                  "名称不可用", "敏感", "违规"):
            if 词 in str(错误 or ""):
                return False
        return any(词.lower() in 文本 for 词 in cls.瞬时错误关键词)

    def _路径锁(self, 路径: str):
        """同一条目录路径一把锁：并发上传到未创建目录时只让一个线程去建。

        （夸克/光鸭已按此修复 5/8 失败的建目录竞态，百度侧对称补齐。）
        """
        with self._解析锁表锁:
            锁 = self._解析锁表.get(路径)
            if 锁 is None:
                锁 = threading.RLock()
                if len(self._解析锁表) > 512:
                    self._解析锁表.clear()
                self._解析锁表[路径] = 锁
            return 锁

    def _上传带重试(self, ctx, 上传源: Path, 父目录ID: str, 适配进度,
                 尝试次数: int = 4):
        """上传遇"同名冲突/正在处理/系统繁忙"等瞬时错误时退避重试。

        （夸克/光鸭已有的对称能力；百度侧补上，避免瞬时错误直接把任务判失败。）
        注意百度上传接口是 `父目录ID=` 关键字 + `允许秒传/确保目录`。
        """
        最后 = None
        for i in range(max(1, 尝试次数)):
            try:
                return ctx.上传.上传文件(
                    str(上传源),
                    父目录ID=父目录ID,
                    进度回调=适配进度,
                    允许秒传=True,
                    确保目录=True,
                )
            except Exception as e:  # noqa: BLE001
                最后 = e
                if i + 1 >= 尝试次数 or not self._是瞬时错误(e):
                    raise
                等待 = min(20.0, 2.0 * (2 ** i))
                self.记录(f"[百度] 上传遇到瞬时错误，{等待:.0f}s 后重试"
                        f"（{i + 1}/{尝试次数}）：{e}", "warning")
                time.sleep(等待)
        raise 最后 if 最后 else RuntimeError("百度上传失败")

    def ensure_dir(self, path: str) -> dict:
        ctx = self._ctx()
        路径 = 规范(path)
        if 路径 == "/":
            return {"id": "/", "path": "/"}
        self._确保可写(ctx)
        # 同一目录串行化：并发抢建目录时百度也会返回冲突类错误
        with self._路径锁(路径):
            try:
                ctx.上传.确保远端目录(路径)
            except Exception as e:  # noqa: BLE001
                if not self._是瞬时错误(e):
                    raise
                等待 = 1.0
                成功 = False
                最后 = e
                for i in range(4):
                    time.sleep(等待)
                    等待 = min(8.0, 等待 * 2)
                    try:
                        ctx.上传.确保远端目录(路径)
                        成功 = True
                        break
                    except Exception as e2:  # noqa: BLE001
                        最后 = e2
                        if not self._是瞬时错误(e2):
                            raise
                if not 成功:
                    self.记录(f"[百度] 建目录反复失败：{路径}：{最后}", "warning")
                    raise 最后
        return {"id": 路径, "path": 路径}

    # ---------------- 上传/下载 ----------------

    def upload(self, local_path: str, remote_dir: str, name: str,
               task_id: str, progress) -> dict:
        源 = Path(local_path)
        if not 源.is_file():
            raise FileNotFoundError(f"本地文件不存在：{local_path}")
        # 目标盘命名规避：百度不允许 \ / : * ? " < > | 与不可见字符，emoji 会报错
        原名称 = name or 源.name
        规范名, 改名说明 = 规范命名("baidu", 原名称)
        if 改名说明:
            self.记录(f"[百度] 命名规避：{改名说明}")
        # ⚠️ 百度上传接口只认**本地文件名**（没有"另存为"参数）——历史实现直接传了
        # 本地临时文件，于是跨盘传输时目标名会变成引擎缓存名（如 `3f9a....bin`）。
        # 这里统一用硬链接别名把"想要的远端名"落到本地文件名上。
        目标名 = 规范名 or 原名称
        if 目标名 and 目标名 != 源.name:
            别名 = 源.with_name(目标名)
            try:
                if not 别名.exists():
                    别名.hardlink_to(源)
                elif not 别名.samefile(源):
                    raise FileExistsError(f"缓存目录存在同名文件：{别名}")
            except OSError:
                import shutil as _sh
                _sh.copy2(源, 别名)
            源 = 别名
        name = 目标名
        总大小 = 源.stat().st_size
        目录 = 规范(remote_dir)
        ctx = self._ctx()
        self._确保可写(ctx)

        def 适配进度(阶段: str, 比例: float) -> None:
            if progress:
                progress(f"上传-{阶段}", int(max(0.0, min(1.0, 比例)) * 总大小), 总大小)

        # GUI 在用的薄封装：上传接口.上传文件(本地, 父目录ID/目标目录, 进度回调, ...)
        结果 = self._上传带重试(ctx, 源, 目录, 适配进度)
        信息 = 结果 if isinstance(结果, dict) else {}
        # 上传后可见性确认（baidu）
        # 列目录可能滞后 1–3 秒；不确认的话"上传完立刻跨盘传输"的源扫描会漏文件。
        请求名 = name or 源.name
        try:
            实际名 = 确认可见(lambda: self.list(目录), 请求名,
                          日志=self.记录)
        except Exception:
            实际名 = None
        落盘名 = 实际名 or 请求名
        返回 = {
            "name": 落盘名,
            "path": str(信息.get("path") or
                        规范((目录.rstrip("/") or "") + "/" + 落盘名)),
            "size": int(信息.get("size") or 总大小),
            "bytes": 总大小,
            "skipped": bool(信息.get("是否秒传")),
            "remote_id": str(信息.get("fs_id") or ""),
        }
        if 落盘名 != 请求名 and not 改名说明:
            返回["改名信息"] = {
                "原名": 请求名,
                "新名": 落盘名,
                "说明": f"服务端把文件名改成了 {落盘名}",
                "策略": "服务端改名",
                "原路径": 规范(f"{目录.rstrip('/')}/{请求名}"),
                "新路径": 规范(f"{目录.rstrip('/')}/{落盘名}"),
            }
        if 改名说明:
            返回["改名信息"] = {
                "原名": 原名称,
                "新名": 落盘名,
                "说明": 改名说明,
                "策略": "命名规避",
                "原路径": 规范(f"{remote_dir.rstrip('/')}/{原名称}"),
                "新路径": 规范(f"{remote_dir.rstrip('/')}/{落盘名}"),
            }
        # 上传后可见性确认（baidu）
        # 列目录可能滞后 1–3 秒；不确认的话"上传完立刻跨盘传输"的源扫描会漏文件。
        # 顺带拿到**服务端实际落盘名**（夸克会把引号转成 &#39; 这类实体）。
        try:
            实际名 = 确认可见(lambda: self.list(目录), name or 源.name,
                          日志=self.记录)
        except Exception:
            实际名 = None
        if 实际名:
            name = 实际名

        return 返回

    def play_link(self, remote_path: str) -> dict:
        """给播放器用的直链（V8_3 新增）：百度需要 UA/Referer，一并给出。"""
        ctx = self._ctx()
        路径 = 规范(remote_path)
        信息 = self.stat(路径)
        if 信息 is None:
            raise FileNotFoundError(f"百度路径不存在：{remote_path}")
        if 信息.get("is_dir"):
            raise IsADirectoryError(f"百度路径是目录，不能播放：{remote_path}")
        地址 = ctx.下载.取直链(路径)
        if not 地址:
            raise RuntimeError("百度没有返回可播放直链（可能需要重新登录）")
        头 = {}
        try:
            头 = dict(ctx.下载._下载请求头(0) or {})
        except Exception:
            头 = {}
        return {"url": str(地址), "headers": 头,
                "size": int(信息.get("size") or 0),
                "name": str(路径.rsplit("/", 1)[-1]),
                "range": True}

    def download(self, remote_path: str, local_path: str,
                 task_id: str, progress, 续传: bool = True,
                 保守: bool = False) -> dict:
        # 进入本次调用时本地已有多少字节（用于区分"断流续传"与"链接不通"）
        try:
            调用前已有 = Path(local_path).stat().st_size \
                if Path(local_path).exists() else 0
        except Exception:
            调用前已有 = 0
        ctx = self._ctx()
        路径 = 规范(remote_path)
        目标 = Path(local_path)
        目标.parent.mkdir(parents=True, exist_ok=True)
        信息 = self.stat(路径)
        总大小 = int(信息.get("size") or 0) if 信息 else 0

        def 适配进度(已下载: int, 总量: int) -> None:
            if progress:
                progress("download", int(已下载 or 0), int(总量 or 总大小 or 0))

        if 保守:
            self.记录("[百度] 保守模式：跳过直链，直接用适配器原生下载")
        分段, 阈值 = 读取下载分段()
        已有 = 目标.stat().st_size if 目标.exists() else 0
        if 已有 and 总大小 and 已有 >= 总大小:
            已有 = 0
        if 已有:
            self.记录(f"[百度] 断点续传：本地已有 {已有 / 1048576:.1f} MiB / "
                    f"{总大小 / 1048576:.1f} MiB")
        if not 保守 and 分段 >= 2 and 总大小 >= max(8 * 1024 * 1024, 阈值):
            try:
                直链 = ctx.下载.取直链(路径)
                头 = ctx.下载._下载请求头(0)
                if 多段下载(直链, 头, str(目标), 总大小, 分段, 阈值,
                          进度=适配进度, 日志=self.记录, 续传=续传):
                    self.记录(f"[百度] 多段下载完成：{分段} 段 / {总大小} 字节")
                    return {"path": str(目标), "size": 目标.stat().st_size,
                            "bytes": 目标.stat().st_size}
            except Exception as e:
                self.记录(f"[百度] 多段下载失败，回退单连接：{e}", "warning")
        if 续传 and not 保守:
            try:
                直链 = ctx.下载.取直链(路径)
                头 = ctx.下载._下载请求头(0)
                if 多段下载(直链, 头, str(目标), 总大小, 1, 0,
                          进度=适配进度, 日志=self.记录, 续传=续传,
                          直链续传=True):
                    return {"path": str(目标), "size": 目标.stat().st_size,
                            "bytes": 目标.stat().st_size}
            except Exception as e:
                self.记录(f"[百度] 直链续传下载失败，回退适配器下载：{e}", "warning")
        # 本地已有部分数据（进入本次调用时的大小）用于区分"断流"与"链接不通"
        本地 = Path(local_path)
        try:
            已有字节 = 本地.stat().st_size if 本地.exists() else 0
        except Exception:
            已有字节 = 0
        # 分类处理（2026-09-16 六方向矩阵实测后细化）：
        #   * 本次**搬动过字节** → 中途断流：上抛，让引擎重试按 Range 续传；
        #   * 本次**一个字节都没动** → 直链/签名 URL 不通（实测光鸭偶发
        #     `peer closed connection ... received 0 bytes`）：死守断点没意义，
        #     回退到适配器自带下载器再试一次。
        本次新增 = max(0, 已有字节 - 调用前已有)
        if 已有字节 and 总大小 and 已有字节 < 总大小 and 本次新增 and not 保守:
            raise RuntimeError(
                f"下载中断：已保留断点 {已有字节 / 1048576:.1f} MiB / "
                f"{总大小 / 1048576:.1f} MiB，重试将从断点续传")
        if 已有字节 and 总大小 and not 本次新增:
            self.记录(f"[" + "百度" + f"] 直链没取到数据，回退适配器下载器"
                    f"（原断点 {已有字节 / 1048576:.1f} MiB 会被覆盖）", "warning")
        ctx.下载.下载到文件(
            路径, str(目标), 进度回调=适配进度, 断点续传=bool(续传))
        return {"path": str(目标), "size": 目标.stat().st_size,
                "bytes": 目标.stat().st_size}

    def delete(self, path: str) -> dict:
        ctx = self._ctx()
        self._确保可写(ctx)
        路径 = 规范(path)
        return dict(ctx.管理.删除([路径]) or {})

    # ==================== 统一登录协议 ====================
    #
    # 百度适配器（核心/认证/登录服务.py）只提供两条登录链路：
    #   ① 扫码：请求二维码 → /channel/unicast 长轮询等确认 →
    #      /v3/login/main/qrbdusslogin 换 BDUSS/STOKEN → 建 pan 域会话；
    #   ② 手工导入 Cookie：粘贴浏览器里 pan.baidu.com 的 Cookie
    #      （实测最小集 BDUSS + STOKEN，推荐再加 BDUSS_BFESS）。
    # 短信 / 邮箱 / 令牌 / 账号密码 四种方式适配器没有对应端点：能力表如实报
    # 「支持=False」，对应方法返回「状态=不支持」——不谎报支持，也不抛异常给界面。
    #
    # 凭证落盘全部交给适配器自己的 会话仓库（数据/会话.json），
    # 本文件既不读也不写它的凭证文件（回滚也只用 仓库.清空/保存会话 两个 API）。

    #: 会话仓库.保存会话() 认的白名单键（Cookie 导入失败时按这些键回滚）
    _会话键 = ("bduss", "bduss_bfess", "stoken", "bdstoken", "uk",
              "baiduid", "baiduid_bfess")

    def _新登录服务(self):
        """新建一个登录服务实例：一次登录动作一个，用完即关。

        不复用全局实例的原因：扫码要「取二维码」与「等待确认」共用同一个 gid
        （适配器在 请求二维码() 里生成 gid，轮询时带同一个 gid），
        而「扫码开始」「扫码等待」很可能落在不同的工作线程上；
        按会话各持一个实例，gid 与 httpx 客户端都不会串。
        """
        return _导入()["登录服务"]()

    def auth_caps(self) -> dict:
        """六种登录方式的真实能力：百度只有「扫码」和「导入 Cookie」。"""
        return {
            "qrcode": {"支持": True,
                       "说明": "百度扫码登录（适配器 登录服务.取登录二维码 + "
                               "等待扫码 + 换取会话）：用「百度网盘」App 扫码并在"
                               "手机上确认，成功后自动写入 数据/会话.json"},
            "cookie": {"支持": True,
                       "说明": "导入浏览器 Cookie（最小集 BDUSS + STOKEN）："
                               "导入后立刻用 /api/gettemplatevariable 向服务端验证，"
                               "失败会回滚到导入前的会话"},
            "sms": {"支持": True,
                    "方式": "内置浏览器",
                    "说明": "短信登录：**在程序内置浏览器里**打开百度登录框并自动切到"
                            "「短信登录」（填手机号 → 收验证码 → 登录），登录完自动"
                            "取走完整会话。为什么不走纯 HTTP：百度现在的短信登录是"
                            "登录框里的 ARMOR 风控组件（跨域 iframe + 设备指纹），"
                            "老接口 ?getsmscode 等已 404、?regphonesend 回了 200 也"
                            "不会真发短信 —— 实测结论，详见 docs/实验_纯HTTP登录.md"},
            "email": {"支持": False,
                      "说明": "百度适配器未提供邮箱登录（可用：扫码 / 导入Cookie）"},
            "token": {"支持": False,
                      "说明": "百度适配器未提供令牌登录（百度会话形态是 "
                              "BDUSS + STOKEN Cookie，没有 access/refresh 令牌；"
                              "可用：扫码 / 导入Cookie）"},
            "password": {"支持": False,
                         "说明": "百度适配器未提供账号密码登录"
                                 "（可用：扫码 / 导入Cookie）"},
        }

    @staticmethod
    def _不支持(名称: str, 原因: str = "") -> dict:
        return {"状态": "不支持",
                "消息": f"百度适配器未提供{名称}（可用：扫码 / 导入Cookie）"
                        + (f"；{原因}" if 原因 else "")}

    def _刷新模板变量(self, 仓库) -> dict:
        """用 pan 域客户端取一次 bdstoken / uk（GET /api/gettemplatevariable）。

        这里**故意不注入会话刷新器**（适配器 认证服务 默认会注入一个）：
        刷新器的语义是「errno:-6 就补取 bdstoken 再重放一次」，
        但用无效 Cookie 时会出现 refresh → 取模板变量 → -6 → refresh …
        的连环重放（每层都是一次真实请求）。显式构造不带刷新器的客户端后，
        拿到 bdstoken 照样会由 认证服务 写进仓库，失败则立刻抛 接口错误。
        适配器自己的 完成扫码登录() 也是这样新建 pan 域客户端的。
        """
        m = _导入()
        网盘网络 = m["网络客户端"](会话提供者=仓库.取会话, 连接超时秒=20.0)
        try:
            认证 = m["认证服务"](网络=网盘网络, 仓库=仓库)
            return dict(认证.取模板变量() or {})
        finally:
            try:
                网盘网络.关闭()
            except Exception:
                pass

    # ---------------- 扫码会话台账 ----------------
    #
    # GUI 是「扫码开始」拿到「会话」句柄，再把句柄交给「扫码等待/取消」，
    # 三个调用可能落在不同工作线程上，所以台账放在后端实例上（self._锁 保护）。

    def _清理过期扫码会话(self, 保留: str = "") -> None:
        """关掉「已过期且没人在等」的会话，避免 http 客户端泄漏。

        调用方需持有 self._锁；正在等的那条不关——它的 login_qr_wait 还要用。
        """
        现在 = time.monotonic()
        for 键, 记录 in list(self._扫码会话.items()):
            if 键 == 保留 or 记录.get("使用中"):
                continue
            存活 = float(记录.get("有效期秒") or 0.0) + 60.0
            if 现在 - float(记录.get("创建") or 0.0) > 存活:
                self._关扫码会话(键)

    def _关扫码会话(self, 键: str) -> None:
        """丢弃一条扫码会话并关闭它的登录服务（调用方需持有 self._锁）。"""
        记录 = self._扫码会话.pop(键, None)
        if self._最近扫码会话 == 键:
            self._最近扫码会话 = ""
        if not 记录:
            return
        服务 = 记录.get("服务")
        if 服务 is not None:
            try:
                服务.关闭()
            except Exception:
                pass

    def _占用扫码会话(self, 会话: str = "") -> tuple[dict | None, str]:
        """原子地「取会话 + 标记使用中」，避免和「取消」抢同一个会话。

        :return: (记录, 空串) 或 (None, 失败原因)
        """
        with self._锁:
            键 = str(会话 or "").strip()
            if not 键:
                键 = self._最近扫码会话
            记录 = self._扫码会话.get(键)
            if 记录 is None:
                return None, "没有正在等待的扫码会话"
            if 记录.get("使用中"):
                return None, "该扫码会话已经在等待中"
            记录["使用中"] = True
            return 记录, ""

    # ---------------- 扫码登录 ----------------

    def login_qr_start(self) -> dict:
        """取一张百度登录二维码（适配器 登录服务.取登录二维码）。"""
        try:
            服务 = self._新登录服务()
        except Exception as e:  # noqa: BLE001
            return self.登录失败(f"百度登录服务不可用：{type(e).__name__}: {e}",
                             "请确认适配器目录完整（核心/认证/登录服务.py）后重试")
        会话 = ""
        try:
            信息 = dict(服务.取登录二维码() or {})
            sign = str(信息.get("sign") or "")
            图片 = str(信息.get("qrcode_b64") or "").strip()
            if not 图片 and sign:
                # 兜底：适配器没带回 base64 时，用 下载二维码图片(sign) 自己拉
                图片 = base64.b64encode(服务.下载二维码图片(sign)).decode("ascii")
            if not 图片:
                raise RuntimeError("适配器未返回二维码图片（qrcode_b64 为空）")
            轮询标识 = str(
                信息.get("轮询标识") or 信息.get("channel_id") or sign).strip()
            if not 轮询标识:
                raise RuntimeError("适配器未返回轮询标识（channel_id / sign 均为空）")
            有效期 = float(_导入()["轮询最长等待秒"])
            会话 = uuid.uuid4().hex[:12]
            记录 = {
                "会话": 会话,
                "服务": 服务,
                "轮询标识": 轮询标识,
                "sign": sign,
                "有效期秒": 有效期,
                "创建": time.monotonic(),
                "取消": threading.Event(),
                "使用中": False,
            }
            with self._锁:
                self._清理过期扫码会话()
                self._扫码会话[会话] = 记录
                self._最近扫码会话 = 会话
            self.记录(f"[百度] 二维码已就绪（sign {len(sign)} 位，"
                     f"轮询标识 {len(轮询标识)} 位），"
                     f"等待扫码，最长 {有效期:.0f} 秒")
            return {
                "状态": "成功",
                "类型": "图片",
                "图片base64": 图片,
                "会话": 会话,
                "有效期秒": 有效期,
                "提示": "请用「百度网盘」App 扫码，并在手机上点「确认登录」"
                        "（本对话框会自动等待）",
            }
        except Exception as e:  # noqa: BLE001
            if 会话:
                # 台账已经登记过：连登录服务一起收掉，别留一条指向已关闭客户端的死会话
                with self._锁:
                    self._关扫码会话(会话)
            else:
                try:
                    服务.关闭()
                except Exception:
                    pass
            self.记录(f"[百度] 获取二维码失败：{type(e).__name__}: {e}", "warning")
            return self.登录失败(
                f"获取二维码失败：{type(e).__name__}: {e}",
                "请检查网络后重试；也可以改用「导入 Cookie」方式登录")

    def login_qr_wait(self, 会话: str = "", 超时秒: float = 180.0) -> dict:
        """阻塞等待扫码结果，成功后自动换取 BDUSS/STOKEN 并补齐 bdstoken。

        轮询用适配器自己的 `登录服务.等待扫码()`（内部 1.5 秒间隔 +
        60 秒长轮询，长轮询读超时算「还没扫码」而不是失败）；
        为了能在日志里持续打进度，这里把它按小片调用，
        每片之间重算剩余时间，并检查「取消」标记。
        """
        记录, 原因 = self._占用扫码会话(会话)
        if 记录 is None:
            return self.登录失败(原因, "请先点「开始扫码」获取二维码，再点「等待扫码」")
        服务 = 记录["服务"]
        轮询标识 = str(记录.get("轮询标识") or "")
        上限 = max(1.0, float(超时秒 or 180.0))
        开始 = time.monotonic()
        try:
            m = _导入()
            状态已扫码 = m["状态_已扫码"]

            def 状态回调(状态) -> None:
                # 适配器用「有没有拿到令牌」判确认，状态位只用于文案；
                # 实测手机上点确认时 status 会从 1 退回 0，所以这里只区分两句话。
                if 状态 == 状态已扫码:
                    self.记录("[百度] 已扫码，请在手机上点「确认登录」…")
                else:
                    self.记录(f"[百度] 扫码状态更新：{状态}")

            令牌 = ""
            连续错误 = 0
            while True:
                if 记录["取消"].is_set():
                    self.记录("[百度] 扫码等待已被取消")
                    return {"状态": "已取消", "消息": "已取消扫码登录"}
                已等 = time.monotonic() - 开始
                if 已等 >= 上限:
                    self.记录(f"[百度] 扫码登录超时（{上限:.0f} 秒未确认）", "warning")
                    return self.登录失败(
                        f"扫码登录超时：{上限:.0f} 秒内没有在手机上确认",
                        "请重新点「开始扫码」换一张二维码；"
                        "也可以改用「导入 Cookie」方式登录")
                self.记录(f"[百度] 等待扫码确认…（已等待 {已等:.0f} 秒，"
                         f"上限 {上限:.0f} 秒）")
                try:
                    令牌 = 服务.等待扫码(
                        轮询标识,
                        状态回调=状态回调,
                        超时秒=min(20.0, max(1.0, 上限 - 已等)))
                    break
                except TimeoutError:
                    连续错误 = 0
                    continue  # 这一片没等到，回循环重算剩余时间
                except Exception as e:  # noqa: BLE001
                    # 单次轮询的网络/接口错误不该毁掉整次扫码：记一条、等 2 秒再试。
                    # 但连续错 3 次就认为不是偶发（连着报错等下去也没意义），抛出去。
                    连续错误 += 1
                    剩余 = 上限 - (time.monotonic() - 开始)
                    if 连续错误 >= 3 or 剩余 <= 0:
                        raise
                    self.记录(f"[百度] 扫码轮询出错（第 {连续错误} 次），"
                             f"继续等待：{type(e).__name__}: {e}", "warning")
                    time.sleep(2.0)
                    continue
            if not 令牌:
                return self.登录失败("适配器没有返回登录令牌（扫码结果异常）",
                                 "请重新点「开始扫码」再试一次")

            self.记录("[百度] 已确认，正在用登录令牌换取 BDUSS / STOKEN …")
            服务.换取会话(令牌)          # 失败会抛 接口错误
            # ⚠️ 实测（2026-09-19）：扫码换取到的只是 **passport 域** STOKEN，
            #    写接口只认 **pan 域**那份，而 pan 域那份是"访问 pan 域时换发"的，
            #    且不一定一次就中 —— 所以走适配器里那条"重试 3 次"的换发逻辑。
            try:
                换到了 = 服务.建立pan域会话_换发stoken()
                self.记录("[百度] pan 域 STOKEN 换发：" + ("成功" if 换到了 else "未变化"))
            except Exception as e:  # noqa: BLE001
                self.记录(f"[百度] pan 域 STOKEN 换发异常（不影响登录）：{e}", "warning")
            服务.建立网盘会话()          # 建 pan 域会话（内部失败只记日志）
            try:
                # 同适配器 完成扫码登录()：种植 pcs / pcsdata 子域，上传通道才不带 401；
                # 它是尽力而为（失败只记日志），所以绝不能影响登录结论。
                子域 = 服务.种植子域会话()
                self.记录(f"[百度] 子域会话种植：{'、'.join(子域) if 子域 else '无'}"
                         f"（失败不影响登录）")
            except Exception as e:  # noqa: BLE001
                self.记录(f"[百度] 子域会话种植失败（不影响登录）："
                         f"{type(e).__name__}: {e}", "warning")
            提示 = "会话已写入适配器 数据/会话.json，可以关闭本对话框"
            暂定问题: list[str] = []      # 先记下"暂时不行"的结论，后面能补回来就删掉
            try:
                模板 = self._刷新模板变量(m["全局会话仓库"])   # 补齐 bdstoken / uk
                if 模板.get("bdstoken"):
                    self.记录("[百度] 已补齐 bdstoken / uk，写操作可用")
                else:
                    暂定问题.append("服务端没有下发 bdstoken")
                    self.记录("[百度] 服务端未下发 bdstoken（稍后复测）", "warning")
            except Exception as e:  # noqa: BLE001
                暂定问题.append(f"bdstoken 未取到（{e}）")
                self.记录(f"[百度] 补齐 bdstoken 失败（稍后复测）："
                         f"{type(e).__name__}: {e}", "warning")
            self._登录成功后的收尾()      # 解除退出后的静默期、清写权限缓存
            # 扫码得到的会话**可能只能读**（实测：它的 BDUSS 进不了网页版 pan 域，
            # 服务端因此不下发 pan 域 STOKEN → 上传/改名/删除恒 errno:-6）。
            # 这里当场验一次写权限，只读就立刻自愈（补种 pan 域 → 浏览器会话），
            # 并把结论如实写进提示 —— 用户不必自己去猜"为什么上传失败"。
            try:
                _写状态缓存.update({"指纹": "", "状态": "", "时间": 0.0})
                状态 = self._写权限状态()
                if 状态 == "可写":
                    # 写权限是**真打接口**验的，之前那些"bdstoken 可能没取到"的
                    # 暂定结论已被推翻，不能再留在提示里（用户实测见过自相矛盾的
                    # 提示："但 bdstoken 未取到…；写操作已验证可用"）。
                    暂定问题.clear()
                    提示 += "；写操作（上传/改名/删除）已验证可用"
                else:
                    if self._试从浏览器补全会话():
                        _写状态缓存.update({"指纹": "", "状态": "", "时间": 0.0})
                        状态 = self._写权限状态()
                    if 状态 == "可写":
                        暂定问题.clear()
                        提示 += "；写操作已验证可用（已从浏览器补全会话）"
                    else:
                        原因 = str(_最近补会话.get("失败原因") or "")
                        提示 += ("；⚠️ 这份扫码会话只能读（服务端没给 pan 域 STOKEN），"
                               "上传/改名/删除会被拒绝。"
                               + (f"自动补全失败：{原因}。" if 原因 else "")
                               + "请在带调试端口的浏览器里登录 pan.baidu.com 后，"
                                 "点「🌐 从浏览器导入完整会话」。")
                    self.记录(f"[百度] 扫码登录后的写权限：{状态}", "warning")
            except Exception as e:  # noqa: BLE001
                self.记录(f"[百度] 扫码登录后写权限检查失败（不影响登录）：{e}", "warning")
            if 暂定问题:
                提示 += "；⚠️ " + "；".join(暂定问题) + "（写操作可能不可用）"
            self.记录("[百度] 扫码登录成功"
                     + (f"（遗留：{'；'.join(暂定问题)}）" if 暂定问题 else ""))
            return self.登录成功("百度扫码登录成功", 提示)
        except Exception as e:  # noqa: BLE001
            self.记录(f"[百度] 扫码登录失败：{type(e).__name__}: {e}", "warning")
            return self.登录失败(
                f"扫码登录失败：{type(e).__name__}: {e}",
                "若二维码已过期，请重新点「开始扫码」")
        finally:
            with self._锁:
                记录["使用中"] = False
                self._关扫码会话(str(记录.get("会话") or ""))

    def login_qr_cancel(self, 会话: str = "") -> dict:
        """请求取消等待（等待中的长轮询最多还要 ~60 秒才返回）。"""
        使用中 = False
        with self._锁:
            键 = str(会话 or "").strip()
            if not 键:
                键 = self._最近扫码会话
            记录 = self._扫码会话.get(键)
            if 记录 is None:
                return {"状态": "已取消", "消息": "已取消扫码登录"}
            try:
                记录["取消"].set()
            except Exception:
                pass
            使用中 = bool(记录.get("使用中"))
            if not 使用中:
                # 没人在等：直接把这条会话收掉；有人等的话由 login_qr_wait 收尾
                self._关扫码会话(键)
        if not 使用中:
            self.记录("[百度] 已取消扫码登录")
            return {"状态": "已取消", "消息": "已取消扫码登录"}
        self.记录("[百度] 已请求取消扫码登录（等待中的轮询返回后结束）")
        return {"状态": "已取消", "消息": "已取消扫码登录",
                "提示": "等待中的轮询会在下一次返回后结束（最长约 60 秒）"}

    # ---------------- 导入 Cookie ----------------

    def _从内置浏览器导入(self, 凭证) -> dict:
        """把内置浏览器窗口捕获的 cookie 落成会话，并验证读 + bdstoken + 写。

        与"从外部浏览器调试端口取"的区别：这条路是程序内嵌 Chromium
        自己登录来的，**不依赖外部浏览器、不依赖调试端口**，
        而且走的就是百度网页版登录 —— pan 域 STOKEN 一定在。
        """
        try:
            import sys as _sys
            根 = str(self.项目根)
            if 根 not in _sys.path:
                _sys.path.insert(0, 根)
            from 核心.认证.浏览器会话导入 import 会话字段
        except Exception as e:  # noqa: BLE001
            return self.登录失败(f"会话字段模块不可用：{e}")
        饼 = []
        for c in (凭证 or []):
            if isinstance(c, dict):
                饼.append(c)
        名字表 = sorted({str(c.get("name") or "") for c in 饼})
        域名表 = sorted({str(c.get("domain") or "") for c in 饼})
        self.记录(f"[百度] 内置浏览器带来 {len(饼)} 条 cookie；"
                 f"名字：{'、'.join(名字表[:20])}；域名：{'、'.join(域名表[:8])}")
        字段 = 会话字段(饼)
        if not 字段.get("bduss"):
            有stoken = any(str(c.get("name")) == "STOKEN" for c in 饼)
            return self.登录失败(
                "内置浏览器里还没拿到 BDUSS（可能没登录成功）",
                f"共收到 {len(饼)} 条 cookie"
                + (f"（有 STOKEN 但没有 BDUSS）" if 有stoken else "")
                + f"；名字：{'、'.join(名字表[:12]) or '（空）'}"
                + "。请在弹出的窗口里登录（扫码/短信都行），"
                  "确认能看到文件列表后再点「✅ 我已登录完成」")
        m = _导入()
        仓库 = m["全局会话仓库"]
        旧会话 = dict(仓库.取会话() or {})
        成功 = False
        try:
            # ⚠️ 不要 清空() 再写：用户可能只是补一个 STOKEN 进来，
            #    抹掉别的字段反而更糟。这里按字段合并（保存会话 内部会先重载）。
            for 键, 值 in 字段.items():
                if 值:
                    仓库.保存会话(**{键: 值})
            self.记录("[百度] 已写入内置浏览器会话："
                     + "、".join(f"{k}({len(v)})" for k, v in 字段.items()))
            模板 = self._刷新模板变量(仓库)
            if not (模板.get("bdstoken") or 仓库.获取bdstoken()):
                return self.登录失败("内置浏览器会话未被服务端接受（没下发 bdstoken）",
                                 "请在窗口里确认已经进入网盘首页（能看到文件列表）")
            # ⚠️ 只要 BDUSS + bdstoken 都拿到了，**先按成功处理**（会话确实写进去了，
            #    用户也真的登录了）；写权限单独报，能写就说能写，不能写就说清原因。
            成功 = True
            self._登录成功后的收尾()
            状态 = self._写权限状态()
            if 状态 == "可写":
                self.记录("[百度] ✅ 内置浏览器登录成功，写权限已验证可用")
                return self.登录成功(
                    "内置浏览器登录成功（含 pan 域 STOKEN）",
                    "写操作（上传/改名/删除）已验证可用，可以关闭登录窗口了")
            # 读得通但写不了：如实说，并给下一步
            self.记录(f"[百度] 内置浏览器会话写权限：{状态}", "warning")
            return self.登录成功(
                "内置浏览器登录成功（含 pan 域 STOKEN）",
                f"会话已写入并可读；写权限复测结果：{状态}。"
                "若上传/改名/删除被拒，请在窗口里打开 pan.baidu.com 首页"
                "（能看到文件列表）后点「✅ 我已登录完成」再试一次")
        except Exception as e:  # noqa: BLE001
            self.记录(f"[百度] 内置浏览器导入失败：{type(e).__name__}: {e}", "warning")
            return self.登录失败(f"内置浏览器导入失败：{type(e).__name__}: {e}")
        finally:
            if not 成功:
                self._还原会话(仓库, 旧会话)

    def _从浏览器导入会话(self) -> dict:
        """从本机浏览器（调试端口）取回完整会话，落盘并**验证写权限**。

        与手工粘贴 Cookie 的区别：这里由浏览器自己解密 cookie，
        因此能拿到 HttpOnly 的 ``BDUSS`` 与 **pan 域 STOKEN** ——
        后者正是写操作（上传/改名/删除/建目录）唯一认的那份凭证。
        """
        try:
            # ⚠️ 必须先把适配器项目根放进 sys.path：桥进程的 cwd 是 v8_3/桥，
            #    `核心` 包只在适配器目录下，直接 import 会 ModuleNotFoundError。
            import sys as _sys
            根 = str(self.项目根)
            if 根 not in _sys.path:
                _sys.path.insert(0, 根)
            from 核心.认证.浏览器会话导入 import 取浏览器会话
        except Exception as e:  # noqa: BLE001
            return self.登录失败(f"浏览器会话导入模块不可用：{e}")
        self.记录("[百度] 正在从本机浏览器读取登录会话（调试端口）…")
        结果 = 取浏览器会话()
        if not 结果.get("成功"):
            return self.登录失败(str(结果.get("消息") or "取浏览器会话失败"),
                             "浏览器需要：① 用带 --remote-debugging-port 的方式启动"
                             "（本项目浏览器面板默认已开）；② 里面已登录 pan.baidu.com")
        会话 = dict(结果.get("会话") or {})
        m = _导入()
        仓库 = m["全局会话仓库"]
        旧会话 = dict(仓库.取会话() or {})
        成功 = False
        try:
            仓库.清空()
            for 键, 值 in 会话.items():
                if 值:
                    仓库.保存会话(**{键: 值})
            if not 仓库.是否有效():
                return self.登录失败("从浏览器取到的会话字段不完整",
                                 f"只拿到 {'、'.join(会话) or '空'}")
            self.记录(f"[百度] 已取到浏览器会话：{'、'.join(会话)}"
                     f"（{结果.get('浏览器')}）")
            # 服务端验证：读接口 + bdstoken
            模板 = self._刷新模板变量(仓库)
            if not (模板.get("bdstoken") or 仓库.获取bdstoken()):
                return self.登录失败("浏览器会话未被服务端接受（没下发 bdstoken）",
                                 "请在浏览器里打开 pan.baidu.com 再试一次")
            # 写权限探针：统一走**带自愈**的那条（_写权限状态），
            # 免得"某个登录路径探出只读、随后被缓存 60 秒"，界面就一直显示不可写。
            成功 = True
            self._登录成功后的收尾()
            写提示 = ""
            try:
                状态 = self._写权限状态()
                if 状态 != "可写":
                    写提示 = f"写权限复测：{状态}"
            except Exception:
                写提示 = ""
            self.记录("[百度] ✅ 浏览器会话导入成功"
                     + ("（写权限探针通过）" if not 写提示 else f"（写探针：{写提示[:60]}）"))
            return self.登录成功(
                "已从浏览器导入完整会话（含 pan 域 STOKEN）",
                ("写操作（上传/改名/删除）现在应该可以用了；"
                 "若仍报 errno:-6，说明浏览器里那份会话也过期了，"
                 "请在浏览器里重新登录一次再点本按钮"
                 + (f"。写探针提示：{写提示}" if 写提示 else "")))
        finally:
            if not 成功:
                self._还原会话(仓库, 旧会话)

    def login_cookie(self, 文本: str) -> dict:
        """导入浏览器 Cookie（适配器 登录服务.手工导入cookie）。

        适配器只做「解析 + 落盘」，不校验凭证真伪（会话仓库.是否有效()
        只看字段在不在），所以这里补两件事：

        1. 导入前先 `仓库.清空()`：`从cookie文本导入` 只覆盖文本里出现的字段，
           不清空的话上一账号的 BDUSS_BFESS / BAIDUID 会留下来，
           而 `网络客户端._构造cookie头()` 会把 BDUSS 和 BDUSS_BFESS 一起发出去
           ——两个身份混在一个 Cookie 头里，服务端认谁不好说；
        2. 导入后真的向服务端问一次 `认证服务.取模板变量()`
           （GET /api/gettemplatevariable）：拿得到 bdstoken 才算登录成功。
           任一步失败都说明这次 Cookie 没生效，此时把会话整体回滚到导入前的样子，
           免得把原本能用的会话弄坏。
        """
        文本 = str(文本 or "").strip()
        # 特殊指令：从本机浏览器的调试端口取**完整**会话（含 HttpOnly 的
        # pan 域 STOKEN）。这是"扫码登录后写操作仍报 errno:-6"的正解 ——
        # 详见 适配器/核心/认证/浏览器会话导入.py 的模块注释。
        if 文本 in ("__取浏览器会话__", "{\"__取浏览器会话__\": true}",
                  '{"__取浏览器会话__": true}'):
            return self._从浏览器导入会话()
        # 内置浏览器（方案 B）：界面把 cookie 列表原样送过来
        if 文本.startswith("{") and '"\u5185\u7f6e\u6d4f\u89c8\u5668" in 文本' or \
                文本.startswith("{") and '"内置浏览器"' in 文本:
            try:
                载荷 = json.loads(文本)
            except Exception as e:  # noqa: BLE001
                return self.登录失败(f"内置浏览器凭证解析失败：{e}")
            凭证 = 载荷.get("凭证") or 载荷.get("cookies") or []
            return self._从内置浏览器导入(凭证)
        if not 文本:
            return self.登录失败(
                "Cookie 文本为空",
                "请在浏览器里复制 pan.baidu.com 请求的 Cookie 头"
                "（至少含 BDUSS 与 STOKEN）")
        服务 = None
        仓库 = None
        旧会话: dict = {}
        成功 = False
        try:
            m = _导入()
            仓库 = m["全局会话仓库"]
            旧会话 = dict(仓库.取会话() or {})   # 导入前快照，失败要回滚
            服务 = self._新登录服务()
            仓库.清空()                          # 换账号时不留下旧账号的字段
            命中 = 服务.手工导入cookie(文本)     # 认不出字段会抛 ValueError
            if not 仓库.是否有效():
                return self.登录失败(
                    f"只识别到 {'、'.join(命中) or '空'}，会话仍不完整",
                    "百度至少要 BDUSS + STOKEN 两个字段，"
                    "请在浏览器里复制完整的 Cookie 头（含 Cookie: 前缀也行）")
            self.记录(f"[百度] Cookie 已导入：{'、'.join(命中)}；"
                     f"正在向服务端验证…")
            try:
                模板 = self._刷新模板变量(仓库)
            except Exception as e:  # noqa: BLE001
                return self.登录失败(
                    f"Cookie 未被服务端接受：{type(e).__name__}: {e}",
                    "请确认 Cookie 来自已登录的 pan.baidu.com 且含 BDUSS 与 STOKEN；"
                    "网络不通也会走到这里，可稍后重试")
            if not (模板.get("bdstoken") or 仓库.获取bdstoken()):
                return self.登录失败(
                    "服务端没有下发 bdstoken，Cookie 可能已失效",
                    "请重新从已登录的 pan.baidu.com 页面复制 Cookie"
                    "（至少含 BDUSS 与 STOKEN）")
            服务.建立网盘会话()
            try:
                # 与扫码路径一致：种植 pcs / pcsdata 子域，上传通道才不带 401
                子域 = 服务.种植子域会话()
                self.记录(f"[百度] 子域会话种植：{'、'.join(子域) if 子域 else '无'}"
                         f"（失败不影响登录）")
            except Exception as e:  # noqa: BLE001
                self.记录(f"[百度] 子域会话种植失败（不影响登录）："
                         f"{type(e).__name__}: {e}", "warning")
            成功 = True
            self.记录("[百度] Cookie 登录成功，bdstoken / uk 已就绪")
            return self.登录成功(
                "百度 Cookie 登录成功",
                f"已从 Cookie 写入会话（{'、'.join(命中)}），可以关闭本对话框")
        except Exception as e:  # noqa: BLE001
            self.记录(f"[百度] Cookie 登录失败：{type(e).__name__}: {e}", "warning")
            return self.登录失败(
                f"Cookie 登录失败：{type(e).__name__}: {e}",
                "请检查粘贴内容是否为 pan.baidu.com 的 Cookie"
                "（至少含 BDUSS 与 STOKEN）")
        finally:
            if not 成功 and 仓库 is not None:
                self._还原会话(仓库, 旧会话)
            if 服务 is not None:
                try:
                    服务.关闭()
                except Exception:
                    pass

    def _还原会话(self, 仓库, 旧会话: dict) -> None:
        """把会话恢复成导入前的样子（只走仓库自己的 API，不碰它的文件）。"""
        try:
            可存 = {k: (旧会话 or {}).get(k) for k in self._会话键
                   if (旧会话 or {}).get(k) is not None}
            仓库.清空()
            if 可存:
                仓库.保存会话(**可存)
            self.记录("[百度] 本次 Cookie 未生效，已把会话回滚到导入前的状态")
        except Exception as e:  # noqa: BLE001
            self.记录(f"[百度] 回滚会话失败：{type(e).__name__}: {e}", "warning")

    # ---------------- 适配器确实没有的方式 ----------------
    #
    # 界面按 auth_caps 已经把这几页置灰；这里再给直接调用（脚本/API）一个
    # 明确答复，免得落到基类那句「请用扫码/Cookie/短信登录」（百度没有短信）。

    def login_sms_send(self, 手机号: str) -> dict:
        return self._不支持("短信登录")

    def login_sms_verify(self, 会话: str, 验证码: str,
                         手机号: str = "") -> dict:
        return self._不支持("短信登录")

    def login_token(self, 访问令牌: str = "", 刷新令牌: str = "",
                    额外: dict | None = None) -> dict:
        return self._不支持(
            "令牌登录",
            "百度会话形态是 BDUSS + STOKEN Cookie，没有 access/refresh 令牌")

    def login_password(self, 账号: str = "", 密码: str = "",
                       额外: dict | None = None) -> dict:
        return self._不支持("账号密码登录")

    # ---------------- 退出登录 ----------------

    def 退出登录(self) -> dict:
        """清掉本机保存的百度会话（数据/会话.json）。只动本地文件。"""
        m = _导入()
        清除: list[str] = []
        # 退出后进入静默期：自愈不许把凭证补回来（用户明确要退出）
        # ⚠️ 顺序很重要：静默期必须在**任何网络调用之前**设好。
        #    以前的顺序是"清空凭证 → 重新登录 → 自愈"，中间那次 loginStatus
        #    会触发自愈，把凭证又写回来 —— 用户看到"退出登录后仍是已登录"。
        _退出后静默["到"] = time.time() + 退出后静默秒
        _写状态缓存.update({"指纹": "", "状态": "", "时间": 0.0})
        _最近补会话["时间"] = 0.0
        self.记录(f"[百度] 已进入退出后的静默期（{int(退出后静默秒)} 秒内不自动恢复登录态）")
        try:
            仓库 = m["全局会话仓库"]
            路径 = getattr(仓库, "文件路径", None)
            仓库.清空()
            if 路径 is not None:
                清除.append(str(路径))
        except Exception as e:  # noqa: BLE001
            return {"状态": "失败", "消息": f"清空百度会话失败：{e}",
                    "提示": "可在适配器原 GUI 里退出登录"}
        try:
            for 名字 in ("会话.json", "令牌.json", "凭证.json"):
                文件 = self.项目根 / "数据" / 名字
                if 文件.is_file():
                    文件.unlink()
                    清除.append(str(文件))
        except Exception as e:  # noqa: BLE001
            self.记录(f"[百度] 清理残留凭证文件失败：{e}", "warning")
        try:
            self._本地.ctx = None      # 作废缓存的网络上下文（旧 Cookie 不再复用）
        except Exception:
            pass
        self.记录("[百度] 已退出登录（本地会话已清除）")
        信息 = {}
        try:
            信息 = self.account()
        except Exception:
            pass
        return {
            "状态": "成功",
            "消息": "已退出登录，本地会话已清除",
            "清除": 清除,
            "账号": 信息,
        }

    def login_email(self, 邮箱: str = "", 密码: str = "",
                    额外: dict | None = None) -> dict:
        return self._不支持("邮箱登录")

    # ---------------- 收尾 ----------------

    def close(self) -> None:
        # 先收掉所有还挂着的扫码会话（关掉它们的登录服务与 httpx 客户端）
        with self._锁:
            for 键 in list(self._扫码会话):
                self._关扫码会话(键)
        for 属性 in ("ctx",):
            ctx = getattr(self._本地, 属性, None)
            if ctx is not None:
                try:
                    ctx.网络.关闭()
                except Exception:
                    pass
