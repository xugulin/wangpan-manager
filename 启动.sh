#!/usr/bin/env bash
# 网盘管理 V8_3 —— 自包含启动器
# ============================================================================
# 用法：
#     启动.sh                 # 图形界面（等价于 启动.py）
#     启动.sh --cli 状态       # 命令行（原样透传给 启动.py）
#     启动.sh --调试           # 终端打调试日志
#
# 这个脚本保证三件事：
#   1) 只用**项目内**的解释器与依赖：运行环境/python（独立 CPython）+ 运行环境/venv
#      （主环境）+ 运行环境/语音识别/venv（Whisper 环境）。
#      不碰系统 python、不碰 ~/.local、不碰任何别的项目。
#   2) 整个项目目录被搬动/改名后依旧可用：venv 的解释器是**相对**链接，跟着项目走；
#      pyvenv.cfg 里记的是绝对路径，搬动后这里顺手改写成当前位置。
#   3) 运行期要写的东西全部落在项目内：XDG 三个目录、pip 缓存、HuggingFace 缓存，
#      不往 ~/.config、~/.cache、~/.local 里丢东西。
#
# 注意：bash 的变量名只认 ASCII，所以下面变量名用英文，注释和提示保持中文。
# ============================================================================
set -euo pipefail

PROJ="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ENVROOT="$PROJ/运行环境"
PY="$ENVROOT/python/bin/python3.14"
PYBIN="$ENVROOT/python/bin"
MAINVENV="$ENVROOT/venv"
ASRVENV="$ENVROOT/语音识别/venv"
MAINPY="$MAINVENV/bin/python"

if [ ! -x "$PY" ]; then
    echo "启动失败：项目自带的解释器不存在或不可执行：$PY" >&2
    echo "（本项目应当完全自包含；这一份可能被删过，请重新同步整个项目目录）" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# 自愈：把某个 venv 的 pyvenv.cfg 与解释器链接改回当前路径
# ---------------------------------------------------------------------------
fix_venv() {
    local venv="$1"
    [ -d "$venv" ] || return 0

    # 解释器链接：统一改成指向项目内解释器的**相对**链接（跟着项目一起搬）
    if [ -L "$venv/bin/python3.14" ] || [ ! -e "$venv/bin/python3.14" ]; then
        local rel
        rel="$(realpath --relative-to="$venv/bin" "$PY" 2>/dev/null || true)"
        [ -n "$rel" ] && ln -sfn "$rel" "$venv/bin/python3.14"
    fi

    # pyvenv.cfg：home/executable 指向项目内解释器
    if [ -f "$venv/pyvenv.cfg" ] && ! grep -qxF "home = $PYBIN" "$venv/pyvenv.cfg"; then
        cat > "$venv/pyvenv.cfg" <<EOF
home = $PYBIN
include-system-site-packages = false
version = 3.14.7
executable = $PY
command = $PY -m venv --without-pip $venv
EOF
        echo "[启动] 已修正环境记录（项目被移动过）：$venv" >&2
    fi
}

fix_venv "$MAINVENV"
fix_venv "$ASRVENV"

if [ ! -x "$MAINPY" ]; then
    echo "启动失败：主环境解释器不可用：$MAINPY" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# 环境隔离：运行期所有可写目录都在项目内
# ---------------------------------------------------------------------------
export PYTHONNOUSERSITE=1                      # 不加载 ~/.local 下的用户级包
export XDG_CONFIG_HOME="$PROJ/数据/运行环境/config"
export XDG_DATA_HOME="$PROJ/数据/运行环境/data"
export XDG_CACHE_HOME="$PROJ/数据/运行环境/cache"
export PIP_CACHE_DIR="$PROJ/数据/运行环境/cache/pip"
export HF_HOME="${HF_HOME:-$ENVROOT/语音识别/缓存}"   # Whisper 模型缓存
mkdir -p "$XDG_CONFIG_HOME" "$XDG_DATA_HOME" "$XDG_CACHE_HOME" "$PIP_CACHE_DIR" 2>/dev/null || true

exec "$MAINPY" "$PROJ/启动.py" "$@"
