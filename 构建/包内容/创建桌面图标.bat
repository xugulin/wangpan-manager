@echo off
rem ============================================================================
rem  Wangpan Manager - create a desktop shortcut (double-click me)
rem
rem  Real work is in the .vbs file next to this one:
rem    * Windows ships cscript.exe since XP - no PowerShell, no execution policy;
rem    * that .vbs is saved as UTF-16LE, so cscript reads it as Unicode and the
rem      Chinese shortcut name / Chinese folder path never get mangled;
rem    * cmd's `if exist "<non-ascii>"` compares BYTES after decoding with the
rem      console code page, so such a name never matches (measured: the file was
rem      right there and cmd still said NO). Name checks therefore live in the .vbs.
rem
rem  This file is 100% ASCII on purpose - do not add non-ASCII text here.
rem ============================================================================
chcp 65001 >nul
setlocal EnableExtensions
set "HERE=%~dp0"
if "%HERE:~-1%"=="\" set "HERE=%HERE:~0,-1%"

rem locate the .vbs by wildcard (never write its non-ASCII name here)
set "VBS="
for %%F in ("%HERE%\*.vbs") do if not defined VBS set "VBS=%%~fF"
if not defined VBS (
  echo [X] helper script ^(*.vbs^) not found next to this file.
  echo     Please extract the whole zip again.
  pause
  exit /b 1
)

where cscript >nul 2>&1
if errorlevel 1 (
  echo [!] cscript not found ^(Windows Script Host missing?^).
  echo     Please drag the launcher ^(*.exe or *.bat^) to the desktop manually.
  pause
  exit /b 1
)

cscript //nologo "%VBS%"
if not errorlevel 1 (
  pause
  exit /b 0
)

echo.
echo [!] Shortcut was not created. Falling back to copying the launcher...
set "LAUNCH="
rem Look for the launcher: *.exe first, then *.bat -- but never this helper script
rem itself. Wine test showed the wildcard order is not guaranteed and it happily
rem copied this helper .bat to the desktop instead of the launcher.
for %%F in ("%HERE%\*.exe") do if not defined LAUNCH set "LAUNCH=%%~fF"
if not defined LAUNCH for %%F in ("%HERE%\*.bat") do (
  if not defined LAUNCH if /i not "%%~fF"=="%~f0" set "LAUNCH=%%~fF"
)
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
