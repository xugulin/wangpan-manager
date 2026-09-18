"""假网盘后端：把逻辑路径映射到本地目录，用于无网络测试。"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

from 后端_基类 import 后端基类, 规范


class 后端(后端基类):
    """项目根就是“云端”存储目录。"""

    def __init__(self, 项目根, 发事件=None, 日志=None):
        super().__init__(项目根, 发事件, 日志)
        self.存储根 = self.项目根 / "云端"
        self.存储根.mkdir(parents=True, exist_ok=True)

    def _本地(self, 路径: str) -> Path:
        路径 = 规范(路径)
        if 路径 == "/":
            return self.存储根
        return self.存储根 / 路径.lstrip("/")

    def account(self) -> dict:
        """假网盘：凭证文件在 = 已登录。

        ⚠️ 以前这里恒回 ``logged_in=True``，于是"退出登录之后界面还在、
        凭证文件没了状态还是已登录"这类 bug 在假网盘上根本测不出来。
        现在如实反映凭证文件的存在与否（与真网盘对齐）。
        """
        凭证 = self._凭证文件()
        存在 = False
        try:
            存在 = Path(凭证).is_file()
        except Exception:
            pass
        return {
            "logged_in": bool(存在),
            "user": "fake-user" if 存在 else "",
            "data_dir": str(self.存储根),
            "detail": {"note": "本地假网盘，仅用于测试",
                       "no_credential": not 存在,
                       "credential": str(凭证)},
        }

    def list(self, path: str) -> list[dict]:
        目录 = self._本地(path)
        if not 目录.is_dir():
            raise FileNotFoundError(f"目录不存在：{path}")
        结果 = []
        with os.scandir(目录) as it:
            for 项 in it:
                st = 项.stat()
                结果.append({
                    "name": 项.name,
                    "path": 规范((path.rstrip("/") or "") + "/" + 项.name),
                    "is_dir": 项.is_dir(),
                    "size": 0 if 项.is_dir() else st.st_size,
                    "modified": time.strftime(
                        "%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime)),
                    "id": 项.name,
                })
        return sorted(结果, key=lambda x: (not x["is_dir"], x["name"]))

    def stat(self, path: str) -> dict | None:
        路径 = self._本地(path)
        if not 路径.exists():
            return None
        st = 路径.stat()
        return {
            "name": 路径.name,
            "path": 规范(path),
            "is_dir": 路径.is_dir(),
            "size": 0 if 路径.is_dir() else st.st_size,
            "modified": time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime)),
            "id": 路径.name,
        }

    def ensure_dir(self, path: str) -> dict:
        目录 = self._本地(path)
        目录.mkdir(parents=True, exist_ok=True)
        return {"id": 规范(path), "path": 规范(path)}

    def upload(self, local_path: str, remote_dir: str, name: str,
               task_id: str, progress) -> dict:
        源 = Path(local_path)
        if not 源.is_file():
            raise FileNotFoundError(f"本地文件不存在：{local_path}")
        目标目录 = self._本地(remote_dir)
        目标目录.mkdir(parents=True, exist_ok=True)
        目标 = 目标目录 / name
        总 = 源.stat().st_size
        已传 = 0
        临时 = 目标.with_name(目标.name + ".part")
        with 源.open("rb") as i, 临时.open("wb") as o:
            while True:
                块 = i.read(1024 * 1024)
                if not 块:
                    break
                o.write(块)
                已传 += len(块)
                if progress:
                    progress("upload", 已传, 总)
        os.replace(临时, 目标)
        return {
            "name": name,
            "path": 规范((remote_dir.rstrip("/") or "") + "/" + name),
            "size": 总,
            "bytes": 已传,
            "skipped": False,
        }

    def play_link(self, remote_path: str) -> dict:
        """假网盘：返回本地 file:// 直链，便于离线验证播放链路。"""
        from pathlib import Path as _P
        路径 = self._本地(remote_path)
        if not 路径.exists():
            raise FileNotFoundError(f"假网盘文件不存在：{remote_path}")
        if 路径.is_dir():
            raise IsADirectoryError(f"假网盘路径是目录：{remote_path}")
        return {"url": str(_P(路径).resolve()), "headers": {},
                "size": 路径.stat().st_size, "name": 路径.name,
                "range": True}

    def download(self, remote_path: str, local_path: str,
                 task_id: str, progress, 续传: bool = True,
                 保守: bool = False) -> dict:
        源 = self._本地(remote_path)
        if not 源.is_file():
            raise FileNotFoundError(f"文件不存在：{remote_path}")
        目标 = Path(local_path)
        目标.parent.mkdir(parents=True, exist_ok=True)
        总 = 源.stat().st_size
        已传 = 0
        with 源.open("rb") as i, 目标.open("wb") as o:
            while True:
                块 = i.read(1024 * 1024)
                if not 块:
                    break
                o.write(块)
                已传 += len(块)
                if progress:
                    progress("download", 已传, 总)
        return {"path": str(目标), "size": 总, "bytes": 已传}

    # ==================== 统一登录（离线联调/自检用） ====================

    #: 1x1 PNG（够 QPixmap 解码，用来验证"图片型二维码"这条路径）
    测试二维码PNG = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
        "z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")

    假验证码 = "123456"

    def _凭证文件(self):
        return self.项目根 / "数据" / "登录.json"

    def _读凭证(self) -> dict:
        import json
        try:
            return json.loads(self._凭证文件().read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _写凭证(self, 数据: dict) -> None:
        import json
        self._凭证文件().parent.mkdir(parents=True, exist_ok=True)
        self._凭证文件().write_text(
            json.dumps(数据, ensure_ascii=False, indent=2), encoding="utf-8")

    def _登出所有方式(self) -> None:
        """清掉所有登录痕迹，方便自检反复跑。"""
        for 文件 in (self._凭证文件(), self.项目根 / "数据" / "令牌.json"):
            try:
                Path(文件).unlink(missing_ok=True)
            except Exception:
                pass
        self._会话 = {}

    def 退出登录(self) -> dict:
        """假网盘：删掉本地凭证文件（离线、无副作用）。"""
        清除 = []
        for 文件 in (self._凭证文件(), self.项目根 / "数据" / "令牌.json"):
            try:
                if Path(文件).is_file():
                    Path(文件).unlink()
                    清除.append(str(文件))
            except Exception:
                pass
        self._会话 = {}
        return {"状态": "成功", "消息": "已退出登录", "清除": 清除,
                "账号": self.account()}

    def auth_caps(self) -> dict:
        # 假网盘故意把"邮箱/账号密码"标成不支持，界面据此禁用这两页
        return {
            "qrcode": {"支持": True, "说明": "扫码登录（假网盘直接成功）"},
            "cookie": {"支持": True, "说明": "导入 Cookie（粘任意 k=v 即可）"},
            "sms": {"支持": True, "说明": f"短信登录（验证码固定 {self.假验证码}）"},
            "email": {"支持": False, "说明": "假网盘未提供邮箱登录"},
            "token": {"支持": True, "说明": "令牌登录（填任意 access_token）"},
            "password": {"支持": False, "说明": "假网盘未提供账号密码登录"},
        }

    #: 假网盘扫码返回的形态：`图片`（base64 二维码）或 `设备码`
    #: （复刻光鸭：只给一条验证地址，界面得自己把地址画成二维码）。
    二维码模式 = "图片"

    def _扫码模式(self) -> str:
        """取扫码形态。

        假后端跑在**桥子进程**里，测试改不了它的类属性，所以额外支持一个
        标记文件 ``数据/扫码模式.txt``（自检写、桥进程读）。
        """
        try:
            文件 = self.项目根 / "数据" / "扫码模式.txt"
            if 文件.is_file():
                文本 = 文件.read_text(encoding="utf-8").strip()
                if 文本:
                    return 文本
        except Exception:
            pass
        return str(self.二维码模式)

    def login_qr_start(self) -> dict:
        self._登出所有方式()
        if self._扫码模式() == "设备码":
            self._会话 = {"类型": "设备码"}
            return {
                "状态": "成功",
                "类型": "设备码",
                "验证地址": "https://example.invalid/__/auth/device/"
                          "?client_id=fake-client&user_code=FAKE-CODE-123",
                "用户码": "FAKE-CODE-123",
                "会话": "假扫码会话",
                "有效期秒": 30,
                "提示": "用假网盘 App 扫描二维码即可授权",
            }
        self._会话 = {"类型": "图片"}
        return {"类型": "图片", "图片base64": self.测试二维码PNG,
                "提示": "用假网盘 App 扫码（自检会自动成功）",
                "会话": "假扫码会话", "有效期秒": 30}

    def login_qr_wait(self, 会话: str = "", 超时秒: float = 180.0) -> dict:
        import time
        time.sleep(min(1.0, max(0.1, float(超时秒 or 1.0))))
        self._写凭证({"方式": "qrcode", "用户": "fake-qr-user",
                   "会话": 会话 or "假扫码会话"})
        return self.登录成功("扫码登录成功（假网盘）", "已写入本地凭证")

    def login_cookie(self, 文本: str) -> dict:
        文本 = str(文本 or "").strip()
        if "=" not in 文本:
            return self.登录失败("Cookie 文本里没有 k=v 字段",
                             "示例：BDUSS=xxx; STOKEN=yyy")
        self._写凭证({"方式": "cookie", "用户": "fake-cookie-user",
                   "原文长度": len(文本)})
        return self.登录成功("Cookie 登录成功（假网盘）")

    def login_sms_send(self, 手机号: str) -> dict:
        手机号 = str(手机号 or "").strip()
        if len(手机号) < 6:
            return self.登录失败("手机号格式不对", "示例：+86 13800000000")
        self._会话 = {"类型": "短信", "手机号": 手机号}
        return {"状态": "成功", "消息": f"验证码已发送（假网盘固定 {self.假验证码}）",
                "会话": "假短信会话", "提示": f"验证码固定 {self.假验证码}"}

    def login_sms_verify(self, 会话: str, 验证码: str,
                         手机号: str = "") -> dict:
        if str(验证码 or "").strip() != self.假验证码:
            return self.登录失败("验证码不正确", f"假网盘验证码固定 {self.假验证码}")
        self._写凭证({"方式": "sms", "用户": "fake-sms-user",
                   "手机号": 手机号 or (self._会话 or {}).get("手机号", "")})
        return self.登录成功("短信登录成功（假网盘）")

    def login_token(self, 访问令牌: str = "", 刷新令牌: str = "",
                    额外: dict | None = None) -> dict:
        if not str(访问令牌 or "").strip():
            return self.登录失败("访问令牌不能为空")
        self._写凭证({"方式": "token", "用户": "fake-token-user",
                   "访问令牌长度": len(str(访问令牌)),
                   "有刷新令牌": bool(刷新令牌)})
        return self.登录成功("令牌登录成功（假网盘）")

    def delete(self, path: str) -> dict:
        目标 = self._本地(path)
        if 目标.is_dir():
            shutil.rmtree(目标)
        elif 目标.exists():
            目标.unlink()
        return {"deleted": True, "path": 规范(path)}
