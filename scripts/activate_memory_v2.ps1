$ErrorActionPreference='Stop'
$taskRoot=Split-Path $PSScriptRoot -Parent
Set-Location -LiteralPath $taskRoot
$statePath=Join-Path $taskRoot 'runtime\status.json'
if (Test-Path -LiteralPath $statePath) {
    $taskState=Get-Content -LiteralPath $statePath -Raw -Encoding UTF8 | ConvertFrom-Json
    $taskProcess=Get-CimInstance Win32_Process -Filter "ProcessId = $($taskState.pid)"
    $expectedEntry=Join-Path $taskRoot 'run.py'
    if ($taskProcess) {
        if ($taskProcess.CommandLine -notlike "*$expectedEntry*") { throw 'Backend identity mismatch; no process was stopped.' }
        New-Item -ItemType File -Path (Join-Path $taskRoot 'runtime\stop.request') -Force | Out-Null
        for ($index=0; $index -lt 40 -and (Test-Path -LiteralPath $statePath); $index++) { Start-Sleep -Milliseconds 500 }
        if (Test-Path -LiteralPath $statePath) { throw 'Verified backend did not stop; migration not started.' }
    }
}
try {
    & (Join-Path $taskRoot '.venv\Scripts\python.exe') (Join-Path $taskRoot 'tools\memory_activate.py')
    if ($LASTEXITCODE -ne 0) { throw 'Migration failed; original database/config restored if installation began.' }
} finally {
    # The cutover helper restores V1 on installation failure. Resume the verified
    # project backend in either case so a failed migration does not leave it down.
    $taskListener=Get-NetTCPConnection -State Listen -LocalPort 6199 -ErrorAction SilentlyContinue
    if (-not $taskListener) {
        Start-Process -FilePath (Join-Path $taskRoot '.venv\Scripts\python.exe') -ArgumentList @('-u','-X','utf8',(Join-Path $taskRoot 'run.py')) -WorkingDirectory $taskRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $taskRoot 'runtime\backend-memory-v2.stdout.log') -RedirectStandardError (Join-Path $taskRoot 'runtime\backend-memory-v2.stderr.log') | Out-Null
    }
}
