$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSHOME 'Modules\Microsoft.PowerShell.Management\Microsoft.PowerShell.Management.psd1') -ErrorAction Stop
Import-Module (Join-Path $PSHOME 'Modules\Microsoft.PowerShell.Utility\Microsoft.PowerShell.Utility.psd1') -ErrorAction Stop
Import-Module CimCmdlets -ErrorAction Stop
$root = Split-Path $PSScriptRoot -Parent
$mutex = New-Object System.Threading.Mutex($false, 'Local\DafeiyuOneClickStart')
$locked = $false
try {
    try { $locked = $mutex.WaitOne(0) } catch [System.Threading.AbandonedMutexException] { $locked = $true }
    if (-not $locked) { Write-Host '一键启动正在运行，请稍候。'; exit 0 }
    $specs = @(
        @{ Name='专用QQ'; Port=6099; Script='start_napcat.ps1'; Entry='runtime\qq\QQ.exe'; Boot='runtime\napcat\NapCatWinBootMain.exe' },
        @{ Name='语音服务'; Port=6210; Script='start_voice.ps1'; Entry='tts_service\gsv_service.py' },
        @{ Name='聊天后端'; Port=6199; Script='start.ps1'; Entry='run.py' }
    )
    $processes = @(Get-CimInstance Win32_Process -ErrorAction Stop)
    $listeners = @([System.Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties().GetActiveTcpListeners())
    foreach ($spec in $specs) {
        $script = Join-Path $PSScriptRoot $spec.Script
        $entry = Join-Path $root $spec.Entry
        $running = @($processes | Where-Object {
            ($_.ExecutablePath -and [string]::Equals($_.ExecutablePath,$entry,[StringComparison]::OrdinalIgnoreCase)) -or
            ($_.CommandLine -and ($_.CommandLine.IndexOf($entry,[StringComparison]::OrdinalIgnoreCase) -ge 0 -or $_.CommandLine.IndexOf($script,[StringComparison]::OrdinalIgnoreCase) -ge 0)) -or
            ($spec.Boot -and $_.ExecutablePath -and [string]::Equals($_.ExecutablePath,(Join-Path $root $spec.Boot),[StringComparison]::OrdinalIgnoreCase))
        }).Count -gt 0
        if ($running) { Write-Host "$($spec.Name)：已在运行。"; continue }
        if (@($listeners | Where-Object { $_.Port -eq $spec.Port }).Count -gt 0) {
            Write-Warning "$($spec.Name)：端口 $($spec.Port) 已被其他进程占用，未重复启动。"
            continue
        }
        if (-not (Test-Path -LiteralPath $script)) { Write-Warning "$($spec.Name)：启动脚本不存在。"; continue }
        $quoted = '"' + $script + '"'
        Start-Process -FilePath (Join-Path $PSHOME 'powershell.exe') -ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-File',$quoted) -WorkingDirectory $root -WindowStyle Hidden | Out-Null
        Write-Host "$($spec.Name)：已发起启动。"
    }
    Start-Sleep -Seconds 15
    . (Join-Path $PSScriptRoot 'diagnose.ps1')
    $health = Get-DafeiyuHealth -ProjectRoot $root
    Write-Host $health.note
    if ($health.login_verified) { Write-Host '专用QQ登录已核验。' }
    else { Write-Host '如果专用QQ尚未登录，请双击 08_刷新登录二维码.cmd，用机器人QQ扫码确认。' }
    $voiceReady = @([System.Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties().GetActiveTcpListeners() | Where-Object { $_.Port -eq 6210 }).Count -gt 0
    if ($voiceReady) { Write-Host '语音服务端口已就绪；模型可能仍在预热。' }
    else { Write-Host '语音服务还在启动或启动失败，可稍后使用 04_诊断.cmd 检查。' }
} finally {
    if ($locked) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
