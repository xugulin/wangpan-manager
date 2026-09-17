"""后端公共基类：统一方法签名与进度事件约定。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Optional


class 后端基类:
    """所有后端都实现这些方法；工作进程通过 JSON-RPC 暴露给 V8_3。"""

    def __init__(self, 项目根: str,
                 发事件: Optional[Callable[..., None]] = None,
                 日志: Optional[Callable[[str, str], None]] = None):
        self.项目根 = Path(项目根).expanduser().resolve()
        self._发事件 = 发事件 or (lambda *a, **k: None)
        self._日志 = 日志 or (lambda level, msg: None)

    def 记录(self, 消息: str, 级别: str = "info") -> None:
        self._日志(级别, 消息)

    def account(self) -> dict:
        raise NotImplementedError

    def list(self, path: str) -> list[dict]:
        raise NotImplementedError

    def stat(self, path: str):
        raise NotImplementedError

    def ensure_dir(self, path: str) -> dict:
        raise NotImplementedError

    def upload(self, local_path: str, remote_dir: str, name: str,
               task_id: str, progress) -> dict:
        raise NotImplementedError

    def play_link(self, remote_path: str) -> dict:
        """给播放器用的直链（V8_3 新增；不支持的后端返回 不支持）。

        返回 ``{"url":…, "headers":{…}, "size":int, "name":str, "range":bool}``。
        """
        raise NotImplementedError("该后端还没实现 play_link（不能直接播放）")

    def download(self, remote_path: str, local_path: str,
                 task_id: str, progress, 续传: bool = True,
                 保守: bool = False) -> dict:
        raise NotImplementedError

    def delete(self, path: str) -> dict:
        raise NotImplementedError

    def close(self) -> None:
        pass

    # ==================== 统一登录协议 ====================
    #
    # V8_3 把登录方式统一成 6 种：扫码 / 导入Cookie / 短信 / 邮箱 / 令牌 / 账号密码。
    # 各适配器**按其核心真实支持的能力**实现（不支持的返回 支持=False + 原因），
    # 界面据此启用/禁用对应页签。
    #
    # 所有 login_* 方法：
    #   * 成功返回 {"状态": "成功", "消息": "...", "账号": {account() 的结果}}
    #   * 失败返回 {"状态": "失败", "消息": "..."}（不要抛异常给界面）
    #   * 可选："提示" 字段给用户看的下一步说明
    #
    # 只允许调用适配器自己的认证/仓库模块落盘，不复制、不改写适配器源码。

    #: 六种标准登录方式（键固定，界面按这个顺序显示）
    登录方式 = ("qrcode", "cookie", "sms", "email", "token", "password")

    def auth_caps(self) -> dict:
        """返回各登录方式的能力：``{方式: {"支持": bool, "说明": str}}``。"""
        说明 = {
            "qrcode": "扫码登录",
            "cookie": "导入 Cookie",
            "sms": "短信登录",
            "email": "邮箱登录",
            "token": "令牌登录",
            "password": "账号密码登录",
        }
        return {键: {"支持": False,
                   "说明": f"{说明[键]}（{type(self).__name__} 未实现）"}
                for 键 in self.登录方式}

    def login_qr_start(self) -> dict:
        """开始扫码登录。

        返回 ``{"类型": "图片"|"设备码", ...}``：

        * 图片：``{"图片base64": "<PNG 的 base64>", "提示": "..."}``
        * 设备码：``{"验证地址": "https://...", "用户码": "ABCD-1234"}``

        另外可带 ``会话``（不透明句柄，交给 login_qr_wait）与 ``有效期秒``。
        """
        raise NotImplementedError

    def login_qr_wait(self, 会话: str = "", 超时秒: float = 180.0) -> dict:
        """阻塞等待扫码结果（用户在手机/浏览器上确认）。"""
        raise NotImplementedError

    def login_qr_cancel(self, 会话: str = "") -> dict:
        return {"状态": "已取消", "消息": "已取消扫码登录"}

    def login_cookie(self, 文本: str) -> dict:
        """粘贴 Cookie / BDUSS / STOKEN 等文本凭证完成登录。"""
        raise NotImplementedError

    def login_sms_send(self, 手机号: str) -> dict:
        """发送短信验证码。成功后返回 ``{"会话": "...", "提示": "..."}``。"""
        raise NotImplementedError

    def login_sms_verify(self, 会话: str, 验证码: str,
                         手机号: str = "") -> dict:
        """校验短信验证码并完成登录。"""
        raise NotImplementedError

    def login_token(self, 访问令牌: str = "", 刷新令牌: str = "",
                    额外: dict | None = None) -> dict:
        """直接写入 access/refresh 令牌完成登录（光鸭这类令牌型网盘）。"""
        raise NotImplementedError

    def login_password(self, 账号: str = "", 密码: str = "",
                       额外: dict | None = None) -> dict:
        """账号密码登录（三家适配器目前都未提供，默认返回不支持）。"""
        return {"状态": "不支持",
                "消息": "该网盘适配器未提供账号密码登录，请用扫码/Cookie/短信登录"}

    def login_email(self, 邮箱: str = "", 密码: str = "",
                    额外: dict | None = None) -> dict:
        """邮箱登录（三家适配器目前都未提供，默认返回不支持）。"""
        return {"状态": "不支持",
                "消息": "该网盘适配器未提供邮箱登录，请用扫码/Cookie/短信登录"}

    # 便捷：把一次成功登录整理成统一结构
    def 登录成功(self, 消息: str = "登录成功", 提示: str = "",
                 额外: dict | None = None) -> dict:
        结果 = {"状态": "成功", "消息": 消息}
        if 提示:
            结果["提示"] = 提示
        if 额外:
            结果.update(额外)
        try:
            结果["账号"] = self.account()
        except Exception:
            pass
        return 结果

    def 登录失败(self, 消息: str, 提示: str = "") -> dict:
        结果 = {"状态": "失败", "消息": str(消息)}
        if 提示:
            结果["提示"] = 提示
        return 结果


def 规范(路径: str) -> str:
    """本地/假网盘使用的安全相对路径。"""
    文本 = str(路径 or "/").replace("\\", "/").strip()
    段 = []
    for s in 文本.split("/"):
        if not s or s == ".":
            continue
        if s == "..":
            raise ValueError(f"非法路径段 '..'：{路径!r}")
        段.append(s)
    return "/" + "/".join(段) if 段 else "/"


def 多段下载(直链: str, 请求头: dict, 目标路径: str, 总大小: int,
          分段数: int, 阈值: int, 进度=None, 日志=None,
          续传: bool = True, 期望指纹: str = "",
          直链续传: bool = False) -> bool:
    """下载入口（可续传）。

    * 默认：走 :func:`续传多段下载`（并行 Range + 断点续传）；
    * ``直链续传=True``：走 :func:`续传单连接下载`（单连接 + Range 追加），
      用于小文件或"服务端不支持多段"的回退路径；
    * ``续传=False``：退回旧的一次性多段下载（失败即重来）。

    返回 True 表示文件已完整落盘；False 表示失败/不适用（调用方决定回退）。
    """
    if 直链续传:
        return 续传单连接下载(直链, 请求头, 目标路径, 总大小,
                          进度=进度, 日志=日志, 期望指纹=期望指纹)
    if 续传:
        return 续传多段下载(直链, 请求头, 目标路径, 总大小, 分段数, 阈值,
                         进度=进度, 日志=日志, 期望指纹=期望指纹)
    return _一次性多段下载(直链, 请求头, 目标路径, 总大小, 分段数, 阈值,
                     进度=进度, 日志=日志)


def _一次性多段下载(直链: str, 请求头: dict, 目标路径: str, 总大小: int,
              分段数: int, 阈值: int, 进度=None, 日志=None) -> bool:
    """旧的并行 Range 下载（不续传，失败即清理重来）。保留给 续传=False 的场景。"""
    import os
    import threading
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from pathlib import Path

    try:
        import httpx
    except Exception:
        return False

    if 分段数 < 2 or 总大小 <= 0 or 总大小 < max(1, 阈值):
        return False
    目标 = Path(目标路径)
    目标.parent.mkdir(parents=True, exist_ok=True)
    段数 = max(2, min(int(分段数), 16))
    每段 = max(4 * 1024 * 1024, (总大小 + 段数 - 1) // 段数)
    区间: list[tuple[int, int]] = []
    起 = 0
    while 起 < 总大小 and len(区间) < 段数:
        止 = min(总大小 - 1, 起 + 每段 - 1)
        区间.append((起, 止))
        起 = 止 + 1
    临时 = [目标.with_name(目标.name + f".v8_3part{i}") for i in range(len(区间))]
    锁 = threading.Lock()
    已下载 = [0]
    客户端 = httpx.Client(timeout=httpx.Timeout(
        connect=15.0, read=1800.0, write=60.0, pool=60.0),
        follow_redirects=True, http2=False)

    def 清理() -> None:
        for p in 临时:
            try:
                p.unlink(missing_ok=True)
            except Exception:
                pass
        try:
            客户端.close()
        except Exception:
            pass

    try:
        # 先探测第一段，服务端不支持 Range 直接回退
        def 下载一段(i: int) -> tuple[int, int]:
            起, 止 = 区间[i]
            头 = dict(请求头 or {})
            头["Range"] = f"bytes={起}-{止}"
            期望 = 止 - 起 + 1
            with 客户端.stream("GET", 直链, headers=头) as 响应:
                if 响应.status_code != 206:
                    raise RuntimeError(f"服务端不支持 Range（HTTP {响应.status_code}）")
                已 = 0
                with 临时[i].open("wb") as f:
                    for 块 in 响应.iter_bytes(chunk_size=1024 * 1024):
                        f.write(块)
                        已 += len(块)
                        with 锁:
                            已下载[0] += len(块)
                            if 进度:
                                try:
                                    进度(已下载[0], 总大小)
                                except Exception:
                                    pass
                if 已 != 期望:
                    raise RuntimeError(f"分段 {i} 字节数不符：{已} != {期望}")
            return 起, 止

        # 探测
        下载一段(0)
        if len(区间) > 1:
            with ThreadPoolExecutor(max_workers=min(8, len(区间) - 1),
                                    thread_name_prefix="多段下载") as 池:
                未来 = [池.submit(下载一段, i) for i in range(1, len(区间))]
                for 未 in as_completed(未来):
                    未.result()
        # 合并
        with 目标.open("wb") as 输出:
            for p in 临时:
                with p.open("rb") as 输入:
                    while True:
                        块 = 输入.read(1024 * 1024)
                        if not 块:
                            break
                        输出.write(块)
        if 目标.stat().st_size != 总大小:
            raise RuntimeError("多段下载合并后大小不符")
        清理()
        return True
    except Exception as e:
        if 日志:
            try:
                日志(f"多段下载回退：{e}")
            except Exception:
                pass
        清理()
        try:
            目标.unlink(missing_ok=True)
        except Exception:
            pass
        return False


# ==================== 目标盘命名规避（2026-09-16 新增） ====================


def 规范命名(网盘: str, 名称: str) -> tuple[str, str]:
    """把文件名改成目标网盘能接受的形态。返回 (新名称, 说明)。

    各家网盘的合法字符集不同（实测百度禁止 ``\\ / : * ? " < > |`` 与不可见字符，
    且 emoji 会让接口直接报错），而这类限制只在**真实上传**时暴露
    （秒传命中会绕过校验，所以"重传一次居然成功"是假象）。
    规则表放在 ``v8_3/敏感词/命名规避.py``，由子进程客户端通过 PYTHONPATH 传入。
    """
    名称 = str(名称 or "")
    if not 名称:
        return 名称, ""
    try:
        import importlib
        模块 = importlib.import_module("v8_3.敏感词.命名规避")
    except Exception:
        return 名称, ""
    try:
        需要, 原因 = 模块.需要规避(网盘, 名称)
        if not 需要:
            return 名称, ""
        新名称, _ = 模块.规避名称(网盘, 名称)
        return (新名称 or 名称), f"{原因}（原名 {名称}）"
    except Exception:
        return 名称, ""


# ==================== 上传后可见性确认（2026-09-16 新增） ====================

#: 有些网盘上传接口返回成功，但文件在"列目录"里要 1–3 秒后才出现
#: （实测夸克：12MB 文件上传成功后立刻列目录看不到 → 紧接着做跨盘传输的
#: "扫描源"就会漏掉这个文件，表现为"少传了文件"却没有任何报错）。
确认可见尝试 = 5
确认可见间隔 = 1.2


def 名称等价(甲: str, 乙: str) -> bool:
    """判断两个文件名是否"其实是一个"（含服务端做的 HTML 实体转义）。"""
    if 甲 == 乙:
        return True
    import html
    try:
        return html.unescape(甲) == html.unescape(乙)
    except Exception:
        return False


def 确认可见(列目录, 名称: str, 尝试: int = 确认可见尝试,
          间隔: float = 确认可见间隔, 日志=None) -> str | None:
    """轮询父目录，确认刚上传的文件已经可见；返回**实际落盘名**。

    * 找到同名（或服务端等价名，如 `&#39;` 转义）→ 返回实际名字；
    * 一直找不到 → 返回 None 并打日志（不抛错：文件很可能只是还在索引，
      抛错反而会把一次成功的上传判成失败）。
    """
    import time as _t
    if not 名称:
        return 名称
    for i in range(max(1, 尝试)):
        if i:
            _t.sleep(间隔)
        try:
            条目 = 列目录() or []
        except Exception:
            continue
        for 项 in 条目:
            实际 = getattr(项, "name", None)
            if 实际 is None and isinstance(项, dict):
                实际 = 项.get("name") or 项.get("file_name") or 项.get("fileName")
            if 实际 and 名称等价(str(实际), 名称):
                if str(实际) != 名称 and 日志:
                    try:
                        日志(f"上传后落盘名与请求名不同：{名称} → {实际}"
                          "（服务端改名，已记录）")
                    except Exception:
                        pass
                return str(实际)
    if 日志:
        try:
            日志(f"上传成功但 {尝试} 次轮询仍未见 {名称}（可能仍在索引）", "warning")
        except Exception:
            pass
    return None


# ==================== 断点续传（2026-09-16 新增） ====================
#
# 背景：直链（签名 URL）下载在网络抖动/超时/被限流时会中途断开。原实现
# （多段下载 + 单连接回退）一旦失败就把临时文件删掉，重试只能**从 0 重下**，
# 大文件在弱网下几乎永远传不完。
#
# 现在的做法：
#   * 分段下载把每段落盘为 `<目标>.v8_3partN`，并把进度写进
#     `<目标>.v8_3resume.json`（总大小 + 每段区间/已完成字节 + 期望指纹）；
#   * 失败时**保留** part 文件与状态，下一次调用按 Range 从断点续传；
#   * 单连接回退也支持续传：目标文件已有一部分时用 `Range: bytes=<已有>-` 追加；
#   * 任务结束（完成/失败/取消）由上层调用 `清理续传文件()` 收尾。


def _测试用断流阈值() -> int:
    """**仅供测试**：模拟"直链不停失败"，用来验证断点续传与**兜底**链路。

    * ``V8_3_DL_ABORT_AFTER=<字节数>``：下载到该字节数就断开；
    * ``V8_3_DL_ABORT_TIMES=<次数>``：前 N 次调用都断（默认 1，即一次性）。

    不设置时永远返回 0，对正常运行没有任何影响。
    """
    import os as _os
    try:
        次数上限 = max(1, int(_os.environ.get("V8_3_DL_ABORT_TIMES") or 1))
    except Exception:
        次数上限 = 1
    if _测试用断流阈值.已触发 >= 次数上限:
        return 0
    try:
        阈值 = int(_os.environ.get("V8_3_DL_ABORT_AFTER") or 0)
    except Exception:
        阈值 = 0
    return 阈值 if 阈值 > 0 else 0


_测试用断流阈值.已触发 = 0


def _检查测试断流(已写总字节: int) -> None:
    阈值 = _测试用断流阈值()
    if 阈值 and 已写总字节 >= 阈值:
        _测试用断流阈值.已触发 += 1
        raise RuntimeError(
            f"测试注入断流（V8_3_DL_ABORT_AFTER={阈值}）："
            f"已写 {已写总字节} 字节后主动断开")


def 续传状态路径(目标路径) -> "object":
    from pathlib import Path
    p = Path(目标路径)
    return p.with_name(p.name + ".v8_3resume.json")


def 读取续传状态(目标路径, 总大小: int, 期望指纹: str = "") -> dict:
    """读续传状态；不存在/总大小不符/指纹不符时返回空状态（代表重新开始）。"""
    import json
    from pathlib import Path
    状态路径 = 续传状态路径(目标路径)
    try:
        状态 = json.loads(Path(状态路径).read_text(encoding="utf-8"))
    except Exception:
        return {}
    if int(状态.get("总大小") or -1) != int(总大小):
        return {}
    if 期望指纹 and str(状态.get("指纹") or "") != str(期望指纹):
        return {}
    return 状态


def 写续传状态(目标路径, 总大小: int, 段: list[dict], 指纹: str = "") -> None:
    import json
    from pathlib import Path
    try:
        Path(续传状态路径(目标路径)).write_text(json.dumps({
            "总大小": int(总大小), "指纹": str(指纹 or ""), "段": 段,
        }, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def 清理续传文件(目标路径) -> None:
    """删掉 part 临时文件与续传状态（任务结束/成功后调用）。"""
    from pathlib import Path
    目标 = Path(目标路径)
    for p in 目标.parent.glob(目标.name + ".v8_3part*"):
        try:
            p.unlink(missing_ok=True)
        except Exception:
            pass
    try:
        Path(续传状态路径(目标路径)).unlink(missing_ok=True)
    except Exception:
        pass


def 已有续传字节(目标路径, 总大小: int, 期望指纹: str = "") -> int:
    """这次重试能复用的字节数（用于日志/统计；0 表示要重头下）。"""
    状态 = 读取续传状态(目标路径, 总大小, 期望指纹)
    if not 状态:
        return 0
    return sum(int(x.get("已") or 0) for x in (状态.get("段") or []))


def 续传多段下载(直链: str, 请求头: dict, 目标路径: str, 总大小: int,
             分段数: int, 阈值: int, 进度=None, 日志=None,
             期望指纹: str = "") -> bool:
    """可续传的并行 Range 下载。

    成功返回 True；服务端不支持 Range 等硬失败返回 False（调用方回退单连接，
    回退路径同样可续传）。中途断流时**保留** part 文件，下次调用从断点继续。
    """
    import threading
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from pathlib import Path

    try:
        import httpx
    except Exception:
        return False

    if 分段数 < 2 or 总大小 <= 0 or 总大小 < max(1, 阈值):
        return False
    目标 = Path(目标路径)
    目标.parent.mkdir(parents=True, exist_ok=True)
    段数 = max(2, min(int(分段数), 16))
    每段 = max(4 * 1024 * 1024, (总大小 + 段数 - 1) // 段数)
    区间: list[tuple[int, int]] = []
    起 = 0
    while 起 < 总大小 and len(区间) < 段数:
        止 = min(总大小 - 1, 起 + 每段 - 1)
        区间.append((起, 止))
        起 = 止 + 1
    临时 = [目标.with_name(目标.name + f".v8_3part{i}") for i in range(len(区间))]

    状态 = 读取续传状态(目标路径, 总大小, 期望指纹)
    旧段 = {int(x.get("i")): x for x in (状态.get("段") or [])
           if isinstance(x, dict) and x.get("i") is not None}
    段状态: list[dict] = []
    for i, (起_, 止_) in enumerate(区间):
        part = 临时[i]
        已 = 0
        if i in 旧段 and int(旧段[i].get("起") or -1) == 起_ \
                and int(旧段[i].get("止") or -1) == 止_:
            已 = min(int(旧段[i].get("已") or 0), 止_ - 起_ + 1)
        if part.exists():
            实有 = part.stat().st_size
            已 = 实有 if 已 == 0 else min(已, 实有)
            if 已 < 实有:        # 文件比记录的长：以文件为准
                已 = 实有
        if 已 >= 止_ - 起_ + 1:
            已 = 止_ - 起_ + 1
        elif 已:
            已 = max(0, 已)
        段状态.append({"i": i, "起": 起_, "止": 止_, "已": 已})

    复用 = sum(int(x["已"]) for x in 段状态)
    if 复用 and 日志:
        日志(f"续传：复用已下载 {复用 / 1048576:.1f} MiB / "
             f"{总大小 / 1048576:.1f} MiB，续传 {len(区间)} 段")

    锁 = threading.Lock()
    已下载 = [复用]
    客户端 = httpx.Client(timeout=httpx.Timeout(
        connect=15.0, read=1800.0, write=60.0, pool=60.0),
        follow_redirects=True, http2=False)

    def 存状态() -> None:
        写续传状态(目标路径, 总大小, 段状态, 期望指纹)

    def 下载一段(i: int) -> None:
        起_, 止_ = 区间[i]
        已 = int(段状态[i]["已"])
        期望总 = 止_ - 起_ + 1
        if 已 >= 期望总:
            return
        头 = dict(请求头 or {})
        头["Range"] = f"bytes={起_ + 已}-{止_}"
        with 客户端.stream("GET", 直链, headers=头) as 响应:
            if 响应.status_code != 206:
                raise RuntimeError(f"服务端不支持 Range（HTTP {响应.status_code}）")
            模式 = "ab" if 已 else "wb"
            with 临时[i].open(模式) as f:
                for 块 in 响应.iter_bytes(chunk_size=256 * 1024):
                    # 防御：服务端若忽略了 Range 末端，会把多出的字节一起发过来。
                    # 这里按区间封顶写入，宁可丢弃多余数据，也不能写出错位的分片。
                    if 已 + len(块) > 期望总:
                        块 = 块[:期望总 - 已]
                        if not 块:
                            break
                    f.write(块)
                    已 += len(块)
                    with 锁:
                        段状态[i]["已"] = 已
                        已下载[0] += len(块)
                        if 进度:
                            try:
                                进度(已下载[0], 总大小)
                            except Exception:
                                pass
                    # 周期性落盘：中途断流也要留下真实断点
                    if 已 % (4 * 1024 * 1024) < len(块):
                        存状态()
                    _检查测试断流(已下载[0])
        实有 = 临时[i].stat().st_size
        if 实有 != 期望总:
            # 这一段落盘长度不对：把记录回退到真实长度，交给下次续传
            with 锁:
                段状态[i]["已"] = min(实有, 期望总)
            存状态()
            raise RuntimeError(f"分段 {i} 字节数不符：{实有} != {期望总}")
        with 锁:
            段状态[i]["已"] = 期望总
        存状态()

    try:
        # 先探测第一段（不合格就整体回退；此时不删 part，留给下层续传判断）
        下载一段(0)
        if len(区间) > 1:
            with ThreadPoolExecutor(max_workers=min(8, len(区间) - 1),
                                    thread_name_prefix="多段下载") as 池:
                未来 = [池.submit(下载一段, i) for i in range(1, len(区间))]
                for 未 in as_completed(未来):
                    未.result()
        # 合并（先写临时目标再原子替换，避免半成品覆盖旧目标）
        合并中 = 目标.with_name(目标.name + ".v8_3merge")
        with 合并中.open("wb") as 输出:
            for p in 临时:
                with p.open("rb") as 输入:
                    while True:
                        块 = 输入.read(1024 * 1024)
                        if not 块:
                            break
                        输出.write(块)
        if 合并中.stat().st_size != 总大小:
            raise RuntimeError("多段下载合并后大小不符")
        合并中.replace(目标)
        清理续传文件(目标路径)
        return True
    except Exception as e:
        try:
            # 关键：断流时把"每段已下多少"落盘，下一次才能真正续上
            for i, part in enumerate(临时):
                try:
                    if part.exists():
                        段状态[i]["已"] = min(part.stat().st_size,
                                          区间[i][1] - 区间[i][0] + 1)
                except Exception:
                    pass
            存状态()
        except Exception:
            pass
        if 日志:
            try:
                断点 = sum(int(x["已"]) for x in 段状态)
                日志(f"多段下载中断，已保留断点 {断点 / 1048576:.1f} MiB：{e}")
            except Exception:
                pass
        return False
    finally:
        try:
            客户端.close()
        except Exception:
            pass


def 续传单连接下载(直链: str, 请求头: dict, 目标路径: str, 总大小: int,
               进度=None, 日志=None, 期望指纹: str = "") -> bool:
    """单连接下载（可续传）：已下载部分用 Range 追加。

    服务端不支持 Range（HTTP 200）时**从头重下**（先截断目标文件），
    避免把新内容接在旧内容后面造成坏文件。
    """
    from pathlib import Path

    try:
        import httpx
    except Exception:
        return False

    目标 = Path(目标路径)
    目标.parent.mkdir(parents=True, exist_ok=True)
    已有 = 目标.stat().st_size if 目标.exists() else 0
    if 总大小 and 已有 > 总大小:      # 比源还大：肯定不对，重下
        已有 = 0
    if 总大小 and 已有 == 总大小:
        return True
    客户端 = httpx.Client(timeout=httpx.Timeout(
        connect=15.0, read=1800.0, write=60.0, pool=60.0),
        follow_redirects=True, http2=False)
    头 = dict(请求头 or {})
    模式 = "wb"
    if 已有:
        头["Range"] = f"bytes={已有}-"
        if 日志:
            日志(f"续传：单连接从 {已有 / 1048576:.1f} MiB 继续")
        try:
            with 客户端.stream("GET", 直链, headers=头) as 响应:
                if 响应.status_code == 206:
                    模式 = "ab"
                else:
                    # 不支持 Range：只能重下
                    已有 = 0
                    模式 = "wb"
                if 模式 == "ab":
                    已 = 已有
                    with 目标.open("ab") as f:
                        # chunk_size=None：服务端来多少写多少。用固定大 chunk 时
                        # httpx 会先攒满再吐，断流会导致"一个字节都没落盘"，
                        # 断点续传就白做了（实测踩到）。
                        for 块 in 响应.iter_bytes(chunk_size=None):
                            f.write(块)
                            已 += len(块)
                            _检查测试断流(已)
                            if 进度:
                                try:
                                    进度(已, 总大小 or 已)
                                except Exception:
                                    pass
                    if 总大小 and 目标.stat().st_size != 总大小:
                        raise RuntimeError(
                            f"续传后大小不符：{目标.stat().st_size} != {总大小}")
                    return True
        except Exception as e:
            if 日志:
                try:
                    日志(f"单连接续传中断：{e}")
                except Exception:
                    pass
            return False
        finally:
            pass
    try:
        with 客户端.stream("GET", 直链, headers=头) as 响应:
            if 响应.status_code not in (200, 206):
                raise RuntimeError(f"HTTP {响应.status_code}")
            已 = 0
            with 目标.open(模式) as f:
                for 块 in 响应.iter_bytes(chunk_size=None):
                    f.write(块)
                    已 += len(块)
                    _检查测试断流(已)
                    if 进度:
                        try:
                            进度(已, 总大小 or 已)
                        except Exception:
                            pass
        if 总大小 and 目标.stat().st_size != 总大小:
            raise RuntimeError(f"下载后大小不符：{目标.stat().st_size} != {总大小}")
        return True
    except Exception as e:
        if 日志:
            try:
                留 = 目标.stat().st_size if 目标.exists() else 0
                日志(f"单连接下载中断，已保留断点 {留 / 1048576:.1f} MiB：{e}")
            except Exception:
                pass
        return False
    finally:
        try:
            客户端.close()
        except Exception:
            pass


def 读取下载分段() -> tuple[int, int]:
    """从环境变量读取 V8_3 配置的下载分段数与阈值。"""
    import os
    try:
        分段 = int(os.environ.get("V8_3_DL_SEGMENTS") or 0)
    except Exception:
        分段 = 0
    try:
        阈值 = int(os.environ.get("V8_3_DL_THRESHOLD") or 0)
    except Exception:
        阈值 = 0
    return 分段, 阈值
