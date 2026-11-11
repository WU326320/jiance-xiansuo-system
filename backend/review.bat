@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title 侵权线索监控 - 复核回写

rem 后端优先用 backend\.venv；不存在才回退 crawler\.venv / PATH。
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=%~dp0..\crawler\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

set "EXCEL=%~1"
if "%EXCEL%"=="" set /p "EXCEL=请输入线索 Excel 路径（可直接拖入）："
set "EXCEL=%EXCEL:"=%"
if "%EXCEL%"=="" (
  echo 未提供 Excel 路径，退出。
  pause
  exit /b 2
)

"%PY%" -m pipeline review import "%EXCEL%"
set "RC=%ERRORLEVEL%"
echo.
pause
exit /b %RC%
