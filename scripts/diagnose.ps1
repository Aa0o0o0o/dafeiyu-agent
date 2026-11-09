param([switch]$Json)

function Get-DafeiyuHealth {
    [CmdletBinding()]
    param([string]$ProjectRoot, [datetime]$Now = (Get-Date))

    $statusPath = Join-Path $ProjectRoot 'runtime\status.json'
    $status = $null
    $fresh = $false
    $identity = $false
    $backendListen = $false
    $qqListen = $false
    if (Test-Path -LiteralPath $statusPath) {
        try {
            $status = Get-Content -LiteralPath $statusPath -Raw -Encoding UTF8 | ConvertFrom-Json
            $age = ($Now - (Get-Item -LiteralPath $statusPath).LastWriteTime).TotalSeconds
            $fresh = $age -ge -5 -and $age -le 15
            if (($status.pid -is [int] -or $status.pid -is [long]) -and $status.pid -gt 0) {
                $process = Get-CimInstance Win32_Process -Filter "ProcessId=$($status.pid)" -ErrorAction Stop
                $entry = [regex]::Escape((Join-Path $ProjectRoot 'run.py'))
                $identity = $null -ne $process -and $process.CommandLine -match $entry
            }
        } catch { $identity = $false }
    }
    try {
        $listeners = @(Get-NetTCPConnection -State Listen -ErrorAction Stop)
        $backendListen = $null -ne $status -and @($listeners | Where-Object { $_.LocalPort -eq 6199 -and $_.OwningProcess -eq $status.pid -and $_.LocalAddress -eq '127.0.0.1' }).Count -gt 0
        $qqListen = @($listeners | Where-Object { $_.LocalPort -eq 6099 -and $_.LocalAddress -eq '127.0.0.1' }).Count -gt 0
    } catch { }
    $alive = $identity -and $fresh -and $backendListen
    $connected = $alive -and $status.ws_connected
    $reason = if (-not $identity) { '后端未运行或进程身份无法核验；旧状态不代表在线。' }
        elseif (-not $fresh) { '状态更新已过期，不能判为在线。' }
        elseif (-not $backendListen) { '后端端口未就绪，不能判为在线。' }
        elseif (-not $connected) { '后端运行，QQ协议尚未连接。' }
        else { '后端与QQ协议已连接；真实收发以实际回执为准。' }
    [pscustomobject]@{
        checked_at = $Now.ToString('o')
        backend_running = [bool]$alive
        process_identity_verified = [bool]$identity
        status_fresh = [bool]$fresh
        backend_port_ready = [bool]$backendListen
        qq_webui_port_ready = [bool]$qqListen
        ws_connected = [bool]$connected
        login_verified = [bool]($connected -and $status.login_verified)
        pid = if ($alive) { $status.pid } else { $null }
        api_requests_reserved = if ($alive) { $status.api_requests_reserved } else { $null }
        budget_reserved_cny = if ($alive) { $status.budget_reserved_cny } else { $null }
        last_error = if ($alive) { $status.last_error } else { $null }
        memory_last_error = if ($alive) { $status.memory_last_error } else { $null }
        local_key_file_exists = Test-Path -LiteralPath (Join-Path $ProjectRoot 'config\local.json')
        note = $reason
    }
}

if ($MyInvocation.InvocationName -ne '.') {
    $projectRoot = Split-Path $PSScriptRoot -Parent
    Get-DafeiyuHealth -ProjectRoot $projectRoot | ConvertTo-Json -Depth 4
    if (-not $Json) {
        Write-Host '密钥文件存在不等于API可用；QQ端口存在不等于账号已登录。不要发送local.json或登录数据。'
    }
}
