# v8_3/敏感词/敏感词数据库.py
"""
敏感词数据库（AC 自动机 + SQLite）

来源：只读学习 网盘管理_V8 / 业务层/敏感词数据库管理.py 后，在本项目内独立落地，
V8_3 不 import 也不修改 V8 的任何文件；除文件头与默认库路径外逻辑保持一致。

职责：
  * 敏感词表：网盘 + 敏感词 + 最后一次成功安全词 + 已失效安全词
  * 改名记录表：原云端路径 → 云端实际路径（下载时可还原原名）
  * 纯 Python AC 自动机：按网盘分组建索引，命中位置用于批量替换
  * 候选安全词生成：拼音 / 同义变体，供上传守卫做 P1–P4 绕过

注意：``网盘名称`` 在 V8_3 里用**网盘实例标识**（如 ``quark`` / ``quark_2``），
所以同一家网盘的不同账号各自有独立的敏感词学习结果。
"""
import logging
import os
import re
import json
import time
import pickle
import sqlite3
import threading
from collections import OrderedDict, deque
from datetime import datetime
from typing import Dict, List, Optional


# ============================================================
# 日志：走标准库 logging → v8_3.日志 会接到终端和界面日志页
# ============================================================
logger = logging.getLogger("敏感词数据库")


def _记(消息: str, 级别: str = "信息") -> None:
    """打一行日志；消息里自带的 ``[敏感词数据库]`` 前缀会去掉（logger 名已带）。"""
    文本 = str(消息 or "")
    for 前缀 in ("[敏感词数据库] ", "[敏感词库] "):
        if 文本.startswith(前缀):
            文本 = 文本[len(前缀):]
            break
    等级 = {"调试": logging.DEBUG, "信息": logging.INFO,
          "警告": logging.WARNING, "错误": logging.ERROR}.get(级别, logging.INFO)
    logger.log(等级, "%s", 文本)


try:
    from pypinyin import lazy_pinyin
    _PYPI_AVAILABLE = True
except ImportError:
    _PYPI_AVAILABLE = False
    _记("[敏感词库] ⚠️ 未安装 pypinyin，拼音替换降级为 Unicode 码点", "警告")

try:
    import ahocorasick
    _AHOCORASICK_AVAILABLE = True
except ImportError:
    _AHOCORASICK_AVAILABLE = False
    _记("[敏感词库] ℹ️ 使用纯 Python AC 自动机")


# ============================================================
# 中文 / 拼音工具
# ============================================================

def 是中文字(c: str) -> bool:
    if not c:
        return False
    code = ord(c)
    return (0x4E00 <= code <= 0x9FFF
            or 0x3400 <= code <= 0x4DBF
            or 0x20000 <= code <= 0x2A6DF
            or 0x2A700 <= code <= 0x2B73F)


def 汉字转拼音(汉字: str) -> str:
    if not 汉字:
        return ""
    if _PYPI_AVAILABLE:
        try:
            结果 = lazy_pinyin(汉字)
            if 结果:
                return 结果[0]
        except Exception:
            pass
    return f"u{ord(汉字):04x}"


def 生成安全词(敏感词: str) -> Optional[str]:
    for i in range(len(敏感词) - 1, -1, -1):
        if 是中文字(敏感词[i]):
            拼音 = 汉字转拼音(敏感词[i])
            return 敏感词[:i] + 拼音 + 敏感词[i + 1:]
    return None


def 全部转拼音(文本: str) -> str:
    if not 文本:
        return ""
    return "".join((汉字转拼音(c) if 是中文字(c) else c)
                   for c in 文本)


_中文段正则 = re.compile(r'[\u4e00-\u9fff\u3400-\u4dbf]+')


def 格式化时间(时间戳: float = None) -> str:
    if 时间戳 is None:
        时间戳 = time.time()
    dt = datetime.fromtimestamp(时间戳)
    return (f"{dt.year}年{dt.month}月{dt.day}日 "
            f"{dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}."
            f"{dt.microsecond // 1000:03d}")


# ============================================================
# 纯 Python AC 自动机
# ============================================================

class _AC节点:
    __slots__ = ('子节点', 'fail', '输出')

    def __init__(self):
        self.子节点 = {}
        self.fail = None
        self.输出 = []


class _纯PythonAC自动机:
    def __init__(self, 词表):
        self.根 = _AC节点()
        self._长度 = 0
        for 词 in 词表:
            if not 词:
                continue
            node = self.根
            for c in 词:
                if c not in node.子节点:
                    node.子节点[c] = _AC节点()
                node = node.子节点[c]
            node.输出.append(词)
            self._长度 += 1
        queue = deque()
        for c, child in self.根.子节点.items():
            child.fail = self.根
            queue.append(child)
        while queue:
            node = queue.popleft()
            for c, child in node.子节点.items():
                fail = node.fail
                while fail is not self.根 and c not in fail.子节点:
                    fail = fail.fail
                if c in fail.子节点 and fail.子节点[c] is not child:
                    child.fail = fail.子节点[c]
                else:
                    child.fail = self.根
                child.输出.extend(child.fail.输出)
                queue.append(child)

    def __len__(self):
        return self._长度

    def iter(self, 文本):
        if not 文本:
            return
        node = self.根
        for i, c in enumerate(文本):
            while node is not self.根 and c not in node.子节点:
                node = node.fail
            if c in node.子节点:
                node = node.子节点[c]
            else:
                node = self.根
            for 词 in node.输出:
                yield i, 词


# ============================================================
# 主类
# ============================================================

class 敏感词数据库:
    """敏感词 + 改名记录 + 探测历史 + 敏感词树 深度融合"""

    VERSION = 2
    自动机缓存上限 = 10
    写缓冲上限 = 500
    改名缓存上限 = 200000
    探测历史上限 = 100000

    def __init__(self, 数据库路径: str = ""):
        if not str(数据库路径 or "").strip():
            # 默认放在 V8_3 自己的 数据/ 目录（V8_3/数据/敏感词.db）
            数据库路径 = str(
                Path(__file__).resolve().parents[2] / "数据" / "敏感词.db")
        self.数据库路径 = str(数据库路径)
        self.序列化缓存路径 = self.数据库路径 + ".cache.pkl"

        目录 = os.path.dirname(self.数据库路径)
        if 目录 and not os.path.exists(目录):
            os.makedirs(目录, exist_ok=True)

        self._conn: Optional[sqlite3.Connection] = None
        self._连接锁 = threading.RLock()
        self._关闭标记 = False

        self._敏感词缓存: Dict[str, Dict[str, dict]] = {}
        self._缓存锁 = threading.RLock()

        self._改名缓存: Dict[str, Dict[str, dict]] = {}
        self._改名反查: Dict[str, Dict[str, str]] = {}

        self._树缓存: Dict[str, Dict[str, dict]] = {}

        self._自动机缓存 = OrderedDict()
        self._自动机锁 = threading.RLock()

        self._写缓冲 = []
        self._写缓冲锁 = threading.Lock()

        self._统计锁 = threading.Lock()
        self._统计 = {
            "预检命中": 0, "新发现": 0,
            "安全词成功": 0, "安全词失效": 0,
            "405_触发绕过": 0, "自动机构建次数": 0,
            "P2单段成功": 0, "P3组合成功": 0, "P4保底成功": 0,
        }

        self._初始化数据库()
        self._预加载()
        self._启动后台线程()

    def _获取连接(self) -> sqlite3.Connection:
        with self._连接锁:
            if self._conn is None:
                conn = sqlite3.connect(
                    self.数据库路径, timeout=30.0,
                    check_same_thread=False)
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                conn.execute("PRAGMA cache_size=-65536")
                self._conn = conn
            return self._conn

    def _初始化数据库(self):
        conn = self._获取连接()
        conn.execute('''
            CREATE TABLE IF NOT EXISTS 敏感词 (
                网盘名称         TEXT NOT NULL,
                敏感词           TEXT NOT NULL,
                发现时间         TEXT NOT NULL,
                最后一次成功安全词 TEXT,
                最后一次成功时间 TEXT,
                已失效安全词     TEXT,
                来源             TEXT DEFAULT '未知',
                PRIMARY KEY (网盘名称, 敏感词)
            )
        ''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_敏感词_网盘 ON 敏感词(网盘名称)')

        conn.execute('''
            CREATE TABLE IF NOT EXISTS 改名记录 (
                网盘名称         TEXT NOT NULL,
                原云端路径       TEXT NOT NULL,
                云端实际路径     TEXT NOT NULL,
                原文件名         TEXT NOT NULL,
                云端文件名       TEXT NOT NULL,
                文件大小         INTEGER DEFAULT 0,
                命中的敏感词     TEXT,
                当前安全词       TEXT,
                改名策略         TEXT,
                改名原因         TEXT,
                创建时间         TEXT NOT NULL,
                更新时间         TEXT NOT NULL,
                状态             TEXT DEFAULT '有效',
                PRIMARY KEY (网盘名称, 原云端路径)
            )
        ''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_改名_云端路径 ON 改名记录(网盘名称, 云端实际路径)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_改名_状态 ON 改名记录(状态)')

        conn.execute('''
            CREATE TABLE IF NOT EXISTS 敏感词探测历史 (
                ID           INTEGER PRIMARY KEY AUTOINCREMENT,
                网盘名称     TEXT NOT NULL,
                原文件名     TEXT NOT NULL,
                试探类型     TEXT NOT NULL,
                试探内容     TEXT NOT NULL,
                结果         TEXT NOT NULL,
                时间         TEXT NOT NULL
            )
        ''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_探测_网盘内容 ON 敏感词探测历史(网盘名称, 试探内容)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_探测_结果 ON 敏感词探测历史(结果)')

        conn.execute('''
            CREATE TABLE IF NOT EXISTS 敏感词树 (
                网盘名称         TEXT NOT NULL,
                词               TEXT NOT NULL,
                类型             TEXT NOT NULL,
                父词             TEXT,
                命中次数         INTEGER DEFAULT 0,
                验证次数         INTEGER DEFAULT 0,
                成功次数         INTEGER DEFAULT 0,
                置信度           REAL DEFAULT 0.0,
                首次发现时间     TEXT NOT NULL,
                最后验证时间     TEXT NOT NULL,
                PRIMARY KEY (网盘名称, 词)
            )
        ''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_树_网盘置信度 ON 敏感词树(网盘名称, 置信度 DESC)')

        conn.commit()
        _记(f"[敏感词数据库] ✅ 初始化: {self.数据库路径}")

    @staticmethod
    def _敏感词row转dict(row) -> dict:
        try:
            失效集合 = json.loads(row['已失效安全词'] or "{}")
        except Exception:
            失效集合 = {}
        return {
            "敏感词": row['敏感词'],
            "网盘名称": row['网盘名称'],
            "发现时间": row['发现时间'],
            "最后一次成功安全词": row['最后一次成功安全词'] or "",
            "最后一次成功时间": row['最后一次成功时间'] or "",
            "已失效安全词": 失效集合,
            "来源": row['来源'] if '来源' in row.keys() else "未知",
        }

    @staticmethod
    def _改名row转dict(row) -> dict:
        return {
            "网盘名称": row['网盘名称'],
            "原云端路径": row['原云端路径'],
            "云端实际路径": row['云端实际路径'],
            "原文件名": row['原文件名'],
            "云端文件名": row['云端文件名'],
            "文件大小": row['文件大小'] or 0,
            "命中的敏感词": row['命中的敏感词'] or "",
            "当前安全词": row['当前安全词'] or "",
            "改名策略": row['改名策略'] or "",
            "改名原因": row['改名原因'] or "",
            "创建时间": row['创建时间'] or "",
            "更新时间": row['更新时间'] or "",
            "状态": row['状态'] or "有效",
        }

    def _预加载(self):
        try:
            if os.path.exists(self.序列化缓存路径):
                缓存mtime = os.path.getmtime(self.序列化缓存路径)
                库mtime = os.path.getmtime(self.数据库路径)
                if 缓存mtime >= 库mtime:
                    with open(self.序列化缓存路径, 'rb') as f:
                        快照 = pickle.load(f)
                    self._敏感词缓存 = 快照.get("敏感词", {})
                    self._改名缓存 = 快照.get("改名", {})
                    self._改名反查 = 快照.get("改名反查", {})
                    self._树缓存 = 快照.get("树", {})
                    词数 = sum(len(v) for v in self._敏感词缓存.values())
                    _记(f"[敏感词数据库] ⚡ 缓存加载 {词数} 词")
                    return
        except Exception as e:
            _记(f"[敏感词数据库] 缓存加载失败: {e}", "警告")

        try:
            conn = self._获取连接()
            词行 = conn.execute("SELECT * FROM 敏感词").fetchall()
            with self._缓存锁:
                self._敏感词缓存 = {}
                for row in 词行:
                    info = self._敏感词row转dict(row)
                    网盘 = info["网盘名称"]
                    self._敏感词缓存.setdefault(网盘, {})
                    self._敏感词缓存[网盘][info["敏感词"]] = info

            改名行 = conn.execute("SELECT * FROM 改名记录").fetchall()
            with self._缓存锁:
                self._改名缓存 = {}
                self._改名反查 = {}
                for row in 改名行:
                    info = self._改名row转dict(row)
                    网盘 = info["网盘名称"]
                    self._改名缓存.setdefault(网盘, {})
                    self._改名反查.setdefault(网盘, {})
                    self._改名缓存[网盘][info["原云端路径"]] = info
                    self._改名反查[网盘][info["云端实际路径"]] = info["原云端路径"]

            树行 = conn.execute("SELECT * FROM 敏感词树").fetchall()
            with self._缓存锁:
                self._树缓存 = {}
                for row in 树行:
                    网盘 = row['网盘名称']
                    词 = row['词']
                    self._树缓存.setdefault(网盘, {})
                    self._树缓存[网盘][词] = {
                        "词": 词, "类型": row['类型'],
                        "父词": row['父词'],
                        "命中次数": row['命中次数'] or 0,
                        "验证次数": row['验证次数'] or 0,
                        "成功次数": row['成功次数'] or 0,
                        "置信度": row['置信度'] or 0.0,
                        "首次发现时间": row['首次发现时间'] or "",
                        "最后验证时间": row['最后验证时间'] or "",
                    }
            词数 = sum(len(v) for v in self._敏感词缓存.values())
            _记(f"[敏感词数据库] ✅ SQLite 加载 {词数} 词")
            self._保存序列化缓存()
        except Exception as e:
            _记(f"[敏感词数据库] 预加载失败: {e}", "警告")

    def _保存序列化缓存(self):
        try:
            with self._缓存锁:
                快照 = {
                    "敏感词": dict(self._敏感词缓存),
                    "改名": dict(self._改名缓存),
                    "改名反查": dict(self._改名反查),
                    "树": dict(self._树缓存),
                }
            with open(self.序列化缓存路径, 'wb') as f:
                pickle.dump(快照, f, protocol=pickle.HIGHEST_PROTOCOL)
        except Exception as e:
            _记(f"[敏感词数据库] 保存缓存失败: {e}", "警告")

    # ==================== 自动机 ====================

    def _获取自动机(self, 网盘名称: str):
        with self._自动机锁:
            if 网盘名称 in self._自动机缓存:
                self._自动机缓存.move_to_end(网盘名称)
                return self._自动机缓存[网盘名称]
            with self._缓存锁:
                词表 = self._敏感词缓存.get(网盘名称, {})
                keys = list(词表.keys()) if 词表 else []
            if not keys:
                return None
            if _AHOCORASICK_AVAILABLE:
                A = ahocorasick.Automaton()
                for 词 in keys:
                    A.add_word(词, 词)
                A.make_automaton()
                ac = A
            else:
                ac = _纯PythonAC自动机(keys)
            self._自动机缓存[网盘名称] = ac
            while len(self._自动机缓存) > self.自动机缓存上限:
                self._自动机缓存.popitem(last=False)
            with self._统计锁:
                self._统计["自动机构建次数"] += 1
            return ac

    def _失效自动机(self, 网盘名称: str):
        with self._自动机锁:
            self._自动机缓存.pop(网盘名称, None)

    # ==================== 查询 ====================

    def 获取敏感词(self, 网盘名称: str,
                  敏感词: str) -> Optional[dict]:
        with self._缓存锁:
            词表 = self._敏感词缓存.get(网盘名称, {})
            if 敏感词 in 词表:
                return dict(词表[敏感词])
        return None

    def 匹配敏感词(self, 网盘名称: str,
                  文件名: str) -> Optional[dict]:
        命中列表 = self.扫描所有敏感词(网盘名称, 文件名)
        if not 命中列表:
            return None
        最长 = max(命中列表, key=lambda h: len(h['敏感词']))
        return self.获取敏感词(网盘名称, 最长['敏感词'])

    def 扫描所有敏感词(self, 网盘名称: str,
                      文本: str) -> List[dict]:
        if not 文本:
            return []
        ac = self._获取自动机(网盘名称)
        if ac is None:
            return []
        try:
            if len(ac) == 0:
                return []
        except Exception:
            return []
        命中 = []
        try:
            for 位置, 词 in ac.iter(文本):
                起 = 位置 - len(词) + 1
                止 = 位置 + 1
                命中.append({'敏感词': 词, '起': 起, '止': 止})
        except Exception:
            return []
        命中.sort(key=lambda x: (x['起'], -(x['止'] - x['起'])))
        去重后 = []
        for h in 命中:
            if (去重后 and 去重后[-1]['起'] <= h['起']
                    and 去重后[-1]['止'] >= h['止']):
                continue
            去重后.append(h)
        return 去重后

    def 生成候选安全词列表(self, 网盘名称: str,
                          敏感词: str) -> List[str]:
        候选 = []
        信息 = self.获取敏感词(网盘名称, 敏感词)
        已失效 = set(信息["已失效安全词"].keys()) if 信息 else set()
        当前 = 敏感词
        for _ in range(len(敏感词) * 2 + 5):
            新词 = 生成安全词(当前)
            if 新词 is None or 新词 == 当前:
                break
            候选.append(新词)
            当前 = 新词
        结果 = [c for c in 候选 if c not in 已失效]
        全拼音 = 全部转拼音(敏感词)
        if (全拼音 != 敏感词 and 全拼音 not in 已失效
                and 全拼音 not in 结果):
            结果.append(全拼音)
        return 结果

    @staticmethod
    def 提取中文段(文件名: str) -> List[str]:
        中文段 = _中文段正则.findall(文件名)
        中文段.sort(key=len, reverse=True)
        return 中文段

    @staticmethod
    def 切中文段(文件名: str) -> List[dict]:
        结果 = []
        for m in _中文段正则.finditer(文件名):
            结果.append({
                '段': m.group(0),
                '起': m.start(),
                '止': m.end(),
            })
        return 结果

    # ==================== 写入 ====================

    def 记录发现(self, 网盘名称: str, 敏感词: str,
                来源: str = "未知") -> dict:
        if not 敏感词:
            return {}
        当前时间 = 格式化时间()
        with self._缓存锁:
            词表 = self._敏感词缓存.setdefault(网盘名称, {})
            if 敏感词 in 词表:
                return dict(词表[敏感词])
            info = {
                "敏感词": 敏感词, "网盘名称": 网盘名称,
                "发现时间": 当前时间,
                "最后一次成功安全词": "",
                "最后一次成功时间": "",
                "已失效安全词": {},
                "来源": 来源,
            }
            词表[敏感词] = info
        with self._写缓冲锁:
            self._写缓冲.append(("发现", (
                网盘名称, 敏感词, 当前时间, 来源)))
            超限 = len(self._写缓冲) >= self.写缓冲上限
        if 超限:
            self._flush()
        self._失效自动机(网盘名称)
        with self._统计锁:
            self._统计["新发现"] += 1
        _记(f"[敏感词数据库] 🆕 '{敏感词}' ({网盘名称}, {来源})")
        return info

    def 记录安全词成功(self, 网盘名称: str, 敏感词: str,
                      安全词: str):
        if not 敏感词 or not 安全词:
            return
        当前时间 = 格式化时间()
        with self._缓存锁:
            词表 = self._敏感词缓存.get(网盘名称, {})
            if 敏感词 not in 词表:
                词表[敏感词] = {
                    "敏感词": 敏感词, "网盘名称": 网盘名称,
                    "发现时间": 当前时间,
                    "最后一次成功安全词": 安全词,
                    "最后一次成功时间": 当前时间,
                    "已失效安全词": {}, "来源": "自动",
                }
            else:
                词表[敏感词]["最后一次成功安全词"] = 安全词
                词表[敏感词]["最后一次成功时间"] = 当前时间
        with self._写缓冲锁:
            self._写缓冲.append(("安全词成功", (
                网盘名称, 敏感词, 安全词, 当前时间)))
            超限 = len(self._写缓冲) >= self.写缓冲上限
        if 超限:
            self._flush()
        with self._统计锁:
            self._统计["安全词成功"] += 1
        _记(f"[敏感词数据库] ✅ '{敏感词}' → '{安全词}'")

    def 记录安全词失效(self, 网盘名称: str, 敏感词: str,
                      安全词: str):
        if not 敏感词 or not 安全词:
            return
        当前时间 = 格式化时间()
        with self._缓存锁:
            词表 = self._敏感词缓存.get(网盘名称, {})
            if 敏感词 not in 词表:
                return
            失效集合 = 词表[敏感词].setdefault("已失效安全词", {})
            失效集合[安全词] = 当前时间
        with self._写缓冲锁:
            self._写缓冲.append(("安全词失效", (
                网盘名称, 敏感词, 安全词, 当前时间)))
            超限 = len(self._写缓冲) >= self.写缓冲上限
        if 超限:
            self._flush()
        with self._统计锁:
            self._统计["安全词失效"] += 1

    # ==================== 探测历史 ====================

    def 记录探测(self, 网盘名称: str, 原文件名: str,
                试探类型: str, 试探内容: str, 结果: str):
        if not 网盘名称 or not 试探内容:
            return
        当前时间 = 格式化时间()
        with self._写缓冲锁:
            self._写缓冲.append(("探测", (
                网盘名称, 原文件名, 试探类型, 试探内容,
                结果, 当前时间)))
            超限 = len(self._写缓冲) >= self.写缓冲上限
        if 超限:
            self._flush()

    def 查询探测历史(self, 网盘名称: str = None,
                    试探内容: str = None,
                    结果: str = None,
                    限制: int = 1000) -> List[dict]:
        try:
            with self._连接锁:
                conn = self._获取连接()
                条件 = []
                参数 = []
                if 网盘名称:
                    条件.append("网盘名称 = ?")
                    参数.append(网盘名称)
                if 试探内容:
                    条件.append("试探内容 = ?")
                    参数.append(试探内容)
                if 结果:
                    条件.append("结果 = ?")
                    参数.append(结果)
                where = f"WHERE {' AND '.join(条件)}" if 条件 else ""
                参数.append(限制)
                rows = conn.execute(f'''
                    SELECT * FROM 敏感词探测历史
                    {where}
                    ORDER BY ID DESC LIMIT ?
                ''', 参数).fetchall()
                return [dict(r) for r in rows]
        except Exception:
            return []

    # ==================== 敏感词树 ====================

    def 重建敏感词树(self, 网盘名称: str,
                    最小验证次数: int = 1):
        try:
            with self._连接锁:
                conn = self._获取连接()
                聚合 = conn.execute('''
                    SELECT 试探内容,
                           COUNT(*) AS 验证次数,
                           SUM(CASE WHEN 结果='成功' THEN 1 ELSE 0 END) AS 成功次数,
                           MIN(时间) AS 首次时间,
                           MAX(时间) AS 最后时间
                    FROM 敏感词探测历史
                    WHERE 网盘名称 = ?
                    GROUP BY 试探内容
                    HAVING 验证次数 >= ?
                ''', (网盘名称, 最小验证次数)).fetchall()

                当前时间 = 格式化时间()
                with self._缓存锁:
                    self._树缓存[网盘名称] = {}
                    树 = self._树缓存[网盘名称]
                    for row in 聚合:
                        词 = row['试探内容']
                        验证 = row['验证次数'] or 0
                        成功 = row['成功次数'] or 0
                        置信度 = 成功 / max(1, 验证)
                        树[词] = {
                            "词": 词,
                            "类型": "单词" if len(词) < 8 else "组合",
                            "父词": None,
                            "命中次数": 验证,
                            "验证次数": 验证,
                            "成功次数": 成功,
                            "置信度": round(置信度, 3),
                            "首次发现时间": row['首次时间'] or 当前时间,
                            "最后验证时间": row['最后时间'] or 当前时间,
                        }

                conn.execute('DELETE FROM 敏感词树 WHERE 网盘名称 = ?',
                            (网盘名称,))
                for 词, info in 树.items():
                    conn.execute('''
                        INSERT INTO 敏感词树
                        (网盘名称, 词, 类型, 父词, 命中次数,
                         验证次数, 成功次数, 置信度,
                         首次发现时间, 最后验证时间)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        网盘名称, 词, info["类型"], info["父词"],
                        info["命中次数"], info["验证次数"],
                        info["成功次数"], info["置信度"],
                        info["首次发现时间"], info["最后验证时间"],
                    ))
                conn.commit()
            _记(f"[敏感词数据库] 🌳 重建: {len(树)} 节点")
            self._保存序列化缓存()
        except Exception as e:
            _记(f"[敏感词数据库] 重建树失败: {e}", "警告")

    def 获取高置信候选(self, 网盘名称: str,
                      文件名: str,
                      最小置信度: float = 0.5) -> List[dict]:
        段列表 = self.切中文段(文件名)
        候选 = []
        with self._缓存锁:
            树 = self._树缓存.get(网盘名称, {})
        for 段 in 段列表:
            词 = 段['段']
            if 词 in 树:
                info = 树[词]
                if info["置信度"] >= 最小置信度:
                    候选.append({
                        "词": 词, "起": 段['起'], "止": 段['止'],
                        "置信度": info["置信度"],
                    })
            else:
                候选.append({
                    "词": 词, "起": 段['起'], "止": 段['止'],
                    "置信度": 0.5,
                })
        候选.sort(key=lambda x: x["置信度"], reverse=True)
        return 候选

    # ==================== 管理 ====================

    def 列出敏感词(self, 网盘名称: str = None) -> List[dict]:
        结果 = []
        with self._缓存锁:
            if 网盘名称:
                网盘列表 = [网盘名称] if 网盘名称 in self._敏感词缓存 else []
            else:
                网盘列表 = list(self._敏感词缓存.keys())
            for 网盘 in 网盘列表:
                for info in self._敏感词缓存[网盘].values():
                    结果.append(dict(info))
        return 结果

    def 添加敏感词(self, 网盘名称: str, 敏感词: str,
                  安全词: str = "") -> bool:
        if not 网盘名称 or not 敏感词:
            return False
        try:
            self.记录发现(网盘名称, 敏感词, 来源="手动")
            if 安全词:
                self.记录安全词成功(网盘名称, 敏感词, 安全词)
            return True
        except Exception:
            return False

    def 删除敏感词(self, 网盘名称: str, 敏感词: str) -> bool:
        with self._缓存锁:
            词表 = self._敏感词缓存.get(网盘名称, {})
            if 敏感词 not in 词表:
                return False
            del 词表[敏感词]
        self._失效自动机(网盘名称)
        try:
            with self._连接锁:
                conn = self._获取连接()
                conn.execute('''
                    DELETE FROM 敏感词
                    WHERE 网盘名称 = ? AND 敏感词 = ?
                ''', (网盘名称, 敏感词))
                conn.commit()
            return True
        except Exception:
            return False

    # ==================== 改名记录 ====================

    def 记录改名(self, 网盘名称: str, 原云端路径: str,
                云端实际路径: str,
                命中的敏感词: str = "", 当前安全词: str = "",
                改名策略: str = "", 改名原因: str = "",
                文件大小: int = 0) -> dict:
        if not 网盘名称 or not 原云端路径 or not 云端实际路径:
            return {}
        if 原云端路径 == 云端实际路径:
            return {}
        当前时间 = 格式化时间()
        原文件名 = os.path.basename(原云端路径)
        云端文件名 = os.path.basename(云端实际路径)

        with self._缓存锁:
            网盘缓存 = self._改名缓存.setdefault(网盘名称, {})
            网盘反查 = self._改名反查.setdefault(网盘名称, {})
            已存在 = 原云端路径 in 网盘缓存
            if 已存在:
                旧 = 网盘缓存[原云端路径]
                网盘反查.pop(旧["云端实际路径"], None)
                创建时间 = 旧["创建时间"]
            else:
                创建时间 = 当前时间

            info = {
                "网盘名称": 网盘名称,
                "原云端路径": 原云端路径,
                "云端实际路径": 云端实际路径,
                "原文件名": 原文件名,
                "云端文件名": 云端文件名,
                "文件大小": int(文件大小 or 0),
                "命中的敏感词": 命中的敏感词,
                "当前安全词": 当前安全词,
                "改名策略": 改名策略,
                "改名原因": 改名原因,
                "创建时间": 创建时间,
                "更新时间": 当前时间,
                "状态": "有效",
            }
            网盘缓存[原云端路径] = info
            网盘反查[云端实际路径] = 原云端路径

            if len(网盘缓存) > self.改名缓存上限:
                项目 = sorted(网盘缓存.items(),
                             key=lambda kv: kv[1]["创建时间"])
                删除数 = len(项目) // 2
                for k, v in 项目[:删除数]:
                    网盘缓存.pop(k, None)
                    网盘反查.pop(v["云端实际路径"], None)

        with self._写缓冲锁:
            self._写缓冲.append(("改名", info))
            超限 = len(self._写缓冲) >= self.写缓冲上限
        if 超限:
            self._flush()
        return info

    def 获取改名记录(self, 网盘名称: str,
                    原云端路径: str) -> Optional[dict]:
        with self._缓存锁:
            网盘缓存 = self._改名缓存.get(网盘名称, {})
            if 原云端路径 in 网盘缓存:
                return dict(网盘缓存[原云端路径])
        return None

    def 反查原名(self, 网盘名称: str,
                云端实际路径: str) -> Optional[str]:
        with self._缓存锁:
            网盘反查 = self._改名反查.get(网盘名称, {})
            if 云端实际路径 in 网盘反查:
                return 网盘反查[云端实际路径]
        return None

    def 反查原文件名(self, 网盘名称: str,
                    云端实际路径: str) -> Optional[str]:
        原路径 = self.反查原名(网盘名称, 云端实际路径)
        if 原路径:
            return os.path.basename(原路径)
        return None

    def 列出改名记录(self, 网盘名称: str = None,
                    状态: str = "有效") -> List[dict]:
        结果 = []
        with self._缓存锁:
            if 网盘名称:
                网盘列表 = [网盘名称] if 网盘名称 in self._改名缓存 else []
            else:
                网盘列表 = list(self._改名缓存.keys())
            for 网盘 in 网盘列表:
                for info in self._改名缓存[网盘].values():
                    if 状态 and info["状态"] != 状态:
                        continue
                    结果.append(dict(info))
        return 结果

    def 删除改名记录(self, 网盘名称: str, 原云端路径: str) -> bool:
        with self._缓存锁:
            网盘缓存 = self._改名缓存.get(网盘名称, {})
            if 原云端路径 not in 网盘缓存:
                return False
            info = 网盘缓存.pop(原云端路径)
            网盘反查 = self._改名反查.get(网盘名称, {})
            网盘反查.pop(info["云端实际路径"], None)
        try:
            with self._连接锁:
                conn = self._获取连接()
                conn.execute('''
                    DELETE FROM 改名记录
                    WHERE 网盘名称 = ? AND 原云端路径 = ?
                ''', (网盘名称, 原云端路径))
                conn.commit()
            return True
        except Exception:
            return False

    def 列出全部(self, 网盘名称: str = None,
                状态: str = "有效") -> List[dict]:
        return self.列出改名记录(网盘名称=网盘名称, 状态=状态)

    # ==================== 统计 ====================

    def 增加统计(self, key: str, n: int = 1):
        with self._统计锁:
            self._统计[key] = self._统计.get(key, 0) + n

    def 获取统计(self) -> dict:
        with self._统计锁:
            return dict(self._统计)

    # ==================== 后台线程 ====================

    def _启动后台线程(self):
        threading.Thread(
            target=self._提交循环, daemon=True).start()

    def _提交循环(self):
        while not self._关闭标记:
            time.sleep(2)
            try:
                self._flush()
            except Exception as e:
                _记(f"[敏感词数据库] 提交异常: {e}")

    def _flush(self):
        with self._写缓冲锁:
            if not self._写缓冲:
                return
            缓冲 = self._写缓冲[:]
            self._写缓冲.clear()

        try:
            with self._连接锁:
                conn = self._获取连接()
                conn.execute("BEGIN TRANSACTION")
                for 操作, 参数 in 缓冲:
                    if 操作 == "发现":
                        网盘, 敏感词, 时间, 来源 = 参数
                        conn.execute('''
                            INSERT OR IGNORE INTO 敏感词
                            (网盘名称, 敏感词, 发现时间,
                             最后一次成功安全词, 最后一次成功时间,
                             已失效安全词, 来源)
                            VALUES (?, ?, ?, '', '', '{}', ?)
                        ''', (网盘, 敏感词, 时间, 来源))
                    elif 操作 == "安全词成功":
                        网盘, 敏感词, 安全词, 时间 = 参数
                        conn.execute('''
                            UPDATE 敏感词
                            SET 最后一次成功安全词 = ?,
                                最后一次成功时间 = ?
                            WHERE 网盘名称 = ? AND 敏感词 = ?
                        ''', (安全词, 时间, 网盘, 敏感词))
                    elif 操作 == "安全词失效":
                        网盘, 敏感词, 安全词, 时间 = 参数
                        row = conn.execute('''
                            SELECT 已失效安全词 FROM 敏感词
                            WHERE 网盘名称 = ? AND 敏感词 = ?
                        ''', (网盘, 敏感词)).fetchone()
                        if row is None:
                            continue
                        try:
                            失效集合 = json.loads(
                                row['已失效安全词'] or "{}")
                        except Exception:
                            失效集合 = {}
                        失效集合[安全词] = 时间
                        conn.execute('''
                            UPDATE 敏感词 SET 已失效安全词 = ?
                            WHERE 网盘名称 = ? AND 敏感词 = ?
                        ''', (json.dumps(失效集合,
                                        ensure_ascii=False),
                              网盘, 敏感词))
                    elif 操作 == "改名":
                        info = 参数
                        conn.execute('''
                            INSERT OR REPLACE INTO 改名记录
                            (网盘名称, 原云端路径, 云端实际路径,
                             原文件名, 云端文件名, 文件大小,
                             命中的敏感词, 当前安全词,
                             改名策略, 改名原因,
                             创建时间, 更新时间, 状态)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ''', (
                            info["网盘名称"], info["原云端路径"],
                            info["云端实际路径"], info["原文件名"],
                            info["云端文件名"], info["文件大小"],
                            info["命中的敏感词"], info["当前安全词"],
                            info["改名策略"], info["改名原因"],
                            info["创建时间"], info["更新时间"],
                            info["状态"],
                        ))
                    elif 操作 == "探测":
                        (网盘, 原文件名, 试探类型, 试探内容,
                         结果, 时间) = 参数
                        conn.execute('''
                            INSERT INTO 敏感词探测历史
                            (网盘名称, 原文件名, 试探类型,
                             试探内容, 结果, 时间)
                            VALUES (?, ?, ?, ?, ?, ?)
                        ''', (网盘, 原文件名, 试探类型,
                              试探内容, 结果, 时间))
                conn.execute("COMMIT")
        except Exception as e:
            _记(f"[敏感词数据库] 批量提交失败: {e}", "警告")
            with self._写缓冲锁:
                self._写缓冲[:0] = 缓冲

    def 关闭(self):
        self._关闭标记 = True
        try:
            self._flush()
        except Exception:
            pass
        self._保存序列化缓存()
        with self._连接锁:
            if self._conn is not None:
                try:
                    self._conn.commit()
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None
        _记("[敏感词数据库] 已关闭")