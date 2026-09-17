@echo off
chcp 65001 >nul
rem 在桌面创建「网盘管理」快捷方式（只写你自己的用户目录）
setlocal
set "HERE=%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ws = New-Object -ComObject WScript.Shell; $lnk = $ws.CreateShortcut([Environment]::GetFolderPath('Desktop') + '\网盘管理.lnk'); $lnk.TargetPath = '%HERE%启动.exe'; $lnk.WorkingDirectory = '%HERE%'; $lnk.IconLocation = '%HERE%启动.exe'; $lnk.Description = '绿色版网盘管家：百度/夸克/光鸭'; $lnk.Save()"
if errorlevel 1 (
  echo 创建失败，可以直接把「启动.exe」拖到桌面当快捷方式用。
) else (
  echo 已在桌面创建「网盘管理」快捷方式。
)
pause
