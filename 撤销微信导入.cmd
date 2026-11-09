@echo off
chcp 65001 >nul
cd /d "%~dp0"
"%~dp0.venv\Scripts\python.exe" -B -m tools.wechat_readonly_probe.revoke_local
pause
