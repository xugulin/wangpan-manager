"""把 Release 里缺的（或没传完的）发布包补齐。

GitHub 的 asset 上传服务偶尔回 500/502（"Error saving asset"），传一半断掉也不会
留下记录；所以这里做一件事：**反复核对"Release 上该有的四个包是不是都在、大小对不对"**，
缺哪个补哪个，直到齐了或者试满次数。

用法（项目根下）::

    运行环境/venv/bin/python 构建/补齐发布包.py [--最多轮数 20]

令牌从 ``~/python/令牌/github-token.txt`` 或 ``GITHUB_TOKEN`` 读。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

项目根 = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(项目根))

from 构建.发布到github import (会话, 确保发布, 传资源, 发布目录,  # noqa: E402
                          拥有人)
from urllib.parse import quote  # noqa: E402
from v8_3 import 更新  # noqa: E402


def 说(文本: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {文本}", flush=True)


def 期望的包() -> dict[str, int]:
    """Release 上应该有的资源：名字 → 字节数。"""
    结果: dict[str, int] = {}
    for 包 in sorted(发布目录.glob("*.zip")):
        结果[包.name] = 包.stat().st_size
    return 结果


def 现有资产(会话对象) -> dict[str, int]:
    发布 = 确保发布(会话对象)
    路径 = (f"https://api.github.com/repos/{拥有人}/{quote(更新.仓库名)}"
          f"/releases/{发布['id']}/assets?per_page=100")
    应答 = 会话对象.get(路径)
    应答.raise_for_status()
    return {x["name"]: int(x["size"]) for x in 应答.json()}


def 主(最多轮数: int) -> int:
    期望 = 期望的包()
    if not 期望:
        raise SystemExit("构建/发布 下没有 zip，先跑 构建/打包.py")
    说(f"应该有 {len(期望)} 个包：")
    for 名, 大小 in 期望.items():
        print(f"   {名}  {大小 / 1048576:.0f} MB")

    with 会话() as 会话对象:
        会话对象.get("https://api.github.com/user").raise_for_status()
        发布 = 确保发布(会话对象)
        for 轮 in range(1, 最多轮数 + 1):
            现有 = 现有资产(会话对象)
            缺 = []
            for 名, 大小 in 期望.items():
                有了 = 现有.get(名)
                if 有了 is None:
                    缺.append((名, "还没有"))
                elif abs(有了 - 大小) > 4096:
                    缺.append((名, f"大小不对（{有了 / 1048576:.0f} MB）"))
            说(f"第 {轮} 轮：{len(期望) - len(缺)}/{len(期望)} 已就位")
            if not 缺:
                说("✅ 四个包全都传好了")
                return 0
            for 名, 原因 in 缺:
                说(f"  补传 {名}（{原因}）")
                传资源(会话对象, 发布, 发布目录 / 名)
            time.sleep(20)
        说("试满次数还没齐，稍后可以再跑一次本脚本")
        return 1


if __name__ == "__main__":
    解析 = argparse.ArgumentParser(description="补齐 Release 上缺的包")
    解析.add_argument("--最多轮数", type=int, default=20)
    raise SystemExit(主(解析.parse_args().最多轮数))
