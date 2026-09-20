@echo off
rem ============================================================================
rem  Wangpan Manager - Windows self check (double-click me)
rem
rem  Real work is in the self-check .ps1 next to this one: cmd.exe decodes .bat
rem  bytes with the console code page and mangles non-ASCII paths/filenames
rem  (measured: mkdir produced a GBK-garbled folder name), while PowerShell is
rem  Unicode-native.
rem
rem  This file is 100% ASCII on purpose - do not add non-ASCII text here.
rem ============================================================================
chcp 65001 >nul
setlocal EnableExtensions
set "HERE=%~dp0"
if "%HERE:~-1%"=="\" set "HERE=%HERE:~0,-1%"

where powershell >nul 2>&1
if errorlevel 1 (
  echo [X] Windows PowerShell not found. Cannot run the self check.
  echo     Please tell the author: this machine has no PowerShell.
  pause
  exit /b 1
)

rem locate the .ps1 by wildcard (never write its non-ASCII name here)
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
echo     the report folder should now be next to this file.
pause
exit /b %CODE%
