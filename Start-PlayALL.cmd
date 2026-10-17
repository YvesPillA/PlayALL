@echo off
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
if exist "%~dp0PlayALL.exe" (
    start "" "%~dp0PlayALL.exe"
    exit /b 0
)
if exist "%~dp0.venv\Scripts\pythonw.exe" (
    start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0desktop_run.py"
    exit /b 0
)
echo Please build PlayALL.exe or install requirements.txt in .venv first.
pause
