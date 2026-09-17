# 百度网盘适配器/界面/公共线程.py
"""公共 QThread 子类

⚠️ 关键：所有线程的 run() 里绝对不能调用 gc.collect()
   Python 3.14 下会触发跨线程 GC，导致 PySide6 段错误
"""

import logging
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from 核心.认证.登录服务 import 登录服务
from 核心.接口.上传接口 import 上传接口
from 核心.网络.网络客户端 import 网络客户端

logger = logging.getLogger("百度网盘.界面.线程")


class 扫码登录线程(QThread):
    """扫码登录后台线程。

    :signal 二维码:   `(二维码内容, 标识)`。百度侧「内容」是**服务端下发的
                      base64 PNG**（不再是本地生成的链接），「标识」是 sign。
    :signal 扫码状态: `/channel/unicast` 的 status：0 未扫 / 1 已扫 / 2 已确认
    """

    二维码 = Signal(str, str)
    扫码状态 = Signal(int)
    成功 = Signal(dict)
    失败 = Signal(str)

    def run(self):
        登录 = None
        try:
            登录 = 登录服务()
            结果 = 登录.完成扫码登录(
                二维码回调=lambda 内容, 标识: self.二维码.emit(内容, 标识),
                状态回调=lambda 状态: self.扫码状态.emit(int(状态)),
            )
            self.成功.emit(结果)
        except Exception as e:
            logger.exception("扫码登录异常")
            self.失败.emit(str(e))
        finally:
            if 登录 is not None:
                try:
                    登录.关闭()
                except Exception:
                    pass


class 网络查询线程(QThread):
    成功 = Signal(str, object)
    失败 = Signal(str, str)

    def __init__(self, 任务名: str, 函数):
        super().__init__()
        self.任务名 = 任务名
        self.函数 = 函数

    def run(self):
        try:
            结果 = self.函数()
            self.成功.emit(self.任务名, 结果)
        except Exception as e:
            logger.exception(f"任务 {self.任务名} 异常")
            self.失败.emit(self.任务名, str(e))


class 批量上传线程(QThread):
    总进度 = Signal(str, float)
    单文件成功 = Signal(str, dict)
    全部成功 = Signal(int, int)
    失败 = Signal(str)

    def __init__(self, 网络: 网络客户端,
                 路径列表: list,
                 父目录ID: str,
                 是文件夹: bool):
        super().__init__()
        self.网络 = 网络
        self.路径列表 = 路径列表
        self.父目录ID = 父目录ID
        self.是文件夹 = 是文件夹

    def run(self):
        try:
            接口 = 上传接口(self.网络)
            总数 = len(self.路径列表)
            成功数 = 0

            for i, 路径 in enumerate(self.路径列表):
                基 = i / 总数
                范围 = 1 / 总数

                def 子进度(阶段: str, p: float,
                           _b=基, _r=范围):
                    self.总进度.emit(阶段, _b + p * _r)

                if self.是文件夹:
                    结果 = 接口.上传文件夹(
                        路径, self.父目录ID,
                        进度回调=子进度,
                        单文件回调=lambda n, info:
                            self.单文件成功.emit(n, info),
                    )
                    成功数 += len(结果)
                else:
                    info = 接口.上传文件(
                        路径, self.父目录ID, 进度回调=子进度)
                    self.单文件成功.emit(Path(路径).name, info)
                    成功数 += 1

            self.全部成功.emit(成功数, 总数)
        except Exception as e:
            logger.exception("批量上传异常")
            self.失败.emit(str(e))