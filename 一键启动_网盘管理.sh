#!/bin/sh
# =============================================================================
#  Wangpan-Manager one-click launcher  (网盘管理 一键启动)
# =============================================================================
#  这个脚本**刻意只用 ASCII 变量名与 POSIX 语法**：
#  某些终端/包装器会用非 bash 的 shell 解析脚本，中文变量名会被当成命令执行
#  （报 "候选: 不是有效的标识符" 之类），所以这里全用 HERE/PROJ/PY/ID 这类名字。
#
#  位置：/home/xgl/python/一键启动_网盘管理.sh
#  用法：
#    ./一键启动_网盘管理.sh                # 正常启动
#    ./一键启动_网盘管理.sh --调试          # 参数透传给 启动.py
#    ./一键启动_网盘管理.sh --check        # 只自检，不启动
#    V83_HOME=/别的/路径 ./一键启动_网盘管理.sh
#    V83_FORCE=1 ./一键启动_网盘管理.sh     # 已在运行也强制再开一个
# =============================================================================
set -u

HERE=$(cd -- "$(dirname -- "$0")" && pwd)
PROJ=${V83_HOME:-}

# 怎么认出"项目本体"：适配器目录里也有 启动.py（那是它自己的 GUI），
# 靠它会把项目找错（实测：找成了适配器目录，然后报"缺依赖"）。
# 所以认**项目本体的特征**：v8_3 包 + 项目自带解释器。
if [ -z "$PROJ" ]; then
  for ID in "$HERE"/*; do
    if [ -d "$ID/v8_3" ] && [ -f "$ID/启动.py" ]; then PROJ=$ID; break; fi
  done
fi
if [ -z "$PROJ" ] || [ ! -f "$PROJ/启动.py" ] || [ ! -d "$PROJ/v8_3" ]; then
  echo "[X] project not found (no 启动.py under $HERE)."
  echo "    set V83_HOME to the project dir, e.g."
  echo "      V83_HOME=/home/xgl/python/<project> $0"
  printf "Press Enter to close..."; read -r _ || true
  exit 1
fi
PROJ=$(cd -- "$PROJ" && pwd)
cd -- "$PROJ" || exit 1

# 找解释器。这里要求"解释器真的在项目自己的 运行环境 里"：
# 工作区里别的目录（例如适配器目录）也有 启动.py，它们的 运行环境/venv 常常是
# 指向别处的符号链接 —— 用它们会在错的项目里找依赖，然后报"缺依赖"。
# 所以用 readlink -f 解析真实路径，确认里面带着项目的 运行环境 段。
PY=""
for ID in "$PROJ/运行环境/venv/bin/python3.14" \
          "$PROJ/运行环境/venv/bin/python3" \
          "$PROJ/运行环境/venv/bin/python" \
          "$PROJ/运行环境/python/bin/python3.14" \
          "$PROJ/运行环境/python/bin/python3"; do
  [ -x "$ID" ] || continue
  REAL=$(readlink -f "$ID" 2>/dev/null || echo "$ID")
  case "$REAL" in
    */运行环境/venv/*|*/运行环境/python/*) PY=$ID; break ;;
  esac
done
if [ -z "$PY" ]; then PY=$(command -v python3 || true); fi
if [ -z "$PY" ]; then
  echo "[X] no python interpreter found (bundled or system)."
  printf "Press Enter to close..."; read -r _ || true
  exit 1
fi

if ! "$PY" -c 'import PySide6, httpx' 2>/dev/null; then
  echo "[X] interpreter lacks dependencies (need PySide6 and httpx): $PY"
  echo "    bundled runtime should be at: $PROJ/运行环境/venv"
  printf "Press Enter to close..."; read -r _ || true
  exit 1
fi

if [ "${1:-}" = "--check" ] || [ "${1:-}" = "--只检查" ]; then
  echo "[OK] project    : $PROJ"
  echo "[OK] interpreter: $PY"
  "$PY" -c 'import sys, PySide6, httpx; print("[OK] python  :", sys.version.split()[0]); print("[OK] PySide6 :", PySide6.__version__); print("[OK] httpx   :", httpx.__version__)'
  exit 0
fi

if [ "${V83_FORCE:-0}" != "1" ]; then
  PIDFILE="$PROJ/数据/.一键启动.pid"
  if [ -f "$PIDFILE" ]; then
    OLD=$(cat "$PIDFILE" 2>/dev/null || true)
    if [ -n "$OLD" ] && kill -0 "$OLD" 2>/dev/null; then
      echo "[i] already running (PID $OLD), bringing it to front..."
      if command -v wmctrl >/dev/null 2>&1; then
        wmctrl -a "网盘管理" 2>/dev/null || true
      elif command -v xdotool >/dev/null 2>&1; then
        xdotool search --name "网盘管理" windowactivate 2>/dev/null || true
      fi
      exit 0
    fi
  fi
fi

if [ "$#" -eq 0 ]; then
  set -- --日志级别 警告
fi

mkdir -p "$PROJ/数据" 2>/dev/null || true
echo "[>] starting 网盘管理 : $PROJ"
echo "    interpreter: $PY   (terminal log level: 警告; full log: 数据/界面日志.txt)"
"$PY" "$PROJ/启动.py" "$@" &
CHILD=$!
echo "$CHILD" > "$PROJ/数据/.一键启动.pid" 2>/dev/null || true
wait "$CHILD"
CODE=$?
rm -f "$PROJ/数据/.一键启动.pid" 2>/dev/null || true
if [ "$CODE" -ne 0 ]; then
  echo
  echo "[!] exited with code $CODE. If the UI did not appear, check:"
  echo "    - $PROJ/数据/界面日志.txt"
  echo "    - bash '$PROJ/启动.sh'   (launcher that prints errors)"
  printf "Press Enter to close..."; read -r _ || true
fi
exit "$CODE"
