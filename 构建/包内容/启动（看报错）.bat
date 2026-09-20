@echo off
rem ============================================================================
rem  Wangpan Manager - verbose launcher (keeps the console open so errors show)
rem  100% ASCII on purpose; see the plain launcher .bat for the explanation.
rem ============================================================================
chcp 65001 >nul
setlocal EnableExtensions
set "HERE=%~dp0"
if "%HERE:~-1%"=="\" set "HERE=%HERE:~0,-1%"
cd /d "%HERE%"

rem  The runtime folder name is non-ASCII; a literal would never match it (see the
rem  plain launcher for the measurement). `for /d` lets the file system fill in the
rem  real name, so no non-ASCII byte is needed in here.
set "PY="
for /d %%D in ("%HERE%\*") do (
  if not defined PY if exist "%%D\venv\Scripts\python.exe"  set "PY=%%D\venv\Scripts\python.exe"
  if not defined PY if exist "%%D\venv\Scripts\python3.exe" set "PY=%%D\venv\Scripts\python3.exe"
  if not defined PY if exist "%%D\python\python.exe"       set "PY=%%D\python\python.exe"
  if not defined PY if exist "%%D\python\python3.exe"      set "PY=%%D\python\python3.exe"
)
set "MAIN="
for %%F in ("%HERE%\*.py") do if not defined MAIN set "MAIN=%%~fF"

if not defined PY (
  echo [X] Bundled Python not found. Extract the whole zip into a normal folder first.
  pause
  exit /b 1
)
if not defined MAIN (
  echo [X] Startup script ^(*.py^) not found next to this file.
  pause
  exit /b 1
)

echo [i] interpreter : %PY%
echo [i] script      : %MAIN%
echo [i] the in-app log is written under the data folder next to this file.
echo.
"%PY%" "%MAIN%"
echo.
echo [i] program exited with code %ERRORLEVEL%
pause
