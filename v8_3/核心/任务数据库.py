"""传输任务持久化（SQLite）。

目的：DSH 宿主重启、程序崩溃或用户主动暂停后，未完成任务可以继续，
而不是从零重扫/重传。只记录任务元数据与状态，不记录凭证。

表结构：
  batches(batch_id PK, source_cloud, source_path, target_cloud,
          target_path, config_json, status, created_at, updated_at)
  tasks(task_id PK, batch_id, source_path, target_path, size, status,
        error, retries, done_bytes, download_bytes, upload_bytes,
        local_cache, started_at, finished_at, stage, skip_reason)
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

from .模型 import 传输任务, 任务状态, 网盘类型, 标识文本


class 任务数据库:
    def __init__(self, 路径: str | Path):
        self.路径 = Path(路径).expanduser()
        self.路径.parent.mkdir(parents=True, exist_ok=True)
        self._锁 = threading.RLock()
        self._连接 = sqlite3.connect(
            str(self.路径), check_same_thread=False, timeout=30)
        self._连接.execute("PRAGMA journal_mode=WAL")
        self._连接.execute("PRAGMA synchronous=NORMAL")
        self._建表()

    def _建表(self) -> None:
        with self._锁:
            c = self._连接
            c.execute("""
                CREATE TABLE IF NOT EXISTS batches(
                    batch_id TEXT PRIMARY KEY,
                    source_cloud TEXT NOT NULL,
                    source_path TEXT NOT NULL,
                    target_cloud TEXT NOT NULL,
                    target_path TEXT NOT NULL,
                    config_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'running',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )""")
            c.execute("""
                CREATE TABLE IF NOT EXISTS tasks(
                    task_id TEXT PRIMARY KEY,
                    batch_id TEXT NOT NULL,
                    source_path TEXT NOT NULL,
                    target_path TEXT NOT NULL,
                    size INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    error TEXT NOT NULL DEFAULT '',
                    retries INTEGER NOT NULL DEFAULT 0,
                    done_bytes INTEGER NOT NULL DEFAULT 0,
                    download_bytes INTEGER NOT NULL DEFAULT 0,
                    upload_bytes INTEGER NOT NULL DEFAULT 0,
                    local_cache TEXT NOT NULL DEFAULT '',
                    started_at REAL NOT NULL DEFAULT 0,
                    finished_at REAL NOT NULL DEFAULT 0,
                    stage TEXT NOT NULL DEFAULT '',
                    skip_reason TEXT NOT NULL DEFAULT ''
                )""")
            c.execute("CREATE INDEX IF NOT EXISTS idx_tasks_batch "
                      "ON tasks(batch_id, status)")
            c.commit()

    # ---------------- 批次 ----------------

    def 保存批次(self, 批次ID: str, 源, 源路径: str,
                 目标, 目标路径: str, 配置: dict,
                 状态: str = "running") -> None:
        """``源`` / ``目标`` 支持实例标识字符串或 :class:`网盘类型` 枚举。"""
        现在 = time.time()
        with self._锁:
            self._连接.execute(
                """INSERT INTO batches(batch_id, source_cloud, source_path,
                        target_cloud, target_path, config_json, status,
                        created_at, updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(batch_id) DO UPDATE SET
                        status=excluded.status, updated_at=excluded.updated_at""",
                (批次ID, 标识文本(源), 源路径, 标识文本(目标), 目标路径,
                 json.dumps(配置, ensure_ascii=False, default=str),
                 状态, 现在, 现在))
            self._连接.commit()

    def 更新批次状态(self, 批次ID: str, 状态: str) -> None:
        with self._锁:
            self._连接.execute(
                "UPDATE batches SET status=?, updated_at=? WHERE batch_id=?",
                (状态, time.time(), 批次ID))
            self._连接.commit()

    def 读取批次(self, 批次ID: str) -> Optional[dict]:
        with self._锁:
            行 = self._连接.execute(
                "SELECT * FROM batches WHERE batch_id=?", (批次ID,)).fetchone()
        if 行 is None:
            return None
        列 = [d[0] for d in self._连接.execute(
            "SELECT * FROM batches LIMIT 0").description]
        数据 = dict(zip(列, 行))
        数据["config"] = json.loads(数据.pop("config_json") or "{}")
        return 数据

    def 未完成批次(self, 限制: int = 50) -> list[dict]:
        with self._锁:
            行s = self._连接.execute(
                """SELECT batch_id, source_cloud, source_path, target_cloud,
                          target_path, status, updated_at, created_at
                   FROM batches WHERE status NOT IN ('done')
                   ORDER BY updated_at DESC LIMIT ?""", (限制,)).fetchall()
        return [
            {"batch_id": r[0], "source_cloud": r[1], "source_path": r[2],
             "target_cloud": r[3], "target_path": r[4], "status": r[5],
             "updated_at": r[6], "created_at": r[7]}
            for r in 行s
        ]

    def 删除批次(self, 批次ID: str) -> None:
        with self._锁:
            self._连接.execute("DELETE FROM tasks WHERE batch_id=?", (批次ID,))
            self._连接.execute("DELETE FROM batches WHERE batch_id=?", (批次ID,))
            self._连接.commit()

    # ---------------- 任务 ----------------

    def 保存任务(self, 任务: 传输任务) -> None:
        with self._锁:
            self._连接.execute(
                """INSERT INTO tasks(task_id, batch_id, source_path,
                        target_path, size, status, error, retries, done_bytes,
                        download_bytes, upload_bytes, local_cache, started_at,
                        finished_at, stage, skip_reason)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(task_id) DO UPDATE SET
                        status=excluded.status, error=excluded.error,
                        retries=excluded.retries, done_bytes=excluded.done_bytes,
                        download_bytes=excluded.download_bytes,
                        upload_bytes=excluded.upload_bytes,
                        local_cache=excluded.local_cache,
                        started_at=excluded.started_at,
                        finished_at=excluded.finished_at,
                        stage=excluded.stage, skip_reason=excluded.skip_reason""",
                (任务.任务ID, 任务.批次ID, 任务.源路径, 任务.目标路径,
                 任务.大小, 任务.状态.value, 任务.错误, 任务.重试次数,
                 任务.已传输, 任务.下载字节, 任务.上传字节, 任务.本地缓存,
                 任务.开始时间, 任务.结束时间, 任务.阶段详情, 任务.跳过原因))
            self._连接.commit()

    def 批量保存任务(self, 任务列表: list[传输任务]) -> None:
        for 任务 in 任务列表:
            self.保存任务(任务)

    def 读取任务(self, 批次ID: str) -> list[传输任务]:
        with self._锁:
            行s = self._连接.execute(
                """SELECT task_id, source_path, target_path, size, status,
                          error, retries, done_bytes, download_bytes,
                          upload_bytes, local_cache, started_at, finished_at,
                          stage, skip_reason
                   FROM tasks WHERE batch_id=? ORDER BY task_id""",
                (批次ID,)).fetchall()
        结果: list[传输任务] = []
        for r in 行s:
            结果.append(传输任务(
                任务ID=r[0], 批次ID=批次ID,
                源网盘="",  # 由调用方按批次配置回填
                目标网盘="",
                源路径=r[1], 目标路径=r[2], 大小=r[3],
                状态=任务状态(r[4]), 错误=r[5], 重试次数=r[6],
                已传输=r[7], 下载字节=r[8], 上传字节=r[9],
                本地缓存=r[10], 开始时间=r[11], 结束时间=r[12],
                阶段详情=r[13], 跳过原因=r[14]))
        return 结果

    def 关闭(self) -> None:
        with self._锁:
            try:
                self._连接.commit()
            except Exception:
                pass
            try:
                self._连接.close()
            except Exception:
                pass
