#!/usr/bin/env bash
# 给 Windows 绿色版装依赖（在 Linux 上交叉装 Windows 轮子）
# 变量名一律 ASCII（bash 不认中文变量名，会被当成命令执行）
set -e
cd "$(dirname "$0")/windows"
export UV_CACHE_DIR=/tmp/uvcache
INDEX="https://mirrors.aliyun.com/pypi/simple"

echo "=== 主程序依赖（先跳过 oss2 / crcmod：它们没有 Windows 轮子）==="
grep -vE "^(oss2|crcmod)==" /tmp/主环境包.txt > /tmp/主环境包_win2.txt
uv pip install --python-platform windows --python-version 3.14 \
  --index-url "$INDEX" \
  --target ./python主/Lib/site-packages -r /tmp/主环境包_win2.txt

echo "=== oss2 单独装（--no-deps：它的依赖上面都装齐了）==="
uv pip install --python-platform windows --python-version 3.14 \
  --index-url "$INDEX" --no-deps \
  --target ./python主/Lib/site-packages oss2==2.19.1

echo "=== 语音识别依赖 ==="
uv pip install --python-platform windows --python-version 3.14 \
  --index-url "$INDEX" \
  --target ./语音识别/python/Lib/site-packages -r /tmp/asr包.txt

echo "=== crcmod：PyPI 上没有 Windows 轮子，用它的纯 Python 实现顶上 ==="
# 相对本脚本推导仓库根（别写死本机路径：别人机器 / CI 上跑不了）
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd -- "$HERE/.." && pwd)"
SRC="$ROOT/运行环境/venv/lib/python3.14/site-packages/crcmod"
DST="./python主/Lib/site-packages/crcmod"
mkdir -p "$DST"
for f in __init__.py crcmod.py predefined.py _crcfunpy.py; do cp "$SRC/$f" "$DST/$f"; done
cp -r "$SRC"/../crcmod-1.7.dist-info ./python主/Lib/site-packages/ 2>/dev/null || true
echo "  crcmod 纯 Python 文件已就位：$(ls "$DST" | tr '\n' ' ')"

echo "=== 体积 ==="
du -sh python主 语音识别
echo "WINDOWS_DEPS_DONE"
