@echo off
rem One-time setup: install the Python packages and add Start Menu + Desktop shortcuts.
cd /d "%~dp0"
set "PY=%LocalAppData%\Python\bin\python.exe"
if not exist "%PY%" set "PY=py"
"%PY%" -m pip install -r requirements.txt
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_shortcuts.ps1"
pause
