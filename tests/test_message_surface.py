import asyncio
import unittest
from unittest.mock import AsyncMock
from src.message_surface import strip_delivery_annotations
from src.stickers import sticker_reply_parts
from src.voice.normalize import spoken_text
from src.contracts import ModelResult
from tests import test_conversation as private
from tests import test_group_chat as groups

LEAK = "[已发送表情：收藏S26caaed8090f53a6]"


class SurfacePureTests(unittest.TestCase):
    def test_metadata_not_a_sticker_command(self):
        self.assertEqual(
            sticker_reply_parts("我在。\n" + LEAK, set()), [("text", "我在。")]
        )
        self.assertEqual(
            strip_delivery_annotations("不要加糖，是25元，不是15元。" + LEAK),
            "不要加糖，是25元，不是15元。",
        )
        self.assertEqual(spoken_text("我在。" + LEAK), "我在。")

    def test_literal_code_examples_are_preserved(self):
        for value in [f"`{LEAK}`", f"```text\n{LEAK}\n```", f"~~~text\n{LEAK}\n~~~"]:
            self.assertEqual(strip_delivery_annotations(value), value)
        self.assertEqual(
            strip_delivery_annotations("好呀。" + LEAK + "[[face:14]]"),
            "好呀。[[face:14]]",
        )


class PrivateSurfaceTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = private.ConversationTests.asyncSetUp
    asyncTearDown = private.ConversationTests.asyncTearDown

    async def test_old_history_is_not_echoed_and_new_reply_has_no_annotation(self):
        self.service.store.save_history(
            [
                {"role": "user", "content": "你好"},
                {"role": "assistant", "content": "我在。" + LEAK},
            ]
        )
        messages, _ = self.service.messages("继续")
        self.assertFalse(
            any(
                m.get("role") == "assistant" and LEAK in str(m.get("content"))
                for m in messages
            )
        )
        self.model.complete = AsyncMock(return_value=ModelResult("我在呢。\n" + LEAK))
        await self.service.receive(
            private.event("继续", "echo-fix"), private.CONFIG["bot_qq"]
        )
        await asyncio.wait_for(self.service.queue.join(), 3)
        self.assertEqual(self.channel.sent, ["我在呢。"])
        self.assertNotIn(LEAK, self.service.store.history()[-1]["content"])
        self.assertIn(LEAK, self.service.store.history()[1]["content"])


class GroupSurfaceTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = groups.GroupTests.asyncSetUp
    asyncTearDown = groups.GroupTests.asyncTearDown

    async def test_group_output_also_filters_internal_receipt(self):
        self.model.complete = AsyncMock(
            return_value=ModelResult("我先看看大家在聊什么。\n" + LEAK)
        )
        await self.group.receive(
            groups.group_event("你怎么看", "group-surface"), groups.CONFIG["bot_qq"]
        )
        await asyncio.wait_for(self.group.queue.join(), 3)
        self.assertEqual(self.group_channel.sent, ["我先看看大家在聊什么。"])
        self.assertNotIn(LEAK, str(self.group.recent))

    async def test_group_context_filters_only_bot_receipts(self):
        import time
        import json

        self.group.current_request = {
            "recent": [
                {
                    "time": time.time(),
                    "member_qq": self.config["bot_qq"],
                    "text": "我在。" + LEAK,
                },
                {
                    "time": time.time(),
                    "member_qq": "12345678",
                    "text": "这个是什么意思" + LEAK,
                },
            ]
        }
        messages, _ = self.group.messages("继续")
        packet = next(
            m["content"]
            for m in messages
            if str(m.get("content", "")).startswith("<group_context_data>")
        )
        recent = json.loads(
            packet.removeprefix("<group_context_data>").removesuffix(
                "</group_context_data>"
            )
        )["recent"]
        self.assertNotIn(LEAK, recent[0]["text"])
        self.assertIn(LEAK, recent[1]["text"])
