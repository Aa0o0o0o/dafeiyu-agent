"""Strict PCM-WAV validation and scope-local ephemeral assets."""

import hashlib
import io
import math
import struct
import wave


def validate_wav(raw, maximum=60):
    if not 44 <= len(raw) <= 20 * 1024 * 1024:
        raise ValueError("audio_size")
    with wave.open(io.BytesIO(raw), "rb") as w:
        channels, width, rate, count = (
            w.getnchannels(),
            w.getsampwidth(),
            w.getframerate(),
            w.getnframes(),
        )
        if (
            channels not in (1, 2)
            or width != 2
            or not 16000 <= rate <= 96000
            or w.getcomptype() != "NONE"
        ):
            raise ValueError("audio_format")
        frames = w.readframes(count)
    if len(frames) != count * channels * width or not 0.2 <= count / rate <= maximum:
        raise ValueError("audio_duration_or_truncated")
    samples = struct.unpack("<" + "h" * (len(frames) // 2), frames)
    rms = math.sqrt(sum(x * x for x in samples) / len(samples))
    if rms < 12 or sum(abs(x) >= 32760 for x in samples) / len(samples) > 0.01:
        raise ValueError("audio_silent_or_clipped")
    return {
        "duration_ms": round(count / rate * 1000),
        "sample_rate": rate,
        "channels": channels,
        "sha256": hashlib.sha256(raw).hexdigest(),
    }
