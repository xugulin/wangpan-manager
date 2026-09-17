#!/usr/bin/env bash
# 组装 Windows 绿色版的运行时（在 Linux 上交叉装配）
# 注意：bash 变量名只能用 ASCII，写中文会被当成命令执行（本项目踩过这个坑，别改回去）
set -e
cd "$(dirname "$0")/windows"
export UV_CACHE_DIR=/tmp/uvcache
MIRROR="https://mirror.nju.edu.cn/github-release/astral-sh/python-build-standalone/20260901"
ASSET="cpython-3.14.7%2B20260901-x86_64-pc-windows-msvc-install_only.tar.gz"

echo "=== ① 下载 Windows 独立 CPython 3.14.7（南大镜像）==="
[ -f cpython-win.tar.gz ] || curl -sL --retry 3 -o cpython-win.tar.gz "$MIRROR/$ASSET"
ls -la cpython-win.tar.gz

echo "=== ② 解开：主环境（依赖直接装在自带 Python 里）==="
rm -rf python python主 语音识别
tar xzf cpython-win.tar.gz
[ -d python ] && mv python python主
ls python主 | head -5

echo "=== ③ 主程序依赖（Windows 轮子）==="
uv pip install --python-platform windows --python-version 3.14 \
  --target ./python主/Lib/site-packages -r /tmp/主环境包.txt 2>&1 | tail -3

echo "=== ④ 语音识别环境：再放一份独立 Python ==="
mkdir -p 语音识别
tar xzf cpython-win.tar.gz -C 语音识别
mv 语音识别/python 语音识别/临时 2>/dev/null || true
[ -d 语音识别/临时 ] && mv 语音识别/临时 语音识别/python
uv pip install --python-platform windows --python-version 3.14 \
  --target ./语音识别/python/Lib/site-packages -r /tmp/asr包.txt 2>&1 | tail -3

echo "=== ⑤ 体积 ==="
du -sh python主 语音识别
echo "WINDOWS_RUNTIME_DONE"
