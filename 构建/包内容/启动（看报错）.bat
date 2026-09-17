@echo off
chcp 65001 >nul
rem 出问题时用这个启动：会保留一个黑窗口，把报错显示出来
cd /d "%~dp0"
set "PY=运行环境\venv\Scripts\python.exe"
if not exist "%PY%" set "PY=运行环境\python\python.exe"
if not exist "%PY%" (
  echo 找不到自带的 Python：请确认压缩包已完整解压。
  pause
  exit /b 1
)
echo 正在用 %PY% 启动，日志同时写入 数据\界面日志.txt …
echo ------------------------------------------------------------
"%PY%" "启动.py" %*
echo ------------------------------------------------------------
echo 程序已退出。把上面的报错截图发给作者：QQ 894597841
pause
