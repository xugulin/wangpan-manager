"""百度认证层/网络层的「errno:-6 日志风暴 + 递归」回归测试（全程离线）。

对应现场故障（用户报告）：
  * ``[认证] 刷新会话失败：RecursionError: maximum recursion depth exceeded``
  * ``[认证] 刷新会话失败：接口返回异常（errno:-6）`` 一次会话刷 730 条

用 httpx 的 MockTransport 造一个"恒回 errno:-6"的服务端，断言：
  1. 同一会话里"真正去刷新"的次数被节流到常数级（不是每个请求一次）；
  2. 刷新会话的告警日志只在状态变化时出现；
  3. 多线程并发不会打出 RecursionError；
  4. `/api/loginStatus` 的回退路径能补到 bdstoken（现场那条 bdstoken 路径）。
"""

from __future__ import annotations

import json
import logging
import sys
import tempfile
import threading
import unittest
from pathlib import Path

项目根 = Path(__file__).resolve().parents[1]
if str(项目根) not in sys.path:
    sys.path.insert(0, str(项目根))

适配器目录 = 项目根 / "适配器" / "百度网盘适配器"
if str(适配器目录) not in sys.path:
    sys.path.insert(0, str(适配器目录))

import httpx  # noqa: E402

from 核心.认证.认证服务 import 认证服务  # noqa: E402
from 核心.认证.会话仓库 import 会话仓库  # noqa: E402
from 核心.网络.网络客户端 import 网络客户端  # noqa: E402


class _记录器(logging.Handler):
    def __init__(self):
        super().__init__()
        self.认证告警: list[str] = []
        self.网络告警: list[str] = []
        self.总数 = 0

    def emit(self, 记录):
        self.总数 += 1
        文本 = 记录.getMessage()
        if 记录.name.endswith("认证") and 记录.levelno >= logging.WARNING:
            self.认证告警.append(文本)
        if 记录.name.endswith("网络") and 记录.levelno >= logging.WARNING:
            self.网络告警.append(文本)


class 百度节流测试(unittest.TestCase):
    def setUp(self):
        # 每个用例都要一份干净的全局节流状态（模块级变量）
        from 核心.网络 import 网络客户端 as 网络模块
        from 核心.认证 import 认证服务 as 认证模块
        网络模块._刷新节流.update({"进行中": None, "上次时间": 0.0,
                                 "上次结果": False})
        网络模块._缺令牌提醒.update({"上次时间": 0.0, "次数": 0})
        认证模块._认证节流.update({"进行中": False, "上次时间": 0.0,
                                 "上次结果": False, "连续失败": 0})
        self.临时 = tempfile.TemporaryDirectory(prefix="v8_3_baidu_")
        self.仓库 = 会话仓库(文件路径=Path(self.临时.name) / "会话.json")
        # 现场那种会话：BDUSS/STOKEN 都在，但服务端一律回 -6
        self.仓库.保存会话(bduss="a" * 192, bduss_bfess="a" * 192,
                       stoken="b" * 64)
        self.请求计数 = {"模板变量": 0, "登录态": 0, "其它": 0}
        self.登录态给令牌 = "c" * 32

    def tearDown(self):
        self.临时.cleanup()

    def _装网络(self, 登录态回令牌: bool = True) -> 认证服务:
        服务 = 认证服务(仓库=self.仓库)

        def 处理(请求: httpx.Request) -> httpx.Response:
            路径 = 请求.url.path
            if 路径.endswith("/api/gettemplatevariable"):
                self.请求计数["模板变量"] += 1
                return httpx.Response(
                    200, json={"errno": -6, "result": [],
                               "request_id": 1})
            if 路径.endswith("/api/loginStatus"):
                self.请求计数["登录态"] += 1
                if 登录态回令牌:
                    return httpx.Response(200, json={
                        "errno": 0,
                        "login_info": {"uk": 455281002, "username": "u",
                                       "bdstoken": self.登录态给令牌},
                    })
                return httpx.Response(200, json={
                    "errno": -6, "errmsg": "账户已过期，重新登陆"})
            self.请求计数["其它"] += 1
            return httpx.Response(200, json={"errno": -6, "info": [],
                                            "request_id": 2})

        服务.网络.客户端 = httpx.Client(
            transport=httpx.MockTransport(处理),
            base_url=服务.网络.基础地址,
            timeout=httpx.Timeout(connect=5.0, read=5.0, write=5.0, pool=5.0),
            follow_redirects=True)
        return 服务

    # ---------------- ① 日志风暴 ----------------

    def test_连续写请求不会每个都去刷新会话(self):
        记录 = _记录器()
        根日志 = logging.getLogger()
        根日志.addHandler(记录)
        原级别 = 根日志.level
        根日志.setLevel(logging.DEBUG)
        try:
            服务 = self._装网络()
            for i in range(10):
                with self.assertRaises(Exception):
                    服务.网络.请求("POST", "/api/create",
                                params={"path": f"/x{i}", "isdir": "1"},
                                需要bdstoken=True)
        finally:
            根日志.removeHandler(记录)
            根日志.setLevel(原级别)

        self.assertLessEqual(
            self.请求计数["模板变量"], 2,
            f"10 次写请求只该真正刷 1~2 次会话，实际 "
            f"{self.请求计数['模板变量']} 次")
        self.assertLessEqual(
            len(记录.认证告警), 1,
            f"刷新失败告警只该出现 1 条，实际 {len(记录.认证告警)} 条："
            f"{记录.认证告警[:3]}")

    # ---------------- ② 日志不再被同一条淹没 ----------------

    def test_缺bdstoken的提醒被节流(self):
        记录 = _记录器()
        根日志 = logging.getLogger()
        根日志.addHandler(记录)
        try:
            服务 = self._装网络()
            for i in range(10):
                try:
                    服务.网络.请求("POST", "/api/create",
                                params={"path": f"/y{i}", "isdir": "1"})
                except Exception:
                    pass
        finally:
            根日志.removeHandler(记录)
        提醒 = [x for x in 记录.网络告警 if "没有 bdstoken" in x]
        self.assertLessEqual(
            len(提醒), 1, f"缺 bdstoken 的提醒最多 1 条，实际 {len(提醒)} 条")

    # ---------------- ③ 多线程不递归 ----------------

    def test_多线程并发不出现递归(self):
        服务 = self._装网络()
        异常: list[str] = []

        def 打():
            try:
                服务.网络.请求("POST", "/api/create",
                            params={"path": "/z", "isdir": "1"},
                            需要bdstoken=True)
            except RecursionError as e:
                异常.append(f"RecursionError: {e}")
            except Exception as e:  # noqa: BLE001
                异常.append(type(e).__name__)

        线程们 = [threading.Thread(target=打) for _ in range(12)]
        for t in 线程们:
            t.start()
        for t in 线程们:
            t.join()
        self.assertNotIn("RecursionError", " ".join(异常), 异常)
        self.assertLessEqual(
            self.请求计数["模板变量"], 2,
            f"12 线程并发也只该刷 1~2 次会话，实际 "
            f"{self.请求计数['模板变量']} 次")

    def test_刷新会话自身不再触发刷新器(self):
        """刷新器内部取模板变量时必须摘掉刷新器，否则认证层↔网络层成环。"""
        服务 = self._装网络()
        self.assertFalse(服务.刷新会话())
        self.assertLessEqual(self.请求计数["模板变量"], 1,
                            "刷新一次只该发一条模板变量请求")

    # ---------------- ④ loginStatus 回退拿 bdstoken ----------------

    def test_模板变量失败时从登录态补bdstoken(self):
        服务 = self._装网络(登录态回令牌=True)
        with self.assertRaises(Exception):
            服务.取模板变量()
        令牌 = 服务.从登录态补bdstoken()
        self.assertEqual(令牌, self.登录态给令牌)
        self.assertEqual(self.仓库.获取bdstoken(), self.登录态给令牌,
                         "拿到的 bdstoken 要落进会话仓库（写接口要用）")
        self.assertEqual(str(self.仓库.获取uk()), "455281002")

    def test_登录态也失效时报错而不是假装成功(self):
        服务 = self._装网络(登录态回令牌=False)
        with self.assertRaises(Exception) as ctx:
            服务.从登录态补bdstoken()
        self.assertIn("过期", str(ctx.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
