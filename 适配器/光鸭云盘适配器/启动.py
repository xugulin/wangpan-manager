# 光鸭云盘适配器/启动.py
"""
项目启动入口
"""
import os
import sys
import gc

# Python 3.14 GC 与 PySide6 QThread 冲突缓解
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