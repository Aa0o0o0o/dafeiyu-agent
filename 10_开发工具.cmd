@echo off
chcp 65001 >nul
if "%~1"=="" (
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\devtools.ps1" check
  pause
) else (
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\devtools.ps1" %*
)
