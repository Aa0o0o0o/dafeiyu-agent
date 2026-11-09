# Project-only process environment. Dot-source this file from developer launchers.
$ErrorActionPreference = 'Stop'
$devProjectRoot = Split-Path $PSScriptRoot -Parent
$devNodeRoot = Join-Path $devProjectRoot 'tools\node-v24.22.1-win-x64'
$devStateRoot = Join-Path $devProjectRoot 'tools\state'
New-Item -ItemType Directory -Force -Path (Join-Path $devProjectRoot '.tmp'),$devStateRoot,(Join-Path $devStateRoot 'config'),(Join-Path $devStateRoot 'appdata'),(Join-Path $devStateRoot 'localappdata') | Out-Null
$env:TEMP = Join-Path $devProjectRoot '.tmp'
$env:TMP = $env:TEMP
$env:PATH = $devNodeRoot + ';' + $env:PATH
$env:npm_config_cache = Join-Path $devProjectRoot '.tmp\npm-devtools-cache'
$env:npm_config_prefix = Join-Path $devProjectRoot 'tools\npm'
$env:npm_config_userconfig = Join-Path $devProjectRoot 'config\devtools.npmrc'
$env:XDG_CONFIG_HOME = Join-Path $devStateRoot 'config'
$env:XDG_CACHE_HOME = Join-Path $devProjectRoot '.tmp\devtools-cache'
$env:XDG_DATA_HOME = Join-Path $devStateRoot 'data'
$env:APPDATA = Join-Path $devStateRoot 'appdata'
$env:LOCALAPPDATA = Join-Path $devStateRoot 'localappdata'
$env:PROMPTFOO_CONFIG_DIR = Join-Path $devStateRoot 'promptfoo'
$env:PROMPTFOO_DISABLE_TELEMETRY = '1'
$env:PROMPTFOO_DISABLE_UPDATE = '1'
$env:OPENSPEC_TELEMETRY = '0'
$env:DO_NOT_TRACK = '1'
$env:LANGSMITH_TRACING = 'false'
$env:LANGCHAIN_TRACING_V2 = 'false'
$env:COVERAGE_FILE = Join-Path $devStateRoot '.coverage'
$env:UV_CACHE_DIR = Join-Path $devProjectRoot '.tmp\uv-cache'
$env:PYTHONUTF8 = '1'
$env:PYTHONPATH = $devProjectRoot
