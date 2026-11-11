@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title ��Ȩ������� - ��������̨ v1

rem ���� backend\.venv�������ڻ��� crawler\.venv���ٻ��� PATH��
set "PY=%~dp0backend\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=%~dp0crawler\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

echo ============================================================
echo  ��Ȩ������� - ��������̨ v1
echo  Python: %PY%
echo  Ĭ�Ϸ���: http://127.0.0.1:8765/
echo ============================================================
echo.

"%PY%" -B -X utf8 "%~dp0utils\start_project.py" %*
set "RC=%ERRORLEVEL%"
echo.
if not "%RC%"=="0" (
  echo ����ʧ�ܣ������� %RC%����鿴 workbench\data\logs\workbench.log
  pause
)
exit /b %RC%
