"""收尾：等包都传齐，然后把 Release 说明换成"分卷版"并做最终核对。

为什么需要它：大文件直传经常被 GitHub 判 500/502/504，所以包以 60 MB 分卷上传，
用户需要知道"下哪几个、怎么合并"。这个脚本负责：

1. 用**下载地址探测**确认每个包的整包或全部分卷都能下载（不信 API 资源列表，
   它会返回过期数据）；
2. 按实际情况生成 Release 说明（分卷版 / 整包版都支持）；
3. 顺带核对程序内置的「一键更新」能不能认出这些资源。

用法（项目根下）::

    运行环境/venv/bin/python 构建/收尾发布.py --等待     # 等到齐再改说明
    运行环境/venv/bin/python 构建/收尾发布.py            # 只核对 + 改说明
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

项目根 = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(项目根))

from 构建.发布到github import 会话, 确保发布, 发布目录  # noqa: E402
from 构建.补齐发布包 import 在不在, 期望的包  # noqa: E402
from v8_3 import 更新  # noqa: E402

分卷目录 = 发布目录 / "分卷"
说明文件 = 项目根 / "构建" / "发布说明.md"


def 说(文本: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {文本}", flush=True)


def 分卷计划() -> dict[str, list[Path]]:
    结果: dict[str, list[Path]] = {}
    for 包 in sorted(发布目录.glob("*.zip")):
        卷 = sorted(分卷目录.glob(包.name + ".part*"))
        if 卷:
            结果[包.name] = 卷
    return 结果


def 卷在不在(卷: Path) -> bool:
    return 在不在(卷.name, 卷.stat().st_size)


def 生成说明(整包在的: list[str], 分卷全的: list[str]) -> str:
    # 从"基础正文"出发（说明文件本身是生成结果，直接读它会把下载表一次次追加成好几份）
    基础 = 项目根 / "构建" / "发布说明-基础.md"
    正文 = (基础 if 基础.is_file() else 说明文件).read_text(encoding="utf-8")
    正文 = 正文.split("### 📥 具体下哪些文件？")[0].rstrip() + "\n"
    情况 = []
    for 平台 in ("Windows", "Linux"):
        for 口味, 标签 in (("full-with-whisper-model", "完整版"),
                        ("lite-no-model", "精简版")):
            名 = f"wangpan-manager-V{更新.当前版本()}-{平台}-{口味}.zip"
            状态 = ("整包" if 名 in 整包在的 else
                  ("分卷" if 名 in 分卷全的 else "缺失"))
            情况.append((平台, 标签, 名, 状态))
    补充 = ["", "### 📥 具体下哪些文件？", "",
          "| 系统 | 版本 | 怎么下 |", "|---|---|---|"]
    for 平台, 标签, 名, 状态 in 情况:
        if 状态 == "整包":
            补充.append(f"| {平台} | {标签} | 直接下 `{名}` |")
        elif 状态 == "分卷":
            卷名 = sorted(p.name for p in 分卷目录.glob(名 + ".part*"))
            补充.append(f"| {平台} | {标签} | 下载 `{名}.part01` ～ "
                      f"`.part{len(卷名):02d}`（共 {len(卷名)} 个），"
                      f"放同一个文件夹后运行 `JOIN-AND-EXTRACT-*.{ 'bat' if 平台=='Windows' else 'sh'}` |")
        else:
            补充.append(f"| {平台} | {标签} | ⏳ 还在上传中 |")
    补充 += ["", "> 合并脚本只用系统自带命令：Windows 用 `copy /b` + `tar`，"
           "Linux 用 `cat` + `unzip`，**不需要额外装任何东西**。",
           "", f"> 校验：`SHA256SUMS.txt` 里是对应整包的 sha256（合并后可用它核对）。"]
    return 正文 + "\n".join(补充) + "\n"


def 有上传在跑() -> bool:
    """本机是不是还有分卷上传器在跑？

    链路被上传占满时，连 github.com 都连不上（探测会全部超时），
    这时候去核对"包在不在"只会得到一片假阴性 —— 所以先等上传停。
    """
    import os
    for 名 in os.listdir("/proc"):
        if not 名.isdigit():
            continue
        try:
            with open(f"/proc/{名}/cmdline", "rb") as f:
                命令 = f.read().decode("utf-8", "ignore")
        except OSError:
            continue
        if "分卷发布" in 命令 or "补齐发布包" in 命令:
            return True
    return False


def main() -> int:
    解析 = argparse.ArgumentParser(description="等包齐了收尾")
    解析.add_argument("--等待", action="store_true", help="循环等到齐")
    参数 = 解析.parse_args()

    期望 = 期望的包()
    计划 = 分卷计划()
    while True:
        if 有上传在跑():
            说("还有上传器在跑（这时探测会全是假阴性），先等它结束")
            if not 参数.等待:
                return 1
            time.sleep(300)
            continue
        整包在的, 分卷全的, 还缺的 = [], [], []
        for 名, 大小 in 期望.items():
            if 在不在(名, 大小):
                整包在的.append(名)
            elif 计划.get(名) and all(卷在不在(卷) for 卷 in 计划[名]):
                分卷全的.append(名)
            else:
                还缺的.append(名)
        说(f"整包 {len(整包在的)} ｜ 分卷齐 {len(分卷全的)} ｜ 还缺 {len(还缺的)}")
        for 名 in 还缺的:
            卷 = 计划.get(名, [])
            在卷 = sum(1 for c in 卷 if 卷在不在(c))
            说(f"   ⏳ {名.split('-')[-1]}：分卷 {在卷}/{len(卷) if 卷 else '未切'}")
        if not 还缺的 or not 参数.等待:
            break
        time.sleep(600)

    if 还缺的:
        说("还有没传完的，说明暂不改（等传齐再跑一次）")
        return 1

    正文 = 生成说明(整包在的, 分卷全的)
    说明文件.write_text(正文, encoding="utf-8")
    with 会话() as s:
        发布 = 确保发布(s)
        r = s.patch(f"https://api.github.com/repos/xugulin/wangpan-manager/releases/{发布['id']}",
                    json={"body": 正文}, timeout=60)
        说(f"Release 说明已更新：{r.status_code}")
        资源 = s.get(f"https://api.github.com/repos/xugulin/wangpan-manager/"
                   f"releases/{发布['id']}/assets?per_page=100").json()
        名单 = [a["name"] for a in 资源] if isinstance(资源, list) else []
        # 内置「一键更新」能不能认出这些资源？
        for 平台 in ("linux", "windows"):
            for 完整 in (True, False):
                单个 = 更新.选择资源([{"name": n, "size": 1} for n in 名单], 平台, 完整)
                组 = 更新.分卷组([{"name": n, "size": 1} for n in 名单], 平台, 完整)
                print(f"   一键更新 {平台} 完整={完整} → "
                      f"{'整包 ' + 单个['name'] if 单个 else ('分卷 ' + str(len(组)) + ' 个' if 组 else '没认出来 ✗')}")
    说("收尾完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
