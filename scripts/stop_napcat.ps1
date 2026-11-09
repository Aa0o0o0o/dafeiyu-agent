$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
$expectedExecutable = [IO.Path]::GetFullPath((Join-Path $projectRoot 'runtime\qq\QQ.exe'))
$targets = @(Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -eq $expectedExecutable })
if ($targets.Count -eq 0) { Write-Host '项目专用 QQ 当前未运行。'; exit 0 }
foreach ($target in $targets) {
    $process = Get-Process -Id $target.ProcessId -ErrorAction SilentlyContinue
    if ($null -eq $process) { continue }
    $closedWindow = $process.CloseMainWindow()
    if ($closedWindow) { $process.WaitForExit(5000) | Out-Null }
    if (-not $process.HasExited) {
        $current = Get-CimInstance Win32_Process -Filter "ProcessId=$($target.ProcessId)"
        if ($null -ne $current -and $current.ExecutablePath -eq $expectedExecutable) {
            Stop-Process -Id $target.ProcessId
            Write-Host "已结束本项目专用 QQ 进程 $($target.ProcessId)。下次可能需要重新扫码。"
        }
    }
}
Write-Host '未处理系统常用 QQ 或其他 Python/Node 进程。'
