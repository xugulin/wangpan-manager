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


def 在不在(名: str, 大小: int) -> bool:
    """这个包到底在不在 Release 上？

    **不能用 API 的资源列表判断**：GitHub 那个接口会返回过期数据
    （实测列表里有 5 个、实际只剩 2 个，据此删资源会删错）。只有下载地址是真的：
    这里发一个 1 字节的 Range 请求，能拿到字节、且总长度对得上才算在。
    """
    from 构建.发布到github import 资源在不在
    return 资源在不在(名, 大小)


def 主(最多轮数: int, 只: str = "", 每包上限: float = 0.0) -> int:
    期望 = 期望的包()
    if 只:
        期望 = {k: v for k, v in 期望.items() if 只 in k}
        if not 期望:
            raise SystemExit(f"没有名字里含「{只}」的包")
    if not 期望:
        raise SystemExit("构建/发布 下没有 zip，先跑 构建/打包.py")
    说(f"应该有 {len(期望)} 个包：")
    for 名, 大小 in 期望.items():
        print(f"   {名}  {大小 / 1048576:.0f} MB")

    with 会话() as 会话对象:
        会话对象.get("https://api.github.com/user").raise_for_status()
        发布 = 确保发布(会话对象)
        for 轮 in range(1, 最多轮数 + 1):
            缺 = []
            for 名, 大小 in 期望.items():
                if not 在不在(名, 大小):
                    缺.append((名, "下载地址探不到（404 或大小不对）"))
            说(f"第 {轮} 轮：{len(期望) - len(缺)}/{len(期望)} 已就位")
            if not 缺:
                说("✅ 四个包全都传好了")
                return 0
            for 名, 原因 in 缺:
                说(f"  补传 {名}（{原因}）")
                if 每包上限 > 0:
                    # 卡住的连接会一直挂着：超时就放弃这次，下一轮换一条新连接再来
                    import threading
                    结果: list[bool] = []

                    def 干() -> None:
                        结果.append(传资源(会话对象, 发布, 发布目录 / 名))

                    线程 = threading.Thread(target=干, daemon=True)
                    线程.start()
                    线程.join(每包上限)
                    if 线程.is_alive():
                        说(f"  ⏱ {名} 超过 {每包上限 / 60:.0f} 分钟还没传完，换个连接重来")
                        try:
                            会话对象.close()
                        except Exception:  # noqa: BLE001
                            pass
                        会话对象 = 会话()
                        会话对象.get("https://api.github.com/user").raise_for_status()
                        发布 = 确保发布(会话对象)
                else:
                    传资源(会话对象, 发布, 发布目录 / 名)
            time.sleep(20)
        说("试满次数还没齐，稍后可以再跑一次本脚本")
        return 1


if __name__ == "__main__":
    解析 = argparse.ArgumentParser(description="补齐 Release 上缺的包")
    解析.add_argument("--最多轮数", type=int, default=20)
    解析.add_argument("--只", default="", help="只补名字里含这个关键词的包")
    解析.add_argument("--每包上限", type=float, default=0.0,
                    help="单个包最多传多少秒，超时就换连接重来（0=不限）")
    参数 = 解析.parse_args()
    raise SystemExit(主(参数.最多轮数, 参数.只, 参数.每包上限))
