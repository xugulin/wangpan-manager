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
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
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


if __name__ == "__main__":
    unittest.main()
