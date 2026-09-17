"""可移植性守卫：陌生人拿到源码也能跑起来。

管三件事：

1. **不写死本机家目录路径** —— ``/home/<用户名>/...`` 这种写法在别人机器上直接失效，
   还顺手把用户名泄露出去；
2. **不写死外部环境** —— 旧版那个共享 venv（名字见下面的 ``旧环境名``）之类，
   本项目完全自包含，
   一律走项目内的 ``运行环境/``；
3. **项目根靠计算得来** —— 不是每处都硬编码一个绝对路径（``v8_3/配置.py``
   与 ``v8_3/自举.py`` 都从 ``__file__`` 推）。

``运行环境/``（第三方依赖本体）与 ``数据/``（运行期日志）不在检查范围内。

跑法（项目根下）::

    运行环境/venv/bin/python -m unittest tests.test_可移植性 -v
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

项目根 = Path(__file__).resolve().parents[1]

#: 文档示例里常见的"占位用户名"，不算泄露
占位用户名 = {"x", "user", "you", "your", "name", "me", "someone", "test", "用户名"}

#: 要扫描的第一方目录
检查目录 = ("v8_3", "工具", "tests", "docs", "适配器")

#: 要扫描的单文件
检查文件 = ("启动.py", "启动.sh", "README.md", "pyproject.toml",
           "配置.json", "配置.example.json")

#: 会被当源码/文档看的后缀
后缀 = (".py", ".sh", ".md", ".json", ".toml", ".txt")

#: 家目录路径：Linux 的 /home/<用户>/… 与 macOS 的 /Users/<用户>/…
#: 前面那句 lookbehind 是为了避开 URL 里恰好出现的同形片段——
#: 只有当它前面不是路径/URL 字符时，才算"写死了本机家目录"。
家目录模式 = re.compile(
    r"(?<![A-Za-z0-9._~%/?&=#-])/(?:home|Users)/([^/\s\"'()<>|]+)")

#: 不许再出现的外部环境名（本项目自包含，不该依赖它们）。
#: 故意拆成两半拼出来：不然这一行自己就会被这条规则扫中（自指）。
旧环境名 = ("python" + "3147uv",)


def _是占位写法(用户: str) -> bool:
    """``<用户名>`` / ``{用户}`` / ``$USER`` 这类占位，不算写死本机路径。"""
    return 用户 in 占位用户名 or 用户[:1] in "{<$"


def _第一方文件() -> list[Path]:
    结果: list[Path] = []
    for 名 in 检查目录:
        基 = 项目根 / 名
        if not 基.is_dir():
            continue
        结果.extend(路径 for 路径 in 基.rglob("*")
                  if 路径.is_file() and 路径.suffix in 后缀
                  and "__pycache__" not in 路径.parts)
    for 名 in 检查文件:
        路径 = 项目根 / 名
        if 路径.is_file():
            结果.append(路径)
    return 结果


class 路径可移植性测试(unittest.TestCase):
    def test_没有写死的家目录路径(self):
        命中: list[str] = []
        for 路径 in _第一方文件():
            文本 = 路径.read_text(encoding="utf-8", errors="ignore")
            for 行号, 行 in enumerate(文本.splitlines(), 1):
                for 用户 in 家目录模式.findall(行):
                    if not _是占位写法(用户):
                        命中.append(
                            f"{路径.relative_to(项目根)}:{行号} 写死了本机家目录（用户 {用户}）")
        self.assertEqual(
            命中, [],
            "别写死本机路径（别人机器上跑不了，还泄露用户名）；"
            "文档里用 <项目根> 之类的占位写法：\n  " + "\n  ".join(命中[:10]))

    def test_没有写死旧的外部环境(self):
        命中: list[str] = []
        for 路径 in _第一方文件():
            文本 = 路径.read_text(encoding="utf-8", errors="ignore")
            for 名 in 旧环境名:
                if 名 in 文本:
                    命中.append(f"{路径.relative_to(项目根)} 提到 {名}")
        self.assertEqual(
            命中, [],
            "本项目完全自包含，不该再出现外部共享环境（用 运行环境/ 下的路径）：\n  "
            + "\n  ".join(命中[:10]))

    def test_项目根是算出来的不是写死的(self):
        from v8_3.配置 import 项目根 as 配置项目根
        from v8_3.自举 import 项目根 as 自举项目根
        self.assertEqual(配置项目根.resolve(), 项目根.resolve())
        self.assertEqual(自举项目根.resolve(), 项目根.resolve())

    def test_shell_脚本里没有非_ASCII_变量名(self):
        """bash 不认中文变量名：``镜像=xxx`` 会被当成命令去执行，报
        "No such file or directory"，脚本直接废掉。

        这个坑在本项目里踩过三次（``启动.sh``、生成的更新脚本、构建脚本），
        所以卡一条测试：**shell 脚本里凡是赋值，变量名必须是 ASCII**。
        （注释、echo 里的中文当然没问题。）
        """
        赋值 = re.compile(r"^\s*([^\s#=]+)\s*=")
        候选 = [p for p in _第一方文件() if p.suffix == ".sh"]
        候选.extend(sorted((项目根 / "构建").glob("*.sh")))
        命中: list[str] = []
        for 路径 in 候选:
            文本 = 路径.read_text(encoding="utf-8", errors="ignore")
            for 行号, 行 in enumerate(文本.splitlines(), 1):
                if 行.lstrip().startswith("#"):
                    continue
                匹配 = 赋值.match(行)
                if 匹配 and any(ord(字) > 127 for 字 in 匹配.group(1)):
                    命中.append(f"{路径.relative_to(项目根)}:{行号} {匹配.group(1)}=…")
        self.assertEqual(
            命中, [],
            "shell 变量名只能用 ASCII（bash 会把中文变量名当命令执行）：\n  "
            + "\n  ".join(命中[:10]))

    def test_环境目录都在项目内(self):
        from v8_3.自举 import 主环境, 项目解释器
        for 路径 in (主环境, 项目解释器):
            self.assertTrue(
                str(路径.resolve()).startswith(str(项目根.resolve())),
                f"{路径} 不在项目内")
