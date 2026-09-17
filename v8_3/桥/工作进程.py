#!/usr/bin/env python3
"""适配器桥接工作进程。

用法（由 V8_3 的 子进程适配器 自动拉起）：
    python 工作进程.py --adapter baidu --project-root <适配器目录> --threads 8

协议：stdin/stdout 每行一个 JSON。
  请求： {"id": 1, "cmd": "list", "params": {...}}
  响应： {"id": 1, "ok": true, "result": ...}
  进度： {"event": "progress", "task_id": "...", "phase": "download",
          "current": 123, "total": 456}
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


后端模块映射 = {
    "baidu": "后端_百度",
    "guangya": "后端_光鸭",
    "quark": "后端_夸克",
    "fake": "后端_假",
}


class 工作进程:
    def __init__(self, adapter: str, project_root: str, threads: int):
        self.adapter = adapter
        self.project_root = str(Path(project_root).expanduser().resolve())
        self.threads = max(1, int(threads))
        self._写锁 = threading.Lock()
        self._后端 = None
        self._池 = ThreadPoolExecutor(
            max_workers=self.threads,
            thread_name_prefix=f"桥-{adapter}",
        )
        # 登录单独一个池：login_qr_wait 会阻塞几分钟（等用户扫码），
        # 不能跟上传/下载抢线程——否则长传输会把登录请求排在队尾，
        # 界面超时了子进程还可能在稍后补写凭证。
        self._登录池 = ThreadPoolExecutor(
            max_workers=2, thread_name_prefix=f"桥-{adapter}-登录")
        # 按任务 ID 的"停下"信号：界面点暂停/取消时，引擎发 pause_task 过来，
        # 上传/下载的进度回调里检查到就抛错 → 任务在**桥进程内部**安全停下。
        # （只在引擎侧的回调里抛异常没用：那个回调跑在客户端读线程上，异常会被吞。）
        self._停止表: dict[str, threading.Event] = {}
        self._停止锁 = threading.Lock()
        # 把适配器项目根插到 sys.path[0]，让它的 核心/ 包可被导入。
        if self.project_root not in sys.path:
            sys.path.insert(0, self.project_root)
        self._装载后端()

    # ---------------- 初始化 ----------------

    def _装载后端(self) -> None:
        模块名 = 后端模块映射.get(self.adapter)
        if 模块名 is None:
            raise SystemExit(f"未知适配器：{self.adapter}")
        try:
            模块 = __import__(模块名)
        except Exception as e:
            # 后端模块在桥目录下；导入失败通常是适配器自身依赖/路径问题
            print(f"[桥] 导入后端 {模块名} 失败：{e}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            raise
        self._后端 = 模块.后端(
            self.project_root,
            发事件=self.发事件,
            日志=self.发日志,
        )
        print(f"[桥] {self.adapter} 后端已就绪：{self.project_root}",
              file=sys.stderr, flush=True)

    # ---------------- 事件 ----------------

    def 发事件(self, 事件种类: str, **字段) -> None:
        消息 = {"event": 事件种类}
        消息.update(字段)
        self._写(消息)

    def 发日志(self, 级别: str, 消息: str) -> None:
        self.发事件("log", level=级别 or "info", message=str(消息))

    def _写(self, 消息: dict) -> None:
        行 = json.dumps(消息, ensure_ascii=False, default=str,
                        separators=(",", ":"))
        with self._写锁:
            sys.stdout.write(行 + "\n")
            sys.stdout.flush()

    # ---------------- 命令分发 ----------------

    def 处理(self, 请求: dict) -> None:
        编号 = 请求.get("id")
        try:
            命令 = str(请求.get("cmd") or "")
            参数 = dict(请求.get("params") or {})
            结果 = self._执行(命令, 参数)
            self._写({"id": 编号, "ok": True, "result": 结果})
        except Exception as e:
            # IsADirectoryError 必须在这里：它是 OSError 但**不是**
            # NotADirectoryError 的子类，以前会走进下面打整段 traceback 的分支
            # （用户只是选了目录，却刷一屏堆栈，看着像程序崩了）。
            if isinstance(e, (FileNotFoundError, NotADirectoryError,
                              IsADirectoryError, PermissionError,
                              ValueError)):
                print(f"[桥] {type(e).__name__}: {e}", file=sys.stderr)
            else:
                traceback.print_exc(file=sys.stderr)
            self._写({
                "id": 编号,
                "ok": False,
                "error": {
                    "type": type(e).__name__,
                    "message": str(e),
                },
            })

    def _执行(self, 命令: str, 参数: dict):
        if 命令 == "ping":
            return {"pong": True, "adapter": self.adapter}
        if 命令 == "close":
            self._池.shutdown(wait=False, cancel_futures=True)
            try:
                self._登录池.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass
            try:
                self._后端.close()
            except Exception:
                pass
            return {"closed": True}

        if 命令 == "account":
            return self._后端.account()
        if 命令 == "list":
            return self._后端.list(参数.get("path") or "/")
        if 命令 == "stat":
            return self._后端.stat(参数.get("path") or "/")
        if 命令 == "ensure_dir":
            return self._后端.ensure_dir(参数.get("path") or "/")
        if 命令 == "delete":
            return self._后端.delete(参数.get("path") or "/")

        # ---------------- 统一登录（6 种方式） ----------------
        if 命令 == "auth_caps":
            return self._后端.auth_caps()
        if 命令 == "login_qr_start":
            return self._后端.login_qr_start()
        if 命令 == "login_qr_wait":
            return self._后端.login_qr_wait(
                str(参数.get("session") or ""),
                float(参数.get("timeout") or 180.0))
        if 命令 == "login_qr_cancel":
            return self._后端.login_qr_cancel(str(参数.get("session") or ""))
        if 命令 == "login_cookie":
            return self._后端.login_cookie(str(参数.get("text") or ""))
        if 命令 == "login_sms_send":
            return self._后端.login_sms_send(str(参数.get("phone") or ""))
        if 命令 == "login_sms_verify":
            return self._后端.login_sms_verify(
                str(参数.get("session") or ""),
                str(参数.get("code") or ""),
                str(参数.get("phone") or ""))
        if 命令 == "login_token":
            return self._后端.login_token(
                str(参数.get("access_token") or ""),
                str(参数.get("refresh_token") or ""),
                dict(参数.get("extra") or {}))
        if 命令 == "login_password":
            return self._后端.login_password(
                str(参数.get("account") or ""),
                str(参数.get("password") or ""),
                dict(参数.get("extra") or {}))
        if 命令 == "login_email":
            return self._后端.login_email(
                str(参数.get("email") or ""),
                str(参数.get("password") or ""),
                dict(参数.get("extra") or {}))

        if 命令 == "upload":
            任务ID = str(参数.get("task_id") or "")
            def 进度(阶段, 当前, 总量):
                self._检查停止(任务ID)
                if 任务ID:
                    self.发事件("progress", task_id=任务ID, phase=阶段,
                              current=int(当前 or 0), total=int(总量 or 0))
            return self._后端.upload(
                str(参数.get("local_path") or ""),
                str(参数.get("remote_dir") or "/"),
                str(参数.get("name") or ""),
                任务ID,
                进度,
            )

        if 命令 == "pause_task":
            任务ID = str(参数.get("task_id") or "")
            with self._停止锁:
                self._停止表.setdefault(任务ID, threading.Event()).set()
            return {"paused": True, "task_id": 任务ID}
        if 命令 == "resume_task":
            任务ID = str(参数.get("task_id") or "")
            with self._停止锁:
                if 任务ID and 任务ID != "*":
                    事件 = self._停止表.pop(任务ID, None)
                    if 事件 is not None:
                        事件.clear()
                    数量 = 1 if 事件 is not None else 0
                else:
                    # 不带任务 ID = 全部解除（批次开始/全部继续时用）
                    数量 = len(self._停止表)
                    self._停止表.clear()
            return {"resumed": True, "task_id": 任务ID, "count": 数量}
        if 命令 == "play_link":
            return self._后端.play_link(str(参数.get("path") or ""))
        if 命令 == "download":
            任务ID = str(参数.get("task_id") or "")
            def 进度(阶段, 当前, 总量):
                self._检查停止(任务ID)
                if 任务ID:
                    self.发事件("progress", task_id=任务ID, phase=阶段,
                              current=int(当前 or 0), total=int(总量 or 0))
            return self._后端.download(
                str(参数.get("remote_path") or ""),
                str(参数.get("local_path") or ""),
                任务ID,
                进度,
                续传=bool(参数.get("resume", True)),
                保守=bool(参数.get("conservative", False)),
            )
        raise ValueError(f"未知命令：{命令}")

    def _检查停止(self, 任务ID: str) -> None:
        """进度回调里的"安全停止点"：被 pause_task 标记过就抛错。"""
        if not 任务ID:
            return
        with self._停止锁:
            事件 = self._停止表.get(任务ID)
        if 事件 is not None and 事件.is_set():
            raise RuntimeError("任务已被用户暂停/取消（在安全点停下）")

    # ---------------- 主循环 ----------------

    def 运行(self) -> int:
        for 行 in sys.stdin:
            行 = 行.strip()
            if not 行:
                continue
            try:
                请求 = json.loads(行)
            except Exception:
                print(f"[桥] 非法 JSON：{行[:200]}", file=sys.stderr)
                continue
            if 请求.get("cmd") == "close":
                self.处理(请求)
                return 0
            命令 = str(请求.get("cmd") or "")
            # 登录命令走独立小池：登录会阻塞几分钟（等扫码），
            # 不能排在长传输后面，也不能占满传输线程。
            池 = (self._登录池
                 if 命令.startswith("login_") or 命令 == "auth_caps"
                 else self._池)
            池.submit(self.处理, 请求)
        return 0


def main(argv=None) -> int:
    解析器 = argparse.ArgumentParser()
    解析器.add_argument("--adapter", required=True)
    解析器.add_argument("--project-root", required=True)
    解析器.add_argument("--threads", type=int, default=8)
    参数 = 解析器.parse_args(argv)
    进程 = 工作进程(参数.adapter, 参数.project_root, 参数.threads)
    try:
        return 进程.运行()
    finally:
        进程._池.shutdown(wait=False, cancel_futures=True)
        try:
            进程._登录池.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
