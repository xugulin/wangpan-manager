"""子进程适配器客户端：行式 JSON 协议 + 线程安全调用。"""

from __future__ import annotations

import itertools
import json
import os
import subprocess
import threading
import time
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
from pathlib import Path
from typing import Any, Callable, Optional

from .适配器 import 云盘适配器, 适配器规格
from .错误 import 适配器错误, 认证错误
from .模型 import 账号信息, 远端条目


class _进度登记:
    def __init__(self, 客户端: "子进程适配器", 任务ID: str):
        self._客户端 = 客户端
        self._任务ID = 任务ID

    def __enter__(self):
        return self

    def 注册(self, 回调: Callable[[str, int, int], None]):
        self._客户端._进度回调[self._任务ID] = 回调

    def __exit__(self, exc_type, exc, tb):
        self._客户端._进度回调.pop(self._任务ID, None)


def _绝对本地路径(路径) -> str:
    """把交给桥进程的**本地路径转成绝对路径**。

    为什么必须有这道转换（实测踩坑）：桥进程是用 ``cwd=v8_3/桥`` 启动的，所以
    ``"数据/字幕/x.srt"`` 这种相对路径会被**解析到桥目录里面**去 —— 下载"成功"
    返回、文件却落在 ``v8_3/桥/数据/字幕/x.srt``，调用方怎么也找不到（而且不报错）。
    绝对化之后，不管桥进程 cwd 是什么，读写都落在同一个地方。
    """
    try:
        return str(Path(路径).expanduser().resolve())
    except Exception:  # noqa: BLE001 - 极端情况下保持原样，别把功能弄断
        return str(路径)


class 子进程适配器(云盘适配器):
    """在一个独立 Python 子进程中运行的适配器。"""

    def __init__(self, 规格: 适配器规格,
                 日志回调: Optional[Callable[[str, str], None]] = None):
        self.规格 = 规格
        self.类型 = 规格.类型
        self._日志回调 = 日志回调
        self._进程: Optional[subprocess.Popen] = None
        self._请求ID = itertools.count(1)
        self._等待表: dict[int, Future] = {}
        self._锁 = threading.RLock()
        self._进度回调: dict[str, Callable[[str, int, int], None]] = {}
        self._stderr线程: Optional[threading.Thread] = None
        self._stdout线程: Optional[threading.Thread] = None
        self._已关闭 = False

    # ---------------- 身份 ----------------

    @property
    def 标识(self) -> str:
        """适配器实例标识（同一个类型可以有多个实例）。"""
        return self.规格.标识

    @property
    def 显示名(self) -> str:
        return self.规格.显示名

    # ---------------- 生命周期 ----------------

    @property
    def 工作进程脚本(self) -> Path:
        return Path(__file__).resolve().parents[1] / "桥" / "工作进程.py"

    def 启动(self) -> None:
        if self._进程 is not None and self._进程.poll() is None:
            return
        self.规格.校验()
        工作脚本 = self.工作进程脚本
        if not 工作脚本.is_file():
            raise FileNotFoundError(f"找不到桥接工作进程：{工作脚本}")
        环境 = os.environ.copy()
        # 让桥进程也能 import v8_3 自己的模块（命名规避规则表等）
        项目根 = str(Path(__file__).resolve().parents[2])
        旧路径 = 环境.get("PYTHONPATH") or ""
        环境["PYTHONPATH"] = (项目根 if not 旧路径
                          else 项目根 + os.pathsep + 旧路径)
        环境["PYTHONIOENCODING"] = "utf-8"
        环境["PYTHONUNBUFFERED"] = "1"
        环境["PYTHONUTF8"] = "1"
        额外 = self.规格.额外参数 or {}
        环境["V8_3_DL_SEGMENTS"] = str(额外.get("下载分段", 0))
        环境["V8_3_DL_THRESHOLD"] = str(额外.get("下载阈值", 0))
        命令 = [
            self.规格.Python解释器, "-u", str(工作脚本),
            "--adapter", self.类型.value,
            "--project-root", str(self.规格.路径),
            "--threads", str(max(1, int(self.规格.线程数))),
        ]
        self._进程 = subprocess.Popen(
            命令,
            cwd=str(工作脚本.parent),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=环境,
        )
        self._已关闭 = False
        self._stdout线程 = threading.Thread(
            target=self._读标准输出, name=f"桥-{self.标识}-out", daemon=True)
        self._stderr线程 = threading.Thread(
            target=self._读标准错误, name=f"桥-{self.标识}-err", daemon=True)
        self._stdout线程.start()
        self._stderr线程.start()
        # 握手：确认后端导入成功
        self.调用("ping", {}, 超时=30.0)

    def 关闭(self) -> None:
        self._已关闭 = True
        if self._进程 is None:
            return
        if self._进程.poll() is None:
            try:
                self.调用("close", {}, 超时=10.0)
            except Exception:
                pass
            try:
                self._进程.terminate()
                self._进程.wait(timeout=5)
            except Exception:
                try:
                    self._进程.kill()
                except Exception:
                    pass
        for 流 in (getattr(self._进程, "stdin", None),
                   getattr(self._进程, "stdout", None),
                   getattr(self._进程, "stderr", None)):
            try:
                if 流 is not None:
                    流.close()
            except Exception:
                pass
        self._进程 = None
        self.失败所有等待("适配器已关闭")

    def 失败所有等待(self, 原因: str) -> None:
        with self._锁:
            等待 = list(self._等待表.values())
            self._等待表.clear()
        for f in 等待:
            if not f.done():
                f.set_exception(适配器错误(原因, 网盘=self.标识))

    # ---------------- 协议 ----------------

    def 调用(self, 命令: str, 参数: dict | None = None,
             超时: Optional[float] = 120.0) -> Any:
        if self._已关闭:
            raise 适配器错误("适配器已关闭", 网盘=self.标识, 命令=命令)
        self.启动进程若需要()
        编号 = next(self._请求ID)
        消息 = json.dumps(
            {"id": 编号, "cmd": 命令, "params": 参数 or {}},
            ensure_ascii=False, separators=(",", ":"))
        future: Future = Future()
        with self._锁:
            if self._进程 is None or self._进程.poll() is not None:
                raise 适配器错误(
                    f"适配器进程已退出（returncode={getattr(self._进程, 'returncode', None)}）",
                    网盘=self.标识, 命令=命令)
            self._等待表[编号] = future
            try:
                assert self._进程.stdin is not None
                self._进程.stdin.write(消息 + "\n")
                self._进程.stdin.flush()
            except Exception as e:
                self._等待表.pop(编号, None)
                raise 适配器错误(f"写入适配器失败：{e}",
                                 网盘=self.标识, 命令=命令) from e
        try:
            return future.result(timeout=超时)
        except FutureTimeoutError as e:
            with self._锁:
                self._等待表.pop(编号, None)
            raise 适配器错误(
                f"适配器调用超时（{超时}s）：{命令}",
                网盘=self.标识, 命令=命令) from e

    def 启动进程若需要(self) -> None:
        if self._进程 is None or self._进程.poll() is not None:
            self.启动()

    def _写回响应(self, 编号: int, 数据: dict) -> None:
        with self._锁:
            future = self._等待表.pop(编号, None)
        if future is None or future.done():
            return
        if 数据.get("ok"):
            future.set_result(数据.get("result"))
        else:
            错误 = 数据.get("error") or {}
            消息 = str(错误.get("message") or "适配器调用失败")
            类型 = str(错误.get("type") or "")
            if 类型 == "未登录":
                future.set_exception(认证错误(消息, 网盘=self.标识))
            else:
                future.set_exception(适配器错误(
                    消息, 网盘=self.标识, 原始=数据))

    def _读标准输出(self) -> None:
        assert self._进程 is not None and self._进程.stdout is not None
        for 行 in self._进程.stdout:
            行 = 行.strip()
            if not 行:
                continue
            try:
                消息 = json.loads(行)
            except Exception:
                self._日志("warn", f"无法解析桥输出：{行[:200]}")
                continue
            if "id" in 消息:
                self._写回响应(int(消息["id"]), 消息)
                continue
            事件 = 消息.get("event")
            if 事件 == "progress":
                任务ID = str(消息.get("task_id") or "")
                回调 = self._进度回调.get(任务ID)
                if 回调:
                    try:
                        回调(str(消息.get("phase") or ""),
                             int(消息.get("current") or 0),
                             int(消息.get("total") or 0))
                    except Exception:
                        pass
            elif 事件 == "log":
                self._日志(str(消息.get("level") or "info"),
                          str(消息.get("message") or ""))
        self.失败所有等待("适配器输出流结束")

    def _读标准错误(self) -> None:
        """桥进程的 stderr ≈ 适配器自己的 print，按"信息"级别上报终端。"""
        assert self._进程 is not None and self._进程.stderr is not None
        for 行 in self._进程.stderr:
            行 = 行.rstrip()
            if not 行:
                continue
            级别 = "警告" if ("Traceback" in 行 or "Error" in 行
                           or "错误" in 行 or "失败" in 行) else "信息"
            self._日志(级别, 行)

    def _日志(self, 级别: str, 消息: str) -> None:
        if self._日志回调:
            try:
                self._日志回调(级别, 消息)
            except Exception:
                pass

    # ---------------- 统一操作 ----------------

    def 账号状态(self) -> 账号信息:
        数据 = self.调用("account", {}, 超时=120.0)
        return 账号信息(
            网盘=self.类型,
            已登录=bool(数据.get("logged_in")),
            用户名=str(数据.get("user") or ""),
            数据目录=str(数据.get("data_dir") or ""),
            详情=dict(数据.get("detail") or {}),
        )

    def 列目录(self, 路径: str = "/") -> list[远端条目]:
        数据 = self.调用("list", {"path": 路径})
        return [远端条目.from_dict(x) for x in (数据 or [])]

    def 文件信息(self, 路径: str) -> Optional[远端条目]:
        数据 = self.调用("stat", {"path": 路径})
        return 远端条目.from_dict(数据) if 数据 else None

    def 确保目录(self, 路径: str) -> dict:
        return dict(self.调用("ensure_dir", {"path": 路径}) or {})

    def 上传(self, 本地路径: str, 远端目录: str, *,
             名称: Optional[str] = None,
             任务ID: str = "",
             进度: Optional[Callable[[str, int, int], None]] = None) -> dict:
        参数 = {
            "local_path": _绝对本地路径(本地路径),
            "remote_dir": 远端目录,
            "name": 名称 or Path(本地路径).name,
            "task_id": 任务ID,
        }
        if 进度 is not None and 任务ID:
            with _进度登记(self, 任务ID) as 登记:
                登记.注册(进度)
                return dict(self.调用("upload", 参数, 超时=None) or {})
        return dict(self.调用("upload", 参数, 超时=None) or {})

    def 取播放直链(self, 远端路径: str, *, 超时: float = 60.0) -> dict:
        """取可直接喂给播放器的直链（含请求头）。V8_3 播放用。"""
        return dict(self.调用("play_link", {"path": 远端路径},
                           超时=超时) or {})

    def 暂停任务(self, 任务ID: str) -> dict:
        """让某个任务在桥进程内部的安全点停下（上传/下载的进度回调处）。

        只在引擎侧回调里抛异常是不够的——那个回调跑在客户端读线程上，
        异常会被吞掉，任务照跑（实测踩到）。
        """
        try:
            return dict(self.调用("pause_task", {"task_id": str(任务ID)},
                               超时=15.0) or {})
        except Exception:
            return {}

    def 继续任务(self, 任务ID: str) -> dict:
        try:
            return dict(self.调用("resume_task", {"task_id": str(任务ID)},
                               超时=15.0) or {})
        except Exception:
            return {}

    def 下载(self, 远端路径: str, 本地路径: str, *,
             任务ID: str = "",
             进度: Optional[Callable[[str, int, int], None]] = None,
             续传: bool = True,
             保守: bool = False) -> dict:
        参数 = {
            "remote_path": 远端路径,
            "local_path": _绝对本地路径(本地路径),
            "task_id": 任务ID,
            "resume": bool(续传),
            # 保守模式：跳过直连签名 URL，直接用适配器原生下载（兜底用）
            "conservative": bool(保守),
        }
        if 进度 is not None and 任务ID:
            with _进度登记(self, 任务ID) as 登记:
                登记.注册(进度)
                return dict(self.调用("download", 参数, 超时=None) or {})
        return dict(self.调用("download", 参数, 超时=None) or {})

    def 删除(self, 路径: str) -> dict:
        return dict(self.调用("delete", {"path": 路径}) or {})

    # ==================== 统一登录（6 种方式） ====================

    登录方式顺序 = ("qrcode", "cookie", "sms", "email", "token", "password")
    登录方式名称 = {
        "qrcode": "扫码登录", "cookie": "导入 Cookie", "sms": "短信登录",
        "email": "邮箱登录", "token": "令牌登录", "password": "账号密码登录",
    }

    def 登录方式表(self) -> dict:
        """``{方式: {"支持": bool, "说明": str, "名称": str}}``；异常时全标不支持。"""
        结果 = {}
        try:
            原始 = dict(self.调用("auth_caps", {}, 超时=30.0) or {})
        except Exception as e:  # noqa: BLE001
            原始 = {}
            for 键 in self.登录方式顺序:
                结果[键] = {"支持": False, "名称": self.登录方式名称[键],
                          "说明": f"读取登录能力失败：{e}"}
            return 结果
        for 键 in self.登录方式顺序:
            项 = dict(原始.get(键) or {})
            结果[键] = {
                "支持": bool(项.get("支持")),
                "名称": self.登录方式名称[键],
                "说明": str(项.get("说明") or self.登录方式名称[键]),
            }
        return 结果

    def 支持登录方式(self) -> list[str]:
        return [k for k, v in self.登录方式表().items() if v["支持"]]

    def 扫码开始(self) -> dict:
        return dict(self.调用("login_qr_start", {}, 超时=60.0) or {})

    def 扫码等待(self, 会话: str = "", 超时秒: float = 180.0) -> dict:
        """阻塞等扫码结果。

        客户端超时比桥里的截止时间多给 120 秒：各家适配器内部轮询循环
        （等确认/换票/落盘）常会比它自己的超时多跑一会儿，太快掐断会出现
        "界面报超时、桥稍后仍写入凭证"的错位。真想中断请用 :meth:`扫码取消`，
        或直接关闭登录对话框（closeEvent 会取消会话）。
        """
        return dict(self.调用(
            "login_qr_wait",
            {"session": 会话, "timeout": float(超时秒)},
            超时=float(超时秒) + 120.0) or {})

    def 扫码取消(self, 会话: str = "") -> dict:
        return dict(self.调用("login_qr_cancel", {"session": 会话},
                            超时=20.0) or {})

    def Cookie登录(self, 文本: str) -> dict:
        return dict(self.调用("login_cookie", {"text": 文本}, 超时=180.0) or {})

    def 短信发送(self, 手机号: str) -> dict:
        return dict(self.调用("login_sms_send", {"phone": 手机号},
                            超时=120.0) or {})

    def 短信校验(self, 会话: str, 验证码: str, 手机号: str = "") -> dict:
        return dict(self.调用("login_sms_verify",
                            {"session": 会话, "code": 验证码,
                             "phone": 手机号}, 超时=180.0) or {})

    def 令牌登录(self, 访问令牌: str, 刷新令牌: str = "",
                额外: dict | None = None) -> dict:
        return dict(self.调用("login_token",
                            {"access_token": 访问令牌,
                             "refresh_token": 刷新令牌,
                             "extra": 额外 or {}}, 超时=180.0) or {})

    def 账号密码登录(self, 账号: str, 密码: str,
                    额外: dict | None = None) -> dict:
        return dict(self.调用("login_password",
                            {"account": 账号, "password": 密码,
                             "extra": 额外 or {}}, 超时=180.0) or {})

    def 退出登录(self) -> dict:
        """清掉该网盘的**本地**登录凭证（删除动作在桥进程侧执行）。

        返回 ``{"状态": "成功"|"失败", "消息": ..., "清除": [...]}``。
        界面拿到成功后会立刻把本页置为未登录（清列表 + 禁写 + 灭绿点）。
        """
        return dict(self.调用("logout", {}, 超时=60.0) or {})

    def 邮箱登录(self, 邮箱: str, 密码: str,
                额外: dict | None = None) -> dict:
        return dict(self.调用("login_email",
                            {"email": 邮箱, "password": 密码,
                             "extra": 额外 or {}}, 超时=180.0) or {})
