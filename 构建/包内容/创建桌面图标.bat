@echo off
rem ============================================================================
rem  Create a desktop shortcut for 网盘管理  (Windows)
rem
rem  IMPORTANT (this file is deliberately ASCII-only):
rem    cmd.exe reads .bat bytes in the console code page. Non-ASCII characters
rem    (Chinese) in a .bat therefore get mis-decoded into garbage and the line
rem    is split into bogus commands like:
rem        '...exe' 不是内部或外部命令，也不是可运行的程序
rem    That is exactly what users saw as "终端显示乱码也失败了".
rem    So: every command here is pure ASCII; the Chinese shortcut name is built
rem    by PowerShell from Unicode code points (no encoding involved).
rem ============================================================================
setlocal EnableExtensions
set "HERE=%~dp0"
if "%HERE:~-1%"=="\" set "HERE=%HERE:~0,-1%"

rem ---- pick the launcher that actually exists ----
set "TARGET="
if exist "%HERE%\启动.exe" set "TARGET=%HERE%\启动.exe"
if not defined TARGET if exist "%HERE%\启动.bat" set "TARGET=%HERE%\启动.bat"
if not defined TARGET if exist "%HERE%\启动.py" set "TARGET=%HERE%\启动.py"
if not defined TARGET (
  echo [X] Cannot find 启动.exe / 启动.bat / 启动.py in:
  echo     %HERE%
  echo     Please extract the whole zip into a normal folder first.
  pause
  exit /b 1
)

rem ---- WANPAN = 网盘管理 (built from code points, so no encoding issue) ----
set "NAME="
for /f "delims=" %%N in ('powershell -NoProfile -Command "[char]0x7F51+[char]0x76D8+[char]0x7BA1+[char]0x7406"') do set "NAME=%%N"
if not defined NAME set "NAME=WangpanManager"

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ws=New-Object -ComObject WScript.Shell;" ^
  "$lnk=$ws.CreateShortcut([Environment]::GetFolderPath('Desktop')+'\'+$env:NAME+'.lnk');" ^
  "$lnk.TargetPath=$env:TARGET;" ^
  "$lnk.WorkingDirectory=$env:HERE;" ^
  "$lnk.IconLocation=$env:TARGET+',0';" ^
  "$lnk.Description='Wangpan Manager - Baidu / Quark / Guangya';" ^
  "$lnk.Save()"

if errorlevel 1 (
  echo [!] Shortcut creation failed.
  echo     You can simply drag 启动.exe (or 启动.bat) onto the desktop instead.
) else (
  echo [OK] Desktop shortcut created.
)
pause
