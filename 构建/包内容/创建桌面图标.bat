@echo off
rem ============================================================================
rem  网盘管理 · Windows: create a desktop shortcut
rem
rem  This .bat only *launches* 创建桌面图标.ps1 and stays ASCII-only.
rem
rem  Why so careful (both bugs were real and reproduced):
rem   1) cmd.exe decodes .bat bytes with the console code page. Chinese text in a
rem      .bat therefore becomes garbage and lines get split into bogus commands
rem      ("'...exe' is not recognized as an internal or external command") --
rem      that is the mojibake the user saw.
rem   2) cmd.exe compares `if exist "中文名"` byte-by-byte, so a filename with
rem      Chinese characters never matches even when the file is right there
rem      (verified under Wine). PowerShell is Unicode-native, so all name checks
rem      and the shortcut creation live in the .ps1 next to this file.
rem ============================================================================
chcp 65001 >nul
setlocal EnableExtensions
set "HERE=%~dp0"
if "%HERE:~-1%"=="\" set "HERE=%HERE:~0,-1%"

rem ---- 真正的活在 创建桌面图标.vbs 里（Unicode 原生，中文名/中文路径都不会乱码）----
where cscript >nul 2>&1
if errorlevel 1 (
  echo [!] cscript not found ^(Windows Script Host missing?^).
  echo     Please drag the launcher ^(.exe or .bat^) to the desktop manually.
  pause
  exit /b 1
)

rem ⚠️ 不要在这里写中文文件名：cmd 读 .bat 是按字节的，中文字面量传下去会变乱码
rem    （Wine 实测：cscript 报 "Can not find script file ...\<mojibake>"）。
rem    用通配符让系统自己把真实文件名填进来，就没有编码问题。
set "VBS="
for %%F in ("%HERE%\*.vbs") do if not defined VBS set "VBS=%%~fF"
if not defined VBS (
  echo [!] 创建桌面图标.vbs not found next to this file.
  pause
  exit /b 1
)
cscript //nologo "%VBS%"
if not errorlevel 1 (
  pause
  exit /b 0
)

echo.
echo [!] Shortcut was not created.
echo     Fallback: copying the launcher to your desktop...
set "LAUNCH="
for %%F in ("%HERE%\*.exe") do set "LAUNCH=%%~fF"
if not defined LAUNCH for %%F in ("%HERE%\*.bat") do set "LAUNCH=%%~fF"
for %%D in ("%USERPROFILE%\Desktop") do set "DESK=%%~fD"
if defined LAUNCH if defined DESK (
  copy /y "%LAUNCH%" "%DESK%\" >nul 2>&1
  if not errorlevel 1 (
    echo [OK] launcher copied to: %DESK%
    echo      ^(rename it / change icon with right-click -^> Properties^)
    pause
    exit /b 0
  )
)
echo [!] Could not create a shortcut. Please drag the launcher to the desktop.
pause
