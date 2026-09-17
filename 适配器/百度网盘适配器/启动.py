# 百度网盘适配器/启动.py
"""
项目启动入口
"""
import os
import sys
import gc

# Python 3.14 GC 与 PySide6 QThread 冲突缓解
#
# ⛔ 千万不要"顺手加一次 gc.collect()"来"缓解内存压力"：
#    实测代价：91 万对象时整堆一次 27~30ms；
#    实测风险：与存活 Qt 对象、后台上传线程并存时做整堆回收会**段错误**
#    （2026-09-14 真机复现：加了 4s 定时 gc.collect() 后，38 文件批量上传
#      必崩于第 20+ 个文件；移除后同一批量可跑完）。
#    上传/下载都是流式处理，内存不会无限增长（实测每文件约 0.7MiB，
#    30 个文件 RSS 35→56MiB），不需要手工回收。
if sys.version_info >= (3, 14):
    gc.disable()
    gc.set_threshold(1000000, 1000000, 1000000)
    print("[启动] Python 3.14，已关闭自动 GC")

项目根目录 = os.path.dirname(os.path.abspath(__file__))
if 项目根目录 not in sys.path:
    sys.path.insert(0, 项目根目录)

print(f"[启动] 项目根目录：{项目根目录}")

try:
    from 界面.主界面 import 运行界面
except Exception as e:
    import traceback
    traceback.print_exc()
    print(f"\n[启动失败] {e}")
    input("按回车键关闭...")
    sys.exit(1)

if __name__ == "__main__":
    运行界面()