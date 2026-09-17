#!/usr/bin/env python3
"""离屏渲染界面截图（人工核对布局用，不联网、不弹窗）。

用法：
    python 工具/界面截图.py [输出目录]      # 默认 /tmp/v8_3_截图
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

项目根 = Path(__file__).resolve().parents[1]
if str(项目根) not in sys.path:
    sys.path.insert(0, str(项目根))

# 自举：不是项目自带的解释器就换过去，避免用系统 python 跑出
# "No module named 'PySide6' / 'httpx'"。详见 v8_3/自举.py
from v8_3.自举 import 确保项目环境

确保项目环境()
os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtWidgets import QApplication

from v8_3.配置 import 保存配置, 加载配置, 准备假网盘目录
from v8_3.界面.主窗口 import 主窗口
from v8_3.界面.网盘对话框 import 网盘编辑对话框


def 建环境(根: Path) -> Path:
    配置项 = []
    # 全部用假网盘后端，截图时不联网也能跑出真实状态
    for i, (名称, 类型) in enumerate((("百度网盘", "fake"), ("光鸭云盘", "fake"),
                                     ("夸克网盘", "fake"), ("百度小号", "fake"),
                                     ("本地测试盘", "fake")), start=1):
        目录 = 根 / f"适配器{i}"
        准备假网盘目录(目录)
        云端 = 目录 / "云端"
        (云端 / "电影").mkdir(parents=True, exist_ok=True)
        (云端 / "备份").mkdir(parents=True, exist_ok=True)
        (云端 / "电影" / "示例影片.mkv").write_bytes(b"0" * 1024)
        (云端 / "说明.md").write_text("# 示例\n", encoding="utf-8")
        (云端 / "照片.jpg").write_bytes(b"0" * 2048)
        配置项.append({"标识": f"disk_{i}", "类型": 类型, "名称": 名称,
                     "路径": str(目录), "线程数": 8, "启用": True})
    配置 = 加载配置(根 / "配置.json")
    配置["适配器"] = 配置项
    保存配置(配置, 根 / "配置.json")
    return 根 / "配置.json"


def main() -> int:
    import time as _time
    输出 = Path(sys.argv[1]).expanduser() if len(sys.argv) > 1 else Path("/tmp/v8_3_截图")
    输出.mkdir(parents=True, exist_ok=True)
    临时 = tempfile.TemporaryDirectory(prefix="v8_3_shot_")
    配置路径 = 建环境(Path(临时.name))
    应用 = QApplication.instance() or QApplication(sys.argv[:1])
    根 = Path(临时.name)
    窗口 = 主窗口(加载配置(配置路径), 配置路径=配置路径)
    窗口.resize(1400, 860)
    窗口.show()
    for _ in range(60):
        应用.processEvents()

    窗口.切换网盘页("disk_5")   # 本地假网盘，有内容可看

    def 存(名: str, 部件=None):
        for _ in range(40):
            应用.processEvents()
            _time.sleep(0.05)
        路径 = 输出 / 名
        (部件 or 窗口).grab().save(str(路径))
        print(f"已保存 {路径}")

    存("01_网盘页_光鸭.png")
    窗口.切换到传输页()
    存("02_传输页_空.png")
    # 加几个上传任务，让底部统计栏有真实数字
    传输 = 窗口.传输页面()
    素材 = Path(tempfile.mkdtemp(prefix="v8_3_shot_src_"))
    for i in range(3):
        文件 = 素材 / f"素材{i}.bin"
        文件.write_bytes(b"x" * (2048 * (i + 1)))
        传输.添加上传任务("disk_5", str(文件), "/备份")
    for _ in range(200):
        应用.processEvents()
        import time as _t
        _t.sleep(0.05)
        if 传输._本地线程 is None:
            break
    # 跑一小批跨网盘传输，让统计栏显示"决策来源/当前决策"
    (根 / "适配器5" / "云端" / "决策源").mkdir(parents=True, exist_ok=True)
    for i in range(4):
        (根 / "适配器5" / "云端" / "决策源" / f"片{i}.bin").write_bytes(b"z" * 4096)
    idx = 传输.源网盘框.findData("disk_5"); 传输.源网盘框.setCurrentIndex(idx)
    传输.源路径框.setText("/决策源")
    idx = 传输.目标网盘框.findData("disk_4"); 传输.目标网盘框.setCurrentIndex(idx)
    传输.目标路径框.setText("/决策目标")
    传输._开始传输()
    for _ in range(200):
        应用.processEvents()
        _time.sleep(0.05)
        if 传输._批次线程 is None:
            break
    存("03_传输页_统计栏.png")
    窗口.切换到敏感词页()
    import time as _t
    窗口.动作.敏感词库().添加敏感词("disk_5", "示例敏感词", "示例安全词")
    窗口._敏感词页面.刷新()
    存("04_敏感词页.png")
    窗口.切换到AI页()
    for _ in range(20):
        应用.processEvents()
        _t.sleep(0.05)
    存("05_AI页.png")
    窗口.切换到日志页()
    存("06_日志页.png")

    对话 = 网盘编辑对话框(窗口.配置, None, 窗口)
    对话.resize(720, 460)
    对话.show()
    存("07_新增网盘对话框.png", 对话)
    对话.close()

    窗口.close()
    for _ in range(20):
        应用.processEvents()
    临时.cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
