"""夸克网盘后端：桥接工作区里的 夸克网盘适配器/核心。"""

from __future__ import annotations

import shutil
import threading
import time
import urllib.parse
import uuid
from pathlib import Path

from 后端_基类 import (后端基类, 规范, 多段下载, 读取下载分段,
                    续传多段下载, 续传单连接下载, 清理续传文件,
                    规范命名,
                    确认可见)


def _导入():
    from 核心.认证.凭证仓库 import 全局凭证仓库
    from 核心.认证.认证服务 import 认证服务
    from 核心.认证.登录服务 import 登录错误, 登录服务
    from 核心.接口.文件接口 import 文件接口
    from 核心.网络.网络客户端 import 网络客户端
    from 核心.下载.下载服务 import 下载服务
    from 核心.上传.上传总调度 import 上传总调度
    return locals()


def _时间文本(值) -> str:
    try:
        数字 = float(值)
        if 数字 > 10_000_000_000:
            数字 /= 1000.0
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(数字))
    except Exception:
        return ""


def _二维码PNG(文本: str) -> str:
    """把二维码内容渲染成 PNG 的 base64；渲染不出来返回 ""。

    夸克**没有二维码图片接口**：`登录服务.取登录二维码()` 只给一条
    `https://su.quark.cn/4_eMHBJ?token=…` 链接，适配器界面也是用本地 qrcode
    库把这条链接画成二维码（界面/弹窗/二维码弹窗.py）。桥是无人值守进程，
    这里做同一件事；桥解释器没装 qrcode/Pillow 时返回 ""，
    调用方退化成「链接」型，由界面自己渲染。
    """
    if not 文本:
        return ""
    try:
        import base64
        import io

        import qrcode

        二维码 = qrcode.QRCode(
            version=None,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=8,
            border=2,
        )
        二维码.add_data(文本)
        二维码.make(fit=True)
        缓冲 = io.BytesIO()
        二维码.make_image(fill_color="black", back_color="white").save(
            缓冲, format="PNG")
        数据 = 缓冲.getvalue()
        return base64.b64encode(数据).decode("ascii") if 数据 else ""
    except Exception:
        return ""


class _扫码会话:
    """一次扫码登录的后台任务。

    `login_qr_start` 只负责把二维码交给界面，真正的轮询跑在后台线程里
    ——适配器的 `登录服务.扫码登录()` 是「取码 + 轮询 + ticket 换 Cookie +
    落盘」一条龙，没有公开的「只轮询/只换票」拆分接口，所以整段交给它跑，
    桥只在外面等结果。

    每个会话一个自己的 `登录服务` 实例（独立 httpx 客户端与 cookie jar），
    互不干扰；结束或取消时 `收尾()` 关掉它。
    """

    def __init__(self, 登录器, 记录):
        self.会话 = ""
        self.登录器 = 登录器
        self._记录 = 记录
        self.二维码就绪 = threading.Event()
        self.完成 = threading.Event()
        self.链接 = ""
        self.用户码 = ""
        self.凭证: dict | None = None
        self.错误 = ""
        self.已取消 = False
        self.线程 = threading.Thread(target=self._跑, daemon=True,
                                    name="夸克扫码登录")

    # ---------------- 线程体 ----------------

    def 启动(self) -> None:
        self.线程.start()

    def _跑(self) -> None:
        try:
            self.凭证 = self.登录器.扫码登录(
                二维码回调=self._回调二维码,
                状态回调=lambda 文本: self._记录(f"[夸克] {文本}"),
                保存=True)
        except Exception as e:      # 登录错误 / 网络异常 / 取消，都不抛给界面
            self.错误 = str(e) or type(e).__name__
            self._记录(f"[夸克] 扫码登录未完成：{self.错误}", "警告")
        finally:
            self.二维码就绪.set()
            self.完成.set()

    def _回调二维码(self, 链接: str, 用户码: str = "") -> None:
        self.链接 = str(链接 or "")
        self.用户码 = str(用户码 or "")      # 夸克没有用户码，恒为 ""
        self.二维码就绪.set()

    # ---------------- 外部 ----------------

    def 等二维码(self, 超时秒: float) -> bool:
        return self.二维码就绪.wait(max(0.1, float(超时秒)))

    def 取消(self) -> None:
        """让适配器的轮询循环立刻退出：它按 `扫码超时秒` 判断是否继续。"""
        self.已取消 = True
        try:
            self.登录器.扫码超时秒 = 0.0
        except Exception:
            pass

    def 收尾(self) -> None:
        try:
            self.登录器.关闭()
        except Exception:
            pass


class _上下文:
    def __init__(self):
        m = _导入()
        cookie = m["全局凭证仓库"].取Cookie()
        if not cookie:
            raise PermissionError("夸克未登录：本地没有可用 Cookie")
        self.网络 = m["网络客户端"](cookie=cookie)
        self.文件 = m["文件接口"](self.网络)
        self.认证 = m["认证服务"](self.网络)
        self.下载 = m["下载服务"](self.网络)
        self.上传 = m["上传总调度"](self.网络)

    def 条目(self, 原始: dict, 目录路径: str) -> dict:
        名字 = str(原始.get("file_name") or "")
        return {
            "name": 名字,
            "path": 规范((目录路径.rstrip("/") or "") + "/" + 名字),
            "is_dir": self.文件.是否目录(原始),
            "size": int(原始.get("size") or 0),
            "modified": _时间文本(原始.get("updated_at") or 原始.get("created_at")),
            "id": str(原始.get("fid") or ""),
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
        self._目录缓存: dict[str, str] = {"": "0", "/": "0"}
        self._缓存锁 = threading.RLock()
        # 登录态版本号：登录成功后 +1，各线程据此丢弃带旧 Cookie 的上下文
        self._上下文代 = 0
        # 扫码会话表（start/wait/cancel 可能落在工作进程的不同线程里，
        # 所以状态必须放在实例上而不是 threading.local）
        self._扫码锁 = threading.RLock()
        self._扫码表: dict[str, _扫码会话] = {}
        self._最近会话 = ""

    def _ctx(self) -> _上下文:
        代 = self._上下文代
        上下文 = getattr(self._本地, "ctx", None)
        if 上下文 is not None and getattr(self._本地, "代", None) == 代:
            return 上下文
        if 上下文 is not None:      # 登录态变了：关掉旧客户端再重建
            try:
                上下文.网络.关闭()
            except Exception:
                pass
            self._本地.ctx = None
        上下文 = _上下文()
        self._本地.ctx = 上下文
        self._本地.代 = 代
        return 上下文

    # ---------------- 账号 ----------------

    def _把刷新后的cookie落库(self, 仓库, 网络) -> None:
        """夸克会在响应里滚动下发新的 ``__puus``；把它写回凭证仓库。

        不写回的话：下一次新建上下文又用旧 Cookie → 服务端回
        ``require login [guest]``（实测：第一次能拿容量，第二次就失败，
        表现就是"用户：- / 时好时坏"）。
        """
        try:
            内存 = str(getattr(网络, "Cookie", "") or "")
            if not 内存:
                return
            旧 = str(仓库.取Cookie() or "")
            if 内存 != 旧:
                仓库.保存Cookie(内存)
                self.记录("[夸克] 已把服务端刷新的 Cookie 写回凭证仓库")
        except Exception as e:  # noqa: BLE001
            self.记录(f"[夸克] Cookie 落库失败（不影响本次）：{e}", "warning")

    def account(self) -> dict:
        m = _导入()
        仓库 = m["全局凭证仓库"]
        cookie = 仓库.取Cookie()
        详情 = {}
        用户名 = ""
        try:
            ctx = self._ctx()
            信息 = {}
            try:
                信息 = ctx.认证.取账号信息() or {}
            except Exception as e:  # noqa: BLE001
                # 服务端滚动下发的 __puus 可能已过期：重建上下文（会重新读库）再试一次
                详情["首次错误"] = str(e)[:120]
                self._本地.ctx = None
                ctx = self._ctx()
                信息 = ctx.认证.取账号信息() or {}
            self._把刷新后的cookie落库(仓库, ctx.网络)
            总 = 信息.get("total_capacity")
            已用 = 信息.get("use_capacity")
            可用 = 信息.get("free_capacity")
            if 可用 is None and 总 is not None and 已用 is not None:
                try:
                    可用 = max(0, int(总) - int(已用))
                except Exception:
                    可用 = None
            # 隐藏空间（secret_*）单独给，界面按需要展示
            详情["member"] = {
                "total_capacity": 总,
                "use_capacity": 已用,
                "free_capacity": 可用,
                "member_type": 信息.get("member_type"),
                "secret_total_capacity": 信息.get("secret_total_capacity"),
                "secret_use_capacity": 信息.get("secret_use_capacity"),
            }
            # 用户名：member_info 里可能有昵称字段，有就用（没有就留空，界面不显示"-"）
            内部 = 信息.get("member_info") if isinstance(信息.get("member_info"), dict) else {}
            for 键 in ("nickname", "nick_name", "user_name", "username", "name"):
                值 = 内部.get(键) or 信息.get(键)
                if 值:
                    用户名 = str(值)
                    break
        except Exception as e:
            详情["error"] = str(e)[:200]
        return {
            "logged_in": bool(cookie),
            "user": 用户名,
            "data_dir": str(self.项目根 / "数据"),
            "detail": 详情,
        }

    # ---------------- 路径/ID ----------------

    def _失效列缓存(self) -> None:
        with self._列缓存锁:
            self._列缓存.clear()

    def _列一层(self, 父ID: str, 用缓存: bool = True) -> list[dict]:
        键 = str(父ID or "0")
        if 用缓存:
            with self._列缓存锁:
                命中 = self._列缓存.get(键)
            if 命中 is not None and 命中[0] > time.time():
                return list(命中[1])
        ctx = self._ctx()
        结果: list[dict] = []
        页 = 1
        while 页 < 1000:
            数据 = ctx.文件.取文件列表(
                parentId=str(父ID or "0"), page=页, pageSize=1000,
                取总数=False)
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
        """按名字找子项。

        ⚠️ 默认 **不用缓存**：夸克服务端对刚上传的文件有 1–3 秒的可见性延迟，
        而 `_列一层` 的缓存 TTL 是 10 秒——两者叠加会让"刚上传完立刻查"
        在 10 秒内一直查不到（实测复现），表现就是跨盘传输报"源路径不存在"。
        列表浏览仍走缓存（列目录接口自己传 用缓存=True），点查一律读实时。
        """
        for 项 in self._列一层(父ID, 用缓存=用缓存):
            if str(项.get("file_name") or "") == 名称:
                return 项
        return None

    def _路径锁(self, 路径: str) -> threading.RLock:
        """同一条目录路径一把锁：只让一个线程去"查—建"，其余等它建好再读。

        ⚠️ 2026-09-16 压力实测：8 个线程并发上传到一个**尚未创建**的目录时，
        5 个失败在 `file is doloading[同名冲突]`——服务端的建目录不是幂等的，
        并发抢建会互相打架。加锁后同一目录只建一次。
        """
        with self._解析锁表锁:
            锁 = self._解析锁表.get(路径)
            if 锁 is None:
                锁 = threading.RLock()
                if len(self._解析锁表) > 512:      # 简单上限，避免无限增长
                    self._解析锁表.clear()
                self._解析锁表[路径] = 锁
            return 锁

    def _建目录并定位(self, 父ID: str, 段: str, 累积: str,
                 尝试次数: int = 4) -> str:
        """创建目录；服务端"同名冲突/正在处理"时重查+重试，返回目录 ID。"""
        最后错误 = None
        for i in range(max(1, 尝试次数)):
            ctx = self._ctx()
            try:
                数据 = ctx.文件.创建文件夹(父ID, 段)
                self._失效列缓存()
                子ID = str((数据 or {}).get("fid") or "")
                if 子ID:
                    return 子ID
                最后错误 = RuntimeError(f"创建目录未返回 fid：{累积}")
            except Exception as e:  # noqa: BLE001
                最后错误 = e
                if not self._是瞬时错误(e) and i == 0:
                    # 非瞬时错误也可能是"并发已建"：先查一次再决定
                    pass
            # 建失败/无 fid：多半是并发已存在或服务端还在处理 → 查一次
            time.sleep(min(3.0, 0.6 * (2 ** i)))
            self._失效列缓存()
            子项 = self._找子项(父ID, 段, 用缓存=False)
            if 子项 is not None:
                子ID = str(子项.get("fid") or "")
                if 子ID:
                    return 子ID
        raise 最后错误 if 最后错误 else RuntimeError(f"夸克目录创建失败：{累积}")

    def _解析目录(self, path: str, 创建: bool = False) -> str:
        路径 = 规范(path)
        if 路径 == "/":
            return "0"
        with self._缓存锁:
            if 路径 in self._目录缓存:
                return self._目录缓存[路径]
        if not 创建:
            return self._解析目录_内(路径, False)
        # 建目录路径串行化：同一目录只让一个线程去建
        with self._路径锁(路径):
            with self._缓存锁:
                if 路径 in self._目录缓存:
                    return self._目录缓存[路径]
            return self._解析目录_内(路径, True)

    def _解析目录_内(self, 路径: str, 创建: bool) -> str:
        父ID = "0"
        累积 = ""
        for 段 in [s for s in 路径.split("/") if s]:
            累积 = f"{累积}/{段}"
            with self._缓存锁:
                已知 = self._目录缓存.get(累积)
            if 已知 is not None:
                父ID = 已知
                continue
            子项 = self._找子项(父ID, 段, 用缓存=False)
            if 子项 is None:
                if not 创建:
                    raise FileNotFoundError(f"夸克路径不存在：{累积}")
                子ID = self._建目录并定位(父ID, 段, 累积)
            else:
                if not self._ctx().文件.是否目录(子项):
                    raise NotADirectoryError(f"夸克路径不是目录：{累积}")
                子ID = str(子项.get("fid") or "")
            if not 子ID:
                raise RuntimeError(f"夸克目录缺少 fid：{累积}")
            父ID = 子ID
            with self._缓存锁:
                self._目录缓存[累积] = 父ID
        return 父ID

    def _解析条目(self, path: str) -> dict | None:
        路径 = 规范(path)
        if 路径 == "/":
            return {"name": "/", "is_dir": True, "id": "0", "fid": "0",
                    "size": 0, "path": "/", "raw": {}}
        父目录, 名称 = 路径.rsplit("/", 1)
        父ID = self._解析目录(父目录 or "/", 创建=False)
        子项 = self._找子项(父ID, 名称, 用缓存=False)   # 点查读实时
        if 子项 is None:
            return None
        ctx = self._ctx()
        return {
            "name": 名称,
            "is_dir": ctx.文件.是否目录(子项),
            "id": str(子项.get("fid") or ""),
            "fid": str(子项.get("fid") or ""),
            "size": int(子项.get("size") or 0),
            "path": 路径,
            "raw": 子项,
        }

    def list(self, path: str) -> list[dict]:
        ctx = self._ctx()
        父ID = self._解析目录(path, 创建=False)
        return [ctx.条目(项, path) for 项 in self._列一层(父ID, 用缓存=False)]
        # 注：列目录本身也读实时（缓存只服务 _找子项 的批量匹配场景）

    def stat(self, path: str) -> dict | None:
        try:
            return self._解析条目(path)
        except FileNotFoundError:
            return None

    def ensure_dir(self, path: str) -> dict:
        目录ID = self._解析目录(path, 创建=True)
        self._失效列缓存()
        return {"id": 目录ID, "path": 规范(path)}

    # ---------------- 上传/下载 ----------------

    #: 服务端"暂时不可用"类错误（可重试）：2026-09-16 压力实测抓到
    #: `file is doloading[同名冲突]`——同一名字的文件正在服务端处理（刚传完/重复传/
    #: 并发同名），过几秒就好；不加处理会直接把任务判失败，表现就是"直链传输有时失败"。
    瞬时错误关键词 = (
        "doloading", "同名冲突", "正在处理", "处理中", "稍后", "重试",
        "try again", "timeout", "超时", "timed out", "connection",
        "reset", "busy", "系统繁忙", "internal", "内部错误",
        "502", "503", "504", "429", "too many", "rate limit", "限流",
    )

    @classmethod
    def _是瞬时错误(cls, 错误) -> bool:
        文本 = str(错误 or "").lower()
        return any(词.lower() in 文本 for 词 in cls.瞬时错误关键词)

    def _上传带重试(self, ctx, 上传源: Path, 目录ID: str, 适配进度,
                 尝试次数: int = 4):
        """上传遇到"同名冲突/正在处理"这类瞬时错误时退避重试。"""
        最后 = None
        for i in range(max(1, 尝试次数)):
            try:
                return ctx.上传.上传文件(
                    文件路径=str(上传源), 父目录ID=目录ID, 进度回调=适配进度)
            except Exception as e:  # noqa: BLE001
                最后 = e
                if i + 1 >= 尝试次数 or not self._是瞬时错误(e):
                    raise
                等待 = min(20.0, 2.0 * (2 ** i))
                self.记录(f"[夸克] 上传遇到瞬时错误，{等待:.0f}s 后重试"
                        f"（{i + 1}/{尝试次数}）：{e}", "warning")
                time.sleep(等待)
        raise 最后 if 最后 else RuntimeError("夸克上传失败")

    def upload(self, local_path: str, remote_dir: str, name: str,
               task_id: str, progress) -> dict:
        源 = Path(local_path)
        if not 源.is_file():
            raise FileNotFoundError(f"本地文件不存在：{local_path}")
        # 目标盘命名规避（当前 quark 无字符限制 → 原样返回；规则表见 v8_3/敏感词/命名规避.py）
        原名称 = name or 源.name
        规范名, 改名说明 = 规范命名("quark", 原名称)
        if 改名说明:
            self.记录(f"[quark] 命名规避：{改名说明}")
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
                elif not 临时别名.samefile(源):
                    raise FileExistsError(f"缓存目录存在同名文件：{临时别名}")
            except OSError:
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

        self._失效列缓存()
        # 上传后可见性确认（quark）
        # 列目录可能滞后 1–3 秒；不确认的话"上传完立刻跨盘传输"的源扫描会漏文件。
        # 顺带拿到**服务端实际落盘名**（夸克会把引号转成 &#39; 这类实体）。
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
            "skipped": bool((结果 or {}).get("秒传") or (结果 or {}).get("skipped")),
            "remote_id": str((结果 or {}).get("fid") or (结果 or {}).get("fileId") or ""),
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
        """给播放器用的直链（V8_3 新增）。

        与 download 的差别：**不下载**，只把签名 URL 与必需请求头交出去，
        由 libvlc 直接拉流播放（支持 Range，所以能拖进度）。
        """
        ctx = self._ctx()
        条目 = self._解析条目(remote_path)
        if 条目 is None:
            raise FileNotFoundError(f"夸克文件不存在：{remote_path}")
        if 条目.get("is_dir"):
            raise IsADirectoryError(f"夸克路径是目录，不能播放：{remote_path}")
        文件ID = str(条目.get("id") or "")
        if not 文件ID:
            raise RuntimeError(f"夸克文件缺少 fid：{remote_path}")
        信息 = ctx.下载.取下载信息(文件ID) or {}
        地址 = str(信息.get("download_url") or "")
        if not 地址:
            raise RuntimeError("夸克没有返回可播放直链（可能需要重新登录）")
        头 = {}
        try:
            头 = dict(ctx.下载._下载请求头() or {})
        except Exception:
            头 = {}
        return {"url": 地址, "headers": 头,
                "size": int(信息.get("size") or 条目.get("size") or 0),
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
            raise FileNotFoundError(f"夸克文件不存在：{remote_path}")
        if 条目.get("is_dir"):
            raise IsADirectoryError(f"夸克路径是目录，不能下载：{remote_path}")
        文件ID = str(条目.get("id") or "")
        if not 文件ID:
            raise RuntimeError(f"夸克文件缺少 fid：{remote_path}")

        目标 = Path(local_path)
        目标.parent.mkdir(parents=True, exist_ok=True)

        def 适配进度(已下载: int, 总量: int) -> None:
            if progress:
                progress("download", int(已下载 or 0), int(总量 or 0))

        下载信息 = {}
        try:
            下载信息 = ctx.下载.取下载信息(文件ID) or {}
        except Exception:
            下载信息 = {}
        总大小 = int(下载信息.get("size") or 条目.get("size") or 0)
        指纹 = str(下载信息.get("md5") or 条目.get("md5") or "")
        if 保守:
            self.记录("[夸克] 保守模式：跳过直链，直接用适配器原生下载")
        分段, 阈值 = 读取下载分段()
        地址 = str(下载信息.get("download_url") or "")
        已有 = 目标.stat().st_size if 目标.exists() else 0
        if 已有 and 总大小 and 已有 >= 总大小:
            已有 = 0            # 本地已完整：不需要再下
        if 已有:
            self.记录(f"[夸克] 断点续传：本地已有 {已有 / 1048576:.1f} MiB / "
                    f"{总大小 / 1048576:.1f} MiB")
        if 地址 and (分段 >= 2 and 总大小 >= max(8 * 1024 * 1024, 阈值)):
            try:
                头 = ctx.下载._下载请求头()
                if 多段下载(地址, 头, local_path, 总大小, 分段, 阈值,
                          进度=适配进度, 日志=self.记录, 续传=续传,
                          期望指纹=指纹):
                    self.记录(f"[夸克] 多段下载完成：{分段} 段 / {总大小} 字节")
                    return {"path": str(目标), "size": 目标.stat().st_size,
                            "bytes": 目标.stat().st_size}
            except Exception as e:
                self.记录(f"[夸克] 多段下载失败，回退单连接：{e}", "warning")
        if 续传 and not 保守:
            try:
                头 = ctx.下载._下载请求头()
                if 多段下载(地址, 头, local_path, 总大小, 分段, 阈值,
                          进度=适配进度, 日志=self.记录, 续传=续传,
                          期望指纹=指纹, 直链续传=True):
                    return {"path": str(目标), "size": 目标.stat().st_size,
                            "bytes": 目标.stat().st_size}
            except Exception as e:
                self.记录(f"[夸克] 直链续传下载失败，回退适配器下载：{e}", "warning")

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
            self.记录(f"[" + "夸克" + f"] 直链没取到数据，回退适配器下载器"
                    f"（原断点 {已有字节 / 1048576:.1f} MiB 会被覆盖）", "warning")

        try:
            结果 = ctx.下载.下载到文件(
                文件ID,
                保存目录=str(目标.parent),
                文件名=目标.name,
                进度回调=适配进度,
                覆盖=True,
            )
        except Exception as e:  # noqa: BLE001
            # 直链是**签名 URL**，会过期（也可能被限流）：这里重取一次链接再试，
            # 比直接把任务判失败更划算。
            if not (self._是瞬时错误(e) or any(
                    k in str(e) for k in ("403", "404", "签名", "expired", "过期"))):
                raise
            self.记录(f"[夸克] 直链下载失败，重取链接后重试一次：{e}", "warning")
            time.sleep(1.5)
            ctx = self._ctx()          # 新上下文 = 新的签名直链
            结果 = ctx.下载.下载到文件(
                文件ID,
                保存目录=str(目标.parent),
                文件名=目标.name,
                进度回调=适配进度,
                覆盖=True,
            )
        实际路径 = Path(str((结果 or {}).get("path") or 目标))
        if 实际路径.resolve() != 目标.resolve() and 实际路径.exists():
            目标.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(实际路径), str(目标))
        return {"path": str(目标), "size": 目标.stat().st_size,
                "bytes": 目标.stat().st_size}

    def delete(self, path: str) -> dict:
        ctx = self._ctx()
        条目 = self._解析条目(path)
        if 条目 is None:
            raise FileNotFoundError(f"夸克路径不存在：{path}")
        文件ID = str(条目.get("id") or "")
        if not 文件ID:
            raise RuntimeError(f"夸克路径缺少 fid：{path}")
        结果 = ctx.文件.删除文件([文件ID], 等待=False)
        if 条目.get("is_dir"):
            规范路径 = 规范(path)
            with self._缓存锁:
                for 键 in list(self._目录缓存):
                    if 键 == 规范路径 or 键.startswith(规范路径.rstrip("/") + "/"):
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

    # ==================== 统一登录协议 ====================
    #
    # 夸克适配器**只有两种真能力**（见 核心/认证/登录服务.py 模块头）：
    #   ① 扫码登录：取登录二维码() → 查扫码状态()/等扫描码() → 扫码登录()
    #   ② 导入 Cookie：手动Cookie登录()（内部先 校验Cookie，再 验证Cookie 探活）
    # 短信/邮箱/令牌/账号密码这四种适配器没有对应通道，一律明确回「不支持」。
    # 落盘只走适配器的 `登录服务.保存凭证()` → `全局凭证仓库`；
    # 桥**不碰** 数据/凭证.json（那是适配器自己的地盘）。

    #: 夸克没有的四种方式：说明里写清「可用什么」，不谎报支持
    未支持方式说明 = {
        "sms": "夸克适配器未提供短信登录（可用：扫码 / 导入Cookie）",
        "email": "夸克适配器未提供邮箱登录（可用：扫码 / 导入Cookie）",
        "token": ("夸克适配器未提供令牌登录：夸克的身份载体是 Cookie 而不是 "
                  "access/refresh token（可用：扫码 / 导入Cookie）"),
        "password": "夸克适配器未提供账号密码登录（可用：扫码 / 导入Cookie）",
    }

    def auth_caps(self) -> dict:
        return {
            "qrcode": {"支持": True,
                     "说明": "扫码登录（夸克 App 扫码授权，凭证落 数据/凭证.json）"},
            "cookie": {"支持": True,
                     "说明": ("导入 Cookie（浏览器 F12 复制 pan.quark.cn 的 "
                            "Cookie，必需字段 __kps/__uid）")},
            "sms": {"支持": False, "说明": self.未支持方式说明["sms"]},
            "email": {"支持": False, "说明": self.未支持方式说明["email"]},
            "token": {"支持": False, "说明": self.未支持方式说明["token"]},
            "password": {"支持": False, "说明": self.未支持方式说明["password"]},
        }

    # ---------------- 适配器没有的四种方式：回「不支持」而不是抛异常 ----------------
    # 基类里 login_sms_send/login_sms_verify/login_token 会抛 NotImplementedError，
    # 界面万一（没看 auth_caps 就）调过来，会变成一条"适配器调用失败"的异常；
    # 这里统一成和 auth_caps 一致的口径，异常不往界面上抛。

    def login_sms_send(self, 手机号: str) -> dict:
        return {"状态": "不支持", "消息": self.未支持方式说明["sms"]}

    def login_sms_verify(self, 会话: str, 验证码: str,
                         手机号: str = "") -> dict:
        return {"状态": "不支持", "消息": self.未支持方式说明["sms"]}

    def login_token(self, 访问令牌: str = "", 刷新令牌: str = "",
                    额外: dict | None = None) -> dict:
        return {"状态": "不支持", "消息": self.未支持方式说明["token"]}

    def login_email(self, 邮箱: str = "", 密码: str = "",
                    额外: dict | None = None) -> dict:
        return {"状态": "不支持", "消息": self.未支持方式说明["email"]}

    def login_password(self, 账号: str = "", 密码: str = "",
                       额外: dict | None = None) -> dict:
        return {"状态": "不支持", "消息": self.未支持方式说明["password"]}

    # ---------------- 退出登录 ----------------

    def 退出登录(self) -> dict:
        """清掉本机保存的夸克凭证（数据/凭证.json）。只动本地文件。"""
        m = _导入()   # 模块级函数，不是方法（写 self. 会 AttributeError）
        清除: list[str] = []
        try:
            仓库 = m["全局凭证仓库"]
            路径 = getattr(仓库, "文件路径", None)
            仓库.清空()
            if 路径 is not None:
                清除.append(str(路径))
        except Exception as e:  # noqa: BLE001
            return {"状态": "失败", "消息": f"清空夸克凭证失败：{e}",
                    "提示": "可在适配器原 GUI 里退出登录"}
        try:
            for 名字 in ("凭证.json", "令牌.json", "会话.json"):
                文件 = self.项目根 / "数据" / 名字
                if 文件.is_file():
                    文件.unlink()
                    清除.append(str(文件))
        except Exception as e:  # noqa: BLE001
            self.记录(f"[夸克] 清理残留凭证文件失败：{e}", "warning")
        try:
            self._本地.ctx = None      # 作废缓存的网络上下文（旧 Cookie 不再复用）
        except Exception:
            pass
        try:
            self._失效列缓存()
        except Exception:
            pass
        self.记录("[夸克] 已退出登录（本地凭证已清除）")
        信息 = {}
        try:
            信息 = self.account()
        except Exception:
            pass
        return {
            "状态": "成功",
            "消息": "已退出登录，本地凭证已清除",
            "清除": 清除,
            "账号": 信息,
        }

    # ---------------- 扫码 ----------------

    def login_qr_start(self) -> dict:
        """取夸克登录二维码。

        返回 `{"类型": "图片"|"链接", "会话": <扫码 token>, "有效期秒": …,
        "二维码链接"/"验证地址": …}`。夸克只给链接，图片是桥用本地 qrcode 库
        渲染的；渲染不出来就退化成「链接」型（界面自己画二维码）。
        """
        try:
            登录器 = self._新登录器()
        except Exception as e:
            return self.登录失败(
                f"夸克登录服务初始化失败：{type(e).__name__}: {e}",
                "检查适配器目录是否完整（核心/认证/登录服务.py）与 httpx 依赖")
        任务 = _扫码会话(登录器, self.记录)
        任务.启动()
        self.记录("[夸克] 正在向夸克申请扫码 token…")
        if not 任务.等二维码(35.0) and not 任务.完成.is_set():
            self._作废会话(任务, "获取二维码超时，已作废该扫码会话")
            return self.登录失败("获取夸克登录二维码超时（35 秒）",
                             "请检查网络后重试")
        if 任务.错误 or not 任务.链接:
            任务.收尾()
            return self.登录失败(
                f"获取夸克登录二维码失败：{任务.错误 or '夸克未返回二维码链接'}",
                "多半是网络不通或夸克接口有变，请稍后重试")

        会话 = self._从链接取token(任务.链接) or uuid.uuid4().hex[:12]
        任务.会话 = 会话
        self._登记会话(任务)
        有效期 = int(getattr(登录器, "扫码超时秒", 300.0) or 300)
        结果 = {
            "会话": 会话,
            "有效期秒": 有效期,
            "二维码链接": 任务.链接,
            "验证地址": 任务.链接,          # 手机浏览器打开可直接唤起夸克 App
        }
        图片 = _二维码PNG(任务.链接)
        if 图片:
            结果["类型"] = "图片"
            结果["图片base64"] = 图片
            结果["提示"] = (f"用夸克 App 扫描二维码授权（约 {有效期 // 60} "
                        "分钟内有效）；扫不到时也可复制「二维码链接」在"
                        "手机浏览器打开")
        else:
            结果["类型"] = "链接"
            结果["提示"] = ("桥进程未装 qrcode/Pillow，只能给出二维码链接："
                        "界面请把「二维码链接」当成二维码**内容**自己渲染成图，"
                        f"或让用户用夸克 App 打开该链接；约 {有效期 // 60} "
                        "分钟内有效")
        self.记录(f"[夸克] 二维码已就绪（会话 {会话}，类型 {结果['类型']}，"
                f"有效期 {有效期}s）")
        return 结果

    def login_qr_wait(self, 会话: str = "", 超时秒: float = 180.0) -> dict:
        """阻塞等待用户扫码确认（后台线程在跑适配器的 扫码登录()）。"""
        try:
            超时 = max(0.0, float(超时秒 or 0.0))
        except (TypeError, ValueError):
            超时 = 180.0
        任务 = self._找会话(会话)
        if 任务 is None:
            return self.登录失败("扫码会话不存在或已结束",
                             "请重新点「获取二维码」")
        self.记录(f"[夸克] 等待扫码（会话 {任务.会话}，最多 {超时:.0f} 秒）")
        任务.完成.wait(超时)

        if 任务.凭证:                   # 适配器已换到 Cookie 并 保存凭证()
            self._注销会话(任务)
            任务.收尾()
            self._登录态已变()
            self.记录(f"[夸克] 扫码登录成功（{len(任务.凭证.get('cookies') or {})} "
                    "个 Cookie，来源 qrcode）")
            return self.登录成功("扫码登录成功（夸克）",
                             "凭证已由适配器写入 数据/凭证.json")
        if 任务.错误:
            self._注销会话(任务)
            任务.收尾()
            提示 = ("已取消扫码登录" if 任务.已取消 else
                  "二维码约 5 分钟内有效，过期或被拒绝请重新获取二维码")
            return self.登录失败(f"扫码登录失败：{任务.错误}", 提示)

        结果 = self.登录失败(
            f"扫码等待超时（{超时:.0f} 秒内未完成授权）",
            "二维码仍可能在有效期内：可以再调一次等待；已过期就重新获取二维码")
        结果.update({"超时": True, "会话": 任务.会话})
        self.记录(f"[夸克] {结果['消息']}", "警告")
        return 结果

    def login_qr_cancel(self, 会话: str = "") -> dict:
        """真正停掉后台轮询（默认实现只是回一句话，轮询会继续跑到超时）。"""
        任务 = self._找会话(会话)
        if 任务 is None:
            return {"状态": "已取消", "消息": "没有正在进行的夸克扫码会话"}
        任务.取消()
        任务.完成.wait(3.0)
        self._注销会话(任务)
        任务.收尾()
        self.记录(f"[夸克] 已取消扫码会话 {任务.会话}")
        return {"状态": "已取消", "消息": "已取消扫码登录"}

    # ---------------- 导入 Cookie ----------------

    def login_cookie(self, 文本: str) -> dict:
        """粘贴浏览器里的夸克 Cookie 完成登录。

        走适配器 `登录服务.手动Cookie登录(验证=True, 保存=True)`：
        先 `校验Cookie`（必须有 __kps/__uid），再用 `验证Cookie`（GET capacity）
        向服务端确认真的可用，避免把过期 Cookie 当成功写进凭证文件。
        """
        文本 = str(文本 or "").strip()
        if not 文本:
            return self.登录失败(
                "Cookie 文本为空",
                "浏览器 F12 → 网络 → 任意 pan.quark.cn 请求 → 复制请求头"
                "里的 Cookie 整行")
        try:
            模块 = _导入()
            登录器 = 模块["登录服务"]()
        except Exception as e:
            return self.登录失败(
                f"夸克登录服务初始化失败：{type(e).__name__}: {e}",
                "检查适配器目录是否完整（核心/认证/登录服务.py）与 httpx 依赖")
        登录错误 = 模块["登录错误"]
        try:
            try:
                结果 = 登录器.手动Cookie登录(文本, 验证=True, 保存=True)
            except 登录错误 as e:
                return self.登录失败(
                    f"Cookie 登录失败：{e}",
                    "确认复制的是 pan.quark.cn 的完整 Cookie（必需 __kps、"
                    "__uid，下载还需要 __puus）且没有过期；也可以改用扫码登录")
            except Exception as e:
                return self.登录失败(
                    f"Cookie 登录失败：{type(e).__name__}: {e}",
                    "Cookie 至少要能解析出 __kps=…; __uid=… 这样的字段")
        finally:
            try:
                登录器.关闭()
            except Exception:
                pass
        self._登录态已变()
        self.记录(f"[夸克] Cookie 登录成功（{len(结果.get('cookies') or {})} 个"
                "字段，来源 manual）")
        return self.登录成功("Cookie 登录成功（夸克）",
                         "凭证已由适配器写入 数据/凭证.json")

    # ---------------- 登录内部工具 ----------------

    def _新登录器(self):
        """新建一个夸克登录服务（用一次就关，避免 httpx 客户端常驻）。"""
        return _导入()["登录服务"]()

    @staticmethod
    def _从链接取token(链接: str) -> str:
        """二维码链接里的 `token=` 就是扫码句柄（夸克 CAS 的 qr token）。"""
        try:
            查询 = urllib.parse.parse_qs(urllib.parse.urlparse(链接).query)
            return str((查询.get("token") or [""])[0])
        except Exception:
            return ""

    def _登记会话(self, 任务: _扫码会话) -> None:
        """只保留最新的扫码会话；上一个没结束的直接作废（它的码已过期）。"""
        with self._扫码锁:
            旧的 = [v for v in self._扫码表.values() if v is not 任务]
            self._扫码表.clear()
            self._扫码表[任务.会话] = 任务
            self._最近会话 = 任务.会话
        for 旧 in 旧的:
            if 旧.完成.is_set():
                continue
            self._作废会话(旧, f"作废上一个扫码会话 {旧.会话}")

    def _作废会话(self, 任务: _扫码会话, 原因: str = "") -> None:
        """停掉后台轮询并择机关掉它的 httpx 客户端（不等它跑满 5 分钟）。"""
        if not 任务.完成.is_set():
            任务.取消()
        if 原因:
            self.记录(f"[夸克] {原因}", "警告")
        threading.Thread(target=self._等它收尾, args=(任务,), daemon=True,
                         name="夸克扫码收尾").start()

    @staticmethod
    def _等它收尾(任务: _扫码会话) -> None:
        任务.完成.wait(5.0)
        任务.收尾()

    def _找会话(self, 会话: str = "") -> _扫码会话 | None:
        会话 = str(会话 or "").strip()
        with self._扫码锁:
            if 会话:
                return self._扫码表.get(会话)
            return self._扫码表.get(self._最近会话)

    def _注销会话(self, 任务: _扫码会话) -> None:
        with self._扫码锁:
            if self._扫码表.get(任务.会话) is 任务:
                self._扫码表.pop(任务.会话, None)
            if self._最近会话 == 任务.会话:
                剩余 = list(self._扫码表)
                self._最近会话 = 剩余[-1] if 剩余 else ""

    def _登录态已变(self) -> None:
        """登录成功后作废缓存：各线程带旧 Cookie 的上下文 + 目录/列表缓存。"""
        self._上下文代 += 1
        self._失效列缓存()
        with self._缓存锁:
            self._目录缓存 = {"": "0", "/": "0"}
        self.记录("[夸克] 登录态已更新，已作废旧的目录/会话缓存")
