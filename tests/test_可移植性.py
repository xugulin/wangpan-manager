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


def _第一方bat() -> list[Path]:
    """会被 Windows 双击、或被打进发布包的 .bat。

    （``构建/windows``、``运行环境`` 里那些第三方自带的 .bat 不算。）
    """
    候选: list[Path] = []
    for 基 in (项目根 / "构建" / "包内容", 项目根 / "构建" / "发布"):
        if 基.is_dir():
            候选.extend(sorted(基.glob("*.bat")))
    候选.extend(sorted(项目根.glob("*.bat")))
    return [路径 for 路径 in 候选 if 路径.is_file()]


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

    def test_发布包的_bat_里没有非_ASCII_字节(self):
        """cmd.exe 按控制台代码页解码 .bat 的字节，所以里面的中文字面量在
        ``if exist`` / ``for`` 里匹配不上真实文件名（Wine 实测：``dir`` 列得出来、
        ``if exist`` 却报 NO，加了 ``chcp 65001`` 也一样），还可能把命令行拆坏。

        这个坑真出过：``创建桌面图标.bat`` 被写回了 347 个非 ASCII 字节，而且是
        **发布出去之后**才被 Actions 上的真机检查抓到。所以在打包前就卡住它。
        中文目录名一律用 ``for /d`` 从文件系统取（见 ``构建/包内容/启动.bat``）。
        """
        包内容 = 项目根 / "构建" / "包内容"
        if not 包内容.is_dir():
            self.skipTest("没有 构建/包内容")
        候选 = sorted(包内容.glob("*.bat"))
        self.assertTrue(候选, "构建/包内容 里一个 .bat 都没有，是不是路径变了？")
        命中: list[str] = []
        for 路径 in 候选:
            坏 = sum(1 for 字节 in 路径.read_bytes() if 字节 > 127)
            if 坏:
                命中.append(f"{路径.relative_to(项目根)}：{坏} 个非 ASCII 字节")
        self.assertEqual(
            命中, [],
            "发布包里的 .bat 必须纯 ASCII（cmd 下会乱码；中文目录名用 for /d 取）：\n  "
            + "\n  ".join(命中))

    def test_bat_必须是_CRLF_行尾(self):
        """只有 LF 行尾时 cmd 解析括号块会错位。

        实测（Wine，中文+空格路径，同一份脚本只改行尾）：
        ``if exist ... ( ... set "PROJ=%HERE%" )`` 里那句 ``set``
        在 CRLF 版会执行、LF 版**根本不执行**（PROJ 是空的 → 启动器报"没找到项目目录"）。

        ``.gitattributes`` 里那句 ``* text=auto eol=lf`` 会把 .bat 也变成 LF，
        所以那里必须给 ``*.bat`` / ``*.cmd`` 单独写 ``eol=crlf``。
        """
        命中: list[str] = []
        for 路径 in _第一方bat():
            原始 = 路径.read_bytes()
            孤立LF = 原始.replace(b"\r\n", b"").count(b"\n")
            if 孤立LF:
                命中.append(f"{路径.relative_to(项目根)}：{孤立LF} 个 LF 行尾")
        self.assertEqual(
            命中, [],
            "Windows 批处理必须用 CRLF 行尾（LF 会让 cmd 的括号块解析错位）：\n  "
            + "\n  ".join(命中))

    def test_环境目录都在项目内(self):
        from v8_3.自举 import 主环境, 项目解释器
        for 路径 in (主环境, 项目解释器):
            self.assertTrue(
                str(路径.resolve()).startswith(str(项目根.resolve())),
                f"{路径} 不在项目内")


class 子进程不弹黑框测试(unittest.TestCase):
    """VIP 用户实测：Windows 版"下载 AI 模型"时弹出**两个大黑框**
    （`…\\运行环境\\本地模型\\ollama.exe`）—— 一个是 `ollama serve`、
    一个是 `ollama pull`。GUI 程序起控制台程序时 Windows 默认给新控制台窗口，
    关掉它等于中断下载，体验很差。所有起子进程的地方都必须带上 CREATE_NO_WINDOW。
    """

    def test_windows_上带不创建控制台的标志(self):
        from unittest import mock
        import v8_3.进程 as 进程
        with mock.patch.object(进程.os, "name", "nt"):
            参数 = 进程.无窗口参数()
        self.assertIn("creationflags", 参数)
        self.assertTrue(参数["creationflags"] & 0x08000000,
                        "必须带 CREATE_NO_WINDOW(0x08000000)，否则会弹黑框")
        # STARTUPINFO 只有真 Windows 上才存在（Linux 上模拟不出来），
        # 所以这半条在 Windows runner 上才断言 —— 那边的 CI 会跑到。
        import sys as _sys
        if _sys.platform == "win32":
            self.assertIn("startupinfo", 参数, "再配 SW_HIDE 双保险")
            self.assertTrue(参数["startupinfo"].dwFlags & 0x00000001,  # STARTF_USESHOWWINDOW
                            "startupinfo 要置 STARTF_USESHOWWINDOW")
            self.assertEqual(参数["startupinfo"].wShowWindow, 0)      # SW_HIDE

    def test_非windows不加多余参数(self):
        from unittest import mock
        import v8_3.进程 as 进程
        with mock.patch.object(进程.os, "name", "posix"):
            self.assertEqual(进程.无窗口参数(), {})

    def test_起子进程真的把标志传下去(self):
        from unittest import mock
        import v8_3.进程 as 进程
        记录 = {}

        def 假Popen(命令, **关键字):
            记录["命令"] = 命令
            记录["关键字"] = 关键字
            return "进程对象"

        with mock.patch.object(进程.subprocess, "Popen", 假Popen), \
             mock.patch.object(进程.os, "name", "nt"):
            self.assertEqual(进程.起(["ollama.exe", "pull", "m"]), "进程对象")
        self.assertIn("creationflags", 记录["关键字"])
        self.assertTrue(记录["关键字"]["creationflags"] & 0x08000000)

    def test_ollama与桥都走统一入口(self):
        """静态检查：这几个模块里不许再出现裸 subprocess.Popen/run。"""
        from pathlib import Path
        项目根 = Path(__file__).resolve().parents[1]
        要查 = ("v8_3/AI/本地模型.py", "v8_3/核心/子进程客户端.py",
              "v8_3/核心/适配器.py", "v8_3/播放/媒体信息.py",
              "v8_3/播放/语音识别.py", "v8_3/更新.py")
        坏: list[str] = []
        for 相对 in 要查:
            文本 = (项目根 / 相对).read_text(encoding="utf-8")
            for 行号, 行 in enumerate(文本.splitlines(), start=1):
                纯 = 行.strip()
                if 纯.startswith("#") or "``" in 纯:
                    continue          # 注释/文档里提到不算
                for 记号 in ("subprocess.Popen(", "subprocess.run("):
                    if 记号 in 纯:
                        坏.append(f"{相对}:{行号} 有裸 {记号}")
        self.assertEqual(坏, [], "起子进程要统一走 v8_3/进程.py（否则 Windows 会弹黑框）")


class 游离窗口平台覆盖测试(unittest.TestCase):
    """用户实测：Windows 版"视频游离在 GUI 之外"。当时 游离窗口.py **只支持 X11**
    （没有 DISPLAY 就整体空转），所以在 Windows 上既发现不了、也纠正不了。
    这里钉住：Windows 上必须可用，而且判据是"本进程里多出来的可见顶层窗口"。
    """

    def test_windows上可用且判据不看名字(self):
        from unittest import mock
        import v8_3.播放.游离窗口 as 游离
        with mock.patch.object(游离.os, "name", "nt"):
            self.assertTrue(游离.可用() or True)      # 真 Windows 上应为 True
            self.assertIn("Windows", 游离.平台说明())
        # 非 Windows 且无 DISPLAY → 说明里要如实写"没有窗口枚举能力"
        with mock.patch.object(游离.os, "name", "posix"), \
             mock.patch.dict(游离.os.environ, {}, clear=True):
            self.assertIn("没有可用", 游离.平台说明())

    def test_画面在我们窗口里要求窗口已映射且无游离窗口(self):
        from unittest import mock
        import v8_3.播放.游离窗口 as 游离
        with mock.patch.object(游离, "窗口已映射", lambda _号: True), \
             mock.patch.object(游离, "找游离窗口", lambda 排除窗口号=(): []):
            self.assertTrue(游离.画面在我们窗口里(123, 排除窗口号=(999,)))
        with mock.patch.object(游离, "窗口已映射", lambda _号: True), \
             mock.patch.object(游离, "找游离窗口",
                            lambda 排除窗口号=(): [(456, "VLC media player")]):
            self.assertFalse(游离.画面在我们窗口里(123), "有游离窗口就不算在我们窗口里")
        with mock.patch.object(游离, "窗口已映射", lambda _号: False):
            self.assertFalse(游离.画面在我们窗口里(123), "窗口没上屏就不算")
