"""Project-local GPT-SoVITS v5 Turbo; fixed model and transcript-backed references."""

from pathlib import Path
import os
import sys
import json
import hashlib


class GSVModel:
    def __init__(self, root, profiles):
        self.root, self.profiles = Path(root).resolve(), profiles
        source = self.root / "tools/voice/gpt-sovits"
        sys.path[:0] = [str(source), str(source / "GPT_SoVITS")]
        os.chdir(source)
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["CUDAGraph"] = "0"
        import jieba
        import jieba.posseg

        sys.modules["jieba_fast"] = jieba
        sys.modules["jieba_fast.posseg"] = jieba.posseg
        import imageio_ffmpeg
        from tools import portable_runtime

        portable_runtime.FFMPEG_EXE = Path(imageio_ffmpeg.get_ffmpeg_exe())
        from TTS_infer_pack.TTS import TTS, TTS_Config

        config = dict(TTS_Config.default_configs["v5turbo"])
        config.update(device="cuda", is_half=True)
        self.pipeline = TTS(TTS_Config({"custom": config}))
        if self.pipeline.configs.version != "v5turbo":
            raise RuntimeError("Wrong acoustic model")
        self.options = json.loads(
            (self.root / "config/gsv-inference.json").read_text(encoding="utf-8-sig")
        )

    def infer(self, spk_audio_prompt, text, output_path, duration_factor=1.0, **kwargs):
        import numpy as np
        import soundfile as sf

        profile = next(
            (
                p
                for p in self.profiles.values()
                if (self.root / p["reference"]).resolve()
                == Path(spk_audio_prompt).resolve()
            ),
            None,
        )
        if not profile or not profile.get("reference_text"):
            raise ValueError("Reference transcript required")
        from src.voice.media import validate_wav

        ref = Path(spk_audio_prompt).resolve()
        if (
            not ref.is_relative_to((self.root / "voices").resolve())
            or Path(spk_audio_prompt).is_symlink()
        ):
            raise ValueError("Reference must be inside the registered voices directory")
        raw = ref.read_bytes()
        audio_info = validate_wav(raw, 10)
        if audio_info["duration_ms"] < 3000:
            raise ValueError("Reference must be 3 to 10 seconds")
        if hashlib.sha256(raw).hexdigest() != profile.get("reference_sha256"):
            raise ValueError("Reference hash mismatch")
        prompt_language = {"zh": "all_zh", "ja": "all_ja", "en": "en"}.get(
            profile.get("reference_language", "zh")
        )
        if prompt_language is None:
            raise ValueError("Unsupported reference language")
        args = dict(
            text=text,
            text_lang="all_zh",
            ref_audio_path=str(spk_audio_prompt),
            prompt_text=profile["reference_text"],
            prompt_lang=prompt_language,
            top_k=self.options["top_k"],
            top_p=self.options["top_p"],
            temperature=self.options["temperature"],
            text_split_method="cut0" if len(text) <= 60 else "cut5",
            batch_size=1,
            speed_factor=1.0 / duration_factor,
            seed=self.options["seed"],
            parallel_infer=False,
            split_bucket=False,
            return_fragment=False,
            streaming_mode=False,
            sample_steps=self.options["sample_steps"],
            cfg_rate=0.0,
            repetition_penalty=1.35,
            super_sampling=False,
            use_cuda_graph=False,
            use_flash_attention=False,
        )
        chunks = list(self.pipeline.run(args))
        if not chunks or len({sr for sr, _ in chunks}) != 1:
            raise ValueError("Invalid synthesis output")
        data = np.concatenate([audio for _, audio in chunks])
        sf.write(output_path, data, chunks[0][0], subtype="PCM_16")
