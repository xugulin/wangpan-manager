# v8_3/AI/DeepSeek价格抓取.py
"""DeepSeek 官方价格抓取器 - 动态获取价格 + 高峰时段规则。

移植自 V8 ``AI层/DeepSeek价格抓取.py``，改动：

* ``requests`` → ``httpx``（``timeout`` / ``headers`` 语义一致，
  异常统一按 ``httpx.HTTPError`` 处理）；
* 不再依赖 V8 的 ``配置加载器``：配置走 ``AI配置()`` 的普通 dict，
  需要时也可以吃 V8 风格对象（有 ``.获取()``）；
* 缓存/历史默认写到 V8_3 自己的 ``数据/`` 目录
  （``AI配置()['价格抓取']['缓存路径'/'历史路径']``）；
* 新增 ``自动刷新``：``False`` 时构造阶段**完全不联网**（只读本地缓存/兜底），
  交给 UI 决定何时 ``手动刷新()``；``禁用网络=True`` 则永久禁止联网；
* 解析函数 ``_解析HTML`` / ``_从文本提取价格`` / ``_解析高峰时段`` 都是纯函数，
  可以用内嵌 HTML 离线自测。

构造函数签名
============
``DeepSeek价格抓取器(配置=None, 自动刷新=True, 禁用网络=None,
                    缓存路径=None, 历史路径=None, 打印状态=True)``

* ``配置``：``AI配置()`` 的 dict / 整份配置 dict / V8 风格配置对象；
* ``禁用网络``：``None`` 时取配置里的 ``价格抓取.禁用网络``（默认 False）；
* 也可以 ``DeepSeek价格抓取器(配置=配置)``（V8 同样写法，键名一致）。

公开方法
========
``获取价格(模型名)`` → dict（``{"空闲": {...}, "高峰": {...}}``）
``获取高峰时段()`` → dict（V8 形态：``{"工作日","时段","时区"}``）
``获取所有模型名()`` → list
``获取元信息()`` → dict
``获取历史(限制=50)`` → list
``手动刷新()`` → bool
``启动定时刷新()`` → 起一个后台线程
"""
import os
import re
import json
import time
import logging
import threading
from typing import Optional, List

import httpx

from .配置适配 import 取AI配置

logger = logging.getLogger(__name__)


class DeepSeek价格抓取器:

    官方文档URL = ("https://api-docs.deepseek.com/"
                 "zh-cn/quick_start/pricing/")

    默认缓存路径 = "数据/DeepSeek价格.json"
    默认历史路径 = "数据/DeepSeek价格历史.jsonl"
    默认刷新间隔 = 24 * 3600
    默认历史最多条数 = 500
    默认禁用网络 = False

    兜底价格 = {
        "抓取时间": "内置兜底",
        "来源": "builtin",
        "模型": {
            "deepseek-flash": {
                "空闲": {"缓存命中": 0.02, "缓存未命中": 1.0,
                        "输出": 4.0},
                "高峰": {"缓存命中": 0.04, "缓存未命中": 2.0,
                        "输出": 8.0},
            },
            "deepseek-v4-pro": {
                "空闲": {"缓存命中": 0.15, "缓存未命中": 4.5,
                        "输出": 13.5},
                "高峰": {"缓存命中": 0.30, "缓存未命中": 9.0,
                        "输出": 27.0},
            },
        },
        "高峰时段": {
            "工作日": [0, 1, 2, 3, 4],
            "时段": [
                {"start": "09:00", "end": "12:00"},
                {"start": "14:00", "end": "18:00"},
            ],
            "时区": "Asia/Shanghai",
        },
        "模型别名": {
            "deepseek-v4-flash": "deepseek-flash",
            "deepseek-v4-flash-vision-exp": "deepseek-flash",
            "deepseek-flash-vision-exp": "deepseek-flash",
        },
    }

    def __init__(self, 配置=None, 自动刷新: bool = True,
                 禁用网络=None, 缓存路径=None, 历史路径=None,
                 打印状态: bool = True):
        self._配置 = 配置
        self.缓存路径 = str(缓存路径 or self.默认缓存路径)
        self.历史路径 = str(历史路径 or self.默认历史路径)
        self.刷新间隔 = self.默认刷新间隔
        self.历史最多条数 = self.默认历史最多条数
        self.禁用网络 = self.默认禁用网络
        self.自动刷新 = bool(自动刷新)
        self._手动时段覆盖 = None

        self._从配置加载(配置)
        if 禁用网络 is not None:
            self.禁用网络 = bool(禁用网络)
        if 缓存路径:
            self.缓存路径 = str(缓存路径)
        if 历史路径:
            self.历史路径 = str(历史路径)

        self._价格数据: Optional[dict] = None
        self._锁 = threading.RLock()
        self._上次刷新 = 0.0

        self._加载()
        if 打印状态:
            self._打印状态()

    # ==================== 配置 ====================

    def _从配置加载(self, 配置):
        """把 ``AI配置()``（或 V8 风格对象）里的价格/时段设置读进来。"""
        try:
            AI段 = 取AI配置(配置)
            价格配置 = AI段.get("价格抓取") or {}
            if not isinstance(价格配置, dict):
                价格配置 = {}
            if "刷新间隔小时" in 价格配置:
                try:
                    小时 = float(价格配置["刷新间隔小时"])
                    self.刷新间隔 = max(60.0, 小时 * 3600)
                except Exception:
                    pass
            if "历史最多条数" in 价格配置:
                try:
                    self.历史最多条数 = max(
                        10, int(价格配置["历史最多条数"]))
                except Exception:
                    pass
            if 价格配置.get("缓存路径"):
                self.缓存路径 = str(价格配置["缓存路径"])
            if 价格配置.get("历史路径"):
                self.历史路径 = str(价格配置["历史路径"])
            if "禁用网络" in 价格配置:
                self.禁用网络 = bool(价格配置["禁用网络"])

            时段配置 = AI段.get("高峰时段") or {}
            if isinstance(时段配置, dict) and 时段配置 and (
                    时段配置.get("时段") or 时段配置.get("开始")
                    or 时段配置.get("类型")):
                self._手动时段覆盖 = dict(时段配置)
        except Exception as e:
            logger.warning("[DeepSeek价格] ⚠️ 配置加载失败: %s", e)

    # ==================== 加载 / 刷新 ====================

    def _加载(self):
        缓存 = self._读磁盘缓存()
        if 缓存:
            with self._锁:
                self._价格数据 = 缓存
                self._上次刷新 = 缓存.get("时间戳", 0)

        if self._需要刷新():
            if self.禁用网络:
                logger.info("[DeepSeek价格] 🔒 已禁用网络")
            elif not self.自动刷新:
                logger.info("[DeepSeek价格] ⏸ 未自动刷新（可手动刷新）")
            else:
                self._尝试网络刷新()

        if not self._价格数据:
            with self._锁:
                self._价格数据 = dict(self.兜底价格)
                self._价格数据["时间戳"] = time.time()

        if self._手动时段覆盖:
            with self._锁:
                if self._价格数据:
                    self._价格数据["高峰时段"] = dict(
                        self._手动时段覆盖)

    def _需要刷新(self) -> bool:
        if not self._价格数据:
            return True
        return (time.time() - self._上次刷新) > self.刷新间隔

    def _读磁盘缓存(self) -> Optional[dict]:
        try:
            if not os.path.exists(self.缓存路径):
                return None
            with open(self.缓存路径, "r",
                      encoding="utf-8") as f:
                数据 = json.load(f)
            if not isinstance(数据, dict) or "模型" not in 数据:
                return None
            if "时间戳" not in 数据:
                数据["时间戳"] = os.path.getmtime(self.缓存路径)
            return 数据
        except Exception:
            return None

    def _写磁盘缓存(self, 数据: dict):
        try:
            目录 = os.path.dirname(self.缓存路径)
            if 目录 and not os.path.exists(目录):
                os.makedirs(目录, exist_ok=True)
            with open(self.缓存路径, "w",
                      encoding="utf-8") as f:
                json.dump(数据, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning("[DeepSeek价格] ⚠️ 缓存写入失败: %s", e)

    def _尝试网络刷新(self):
        if self.禁用网络:
            return
        try:
            logger.info("[DeepSeek价格] 🌐 抓取 %s", self.官方文档URL)
            响应 = httpx.get(
                self.官方文档URL,
                headers={
                    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) "
                                  "AppleWebKit/537.36 Chrome/120",
                    "Accept": "text/html,application/xhtml+xml",
                },
                timeout=15)
            响应.raise_for_status()
            解析结果 = self._解析HTML(响应.text)
            if not 解析结果 or not 解析结果.get("模型"):
                logger.warning("[DeepSeek价格] ⚠️ 解析为空，保留缓存/兜底")
                return

            if self._手动时段覆盖:
                解析结果["高峰时段"] = dict(self._手动时段覆盖)

            解析结果["时间戳"] = time.time()
            解析结果["抓取时间"] = time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime())
            解析结果["来源"] = "web"

            with self._锁:
                旧价格 = self._价格数据
                self._价格数据 = 解析结果
                self._上次刷新 = 解析结果["时间戳"]

            self._写磁盘缓存(解析结果)
            self._追加历史(解析结果, 旧价格)
            logger.info("[DeepSeek价格] ✅ 抓取成功: %s",
                        list(解析结果['模型'].keys()))
        except httpx.HTTPError as e:
            logger.warning("[DeepSeek价格] ⚠️ 抓取失败(HTTP): %s", e)
        except Exception as e:
            logger.warning("[DeepSeek价格] ⚠️ 抓取失败: %s", e)

    # ==================== 解析（纯函数，可离线自测） ====================

    def _解析HTML(self, html: str) -> Optional[dict]:
        结果 = {
            "模型": {},
            "高峰时段": self._解析高峰时段(html),
            "模型别名": dict(self.兜底价格["模型别名"]),
        }
        try:
            模型表 = self._提取表格(html, "模型细节")
            if 模型表:
                self._解析模型表(模型表, 结果)
            else:
                # 没有找到表格就全局扫
                self._解析模型表(html, 结果)
        except Exception as e:
            logger.warning("[DeepSeek价格] ⚠️ 解析异常: %s", e)
        return 结果 if 结果["模型"] else None

    @staticmethod
    def _提取表格(html: str, 关键字: str) -> Optional[str]:
        try:
            idx = html.find(关键字)
            if idx < 0:
                return None
            start = html.find("<table", idx)
            if start < 0:
                return None
            end = html.find("</table>", start)
            if end < 0:
                return None
            return html[start:end + len("</table>")]
        except Exception:
            return None

    def _解析模型表(self, table_html: str, 结果: dict):
        """扫描所有单元格，找到 deepseek-xxx 名字"""
        rows = re.findall(r'<tr[^>]*>(.*?)</tr>',
                          table_html, re.DOTALL | re.IGNORECASE)
        for row in rows:
            cells = re.findall(r'<t[dh][^>]*>(.*?)</t[dh]>',
                              row, re.DOTALL | re.IGNORECASE)
            cells = [re.sub(r'<[^>]+>', '', c).strip()
                    for c in cells]
            for 单元格 in cells:
                if "deepseek" not in 单元格.lower():
                    continue
                for m in re.finditer(
                        r'(deepseek[\-a-z0-9]+)',
                        单元格, re.IGNORECASE):
                    模型名 = m.group(1).lower()
                    # 去掉尾部如 -0813
                    模型名 = re.sub(
                        r'-(v?\d+[\d\.\-]*)$', '', 模型名)
                    if 模型名 not in self.兜底价格["模型"]:
                        continue
                    if 模型名 not in 结果["模型"]:
                        结果["模型"][模型名] = {
                            "空闲": {"缓存命中": 0.0,
                                    "缓存未命中": 0.0,
                                    "输出": 0.0},
                            "高峰": {"缓存命中": 0.0,
                                    "缓存未命中": 0.0,
                                    "输出": 0.0},
                        }
        # 用文本流解析价格
        self._从文本提取价格(table_html, 结果)

    def _从文本提取价格(self, html: str, 结果: dict):
        """基于文字描述定位价格，按“每行模型数×2”解析"""
        text = re.sub(r'<[^>]+>', ' ', html)
        text = re.sub(r'\s+', ' ', text)

        模型名列表 = list(结果["模型"].keys())
        if not 模型名列表:
            return

        # 找“价格”关键字
        idx = text.find("价格")
        if idx < 0:
            idx = text.find("百万tokens")
        if idx < 0:
            return
        价格区 = text[idx:]

        价格模式 = re.compile(r'([\d.]+)\s*元')
        所有价格 = [float(m.group(1))
                   for m in 价格模式.finditer(价格区)]

        if not 所有价格:
            return

        模型数 = min(len(模型名列表), 2)
        每行 = 模型数 * 2

        # 至少需要 3 行（命中/未命中/输出）
        if len(所有价格) < 每行 * 3:
            self._尽力填充(所有价格, 模型名列表, 结果)
            return

        缓存命中行 = 所有价格[0:每行]
        缓存未命中行 = 所有价格[每行:每行 * 2]
        输出行 = 所有价格[每行 * 2:每行 * 3]

        for i, 模型名 in enumerate(模型名列表[:模型数]):
            结果["模型"][模型名] = {
                "空闲": {
                    "缓存命中": 缓存命中行[i * 2],
                    "缓存未命中": 缓存未命中行[i * 2],
                    "输出": 输出行[i * 2],
                },
                "高峰": {
                    "缓存命中": 缓存命中行[i * 2 + 1],
                    "缓存未命中": 缓存未命中行[i * 2 + 1],
                    "输出": 输出行[i * 2 + 1],
                },
            }
        logger.info("[DeepSeek价格] ✅ 解析 %s 模型", len(结果['模型']))

    # 兼容旧调用
    def _正则解析价格(self, html: str, 结果: dict):
        self._从文本提取价格(html, 结果)

    def _填入模型价格(self, 模型名, 价格块, 结果):
        pass

    def _按行列解析(self, 所有价格, 模型名列表, 结果):
        self._尽力填充(所有价格, 模型名列表, 结果)

    def _尽力填充(self, 所有价格: List[float],
                 模型名列表: List[str], 结果: dict):
        内置 = self.兜底价格["模型"]
        for 模型名 in 模型名列表:
            if 模型名 in 内置:
                结果["模型"][模型名] = dict(内置[模型名])
            else:
                结果["模型"][模型名] = dict(
                    内置.get("deepseek-flash", {}))

    def _解析高峰时段(self, html: str) -> dict:
        默认 = dict(self.兜底价格["高峰时段"])
        text = re.sub(r'<[^>]+>', ' ', html)
        text = re.sub(r'\s+', ' ', text)

        idx = text.find("高峰时段")
        if idx < 0:
            idx = text.find("高峰")
        if idx < 0:
            return 默认

        片段 = text[idx:idx + 400]

        时段模式 = re.compile(
            r'(\d{1,2}):(\d{2})\s*[-–—~到至]\s*(\d{1,2}):(\d{2})')
        时段列表 = []
        for m in 时段模式.finditer(片段):
            起 = f"{int(m.group(1)):02d}:{m.group(2)}"
            止 = f"{int(m.group(3)):02d}:{m.group(4)}"
            时段列表.append({"start": 起, "end": 止})

        if not 时段列表:
            return 默认

        工作日 = [0, 1, 2, 3, 4]
        if ("周一至周五" in 片段
                or "周一到周五" in 片段
                or "工作日" in 片段):
            工作日 = [0, 1, 2, 3, 4]
        elif "周一至周日" in 片段 or "每天" in 片段:
            工作日 = [0, 1, 2, 3, 4, 5, 6]

        return {
            "工作日": 工作日,
            "时段": 时段列表,
            "时区": "Asia/Shanghai",
        }

    # ==================== 历史 ====================

    def _追加历史(self, 新数据: dict, 旧数据: Optional[dict]):
        try:
            目录 = os.path.dirname(self.历史路径)
            if 目录 and not os.path.exists(目录):
                os.makedirs(目录, exist_ok=True)
            记录 = {
                "时间戳": 新数据.get("时间戳", time.time()),
                "抓取时间": 新数据.get("抓取时间",
                                    time.strftime("%Y-%m-%d %H:%M:%S")),
                "来源": 新数据.get("来源", "unknown"),
                "模型数": len(新数据.get("模型", {})),
                "价格": 新数据.get("模型", {}),
                "变化": self._计算变化(旧数据, 新数据) if 旧数据 else [],
            }
            with open(self.历史路径, "a",
                      encoding="utf-8") as f:
                f.write(json.dumps(记录, ensure_ascii=False) + "\n")
            self._裁剪历史()
        except Exception as e:
            logger.warning("[DeepSeek价格] ⚠️ 追加历史失败: %s", e)

    def _计算变化(self, 旧数据: dict, 新数据: dict) -> list:
        if not 旧数据:
            return []
        变化 = []
        旧模型表 = 旧数据.get("模型", {})
        新模型表 = 新数据.get("模型", {})
        for 模型名 in 新模型表:
            旧价 = 旧模型表.get(模型名, {})
            新价 = 新模型表.get(模型名, {})
            for 时段 in ("空闲", "高峰"):
                for key in ("缓存命中", "缓存未命中", "输出"):
                    旧值 = 旧价.get(时段, {}).get(key, 0)
                    新值 = 新价.get(时段, {}).get(key, 0)
                    if abs(旧值 - 新值) > 1e-6:
                        变化.append({
                            "模型": 模型名, "时段": 时段,
                            "项": key, "旧值": 旧值, "新值": 新值,
                        })
        return 变化

    def _裁剪历史(self):
        try:
            if not os.path.exists(self.历史路径):
                return
            with open(self.历史路径, "r",
                      encoding="utf-8") as f:
                所有行 = f.readlines()
            if len(所有行) > self.历史最多条数:
                with open(self.历史路径, "w",
                          encoding="utf-8") as f:
                    f.writelines(所有行[-self.历史最多条数:])
        except Exception:
            pass

    def 获取历史(self, 限制: int = 50) -> list:
        try:
            if not os.path.exists(self.历史路径):
                return []
            with open(self.历史路径, "r",
                      encoding="utf-8") as f:
                所有行 = f.readlines()
            结果 = []
            for line in 所有行[-限制:]:
                line = line.strip()
                if not line:
                    continue
                try:
                    结果.append(json.loads(line))
                except Exception:
                    continue
            结果.reverse()
            return 结果
        except Exception:
            return []

    # ==================== 对外接口 ====================

    def 获取价格(self, 模型名: str) -> dict:
        with self._锁:
            数据 = self._价格数据 or self.兜底价格
        别名 = 数据.get("模型别名", {})
        真实名 = 别名.get(模型名, 模型名)
        模型价格 = 数据.get("模型", {}).get(真实名)
        if 模型价格:
            return dict(模型价格)
        return dict(数据.get("模型", {}).get(
            "deepseek-flash",
            self.兜底价格["模型"]["deepseek-flash"]))

    def 获取高峰时段(self) -> dict:
        if self._手动时段覆盖:
            return dict(self._手动时段覆盖)
        with self._锁:
            数据 = self._价格数据 or self.兜底价格
        return dict(数据.get("高峰时段",
                            self.兜底价格["高峰时段"]))

    def 获取所有模型名(self) -> list:
        with self._锁:
            数据 = self._价格数据 or self.兜底价格
        return list(数据.get("模型", {}).keys())

    def 获取元信息(self) -> dict:
        with self._锁:
            数据 = self._价格数据 or {}
        return {
            "来源": 数据.get("来源", "unknown"),
            "抓取时间": 数据.get("抓取时间", "未知"),
            "时间戳": 数据.get("时间戳", 0),
            "模型数": len(数据.get("模型", {})),
            "刷新间隔小时": self.刷新间隔 / 3600,
            "历史条数": self.历史最多条数,
            "缓存路径": self.缓存路径,
            "历史路径": self.历史路径,
            "禁用网络": self.禁用网络,
            "自动刷新": self.自动刷新,
            "时段来源": "配置" if self._手动时段覆盖 else "官网/兜底",
        }

    def 手动刷新(self) -> bool:
        if self.禁用网络:
            return False
        self.自动刷新 = True
        self._尝试网络刷新()
        return self._价格数据 is not None

    def _打印状态(self):
        info = self.获取元信息()
        图标 = {"web": "🌐", "builtin": "📦"}.get(
            info["来源"], "❓")
        logger.info("[DeepSeek价格] %s %s | %s | %s 模型",
                    图标, info["来源"], info["抓取时间"],
                     info["模型数"])

    def 启动定时刷新(self):
        if self.禁用网络:
            return

        def 循环():
            while True:
                time.sleep(self.刷新间隔)
                try:
                    self._尝试网络刷新()
                except Exception as e:
                    logger.warning("[DeepSeek价格] 定时刷新失败: %s", e)

        threading.Thread(target=循环, daemon=True).start()
        logger.info("[DeepSeek价格] ⏰ 定时刷新已启动")
