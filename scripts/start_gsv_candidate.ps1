$ErrorActionPreference='Stop'
$voiceRoot=Split-Path $PSScriptRoot -Parent
Set-Location -LiteralPath $voiceRoot
$env:TEMP=Join-Path $voiceRoot '.tmp'
$env:TMP=$env:TEMP
$env:HF_HUB_OFFLINE='1'
$env:PYTHONUTF8='1'
$env:PYTHONPATH=$voiceRoot
$env:MODELSCOPE_CACHE=Join-Path $voiceRoot 'tools\voice\modelscope-cache'
$env:HF_HOME=Join-Path $voiceRoot 'tools\voice\hf-cache'
$env:DAFEIYU_TTS_TOKEN= & (Join-Path $voiceRoot '.venv\Scripts\python.exe') -c "import json,win32crypt;from pathlib import Path;p=json.loads(Path('config/voice-secret.json').read_text());print(win32crypt.CryptUnprotectData(bytes.fromhex(p['dpapi']),None,None,None,0)[1].decode())"
& (Join-Path $voiceRoot 'tools\voice\gsv-env\Scripts\python.exe') -u (Join-Path $voiceRoot 'tts_service\gsv_candidate.py')

