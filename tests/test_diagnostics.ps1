# Offline PowerShell health regression: synthetic state, no API, QQ or credentials.
$ErrorActionPreference = 'Stop'
$testRoot = Split-Path $PSScriptRoot -Parent
. (Join-Path $testRoot 'scripts\diagnose.ps1')
$fixtureRoot = Join-Path $testRoot ('.tmp\health-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path (Join-Path $fixtureRoot 'runtime') -Force | Out-Null
$now = Get-Date
$stateFile = Join-Path $fixtureRoot 'runtime\status.json'
@{pid=12345;ws_connected=$true;login_verified=$true} | ConvertTo-Json | Set-Content -LiteralPath $stateFile -Encoding UTF8
$script:fakeProcess = $null
$script:fakeListeners = @(
    [pscustomobject]@{LocalPort=6199;LocalAddress='127.0.0.1';OwningProcess=12345},
    [pscustomobject]@{LocalPort=6099;LocalAddress='127.0.0.1';OwningProcess=56789}
)
function Get-CimInstance { [CmdletBinding()]param($ClassName,$Filter) $script:fakeProcess }
function Get-NetTCPConnection { [CmdletBinding()]param($State) $script:fakeListeners }
function Assert-Health($condition, $label) { if (-not $condition) { throw $label }; Write-Output ('PASS: ' + $label) }
try {
    Assert-Health (-not (Get-DafeiyuHealth $fixtureRoot $now).ws_connected) 'dead process cannot use old connected status'
    $script:fakeProcess = [pscustomobject]@{CommandLine='unrelated.exe'}
    Assert-Health (-not (Get-DafeiyuHealth $fixtureRoot $now).backend_running) 'reused PID with unrelated command is rejected'
    $script:fakeProcess = [pscustomobject]@{CommandLine=('python.exe -u ' + (Join-Path $fixtureRoot 'run.py'))}
    (Get-Item -LiteralPath $stateFile).LastWriteTime = $now.AddMinutes(-2)
    Assert-Health (-not (Get-DafeiyuHealth $fixtureRoot $now).ws_connected) 'stale heartbeat is rejected'
    (Get-Item -LiteralPath $stateFile).LastWriteTime = $now
    Assert-Health ((Get-DafeiyuHealth $fixtureRoot $now).ws_connected) 'fresh verified process and listener accepted'
    $script:fakeListeners = @()
    Assert-Health (-not (Get-DafeiyuHealth $fixtureRoot $now).backend_running) 'missing listener is rejected'
} finally {
    $resolved = (Resolve-Path -LiteralPath $fixtureRoot).Path
    $allowed = (Resolve-Path -LiteralPath (Join-Path $testRoot '.tmp')).Path.TrimEnd('\') + '\'
    if (-not $resolved.StartsWith($allowed, [StringComparison]::OrdinalIgnoreCase)) { throw 'Fixture escaped temporary root' }
    if ((Get-Item -LiteralPath $resolved).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Fixture is a link' }
    Remove-Item -LiteralPath $resolved -Force -Recurse
}
