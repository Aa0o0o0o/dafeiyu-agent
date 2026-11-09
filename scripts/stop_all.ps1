param([switch]$PreviewOnly)
$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath((Split-Path $PSScriptRoot -Parent)).TrimEnd('\')
$backend = Join-Path $root 'run.py'
$voiceScript = Join-Path $root 'tts_service\gsv_service.py'
$voiceRoot = (Join-Path $root 'tools\voice').TrimEnd('\') + '\'
$qqExe = Join-Path $root 'runtime\qq\QQ.exe'
$bootExe = Join-Path $root 'runtime\napcat\NapCatWinBootMain.exe'

function Get-ProjectTargets {
    $targets = foreach ($item in @(Get-CimInstance Win32_Process -ErrorAction Stop)) {
        $exe = [string]$item.ExecutablePath
        $line = [string]$item.CommandLine
        if (-not $exe) { continue }
        $kind = $null
        if ($item.Name -match '^python(?:w)?\.exe$' -and $line.IndexOf($backend,[StringComparison]::OrdinalIgnoreCase) -ge 0) {
            $kind = '聊天后端'
        } elseif ($item.Name -match '^python(?:w)?\.exe$' -and $exe.StartsWith($voiceRoot,[StringComparison]::OrdinalIgnoreCase) -and $line.IndexOf($voiceScript,[StringComparison]::OrdinalIgnoreCase) -ge 0) {
            $kind = '语音服务'
        } elseif ([string]::Equals($exe,$qqExe,[StringComparison]::OrdinalIgnoreCase)) {
            $kind = '专用QQ'
        } elseif ([string]::Equals($exe,$bootExe,[StringComparison]::OrdinalIgnoreCase) -and $line.IndexOf($qqExe,[StringComparison]::OrdinalIgnoreCase) -ge 0) {
            $kind = '专用QQ启动器'
        }
        if ($kind) { [pscustomobject]@{ Id=[int]$item.ProcessId; Kind=$kind; Executable=$exe; CommandLine=$line } }
    }
    return @($targets)
}

if ($PreviewOnly) {
    $targets = @(Get-ProjectTargets)
    if ($targets.Count -eq 0) { Write-Host '未发现项目进程。' }
    foreach ($item in $targets) { Write-Host ("将停止：{0} (PID {1})" -f $item.Kind,$item.Id) }
    Write-Host '预览模式：没有停止任何进程。'
    exit 0
}

$mutex = New-Object System.Threading.Mutex($false, 'Local\DafeiyuOneClickStart')
$locked = $false
$failed = $false
try {
    try { $locked = $mutex.WaitOne(0) } catch [System.Threading.AbandonedMutexException] { $locked = $true }
    if (-not $locked) { Write-Host '一键启动正在运行，请等它完成后再停止。'; exit 1 }
    $targets = @(Get-ProjectTargets)
    if (@($targets | Where-Object Kind -eq '聊天后端').Count -gt 0) {
        & (Join-Path $PSScriptRoot 'stop.ps1')
        $deadline = (Get-Date).AddSeconds(30)
        do {
            Start-Sleep -Seconds 1
            $remaining = @(Get-ProjectTargets | Where-Object Kind -eq '聊天后端')
        } while ($remaining.Count -gt 0 -and (Get-Date) -lt $deadline)
        if ($remaining.Count -gt 0) {
            Write-Warning '聊天后端仍在退出中；为保护未完成的工作，本次没有强制结束它。请稍后重试。'
            exit 1
        }
        Write-Host '聊天后端已停止。'
    } else { Write-Host '聊天后端未运行。' }

    foreach ($kind in @('语音服务','专用QQ','专用QQ启动器')) {
        foreach ($target in @(Get-ProjectTargets | Where-Object Kind -eq $kind)) {
            # Recheck the same executable and command line immediately before stopping.
            $current = Get-CimInstance Win32_Process -Filter ("ProcessId={0}" -f $target.Id) -ErrorAction SilentlyContinue
            if (-not $current -or $current.ExecutablePath -ne $target.Executable -or $current.CommandLine -ne $target.CommandLine) { continue }
            try {
                Stop-Process -Id $target.Id -ErrorAction Stop
                Write-Host ("已停止：{0} (PID {1})" -f $target.Kind,$target.Id)
            } catch {
                $failed = $true
                Write-Warning ("未能停止：{0} (PID {1})" -f $target.Kind,$target.Id)
            }
        }
    }
    if ($failed) { Write-Warning '部分服务未能停止，请稍后重试。'; exit 1 }
    Write-Host '停止请求已完成。重新启动可使用 00_一键启动大肥鱼.cmd。'
} finally {
    if ($locked) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}