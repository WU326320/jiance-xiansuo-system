@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title 侵权线索监控 - 一键运行

rem 后端优先用 backend\.venv；不存在才回退 crawler\.venv / PATH。
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=%~dp0..\crawler\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

echo ============================================================
echo  侵权线索监控后端 - 一键运行
echo  Python: %PY%
echo ============================================================
echo.

set "IP="
set "KEYWORDS="
set /p "IP=请输入本次 IP 名（必填）："
set /p "KEYWORDS=请输入关键词（逗号分隔，留空默认用 IP 名）："

echo.
echo  将打开白名单文件：whitelist\%IP%.txt
echo  每行格式「平台,店铺名」，# 开头为注释；保存并关闭记事本后继续。
echo.

"%PY%" -m pipeline run-entry --interactive --ip "%IP%" --keywords "%KEYWORDS%" %*
set "RC=%ERRORLEVEL%"

echo.
if "%RC%"=="2" (
  echo 已有另一个运行实例，本次已退出。请等它结束后再试。
) else if "%RC%"=="0" (
  echo 运行结束。请查看告警，按需复核线索。
) else (
  echo 运行结束但有错误，请查看上方输出和 logs 目录。
)
echo.
pause
exit /b %RC%
