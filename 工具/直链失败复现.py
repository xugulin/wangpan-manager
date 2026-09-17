"""直链传输失败复现：生成边界文件 → 上传夸克 → 跨盘传到光鸭 → 汇总失败原因。

用法：
    python 工具/直链失败复现.py            # 跑完整流程（联网，会在网盘建临时目录）
    python 工具/直链失败复现.py --保留       # 不清理云上临时目录，便于人工查看

设计：文件名/大小/结构都挑最容易让"直链下载 + 目标上传"出问题的形态。
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

项目根 = Path(__file__).resolve().parents[1]
if str(项目根) not in sys.path:
    sys.path.insert(0, str(项目根))

from v8_3.配置 import 动作, 加载配置, 传输参数, 缓存参数, 数据库路径
from v8_3.核心.传输引擎 import 传输请求
from v8_3.核心.模型 import 网盘类型

前缀 = "V8_3_直链诊断"


def 造文件(根: Path) -> list[dict]:
    """返回 [{'相对': 相对路径, '说明': 触发点}]，并在 根 下建好文件。"""
    样本: list[tuple[str, bytes, str]] = [
        ("普通文件.txt", b"hello v8_3\n" * 4, "基线：普通小文件"),
        ("空文件.txt", b"", "0 字节：直链/上传常对空文件特判"),
        ("一字节.bin", b"A", "1 字节：Range/分片边界"),
        ("带 空格 和#井号%百分号&与号+加号.txt", b"url-unsafe\n" * 3,
         "URL 不安全字符：未编码会 403/404"),
        ("emoji🎬🦆🅱️.mp4", b"emoji-name\n" * 3, "emoji：编码/长度问题"),
        ("引号'单\"双.txt", b"quotes\n", "引号：签名串拼接易错"),
        ("换行\n名字.txt", b"newline-in-name\n", "文件名含换行：最典型的传输杀手"),
        ("制表\t符.txt", b"tab-in-name\n", "文件名含制表符"),
        ("点·中点·符号.txt", b"middot\n", "非 ASCII 标点"),
        ("中文（全角）标点，。！？.txt", b"fullwidth\n", "全角标点"),
        (f"{'超长名字' * 20}.txt", b"long-name\n", "超长文件名（约 184 字节）"),
        ("末尾空格 .txt", b"trailing-space\n", "名字以空格结尾"),
        ("末尾点.txt.", b"trailing-dot\n", "名字以点结尾"),
        ("两个..点.txt", b"double-dot\n", "名字含 .."),
        ("-开头.txt", b"dash-start\n", "名字以 - 开头（命令行/签名风险）"),
        ("以.开头隐藏.txt", b"dot-start\n", "隐藏文件"),
        ("敏感词总统选举.txt", b"sensitive\n" * 2,
         "敏感词：光鸭可能拦截（会被改名或失败）"),
        ("大小写冲突.txt", b"lower\n", "与下一个文件仅大小写不同"),
        ("大小写冲突.TXT", b"UPPER\n", "大小写冲突对（部分网盘不区分）"),
        ("NFC_é_合成.txt", "café\n".encode("utf-8"), "Unicode NFC 合成形"),
        ("NFD_e+́_分解.txt", "cafe\u0301\n".encode("utf-8"), "Unicode NFD 分解形"),
        ("中等3MB.bin", bytes(3 * 1024 * 1024), "3MB：普通分片"),
        ("大文件12MB.bin", bytes(12 * 1024 * 1024),
         "12MB：≥8MB 会走多段 Range 下载"),
        ("深/层/目/录/嵌/套/测/试/第9层/文件.txt",
         b"deep\n", "深层目录：路径逐级创建"),
    ]
    结果: list[dict] = []
    for 名, 数据, 说明 in 样本:
        路径 = 根 / 名
        路径.parent.mkdir(parents=True, exist_ok=True)
        路径.write_bytes(数据)
        结果.append({"相对": 名, "说明": 说明, "大小": len(数据)})
    # 再加一层很深的嵌套
    深 = 根 / "/".join(f"d{i}" for i in range(1, 13))
    深.mkdir(parents=True, exist_ok=True)
    (深 / "最深.txt").write_bytes(b"deepest\n")
    结果.append({"相对": "/".join(f"d{i}" for i in range(1, 13)) + "/最深.txt",
             "说明": "12 层嵌套", "大小": 9})
    return 结果


def 上传到夸克(适配器, 本地根: Path, 远端根: str, 并发: int = 6) -> tuple[int, list[str]]:
    文件们 = sorted(p for p in 本地根.rglob("*") if p.is_file())
    目录集 = sorted({远端根 + "/" + f.parent.relative_to(本地根).as_posix()
                 for f in 文件们 if f.parent != 本地根}, key=len)
    适配器.确保目录(远端根)
    for 目录 in 目录集:
        if 目录.endswith("/."):
            continue
        适配器.确保目录(目录)
    成功, 失败 = 0, []

    def 传一个(文件: Path):
        相对目录 = 文件.parent.relative_to(本地根).as_posix()
        远端目录 = 远端根 + ("" if 相对目录 == "." else "/" + 相对目录)
        try:
            适配器.上传(str(文件), 远端目录, 名称=文件.name,
                      任务ID=f"probe-{abs(hash(str(文件))) % 10**8}")
            return None
        except Exception as e:  # noqa: BLE001
            return f"{文件.name}: {e}"

    with ThreadPoolExecutor(max_workers=并发) as 池:
        未来 = {池.submit(传一个, f): f for f in 文件们}
        for 未 in as_completed(未来):
            错误 = 未.result()
            if 错误:
                失败.append(错误)
            else:
                成功 += 1
    return 成功, 失败


def main() -> int:
    解析 = argparse.ArgumentParser()
    解析.add_argument("--保留", action="store_true", help="保留云上临时目录")
    解析.add_argument("--源", default="quark")
    解析.add_argument("--目标", default="guangya")
    参数 = 解析.parse_args()

    时间戳 = time.strftime("%m%d_%H%M%S")
    远端根 = f"/{前缀}_{时间戳}"
    本地 = Path(tempfile.mkdtemp(prefix="v8_3_直链_"))
    源根 = 本地 / "源"
    源根.mkdir(parents=True, exist_ok=True)
    样本 = 造文件(源根)
    print(f"① 生成 {len(样本)} 个边界样本 → {源根}")

    配置 = 加载配置()
    传输 = 传输参数(配置)
    with 动作(配置, 日志回调=lambda m, l="信息": None) as 动作对象:
        源适配器 = 动作对象.适配器(参数.源)
        目标适配器 = 动作对象.适配器(参数.目标)

        print(f"② 上传到 {参数.源}:{远端根}/源 …")
        成功, 失败 = 上传到夸克(源适配器, 源根, f"{远端根}/源")
        print(f"   上传：成功 {成功}，失败 {len(失败)}")
        for x in 失败:
            print(f"   ❌ 上传失败：{x}")

        print(f"③ 跨盘传输 {参数.源}:{远端根}/源 → {参数.目标}:{远端根}/目标 …")
        事件: list[dict] = []
        引擎 = 动作对象.构建引擎([参数.源, 参数.目标])
        请求 = 传输请求(
            源网盘=参数.源, 源路径=f"{远端根}/源",
            目标网盘=参数.目标, 目标路径=f"{远端根}/目标",
            并发=0,
            重试次数=int(传输.get("重试次数") or 3),
            小文件阈值=int(传输.get("小文件阈值") or 8 * 1024 * 1024),
            小文件并发=int(传输.get("小文件并发") or 16),
            大文件并发=int(传输.get("大文件并发") or 3),
            断点续传=bool(传输.get("断点续传", True)),
            缓存=缓存参数(配置),
        )
        统计 = 引擎.传输(请求, 事件回调=lambda e: 事件.append(e.to_dict()))
        print(f"   完成 {统计.已完成}，跳过 {统计.跳过}，失败 {统计.失败}，"
              f"耗时 {统计.结束时间 - 统计.开始时间:.1f}s")

        print("\n④ 逐个任务结果")
        说明表 = {x["相对"]: x["说明"] for x in 样本}
        坏: list[dict] = []
        for 数据 in 事件:
            任务 = 数据.get("task") or {}
            if 数据.get("type") not in ("任务完成", "任务失败", "任务跳过"):
                continue
            源路径 = str(任务.get("source_path") or "")
            相对 = 源路径.split("/源/", 1)[-1]
            状态 = 数据.get("type")
            标记 = {"任务完成": "✅", "任务跳过": "⏭", "任务失败": "❌"}[状态]
            if 状态 != "任务完成":
                坏.append({"相对": 相对, "状态": 状态,
                         "错误": 任务.get("error") or 任务.get("skip_reason") or "",
                         "说明": 说明表.get(相对, "")})
            print(f"   {标记} {相对[:56]:<58} {说明表.get(相对, '')[:26]}"
                  + (f"  ← {任务.get('error') or 任务.get('skip_reason')}"
                     if 状态 != "任务完成" else ""))

        print("\n⑤ 失败/跳过汇总")
        if not 坏:
            print("   （无失败）")
        for 项 in 坏:
            错误 = str(项["错误"])
            类 = ("目标已存在" if "已存在" in 错误 else
                 "敏感词" if "敏感词" in 错误 else
                 "直链/HTTP" if any(k in 错误 for k in ("403", "404", "签名", "直链", "HTTP"))
                 else "上传" if "上传" in 错误 else
                 "下载" if ("下载" in 错误 or "Download" in 错误) else "其它")
            print(f"   ❌ {项['相对'][:52]:<54} [{类}] {错误[:120]}")

        if not 参数.保留:
            for 适配器, 目录 in ((目标适配器, f"{远端根}/目标"), (源适配器, f"{远端根}/源")):
                try:
                    适配器.删除(目录)
                    print(f"   已清理 {目录}")
                except Exception as e:  # noqa: BLE001
                    print(f"   ⚠️ 清理 {目录} 失败：{e}")
        else:
            print(f"   （已保留云上目录 {远端根}，请人工检查后删除）")

    shutil.rmtree(本地, ignore_errors=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)
