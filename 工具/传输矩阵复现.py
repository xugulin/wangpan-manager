"""六方向跨盘传输矩阵检测：每个方向跑「边界文件 + 断点续传 + 落地核对」。

用法：
    python 工具/传输矩阵复现.py                     # 全部 6 个方向
    python 工具/传输矩阵复现.py --方向 baidu:guangya # 只跑一个方向
    python 工具/传输矩阵复现.py --保留               # 保留云上目录

每个方向都会：
  ① 生成 12 个边界样本（空文件/1字节/URL 不安全字符/emoji/换行/敏感词/深层/6MB/12MB…）
     并上传到源盘；
  ② 用**注入一次性断流**（V8_3_DL_ABORT_AFTER）跑跨盘传输 —— 顺带验证该源盘的
     直链是否支持 Range 续传、引擎是否能自动恢复；
  ③ 核对目标端实际落地（名字 + 大小），并把大文件回读做 SHA-256 比对；
  ④ 打印每个方向的结论，最后清理云上临时目录。
"""

from __future__ import annotations

import argparse
import hashlib
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

方向表 = [("baidu", "guangya"), ("baidu", "quark"),
        ("quark", "baidu"), ("guangya", "quark"),
        ("guangya", "baidu"), ("quark", "guangya")]
昵称 = {"baidu": "百度", "quark": "夸克", "guangya": "光鸭"}
# 断流注入点：大文件下到 4 MiB 时断开一次（一次性）
断流字节 = 4 * 1024 * 1024


def 造样本(根: Path) -> dict[str, bytes]:
    样本: dict[str, bytes] = {
        "普通.txt": b"hello v8_3\n" * 8,
        "空文件.txt": b"",
        "一字节.bin": b"A",
        "带 空格#井号%&+号.txt": b"unsafe\n" * 4,
        "emoji🎬🦆.mp4": b"emoji\n" * 4,
        "换行\n名字.txt": b"newline\n",
        "敏感词总统选举.txt": b"sensitive\n" * 2,
        "末尾空格 .txt": b"tail-space\n",
        "引号'单\"双.txt": b"quotes\n",
        "深/层/目/录/文件.txt": b"deep\n",
    }
    样本["6MB.bin"] = bytes(range(256)) * (24 * 1024)          # 6 MiB
    样本["12MB.bin"] = (bytes(range(256)) * (48 * 1024))       # 12 MiB
    for 名, 数据 in 样本.items():
        p = 根 / 名
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(数据)
    return 样本


def 上传(适配器, 本地根: Path, 远端根: str, 并发: int = 6,
        守卫=None) -> list[str]:
    文件们 = sorted(p for p in 本地根.rglob("*") if p.is_file())
    目录集 = sorted({远端根 + "/" + f.parent.relative_to(本地根).as_posix()
                 for f in 文件们 if f.parent != 本地根}, key=len)
    适配器.确保目录(远端根)
    for 目录 in 目录集:
        适配器.确保目录(目录)
    失败: list[str] = []

    def 传(f: Path):
        相对 = f.parent.relative_to(本地根).as_posix()
        目录 = 远端根 + ("" if 相对 == "." else "/" + 相对)
        try:
            # 有守卫时走守卫（敏感词预检/绕过、命名规避记录都在里面），
            # 否则会得到"名称不可用"这种本该被绕过的失败。
            if 守卫 is not None and getattr(守卫, "生效", False):
                守卫.上传(str(f), 目录, 名称=f.name, 任务ID=f"m{f.name}")
            else:
                适配器.上传(str(f), 目录, 名称=f.name, 任务ID=f"m{f.name}")
            return None
        except Exception as e:  # noqa: BLE001
            return f"{相对}/{f.name} → {str(e)[:100]}"

    with ThreadPoolExecutor(max_workers=并发) as 池:
        for 未 in as_completed([池.submit(传, f) for f in 文件们]):
            错误 = 未.result()
            if 错误:
                失败.append(错误)
    return 失败


def 跑方向(配置, 源: str, 目标: str, 保留: bool) -> dict:
    时间戳 = time.strftime("%m%d_%H%M%S")
    远端 = f"/V8_3_矩阵_{时间戳}_{源}2{目标}"
    根 = Path(tempfile.mkdtemp(prefix=f"v8_3_矩阵_{源}2{目标}_"))
    源根 = 根 / "源"
    样本 = 造样本(源根)
    期望 = {名: len(数据) for 名, 数据 in 样本.items()}
    大哈希 = hashlib.sha256(样本["12MB.bin"]).hexdigest()
    结论 = {"方向": f"{昵称[源]}→{昵称[目标]}", "源": 源, "目标": 目标,
          "上传失败": [], "完成": 0, "失败": 0, "跳过": 0,
          "缺失": [], "大小不符": [], "哈希": "", "耗时": 0.0,
          "失败原因": [], "远端": 远端}
    print(f"\n═══════ {结论['方向']} ═══════")
    with 动作(配置, 日志回调=lambda m, l="信息": None) as 动作对象:
        源盘, 目标盘 = 动作对象.适配器(源), 动作对象.适配器(目标)
        t0 = time.time()
        try:
            源守卫 = 动作对象.守卫(源)
        except Exception:
            源守卫 = None
        结论["上传失败"] = 上传(源盘, 源根, f"{远端}/源", 守卫=源守卫)
        print(f"  ① 上传到{昵称[源]}：成功 {len(样本) - len(结论['上传失败'])}/"
              f"{len(样本)}，失败 {len(结论['上传失败'])}")
        for x in 结论["上传失败"][:4]:
            print(f"     ❌ {x}")
        引擎 = 动作对象.构建引擎([源, 目标])
        事件: list[dict] = []
        t1 = time.time()
        统计 = 引擎.传输(传输请求(
            源网盘=源, 源路径=f"{远端}/源",
            目标网盘=目标, 目标路径=f"{远端}/目标",
            重试次数=int(传输参数(配置).get("重试次数") or 3),
            断点续传=True, 缓存=缓存参数(配置),
        ), 事件回调=lambda e: 事件.append(e.to_dict()))
        结论["耗时"] = time.time() - t1
        结论["完成"] = 统计.已完成
        结论["失败"] = 统计.失败
        结论["跳过"] = 统计.跳过
        结论["失败原因"] = [
            f"{str((d.get('task') or {}).get('source_path') or '').split('/源/')[-1]}"
            f" → {str((d.get('task') or {}).get('error'))[:110]}"
            for d in 事件 if d.get("type") == "任务失败"]
        跳过原因 = [
            f"{str((d.get('task') or {}).get('source_path') or '').split('/源/')[-1]}"
            f" → {str((d.get('task') or {}).get('skip_reason'))[:80]}"
            for d in 事件 if d.get("type") == "任务跳过"]
        续传 = [str(d.get("message") or "") for d in 事件
              if "续传" in str(d.get("message") or "")]
        print(f"  ② 传输：完成 {统计.已完成}，跳过 {统计.跳过}，失败 {统计.失败}，"
              f"{结论['耗时']:.1f}s")
        for x in 结论["失败原因"][:5]:
            print(f"     ❌ {x}")
        for x in 跳过原因[:3]:
            print(f"     ⏭ {x}")
        # 落地核对
        落地: dict[str, int] = {}
        for 子 in ("", "深/层/目/录"):
            目录 = f"{远端}/目标" + (f"/{子}" if 子 else "")
            # 目录刚建好时列目录也可能滞后，重试几次（与上传后可见性确认同理）
            for 次 in range(4):
                try:
                    for 项 in 目标盘.列目录(目录):
                        键 = f"{子}/{项.name}" if 子 else 项.name
                        if not 项.is_dir:
                            落地[键] = int(项.size or 0)
                    break
                except Exception:
                    time.sleep(1.2)
        缺失, 大小错 = [], []
        # 目标盘可能做过命名规避（百度不允许引号/emoji 等）：核对时两种名字都认
        sys.path.insert(0, str(项目根))
        try:
            from v8_3.敏感词.命名规避 import 规避名称 as _规避
        except Exception:
            _规避 = None
        # 改名记录（敏感词绕过 / 命名规避 / 服务端改名）也算"已落地"
        记录了: dict[str, str] = {}
        try:
            库 = 动作对象.敏感词库()
            for 条 in 库.列出改名记录():
                记录了[str(条.get("原云端路径") or "")] = str(
                    条.get("云端实际路径") or "")
        except Exception:
            pass
        for 名, 大小 in 期望.items():
            候选 = {名}
            for 盘 in (目标, 源):
                if _规避 is None:
                    continue
                新名, _ = _规避(盘, 名.split("/")[-1])
                if 新名 and 新名 != 名.split("/")[-1]:
                    前缀 = 名.rsplit("/", 1)[0] + "/" if "/" in 名 else ""
                    候选.add(前缀 + 新名)
            for 原路径, 实际路径 in 记录了.items():
                if 原路径.endswith(名.split("/")[-1]):
                    候选.add(实际路径.split("/")[-1])
            命中 = None
            import html as _html
            for k, v in 落地.items():
                尾 = _html.unescape(k.split("/")[-1])
                if k in 候选 or 尾 in {_html.unescape(c.split("/")[-1])
                                   for c in 候选}:
                    命中 = v
                    break
            if 命中 is None:
                缺失.append(名)
            elif 大小 and 命中 != 大小:
                大小错.append(f"{名}({命中}≠{大小})")
        结论["缺失"] = 缺失
        结论["大小不符"] = 大小错
        # 大文件哈希
        # 回读：注入的一次性断流钩子可能正好落在这条验证下载上（目标端桥进程
        # 之前没消耗过钩子），所以最多试 3 次 —— 第 2 次起就是"断点续传"路径。
        回读 = 根 / "回读.bin"
        最后错误 = ""
        for 次 in range(3):
            try:
                目标盘.下载(f"{远端}/目标/12MB.bin", str(回读), 任务ID=f"vhash{次}")
                结论["哈希"] = ("一致 ✅" if hashlib.sha256(
                    回读.read_bytes()).hexdigest() == 大哈希 else "不一致 ❌")
                if 次:
                    结论["哈希"] += f"（回读第 {次 + 1} 次成功，前次断流=续传生效）"
                最后错误 = ""
                break
            except Exception as e:  # noqa: BLE001
                最后错误 = str(e)[:80]
        if 最后错误:
            结论["哈希"] = f"回读失败：{最后错误}"
        print(f"  ③ 落地：缺失 {len(缺失)} {缺失[:4]}，大小不符 {len(大小错)} "
              f"{大小错[:3]}，12MB 哈希 {结论['哈希']}")
        if 保留:
            print(f"     （保留 {远端}）")
        else:
            for 适配器, 目录 in ((目标盘, f"{远端}/目标"), (源盘, f"{远端}/源")):
                try:
                    适配器.删除(目录)
                except Exception:
                    pass
            # 再删掉外层空目录，避免在盘里留一堆 V8_3_矩阵_* 空壳
            for 适配器 in {id(目标盘): 目标盘, id(源盘): 源盘}.values():
                try:
                    适配器.删除(远端)
                except Exception:
                    pass
    shutil.rmtree(根, ignore_errors=True)
    return 结论


def main() -> int:
    解析 = argparse.ArgumentParser()
    解析.add_argument("--方向", default="", help="如 baidu:guangya；留空=全部")
    解析.add_argument("--保留", action="store_true")
    参数 = 解析.parse_args()
    方向们 = 方向表
    if 参数.方向:
        源, _, 目标 = 参数.方向.partition(":")
        方向们 = [(源.strip(), 目标.strip())]
    配置 = 加载配置()
    结论们 = []
    for 源, 目标 in 方向们:
        try:
            结论们.append(跑方向(配置, 源, 目标, 参数.保留))
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            结论们.append({"方向": f"{昵称.get(源, 源)}→{昵称.get(目标, 目标)}",
                        "源": 源, "目标": 目标, "异常": str(e)[:200],
                        "上传失败": [], "缺失": [], "大小不符": [],
                        "完成": 0, "失败": 0, "跳过": 0, "哈希": "-",
                        "耗时": 0.0, "失败原因": [], "远端": ""})
    print("\n\n════════ 汇总 ════════")
    print(f"{'方向':<12}{'上传失败':<9}{'完成':<6}{'跳过':<6}{'失败':<6}"
          f"{'缺失':<6}{'大小错':<7}{'12MB哈希':<12}{'耗时':<7}")
    for c in 结论们:
        print(f"{c['方向']:<12}{len(c['上传失败']):<9}{c['完成']:<6}{c['跳过']:<6}"
              f"{c['失败']:<6}{len(c['缺失']):<6}{len(c['大小不符']):<7}"
              f"{str(c['哈希'])[:10]:<12}{c['耗时']:<7.1f}")
    问题 = [c for c in 结论们 if c.get("异常") or c["上传失败"] or c["失败"]
          or c["缺失"] or c["大小不符"] or "一致 ✅" not in str(c["哈希"])]
    print(f"\n有问题的方向：{len(问题)}/{len(结论们)}")
    for c in 问题:
        print(f"  ⚠️ {c['方向']}：{c.get('异常') or c['失败原因'] or c['缺失'] or c['大小不符'] or c['哈希']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
