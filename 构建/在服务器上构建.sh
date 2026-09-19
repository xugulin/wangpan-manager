#!/usr/bin/env bash
# ============================================================================
# 在"干净"的机器上（比如 GitHub Actions 的 runner）把四个绿色版包建出来。
#
# 为什么要有它：本机往 GitHub 传大文件会被限速到几 MB/s，
# 而 asset 保存超过约 30 秒就会失败 —— 所以打包这一步交给 GitHub 自己的机器做，
# 打包好的 zip 就在 GitHub 网络里，上传快且稳。
#
# 用法：bash 构建/在服务器上构建.sh [--只 linux|windows] [--口味 完整|精简]
#
# 默认口径（与本地一致）：**只打"不含模型"的包**（--口味 精简），
# 包名不带口味后缀，例如 网盘管理-V1.0.1-Linux.zip。
#
# 变量名一律 ASCII（bash 不认中文变量名，会被当成命令执行）。
# ============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYVER="3.14.7"
PBS_TAG="20260901"                     # python-build-standalone 的版本标签
PBS_BASE="https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_TAG}"
INDEX="${PIP_INDEX_URL:-https://pypi.org/simple}"
MODEL_REPO="Systran/faster-whisper-small"
HF_ENDPOINT="${HF_ENDPOINT:-https://hf-mirror.com}"
export UV_CACHE_DIR="${UV_CACHE_DIR:-$ROOT/.uvcache}"

ONLY_PLATFORM="全部"
ONLY_TASTE="精简"        # 默认只打"不含模型"的包（口径见文件头）
while [ $# -gt 0 ]; do
  case "$1" in
    --只) ONLY_PLATFORM="${2:-全部}"; shift 2 ;;
    --口味) ONLY_TASTE="${2:-全部}"; shift 2 ;;
    *) shift ;;
  esac
done

log() { echo "[$(date +%H:%M:%S)] $*"; }

# ---------------------------------------------------------------- uv
if ! command -v uv >/dev/null 2>&1; then
  log "安装 uv …"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi
log "uv $(uv --version)"

# ------------------------------------------------- Linux 运行环境
build_linux() {
  log "== Linux 运行环境 =="
  if [ ! -d 运行环境/venv ]; then
    log "下载独立 CPython ${PYVER}（Linux）"
    UV_PYTHON_INSTALL_DIR="$ROOT/运行环境/python" uv python install "$PYVER"
    # uv 会装到 <dir>/cpython-xxx/ 下，拍平到 运行环境/python/
    VERDIR="$(find "$ROOT/运行环境/python" -maxdepth 1 -type d -name 'cpython-*' | head -1)"
    if [ -n "$VERDIR" ]; then
      shopt -s dotglob
      mv "$VERDIR"/* "$ROOT/运行环境/python/"
      shopt -u dotglob
      rmdir "$VERDIR"
    fi
    rm -f "$ROOT/运行环境/python/.lock" "$ROOT/运行环境/python/.gitignore"
    log "建主环境 venv"
    "$ROOT/运行环境/python/bin/python3.14" -m venv --without-pip "$ROOT/运行环境/venv"
    ln -sfn ../../python/bin/python3.14 "$ROOT/运行环境/venv/bin/python3.14"
    log "装主程序依赖"
    uv pip install --python "$ROOT/运行环境/venv/bin/python" \
      --index-url "$INDEX" -r 构建/依赖-主程序.txt
    log "建语音识别环境 venv"
    mkdir -p 运行环境/语音识别
    "$ROOT/运行环境/python/bin/python3.14" -m venv --without-pip "$ROOT/运行环境/语音识别/venv"
    ln -sfn ../../../python/bin/python3.14 "$ROOT/运行环境/语音识别/venv/bin/python3.14"
    uv pip install --python "$ROOT/运行环境/语音识别/venv/bin/python" \
      --index-url "$INDEX" -r 构建/依赖-语音识别.txt
  fi
  log "Linux 运行环境就绪：$(du -sh 运行环境 | cut -f1)"
}

# ------------------------------------------------- Windows 运行环境
build_windows() {
  log "== Windows 运行环境（交叉装配）=="
  if [ -d 构建/windows/python主 ]; then
    log "已存在，跳过"
    return
  fi
  mkdir -p 构建/windows
  cd 构建/windows
  log "下载独立 CPython ${PYVER}（Windows）"
  curl -sSL --retry 3 -o cpython-win.tar.gz \
    "${PBS_BASE}/cpython-${PYVER}%2B${PBS_TAG}-x86_64-pc-windows-msvc-install_only.tar.gz"
  rm -rf python主 语音识别
  tar xzf cpython-win.tar.gz
  mv python python主
  log "装主程序依赖（Windows 轮子）"
  uv pip install --python-platform windows --python-version 3.14 \
    --index-url "$INDEX" --target ./python主/Lib/site-packages \
    -r "$ROOT/构建/依赖-主程序-win.txt"
  log "装 oss2（--no-deps）与 crcmod 的纯 Python 实现"
  uv pip install --python-platform windows --python-version 3.14 \
    --index-url "$INDEX" --no-deps --target ./python主/Lib/site-packages oss2==2.19.1
  mkdir -p ./python主/Lib/site-packages/crcmod
  cp "$ROOT/构建/crcmod纯Python/"* ./python主/Lib/site-packages/crcmod/
  cp -r "$ROOT/构建/crcmod纯Python/crcmod-1.7.dist-info" ./python主/Lib/site-packages/ 2>/dev/null || true
  log "语音识别环境"
  mkdir -p 语音识别
  tar xzf cpython-win.tar.gz -C 语音识别
  mv 语音识别/python 语音识别/临时
  mv 语音识别/临时 语音识别/python
  uv pip install --python-platform windows --python-version 3.14 \
    --index-url "$INDEX" --target ./语音识别/python/Lib/site-packages \
    -r "$ROOT/构建/依赖-语音识别.txt"
  rm -f cpython-win.tar.gz
  cd "$ROOT"
  log "Windows 运行环境就绪：$(du -sh 构建/windows | cut -f1)"
}

# ------------------------------------------------- 语音模型
fetch_model() {
  log "== 下载 Whisper 模型（$MODEL_REPO）=="
  TARGET="运行环境/语音识别/模型/faster-whisper-small"
  if [ -f "$TARGET/model.bin" ]; then
    log "已存在，跳过"
    return
  fi
  mkdir -p "$TARGET"
  for F in model.bin config.json tokenizer.json vocabulary.txt; do
    log "  · $F"
    HF_ENDPOINT="$HF_ENDPOINT" curl -sSL --retry 3 -o "$TARGET/$F" \
      "$HF_ENDPOINT/$MODEL_REPO/resolve/main/$F" ||
      curl -sSL --retry 3 -o "$TARGET/$F" "https://huggingface.co/$MODEL_REPO/resolve/main/$F"
  done
  log "模型就绪：$(du -sh "$TARGET" | cut -f1)"
}

case "$ONLY_PLATFORM" in
  linux) build_linux ;;
  windows) build_windows ;;
  *) build_linux; build_windows ;;
esac
# 只有"完整版"才需要下语音模型权重；默认口径是精简版，这一步会跳过
if [ "$ONLY_TASTE" = "完整" ] || [ "$ONLY_TASTE" = "全部" ]; then
  fetch_model
fi

log "== 打 zip =="
python3 构建/打包.py --平台 "$ONLY_PLATFORM" --口味 "$ONLY_TASTE" || \
  "$ROOT/运行环境/venv/bin/python" 构建/打包.py --平台 "$ONLY_PLATFORM" --口味 "$ONLY_TASTE"

log "产物："
ls -la 构建/发布/ | tail -6
