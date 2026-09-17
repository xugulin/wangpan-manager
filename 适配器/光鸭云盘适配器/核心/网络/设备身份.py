# 光鸭云盘适配器/核心/网络/设备身份.py
"""
设备身份
作用：管理设备ID、设备类型、设备指纹（对应请求头 did/dt/smid）
持久化到 ~/.光鸭云盘/设备信息.json
"""

import base64
import json
import platform
import uuid
from pathlib import Path

存储目录 = Path.home() / ".光鸭云盘"
存储目录.mkdir(parents=True, exist_ok=True)
存储文件 = 存储目录 / "设备信息.json"


def _读取存储() -> dict:
    if 存储文件.exists():
        try:
            return json.loads(存储文件.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _写入存储(数据: dict) -> None:
    存储文件.write_text(
        json.dumps(数据, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def 获取设备ID() -> str:
    """32 位十六进制设备ID，持久化"""
    数据 = _读取存储()
    if "设备ID" not in 数据:
        数据["设备ID"] = uuid.uuid4().hex
        _写入存储(数据)
    return 数据["设备ID"]


def 获取设备指纹() -> str:
    """Base64 编码设备指纹（对应 smid）"""
    数据 = _读取存储()
    if "设备指纹" in 数据:
        return 数据["设备指纹"]

    原始字段 = [
        platform.system(),
        platform.release(),
        platform.machine(),
        platform.python_version(),
        str(uuid.getnode()),
    ]
    指纹 = base64.b64encode(
        "|".join(原始字段).encode("utf-8")).decode("utf-8")
    数据["设备指纹"] = 指纹
    _写入存储(数据)
    return 指纹


def 获取设备类型() -> str:
    """设备类型（抓包中 dt=4）"""
    return "4"