import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

try:
    import numpy as np
    import soundfile as sf
except ImportError:
    raise unittest.SkipTest(
        "Run candidate adapter tests in tools/voice/gsv-env; do not add TTS dependencies to chat runtime"
    )
from tts_service.gsv_engine import GSVModel
from src.voice.media import validate_wav

ROOT = Path(__file__).resolve().parents[1]


class GSVAdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT / ".tmp")
        self.root = Path(self.tmp.name)
        (self.root / "voices").mkdir()
        self.ref = self.root / "voices/ref.wav"
        sf.write(
            self.ref, 0.1 * np.sin(np.arange(24000 * 4) / 10), 24000, subtype="PCM_16"
        )
        self.profile = {
            "reference": "voices/ref.wav",
            "reference_text": "这是一句测试参考。",
            "reference_sha256": hashlib.sha256(self.ref.read_bytes()).hexdigest(),
        }
        self.calls = []

        def run(args):
            self.calls.append(args)
            yield 48000, (1000 * np.sin(np.arange(48000) / 10)).astype(np.int16)

        self.engine = GSVModel.__new__(GSVModel)
        self.engine.root = self.root
        self.engine.profiles = {"test": self.profile}
        self.engine.pipeline = SimpleNamespace(run=run)
        self.engine.options = dict(
            sample_steps=8, top_k=15, top_p=1.0, temperature=1.0, seed=242
        )
        self.out = self.root / "output.wav"

    def tearDown(self):
        self.tmp.cleanup()

    def test_exact_text_transcript_and_v5_parameters_produce_pcm(self):
        self.engine.infer(str(self.ref), "不要加糖，是25元。", str(self.out))
        info = validate_wav(self.out.read_bytes())
        self.assertEqual(info["sample_rate"], 48000)
        self.assertEqual(info["duration_ms"], 1000)
        args = self.calls[0]
        self.assertEqual(args["text"], "不要加糖，是25元。")
        self.assertEqual(args["prompt_text"], self.profile["reference_text"])
        self.assertEqual(args["sample_steps"], 8)
        self.assertEqual(args["cfg_rate"], 0.0)
        self.assertFalse(args["return_fragment"])

    def test_tampered_reference_rejected_before_inference(self):
        self.ref.write_bytes(self.ref.read_bytes() + b"x")
        with self.assertRaises(ValueError):
            self.engine.infer(str(self.ref), "你好", str(self.out))
        self.assertEqual(self.calls, [])

    def test_unregistered_path_or_missing_transcript_rejected(self):
        with self.assertRaises(ValueError):
            self.engine.infer(str(self.root / "foreign.wav"), "你好", str(self.out))
        self.profile["reference_text"] = ""
        with self.assertRaises(ValueError):
            self.engine.infer(str(self.ref), "你好", str(self.out))
        self.assertEqual(self.calls, [])

    def test_long_reference_rejected_before_inference(self):
        sf.write(
            self.ref, 0.1 * np.sin(np.arange(24000 * 11) / 10), 24000, subtype="PCM_16"
        )
        self.profile["reference_sha256"] = hashlib.sha256(
            self.ref.read_bytes()
        ).hexdigest()
        with self.assertRaises(ValueError):
            self.engine.infer(str(self.ref), "你好", str(self.out))
        self.assertEqual(self.calls, [])

    def test_japanese_reference_keeps_chinese_target(self):
        self.profile["reference_language"] = "ja"
        self.profile["reference_text"] = "これはテストです。"
        self.engine.infer(str(self.ref), "今天过得怎么样？", str(self.out))
        self.assertEqual(self.calls[0]["prompt_lang"], "all_ja")
        self.assertEqual(self.calls[0]["text_lang"], "all_zh")
        self.assertEqual(self.calls[0]["prompt_text"], "これはテストです。")

    def test_unknown_reference_language_rejected(self):
        self.profile["reference_language"] = "unknown"
        with self.assertRaises(ValueError):
            self.engine.infer(str(self.ref), "你好", str(self.out))
        self.assertEqual(self.calls, [])

    def test_short_chat_keeps_one_utterance_and_all_negations(self):
        line = "不要加糖，也不是少糖。今天25元，不是15元。"
        self.engine.infer(str(self.ref), line, str(self.out))
        self.assertEqual(self.calls[0]["text_split_method"], "cut0")
        self.assertEqual(self.calls[0]["text"], line)

    def test_long_explanation_keeps_complete_text_with_bounded_segmentation(self):
        line = "不要省略这条解释，也不要改变数字25。" * 6
        self.engine.infer(str(self.ref), line, str(self.out))
        self.assertEqual(self.calls[0]["text_split_method"], "cut5")
        self.assertEqual(self.calls[0]["text"], line)
