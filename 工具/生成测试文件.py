#!/usr/bin/env python3
"""生成跨网盘传输测试文件。

示例：
    python 工具/生成测试文件.py --目录 /tmp/v8_3_test --小文件数 200 \
        --小文件大小 4096 --大文件MB 20 --嵌套
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import string
from pathlib import Path


def 随机名(n: int = 12) -> str:
    return "".join(random.choice(string.ascii_lowercase + string.digits)
                   for _ in range(n))


def 写文件(路径: Path, 大小: int, 种子: bytes = b"v8_3") -> str:
    路径.parent.mkdir(parents=True, exist_ok=True)
    哈希 = hashlib.sha256()
    with 路径.open("wb") as f:
        剩余 = 大小
        while 剩余 > 0:
            块 = (种子 + os.urandom(min(65536, 剩余)))
            f.write(块)
            哈希.update(块)
            剩余 -= len(块)
    return 哈希.hexdigest()


def main(argv=None) -> int:
    解析器 = argparse.ArgumentParser()
    解析器.add_argument("--目录", required=True)
    解析器.add_argument("--小文件数", type=int, default=100)
    解析器.add_argument("--小文件大小", type=int, default=4096)
    解析器.add_argument("--大文件MB", type=int, default=0)
    解析器.add_argument("--大文件数", type=int, default=1)
    解析器.add_argument("--嵌套", action="store_true")
    解析器.add_argument("--随机", action="store_true")
    参数 = 解析器.parse_args(argv)
    if 参数.随机:
        random.seed()

    根 = Path(参数.目录).expanduser().resolve()
    根.mkdir(parents=True, exist_ok=True)
    清单: dict[str, str] = {}

    def 放置(子目录: str, 文件名: str, 大小: int):
        相对 = f"{子目录}/{文件名}".strip("/")
        清单[相对] = 写文件(根 / 相对, 大小, 种子=相对.encode())

    for i in range(参数.小文件数):
        子目录 = ""
        if 参数.嵌套:
            子目录 = f"目录_{i % 7:02d}" + (f"/子目录_{i % 3}" if i % 5 == 0 else "")
        放置(子目录, f"小文件_{i:05d}_{随机名(6)}.txt", 参数.小文件大小)
    for i in range(参数.大文件数):
        放置("大文件", f"大文件_{i:02d}_{随机名(6)}.bin",
             参数.大文件MB * 1024 * 1024)
    (根 / "清单.json").write_text(
        json.dumps(清单, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已生成 {len(清单)} 个文件，共 "
          f"{sum(p.stat().st_size for p in 根.rglob('*') if p.is_file())} 字节")
    print(f"根目录：{根}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
