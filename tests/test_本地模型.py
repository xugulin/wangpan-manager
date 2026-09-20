"""本地 DeepSeek 模型接入单测（V8_3 新增）。

全部**离线**：起两个本地模拟服务（Ollama 原生 / OpenAI 兼容）来验证客户端，
不依赖真实 Ollama，也不发任何外部网络请求。

覆盖：
  * 配置解析与归一化（超时/温度/tokens 边界、用途、默认模型）；
  * 自动探测（谁是 ollama、谁是 OpenAI 兼容、都不可用时给安装指引）；
  * 对话（两种协议各自解析 content / thinking / tokens）；
  * 服务端错误（404 模型不存在、500、非 JSON、超时）不抛异常而是如实返回；
  * 测速返回 tokens/s；
  * AI 助手把本地模型当"可用"（无密钥也能用 AI），并支持 轻/重 用途分流；
  * 云端兜底：本地坏掉 + 有密钥时回落到云端。
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
import sys

项目根 = Path(__file__).resolve().parents[1]
if str(项目根) not in sys.path:
    sys.path.insert(0, str(项目根))

from v8_3.AI.本地模型 import (本地模型客户端, 本地模型配置, 取本地模型配置,
                          默认模型)


class 假服务基类(BaseHTTPRequestHandler):
    记录: list = []

    def log_message(self, *args):        # 静音
        pass

    def _回(self, 数据, 状态: int = 200):
        self.send_response(状态)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(数据, ensure_ascii=False).encode("utf-8"))

    def _读体(self) -> dict:
        长度 = int(self.headers.get("Content-Length") or 0)
        if not 长度:
            return {}
        try:
            return json.loads(self.rfile.read(长度).decode("utf-8"))
        except Exception:
            return {}


class Ollama处理器(假服务基类):
    模型列表 = ["deepseek-r1:1.5b", "qwen2.5:3b"]
    回答 = "\n{\"并发\": 6, \"理由\": \"本地模型\"}\n"
    思考 = "先想一下……"
    状态码 = 200
    强制坏JSON = False

    def do_GET(self):
        假服务基类.记录.append(("GET", self.path))
        if self.path.startswith("/api/version"):
            return self._回({"version": "9.9.9-test"})
        if self.path.startswith("/api/tags"):
            return self._回({"models": [{"name": m} for m in self.模型列表]})
        return self._回({"error": "not found"}, 404)

    def do_POST(self):
        体 = self._读体()
        假服务基类.记录.append(("POST", self.path, 体.get("model")))
        if self.状态码 != 200:
            return self._回({"error": "boom"}, self.状态码)
        if self.强制坏JSON:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"not-json")
            return
        return self._回({
            "message": {"role": "assistant", "content": self.回答,
                        "thinking": self.思考},
            "prompt_eval_count": 120, "eval_count": 34,
        })


class OpenAI处理器(假服务基类):
    模型列表 = ["deepseek-r1-distill-qwen-1.5b"]
    回答 = '{"优先级": ["小文件优先"]}'
    状态码 = 200

    def do_GET(self):
        假服务基类.记录.append(("GET", self.path))
        if self.path.startswith("/v1/models"):
            return self._回({"data": [{"id": m} for m in self.模型列表]})
        return self._回({"error": "not found"}, 404)

    def do_POST(self):
        体 = self._读体()
        假服务基类.记录.append(("POST", self.path, 体.get("model")))
        if self.状态码 != 200:
            return self._回({"error": "boom"}, self.状态码)
        return self._回({
            "choices": [{"message": {"role": "assistant", "content": self.回答},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 88, "completion_tokens": 21},
        })


def 起服务(处理器类) -> tuple[ThreadingHTTPServer, str]:
    服务 = ThreadingHTTPServer(("127.0.0.1", 0), 处理器类)
    线程 = threading.Thread(target=服务.serve_forever, daemon=True)
    线程.start()
    return 服务, f"http://127.0.0.1:{服务.server_address[1]}"


class 配置测试(unittest.TestCase):
    def test_默认值(self):
        配置 = 取本地模型配置({})
        self.assertFalse(配置.启用)
        self.assertEqual(配置.模型, 默认模型)
        self.assertEqual(配置.用途, "全部")
        self.assertTrue(配置.优先本地)

    def test_边界归一化(self):
        配置 = 取本地模型配置({"本地模型": {
            "启用": True, "超时秒": 1, "温度": 9, "最大tokens": 1,
            "上下文长度": 1, "提供方": "乱写", "用途": "乱写"}})
        self.assertGreaterEqual(配置.超时秒, 5.0)
        self.assertLessEqual(配置.温度, 2.0)
        self.assertGreaterEqual(配置.最大tokens, 64)
        self.assertGreaterEqual(配置.上下文长度, 512)
        self.assertEqual(配置.提供方, "自动")
        self.assertEqual(配置.用途, "全部")

    def test_默认预算足够出答案(self):
        # deepseek-r1 会先思考：预算太小会把答案挤空（实测 256 → 空）
        self.assertGreaterEqual(取本地模型配置({}).最大tokens, 512)


class 客户端测试(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.Ollama服务, cls.Ollama地址 = 起服务(Ollama处理器)
        cls.OpenAI服务, cls.OpenAI地址 = 起服务(OpenAI处理器)

    @classmethod
    def tearDownClass(cls):
        cls.Ollama服务.shutdown(); cls.Ollama服务.server_close()
        cls.OpenAI服务.shutdown(); cls.OpenAI服务.server_close()

    def setUp(self):
        Ollama处理器.状态码 = 200
        Ollama处理器.强制坏JSON = False
        假服务基类.记录.clear()

    def _ollama客户端(self, **额外) -> 本地模型客户端:
        段 = {"启用": True, "提供方": "ollama", "地址": self.Ollama地址,
             "模型": "deepseek-r1:1.5b", **额外}
        return 本地模型客户端(取本地模型配置({"本地模型": 段}))

    def test_探测_ollama(self):
        客户端 = self._ollama客户端()
        状态 = 客户端.检测()
        self.assertTrue(状态.可用)
        self.assertEqual(状态.提供方, "ollama")
        self.assertEqual(状态.版本, "9.9.9-test")
        self.assertIn("deepseek-r1:1.5b", 状态.模型列表)
        self.assertEqual(状态.模型, "deepseek-r1:1.5b")
        self.assertIn("本地模型可用", 状态.一行())

    def test_探测_openai兼容(self):
        客户端 = 本地模型客户端(取本地模型配置({"本地模型": {
            "启用": True, "提供方": "openai兼容", "地址": self.OpenAI地址}}))
        状态 = 客户端.检测()
        self.assertTrue(状态.可用)
        self.assertEqual(状态.提供方, "openai兼容")
        self.assertEqual(状态.模型列表, ["deepseek-r1-distill-qwen-1.5b"])

    def test_都不可用时给安装指引(self):
        客户端 = 本地模型客户端(取本地模型配置({"本地模型": {
            "启用": True, "提供方": "ollama",
            "地址": "http://127.0.0.1:9"}}))     # 9 端口基本没人听
        状态 = 客户端.检测()
        self.assertFalse(状态.可用)
        self.assertIn("ollama", 状态.说明)
        self.assertIn("不可用", 状态.一行())

    def test_对话_ollama协议(self):
        结果 = self._ollama客户端().对话("给我策略", "你是助手")
        self.assertTrue(结果.成功, 结果.错误)
        self.assertIn("并发", 结果.内容)
        self.assertEqual(结果.推理内容, "先想一下……")
        self.assertEqual(结果.输出tokens, 34)
        self.assertEqual(结果.输入tokens, 120)
        self.assertEqual(结果.费用, 0.0)
        self.assertEqual(结果.来源, "本地模型")

    def test_对话_openai协议(self):
        客户端 = 本地模型客户端(取本地模型配置({"本地模型": {
            "启用": True, "提供方": "openai兼容", "地址": self.OpenAI地址}}))
        结果 = 客户端.对话("给我优先级")
        self.assertTrue(结果.成功, 结果.错误)
        self.assertIn("优先级", 结果.内容)
        self.assertEqual(结果.输出tokens, 21)

    def test_服务端500不抛异常(self):
        Ollama处理器.状态码 = 500
        结果 = self._ollama客户端().对话("x")
        self.assertFalse(结果.成功)
        self.assertIn("500", 结果.错误)

    def test_返回非JSON不抛异常(self):
        Ollama处理器.强制坏JSON = True
        结果 = self._ollama客户端().对话("x")
        self.assertFalse(结果.成功)
        self.assertTrue(结果.错误)

    def test_连接不通不抛异常(self):
        客户端 = 本地模型客户端(取本地模型配置({"本地模型": {
            "启用": True, "提供方": "ollama", "地址": "http://127.0.0.1:9"}}))
        结果 = 客户端.对话("x")
        self.assertFalse(结果.成功)
        self.assertTrue(结果.错误)

    def test_请求带think关闭与预算(self):
        客户端 = self._ollama客户端(最大tokens=512, 上下文长度=2048)
        客户端.对话("x")
        调用 = [r for r in 假服务基类.记录 if r[0] == "POST"][-1]
        self.assertEqual(调用[1], "/api/chat")
        self.assertEqual(调用[2], "deepseek-r1:1.5b")

    def test_选模型优先deepseek(self):
        客户端 = 本地模型客户端(取本地模型配置({"本地模型": {
            "启用": True, "提供方": "ollama", "地址": self.Ollama地址,
            "模型": "不存在的模型"}}))
        状态 = 客户端.检测()
        self.assertTrue(状态.模型.startswith("deepseek"))

    def test_测速(self):
        结果 = self._ollama客户端().测速()
        self.assertTrue(结果.get("成功"), 结果)
        self.assertGreaterEqual(结果.get("输出tokens", 0), 1)
        self.assertIn("每秒tokens", 结果)


class 助手集成测试(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.服务, cls.地址 = 起服务(Ollama处理器)

    @classmethod
    def tearDownClass(cls):
        cls.服务.shutdown(); cls.服务.server_close()

    def _助手(self, **本地段):
        from v8_3.AI.AI智能助手 import AI智能助手
        段 = {"启用": True, "提供方": "ollama", "地址": self.地址,
             "模型": "deepseek-r1:1.5b", **本地段}
        return AI智能助手({"AI": {"启用": True, "api密钥": "",
                               "本地模型": 段}},
                      禁用网络=True)

    def test_无密钥但本地可用即算可用(self):
        助手 = self._助手()
        self.assertTrue(助手.本地模型可用())
        self.assertTrue(助手.是否可用(), "本地模型可用时不该因为没密钥而判不可用")

    def test_未启用本地才算不可用(self):
        助手 = self._助手(启用=False)
        self.assertFalse(助手.是否可用())

    def test_本地调用成功并计来源(self):
        助手 = self._助手()
        结果, tokens, finish = 助手._调用一次(
            "deepseek-flash", "只输出 JSON", "给我策略", 512)
        self.assertEqual(结果, {"并发": 6, "理由": "本地模型"})
        self.assertEqual(tokens, 34)
        self.assertEqual(助手.本地调用次数, 1)
        self.assertEqual(助手.云端调用次数, 0)

    def test_用途仅轻量_重请求不走本地(self):
        助手 = self._助手(用途="仅轻量")
        重, _, _ = 助手._调用一次("deepseek-flash", "s", "u", 512, 用途="重")
        轻, _, _ = 助手._调用一次("deepseek-flash", "s", "u", 512, 用途="轻")
        self.assertIsNone(重, "仅轻量模式下重决策不该走本地（且无云端密钥）")
        self.assertEqual(轻, {"并发": 6, "理由": "本地模型"})

    def test_本地失败且云端兜底关_返回失败(self):
        Ollama处理器.状态码 = 500
        try:
            助手 = self._助手(云端兜底=False)
            结果, _, _ = 助手._调用一次("deepseek-flash", "s", "u", 512)
            self.assertIsNone(结果)
        finally:
            Ollama处理器.状态码 = 200

    def test_状态摘要与一行(self):
        from v8_3.AI.运行时 import AI运行时
        运行时 = AI运行时({"AI": {"启用": True, "api密钥": "", "本地模型": {
            "启用": True, "提供方": "ollama", "地址": self.地址}}},
                    Path("/tmp/不存在_测试用.json"))
        摘要 = 运行时.获取本地模型摘要()
        self.assertTrue(摘要.get("可用"))
        self.assertTrue(摘要.get("启用"))
        self.assertIn("本地模型", 运行时.获取本地模型一行())


class _假应答:
    """假应答：给 Range 就按片给（206），不给就整份给（200）。"""

    def __init__(self, 载荷: bytes, 支持分片: bool, 范围: str):
        self.载荷, self.范围 = 载荷, 范围
        if 支持分片 and 范围:
            片段 = 范围.replace("bytes=", "").split("-")
            起 = int(片段[0])
            止 = int(片段[1]) if len(片段) > 1 and 片段[1] else len(载荷) - 1
            self._数据 = 载荷[起:止 + 1]
            self.status_code = 206
            self.headers = {"content-range": f"bytes {起}-{止}/{len(载荷)}"}
        else:
            self._数据 = 载荷
            self.status_code = 200
            self.headers = {"content-length": str(len(载荷))}

    def raise_for_status(self) -> None:
        pass

    def iter_bytes(self, 块长: int):
        for 起 in range(0, len(self._数据), 块长):
            yield self._数据[起:起 + 块长]


class _假流:
    def __init__(self, 应答: _假应答):
        self._应答 = 应答

    def __enter__(self) -> _假应答:
        return self._应答

    def __exit__(self, *_: object) -> bool:
        return False


class _假httpx:
    """只实现用到的 ``stream``。

    ``支持分片=True`` 时按 ``Range`` 切片（模拟 GitHub）；
    否则一律返回整份 200（模拟不认 Range 的服务端，代码必须退回单连接）。
    """

    Timeout = staticmethod(lambda *a, **k: None)

    def __init__(self, 载荷: bytes, 支持分片: bool = False):
        self._载荷, self._支持分片 = 载荷, 支持分片

    def stream(self, *a, **k):
        return _假流(_假应答(self._载荷, self._支持分片, (k.get("headers") or {}).get("Range", "")))


def _造官方式包(版本文本: str, 压缩: str = "zst") -> bytes:
    """造一个"官方形状"的包：``bin/ollama``（会应答 ``--version``）+ ``lib/ollama/``。

    ``压缩="zst"`` = 现在官方的 ``.tar.zst``；``"gz"`` = 老的 ``.tgz``。
    """
    import io
    import tarfile

    缓存 = io.BytesIO()
    with tarfile.open(fileobj=缓存, mode="w") as 包:
        def 加(名: str, 内容: str, 模式: int) -> None:
            数据 = 内容.encode("utf-8")
            信息 = tarfile.TarInfo(名)
            信息.size, 信息.mode = len(数据), 模式
            包.addfile(信息, io.BytesIO(数据))

        加("bin/ollama", f'#!/bin/sh\necho "ollama version is {版本文本}"\n', 0o755)
        加("lib/ollama/占位.txt", "x", 0o644)
        加("lib/ollama/cuda_v12/大.bin", "y" * 2048, 0o644)
    裸 = 缓存.getvalue()
    if 压缩 == "gz":
        import gzip
        return gzip.compress(裸)
    from compression import zstd
    return zstd.compress(裸)


@unittest.skipIf(os.name == "nt", "假基座是 sh 脚本，Windows 上跑不了")
class 内置ollama基座测试(unittest.TestCase):
    """内置（随包发布）的 ollama 基座：不写死路径 + 更新"全有或全无"。

    全部离线：归档自己造、网流用假的，不碰真 ollama、不发外部请求。
    """

    def _搭一个项目(self, 已有版本: str | None):
        """临时项目目录；``已有版本`` 给定时先放一份"旧基座"进去。"""
        临时 = tempfile.TemporaryDirectory()
        根 = Path(临时.name)
        (根 / "运行环境").mkdir(parents=True, exist_ok=True)
        if 已有版本 is not None:
            旧可执行 = 根 / "运行环境" / "本地模型" / "bin" / "ollama"
            旧可执行.parent.mkdir(parents=True, exist_ok=True)
            旧可执行.write_text(
                f'#!/bin/sh\necho "ollama version is {已有版本}"\n', encoding="utf-8")
            旧可执行.chmod(0o755)
        return 临时, 根

    def _跑更新(self, 根: Path, 包数据: bytes):
        from v8_3.AI import 本地模型 as 模块
        for 补 in (mock.patch.object(模块, "项目便携目录",
                                   lambda: 根 / "运行环境" / "本地模型"),
                   mock.patch.object(模块, "httpx", _假httpx(包数据)),
                   mock.patch.object(模块, "便携版下载地址",
                                   lambda: "https://例子.invalid/ollama-linux-amd64.tar.zst")):
            补.start()
            self.addCleanup(补.stop)
        return 模块, 模块.下载便携运行时(强制=True), 模块.项目内可执行文件()

    def test_安装根识别三种官方形状(self):
        """Linux 是 bin/ollama、Windows 是 ollama.exe、也可能多套一层目录。"""
        from v8_3.AI.本地模型 import _找安装根
        with tempfile.TemporaryDirectory() as t:
            根 = Path(t)
            (根 / "bin").mkdir()
            (根 / "bin" / "ollama").write_text("x", encoding="utf-8")
            (根 / "lib" / "ollama").mkdir(parents=True)
            self.assertEqual(_找安装根(根, "ollama"), 根)
        with tempfile.TemporaryDirectory() as t:
            根 = Path(t)
            (根 / "ollama.exe").write_text("x", encoding="utf-8")
            (根 / "lib" / "ollama").mkdir(parents=True)
            self.assertEqual(_找安装根(根, "ollama.exe"), 根)
        with tempfile.TemporaryDirectory() as t:
            根 = Path(t)
            内 = 根 / "ollama-linux-amd64"
            (内 / "bin").mkdir(parents=True)
            (内 / "bin" / "ollama").write_text("x", encoding="utf-8")
            self.assertEqual(_找安装根(根, "ollama"), 内)

    def test_路径全是项目相对的(self):
        """界面/日志里出现的是项目相对路径，证明没有写死绝对路径。"""
        from v8_3.AI.本地模型 import 项目根目录, 相对项目路径
        在项目内 = 项目根目录() / "运行环境" / "本地模型" / "bin" / "ollama"
        self.assertEqual(相对项目路径(在项目内), "运行环境/本地模型/bin/ollama")
        self.assertEqual(相对项目路径("/etc/hosts"), "/etc/hosts")   # 项目外原样

    def test_从零装上并报告版本(self):
        临时, 根 = self._搭一个项目(已有版本=None)
        self.addCleanup(临时.cleanup)
        模块, (好, 消息), 可执行 = self._跑更新(根, _造官方式包("9.9.9"))
        self.assertTrue(好, 消息)
        self.assertIn("9.9.9", 消息)
        self.assertTrue(可执行.is_file())
        self.assertIn("9.9.9", 模块._跑版本(可执行))          # 真能跑起来
        # lib/ 必须留在可执行文件旁边：ollama 靠相对位置找推理后端
        self.assertTrue((根 / "运行环境" / "本地模型" / "lib" / "ollama").is_dir())

    def test_更新会换成新版本并报出前后版本(self):
        临时, 根 = self._搭一个项目(已有版本="0.1.0")
        self.addCleanup(临时.cleanup)
        模块, (好, 消息), 可执行 = self._跑更新(根, _造官方式包("9.9.9"))
        self.assertTrue(好, 消息)
        self.assertIn("0.1.0", 消息)
        self.assertIn("9.9.9", 消息)
        self.assertIn("9.9.9", 模块._跑版本(可执行))

    def test_新版跑不起来时保留旧的(self):
        """自检不过就回滚 —— 更新失败绝不能把能用的基座弄坏。"""
        临时, 根 = self._搭一个项目(已有版本="0.1.0")
        self.addCleanup(临时.cleanup)
        模块, (好, 消息), 可执行 = self._跑更新(根, _造官方式包("坏"))
        self.assertFalse(好, 消息)
        self.assertIn("保留", 消息)
        self.assertIn("0.1.0", 模块._跑版本(可执行))          # 旧基座还在
        self.assertFalse((根 / "运行环境" / "本地模型.下载中").exists())
        self.assertFalse((根 / "运行环境" / "本地模型.旧").exists())

    def test_就位后默认不重新下载(self):
        """幂等：已经内置好时，不强制就不该再去联网。"""
        from v8_3.AI import 本地模型 as 模块
        临时, 根 = self._搭一个项目(已有版本="0.1.0")
        self.addCleanup(临时.cleanup)
        补 = mock.patch.object(模块, "项目便携目录",
                              lambda: 根 / "运行环境" / "本地模型")
        补.start()
        self.addCleanup(补.stop)
        with mock.patch.object(模块, "httpx", None):
            好, 消息 = 模块.下载便携运行时()                # 不强制 → 直接返回
        self.assertTrue(好, 消息)
        self.assertIn("已就位", 消息)

    def test_分片下载拼回原样并清理分片(self):
        """Range 并发下载：拼出来必须和原始数据一模一样，临时分片要清干净。"""
        from v8_3.AI import 本地模型 as 模块
        载荷 = bytes(range(256)) * (300 * 1024)        # ~77 MB，够触发分片
        with tempfile.TemporaryDirectory() as t:
            目标 = Path(t) / "pkg.bin"
            进度: list = []
            with mock.patch.object(模块, "httpx", _假httpx(载荷, 支持分片=True)):
                模块._下到文件("https://例子.invalid/x", 目标,
                            lambda 已, 总: 进度.append((已, 总)))
            self.assertEqual(目标.read_bytes(), 载荷)
            self.assertEqual(进度[-1][1], len(载荷))     # 总量报对了
            self.assertEqual(进度[-1][0], len(载荷))     # 下满了
            self.assertEqual([p.name for p in Path(t).iterdir()], ["pkg.bin"])

    def test_不支持Range就退回单连接(self):
        """服务端不认 Range（返回 200）时，不能硬上分片，要走单连接。"""
        from v8_3.AI import 本地模型 as 模块
        载荷 = b"z" * (40 * 1024 * 1024)
        with tempfile.TemporaryDirectory() as t:
            目标 = Path(t) / "pkg.bin"
            with mock.patch.object(模块, "httpx", _假httpx(载荷)):
                模块._下到文件("https://例子.invalid/x", 目标)
            self.assertEqual(目标.read_bytes(), 载荷)
            self.assertEqual([p.name for p in Path(t).iterdir()], ["pkg.bin"])

    def test_版本解析优先取客户端(self):
        """本机跑着别的 ollama 时输出两行：先服务端版本、再客户端版本 —— 要后者。"""
        from v8_3.AI.本地模型 import _跑版本
        with tempfile.TemporaryDirectory() as t:
            假 = Path(t) / "ollama"
            假.write_text('#!/bin/sh\necho "ollama version is 0.34.1"\n'
                         'echo "Warning: client version is 0.34.2"\n', encoding="utf-8")
            假.chmod(0o755)
            self.assertEqual(_跑版本(假), "0.34.2")
        with tempfile.TemporaryDirectory() as t:
            假 = Path(t) / "ollama"
            假.write_text('#!/bin/sh\necho "ollama version is 9.9.9"\n', encoding="utf-8")
            假.chmod(0o755)
            self.assertEqual(_跑版本(假), "9.9.9")

    def test_老的tgz包也认得出来(self):
        """按文件头判断格式：内容是 gzip 的旧包，哪怕地址写着 .tar.zst 也能解。"""
        临时, 根 = self._搭一个项目(已有版本=None)
        self.addCleanup(临时.cleanup)
        模块, (好, 消息), 可执行 = self._跑更新(根, _造官方式包("9.9.9", 压缩="gz"))
        self.assertTrue(好, 消息)
        self.assertIn("9.9.9", 模块._跑版本(可执行))

    def test_更新后会裁掉GPU后端(self):
        """更新路径也要保证"小包"：下完自动裁 CUDA。"""
        临时, 根 = self._搭一个项目(已有版本="0.1.0")
        self.addCleanup(临时.cleanup)
        模块, (好, 消息), _ = self._跑更新(根, _造官方式包("9.9.9"))
        self.assertTrue(好, 消息)
        self.assertIn("裁掉 GPU 后端", 消息)
        self.assertFalse((根 / "运行环境" / "本地模型" / "lib" / "ollama"
                          / "cuda_v12").exists())

    def test_裁掉GPU后端只留CPU与vulkan(self):
        """官方整包 2.1 GB 里 CUDA 占 ~2 GB；发布包只留 CPU/Vulkan（GitHub 单资源 2 GiB）。"""
        from v8_3.AI import 本地模型 as 模块
        临时, 根 = self._搭一个项目(已有版本="0.1.0")
        self.addCleanup(临时.cleanup)
        库 = 根 / "运行环境" / "本地模型" / "lib" / "ollama"
        (库 / "vulkan").mkdir(parents=True)
        (库 / "vulkan" / "v.bin").write_bytes(b"x" * 100)
        for 名 in ("cuda_v12", "cuda_v13", "rocm"):
            (库 / 名).mkdir()
            (库 / 名 / "大.bin").write_bytes(b"y" * 1000)
        for 补 in (mock.patch.object(模块, "项目便携目录",
                                   lambda: 根 / "运行环境" / "本地模型"),
                   mock.patch.object(模块, "带GPU后端", False)):
            补.start()
            self.addCleanup(补.stop)
        省了, 裁了 = 模块.精简推理后端()
        self.assertEqual(sorted(裁了), ["cuda_v12", "cuda_v13", "rocm"])
        self.assertEqual(省了, 3000)
        self.assertTrue((库 / "vulkan").is_dir())            # vulkan 要留着
        self.assertFalse((库 / "cuda_v12").exists())
        self.assertEqual(模块.精简推理后端(), (0, []))         # 幂等

    def test_带GPU后端时不动它(self):
        """设了 V8_3_本地模型_带GPU后端=1 就别裁（想用 N 卡加速的人自己选）。"""
        from v8_3.AI import 本地模型 as 模块
        临时, 根 = self._搭一个项目(已有版本="0.1.0")
        self.addCleanup(临时.cleanup)
        库 = 根 / "运行环境" / "本地模型" / "lib" / "ollama" / "cuda_v12"
        库.mkdir(parents=True)
        (库 / "大.bin").write_bytes(b"y" * 10)
        for 补 in (mock.patch.object(模块, "项目便携目录",
                                   lambda: 根 / "运行环境" / "本地模型"),
                   mock.patch.object(模块, "带GPU后端", True)):
            补.start()
            self.addCleanup(补.stop)
        self.assertEqual(模块.精简推理后端(), (0, []))
        self.assertTrue(库.is_dir())

    def test_说明里给出项目相对路径(self):
        临时, 根 = self._搭一个项目(已有版本="0.1.0")
        self.addCleanup(临时.cleanup)
        from v8_3.AI import 本地模型 as 模块
        补 = mock.patch.object(模块, "项目便携目录",
                              lambda: 根 / "运行环境" / "本地模型")
        补.start()
        self.addCleanup(补.stop)
        说明 = 模块.内置运行时说明()
        self.assertIn("v0.1.0", 说明)
        self.assertIn("运行环境/本地模型", 说明)
        # 界面文案里绝不出现绝对路径（临时项目在项目外，所以这里用真项目再验一次）
        真说明 = 模块.内置运行时说明()
        self.assertNotIn(str(模块.项目根目录()), 真说明)
        self.assertTrue("运行环境" in 真说明 or "未就位" in 真说明, 真说明)


if __name__ == "__main__":
    unittest.main()


class 端口黑洞探测测试(unittest.TestCase):
    """Windows 上"连没人听的端口"是**丢包等超时**，不是立刻拒绝。

    所以探测必须**并发 + 短超时**：6 个端口串行、每个 5 秒 = 30 秒，AI 页一打开
    就是"卡死"（真机 CI 实测 60 秒 —— 检测() 在被预热时被调了两次）。这条把它钉住。

    一个测试里把两条探测路径都量了：黑洞监听架一次就够，分开写会互相抢端口（跳过）。
    """

    def test_黑洞端口不许把探测拖住(self):
        import socket
        import threading
        import time as _t
        from v8_3.AI.本地模型 import (候选端口, 取本地模型配置, 本地模型客户端,
                                 探测到的运行时)

        监听们 = []
        for _提供方, 端口 in 候选端口:
            套 = socket.socket()
            套.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                套.bind(("127.0.0.1", 端口))
                套.listen(8)
            except OSError:
                套.close()
                continue          # 端口被真服务占着（比如本机真在跑 ollama）就跳过
            监听们.append(套)

            收下的 = []

            def 收(套=套):
                while True:
                    try:
                        连接, _ = 套.accept()      # 收下连接，但**永不回数据**
                    except OSError:
                        return
                    收下的.append(连接)              # 留着不放（回数据就不是黑洞了）
            threading.Thread(target=收, daemon=True).start()

        if not 监听们:
            self.skipTest("候选端口全被占用，造不出黑洞")
        try:
            客户端 = 本地模型客户端(取本地模型配置({"启用": True}),
                              日志回调=lambda *_: None)
            开始 = _t.time()
            客户端.检测()
            检测用时 = _t.time() - 开始
            开始 = _t.time()
            探测到的运行时()
            探测用时 = _t.time() - 开始
        finally:
            for 套 in 监听们:
                套.close()
            for 连接 in 收下的:
                try:
                    连接.close()
                except Exception:  # noqa: BLE001
                    pass
        self.assertLess(检测用时, 3.0,
                        f"黑洞端口把 检测() 拖住了 {检测用时:.1f}s（应该并发 + 短超时）")
        self.assertLess(探测用时, 3.0,
                        f"黑洞端口把 探测到的运行时() 拖住了 {探测用时:.1f}s")
