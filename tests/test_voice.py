"""No real model/QQ: PCM validation, normalization, stale generation and delivery faults."""

import asyncio
import io
import json
import math
import struct
import unittest
import wave

import httpx
from unittest.mock import AsyncMock
from src.voice.media import validate_wav
from src.voice.normalize import spoken_text, voice_requested
from src.voice.coordinator import VoiceCoordinator
from tests import test_conversation as fixtures
from src.contracts import ModelResult


def wav():
    b = io.BytesIO()
    with wave.open(b, "wb") as w:
        w.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
        w.writeframes(
            b"".join(
                struct.pack("<h", int(1000 * math.sin(i / 10))) for i in range(24000)
            )
        )
    return b.getvalue()


class VoicePureTests(unittest.TestCase):
    def test_real_pcm_duration_and_invalid_truncation_silence(self):
        self.assertEqual(validate_wav(wav())["duration_ms"], 1000)
        for raw in (b"", b"not wav", wav()[:-10]):
            with self.assertRaises((ValueError, wave.Error, EOFError)):
                validate_wav(raw)
        with self.assertRaises(ValueError):
            validate_wav(wav(), 0.5)

    def test_normalizer_preserves_numbers_negation_parentheses(self):
        text = "**不要**加糖（不是少糖），25元，10%。"
        self.assertEqual(spoken_text(text), "不要加糖（不是少糖），25元，10%。")
        for bad in ("```python x```", "https://example.com", "密码：abc", "很长" * 110):
            with self.assertRaises(ValueError):
                spoken_text(bad)
        self.assertTrue(voice_requested("用语音说你好"))
        self.assertFalse(voice_requested("你好"))
        self.assertFalse(voice_requested("不要用语音，用文字回复"))


class VoiceConversationTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.ConversationTests.asyncSetUp
    asyncTearDown = fixtures.ConversationTests.asyncTearDown
    receive = fixtures.ConversationTests.receive

    async def setup_voice(self, handler=None, *, caption_default=True):
        voice = VoiceCoordinator(
            self.service,
            self.db.parent,
            {"mode": "on_demand", "caption_default": caption_default},
        )
        await voice.client.aclose()
        voice.headers = lambda: {"Authorization": "Bearer offline-only"}
        voice.client = httpx.AsyncClient(
            base_url="http://127.0.0.1:6210",
            transport=httpx.MockTransport(
                handler or (lambda r: httpx.Response(200, content=wav()))
            ),
        )
        self.service.voice = voice
        self.channel.send_record = AsyncMock(return_value="voice-receipt")
        return voice

    async def test_context_drives_delivery_without_extra_model_or_text_leak(self):
        requests = []

        def capture(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, content=wav())

        voice = await self.setup_voice(capture, caption_default=False)
        voice.options.update(mode="automatic", automatic_probability=1.0)
        self.model.complete = AsyncMock(return_value=ModelResult("我在，慢慢说。"))
        await self.receive(
            fixtures.event("我今天很难过，用语音陪我聊聊", "delivery-style")
        )
        await asyncio.wait_for(self.service.queue.join(), 3)
        self.assertEqual(requests[0]["style"], "gentle")
        self.assertEqual(requests[0]["input"], "我在，慢慢说。")
        self.assertGreater(requests[0]["duration_factor"], 1.0)
        self.assertEqual(self.model.complete.await_count, 1)
        self.assertEqual(self.channel.sent, [])
        metadata = self.service.store.db.execute(
            "SELECT data FROM voice_job_metadata"
        ).fetchone()[0]
        self.assertNotIn("难过", metadata)
        self.assertNotIn("慢慢说", metadata)
        self.assertEqual(json.loads(metadata)["delivery"]["style"], "gentle")

    async def test_retired_voice_does_not_override_rollout_or_reset_preferences(self):
        old = await self.setup_voice(caption_default=False)
        actor = self.service.peer_qq
        old.set_preferences(actor, enabled=False, caption=True)
        with self.service.store.db:
            self.service.store.db.execute(
                "INSERT OR REPLACE INTO voice_profiles VALUES(?,?)",
                (self.service.scope, "dafeiyu_gsv_cn_v1"),
            )
        await old.close()
        voice = VoiceCoordinator(
            self.service,
            self.db.parent,
            {
                "profile": "dafeiyu_suzuran_cn_v1",
                "allowed_profiles": ["dafeiyu_suzuran_cn_v1"],
                "mode": "automatic",
                "caption_default": False,
            },
        )
        self.service.voice = voice
        self.assertEqual(voice.options["profile"], "dafeiyu_suzuran_cn_v1")
        self.assertEqual(voice.preferences(actor), (0, 1))
        self.assertFalse(voice.should_speak("用语音回复", "我在。", actor))

    async def test_automatic_short_chat_and_explicit_preferences(self):
        voice = await self.setup_voice(caption_default=False)
        voice.options.update(
            mode="automatic", automatic_probability=0.45, automatic_max_chars=100
        )
        voice.random = lambda: 0.1
        actor = self.service.peer_qq
        self.assertTrue(voice.should_speak("你好呀", "我在，今天怎么样？", actor))
        voice.random = lambda: 0.9
        self.assertFalse(voice.should_speak("你好呀", "我在。", actor))
        self.assertTrue(voice.should_speak("用语音回复", "我在。", actor))
        voice.random = lambda: 0.1
        for incoming, answer in [
            ("不要用语音", "好的。"),
            ("只发文字", "好的。"),
            ("不方便听", "好的。"),
            ("详细分析这段代码", "这段代码有问题。"),
            ("你好", "很长" * 60),
            ("看这个", "https://example.com"),
            ("你好", "1. 第一项\n2. 第二项"),
        ]:
            self.assertFalse(voice.should_speak(incoming, answer, actor), incoming)
        voice.set_preferences(actor, enabled=False)
        self.assertFalse(voice.should_speak("用语音回复", "你好。", actor))
        voice.set_preferences(actor, enabled=True)
        voice.options["mode"] = "text"
        self.assertFalse(voice.should_speak("用语音回复", "你好。", actor))
        voice.options["mode"] = "on_demand"
        self.assertFalse(voice.should_speak("你好", "你好。", actor))

    async def test_automatic_reply_uses_one_voice_without_duplicate_text(self):
        voice = await self.setup_voice(caption_default=False)
        voice.options.update(mode="automatic", automatic_probability=0.45)
        voice.random = lambda: 0.1
        self.model.complete = AsyncMock(return_value=ModelResult("我在呢。"))
        await self.receive(fixtures.event("你好", "automatic"))
        await asyncio.wait_for(self.service.queue.join(), 3)
        self.channel.send_record.assert_awaited_once()
        self.assertEqual(self.channel.sent, [])
        self.assertEqual(len(self.service.store.history()), 2)
        self.assertEqual(self.model.complete.await_count, 1)

    async def test_delivered_voice_quote_uses_verified_text_and_survives_restart_via_digest(self):
        voice = await self.setup_voice(caption_default=False)
        voice.options.update(mode="automatic", automatic_probability=1.0)
        self.model.complete = AsyncMock(return_value=ModelResult("今天天气很好。"))
        await self.receive(fixtures.event("你好", "voice-quote"))
        await asyncio.wait_for(self.service.queue.join(), 3)
        self.assertEqual(voice.known_quote("voice-receipt"), "今天天气很好。")
        voice.quote_cache.clear()
        self.assertEqual(voice.known_quote("voice-receipt"), "今天天气很好。")
        self.assertIsNone(voice.known_quote("another-receipt"))
        voice.clear_quote_references()
        self.assertIsNone(voice.known_quote("voice-receipt"))

    async def test_voice_no_caption_preserves_contextual_expression(self):
        voice = await self.setup_voice(caption_default=False)
        asset = await voice.prepare("mix", "好呀。", self.service.peer_qq, lambda: True)
        self.channel.send_expression = AsyncMock(return_value="expression-receipt")
        await self.service.reply(
            "mix",
            "好呀。",
            voice_asset=asset,
            reply_parts=[("text", "好呀。"), ("face", "21")],
        )
        self.channel.send_record.assert_awaited_once()
        self.channel.send_expression.assert_awaited_once_with("face", "21")
        self.assertEqual(self.channel.sent, [])

    async def test_automatic_failure_keeps_original_text(self):
        voice = await self.setup_voice(
            lambda r: httpx.Response(503), caption_default=False
        )
        voice.options.update(mode="automatic", automatic_probability=1.0)
        self.model.complete = AsyncMock(return_value=ModelResult("别急，慢慢说。"))
        await self.receive(fixtures.event("你好", "auto-failure"))
        await asyncio.wait_for(self.service.queue.join(), 3)
        self.channel.send_record.assert_not_awaited()
        self.assertEqual(self.channel.sent, ["别急，慢慢说。"])

    async def test_model_inline_face_with_automatic_voice_delivers_both_without_leak(
        self,
    ):
        from src.stickers import StickerLibrary

        lib = StickerLibrary(self.db.parent / "stickers")
        self.service.sticker_library = lib
        voice = await self.setup_voice(caption_default=False)
        voice.options.update(mode="automatic", automatic_probability=1)
        self.channel.send_expression = AsyncMock(return_value="face-receipt")
        self.model.complete = AsyncMock(
            return_value=ModelResult("卖萌也没用，摸腿那笔账还挂着呢[[face:97]]")
        )
        try:
            await self.receive(fixtures.event("嘿嘿", "inline-voice"))
            await asyncio.wait_for(self.service.queue.join(), 3)
            self.channel.send_record.assert_awaited_once()
            self.channel.send_expression.assert_awaited_once_with("face", "97")
            self.assertEqual(self.channel.sent, [])
            self.assertNotIn("[[face:", str(self.service.store.history()))
        finally:
            await lib.close()

    async def test_disabled_library_still_does_not_leak_control_markers(self):
        self.channel.send_expression = AsyncMock(return_value="must-not-send")
        self.model.complete = AsyncMock(return_value=ModelResult("好呀[[face:97]]"))
        await self.receive(fixtures.event("你好", "disabled-control"))
        await asyncio.wait_for(self.service.queue.join(), 3)
        self.assertEqual(self.channel.sent, ["好呀"])
        self.channel.send_expression.assert_not_awaited()

    async def test_natural_voice_has_display_pause_before_record(self):
        voice = await self.setup_voice(caption_default=False)
        asset = await voice.prepare(
            "pace", "我在呢。", self.service.peer_qq, lambda: True
        )
        ordered = []

        async def sleep(seconds):
            self.assertGreaterEqual(seconds, 1.1)
            ordered.append("pause")

        async def record(asset):
            ordered.append("voice")
            return "voice-receipt"

        self.service.config["typing_delay_enabled"] = True
        self.service.sleep = sleep
        self.channel.send_record = record
        await self.service.reply(
            "pace", "我在呢。", voice_asset=asset, conversational=True
        )
        self.assertEqual(ordered, ["pause", "voice"])

    async def test_final_text_only_sent_to_tts_and_one_memory_reply(self):
        requests = []

        def handler(r):
            requests.append(json.loads(r.content))
            return httpx.Response(200, content=wav())

        await self.setup_voice(handler)
        self.model.complete = AsyncMock(return_value=ModelResult("我在呀，慢慢说。"))
        await self.receive(fixtures.event("用语音回复我", "voice"))
        await asyncio.wait_for(self.service.queue.join(), 3)
        self.assertEqual(requests[0]["input"], "我在呀，慢慢说。")
        self.assertNotIn("messages", requests[0])
        self.channel.send_record.assert_awaited_once()
        self.assertEqual(self.channel.sent, ["我在呀，慢慢说。"])
        self.assertEqual(len(self.service.store.history()), 2)
        self.assertEqual(self.model.complete.await_count, 1)

    async def test_not_ready_falls_back_original_text_once(self):
        await self.setup_voice(
            lambda r: httpx.Response(503, json={"reason": "not_ready"})
        )
        await self.receive(fixtures.event("发一条语音", "offline"))
        await asyncio.wait_for(self.service.queue.join(), 3)
        self.channel.send_record.assert_not_awaited()
        self.assertEqual(len(self.channel.sent), 1)

    async def test_stale_result_never_becomes_deliverable(self):
        voice = await self.setup_voice()
        calls = iter([True, False])
        with self.assertRaises(asyncio.CancelledError):
            await voice.prepare(
                "old", "合成测试", self.service.peer_qq, lambda: next(calls)
            )
        self.assertEqual(list(voice.directory.glob("*.wav")), [])

    async def test_unknown_record_result_never_retries_or_falls_back(self):
        await self.setup_voice()
        self.channel.send_record.side_effect = TimeoutError()
        await self.receive(fixtures.event("用语音回复", "unknown"))
        await asyncio.wait_for(self.service.queue.join(), 3)
        self.channel.send_record.assert_awaited_once()
        self.assertEqual(self.channel.sent, [])

    async def test_audio_success_caption_failure_does_not_resend_voice(self):
        await self.setup_voice()
        self.channel.fail = True
        await self.receive(fixtures.event("用语音回复", "partial"))
        await asyncio.wait_for(self.service.queue.join(), 3)
        self.channel.send_record.assert_awaited_once()
        row = self.service.store.db.execute(
            "select voice_receipt from voice_jobs"
        ).fetchone()
        self.assertEqual(row[0], "voice-receipt")

    async def test_optout_and_invalidation_remove_scope_media(self):
        voice = await self.setup_voice()
        asset = await voice.prepare(
            "test", "测试语音", self.service.peer_qq, lambda: True
        )
        self.assertTrue(asset)
        deadline, created = voice.db.execute(
            "SELECT deadline,created FROM voice_jobs WHERE id=?", (asset["job"],)
        ).fetchone()
        self.assertEqual(deadline - created, 120)
        voice.invalidate()
        self.assertEqual(list(voice.directory.glob("*.wav")), [])
        voice.set_preferences(self.service.peer_qq, enabled=False)
        self.assertIsNone(
            await voice.prepare("test2", "不发", self.service.peer_qq, lambda: True)
        )

    async def test_caption_off_still_persists_one_logical_reply(self):
        voice = await self.setup_voice(caption_default=False)
        self.assertEqual(voice.preferences(self.service.peer_qq), (1, 0))
        await self.receive(fixtures.event("用语音说", "only"))
        await asyncio.wait_for(self.service.queue.join(), 3)
        self.channel.send_record.assert_awaited_once()
        self.assertEqual(self.channel.sent, [])
        self.assertEqual(len(self.service.store.history()), 2)


from tests import test_open_contacts as contacts  # noqa: E402


class VoiceWireTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = contacts.AccessTests.asyncSetUp
    asyncTearDown = contacts.AccessTests.asyncTearDown

    async def test_voice_quote_calls_qq_transcription_only_after_scope_check(self):
        peer = await self.adapter.target_for(
            fixtures.event(user_id=int(contacts.A), sender={"user_id": int(contacts.A)})
        )
        raw = {
            "message_id": -17, "message_type": "private", "group_id": None,
            "sender": {"user_id": int(contacts.A)},
            "message": [{"type": "record", "data": {"file": "synthetic"}}],
        }
        async def action(name, **kwargs):
            if name == "get_msg":
                return raw
            if name == "fetch_ptt_text":
                self.assertEqual(kwargs["message_id"], -17)
                return {"text": "合成转写"}
            raise AssertionError(name)
        self.adapter.bot.call_action = AsyncMock(side_effect=action)
        quote = await peer.channel.fetch_quoted("-17")
        self.assertIn("合成转写", quote["text"])
        self.assertEqual([x.args[0] for x in self.adapter.bot.call_action.await_args_list],
                         ["get_msg", "fetch_ptt_text"])
        other = await self.adapter.target_for(
            fixtures.event(user_id=int(contacts.B), sender={"user_id": int(contacts.B)})
        )
        self.adapter.bot.call_action.reset_mock()
        from src.reply_context import QuoteError
        with self.assertRaises(QuoteError):
            await other.channel.fetch_quoted("-17")
        self.assertEqual([x.args[0] for x in self.adapter.bot.call_action.await_args_list], ["get_msg"])

    async def test_voice_quote_uses_local_fallback_after_qq_transcription_failure(self):
        import base64
        from unittest.mock import patch
        peer = await self.adapter.target_for(
            fixtures.event(user_id=int(contacts.A), sender={"user_id": int(contacts.A)})
        )
        model = self.root / "tools/voice/asr-models/small.pt"
        model.parent.mkdir(parents=True)
        model.write_bytes(b"synthetic-model")
        raw = {"message_id": -17, "message_type": "private", "group_id": None,
               "sender": {"user_id": int(contacts.A)},
               "message": [{"type": "record", "data": {"file": "qq-token-123"}}]}
        async def action(name, **kwargs):
            if name == "get_msg": return raw
            if name == "fetch_ptt_text": raise RuntimeError("unavailable")
            if name == "get_record":
                self.assertEqual(kwargs["file"], "qq-token-123")
                return {"base64": base64.b64encode(wav()).decode()}
            raise AssertionError(name)
        self.adapter.bot.call_action = AsyncMock(side_effect=action)
        with patch("src.voice.local_asr.transcribe_wav", new=AsyncMock(return_value={"text": "本机识别", "source": "local_whisper"})):
            result = await peer.channel.fetch_quoted("-17")
        self.assertIn("本机识别", result["text"])
        self.assertIn("local_whisper", result["text_source"])
        self.assertEqual([c.args[0] for c in self.adapter.bot.call_action.await_args_list],
                         ["get_msg", "fetch_ptt_text", "get_record"])

    async def test_record_segment_is_fixed_to_verified_peer_and_rejects_other_scope(
        self,
    ):
        peer = await self.adapter.target_for(
            fixtures.event(user_id=int(contacts.A), sender={"user_id": int(contacts.A)})
        )
        voice = VoiceCoordinator(peer, self.root, {"mode": "on_demand"})
        peer.voice = voice
        await voice.client.aclose()
        voice.headers = lambda: {}
        voice.client = httpx.AsyncClient(
            base_url="http://127.0.0.1:6210",
            transport=httpx.MockTransport(lambda r: httpx.Response(200, content=wav())),
        )
        asset = await voice.prepare("wire", "测试语音", contacts.A, lambda: True)
        self.adapter.bot.call_action = AsyncMock(return_value={"message_id": 125})
        receipt = await peer.channel.send_record(asset)
        self.assertEqual(receipt, "125")
        call = self.adapter.bot.call_action.await_args
        self.assertEqual(call.args, ("send_private_msg",))
        self.assertEqual(call.kwargs["user_id"], int(contacts.A))
        self.assertEqual(call.kwargs["message"][0]["type"], "record")
        self.assertTrue(
            call.kwargs["message"][0]["data"]["file"].startswith("file:///")
        )
        other = await self.adapter.target_for(
            fixtures.event(user_id=int(contacts.B), sender={"user_id": int(contacts.B)})
        )
        other.voice = VoiceCoordinator(other, self.root, {"mode": "on_demand"})
        with self.assertRaises(ValueError):
            await other.channel.send_record(asset)
        self.assertEqual(self.adapter.bot.call_action.await_count, 1)
