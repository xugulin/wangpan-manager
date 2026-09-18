"""光鸭云盘后端：桥接工作区里的 光鸭云盘适配器/核心。"""

from __future__ import annotations

import threading
import time
import uuid
from pathlib import Path

from 后端_基类 import (后端基类, 规范, 多段下载, 读取下载分段,
                    续传多段下载, 续传单连接下载, 清理续传文件,
                    规范命名,
                    确认可见)


def _导入():
    from 核心.认证.令牌仓库 import 全局令牌仓库
    from 核心.认证.认证服务 import 认证服务
    from 核心.认证.登录服务 import 登录服务
    from 核心.接口.文件接口 import 文件接口
    from 核心.接口.上传接口 import 上传接口
    from 核心.下载.下载服务 import 下载服务
    from 核心.网络.网络客户端 import 网络客户端
    return locals()


def _是否目录(条目: dict) -> bool:
    """光鸭实测：目录 resType=2；文件 resType=1、fileType=4。

    ⚠️ dirType 不能用来判目录 —— 目录和文件都可能是 1。
    """
    值 = 条目.get("resType")
    if 值 is not None:
        try:
            return int(值) == 2
        except Exception:
            pass
    # 兜底：目录通常没有 fileType/fileSize/ext
    if 条目.get("fileType") in (None, "") and 条目.get("fileSize") in (None, ""):
        return bool(条目.get("dirType"))
    return False


def _时间文本(值) -> str:
    try:
        数字 = float(值)
        if 数字 > 10_000_000_000:  # 毫秒
            数字 /= 1000.0
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(数字))
    except Exception:
        return ""


class _上下文:
    def __init__(self):
        m = _导入()
        self.令牌仓库 = m["全局令牌仓库"]
        令牌 = self.令牌仓库.获取访问令牌()
        if not 令牌:
            raise PermissionError("光鸭未登录：没有有效访问令牌")
        self.网络 = m["网络客户端"]()
        self.网络.设置访问令牌(令牌)
        self.认证 = m["认证服务"](self.网络)
        self.文件 = m["文件接口"](self.网络)
        self.上传 = m["上传接口"](self.网络)
        self.下载 = m["下载服务"](self.网络)

    def 条目(self, 原始: dict, 目录路径: str) -> dict:
        名字 = str(原始.get("fileName") or "")
        return {
            "name": 名字,
            "path": 规范((目录路径.rstrip("/") or "") + "/" + 名字),
            "is_dir": _是否目录(原始),
            "size": int(原始.get("fileSize") or 0),
            "modified": _时间文本(原始.get("utime") or 原始.get("ctime")),
            "id": str(原始.get("fileId") or ""),
            "raw": 原始,
        }


class 后端(后端基类):
    def __init__(self, 项目根, 发事件=None, 日志=None):
        super().__init__(项目根, 发事件, 日志)
        self._本地 = threading.local()
        self._解析锁表: dict[str, threading.RLock] = {}
        self._解析锁表锁 = threading.Lock()
        self._列缓存: dict[str, tuple[float, list[dict]]] = {}
        self._列缓存锁 = threading.RLock()
        self._目录缓存: dict[str, str] = {"": "", "/": ""}
        self._缓存锁 = threading.RLock()
        # 统一登录：会话表 + 令牌版本
        # （令牌版本一变，各线程缓存的网络上下文会在下次调用时自动重建，
        #   否则登录成功后 account() 还可能拿着旧令牌/旧账号。）
        self._登录锁 = threading.RLock()
        self._扫码会话: dict[str, dict] = {}
        self._短信会话: dict[str, dict] = {}
        self._令牌版本 = 0

    def _ctx(self) -> _上下文:
        现在 = _导入()["全局令牌仓库"].获取访问令牌忽略过期()
        上下文 = getattr(self._本地, "ctx", None)
        if (上下文 is not None and 现在
                and getattr(self._本地, "版本", -1) == self._令牌版本
                and 上下文.网络.访问令牌 == 现在):
            return 上下文
        # 令牌变了（重新登录 / 适配器静默续期 / 被清空）就作废重建，
        # 否则网络上下文里的 Authorization 还是构造时的旧令牌快照。
        if 上下文 is not None:
            try:
                上下文.网络.关闭()
            except Exception:
                pass
            self._本地.ctx = None
        上下文 = _上下文()  # 未登录会抛 PermissionError，不缓存
        self._本地.ctx = 上下文
        self._本地.版本 = self._令牌版本
        return 上下文

    # ---------------- 账号 ----------------

    def account(self) -> dict:
        m = _导入()
        仓库 = m["全局令牌仓库"]
        令牌 = 仓库.获取访问令牌()
        用户名 = ""
        详情 = {}
        try:
            # 登录凭证文件在不在：界面靠这个把"你在原 GUI 里退出了"同步过来
            文件 = getattr(仓库, "文件路径", None)
            详情["no_credential"] = not (文件 is not None and 文件.is_file())
        except Exception:
            pass
        try:
            ctx = self._ctx()
            用户 = ctx.认证.取当前用户(令牌)
            详情 = {**详情, **dict(用户 or {})}
            用户名 = str(详情.get("name") or 详情.get("nickname") or "")
        except Exception as e:
            详情["error"] = str(e)[:200]
        return {
            "logged_in": bool(令牌),
            "user": 用户名,
            "data_dir": str(self.项目根 / "数据"),
            "detail": 详情,
        }

    # ==================== 统一登录（扫码 / 短信 / 令牌） ====================
    #
    # 光鸭是**令牌型**网盘，适配器只有三条真路径（全部走它自己的模块落盘）：
    #   * 扫码：设备授权码流程 请求设备授权码() → 轮询令牌() → 写入令牌仓库
    #   * 短信：短信登录_初始化盾() → 短信登录_发送验证码()
    #           → 短信登录_校验验证码() → 短信登录_登录()
    #   * 令牌：把用户给的 access/refresh 写进 全局令牌仓库
    # Cookie / 邮箱 / 账号密码适配器没有 → 一律明确回"不支持"，不谎报能力。

    #: 光鸭适配器不提供的登录方式（界面据此禁用对应页签）
    不支持说明 = {
        "cookie": "光鸭适配器未提供 Cookie 登录（可用：扫码 / 短信 / 令牌）",
        "email": "光鸭适配器未提供邮箱登录（可用：扫码 / 短信 / 令牌）",
        "password": "光鸭适配器未提供账号密码登录（可用：扫码 / 短信 / 令牌）",
    }

    def auth_caps(self) -> dict:
        return {
            "qrcode": {"支持": True,
                       "说明": "扫码登录（设备授权码：打开验证地址并输入用户码）"},
            "cookie": {"支持": False, "说明": self.不支持说明["cookie"]},
            "sms": {"支持": True,
                    "说明": "短信登录（手机号需带国家码，如 +86 13800000000）"},
            "email": {"支持": False, "说明": self.不支持说明["email"]},
            "token": {"支持": True,
                      "说明": "令牌登录（粘贴 access_token，可带 refresh_token）"},
            "password": {"支持": False, "说明": self.不支持说明["password"]},
        }

    # ---------------- 登录小工具 ----------------

    @staticmethod
    def _新建登录服务():
        """新建一个光鸭登录服务（调用方负责 关闭()）。"""
        return _导入()["登录服务"]()

    def _失效上下文(self) -> None:
        """令牌变了：版本 +1、各线程上下文重建，并清掉可能属于上一个账号的缓存。"""
        with self._登录锁:  # 两个登录并发时别丢掉自增
            self._令牌版本 += 1
        self._失效列缓存()
        with self._缓存锁:  # 换号后别拿旧账号的 fileId 拼路径
            self._目录缓存.clear()
            self._目录缓存.update({"": "", "/": ""})

    def _清理登录会话(self) -> None:
        """清掉过期/过多的扫码、短信会话（自带锁，可重入）。"""
        现在 = time.time()
        with self._登录锁:
            for 表 in (self._扫码会话, self._短信会话):
                for 键 in list(表):
                    if 现在 - float(表[键].get("创建") or 0) > 900.0:
                        # 短信会话上挂着复用的登录服务：过期时顺手关掉，
                        # 别把 httpx 客户端一直攒在内存里（长跑进程）。
                        self._关掉登录服务(表[键])
                        表.pop(键, None)
                while len(表) > 8:  # 长跑进程里别越堆越多
                    最早 = min(表, key=lambda k: float(表[k].get("创建") or 0))
                    self._关掉登录服务(表[最早])
                    表.pop(最早, None)

    @staticmethod
    def _安全响应(数据) -> str:
        """给界面看的响应摘要：剔除 device_code / 令牌 / 会话标识等敏感串。"""
        敏感 = ("device_code", "access_token", "refresh_token",
                "verification_token", "captcha_token", "verification_id",
                "verification_code")
        内容 = dict(数据) if isinstance(数据, dict) else {"原始": 数据}
        摘要 = {k: v for k, v in 内容.items() if k not in 敏感}
        return str(摘要)[:200]

    @staticmethod
    def _脱敏手机号(手机号: str) -> str:
        文本 = str(手机号 or "")
        if len(文本) <= 6:
            return 文本
        return f"{文本[:6]}****{文本[-2:]}"

    @staticmethod
    def _规范手机号(手机号: str) -> str:
        """整理成光鸭要的 ``"+86 13800000000"``；识别不了返回 ""。

        用户在界面上往往只填 11 位号码，适配器要的是"国家码 + 空格 + 号码"。
        """
        文本 = str(手机号 or "").strip()
        for 符号 in (" ", "-", "\t", "(", ")", "（", "）"):
            文本 = 文本.replace(符号, "")
        if not 文本:
            return ""
        带加号 = 文本.startswith("+")
        数字 = "".join(字符 for 字符 in 文本 if 字符 in "0123456789")
        if not 数字:
            return ""
        try:  # 光鸭适配器的默认国家码（"+86"）
            from 核心.认证.登录服务 import 默认国家码 as _默认国家码
            本地国家码 = str(_默认国家码 or "+86").lstrip("+") or "86"
        except Exception:
            本地国家码 = "86"
        国家码, 号码 = "", ""
        if 带加号:
            if 数字.startswith("86"):
                国家码, 号码 = "86", 数字[2:]
            else:
                for 长度 in (1, 2, 3):  # 国家码 1~3 位，取最短可行解
                    if 6 <= len(数字) - 长度 <= 15:
                        国家码, 号码 = 数字[:长度], 数字[长度:]
                        break
        elif 数字.startswith(本地国家码) and len(数字) > 11:
            国家码, 号码 = 本地国家码, 数字[len(本地国家码):]
        else:
            国家码, 号码 = 本地国家码, 数字
        if not 国家码 or not (6 <= len(号码) <= 15):
            return ""
        return f"+{国家码} {号码}"

    @staticmethod
    def _JWT载荷(令牌: str) -> dict:
        """尽力解析 JWT 载荷（取 exp/sub）；不是 JWT 就返回 {}。"""
        import base64
        import json
        try:
            段 = str(令牌 or "").split(".")
            if len(段) < 2:
                return {}
            载荷 = 段[1] + "=" * (-len(段[1]) % 4)
            数据 = json.loads(base64.urlsafe_b64decode(载荷.encode("ascii")))
            return 数据 if isinstance(数据, dict) else {}
        except Exception:
            return {}

    def _令牌有效期(self, 访问令牌: str, 额外: dict) -> int:
        """算 access_token 的剩余秒数：JWT exp > 额外参数 > 光鸭默认 7200。"""
        声明 = self._JWT载荷(访问令牌)
        try:
            return int(float(声明["exp"]) - time.time())
        except Exception:
            pass
        for 键 in ("有效期秒", "expires_in", "expiresIn"):
            try:
                值 = int(额外.get(键))
                if 值 > 0:
                    return 值
            except Exception:
                continue
        return 7200

    def _校验访问令牌(self, 访问令牌: str) -> dict:
        """用适配器自己的 认证服务.取当前用户() 验一次；无效令牌会抛异常。"""
        m = _导入()
        网络 = m["网络客户端"]()
        try:
            网络.设置访问令牌(访问令牌)
            用户 = m["认证服务"](网络).取当前用户(访问令牌)
            return dict(用户) if isinstance(用户, dict) else {"原始": 用户}
        finally:
            try:
                网络.关闭()
            except Exception:
                pass

    def _用刷新令牌换访问令牌(self, 刷新令牌: str) -> dict:
        """只给了 refresh_token 时，用适配器自己的刷新实现换一份新令牌。

        返回适配器的刷新响应（含 access_token / refresh_token / expires_in /
        sub），失败返回 {}。**必须整份返回**：服务端可能轮换 refresh_token，
        只取 access_token 会把用户填的旧 refresh_token 写回去（下次续期失败）。

        ⚠️ 适配器没有公开的"用 refresh_token 直接换登录令牌"接口，这里调用的是
        ``核心/认证/令牌仓库.py`` 里的模块私有函数 ``_默认刷新实现``；保存令牌
        本身走的是公开接口 ``令牌仓库.保存令牌``。
        """
        try:
            from 核心.认证 import 令牌仓库 as 令牌模块
            刷新实现 = getattr(令牌模块, "_默认刷新实现", None)
            if 刷新实现 is None:
                self.记录("[光鸭] 适配器没有 _默认刷新实现，无法用 refresh_token 换令牌",
                         "warning")
                return {}
            结果 = 刷新实现(刷新令牌)
            if isinstance(结果, dict) and 结果.get("access_token"):
                return dict(结果)
        except Exception as e:
            self.记录(f"[光鸭] refresh_token 换 access_token 失败：{e}", "warning")
        return {}

    def _写入令牌(self, 令牌数据: dict) -> None:
        """把登录响应写进适配器自己的 全局令牌仓库（公开接口），并回读校验。

        ``令牌仓库.保存令牌(刷新令牌="")`` 的语义是"保留原有刷新令牌"——那可能是
        上一个账号留下的，会让后续静默刷新把令牌换成别的账号，所以没有新刷新
        令牌时先用公开的 ``清空令牌()`` 清干净再写。
        """
        仓库 = _导入()["全局令牌仓库"]
        访问令牌 = str((令牌数据 or {}).get("access_token") or "").strip()
        if not 访问令牌:
            raise RuntimeError("令牌数据缺少 access_token")
        try:
            有效期秒 = int((令牌数据 or {}).get("expires_in") or 7200)
        except Exception:
            有效期秒 = 7200
        刷新令牌 = str((令牌数据 or {}).get("refresh_token") or "").strip()
        用户标识 = str((令牌数据 or {}).get("sub") or "").strip()
        if not 刷新令牌:
            仓库.清空令牌()
        仓库.保存令牌(
            访问令牌, 有效期秒,
            刷新令牌=刷新令牌, 用户标识=用户标识,
        )
        # 令牌仓库._保存() 写盘失败只 warning 不抛，这里回读一次避免"报成功没落盘"
        import json
        try:
            落盘 = json.loads(仓库.文件路径.read_text(encoding="utf-8"))
        except Exception as e:
            raise RuntimeError(f"令牌写入后回读失败：{e}") from e
        if str((落盘 or {}).get("访问令牌") or "") != 访问令牌:
            raise RuntimeError(f"令牌没有真正落到 {仓库.文件路径}")
        self._失效上下文()

    # ---------------- 扫码（设备授权码） ----------------

    def login_qr_start(self) -> dict:
        try:
            服务 = self._新建登录服务()
        except Exception as e:
            return self.登录失败(f"加载光鸭登录服务失败：{e}")
        try:
            设备 = 服务.请求设备授权码()
        except Exception as e:
            return self.登录失败(
                f"请求光鸭设备授权码失败：{e}",
                "检查网络/代理后重试（需要能访问 account.guangyapan.com）")
        finally:
            try:
                服务.关闭()
            except Exception:
                pass

        设备 = dict(设备) if isinstance(设备, dict) else {}
        device_code = str(设备.get("device_code") or "")
        用户码 = str(设备.get("user_code") or "")
        验证地址 = str(设备.get("verification_uri_complete")
                   or 设备.get("verification_uri")
                   or 设备.get("verification_url") or "")
        if not device_code:
            return self.登录失败("光鸭设备授权码响应异常（没有 device_code）",
                             f"响应：{self._安全响应(设备)}")
        try:
            轮询间隔 = max(1, int(设备.get("interval") or 3))
        except Exception:
            轮询间隔 = 3
        try:
            有效期秒 = max(5, int(设备.get("expires_in") or 120))
        except Exception:
            有效期秒 = 120

        会话 = "gy-qr-" + uuid.uuid4().hex[:16]  # 不透明句柄，界面原样回传
        with self._登录锁:
            self._扫码会话[会话] = {
                "device_code": device_code,
                "轮询间隔": 轮询间隔,
                "有效期秒": 有效期秒,
                "创建": time.time(),
                "取消": threading.Event(),
            }
            self._清理登录会话()
        self.记录(f"[光鸭] 已取得设备授权码：用户码 {用户码 or '（无）'}，"
                 f"有效期 {有效期秒}s，轮询间隔 {轮询间隔}s")
        # ⚠️ 光鸭的登录是**设备授权码**：服务端把「验证地址」整条 URL 交给客户端，
        #    适配器原 GUI（界面/弹窗/二维码弹窗.py）就是**把这条地址本身画成二维码**
        #    （qrcode.add_data(链接)）—— 用光鸭 App 扫这个码即完成授权。
        #    我们以前只回「验证地址」，界面对「设备码」类型不渲染二维码，
        #    用户看到的就是"二维码出不来、只能手打一长串用户码"，登录自然慢。
        #    这里把同一条地址同时放进 `二维码链接`，界面就会本地画码。
        return {
            "状态": "成功",
            "类型": "设备码",
            "验证地址": 验证地址,
            "二维码链接": 验证地址,
            "用户码": 用户码,
            "会话": 会话,
            "有效期秒": 有效期秒,
            "轮询间隔": 轮询间隔,
            "消息": f"已申请设备授权码（用户码 {用户码 or '见验证地址'}）",
            "提示": ("用光鸭 App 扫描二维码即可授权"
                    + (f"；也可以打开验证地址并输入用户码 {用户码}"
                       if 用户码 else "；也可以直接在浏览器打开验证地址")),
        }

    def login_qr_wait(self, 会话: str = "", 超时秒: float = 180.0) -> dict:
        会话 = str(会话 or "").strip()
        self._清理登录会话()
        with self._登录锁:
            条目 = self._扫码会话.get(会话) if 会话 else None
            if 条目 is None and not 会话 and self._扫码会话:
                # 界面没带会话：退化成"最近一次扫码"
                会话 = max(self._扫码会话,
                           key=lambda k: float(self._扫码会话[k].get("创建") or 0))
                条目 = self._扫码会话[会话]
        if 条目 is None and 会话 and not 会话.startswith("gy-qr-"):
            # 兼容：调用方没回传句柄、而是把 device_code 当"会话"传回来。
            # 本桥自己的句柄都带 gy-qr- 前缀，所以走到这里基本就是裸 device_code。
            条目 = {"device_code": 会话, "轮询间隔": 3, "有效期秒": 300,
                    "创建": time.time()}
            self.记录("[光鸭] 会话句柄未命中，按 device_code 直接轮询", "warning")
        if 条目 is None:
            return self.登录失败("扫码会话不存在或已结束",
                             "请重新点击「开始扫码」再等待结果")
        device_code = str(条目.get("device_code") or "")
        if not device_code:
            return self.登录失败("扫码会话缺少设备码", "请重新开始扫码")

        try:
            超时秒 = float(超时秒)
        except Exception:
            超时秒 = 180.0
        if 超时秒 <= 0:
            超时秒 = 180.0
        已过 = max(0.0, time.time() - float(条目.get("创建") or time.time()))
        剩余 = max(0.0, float(条目.get("有效期秒") or 120) - 已过)
        if 剩余 <= 1.0:
            return self.登录失败("设备授权码已过期", "请重新点击「开始扫码」")
        等待秒 = int(max(1.0, min(超时秒, 剩余)))
        轮询间隔 = max(1, int(条目.get("轮询间隔") or 3))
        取消 = 条目.get("取消")

        try:
            服务 = self._新建登录服务()
        except Exception as e:
            return self.登录失败(f"加载光鸭登录服务失败：{e}")

        结果盒: dict = {}

        def _轮询() -> None:
            """适配器的 轮询令牌() 是阻塞式的，放后台线程好打进度。"""
            try:
                令牌 = 服务.轮询令牌(device_code, 轮询间隔, 等待秒)
                if 取消 is not None and 取消.is_set():
                    结果盒["已取消"] = True  # 用户取消了：拿到令牌也不写入
                else:
                    结果盒["令牌"] = 令牌
            except Exception as e:  # 轮询内部已吞网络异常，这里只兜底
                结果盒["错误"] = e
            finally:
                try:
                    服务.关闭()
                except Exception:
                    pass

        线程 = threading.Thread(target=_轮询, name="光鸭扫码轮询", daemon=True)
        self.记录(f"[光鸭] 开始等待扫码授权（最长 {等待秒}s，"
                 f"每 {轮询间隔}s 轮询一次）")
        try:
            线程.start()
        except Exception as e:
            try:
                服务.关闭()
            except Exception:
                pass
            return self.登录失败(f"启动扫码轮询线程失败：{e}")

        开始 = time.time()
        下次汇报 = 开始 + 10.0
        while 线程.is_alive():
            线程.join(timeout=1.0)
            现在 = time.time()
            if 取消 is not None and 取消.is_set():
                break
            if 线程.is_alive() and 现在 >= 下次汇报:
                self.记录(f"[光鸭] 仍在等待扫码授权…已等待 {int(现在 - 开始)}s"
                         f" / {等待秒}s")
                下次汇报 = 现在 + 10.0
        if not 线程.is_alive():
            try:
                服务.关闭()
            except Exception:
                pass

        错误 = 结果盒.get("错误")
        if 错误 is not None:
            return self.登录失败(f"轮询扫码结果失败：{错误}", "请重新开始扫码")
        令牌数据 = 结果盒.get("令牌")
        # 与 login_qr_cancel() 抢同一把锁定序：取消先拿到锁，就绝不写令牌
        with self._登录锁:
            if 取消 is not None and 取消.is_set():
                self._扫码会话.pop(会话, None)
                self.记录("[光鸭] 扫码登录已取消")
                return {"状态": "已取消", "消息": "已取消扫码登录"}
            if not 令牌数据:
                return self.登录失败(
                    f"扫码登录超时（已等待 {int(time.time() - 开始)}s）或用户取消了授权",
                    "请重新点击「开始扫码」")
            try:
                self._写入令牌(dict(令牌数据))
            except Exception as e:
                return self.登录失败(f"扫码成功但保存令牌失败：{e}")
            self._扫码会话.pop(会话, None)
        self.记录("[光鸭] 扫码登录成功，令牌已写入适配器令牌仓库")
        return self.登录成功("扫码登录成功", "光鸭令牌已保存，可直接使用网盘功能")

    def login_qr_cancel(self, 会话: str = "") -> dict:
        会话 = str(会话 or "").strip()
        with self._登录锁:
            目标 = [会话] if 会话 in self._扫码会话 else list(self._扫码会话)
            for 键 in 目标:
                事件 = self._扫码会话.get(键, {}).get("取消")
                if 事件 is not None:
                    try:
                        事件.set()
                    except Exception:
                        pass
                self._扫码会话.pop(键, None)
        self.记录("[光鸭] 已取消扫码登录")
        return {"状态": "已取消", "消息": "已取消扫码登录"}

    # ---------------- 短信 ----------------

    @staticmethod
    def _关掉登录服务(条目: dict) -> None:
        """关掉一条短信会话上挂着的登录服务（幂等）。"""
        try:
            服务 = (条目 or {}).pop("服务", None)
            if 服务 is not None:
                服务.关闭()
        except Exception:
            pass

    def login_sms_send(self, 手机号: str) -> dict:
        """⚠️ 会真的调适配器发短信（真实扣费）：只有号码校验通过才会外呼。

        ⚠️ 关键差异（对齐适配器原 GUI 的 短信登录对话框）：原 GUI 是
        **一个 登录服务 实例走完 发送 → 校验 → 登录** 全流程，httpx 客户端、
        连接池、服务端下发的会话 Cookie 全程保留；而桥以前每一步都
        `_新建登录服务()` 再 `关闭()`，等于三份互不相干的客户端，
        短信登录成功率自然比原 GUI 低。现在把服务实例挂在会话上复用，
        校验完成或会话过期时再关闭。
        """
        规范号 = self._规范手机号(手机号)
        if not 规范号:
            return self.登录失败(
                "手机号格式不对",
                "光鸭要带国家码，示例：+86 13800000000（只填 11 位会自动按 +86 处理）")
        try:
            服务 = self._新建登录服务()
        except Exception as e:
            return self.登录失败(f"加载光鸭登录服务失败：{e}")
        try:
            captcha_token = 服务.短信登录_初始化盾(规范号)
            数据 = 服务.短信登录_发送验证码(规范号, captcha_token)
        except Exception as e:
            try:
                服务.关闭()
            except Exception:
                pass
            return self.登录失败(
                f"发送短信验证码失败：{e}",
                "若被风控拦截，请先在光鸭 App/网页端登录一次再试")

        数据 = dict(数据) if isinstance(数据, dict) else {}
        verification_id = str(数据.get("verification_id") or "")
        if not verification_id:
            try:
                服务.关闭()
            except Exception:
                pass
            return self.登录失败("发送验证码响应异常（没有 verification_id）",
                             f"响应：{self._安全响应(数据)}")
        try:
            有效秒 = int(数据.get("expires_in") or 300)
        except Exception:
            有效秒 = 300

        会话 = "gy-sms-" + uuid.uuid4().hex[:16]
        with self._登录锁:
            self._短信会话[会话] = {
                "手机号": 规范号,
                "verification_id": verification_id,
                "创建": time.time(),
                "服务": 服务,          # 跨步骤复用同一客户端（见上面注释）
            }
            self._清理登录会话()
        脱敏 = self._脱敏手机号(规范号)
        self.记录(f"[光鸭] 短信验证码已发送到 {脱敏}（{有效秒}s 内有效）")
        return {
            "状态": "成功",
            "消息": f"验证码已发送到 {脱敏}",
            "会话": 会话,
            "手机号": 规范号,
            "有效期秒": 有效秒,
            "提示": f"请填写收到的短信验证码（约 {max(1, 有效秒 // 60)} 分钟内有效）",
        }

    def login_sms_verify(self, 会话: str, 验证码: str,
                         手机号: str = "") -> dict:
        会话 = str(会话 or "").strip()
        验证码 = str(验证码 or "").strip()
        if not 验证码:
            return self.登录失败("验证码不能为空", "请填写短信里收到的数字验证码")
        if not (4 <= len(验证码) <= 8) or any(
                字符 not in "0123456789" for 字符 in 验证码):
            return self.登录失败("验证码格式不对", "验证码是 4~8 位数字")
        传入号码 = self._规范手机号(手机号)
        if str(手机号 or "").strip() and not 传入号码:
            return self.登录失败("手机号格式不对",
                             "示例：+86 13800000000")

        self._清理登录会话()
        with self._登录锁:
            条目 = self._短信会话.get(会话) if 会话 else None
            if 条目 is None and not 会话 and self._短信会话:
                会话 = max(self._短信会话,
                           key=lambda k: float(self._短信会话[k].get("创建") or 0))
                条目 = self._短信会话[会话]
        if 条目 is None:
            return self.登录失败("短信会话不存在或已过期",
                             "请重新点击「发送验证码」")
        # 以发验证码时的号码为准（verification_id 是绑在那个号码上的），
         # 界面另外传的号码只在会话里没有时兜底
        号码 = str(条目.get("手机号") or "") or 传入号码
        if not 号码:
            return self.登录失败("短信会话缺少手机号", "请重新点击「发送验证码」")

        try:
            服务 = 条目.get("服务") or self._新建登录服务()
        except Exception as e:
            return self.登录失败(f"加载光鸭登录服务失败：{e}")
        try:
            校验 = 服务.短信登录_校验验证码(
                str(条目.get("verification_id") or ""), 验证码)
            校验令牌 = (str(校验.get("verification_token") or "")
                     if isinstance(校验, dict) else "")
            令牌数据 = 服务.短信登录_登录(号码, 验证码, 校验令牌)
        except Exception as e:
            # 验证码错/过期都可能，保留会话让用户直接重输验证码
            return self.登录失败(f"短信登录失败：{e}",
                             "验证码错误可直接重试；已过期请重新发送")

        # 适配器 短信登录_登录() 内部已经存过令牌；这里再按公开口径写一次，
        # 保证过期时间准确，并清掉可能残留的上一个账号的刷新令牌。
        try:
            if isinstance(令牌数据, dict) and 令牌数据.get("access_token"):
                self._写入令牌(令牌数据)
        except Exception as e:
            return self.登录失败(f"短信登录成功但保存令牌失败：{e}")
        finally:
            self._关掉登录服务(条目)
        with self._登录锁:
            self._短信会话.pop(会话, None)
        self.记录("[光鸭] 短信登录成功，令牌已写入适配器令牌仓库")
        return self.登录成功("短信登录成功", "光鸭令牌已保存，可直接使用网盘功能")

    # ---------------- 令牌 ----------------

    def login_token(self, 访问令牌: str = "", 刷新令牌: str = "",
                    额外: dict | None = None) -> dict:
        访问令牌 = str(访问令牌 or "").strip()
        刷新令牌 = str(刷新令牌 or "").strip()
        额外 = dict(额外) if isinstance(额外, dict) else {}
        if 访问令牌.lower().startswith("bearer "):  # 有人会把整行复制进来
            访问令牌 = 访问令牌[7:].strip()

        if not 访问令牌:
            if not 刷新令牌:
                return self.登录失败(
                    "访问令牌不能为空",
                    "在浏览器登录光鸭后，复制 access_token（可一并填 refresh_token）")
            刷新结果 = self._用刷新令牌换访问令牌(刷新令牌)
            访问令牌 = str(刷新结果.get("access_token") or "").strip()
            if not 访问令牌:
                return self.登录失败("刷新令牌无效或已过期",
                                 "请重新扫码登录，或换一个有效的 refresh_token")
            # 服务端可能轮换 refresh_token：整份用新响应，别把旧的写回去
            刷新令牌 = str(刷新结果.get("refresh_token") or 刷新令牌).strip()
            if not any(额外.get(键) for 键 in ("有效期秒", "expires_in", "expiresIn")):
                try:
                    额外["有效期秒"] = int(刷新结果.get("expires_in") or 0)
                except Exception:
                    pass
            if 刷新结果.get("sub"):
                额外.setdefault("sub", str(刷新结果.get("sub")))

        # 先验证、后落盘：坏令牌不会把原来的好令牌顶掉
        try:
            用户 = self._校验访问令牌(访问令牌)
        except Exception as e:
            return self.登录失败(
                f"令牌校验失败：{e}",
                "确认 access_token 未过期且复制完整（不要带 Bearer 前缀）")

        有效期秒 = self._令牌有效期(访问令牌, 额外)
        if 有效期秒 <= 0:
            return self.登录失败("访问令牌已过期",
                             "请换一个新的 access_token，或用扫码登录")
        声明 = self._JWT载荷(访问令牌)
        用户标识 = str(额外.get("sub") or 额外.get("用户标识")
                    or 声明.get("sub") or "")
        try:
            self._写入令牌({
                "access_token": 访问令牌,
                "refresh_token": 刷新令牌,
                "expires_in": 有效期秒,
                "sub": 用户标识,
            })
        except Exception as e:
            return self.登录失败(f"保存令牌失败：{e}")

        名字 = ""
        if isinstance(用户, dict):
            名字 = str(用户.get("name") or 用户.get("nickname")
                     or 用户.get("username") or 用户.get("phone") or "")
        分钟 = max(1, 有效期秒 // 60)
        self.记录(f"[光鸭] 令牌登录成功：{名字 or 用户标识 or '（已认证）'}"
                 f"（约 {分钟} 分钟有效）")
        return self.登录成功(
            f"令牌登录成功{'：' + 名字 if 名字 else ''}",
            f"已写入光鸭令牌仓库（access_token 约 {分钟} 分钟有效）"
            + ("，将用 refresh_token 静默续期" if 刷新令牌
               else "，未提供 refresh_token"))

    # ---------------- 光鸭没有的登录方式 ----------------

    def _不支持登录(self, 方式: str) -> dict:
        return {"状态": "不支持", "消息": self.不支持说明[方式]}

    def login_cookie(self, 文本: str) -> dict:
        return self._不支持登录("cookie")

    def login_email(self, 邮箱: str = "", 密码: str = "",
                    额外: dict | None = None) -> dict:
        return self._不支持登录("email")

    def login_password(self, 账号: str = "", 密码: str = "",
                       额外: dict | None = None) -> dict:
        return self._不支持登录("password")

    # ---------------- 退出登录 ----------------

    def 退出登录(self) -> dict:
        """清掉本机保存的光鸭令牌（数据/令牌.json），并把登录态同步收干净。

        只动本地文件：不调云端接口撤令牌、不碰云端文件。
        用户现场反馈：在原 GUI 里退出登录后，网盘管理这边列表还在、还能上传；
        根因就是没人把"凭证没了"这件事同步给界面 —— 这里从桥这一层就把
        内存令牌、各线程网络上下文、目录缓存全部作废，account() 立刻回未登录。
        """
        m = self._导入()
        清除: list[str] = []
        # 退出登录时把还挂着的登录会话一起收掉（短信会话上有复用的登录服务）
        with self._登录锁:
            for 键 in list(self._短信会话):
                self._关掉登录服务(self._短信会话.pop(键, None) or {})
            self._扫码会话.clear()
        try:
            仓库 = m["全局令牌仓库"]
            路径 = getattr(仓库, "文件路径", None)
            仓库.清空令牌()
            if 路径 is not None:
                清除.append(str(路径))
        except Exception as e:  # noqa: BLE001
            return self.登录失败(f"清空光鸭令牌失败：{e}",
                             "可在适配器原 GUI 里退出登录")
        # 兜底：万一仓库路径与项目数据目录不一致，这里再扫一遍
        try:
            for 名字 in ("令牌.json", "凭证.json", "会话.json"):
                文件 = self.项目根 / "数据" / 名字
                if 文件.is_file():
                    文件.unlink()
                    清除.append(str(文件))
        except Exception as e:  # noqa: BLE001
            self.记录(f"[光鸭] 清理残留凭证文件失败：{e}", "warning")
        # 作废所有线程的网络上下文与目录/列缓存：否则下一次调用还会拿着旧令牌
        self._失效上下文()
        try:
            self.记录("[光鸭] 已退出登录（本地令牌已清除）")
        except Exception:
            pass
        信息 = {}
        try:
            信息 = self.account()
        except Exception:
            pass
        return {
            "状态": "成功",
            "消息": "已退出登录，本地令牌已清除",
            "清除": 清除,
            "账号": 信息,
        }

    # ---------------- 路径/ID ----------------

    def _失效列缓存(self) -> None:
        with self._列缓存锁:
            self._列缓存.clear()

    def _列一层(self, 父ID: str, 用缓存: bool = True) -> list[dict]:
        键 = str(父ID or "")
        if 用缓存:
            with self._列缓存锁:
                命中 = self._列缓存.get(键)
            if 命中 is not None and 命中[0] > time.time():
                return list(命中[1])
        ctx = self._ctx()
        结果: list[dict] = []
        页 = 0
        while 页 < 1000:
            数据 = ctx.文件.取文件列表(
                parentId=父ID or "", page=页, pageSize=1000,
                orderBy=3, sortType=1)
            列表 = 数据.get("list") or []
            for 项 in 列表:
                结果.append(项)
            if len(列表) < 1000:
                break
            页 += 1
        with self._列缓存锁:
            self._列缓存[键] = (time.time() + 10.0, list(结果))
        return 结果

    def _找子项(self, 父ID: str, 名称: str,
                用缓存: bool = False) -> dict | None:
        """按名字找子项（默认读实时）。

        与夸克同理：服务端对刚上传的文件有可见性延迟，而 `_列一层` 有 10 秒缓存，
        点查若走缓存会把延迟放大成"查不到"，导致跨盘传输误报"源路径不存在"。
        """
        for 项 in self._列一层(父ID, 用缓存=用缓存):
            if str(项.get("fileName") or "") == 名称:
                return 项
        return None

    def _路径锁(self, 路径: str) -> threading.RLock:
        """同一条目录路径一把锁（与夸克同理：并发抢建目录会互相打架）。"""
        with self._解析锁表锁:
            锁 = self._解析锁表.get(路径)
            if 锁 is None:
                锁 = threading.RLock()
                if len(self._解析锁表) > 512:
                    self._解析锁表.clear()
                self._解析锁表[路径] = 锁
            return 锁

    def _解析目录(self, path: str, 创建: bool = False) -> str:
        路径 = 规范(path)
        if 路径 == "/":
            return ""
        with self._缓存锁:
            if 路径 in self._目录缓存:
                return self._目录缓存[路径]
        if 创建:
            with self._路径锁(路径):
                with self._缓存锁:
                    if 路径 in self._目录缓存:
                        return self._目录缓存[路径]
                return self._解析目录_内(路径, True)
        return self._解析目录_内(路径, False)

    def _解析目录_内(self, 路径: str, 创建: bool) -> str:
        父ID = ""
        累积 = ""
        for 段 in [s for s in 路径.split("/") if s]:
            累积 = f"{累积}/{段}"
            with self._缓存锁:
                已知 = self._目录缓存.get(累积)
            if 已知 is not None:
                父ID = 已知
                continue
            子项 = self._找子项(父ID, 段)
            if 子项 is None:
                if not 创建:
                    raise FileNotFoundError(f"光鸭路径不存在：{累积}")
                ctx = self._ctx()
                try:
                    子ID = ctx.上传.创建目录(父ID, 段, failIfNameExist=True)
                    self._失效列缓存()
                except Exception as e:  # noqa: BLE001
                    # 并发创建/服务端"正在处理"：重查几次（最终一致性）
                    子ID = ""
                    子项 = None
                    for i in range(4):
                        time.sleep(min(3.0, 0.6 * (2 ** i)))
                        self._失效列缓存()
                        子项 = self._找子项(父ID, 段, 用缓存=False)
                        if 子项 is not None:
                            子ID = str(子项.get("fileId") or "")
                            if 子ID:
                                break
                    if not 子ID:
                        self.记录(f"[光鸭] 建目录失败且重查不到：{累积}：{e}", "warning")
                        raise
            else:
                if not _是否目录(子项):
                    raise NotADirectoryError(f"光鸭路径不是目录：{累积}")
                子ID = str(子项.get("fileId") or "")
            if not 子ID:
                raise RuntimeError(f"光鸭目录缺少 fileId：{累积}")
            父ID = 子ID
            with self._缓存锁:
                self._目录缓存[累积] = 父ID
        return 父ID

    def _解析条目(self, path: str) -> dict | None:
        路径 = 规范(path)
        if 路径 == "/":
            return {"name": "/", "is_dir": True, "fileId": "", "fileName": "/",
                    "fileSize": 0, "utime": 0, "path": "/", "id": ""}
        父目录, 名称 = 路径.rsplit("/", 1)
        父ID = self._解析目录(父目录 or "/", 创建=False)
        子项 = self._找子项(父ID, 名称)
        if 子项 is None:
            return None
        return {
            "name": 名称,
            "path": 路径,
            "is_dir": _是否目录(子项),
            "size": int(子项.get("fileSize") or 0),
            "modified": _时间文本(子项.get("utime") or 子项.get("ctime")),
            "id": str(子项.get("fileId") or ""),
            # 兼容内部仍可能读原始字段的旧调用
            "fileId": str(子项.get("fileId") or ""),
            "fileName": 名称,
            "fileSize": int(子项.get("fileSize") or 0),
            "utime": 子项.get("utime") or 0,
            "raw": 子项,
        }

    def list(self, path: str) -> list[dict]:
        ctx = self._ctx()
        父ID = self._解析目录(path, 创建=False)
        结果 = [ctx.条目(项, path) for 项 in self._列一层(父ID, 用缓存=False)]
        return 结果

    def stat(self, path: str) -> dict | None:
        try:
            return self._解析条目(path)
        except FileNotFoundError:
            return None

    def ensure_dir(self, path: str) -> dict:
        目录ID = self._解析目录(path, 创建=True)
        return {"id": 目录ID, "path": 规范(path)}

    # ---------------- 上传/下载 ----------------

    def _上传带重试(self, ctx, 上传源: Path, 目录ID: str, 适配进度,
                 尝试次数: int = 4):
        """上传遇"同名冲突/正在处理"等瞬时错误时退避重试（与夸克一致）。"""
        最后 = None
        for i in range(max(1, 尝试次数)):
            try:
                return ctx.上传.上传文件(
                    本地路径=str(上传源),
                    父目录ID=目录ID,
                    进度回调=适配进度,
                )
            except Exception as e:  # noqa: BLE001
                最后 = e
                if i + 1 >= 尝试次数 or not self._是瞬时错误(e):
                    raise
                等待 = min(20.0, 2.0 * (2 ** i))
                self.记录(f"[光鸭] 上传遇到瞬时错误，{等待:.0f}s 后重试"
                        f"（{i + 1}/{尝试次数}）：{e}", "warning")
                time.sleep(等待)
        raise 最后 if 最后 else RuntimeError("光鸭上传失败")

    #: 服务端"暂时不可用"类错误（可重试）——与夸克后端保持同一套判定
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

    def upload(self, local_path: str, remote_dir: str, name: str,
               task_id: str, progress) -> dict:
        源 = Path(local_path)
        if not 源.is_file():
            raise FileNotFoundError(f"本地文件不存在：{local_path}")
        # 目标盘命名规避（当前 guangya 无字符限制 → 原样返回；规则表见 v8_3/敏感词/命名规避.py）
        原名称 = name or 源.name
        规范名, 改名说明 = 规范命名("guangya", 原名称)
        if 改名说明:
            self.记录(f"[guangya] 命名规避：{改名说明}")
            name = 规范名
            别名 = 源.with_name(name)
            try:
                if not 别名.exists():
                    别名.hardlink_to(源)
            except OSError:
                import shutil as _sh
                _sh.copy2(源, 别名)
            源 = 别名
        目录ID = self._解析目录(remote_dir, 创建=True)
        总大小 = 源.stat().st_size
        ctx = self._ctx()

        上传源 = 源
        临时别名 = None
        if name and name != 源.name:
            临时别名 = 源.with_name(name)
            try:
                if not 临时别名.exists():
                    临时别名.hardlink_to(源)
                elif 临时别名.samefile(源) is False:
                    raise FileExistsError(f"缓存目录存在同名文件：{临时别名}")
            except OSError:
                import shutil
                shutil.copy2(源, 临时别名)
            上传源 = 临时别名

        def 适配进度(阶段: str, 比例: float) -> None:
            if progress:
                progress(f"上传-{阶段}",
                         int(max(0.0, min(1.0, float(比例 or 0))) * 总大小),
                         总大小)

        try:
            结果 = self._上传带重试(ctx, 上传源, 目录ID, 适配进度)
        finally:
            if 临时别名 is not None:
                try:
                    临时别名.unlink(missing_ok=True)
                except Exception:
                    pass

        信息 = 结果 if isinstance(结果, dict) else {}
        self._失效列缓存()
        # 上传后可见性确认（guangya）
        # 列目录可能滞后 1–3 秒；不确认的话"上传完立刻跨盘传输"的源扫描会漏文件。
        请求名 = name or 源.name
        try:
            实际名 = 确认可见(lambda: self.list(remote_dir), 请求名,
                          日志=self.记录)
        except Exception:
            实际名 = None
        落盘名 = 实际名 or 请求名
        目录 = 规范(remote_dir)
        返回 = {
            "name": 落盘名,
            "path": 规范((目录.rstrip("/") or "") + "/" + 落盘名),
            "size": 总大小,
            "bytes": 总大小,
            "skipped": bool(信息.get("_秒传") or 信息.get("是否秒传")),
            "remote_id": str(信息.get("fileId") or ""),
        }
        if 落盘名 != 请求名:
            返回["改名信息"] = {
                "原名": 请求名,
                "新名": 落盘名,
                "说明": f"服务端把文件名改成了 {落盘名}",
                "策略": "服务端改名",
                "原路径": 规范(f"{目录.rstrip('/')}/{请求名}"),
                "新路径": 规范(f"{目录.rstrip('/')}/{落盘名}"),
            }
        return 返回

    def play_link(self, remote_path: str) -> dict:
        """给播放器用的直链（V8_3 新增）：只取签名地址，不下载。"""
        ctx = self._ctx()
        条目 = self._解析条目(remote_path)
        if 条目 is None:
            raise FileNotFoundError(f"光鸭文件不存在：{remote_path}")
        if 条目.get("is_dir"):
            raise IsADirectoryError(f"光鸭路径是目录，不能播放：{remote_path}")
        文件ID = str(条目.get("id") or "")
        if not 文件ID:
            raise RuntimeError(f"光鸭文件缺少 fileId：{remote_path}")
        信息 = ctx.下载.取下载地址(文件ID) or {}
        地址 = str(信息.get("signedURL") or 信息.get("url") or "")
        if not 地址:
            raise RuntimeError("光鸭没有返回可播放直链")
        return {"url": 地址, "headers": {},
                "size": int(条目.get("size") or 0),
                "name": str(条目.get("name") or remote_path.rsplit("/", 1)[-1]),
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
        条目 = self._解析条目(remote_path)
        if 条目 is None:
            raise FileNotFoundError(f"光鸭文件不存在：{remote_path}")
        if 条目.get("is_dir"):
            raise IsADirectoryError(f"光鸭路径是目录，不能下载：{remote_path}")
        文件ID = str(条目.get("id") or "")
        if not 文件ID:
            raise RuntimeError(f"光鸭文件缺少 fileId：{remote_path}")

        def 适配进度(已下载: int, 总量: int) -> None:
            if progress:
                progress("download", int(已下载 or 0), int(总量 or 0))

        总大小 = int(条目.get("size") or 0)
        指纹 = str(条目.get("hash") or 条目.get("contentHash") or "")
        if 保守:
            self.记录("[光鸭] 保守模式：跳过直链，直接用适配器原生下载")
        分段, 阈值 = 读取下载分段()
        目标 = Path(local_path)
        已有 = 目标.stat().st_size if 目标.exists() else 0
        if 已有 and 总大小 and 已有 >= 总大小:
            已有 = 0
        if 已有:
            self.记录(f"[光鸭] 断点续传：本地已有 {已有 / 1048576:.1f} MiB / "
                    f"{总大小 / 1048576:.1f} MiB")
        if not 保守 and 分段 >= 2 and 总大小 >= max(8 * 1024 * 1024, 阈值):
            try:
                信息 = ctx.下载.取下载地址(文件ID)
                地址 = str(信息.get("signedURL") or 信息.get("url") or "")
                if 地址 and 多段下载(地址, {}, local_path, 总大小, 分段,
                                 阈值, 进度=适配进度, 日志=self.记录,
                                 续传=续传, 期望指纹=指纹):
                    self.记录(f"[光鸭] 多段下载完成：{分段} 段 / {总大小} 字节")
                    目标 = Path(local_path)
                    return {"path": str(目标),
                            "size": 目标.stat().st_size,
                            "bytes": 目标.stat().st_size}
            except Exception as e:
                self.记录(f"[光鸭] 多段下载失败，回退单连接：{e}", "warning")
        # 回退：直链单连接（可续传）——比"从零重下"友好得多
        if 续传 and not 保守:
            try:
                信息 = ctx.下载.取下载地址(文件ID)
                地址 = str(信息.get("signedURL") or 信息.get("url") or "")
                if 地址 and 多段下载(地址, {}, local_path, 总大小, 1,
                                 0, 进度=适配进度, 日志=self.记录,
                                 续传=续传, 期望指纹=指纹, 直链续传=True):
                    目标 = Path(local_path)
                    return {"path": str(目标), "size": 目标.stat().st_size,
                            "bytes": 目标.stat().st_size}
            except Exception as e:
                self.记录(f"[光鸭] 直链续传下载失败，回退适配器下载：{e}", "warning")
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
            self.记录(f"[" + "光鸭" + f"] 直链没取到数据，回退适配器下载器"
                    f"（原断点 {已有字节 / 1048576:.1f} MiB 会被覆盖）", "warning")
        ctx.下载.下载到文件(文件ID, local_path, 进度回调=适配进度)
        目标 = Path(local_path)
        return {"path": str(目标), "size": 目标.stat().st_size,
                "bytes": 目标.stat().st_size}

    def delete(self, path: str) -> dict:
        ctx = self._ctx()
        条目 = self._解析条目(path)
        if 条目 is None:
            raise FileNotFoundError(f"光鸭路径不存在：{path}")
        文件ID = str(条目.get("id") or "")
        if not 文件ID:
            raise RuntimeError(f"光鸭路径缺少 fileId：{path}")
        结果 = ctx.文件.删除文件([文件ID]) if 条目.get("is_dir") else ctx.文件.删除文件([文件ID])
        # 删除目录时同时清掉路径缓存
        if 条目.get("is_dir"):
            with self._缓存锁:
                for 键 in list(self._目录缓存):
                    if 键 == 规范(path) or 键.startswith(规范(path).rstrip("/") + "/"):
                        self._目录缓存.pop(键, None)
        self._失效列缓存()
        return dict(结果 or {})

    def close(self) -> None:
        ctx = getattr(self._本地, "ctx", None)
        if ctx is not None:
            try:
                ctx.网络.关闭()
            except Exception:
                pass
