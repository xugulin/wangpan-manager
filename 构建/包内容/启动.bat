@echo off
rem ============================================================================
rem  网盘管理 · Windows launcher (double-click me)
rem
rem  ASCII-only on purpose: cmd.exe decodes .bat bytes with the console code page,
rem  so Chinese text here would turn into garbage. See 创建桌面图标.bat for details.
rem ============================================================================
setlocal EnableExtensions
set "HERE=%~dp0"
if "%HERE:~-1%"=="\" set "HERE=%HERE:~0,-1%"
cd /d "%HERE%"

set "PY="
if exist "%HERE%\运行环境\venv\Scripts\pythonw.exe" set "PY=%HERE%\运行环境\venv\Scripts\pythonw.exe"
if not defined PY if exist "%HERE%\运行环境\venv\Scripts\python.exe" set "PY=%HERE%\运行环境\venv\Scripts\python.exe"
if not defined PY if exist "%HERE%\运行环境\python\pythonw.exe" set "PY=%HERE%\运行环境\python\pythonw.exe"
if not defined PY if exist "%HERE%\运行环境\python\python.exe" set "PY=%HERE%\运行环境\python\python.exe"
if not defined PY (
  echo [X] Bundled Python not found ^(运行环境\venv\Scripts\pythonw.exe^).
  echo     Please extract the whole zip into a normal folder and try again.
  pause
  exit /b 1
)

if not exist "%HERE%\数据" mkdir "%HERE%\数据" >nul 2>&1
>>"%HERE%\数据\一键启动.log" echo %date% %time% starting interpreter=%PY% args=%*
start "" "%PY%" "%HERE%\启动.py" %*
exit /b 0
