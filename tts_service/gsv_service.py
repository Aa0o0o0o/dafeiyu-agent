"""User-authorized production v5 Turbo service; shared authenticated transport."""

import sys
import threading
from pathlib import Path
from http.server import ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tts_service import app  # noqa: E402
from tts_service.gsv_candidate import CandidateEngine  # noqa: E402


class ProductionEngine(CandidateEngine):
    registry_path = ROOT / "voices/registry.json"


class ProductionHandler(app.Handler):
    def do_GET(self):
        if self.path == "/capabilities":
            if not self.authorized():
                return self.respond(401, {"reason": "unauthorized"})
            return self.respond(
                200,
                {
                    "engine": "GPT-SoVITS-v5-Turbo",
                    "candidate_only": False,
                    "port": 6210,
                    "language": ["ZH"],
                    "format": "wav",
                    "speech_styles": [
                        "plain",
                        "playful",
                        "gentle",
                        "serious",
                        "curious",
                    ],
                    "emotion_control": "registered_same_speaker_reference_if_configured_otherwise_base",
                    "duration_factor": [0.95, 1.08],
                    "model_request_alias": "dafeiyu-index-tts",
                    "alias_notice": "Legacy local transport alias; actual engine is GPT-SoVITS-v5-Turbo",
                    "quality_status": "user-accepted Suzuran Mandarin; contextual reference and mild rate control",
                    "production_profile": "dafeiyu_suzuran_cn_v1",
                    "profile_revision": "suzuran-gsv-production-20261031",
                },
            )
        return super().do_GET()


if __name__ == "__main__":
    if len(app.TOKEN) < 32:
        raise SystemExit("TTS token is not configured")
    app.ENGINE = ProductionEngine()
    threading.Thread(target=app.ENGINE.load, daemon=True).start()
    ThreadingHTTPServer(("127.0.0.1", 6210), ProductionHandler).serve_forever()
