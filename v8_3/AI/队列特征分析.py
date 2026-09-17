# v8_3/AI/队列特征分析.py
"""从文件列表提取聚合特征 - 供 AI 决策用（移植自 V8 ``AI层/队列特征分析.py``）。

与 V8 的差异
============
* 纯计算模块，无网络、无配置、无落盘依赖，逻辑与 V8 一致（逐行对齐）；
* 只改了模块头注释；公开 API 不变：

构造函数签名
============
本模块只有静态方法，无需构造。

公开方法
========
``队列特征分析器.分析(文件列表)`` → dict
``队列特征分析器.计算指纹(特征)`` → str（md5）
``队列特征分析器.相似度(a, b)`` → float（0~1）
"""
import os
import hashlib
import json
import math
from typing import List, Tuple


_类别映射 = {
    "媒体": {'.mp4', '.avi', '.mkv', '.mov', '.wmv', '.flv',
            '.mp3', '.wav', '.flac', '.aac', '.ogg',
            '.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp'},
    "游戏ROM": {'.nes', '.smc', '.sfc', '.gba', '.gbc', '.gb',
               '.nds', '.iso', '.cue', '.bin'},
    "文档": {'.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt',
            '.pptx', '.txt', '.md', '.log'},
    "代码": {'.py', '.js', '.ts', '.java', '.c', '.cpp', '.go',
            '.rs', '.rb', '.php', '.sh'},
    "压缩包": {'.zip', '.rar', '.7z', '.tar', '.gz', '.bz2'},
}

_敏感扩展名 = {'.txt', '.ini', '.cfg', '.conf'}
_大小桶键 = ["小(<1MB)", "中(1-10MB)", "大(10-100MB)", "巨大(>100MB)"]
_类型桶键 = ["媒体", "游戏ROM", "文档", "代码", "压缩包", "其他"]


class 队列特征分析器:

    @staticmethod
    def 分析(文件列表: List[Tuple[str, str, int]]) -> dict:
        if not 文件列表:
            return 队列特征分析器._空特征()

        总任务数 = len(文件列表)
        总字节 = 0
        大小桶 = {k: 0 for k in _大小桶键}
        类型桶 = {k: 0 for k in _类型桶键}
        含中文名数 = 0
        敏感扩展名数 = 0
        源目录集合 = set()
        目标目录集合 = set()
        最大文件字节 = 0

        for 源, 目标, 大小 in 文件列表:
            总字节 += 大小
            if 大小 > 最大文件字节:
                最大文件字节 = 大小

            if 大小 < 1024 * 1024:
                大小桶["小(<1MB)"] += 1
            elif 大小 < 10 * 1024 * 1024:
                大小桶["中(1-10MB)"] += 1
            elif 大小 < 100 * 1024 * 1024:
                大小桶["大(10-100MB)"] += 1
            else:
                大小桶["巨大(>100MB)"] += 1

            文件名 = os.path.basename(目标)
            扩展名 = os.path.splitext(文件名)[1].lower()
            命中类别 = "其他"
            for 类别, 扩展集 in _类别映射.items():
                if 扩展名 in 扩展集:
                    命中类别 = 类别
                    break
            类型桶[命中类别] += 1

            if 队列特征分析器._含中文(文件名):
                含中文名数 += 1
            if 扩展名 in _敏感扩展名:
                敏感扩展名数 += 1

            源目录集合.add(os.path.dirname(源))
            目标目录集合.add(os.path.dirname(目标))

        平均大小 = 总字节 // 总任务数 if 总任务数 else 0

        return {
            "总任务数": 总任务数,
            "总字节": 总字节,
            "总MB": round(总字节 / 1024 / 1024, 2),
            "平均文件大小_MB": round(平均大小 / 1024 / 1024, 2),
            "最大文件_MB": round(最大文件字节 / 1024 / 1024, 2),
            "大小分布": 大小桶,
            "类型分布": 类型桶,
            "含中文文件名数": 含中文名数,
            "敏感扩展名数": 敏感扩展名数,
            "源目录数": len(源目录集合),
            "目标目录数": len(目标目录集合),
        }

    @staticmethod
    def _含中文(s: str) -> bool:
        for c in s:
            if '\u4e00' <= c <= '\u9fff':
                return True
        return False

    @staticmethod
    def _空特征() -> dict:
        return {
            "总任务数": 0, "总字节": 0, "总MB": 0,
            "平均文件大小_MB": 0, "最大文件_MB": 0,
            "大小分布": {k: 0 for k in _大小桶键},
            "类型分布": {k: 0 for k in _类型桶键},
            "含中文文件名数": 0, "敏感扩展名数": 0,
            "源目录数": 0, "目标目录数": 0,
        }

    @staticmethod
    def 计算指纹(特征: dict) -> str:
        精简 = {
            "总数": 特征.get("总任务数", 0),
            "总MB": int(特征.get("总MB", 0)),
            "大小分布": 特征.get("大小分布", {}),
            "类型分布": 特征.get("类型分布", {}),
            "中文": 特征.get("含中文文件名数", 0),
        }
        文本 = json.dumps(精简, sort_keys=True,
                         ensure_ascii=False)
        return hashlib.md5(文本.encode('utf-8')).hexdigest()

    @staticmethod
    def 相似度(a: dict, b: dict) -> float:
        if not a or not b:
            return 0.0
        总字节相似 = 队列特征分析器._比例接近(
            a.get("总字节", 0), b.get("总字节", 0), tol=0.5)
        va = [a.get("大小分布", {}).get(k, 0) for k in _大小桶键]
        vb = [b.get("大小分布", {}).get(k, 0) for k in _大小桶键]
        大小相似 = 队列特征分析器._余弦(va, vb)
        va = [a.get("类型分布", {}).get(k, 0) for k in _类型桶键]
        vb = [b.get("类型分布", {}).get(k, 0) for k in _类型桶键]
        类型相似 = 队列特征分析器._余弦(va, vb)
        return 总字节相似 * 0.3 + 大小相似 * 0.4 + 类型相似 * 0.3

    @staticmethod
    def _比例接近(x: float, y: float, tol: float = 0.3) -> float:
        if max(x, y) == 0:
            return 1.0
        return max(0.0, 1.0 - min(1.0,
                                abs(x - y) / max(x, y, 1) / tol))

    @staticmethod
    def _余弦(a: list, b: list) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a)) or 1.0
        nb = math.sqrt(sum(y * y for y in b)) or 1.0
        return dot / (na * nb)
