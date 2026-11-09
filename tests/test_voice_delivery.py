"""Speech delivery policy tests: no model, no private data, no GPU."""

import unittest
from unittest.mock import patch
import tempfile
import hashlib
from pathlib import Path
from src.voice.delivery import plan_delivery
from tts_service.app import Engine

ROOT = Path(__file__).resolve().parents[1]


class DeliveryTests(unittest.TestCase):
    def test_styles_and_serious_priority(self):
        self.assertEqual(
            plan_delivery("我在，慢慢说。", "今天好难过")["style"], "gentle"
        )
        self.assertEqual(plan_delivery("哈哈，太棒啦！")["style"], "playful")
        self.assertEqual(plan_delivery("后来呢，怎么想到这个的？")["style"], "curious")
        self.assertEqual(
            plan_delivery("哈哈是例子，药物剂量请咨询医生。", "认真解释药物剂量")[
                "style"
            ],
            "serious",
        )
        self.assertEqual(plan_delivery("今天天气晴。")["style"], "plain")

    def test_not_rewrite_or_exaggerate_and_smooth_rate(self):
        text = "不要加糖，是25元，不是15元。"
        for affect in ({}, {"energy": float("nan")}, {"energy": -999}, {"energy": 1}):
            p = plan_delivery(text, affect=affect, previous_factor=1.02)
            self.assertEqual(set(p), {"style", "duration_factor"})
            self.assertTrue(0.98 <= p["duration_factor"] <= 1.04)
            self.assertLessEqual(abs(p["duration_factor"] - 1.02), 0.021)
        self.assertEqual(plan_delivery("普通消息")["duration_factor"], 1.0)

    def test_index_emotion_is_real_and_mild(self):
        e = Engine()
        self.assertEqual(e.inference_options("plain"), {})
        p = e.inference_options("playful")
        self.assertEqual(len(p["emo_vector"]), 8)
        self.assertGreater(p["emo_vector"][0], 0)
        self.assertFalse(p["use_emo_text"])
        self.assertLessEqual(sum(p["emo_vector"]), 0.65)
        with self.assertRaises(ValueError):
            e.inference_options("evil")

    def test_reference_is_registered_same_character_and_hash_checked(self):
        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as td:
            root = Path(td)
            (root / "voices").mkdir()
            a = root / "voices/a.wav"
            b = root / "voices/b.wav"
            a.write_bytes(b"a")
            b.write_bytes(b"b")

            def profile(path):
                return {
                    "enabled": True,
                    "approved": True,
                    "reference": str(path.relative_to(root)),
                    "reference_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "speaker_key": "suzuran-cn",
                }

            e = Engine()
            e.profiles = {"base": profile(a), "happy": profile(b)}
            e.profiles["base"]["speech_styles"] = {"playful": "happy"}
            with patch("tts_service.app.ROOT", root):
                self.assertEqual(e.reference("base", "playful"), b)
                self.assertEqual(e.reference("base", "gentle"), a)
                e.profiles["happy"]["speaker_key"] = "someone-else"
                with self.assertRaises(ValueError):
                    e.reference("base", "playful")
                e.profiles["happy"]["speaker_key"] = "suzuran-cn"
                b.write_bytes(b"tampered")
                with self.assertRaises(ValueError):
                    e.reference("base", "playful")
                with self.assertRaises(ValueError):
                    e.reference("base", "unknown")


class SpeechTransportTests(unittest.TestCase):
    def test_style_reaches_inference_and_invalid_style_never_runs(self):
        import threading
        import io
        import wave
        import httpx
        from http.server import ThreadingHTTPServer
        from tts_service import app
        from unittest.mock import Mock

        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as td:
            root = Path(td)
            pcm = io.BytesIO()
            with wave.open(pcm, "wb") as w:
                w.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
                w.writeframes(b"\x01\x01" * 24000)
            calls = []

            def infer(**kw):
                calls.append(kw)
                Path(kw["output_path"]).write_bytes(pcm.getvalue())

            e = Engine()
            e.ready = True
            e.reference = Mock(return_value=root / "voices/ref.wav")
            from types import SimpleNamespace

            e.model = SimpleNamespace(infer=infer)
            server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            with (
                patch.object(app, "ENGINE", e),
                patch.object(app, "ROOT", root),
                patch.object(app, "TOKEN", "test-only"),
            ):
                thread.start()
                try:
                    with httpx.Client(
                        base_url=f"http://127.0.0.1:{server.server_port}",
                        trust_env=False,
                        headers={"Authorization": "Bearer test-only"},
                    ) as c:
                        body = dict(
                            model="dafeiyu-index-tts",
                            voice="test",
                            input="不要加糖，是25元，不是15元。",
                            response_format="wav",
                            language="ZH",
                            style="gentle",
                            duration_factor=1.03,
                            request_id="a" * 32,
                        )
                        self.assertEqual(
                            c.post("/v1/audio/speech", json=body).status_code, 200
                        )
                        self.assertEqual(calls[0]["text"], body["input"])
                        self.assertEqual(calls[0]["duration_factor"], 1.03)
                        self.assertEqual(
                            calls[0]["emo_vector"],
                            e.inference_options("gentle")["emo_vector"],
                        )
                        e.reference.assert_called_with("test", "gentle")
                        self.assertEqual(
                            c.post(
                                "/v1/audio/speech", json=dict(body, style="[crying]")
                            ).status_code,
                            400,
                        )
                        self.assertEqual(len(calls), 1)
                        # The HTTP response can arrive before the preceding
                        # handler finishes its cleanup; acquire its lock first
                        # so that cleanup cannot discard the new cancellation.
                        with e.lock:
                            e.cancelled.add("a" * 32)
                        self.assertEqual(
                            c.post("/v1/audio/speech", json=body).status_code, 409
                        )
                        self.assertEqual(len(calls), 1)
                finally:
                    server.shutdown()
                    thread.join()
                    server.server_close()
