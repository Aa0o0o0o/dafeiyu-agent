"""Separate v5 Turbo candidate endpoint; production service/config remain unchanged."""

import contextlib
import io
import json
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tts_service import app  # noqa: E402
from tts_service.gsv_engine import GSVModel  # noqa: E402
from src.voice.media import validate_wav  # noqa: E402


class CandidateEngine(app.Engine):
    registry_path = ROOT / "voices/gsv-candidate-registry.json"

    def inference_options(self, style):
        # GSV uses registered reference selection, not Index emotion vectors.
        from src.voice.delivery import STYLES

        if style not in STYLES:
            raise ValueError("unsupported_style")
        return {}

    def load(self):
        try:
            self.profiles = json.loads(self.registry_path.read_text(encoding="utf-8"))[
                "profiles"
            ]
            with contextlib.redirect_stdout(io.StringIO()):
                self.model = GSVModel(ROOT, self.profiles)
                voice = next(iter(self.profiles))
                path = ROOT / "runtime/tts-work/gsv-candidate-warmup.wav"
                path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    self.model.infer(str(self.reference(voice)), "你好。", str(path))
                    validate_wav(path.read_bytes(), 60)
                finally:
                    path.unlink(missing_ok=True)
            self.ready = True
            self.reason = None
        except Exception as error:
            self.reason = "load_" + type(error).__name__


class CandidateHandler(app.Handler):
    def do_GET(self):
        if self.path == "/capabilities":
            if not self.authorized():
                return self.respond(401, {"reason": "unauthorized"})
            return self.respond(
                200,
                {
                    "engine": "GPT-SoVITS-v5-Turbo",
                    "candidate_only": True,
                    "port": 6211,
                    "language": ["ZH"],
                    "model_request_alias": "dafeiyu-index-tts",
                    "alias_notice": "Existing local transport alias only; not the actual engine identity",
                },
            )
        return super().do_GET()


if __name__ == "__main__":
    if len(app.TOKEN) < 32:
        raise SystemExit("TTS token is not configured")
    app.ENGINE = CandidateEngine()
    threading.Thread(target=app.ENGINE.load, daemon=True).start()
    ThreadingHTTPServer(("127.0.0.1", 6211), CandidateHandler).serve_forever()
