"""One-shot local ASR; raw audio on stdin, result on stdout, no temporary audio files."""
import io
import json
import sys
from pathlib import Path

MAX_BYTES = 12 * 1024 * 1024
raw = sys.stdin.buffer.read(MAX_BYTES + 1)
if not 0 < len(raw) <= MAX_BYTES:
    raise SystemExit(2)
import numpy as np
import scipy.signal
import soundfile as sf
import torch
import whisper

with sf.SoundFile(io.BytesIO(raw)) as f:
    if f.samplerate < 8000 or f.samplerate > 96000 or f.channels not in (1, 2) or f.frames / f.samplerate > 120:
        raise SystemExit(2)
    audio = f.read(dtype='float32', always_2d=True).mean(axis=1)
    rate = f.samplerate
if rate != 16000:
    from math import gcd
    factor = gcd(rate, 16000)
    audio = scipy.signal.resample_poly(audio, 16000 // factor, rate // factor).astype(np.float32)
model_file = Path(__file__).resolve().parents[1] / 'asr-models' / 'small.pt'
if not model_file.is_file():
    raise SystemExit(3)
device = 'cuda' if torch.cuda.is_available() else 'cpu'
model = whisper.load_model(str(model_file), device=device)
output = model.transcribe(audio, language='zh', fp16=device == 'cuda', temperature=0,
                          condition_on_previous_text=False, verbose=None)
text = output.get('text', '').strip()
if not text or len(text) > 3000:
    raise SystemExit(4)
print(json.dumps({'text': text}, ensure_ascii=False))
