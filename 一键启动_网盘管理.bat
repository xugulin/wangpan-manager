@echo off
chcp 65001 >nul
rem =====================================================================
rem  Wangpan-Manager one-click launcher for Windows (网盘管理 一键启动)
rem  用法：双击本文件。
rem  * 自动找项目目录（本文件所在目录下带 v8_3 子目录的那个）
rem  * 优先用项目自带解释器 运行环境\venv\Scripts\python.exe
rem  * 找不到就提示你改 V83_HOME
rem =====================================================================
setlocal enabledelayedexpansion
set "HERE=%~dp0"
if "%HERE:~-1%"=="\" set "HERE=%HERE:~0,-1%"
if not "%V83_HOME%"=="" set "PROJ=%V83_HOME%"

if "%PROJ%"=="" (
  rem 1) first: is the script itself in the project root?
  if exist "%HERE%\v8_3" if exist "%HERE%\启动.py" set "PROJ=%HERE%"
  rem 2) else scan subdirs (script placed outside the project)
  for /d %%D in ("%HERE%\*") do (
    if exist "%%D\v8_3" if exist "%%D\启动.py" set "PROJ=%%D"
  )
)
if "%PROJ%"=="" (
  echo [X] 没找到项目目录（需要含 v8_3 与 启动.py）。
  echo     可以设置 V83_HOME 指向项目，例如：
  echo       set V83_HOME=D:\path\to\项目
  pause
  exit /b 1
)

set "PY="
rem 用 if exist 逐个判断（比 for 里靠延迟展开更稳，路径含中文也不怕）
if exist "%PROJ%\运行环境\venv\Scripts\python.exe" set "PY=%PROJ%\运行环境\venv\Scripts\python.exe"
if "%PY%"=="" if exist "%PROJ%\运行环境\venv\Scripts\python3.exe" set "PY=%PROJ%\运行环境\venv\Scripts\python3.exe"
if "%PY%"=="" if exist "%PROJ%\运行环境\python\python.exe" set "PY=%PROJ%\运行环境\python\python.exe"
if "%PY%"=="" (
  echo [X] 找不到项目自带的 Python：%PROJ%\运行环境\venv\Scripts\python.exe
  pause
  exit /b 1
)

"%PY%" -c "import PySide6, httpx" 2>nul
if errorlevel 1 (
  echo [X] 解释器缺少依赖（需要 PySide6 与 httpx）：%PY%
  pause
  exit /b 1
)

if "%~1"=="--check" (
  echo [OK] 项目  ：%PROJ%
  echo [OK] 解释器：%PY%
  "%PY%" -c "import sys, PySide6, httpx; print('[OK] python :', sys.version.split()[0]); print('[OK] PySide6:', PySide6.__version__)"
  pause
  exit /b 0
)

echo [^>] 启动 网盘管理：%PROJ%
cd /d "%PROJ%"
"%PY%" "%PROJ%\启动.py" --日志级别 警告
if errorlevel 1 (
  echo.
  echo [!] 启动失败。看这两处：
  echo     · %PROJ%\数据\界面日志.txt
  echo     · 用命令行跑： "%PY%" "%PROJ%\启动.py" --调试
  pause
)
endlocal
