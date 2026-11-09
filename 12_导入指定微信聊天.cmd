@echo off
cd /d "%~dp0"
chcp 65001 >nul
set PYTHONUTF8=1
"%~dp0tools\wechat-sqlcipher-env\Scripts\python.exe" -m tools.wechat_readonly_probe.import_operator
pause
