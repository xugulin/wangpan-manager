#!/usr/bin/env python3
"""跨网盘传输基准：生成测试文件 → 上传到源网盘 → 跨网盘传输 → 打印吞吐。

示例：
    python 工具/传输基准.py --源 夸克 --目标 光鸭 \
        --文件数 100 --文件KB 4 --大文件MB 10 --并发 16
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
import time
from pathlib import Path

项目根 = Path(__file__).resolve().parents[1]
if str(项目根) not in sys.path:
    sys.path.insert(0, str(项目根))

# 自举：不是项目自带的解释器就换过去，避免用系统 python 跑出
# "No module named 'PySide6' / 'httpx'"。详见 v8_3/自举.py
from v8_3.自举 import 确保项目环境

确保项目环境()

from v8_3.配置 import 动作, 加载配置, 传输参数, 缓存参数
from v8_3.核心.传输引擎 import 传输请求
from v8_3.核心.模型 import 网盘类型, 任务状态
from v8_3.配置 import 解析标识
from 工具.生成测试文件 import main as 生成入口  # type: ignore


def 上传目录(适配器, 本地: Path, 远端: str, 并发: int = 8):
    文件列表 = sorted(p for p in 本地.rglob("*") if p.is_file())
    目录集 = sorted({
        远端.rstrip("/") + ("" if (f.parent.relative_to(本地).as_posix() in ("", "."))
                          else "/" + f.parent.relative_to(本地).as_posix())
        for f in 文件列表
    }, key=len)
    for 子目录 in 目录集:
        适配器.确保目录(子目录)

    from concurrent.futures import ThreadPoolExecutor, as_completed
    完成 = [0]
    失败: list[str] = []

    def 上传一个(序号文件):
        序号, 文件 = 序号文件
        相对 = 文件.parent.relative_to(本地).as_posix()
        子目录 = 远端.rstrip("/") + ("" if 相对 in ("", ".") else "/" + 相对)
        try:
            适配器.上传(str(文件), 子目录, 任务ID=f"bench-{序号}",
                        进度=None)
            return None
        except Exception as e:
            return f"{文件}: {e}"

    with ThreadPoolExecutor(max_workers=max(1, 并发)) as 池:
        未来 = [池.submit(上传一个, (i + 1, f))
                for i, f in enumerate(文件列表)]
        for 未 in as_completed(未来):
            错误 = 未.result()
            if 错误:
                失败.append(错误)
            else:
                完成[0] += 1
    if 失败:
        raise RuntimeError(f"上传失败 {len(失败)} 个：{失败[:3]}")
    return 完成[0]


def 校验目标(适配器, 远端根: str, 本地根: Path, 临时目录: Path) -> list[str]:
    """递归回载目标端文件并比对本地 SHA-256；返回失败列表。"""
    import hashlib
    import shutil
    临时目录 = Path(临时目录)
    shutil.rmtree(临时目录, ignore_errors=True)
    临时目录.mkdir(parents=True, exist_ok=True)
    失败: list[str] = []

    def 遍历(远端路径: str, 相对: str):
        for 条目 in 适配器.列目录(远端路径):
            子相对 = f"{相对}/{条目.name}".strip("/") if 相对 else 条目.name
            if 条目.is_dir:
                遍历(条目.path, 子相对)
                continue
            目标 = 临时目录 / 子相对
            目标.parent.mkdir(parents=True, exist_ok=True)
            适配器.下载(条目.path, str(目标))
            本地 = 本地根 / 子相对
            if not 本地.is_file():
                失败.append(f"{子相对}：本地不存在")
                continue
            h1 = hashlib.sha256(目标.read_bytes()).hexdigest()
            h2 = hashlib.sha256(本地.read_bytes()).hexdigest()
            if h1 != h2:
                失败.append(f"{子相对}：哈希不一致")

    遍历(远端根, "")
    return 失败


def main(argv=None) -> int:
    解析器 = argparse.ArgumentParser()
    解析器.add_argument("--源", required=True)
    解析器.add_argument("--目标", required=True)
    解析器.add_argument("--文件数", type=int, default=50)
    解析器.add_argument("--文件KB", type=int, default=4)
    解析器.add_argument("--大文件MB", type=int, default=0)
    解析器.add_argument("--大文件数", type=int, default=1)
    解析器.add_argument("--并发", type=int, default=12)
    解析器.add_argument("--上传并发", type=int, default=8)
    解析器.add_argument("--校验", action="store_true",
                       help="传输完成后从目标端回载并做 SHA-256 校验")
    解析器.add_argument("--保留", action="store_true")
    解析器.add_argument("--前缀", default="V8_3_基准")
    参数 = 解析器.parse_args(argv)

    配置 = 加载配置()
    源网盘 = 解析标识(配置, 参数.源)      # 实例标识；也接受 类型/中文名/实例名称
    目标网盘 = 解析标识(配置, 参数.目标)
    临时 = Path(tempfile.mkdtemp(prefix="v8_3_bench_"))
    远端根 = f"/{参数.前缀}_{int(time.time())}"
    try:
        生成参数 = [
            "--目录", str(临时 / "数据"),
            "--小文件数", str(参数.文件数),
            "--小文件大小", str(max(1, 参数.文件KB) * 1024),
            "--大文件MB", str(参数.大文件MB),
            "--大文件数", str(参数.大文件数),
            "--嵌套",
        ]
        生成入口(生成参数)
        本地 = 临时 / "数据"

        with 动作(配置, 日志回调=lambda m: None) as 动作对象:
            源适配器 = 动作对象.适配器(源网盘)
            目标适配器 = 动作对象.适配器(目标网盘)
            print(f"上传测试数据到 {动作对象.名称(源网盘)}:{远端根} …")
            t0 = time.time()
            n = 上传目录(源适配器, 本地, 远端根, 参数.上传并发)
            print(f"上传完成：{n} 个文件，{time.time()-t0:.1f}s")

            传输默认 = 传输参数(配置)
            请求 = 传输请求(
                源网盘=源网盘, 源路径=远端根,
                目标网盘=目标网盘, 目标路径=f"{远端根}_目标",
                小文件阈值=int(传输默认.get("小文件阈值") or 8 * 1024 * 1024),
                小文件并发=参数.并发,
                大文件并发=max(1, min(4, 参数.并发 // 4 or 1)),
                重试次数=int(传输默认.get("重试次数") or 3),
                断点续传=bool(传输.get("断点续传", True)),
                缓存=缓存参数(配置),
            )
            引擎 = 动作对象.构建引擎([源网盘, 目标网盘])
            print("开始跨网盘传输 …")
            t0 = time.time()
            统计 = 引擎.传输(请求)
            耗时 = time.time() - t0
            print(f"传输完成：{统计.已完成}/{统计.总任务数}，"
                  f"失败 {统计.失败}，跳过 {统计.跳过}")
            print(f"耗时 {耗时:.2f}s，"
                  f"{统计.总字节/1048576/耗时:.2f} MiB/s（按字节）")
            if 统计.失败:
                return 1
            if 参数.校验:
                校验失败 = 校验目标(目标适配器, f"{远端根}_目标", 本地,
                                临时 / "校验")
                if 校验失败:
                    print(f"校验失败 {len(校验失败)} 项：{校验失败[:5]}")
                    return 1
                print("校验通过：目标端 SHA-256 与本地一致")
            if not 参数.保留:
                print("清理云端测试数据 …")
                for 适配器, 路径 in ((源适配器, 远端根),
                                   (目标适配器, f"{远端根}_目标")):
                    try:
                        适配器.删除(路径)
                    except Exception as e:
                        print(f"清理失败（可手动删除）：{e}")
    finally:
        shutil.rmtree(临时, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
