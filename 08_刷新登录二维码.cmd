@echo off
chcp 65001 >nul
cd /d "%~dp0"
".venv\Scripts\python.exe" "scripts\qq_login.py" --refresh
if not errorlevel 1 start "" "%~dp0runtime\qq-login.png"
pause
