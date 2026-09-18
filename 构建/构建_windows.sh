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

echo "=== ⑤ 便携 ollama 运行时（Windows 版，不含模型权重）==="
# 用户要求：打包预装基础 ollama，但不预装任何模型权重。
# 有就跳过；下不动也不让整个构建失败（用户可在 AI 页点「装运行时」补装）。
if [ ! -f 本地模型/ollama.exe ]; then
  mkdir -p 本地模型
  echo "  下载 ollama-windows-amd64.zip…"
  if curl -fL --retry 3 -o ollama-win.zip https://ollama.com/download/ollama-windows-amd64.zip; then
    if command -v unzip >/dev/null 2>&1; then
      unzip -q -o ollama-win.zip -d 本地模型 || true
    else
      7z x -y -o本地模型 ollama-win.zip >/dev/null || true
    fi
    rm -f ollama-win.zip
  else
    echo "  ⚠️ ollama 下载失败：这个 Windows 包将不含 ollama（AI 页可补装）"
  fi
fi
ls -la 本地模型 2>/dev/null | head -5 || true

echo "=== ⑥ 体积 ==="
du -sh python主 语音识别
echo "WINDOWS_RUNTIME_DONE"
