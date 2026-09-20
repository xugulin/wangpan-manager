@echo off
rem ============================================================================
rem  网盘管理 · Windows 自检（双击运行）
rem
rem  真正的活都交给同目录的 自检-Windows.ps1 做 —— 原因（踩过的坑）：
rem    cmd.exe 是按「控制台代码页」把 .bat 当**字节**解析的，路径或文件名里只要
rem    有中文就可能变成乱码（实测：mkdir 出来的目录名会变成 GBK 乱码，
rem    如 自检报告 -> 鑷鎶ュ憡）。PowerShell 是 Unicode 原生的，中文名/中文路径
rem    都不会乱。所以 .bat 只做一件事：把参数原样转交给 .ps1。
rem
rem  本文件保持纯 ASCII（避免 cmd 解析歧义）。
rem ============================================================================
chcp 65001 >nul
setlocal EnableExtensions
set "HERE=%~dp0"
if "%HERE:~-1%"=="\" set "HERE=%HERE:~0,-1%"

where powershell >nul 2>&1
if errorlevel 1 (
  echo [X] Windows PowerShell not found. Cannot run the self check.
  echo     Please tell the author: "windows has no powershell".
  pause
  exit /b 1
)

rem 用通配符取真实文件名（不在 .bat 里写中文文件名，避免代码页问题）
set "PS1="
for %%F in ("%HERE%\*.ps1") do if not defined PS1 set "PS1=%%~fF"
if not defined PS1 (
  echo [X] self-check script ^(*.ps1^) not found next to this file.
  pause
  exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%"
set "CODE=%ERRORLEVEL%"
echo.
echo [i] self check finished, exit code %CODE%
echo     report folder should be next to this file.
pause
exit /b %CODE%
