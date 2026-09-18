#!/usr/bin/env bash
# 磁盘体检 + 腾出工作区（**ASCII 变量名版**，随便哪个终端都能跑）
#
# 为什么有这一版：项目里另一份同名脚本用了中文变量名（项目根=...），在部分
# 终端/包装器（fish、以及某些把参数当命令的 wrapper）里会被当成命令执行，
# 于是出现 "项目根=...: 没有那个文件或目录"、"${V8_3_工作区:-...}: 错误的替换"
# 这种报错，脚本只打印不干活。这个版本只用 ASCII 变量名与 POSIX 语法。
#
# 用法：
#   bash 工具/腾出工作区.sh            # 只看不动
#   bash 工具/腾出工作区.sh --migrate  # 建工作区 + 清掉本项目的测试残留
set -u

HERE=$(cd -- "$(dirname -- "$0")" && pwd)
ROOT=$(cd -- "$HERE/.." && pwd)
WORK=${V83_WORK:-$HOME/v8_3_工作区}

echo "===== 1) 磁盘现状 ====="
df -h / /home /tmp 2>/dev/null

echo
echo "===== 2) /home 下最大的目录 top15（只看，不删）====="
du -xh --max-depth=2 "$HOME" 2>/dev/null | sort -rh | head -15

echo
echo "===== 3) 项目内部最大的目录 top10 ====="
du -xh --max-depth=2 "$ROOT" 2>/dev/null | sort -rh | head -10

echo
echo "===== 4) /tmp 里最大的目录 top15（配额就是被它顶满的）====="
du -xh --max-depth=1 /tmp 2>/dev/null | sort -rh | head -15

echo
if [ "${1:-}" != "--migrate" ]; then
  echo "（只看了没动。要建工作区并清理，加 --migrate 再跑一次）"
  exit 0
fi

echo "===== 5) 建工作区：$WORK ====="
mkdir -p "$WORK/临时" "$WORK/构建" "$WORK/模型" "$WORK/截图" "$WORK/日志"
ls -d "$WORK"/*/ 2>/dev/null

echo
echo "===== 6) 清掉本项目的测试残留（只删这些模式）====="
rm -rf /tmp/v8_3_* /tmp/ai_market_* /tmp/gy_qr_* /tmp/v83* /tmp/v8_3* 2>/dev/null
echo "  已清：/tmp/v8_3_* /tmp/ai_market_* /tmp/gy_qr_* /tmp/v83*"

echo
echo "===== 7) 建议：把重活的临时目录指到大盘 ====="
echo "  fish:   set -x TMPDIR $WORK/临时"
echo "  bash:   export TMPDIR=$WORK/临时"
echo "  （ollama 权重想放大盘就再设 OLLAMA_MODELS=$WORK/模型）"

echo
echo "===== 8) 清理后空间 ====="
df -h / /home /tmp 2>/dev/null
