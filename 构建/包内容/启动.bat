@echo off
rem ============================================================================
rem  Wangpan Manager - Windows launcher (double-click me)
rem
rem  100% ASCII on purpose. Two measured reasons:
rem    * cmd.exe decodes .bat bytes with the console code page, so a Chinese
rem      filename in the script never matches the real file
rem      (`if exist "CN-name"` reported NO while the file was right there);
rem    * Chinese in the middle of a command can be split into bogus commands.
rem  Therefore: no non-ASCII literal is written here - names come from the file
rem  system (`for /d` and wildcards do the matching for us) or are handed
rem  straight to the launcher EXE.
rem ============================================================================
chcp 65001 >nul
setlocal EnableExtensions
set "HERE=%~dp0"
if "%HERE:~-1%"=="\" set "HERE=%HERE:~0,-1%"
cd /d "%HERE%"

rem ---- 1) the shipped launcher EXE (knows where the bundled Python is) ----
set "EXE="
for %%F in ("%HERE%\*.exe") do if not defined EXE set "EXE=%%~fF"
if defined EXE (
  start "" "%EXE%"
  exit /b 0
)

rem ---- 2) fallback: bundled python + startup script, found without literals ----
rem  The bundled runtime folder has a non-ASCII name. Writing that name here as a
rem  literal does NOT work: cmd decodes the .bat bytes with the console code page,
rem  so the literal stops matching the real folder (measured under Wine: `dir`
rem  lists it while `if exist` answers NO, with and without `chcp 65001`). `for /d`
rem  takes the name from the file system instead, so this file stays 100% ASCII.
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
  echo [X] Bundled Python not found. Please extract the whole zip into a normal folder.
  pause
  exit /b 1
)
if not defined MAIN (
  echo [X] Startup script ^(*.py^) not found next to this file.
  pause
  exit /b 1
)

start "" "%PY%" "%MAIN%" %*
exit /b 0
