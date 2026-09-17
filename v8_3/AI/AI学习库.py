# v8_3/AI/AI学习库.py
"""AI 调度学习库 - 记录每次 AI 决策的效果，供下次决策参考。

移植自 V8 ``业务层/AI学习库.py``，改动：

* 默认库路径改为 V8_3 自己的 ``AI学习库路径()``
  （``数据/AI学习库.db``，绝不写到 V8 目录）；也可以显式传 ``数据库路径``；
* 日志从 ``print`` 换成 ``logging``（不打印任何密钥，本模块也接触不到密钥）；
* ``查询相似高收益策略`` 的 ``相似度函数`` 省略时默认用
  ``队列特征分析器.相似度``。

构造函数签名
============
``AI学习库(数据库路径=None, 配置=None)``

* ``AI学习库()``：用 ``AI学习库路径()``；
* ``AI学习库("D:/x/AI学习库.db")``：V8 风格（位置参数就是路径）；
* ``AI学习库(配置)`` / ``AI学习库(配置=整份配置)``：路径取
  ``AI学习库路径(配置)``；
* ``AI学习库(配置=AI配置())``：AI 段里没有学习库路径键，等价于默认路径。

公开方法
========
``记录策略(特征指纹, 特征, 策略, 决策来源="ai")`` → dict
``更新效果(特征指纹, 吞吐_MBps=None, 失败率=None, 总耗时_秒=None, 收益评分=None)``
``获取记录(特征指纹)`` → dict | None
``查询相似高收益策略(特征, 相似度函数=None, 阈值=0.85)`` → dict | None
``获取统计()`` → dict
``关闭()``（可当上下文管理器用：``with AI学习库(...) as 库: ...``）
"""
import os
import json
import time
import sqlite3
import logging
import threading
from datetime import datetime
from typing import Dict, Optional

from ..配置 import AI学习库路径
from .队列特征分析 import 队列特征分析器

logger = logging.getLogger(__name__)


def 格式化时间(时间戳: float = None) -> str:
    if 时间戳 is None:
        时间戳 = time.time()
    dt = datetime.fromtimestamp(时间戳)
    return (f"{dt.year}年{dt.month}月{dt.day}日 "
            f"{dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}."
            f"{dt.microsecond // 1000:03d}")


def _默认库路径(配置=None) -> str:
    if isinstance(配置, dict) and "AI" in 配置:
        return AI学习库路径(配置)
    return AI学习库路径()


class AI学习库:
    VERSION = 1
    最多保留记录 = 10000
    内存缓存上限 = 2000

    def __init__(self, 数据库路径=None, 配置=None):
        if isinstance(数据库路径, dict) and 配置 is None:
            数据库路径, 配置 = None, 数据库路径
        self.数据库路径 = str(数据库路径 or _默认库路径(配置))
        目录 = os.path.dirname(self.数据库路径)
        if 目录 and not os.path.exists(目录):
            os.makedirs(目录, exist_ok=True)

        self._conn = None
        self._连接锁 = threading.RLock()
        self._关闭标记 = False

        self._内存缓存: Dict[str, dict] = {}
        self._缓存锁 = threading.RLock()

        self._初始化数据库()
        self._预加载()

    # ==================== 连接 ====================

    def _获取连接(self):
        with self._连接锁:
            if self._conn is None:
                conn = sqlite3.connect(
                    self.数据库路径, timeout=30.0,
                    check_same_thread=False)
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                self._conn = conn
            return self._conn

    def _初始化数据库(self):
        conn = self._获取连接()
        conn.execute('''
            CREATE TABLE IF NOT EXISTS AI决策记录 (
                特征指纹       TEXT PRIMARY KEY,
                特征JSON       TEXT NOT NULL,
                策略JSON       TEXT NOT NULL,
                决策来源       TEXT,
                吞吐_MBps      REAL DEFAULT 0,
                失败率         REAL DEFAULT 0,
                总耗时_秒      REAL DEFAULT 0,
                收益评分       INTEGER DEFAULT 0,
                使用次数       INTEGER DEFAULT 1,
                创建时间       TEXT NOT NULL,
                更新时间       TEXT NOT NULL
            )
        ''')
        conn.execute("CREATE INDEX IF NOT EXISTS idx_创建时间 ON AI决策记录(创建时间)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_收益 ON AI决策记录(收益评分)")
        conn.commit()
        logger.info("[AI学习库] ✅ 初始化: %s", self.数据库路径)

    @staticmethod
    def _row转dict(row) -> dict:
        try:
            特征 = json.loads(row['特征JSON'])
        except Exception:
            特征 = {}
        try:
            策略 = json.loads(row['策略JSON'])
        except Exception:
            策略 = {}
        return {
            "特征指纹": row['特征指纹'],
            "特征": 特征, "策略": 策略,
            "决策来源": row['决策来源'] or "",
            "吞吐_MBps": row['吞吐_MBps'] or 0,
            "失败率": row['失败率'] or 0,
            "总耗时_秒": row['总耗时_秒'] or 0,
            "收益评分": row['收益评分'] or 0,
            "使用次数": row['使用次数'] or 1,
            "创建时间": row['创建时间'] or "",
            "更新时间": row['更新时间'] or "",
        }

    def _预加载(self):
        try:
            conn = self._获取连接()
            rows = conn.execute(
                "SELECT * FROM AI决策记录 "
                "ORDER BY 更新时间 DESC LIMIT ?",
                (self.内存缓存上限,)).fetchall()
            with self._缓存锁:
                self._内存缓存 = {}
                for row in rows:
                    info = self._row转dict(row)
                    self._内存缓存[info["特征指纹"]] = info
            logger.info("[AI学习库] ✅ 预加载 %s 条", len(self._内存缓存))
        except Exception as e:
            logger.warning("[AI学习库] 预加载失败: %s", e)

    # ==================== 写入 ====================

    def 记录策略(self, 特征指纹: str, 特征: dict, 策略: dict,
                决策来源: str = "ai") -> dict:
        当前时间 = 格式化时间()
        with self._缓存锁:
            已存在 = 特征指纹 in self._内存缓存
            if 已存在:
                旧 = self._内存缓存[特征指纹]
                记录 = dict(旧)
                记录["策略"] = 策略
                记录["决策来源"] = 决策来源
                记录["使用次数"] = 旧.get("使用次数", 1) + 1
                记录["更新时间"] = 当前时间
            else:
                记录 = {
                    "特征指纹": 特征指纹,
                    "特征": 特征, "策略": 策略,
                    "决策来源": 决策来源,
                    "吞吐_MBps": 0.0, "失败率": 0.0,
                    "总耗时_秒": 0.0, "收益评分": 0,
                    "使用次数": 1,
                    "创建时间": 当前时间,
                    "更新时间": 当前时间,
                }
            self._内存缓存[特征指纹] = 记录
        self._写库(记录)
        return 记录

    def 更新效果(self, 特征指纹: str,
                吞吐_MBps: float = None,
                失败率: float = None,
                总耗时_秒: float = None,
                收益评分: int = None) -> Optional[dict]:
        with self._缓存锁:
            记录 = self._内存缓存.get(特征指纹)
            if not 记录:
                return None
            if 吞吐_MBps is not None:
                记录["吞吐_MBps"] = round(吞吐_MBps, 3)
            if 失败率 is not None:
                记录["失败率"] = round(失败率, 4)
            if 总耗时_秒 is not None:
                记录["总耗时_秒"] = round(总耗时_秒, 2)
            if 收益评分 is not None:
                记录["收益评分"] = int(收益评分)
            记录["更新时间"] = 格式化时间()
        self._写库(记录)
        return dict(记录)

    def _写库(self, 记录):
        try:
            with self._连接锁:
                conn = self._获取连接()
                conn.execute('''
                    INSERT OR REPLACE INTO AI决策记录
                    (特征指纹, 特征JSON, 策略JSON, 决策来源,
                     吞吐_MBps, 失败率, 总耗时_秒, 收益评分,
                     使用次数, 创建时间, 更新时间)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    记录["特征指纹"],
                    json.dumps(记录["特征"], ensure_ascii=False),
                    json.dumps(记录["策略"], ensure_ascii=False),
                    记录["决策来源"],
                    记录["吞吐_MBps"], 记录["失败率"],
                    记录["总耗时_秒"], 记录["收益评分"],
                    记录["使用次数"],
                    记录["创建时间"], 记录["更新时间"],
                ))
                conn.commit()
        except Exception as e:
            logger.warning("[AI学习库] 写库失败: %s", e)

    # ==================== 读取 ====================

    def 获取记录(self, 特征指纹: str) -> Optional[dict]:
        with self._缓存锁:
            记录 = self._内存缓存.get(特征指纹)
            return dict(记录) if 记录 else None

    def 查询相似高收益策略(self, 特征: dict, 相似度函数=None,
                          阈值: float = 0.85) -> Optional[dict]:
        if 相似度函数 is None:
            相似度函数 = 队列特征分析器.相似度
        最佳 = None
        最佳相似 = 0.0
        最佳收益 = -999
        with self._缓存锁:
            快照 = list(self._内存缓存.values())
        for 记录 in 快照:
            if 记录["收益评分"] < 0:
                continue
            if 记录["使用次数"] < 1:
                continue
            s = 相似度函数(特征, 记录["特征"])
            if s < 阈值:
                continue
            if (s > 最佳相似 + 0.02
                    or (abs(s - 最佳相似) <= 0.02
                        and 记录["收益评分"] > 最佳收益)):
                最佳 = 记录
                最佳相似 = s
                最佳收益 = 记录["收益评分"]
        if 最佳:
            return {"记录": dict(最佳), "相似度": 最佳相似}
        return None

    def 获取统计(self) -> dict:
        with self._缓存锁:
            记录列表 = list(self._内存缓存.values())
        总记录 = len(记录列表)
        正收益 = sum(1 for r in 记录列表 if r["收益评分"] > 0)
        负收益 = sum(1 for r in 记录列表 if r["收益评分"] < 0)
        零收益 = 总记录 - 正收益 - 负收益
        return {
            "总记录": 总记录, "正收益": 正收益,
            "负收益": 负收益, "零收益": 零收益,
        }

    # ==================== 关闭 ====================

    def 关闭(self):
        self._关闭标记 = True
        with self._连接锁:
            if self._conn is not None:
                try:
                    self._conn.commit()
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None

    def __enter__(self):
        return self

    def __exit__(self, *异常):
        self.关闭()
        return False
