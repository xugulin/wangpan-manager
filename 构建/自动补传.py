"""等链路好起来就自动把缺的包补上。

背景：本机往 GitHub 传大文件会被限速（实测 0.2～15 MB/s 之间乱跳），限速严重时
连 5 MB 都要十几秒、还会超时 —— 这时硬传只会把时间浪费在失败上。

这个脚本的做法：
1. 每 5 分钟拿 5 MB 试一下**当前上传速度**；
2. 速度够快（≥ 2 MB/s）才动手传缺失的整包，一次一个，失败就退回等待；
3. 传完立刻用**下载地址**复核（不信 GitHub 的 API 资源列表，它会返回过期数据）；
4. 四个包都齐了就退出。

用法（项目根下，可以挂着跑很久）::

    运行环境/venv/bin/python 构建/自动补传.py [--阈值 2.0] [--间隔 300]
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

项目根 = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(项目根))

from 构建.发布到github import 会话, 确保发布, 传资源, 发布目录  # noqa: E402
from 构建.补齐发布包 import 在不在, 期望的包  # noqa: E402


def 说(文本: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {文本}", flush=True)


def 测速(会话对象, 发布: dict) -> float:
    """返回当前上传速度（MB/s）。顺手把探测文件删掉，不留垃圾。"""
    import httpx
    数据 = b"x" * (5 * 1024 * 1024)
    地址 = (f"https://uploads.github.com/repos/xugulin/wangpan-manager/"
          f"releases/{发布['id']}/assets")
    for 试 in range(2):
        开始 = time.time()
        try:
            应答 = 会话对象.post(地址, params={"name": f"speed-probe-{int(开始)}.bin"},
                              content=数据,
                              headers={"Content-Type": "application/octet-stream"},
                              timeout=httpx.Timeout(20.0, write=45.0))
        except Exception as 错:  # noqa: BLE001
            print(f"    探测异常：{type(错).__name__}")
            continue
        用时 = max(time.time() - 开始, 0.05)
        if 应答.status_code in (200, 201):
            try:
                会话对象.delete("https://api.github.com/repos/xugulin/wangpan-manager/"
                             f"releases/assets/{应答.json()['id']}")
            except Exception:  # noqa: BLE001
                pass
            return 5.0 / 用时
        print(f"    探测返回 {应答.status_code}")
    return 0.0


def main() -> int:
    解析 = argparse.ArgumentParser(description="链路好时自动补传发布包")
    解析.add_argument("--间隔", type=int, default=900, help="每轮间隔秒数")
    解析.add_argument("--阈值", type=float, default=0.0, help="（保留参数，现在不按速度挑）")
    解析.add_argument("--最多轮", type=int, default=0, help="0 = 一直跑")
    参数 = 解析.parse_args()

    期望 = 期望的包()
    说(f"要保证 {len(期望)} 个包都在 Release 上（阈值 {参数.阈值} MB/s，间隔 {参数.间隔}s）")
    轮 = 0
    while True:
        轮 += 1
        缺 = [(名, 大小) for 名, 大小 in 期望.items() if not 在不在(名, 大小)]
        if not 缺:
            说("✅ 四个包都能下载了，收工")
            return 0
        说(f"第 {轮} 轮：还缺 {len(缺)} 个 → {[n.split('-')[-1] for n, _ in 缺]}")
        with 会话() as s:
            s.get("https://api.github.com/user").raise_for_status()
            发布 = 确保发布(s)
            速度 = 测速(s, 发布)
            说(f"  当前上传速度约 {速度:.2f} MB/s")
            # 不按速度挑：实测**慢速反而成功过**（0.16 / 0.46 MB/s 那两次），
            # 快速连接反倒常被 GitHub 判 500。所以只要链路活着就试，一次只传一个。
            for 名, _ in 缺:
                说(f"  试着传 {名}")
                if 传资源(s, 发布, 发布目录 / 名):
                    说("   ✓ 传完一个")
                    break
                说("  这次没成，下一轮再来")
        if 参数.最多轮 and 轮 >= 参数.最多轮:
            说("达到最多轮数，退出")
            return 1
        time.sleep(参数.间隔)


if __name__ == "__main__":
    raise SystemExit(main())
