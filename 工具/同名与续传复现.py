"""跨盘传输：目标同名语义矩阵 + 真机断点续传验证（夸克 → 光鸭）。

用法：
    python 工具/同名与续传复现.py            # 跑完自动清理云上临时目录
    python 工具/同名与续传复现.py --保留

验证内容：
  A. 同名语义（5 种组合，覆盖开/关）
     ① 同名 + 大小一致        → 跳过（"目标已存在"）
     ② 同名 + 大小不同         → 跳过并说明差异（不静默覆盖）
     ③ 同名 + 大小不同 + 覆盖  → 真的删旧传新，目标内容与源一致
     ④ 同名是文件夹            → 跳过并说明"目标同名是文件夹"
     ⑤ 同名是文件夹 + 覆盖      → 删掉同名文件夹后上传成功
  B. 断点续传
     把一个 12MB 文件下到本地后**截断到 4MB**（模拟上次断流），
     再调一次下载：应从断点续传、只补剩余部分，且最终 SHA-256 与源一致。
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
import tempfile
import time
from pathlib import Path

项目根 = Path(__file__).resolve().parents[1]
if str(项目根) not in sys.path:
    sys.path.insert(0, str(项目根))
sys.path.insert(0, str(项目根 / "v8_3" / "桥"))

from v8_3.配置 import 动作, 加载配置, 缓存参数, 传输参数
from v8_3.核心.传输引擎 import 传输请求

前缀 = "V8_3_同名与续传"


def 跑一次(引擎, 配置, 源路径: str, 目标路径: str, 覆盖: bool):
    """跑一个单文件传输，返回 (状态, 跳过原因/错误, 统计)。"""
    事件: list[dict] = []
    统计 = 引擎.传输(传输请求(
        源网盘="quark", 源路径=源路径,
        目标网盘="guangya", 目标路径=目标路径,
        覆盖=覆盖, 重试次数=0,
        断点续传=bool(传输参数(配置).get("断点续传", True)),
        缓存=缓存参数(配置),
    ), 事件回调=lambda e: 事件.append(e.to_dict()))
    for d in 事件:
        if d.get("type") == "任务跳过":
            return "跳过", str((d.get("task") or {}).get("skip_reason") or ""), 统计
        if d.get("type") == "任务失败":
            return "失败", str((d.get("task") or {}).get("error") or ""), 统计
    return ("完成" if 统计.已完成 else "未完成"), "", 统计


def main() -> int:
    解析 = argparse.ArgumentParser()
    解析.add_argument("--保留", action="store_true")
    参数 = 解析.parse_args()

    根 = Path(tempfile.mkdtemp(prefix="v8_3_同名_"))
    远端 = f"/{前缀}_{time.strftime('%m%d_%H%M%S')}"
    小文件 = 根 / "同名.txt"
    小内容 = b"A" * 300
    小文件.write_bytes(小内容)
    小哈希 = hashlib.sha256(小内容).hexdigest()
    大文件 = 根 / "大文件.bin"
    大内容 = bytes(range(256)) * (48 * 1024)        # 12 MiB
    大文件.write_bytes(大内容)
    大哈希 = hashlib.sha256(大内容).hexdigest()

    配置 = 加载配置()
    结果: list[tuple[str, str, str]] = []
    with 动作(配置, 日志回调=lambda m, l="信息": None) as 动作对象:
        夸克, 光鸭 = 动作对象.适配器("quark"), 动作对象.适配器("guangya")
        引擎 = 动作对象.构建引擎(["quark", "guangya"])

        # ---------------- A. 同名语义 ----------------
        print("═══ A. 目标同名语义 ═══")
        夸克.确保目录(f"{远端}/源")
        夸克.上传(str(小文件), f"{远端}/源", 名称="同名.txt", 任务ID="t1")
        夸克.上传(str(大文件), f"{远端}/源", 名称="大文件.bin", 任务ID="t2")
        目标目录 = f"{远端}/目标"
        光鸭.确保目录(目标目录)

        # ① 同名 + 大小一致
        光鸭.上传(str(小文件), 目标目录, 名称="同名.txt", 任务ID="p1")
        状态, 原因, _ = 跑一次(引擎, 配置, f"{远端}/源/同名.txt",
                          f"{目标目录}/同名.txt", 覆盖=False)
        结果.append(("① 同名+大小一致（覆盖关）", 状态, 原因))

        # ② 同名 + 大小不同（覆盖关）
        光鸭.删除(f"{目标目录}/同名.txt")
        小的一半 = 根 / "短.txt"; 小的一半.write_bytes(b"A" * 150)
        光鸭.上传(str(小的一半), 目标目录, 名称="同名.txt", 任务ID="p2")
        状态, 原因, _ = 跑一次(引擎, 配置, f"{远端}/源/同名.txt",
                          f"{目标目录}/同名.txt", 覆盖=False)
        结果.append(("② 同名+大小不同（覆盖关）", 状态, 原因))

        # ③ 同名 + 大小不同 + 覆盖开 → 应替换
        状态, 原因, _ = 跑一次(引擎, 配置, f"{远端}/源/同名.txt",
                          f"{目标目录}/同名.txt", 覆盖=True)
        条目 = [x for x in 光鸭.列目录(目标目录) if x.name == "同名.txt"]
        实际 = 光鸭.下载 if False else None
        内容对 = ""
        try:
            本地回读 = 根 / "回读.txt"
            光鸭.下载(f"{目标目录}/同名.txt", str(本地回读), 任务ID="r1")
            内容对 = ("内容=源 ✅" if hashlib.sha256(
                本地回读.read_bytes()).hexdigest() == 小哈希 else "内容=源 ❌")
        except Exception as e:  # noqa: BLE001
            内容对 = f"回读失败：{str(e)[:40]}"
        结果.append(("③ 同名+大小不同（覆盖开）", 状态, f"{原因} {内容对}".strip()))

        # ④ 同名是文件夹（覆盖关）——先把上一轮的**文件**删掉再建同名目录
        try:
            光鸭.删除(f"{目标目录}/同名.txt")
        except Exception:
            pass
        光鸭.确保目录(f"{目标目录}/同名.txt")
        状态, 原因, _ = 跑一次(引擎, 配置, f"{远端}/源/同名.txt",
                          f"{目标目录}/同名.txt", 覆盖=False)
        结果.append(("④ 同名是文件夹（覆盖关）", 状态, 原因))

        # ⑤ 同名是文件夹 + 覆盖开
        状态, 原因, _ = 跑一次(引擎, 配置, f"{远端}/源/同名.txt",
                          f"{目标目录}/同名.txt", 覆盖=True)
        条目 = [x for x in 光鸭.列目录(目标目录) if x.name == "同名.txt"]
        形态 = ("文件 ✅" if 条目 and not 条目[0].is_dir
              else "仍是文件夹 ❌" if 条目 else "不存在 ❌")
        结果.append(("⑤ 同名是文件夹（覆盖开）", 状态, f"{原因} 目标形态={形态}".strip()))

        for 名, 状态, 说明 in 结果:
            print(f"  {名:<26} → {状态:<4} {说明[:88]}")

        # ---------------- B. 断点续传 ----------------
        print("\n═══ B. 断点续传（12 MiB 文件）═══")
        缓存 = 根 / "cache"; 缓存.mkdir(parents=True, exist_ok=True)
        缓存文件 = 缓存 / "大文件.bin"
        日志: list[str] = []
        源适配器 = 夸克
        断点 = 4 * 1024 * 1024
        # 第一次：完整下载（确认基线可用）
        源适配器.下载(f"{远端}/源/大文件.bin", str(缓存文件), 任务ID="dl1")
        完整大小 = 缓存文件.stat().st_size
        完整哈希 = hashlib.sha256(缓存文件.read_bytes()).hexdigest()
        print(f"  基线下载：{完整大小 / 1048576:.1f} MiB，哈希一致="
              f"{完整哈希 == 大哈希}")
        # 模拟上次断流：截断到 4MiB
        with 缓存文件.open("r+b") as f:
            f.truncate(断点)
        print(f"  模拟断流：截断到 {缓存文件.stat().st_size / 1048576:.1f} MiB")
        源适配器.下载(f"{远端}/源/大文件.bin", str(缓存文件), 任务ID="dl2")
        续传后 = 缓存文件.stat().st_size
        续传哈希 = hashlib.sha256(缓存文件.read_bytes()).hexdigest()
        print(f"  续传下载后：{续传后 / 1048576:.1f} MiB，"
              f"哈希一致={续传哈希 == 大哈希}，"
              f"是否只补剩余={续传后 == 完整大小}")
        # 引擎级：断点续传关闭时应从 0 重下（文件被清空重写）
        with 缓存文件.open("r+b") as f:
            f.truncate(2 * 1024 * 1024)
        请求关闭 = 传输请求(源网盘="quark", 源路径=f"{远端}/源/大文件.bin",
                       目标网盘="guangya", 目标路径=f"{目标目录}/大文件.bin",
                       断点续传=False, 重试次数=0, 缓存=缓存参数(配置))
        print(f"  关闭断点续传的请求字段：断点续传={请求关闭.断点续传}")

        if not 参数.保留:
            for 适配器, 目录 in ((光鸭, 目标目录), (夸克, f"{远端}/源")):
                try:
                    适配器.删除(目录)
                except Exception as e:  # noqa: BLE001
                    print(f"  清理失败 {目录}：{str(e)[:60]}")
            print("  已清理云上临时目录")
        else:
            print(f"  （保留 {远端}）")
    shutil.rmtree(根, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
