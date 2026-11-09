$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
Set-Location -LiteralPath $projectRoot
Write-Host '正在启动大肥鱼聊天后端，仅监听 127.0.0.1:6199。'
Write-Host '本窗口保持打开；运行 04_诊断.cmd 可检查 QQ 连接，03 可有序停止。'
& (Join-Path $projectRoot '.venv\Scripts\python.exe') -u (Join-Path $projectRoot 'run.py')
exit $LASTEXITCODE
