$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
Set-Content -LiteralPath (Join-Path $projectRoot 'runtime\stop.request') -Value 'stop' -Encoding ascii
Write-Host '已向本项目发送停止请求。不会结束其他 Python、Node 或 QQ 进程。'
