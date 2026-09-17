"""直链压力复现：内容唯一的多样文件 → 夸克 → 光鸭，并核对"引擎报完成 vs 实际落地"。

用法：
    python 工具/直链压力复现.py                      # 默认 120 小 + 5 大
    python 工具/直链压力复现.py --小 200 --大 4 --大MB 12
    python 工具/直链压力复现.py --保留                # 保留云上目录

与 直链失败复现.py 的分工：
  * 直链失败复现.py —— 文件名/大小/结构等**边界形态**；
  * 本脚本       —— **并发与吞吐压力**（高并发小文件 + 多段大文件），
                     重点是"引擎说完成了，目标端到底有没有、大小对不对"。
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

项目根 = Path(__file__).resolve().parents[1]
if str(项目根) not in sys.path:
    sys.path.insert(0, str(项目根))

from v8_3.配置 import 动作, 加载配置, 缓存参数, 传输参数
from v8_3.核心.传输引擎 import 传输请求

前缀 = "V8_3_压力"


def 造数据(源根: Path, 小数量: int, 大数量: int, 大MB: int) -> dict[str, int]:
    """每个文件内容**唯一**（避免秒传/同哈希把并发问题掩盖掉）。"""
    (源根 / "小").mkdir(parents=True, exist_ok=True)
    (源根 / "大").mkdir(parents=True, exist_ok=True)
    期望: dict[str, int] = {}
    seed = os.urandom(8)
    for i in range(小数量):
        数据 = (seed + i.to_bytes(4, "big")) * 64          # 约 768B，内容互不相同
        p = 源根 / "小" / f"s{i:04d}.bin"
        p.write_bytes(数据)
        期望[f"小/{p.name}"] = len(数据)
    for i in range(大数量):
        块 = (seed + b"B" + i.to_bytes(4, "big"))
        大小 = 大MB * 1024 * 1024
        数据 = (块 * (大小 // len(块) + 1))[:大小]
        p = 源根 / "大" / f"big{i}.bin"
        p.write_bytes(数据)
        期望[f"大/{p.name}"] = len(数据)
    return 期望


def 上传(适配器, 源根: Path, 远端根: str, 并发: int) -> list[str]:
    文件们 = sorted(p for p in 源根.rglob("*") if p.is_file())
    失败: list[str] = []

    def 传(f: Path):
        相对 = f.parent.relative_to(源根).as_posix()
        try:
            适配器.上传(str(f), f"{远端根}/{相对}", 名称=f.name,
                      任务ID=f"u{f.name}")
            return None
        except Exception as e:  # noqa: BLE001
            return f"{相对}/{f.name} → {str(e)[:110]}"

    with ThreadPoolExecutor(max_workers=并发) as 池:
        for 未 in as_completed([池.submit(传, f) for f in 文件们]):
            错误 = 未.result()
            if 错误:
                失败.append(错误)
    return 失败


def main() -> int:
    解析 = argparse.ArgumentParser()
    解析.add_argument("--小", type=int, default=120)
    解析.add_argument("--大", type=int, default=5)
    解析.add_argument("--大MB", type=int, default=12)
    解析.add_argument("--上传并发", type=int, default=6)
    解析.add_argument("--保留", action="store_true")
    解析.add_argument("--源", default="quark")
    解析.add_argument("--目标", default="guangya")
    参数 = 解析.parse_args()

    根 = Path(tempfile.mkdtemp(prefix="v8_3_压力_"))
    源根 = 根 / "源"
    期望 = 造数据(源根, 参数.小, 参数.大, 参数.大MB)
    print(f"① 生成 {len(期望)} 个文件（内容唯一）：小 {参数.小} 个，"
          f"大 {参数.大} 个 × {参数.大MB}MB")

    远端 = f"/{前缀}_{time.strftime('%m%d_%H%M%S')}"
    配置 = 加载配置()
    传输 = 传输参数(配置)
    with 动作(配置, 日志回调=lambda m, l="信息": None) as 动作对象:
        源盘 = 动作对象.适配器(参数.源)
        目标盘 = 动作对象.适配器(参数.目标)

        t0 = time.time()
        上传失败 = 上传(源盘, 源根, f"{远端}/源", 参数.上传并发)
        print(f"② 上传 {参数.源}：成功 {len(期望) - len(上传失败)} / "
              f"失败 {len(上传失败)}，{time.time() - t0:.1f}s")
        for x in 上传失败[:8]:
            print(f"   ❌ {x}")

        实际源 = {}
        for 子 in ("小", "大"):
            try:
                for 项 in 源盘.列目录(f"{远端}/源/{子}"):
                    实际源[f"{子}/{项.name}"] = int(项.size or 0)
            except Exception as e:  # noqa: BLE001
                print(f"   （源目录 {子} 列目录失败：{str(e)[:80]}）")

        print(f"③ 跨盘传输 {参数.源} → {参数.目标} …")
        事件: list[dict] = []
        引擎 = 动作对象.构建引擎([参数.源, 参数.目标])
        t1 = time.time()
        统计 = 引擎.传输(传输请求(
            源网盘=参数.源, 源路径=f"{远端}/源",
            目标网盘=参数.目标, 目标路径=f"{远端}/目标",
            重试次数=int(传输.get("重试次数") or 3),
            小文件阈值=int(传输.get("小文件阈值") or 8 * 1024 * 1024),
            小文件并发=int(传输.get("小文件并发") or 16),
            大文件并发=int(传输.get("大文件并发") or 3),
            断点续传=bool(传输.get("断点续传", True)),
            缓存=缓存参数(配置),
        ), 事件回调=lambda e: 事件.append(e.to_dict()))
        耗时 = time.time() - t1
        print(f"   引擎报告：总 {统计.总任务数}，完成 {统计.已完成}，"
              f"跳过 {统计.跳过}，失败 {统计.失败}，{耗时:.1f}s，"
              f"平均 {统计.平均速度:.2f} MiB/s")
        失败任务 = [(str((d.get("task") or {}).get("source_path") or "").split("/源/")[-1],
                 (d.get("task") or {}).get("error"))
                for d in 事件 if d.get("type") == "任务失败"]
        for p_, e_ in 失败任务[:10]:
            print(f"   ❌ {p_}：{str(e_)[:140]}")

        print("④ 核对目标端实际落地")
        落地 = {}
        for 子 in ("小", "大"):
            try:
                for 项 in 目标盘.列目录(f"{远端}/目标/{子}"):
                    落地[f"{子}/{项.name}"] = int(项.size or 0)
            except Exception as e:  # noqa: BLE001
                print(f"   （目标目录 {子} 列目录失败：{str(e)[:80]}）")
        缺失 = [k for k in 实际源 if k not in 落地]
        尺寸错 = [k for k in 落地 if k in 实际源 and 实际源[k] != 落地[k]]
        print(f"   源端 {len(实际源)} 个 → 目标端 {len(落地)} 个")
        print(f"   ★ 缺失 {len(缺失)}：{缺失[:8]}"
              + ("  …" if len(缺失) > 8 else ""))
        print(f"   ★ 大小不符 {len(尺寸错)}：{尺寸错[:8]}"
              + ("  …" if len(尺寸错) > 8 else ""))
        if not 缺失 and not 尺寸错 and 统计.失败 == 0:
            print("   ✅ 本次压力运行：全部落地、大小一致、无失败任务")
        else:
            print("   ⚠️ 本次运行存在差异，见上（这是复现结果，用于定位）")

        if not 参数.保留:
            for 适配器, 目录 in ((目标盘, f"{远端}/目标"), (源盘, f"{远端}/源")):
                try:
                    适配器.删除(目录)
                except Exception as e:  # noqa: BLE001
                    print(f"   ⚠️ 清理 {目录} 失败：{str(e)[:80]}")
            print("   已清理云上临时目录")
        else:
            print(f"   （已保留 {远端}）")

    shutil.rmtree(根, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
