$ErrorActionPreference = 'Stop'
Import-Module -Name (Join-Path $PSHOME 'Modules\Microsoft.PowerShell.Utility\Microsoft.PowerShell.Utility.psd1') -ErrorAction Stop
$projectRoot = Split-Path $PSScriptRoot -Parent
$napcatRoot = Join-Path $projectRoot 'runtime\napcat'
$qqRoot = Join-Path $projectRoot 'runtime\qq'
$qqVersion = '9.9.36-53644'
$dataRoot = Join-Path $projectRoot 'runtime\qq-data'
$profileRoot = Join-Path $projectRoot 'runtime\qq-profile'
$manifest = Get-Content -LiteralPath (Join-Path $projectRoot 'evidence\napcat-provision.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$entry = Join-Path $napcatRoot 'napcat.mjs'
if ((Get-FileHash -LiteralPath $entry -Algorithm SHA256).Hash.ToLowerInvariant() -ne $manifest.patched_entry_sha256) { throw 'NapCat patch checksum mismatch. Refusing to launch.' }
$resolvedProject = [IO.Path]::GetFullPath($projectRoot).TrimEnd('\') + '\'
foreach ($directory in @($qqRoot,$dataRoot,$profileRoot,$napcatRoot)) {
    if (-not [IO.Path]::GetFullPath($directory).StartsWith($resolvedProject,[StringComparison]::OrdinalIgnoreCase)) { throw 'Runtime directory outside project' }
}
New-Item -ItemType Directory -Force -Path $dataRoot,$profileRoot,(Join-Path $profileRoot 'AppData\Roaming'),(Join-Path $profileRoot 'AppData\Local') | Out-Null
$env:DAFEIYU_QQ_DATA_ROOT=$dataRoot
$env:NAPCAT_WORKDIR=$napcatRoot
$env:NAPCAT_DISABLE_MULTI_PROCESS='1'
$env:NAPCAT_DISABLE_PIPE='1'
$env:NAPCAT_PATCH_PACKAGE=Join-Path $napcatRoot 'qqnt.json'
$env:NAPCAT_LOAD_PATH=Join-Path $napcatRoot 'loadNapCat.js'
$env:NAPCAT_INJECT_PATH=Join-Path $napcatRoot 'NapCatWinBootHook.dll'
$env:NAPCAT_MAIN_PATH=$entry
$env:NAPCAT_WRAPPER_PATH=Join-Path $qqRoot "versions\$qqVersion\resources\app\wrapper.node"
$env:NAPCAT_QQ_PACKAGE_INFO_PATH=Join-Path $qqRoot "versions\$qqVersion\resources\app\package.json"
$env:NAPCAT_QQ_VERSION_CONFIG_PATH=Join-Path $qqRoot 'versions\config.json'
$env:APPDATA=Join-Path $profileRoot 'AppData\Roaming'
$env:LOCALAPPDATA=Join-Path $profileRoot 'AppData\Local'
$env:USERPROFILE=$profileRoot
$env:TEMP=Join-Path $projectRoot '.tmp'
$env:TMP=$env:TEMP
Set-Location -LiteralPath $napcatRoot
Write-Host '正在启动项目内 QQ 副本，专用号 1111111111。请仅使用该账号扫码。'
Write-Host '关闭本窗口前先按 Ctrl+C；不会结束常用 QQ。'
& (Join-Path $napcatRoot 'NapCatWinBootMain.exe') (Join-Path $qqRoot 'QQ.exe') $env:NAPCAT_INJECT_PATH '-q' '1111111111'
exit $LASTEXITCODE
