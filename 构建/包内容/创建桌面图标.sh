#!/usr/bin/env bash
# 创建「网盘管理」的桌面/开始菜单图标。
# 只写你自己的用户目录（~/.local/share/applications 与桌面），不碰系统。
set -eu
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
mkdir -p "$APPS"
条目="$APPS/wangpan-manager.desktop"
cat > "$条目" <<EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=网盘管理
Name[en]=Wangpan Manager
Comment=绿色版网盘管家：百度 / 夸克 / 光鸭，跨盘互传与 4K 播放
Exec=$HERE/启动.sh
Path=$HERE
Icon=$HERE/assets/icon.png
Terminal=false
StartupNotify=true
Categories=Network;FileTools;
Keywords=网盘;百度网盘;夸克;光鸭;wangpan;netdisk;
EOF
chmod +x "$条目"
# 顺带在桌面放一个（有桌面目录才放）
桌面="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/桌面")"
for 候选 in "$桌面" "$HOME/Desktop" "$HOME/桌面"; do
  if [ -d "$候选" ]; then
    cp "$条目" "$候选/网盘管理.desktop"
    chmod +x "$候选/网盘管理.desktop"
    echo "桌面图标：$候选/网盘管理.desktop"
    break
  fi
done
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$APPS" 2>/dev/null || true
echo "应用菜单图标：$条目"
echo "完成，去应用菜单/桌面找「网盘管理」即可。"
