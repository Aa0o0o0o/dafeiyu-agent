@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "TEMP=%cd%\.tmp"
set "TMP=%TEMP%"
".venv\Scripts\python.exe" -m unittest discover -s tests -v
pause
