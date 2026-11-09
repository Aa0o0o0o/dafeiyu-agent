"""Synthetic expression semantics, scoped QQ delivery and permanent capability knowledge."""

import asyncio
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from PIL import Image

from src.capabilities import capability_manifest, capability_snapshot
from src.contracts import ModelResult
from src.qq_expressions import ExpressionSession, native_expression
from src.stickers import StickerLibrary, sticker_reply_parts
from src.tool_catalog import MODEL_TOOL_NAMES
from src.vision import FACE_LABELS, parse_message
from tests import test_conversation as fixtures
from tests.test_chat_web import tool_result
from tests import test_open_contacts as contacts

from tests.test_vision import png

A, UNKNOWN, GROUP = contacts.A, contacts.UNKNOWN, contacts.GROUP


class ExpressionLibraryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=fixtures.ROOT / ".tmp")
        self.root = Path(self.tmp.name)
        self.group = "12345001"
        self.lib = StickerLibrary(self.root / "stickers")
        self.token = self.lib.add_bytes(self.group, png(), "[表情包]")
        self.choices = set()
        self.session = ExpressionSession(self.lib, [self.group], self.choices)

    async def asyncTearDown(self):
        await self.lib.close()
        self.tmp.cleanup()

    async def execute(self, **args):
        return await self.session.execute(json.dumps(args, ensure_ascii=False))

    async def describe(self, **extra):
        return await self.execute(
            action="describe",
            token=self.token,
            description="一只猫大笑，表达开心",
            visible_text="哈哈哈",
            tags=["开心", "大笑"],
            emotions=["欢乐"],
            **extra,
        )

    async def test_only_after_completed_visual_turn_may_meaning_be_cached(self):
        with self.assertRaises(ValueError):
            await self.describe()
        result, images = await self.execute(action="inspect", tokens=[self.token])
        self.assertEqual(result["inspected"][0]["id"], self.token)
        self.assertTrue(any(x["type"] == "image_url" for x in images))
        with self.assertRaises(ValueError):
            await self.describe()
        self.session.model_saw_images()
        await self.describe()
        item = self.lib.catalog([self.group], "笑死了")[0]
        self.assertTrue(item["meaning_verified"])
        self.assertEqual(item["meaning_source"], "model_visual")
        self.assertEqual(item["visible_text"], "哈哈哈")
        await self.lib.close()
        self.lib = StickerLibrary(self.root / "stickers")
        self.assertEqual(self.lib.catalog([self.group])[0]["emotions"], ["欢乐"])

    async def test_owner_correction_wins_over_stale_model_description(self):
        await self.execute(action="inspect", tokens=[self.token])
        self.session.model_saw_images()
        await self.describe()
        self.lib.rename(self.token, [self.group], "实际是无奈，不是大笑")
        await self.describe()
        item = self.lib.catalog([self.group])[0]
        self.assertEqual(item["meaning_source"], "owner_label")
        self.assertEqual(item["description"], "实际是无奈，不是大笑")

    async def test_search_uses_meaning_instead_of_generic_summary(self):
        b = io.BytesIO()
        Image.new("RGB", (8, 8), "green").save(b, format="PNG")
        other = self.lib.add_bytes(self.group, b.getvalue(), "[表情包]")
        self.lib.remember_meaning(
            self.token,
            [self.group],
            description="无奈摊手吐槽",
            visible_text="服了",
            tags=["无语"],
            emotions=["无奈"],
        )
        self.lib.remember_meaning(
            other,
            [self.group],
            description="笑得很开心",
            visible_text="",
            tags=["欢乐"],
            emotions=["开心"],
        )
        result, _ = await self.execute(action="search", query="这也太离谱了")
        self.assertEqual(result["stickers"][0]["id"], self.token)
        self.assertIn(self.token, self.choices)

    async def test_untrusted_scope_path_secrets_and_unseen_annotations_are_denied(self):
        for args in [
            dict(action="inspect", tokens=["../../secret"]),
            dict(action="inspect", tokens=[self.token], scope="elsewhere"),
            dict(action="search", query="密码：private-test-value"),
            dict(action="describe", token=self.token, description="unseen"),
        ]:
            with self.assertRaises(ValueError):
                await self.execute(**args)
        foreign = ExpressionSession(self.lib, ["12345002"], set())
        with self.assertRaises(ValueError):
            await foreign.execute(
                json.dumps({"action": "inspect", "tokens": [self.token]})
            )

    async def test_native_market_replay_is_only_from_valid_stored_media(self):
        native = {
            "emoji_package_id": 123,
            "emoji_id": "a" * 32,
            "key": "b" * 32,
            "summary": "合成商城表情",
        }
        self.lib.remember_native(self.token, [self.group], native)
        self.assertEqual(
            self.lib.native_segment(self.token, [self.group]),
            {"type": "mface", "data": native},
        )
        with self.assertRaises(ValueError):
            self.lib.native_segment(self.token, ["12345002"])
        with self.assertRaises(ValueError):
            self.lib.remember_native(self.token, [self.group], {"emoji_id": "any"})
        path, _ = self.lib.resolve(self.token, [self.group])
        path.write_bytes(b"tampered")
        with self.assertRaises(ValueError):
            self.lib.native_segment(self.token, [self.group])

    async def test_inline_marker_user_reproduction_and_no_raw_controls(self):
        sample = "卖萌也没用，摸腿那笔账还挂着呢[[face:97]]"
        self.assertEqual(
            sticker_reply_parts(sample, set(), FACE_LABELS),
            [("text", "卖萌也没用，摸腿那笔账还挂着呢"), ("face", "97")],
        )
        mixed = f"好呀[[face:14]]收到[[sticker:{self.token}]]"
        self.assertEqual(
            sticker_reply_parts(mixed, {self.token}, FACE_LABELS),
            [
                ("text", "好呀"),
                ("face", "14"),
                ("text", "收到"),
                ("sticker", self.token),
            ],
        )
        for marker in [
            "[[face:999999]]",
            "[[face:nope]]",
            "[[face:97]",
            "[[face:97",
            "[[sticker:" + "x" * 200 + "]]",
        ]:
            self.assertEqual(
                sticker_reply_parts("好呀" + marker, set(), FACE_LABELS),
                [("text", "好呀")],
            )
        self.assertEqual(
            sticker_reply_parts("好呀[[face:97]]", set(), None), [("text", "好呀")]
        )
        for sample in [
            "示例 `[[face:97]]`",
            "~~~text\n[[face:97]]\n~~~",
            "```python\n[[face:97]]",
        ]:
            self.assertEqual(
                sticker_reply_parts(sample, set(), FACE_LABELS), [("text", sample)]
            )

    async def test_single_bracket_face_is_sent_as_expression_and_code_is_preserved(self):
        message = "哦，原来是CS起家的群，现在聊成这样了 [face:13]"
        self.assertEqual(sticker_reply_parts(message, set(), FACE_LABELS),
                         [("text", "哦，原来是CS起家的群，现在聊成这样了"), ("face", "13")])
        self.assertEqual(sticker_reply_parts("你好[face:999999]", set(), FACE_LABELS),
                         [("text", "你好")])
        self.assertEqual(sticker_reply_parts("代码 `[face:13]`", set(), FACE_LABELS),
                         [("text", "代码 `[face:13]`")])
        self.assertEqual(sticker_reply_parts("[face:13]", set(), None), [])

    async def test_ordered_native_and_magic_faces_never_execute_inside_code(self):
        text = f"好耶\n[[sticker:{self.token}]]\n[[face:14]]\n[[dice]]\n[[rps]]"
        self.assertEqual(
            sticker_reply_parts(text, {self.token}, FACE_LABELS),
            [
                ("text", "好耶"),
                ("sticker", self.token),
                ("face", "14"),
                ("dice", ""),
                ("rps", ""),
            ],
        )
        code = "```\n[[face:14]]\n[[dice]]\n```"
        self.assertEqual(
            sticker_reply_parts(code, set(), FACE_LABELS), [("text", code)]
        )
        self.assertEqual(sticker_reply_parts("[[face:999999]]", set(), FACE_LABELS), [])
        with self.assertRaises(ValueError):
            native_expression("face", "999999")
        self.assertEqual(native_expression("dice"), {"type": "dice", "data": {}})
        received = parse_message([{"type": "dice", "data": {"result": 6}}])
        self.assertIn("6", received.text)

    async def test_gif_inspection_uses_frames_but_sending_bytes_are_unchanged(self):
        a = Image.new("RGB", (20, 20), "red")
        b = Image.new("RGB", (20, 20), "blue")
        buf = io.BytesIO()
        a.save(buf, format="GIF", save_all=True, append_images=[b], duration=100)
        token = self.lib.add_bytes(self.group, buf.getvalue(), "GIF")
        result, _ = await self.execute(action="inspect", tokens=[token])
        self.assertEqual(result["inspected"][0]["image_frames"], 2)
        self.assertEqual(
            self.lib.resolve(token, [self.group])[0].read_bytes(), buf.getvalue()
        )


class ExpressionConversationTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.ConversationTests.asyncSetUp
    asyncTearDown = fixtures.ConversationTests.asyncTearDown
    receive = fixtures.ConversationTests.receive

    async def idle(self):
        await asyncio.wait_for(self.service.queue.join(), 3)

    async def test_casual_local_candidate_seen_cached_and_sent_without_explicit_request(
        self,
    ):
        lib = StickerLibrary(
            self.db.parent / "stickers", account_scope=fixtures.CONFIG["bot_qq"]
        )
        self.service.sticker_library = lib
        self.service.config = dict(
            self.service.config,
            sticker_account_shared=True,
            expression_preview_probability=1,
            expression_suggestion_probability=1,
        )
        token = lib.add_bytes(fixtures.CONFIG["bot_qq"], png(), "[表情包]")
        self.channel.send_sticker = AsyncMock(return_value="sticker-receipt")
        seen = []
        outcomes = [
            tool_result(
                "qq_expression",
                json.dumps(
                    {
                        "action": "describe",
                        "token": token,
                        "description": "合成笑脸，表示开心",
                        "visible_text": "",
                        "tags": ["开心"],
                        "emotions": ["欢乐"],
                    }
                ),
                "describe-preview",
            ),
            ModelResult(f"哈哈[[sticker:{token}]]"),
        ]

        async def complete(messages, **options):
            seen.append(json.loads(json.dumps(messages)))
            return outcomes.pop(0)

        self.model.complete = complete
        try:
            await self.receive(fixtures.event("哈哈今天真开心", "preview-casual"))
            await self.idle()
            self.assertEqual(len(seen), 2)
            self.assertTrue(
                any(
                    isinstance(m.get("content"), list)
                    and any(x["type"] == "image_url" for x in m["content"])
                    for m in seen[0]
                )
            )
            self.assertTrue(
                lib.catalog([fixtures.CONFIG["bot_qq"]])[0]["meaning_verified"]
            )
            self.channel.send_sticker.assert_awaited_once_with(token)
            self.assertNotIn("base64", str(self.service.store.history()))
            self.assertNotIn("[[sticker", str(self.channel.sent))
        finally:
            await lib.close()

    async def test_candidate_is_optional_and_serious_text_request_does_not_preview(
        self,
    ):
        lib = StickerLibrary(
            self.db.parent / "stickers", account_scope=fixtures.CONFIG["bot_qq"]
        )
        self.service.sticker_library = lib
        self.service.config = dict(
            self.service.config,
            sticker_account_shared=True,
            expression_preview_probability=1,
        )
        lib.add_bytes(fixtures.CONFIG["bot_qq"], png(), "[表情包]")
        self.channel.send_sticker = AsyncMock(return_value="unexpected")
        seen = []

        async def complete(messages, **options):
            seen.append(json.loads(json.dumps(messages)))
            return ModelResult("明白了。")

        self.model.complete = complete
        try:
            await self.receive(fixtures.event("你好", "optional-image"))
            await self.idle()
            self.channel.send_sticker.assert_not_awaited()
            await self.receive(
                fixtures.event("只发文字，详细分析代码", "serious-no-image")
            )
            await self.idle()
            self.assertFalse(
                any(
                    isinstance(m.get("content"), list)
                    and any(x["type"] == "image_url" for x in m["content"])
                    for m in seen[-1]
                )
            )
            self.assertEqual(self.channel.sent, ["明白了。", "明白了。"])
        finally:
            await lib.close()

    async def test_same_chat_model_inspects_caches_and_sends_then_reuses_without_extra_classifier(
        self,
    ):
        lib = StickerLibrary(
            self.db.parent / "stickers", account_scope=fixtures.CONFIG["bot_qq"]
        )
        self.service.config = dict(self.service.config, sticker_account_shared=True)
        self.service.sticker_library = lib
        token = lib.add_bytes(fixtures.CONFIG["bot_qq"], png(), "[表情包]")
        self.channel.send_sticker = AsyncMock(return_value="receipt")
        seen = []
        results = [
            tool_result(
                "qq_expression",
                json.dumps({"action": "inspect", "tokens": [token]}),
                "inspect",
            ),
            tool_result(
                "qq_expression",
                json.dumps(
                    {
                        "action": "describe",
                        "token": token,
                        "description": "笑脸，表达开心",
                        "visible_text": "",
                        "tags": ["开心"],
                        "emotions": ["欢乐"],
                    }
                ),
                "describe",
            ),
            ModelResult(f"好耶\n[[sticker:{token}]]"),
            ModelResult(f"[[sticker:{token}]]"),
        ]

        async def complete(messages, **options):
            seen.append(json.loads(json.dumps(messages)))
            self.assertIn("qq_expression", options["tool_names"])
            return results.pop(0)

        self.model.complete = complete
        try:
            await self.receive(fixtures.event("今天真开心", "first"))
            await self.idle()
            self.assertEqual(len(seen), 3)
            self.assertTrue(
                any(
                    isinstance(m.get("content"), list)
                    and any(x["type"] == "image_url" for x in m["content"])
                    for m in seen[1]
                )
            )
            self.assertTrue(
                lib.catalog([fixtures.CONFIG["bot_qq"]])[0]["meaning_verified"]
            )
            await self.receive(fixtures.event("哈哈", "second"))
            await self.idle()
            self.assertEqual(len(seen), 4)
            self.assertEqual(self.channel.send_sticker.await_count, 2)
            self.assertEqual(self.service.store.usage()[0], 4)
            history = str(self.service.store.history())
            self.assertNotIn("[[sticker:", history)
            self.assertNotIn("base64", history)
        finally:
            await lib.close()

    async def test_native_face_and_text_delivery_order_are_preserved(self):
        lib = StickerLibrary(self.db.parent / "stickers")
        self.service.sticker_library = lib
        ordered = []

        async def text(value):
            ordered.append(("text", value))
            return "ok"

        async def expression(kind, value):
            ordered.append((kind, value))
            return "ok"

        self.channel.send_text = text
        self.channel.send_expression = expression
        self.model.complete = AsyncMock(
            return_value=ModelResult("嗨\n[[face:14]]\n[[rps]]")
        )
        try:
            await self.receive(fixtures.event())
            await self.idle()
            self.assertEqual(ordered, [("text", "嗨"), ("face", "14"), ("rps", "")])
            self.assertNotIn("已发送表情", str(self.service.store.history()))
            self.assertIn("face", str(self.service.messages("继续")[0]))
        finally:
            await lib.close()

    async def test_single_bracket_face_is_delivered_as_a_qq_face(self):
        lib = StickerLibrary(self.db.parent / "stickers")
        self.service.sticker_library = lib
        self.service.voice = None
        ordered = []
        async def send_text(value):
            ordered.append(("text", value))
            return "123"
        async def send_expression(kind, value):
            ordered.append((kind, value))
            return "124"
        self.channel.send_text = send_text
        self.channel.send_expression = send_expression
        self.model.complete = AsyncMock(return_value=ModelResult(
            "哦，原来是CS起家的群，现在聊成这样了 [face:13]"))
        try:
            await self.receive(fixtures.event("你好", "single-face"))
            await self.idle()
            self.assertEqual(ordered, [("text", "哦，原来是CS起家的群，现在聊成这样了"),
                                       ("face", "13")])
            self.assertNotIn("[face:13]", str(self.service.store.history()))
        finally:
            await lib.close()

    async def test_semantic_send_request_is_not_mistaken_for_invalid_token_command(
        self,
    ):
        lib = StickerLibrary(self.db.parent / "stickers")
        self.service.sticker_library = lib
        try:
            await self.receive(fixtures.event("大肥鱼 发表情 开心"))
            await self.idle()
            self.assertEqual(len(self.model.calls), 1)
        finally:
            await lib.close()

    async def test_permanent_capabilities_survive_memory_clear_and_do_not_invent_disabled_tools(
        self,
    ):
        def system():
            return self.service.messages("你会什么")[0][0]["content"]

        before = system()
        self.assertIn("runtime_capabilities", before)
        self.assertIn("qq_expression", before)
        await self.receive(fixtures.event("大肥鱼 清空记忆", "clear"))
        after = system()
        self.assertIn("runtime_capabilities", after)
        snapshot = capability_snapshot(self.service)
        browser = next(x for x in snapshot["features"] if x["id"] == "browser")
        self.assertFalse(browser["enabled"])
        self.assertEqual(
            {
                name
                for entry in capability_manifest()["features"]
                for name in entry["model_tools"]
            },
            MODEL_TOOL_NAMES,
        )
        await self.receive(fixtures.event("大肥鱼 能力", "abilities"))
        self.assertIn("当前能用", self.channel.sent[-1])
        self.assertEqual(len(self.model.calls), 0)


class ExpressionWireTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = contacts.AccessTests.asyncSetUp
    asyncTearDown = contacts.AccessTests.asyncTearDown

    async def test_native_qq_segments_keep_verified_account_fixed_recipient_and_no_retry(
        self,
    ):
        self.adapter.service.config["sticker_enabled"] = True
        peer = await self.adapter.target_for(
            fixtures.event(user_id=int(A), sender={"user_id": int(A)})
        )
        self.adapter.bot.call_action = AsyncMock(return_value={"message_id": 123})
        await peer.channel.send_expression("face", "14")
        call = self.adapter.bot.call_action.await_args
        self.assertEqual(call.args, ("send_private_msg",))
        self.assertEqual(call.kwargs["user_id"], int(A))
        self.assertEqual(
            call.kwargs["message"], [{"type": "face", "data": {"id": "14"}}]
        )
        await self.adapter.groups[GROUP].channel.send_expression("dice", "")
        self.assertEqual(
            self.adapter.bot.call_action.await_args.kwargs["group_id"], int(GROUP)
        )
        count = self.adapter.bot.call_action.await_count
        with self.assertRaises((ConnectionError, ValueError)):
            await self.adapter.send_expression("face", "14", peer_qq=UNKNOWN)
        with self.assertRaises(ValueError):
            await peer.channel.send_expression("face", "../../file")
        self.assertEqual(self.adapter.bot.call_action.await_count, count)
        self.adapter.bot.call_action.side_effect = TimeoutError
        with self.assertRaises(TimeoutError):
            await peer.channel.send_expression("face", "14")
        self.assertEqual(self.adapter.bot.call_action.await_count, count + 1)

    async def test_collected_market_face_is_sent_with_its_verified_native_metadata(
        self,
    ):
        self.adapter.service.config["sticker_enabled"] = True
        lib = StickerLibrary(self.root / "runtime/stickers")
        self.adapter.stickers = lib
        token = lib.add_bytes(GROUP, png(), "合成商城表情")
        native = {
            "emoji_package_id": 123,
            "emoji_id": "a" * 32,
            "key": "b" * 32,
            "summary": "合成商城表情",
        }
        lib.remember_native(token, [GROUP], native)
        peer = await self.adapter.target_for(
            fixtures.event(user_id=int(A), sender={"user_id": int(A)})
        )
        self.adapter.bot.call_action = AsyncMock(return_value={"message_id": 124})
        await peer.channel.send_sticker(token)
        call = self.adapter.bot.call_action.await_args
        self.assertEqual(call.kwargs["user_id"], int(A))
        self.assertEqual(call.kwargs["message"], [{"type": "mface", "data": native}])


class CapabilitySyncTests(unittest.TestCase):
    def test_new_tool_without_capability_registration_is_rejected(self):
        with patch(
            "src.capabilities.MODEL_TOOL_NAMES",
            MODEL_TOOL_NAMES | {"unregistered_future_tool"},
        ):
            with self.assertRaises(ValueError):
                capability_manifest.__wrapped__()
