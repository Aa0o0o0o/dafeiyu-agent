"""Bounded local ASR on a verified QQ quote; no network or audio files here."""
import asyncio
import json
import os
from pathlib import Path

MAX_AUDIO_BYTES = 12 * 1024 * 1024

async def transcribe_wav(raw, project_root):
    if not isinstance(raw, bytes) or not 44 <= len(raw) <= MAX_AUDIO_BYTES:
        raise ValueError('invalid audio size')
    if raw[:4] != b'RIFF' or raw[8:12] != b'WAVE':
        raise ValueError('not wav')
    root = Path(project_root)
    model = root / 'tools/voice/asr-models/small.pt'
    worker = root / 'tools/voice/local-asr/transcribe.py'
    python = root / 'tools/voice/index-env/Scripts/python.exe'
    if not model.is_file() or not worker.is_file() or not python.is_file():
        raise ValueError('local ASR unavailable')
    env = os.environ.copy()
    env.update({'HF_HUB_OFFLINE': '1', 'PYTHONUTF8': '1',
                'TEMP': str(root / '.tmp'), 'TMP': str(root / '.tmp')})
    process = await asyncio.create_subprocess_exec(str(python), str(worker), cwd=str(root),
               env=env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
               stderr=asyncio.subprocess.DEVNULL,
               creationflags=getattr(__import__('subprocess'), 'CREATE_NO_WINDOW', 0))
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(raw), 120)
    except BaseException:
        if process.returncode is None:
            process.kill()
            await process.wait()
        raise
    if process.returncode != 0 or len(stdout) > 24000:
        raise ValueError('local ASR failed')
    response = json.loads(stdout.decode('utf-8'))
    if not isinstance(response, dict) or not isinstance(response.get('text'), str):
        raise ValueError('invalid ASR result')
    response["source"] = "local_whisper"
    return response
