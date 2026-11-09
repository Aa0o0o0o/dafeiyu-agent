"""Authenticated loopback IndexTTS worker, separate interpreter and GPU dependency stack."""

import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import re
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.voice.media import validate_wav  # noqa: E402
from src.voice.delivery import STYLES  # noqa: E402


class Engine:
    def __init__(self):
        self.ready = False
        self.reason = "loading"
        self.model = None
        self.lock = threading.Lock()
        self.pending = threading.BoundedSemaphore(5)
        self.cancelled = set()
        self.profiles = {}

    def load(self):
        try:
            self.profiles = json.loads((ROOT / "voices/registry.json").read_text())[
                "profiles"
            ]
            source = ROOT / "tools/voice/index-tts"
            sys.path.insert(0, str(source))
            os.chdir(source)
            from indextts.infer_v2_5 import IndexTTS2

            with contextlib.redirect_stdout(io.StringIO()):
                self.model = IndexTTS2(
                    cfg_path=str(source / "checkpoints/config.yaml"),
                    model_dir=str(source / "checkpoints"),
                    device="cuda:0",
                    use_bf16=True,
                    use_cuda_kernel=False,
                    use_qwen_emo=False,
                )
            from tts_service.offload import enable_stage_offload

            enable_stage_offload(self.model)
            approved = next(
                (
                    key
                    for key, value in self.profiles.items()
                    if value.get("enabled") and value.get("approved")
                ),
                None,
            )
            if approved:
                warm_path = ROOT / "runtime/tts-work/warmup.wav"
                warm_path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    with contextlib.redirect_stdout(io.StringIO()):
                        self.model.infer(
                            spk_audio_prompt=str(self.reference(approved)),
                            text="你好。",
                            output_path=str(warm_path),
                            lang="ZH",
                            use_random=False,
                            duration_factor=1.0,
                            verbose=False,
                        )
                    validate_wav(warm_path.read_bytes(), 60)
                finally:
                    warm_path.unlink(missing_ok=True)
            self.ready = True
            self.reason = None
        except Exception as error:
            self.reason = "load_" + type(error).__name__

    def inference_options(self, style):
        if style not in STYLES:
            raise ValueError("unsupported_style")
        vectors = {
            "playful": [0.35, 0, 0, 0, 0, 0, 0.05, 0.1],
            "gentle": [0, 0, 0, 0, 0, 0.05, 0, 0.45],
            "curious": [0.08, 0, 0, 0, 0, 0, 0.18, 0.2],
            "serious": [0, 0, 0, 0, 0, 0, 0, 0.45],
        }
        return (
            {"emo_vector": vectors[style], "use_emo_text": False}
            if style in vectors
            else {}
        )

    def reference(self, voice, style="plain"):
        if style not in STYLES:
            raise ValueError("unsupported_style")
        p = self.profiles.get(voice)
        if not p or not p.get("enabled") or not p.get("approved"):
            raise ValueError("unsupported_voice")
        target = p.get("speech_styles", {}).get(style)
        if target:
            selected = self.profiles.get(target)
            if (
                not selected
                or not p.get("speaker_key")
                or selected.get("speaker_key") != p["speaker_key"]
            ):
                raise ValueError("style_speaker_mismatch")
            p = selected
        if not p or not p.get("enabled") or not p.get("approved"):
            raise ValueError("unsupported_voice")
        path = (ROOT / p["reference"]).resolve()
        if not path.is_relative_to((ROOT / "voices").resolve()) or path.is_symlink():
            raise ValueError("reference_path")
        if hashlib.sha256(path.read_bytes()).hexdigest() != p["reference_sha256"]:
            raise ValueError("reference_hash")
        return path


ENGINE = Engine()
TOKEN = os.environ.get("DAFEIYU_TTS_TOKEN", "")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def respond(self, code, data, audio=False):
        body = data if audio else json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "audio/wav" if audio else "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionError):
            pass

    def authorized(self):
        return bool(TOKEN) and secrets.compare_digest(
            self.headers.get("Authorization", ""), "Bearer " + TOKEN
        )

    def do_GET(self):
        if not self.authorized():
            return self.respond(401, {"reason": "unauthorized"})
        if self.path == "/healthz":
            return self.respond(
                200,
                {
                    "alive": True,
                    "ready": ENGINE.ready
                    and any(
                        p.get("approved") and p.get("enabled")
                        for p in ENGINE.profiles.values()
                    ),
                    "model_loaded": ENGINE.ready,
                    "reason": ENGINE.reason
                    if not ENGINE.ready
                    else (
                        None
                        if any(
                            p.get("approved") and p.get("enabled")
                            for p in ENGINE.profiles.values()
                        )
                        else "voice_approval_required"
                    ),
                },
            )
        if self.path == "/capabilities":
            return self.respond(
                200,
                {
                    "engine": "IndexTTS-2.5",
                    "language": ["ZH"],
                    "format": "wav",
                    "workers": 1,
                    "qwen_emotion": False,
                    "duration_factor": [0.95, 1.08],
                    "speech_styles": sorted(STYLES),
                    "emotion_control": "bounded_index_vector_no_extra_classifier",
                },
            )
        if self.path == "/voices":
            return self.respond(
                200,
                {
                    "voices": [
                        {
                            "id": k,
                            "revision": v.get("revision"),
                            "approved": v.get("approved", False),
                        }
                        for k, v in ENGINE.profiles.items()
                    ]
                },
            )
        return self.respond(404, {"reason": "not_found"})

    def do_POST(self):
        if not self.authorized():
            return self.respond(401, {"reason": "unauthorized"})
        if re.fullmatch(r"/internal/jobs/[a-f0-9]{32}/cancel", self.path):
            ENGINE.cancelled.add(self.path.split("/")[-2])
            return self.respond(200, {"cancelled": True})
        if self.path != "/v1/audio/speech":
            return self.respond(404, {"reason": "not_found"})
        if not ENGINE.ready:
            return self.respond(503, {"reason": "not_ready"})
        size = int(self.headers.get("Content-Length", "0"))
        if not 0 < size <= 32768:
            return self.respond(400, {"reason": "invalid_size"})
        try:
            args = json.loads(self.rfile.read(size))
            allowed = {
                "model",
                "voice",
                "input",
                "response_format",
                "language",
                "style",
                "duration_factor",
                "request_id",
            }
            if (
                set(args) - allowed
                or args.get("model") != "dafeiyu-index-tts"
                or args.get("response_format") != "wav"
                or args.get("language") != "ZH"
                or args.get("style") not in STYLES
            ):
                raise ValueError("unsupported_option")
            text = args["input"]
            job = args["request_id"]
            factor = args.get("duration_factor", 1.0)
            if (
                not isinstance(text, str)
                or not 1 <= len(text) <= 200
                or not re.fullmatch("[a-f0-9]{32}", job)
                or type(factor) not in (int, float)
                or not 0.95 <= factor <= 1.08
            ):
                raise ValueError("invalid_input")
            reference = ENGINE.reference(args["voice"], args["style"])
            inference_options = ENGINE.inference_options(args["style"])
        except Exception:
            return self.respond(400, {"reason": "invalid_request_or_voice"})
        if not ENGINE.pending.acquire(False):
            return self.respond(429, {"reason": "queue_full"})
        acquired = False
        path = ROOT / "runtime/tts-work" / f"{job}.wav"
        try:
            acquired = ENGINE.lock.acquire(timeout=5)
            if not acquired:
                return self.respond(429, {"reason": "queue_timeout"})
            if job in ENGINE.cancelled:
                return self.respond(409, {"reason": "cancelled"})
            path.parent.mkdir(parents=True, exist_ok=True)
            start = time.monotonic()
            with contextlib.redirect_stdout(io.StringIO()):
                ENGINE.model.infer(
                    spk_audio_prompt=str(reference),
                    text=text,
                    output_path=str(path),
                    lang="ZH",
                    duration_factor=factor,
                    use_random=False,
                    verbose=False,
                    **inference_options,
                )
            if job in ENGINE.cancelled or time.monotonic() - start > 90:
                return self.respond(408, {"reason": "expired"})
            raw = path.read_bytes()
            validate_wav(raw, 60)
            return self.respond(200, raw, True)
        except Exception as error:
            return self.respond(
                503,
                {
                    "reason": "oom"
                    if "OutOfMemory" in type(error).__name__
                    else "synthesis_failed"
                },
            )
        finally:
            path.unlink(missing_ok=True)
            ENGINE.cancelled.discard(job)
            if acquired:
                ENGINE.lock.release()
            ENGINE.pending.release()


if __name__ == "__main__":
    if len(TOKEN) < 32:
        raise SystemExit("TTS token is not configured")
    threading.Thread(target=ENGINE.load, daemon=True).start()
    ThreadingHTTPServer(("127.0.0.1", 6210), Handler).serve_forever()
