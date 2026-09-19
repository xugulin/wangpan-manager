#!/bin/sh
# =============================================================================
#  网盘管理 · Linux 启动器（绿色版）
# =============================================================================
#  双击本文件，或在终端里执行 ./启动.sh
#
#  它做的事（全都在包内，不碰系统）：
#    ① 切到本文件所在目录（包根）
#    ② 用包内自带的解释器 运行环境/venv/bin/python 启动 启动.py
#    ③ 找不到自带解释器时给出可读的提示，而不是一闪而过
#
#  ⚠️ 本文件刻意只用 ASCII 变量名 + POSIX 语法：某些桌面环境/终端会用
#     非 bash 的 shell 解析脚本，中文变量名会被当成命令执行。
# =============================================================================
set -u

HERE=$(cd -- "$(dirname -- "$0")" && pwd)
cd -- "$HERE" || exit 1

PY=""
for ID in "$HERE/运行环境/venv/bin/python3.14" \
          "$HERE/运行环境/venv/bin/python3" \
          "$HERE/运行环境/venv/bin/python" \
          "$HERE/运行环境/python/bin/python3.14" \
          "$HERE/运行环境/python/bin/python3"; do
  if [ -x "$ID" ]; then PY=$ID; break; fi
done
if [ -z "$PY" ]; then
  echo "[X] 没找到包内自带的解释器（运行环境/venv/bin/python）。"
  echo "    包可能解压不完整，请重新解压完整的压缩包。"
  printf "按回车关闭…"; read -r _ || true
  exit 1
fi

# ---- 让自带的 venv 在"解压到任何位置"都能用 ----
# venv 靠 pyvenv.cfg 里的 home/executable 找基础解释器与标准库；发布包里这些值
# 被抹成了占位符（不能带构建机路径 —— 那是隐私），所以在**启动时**用绝对路径补回来。
# （必须在 python 启动**之前**写：解释器一起来就读这个文件了。）
CFG="$HERE/运行环境/venv/pyvenv.cfg"
if [ -f "$CFG" ]; then
  BASE="$HERE/运行环境/python"
  if [ -x "$BASE/bin/python3.14" ]; then
    {
      echo "home = $BASE/bin"
      echo "include-system-site-packages = false"
      echo "version = 3.14.7"
      echo "executable = $BASE/bin/python3.14"
      echo "command = $BASE/bin/python3.14 -m venv --without-pip $HERE/运行环境/venv"
    } > "$CFG" 2>/dev/null || true
  fi
fi

# 同理：自带 Python 的 _sysconfigdata 里若有占位符，补成真实绝对路径
for SD in "$HERE"/运行环境/python/lib/python3.14/_sysconfigdata_*.py; do
  [ -f "$SD" ] || continue
  if grep -q "<项目根>" "$SD" 2>/dev/null; then
    sed -i "s|<项目根>|$HERE/运行环境/python|g" "$SD" 2>/dev/null || true
  fi
done

exec "$PY" "$HERE/启动.py" "$@"
