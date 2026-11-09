"""Proactive group opportunities, unmetered local quotas; synthetic only."""

import asyncio
import json
import time
import unittest
from unittest.mock import AsyncMock, patch

from src.contracts import ModelResult
from src.social_events import PokeGate
from src.stickers import StickerLibrary, sticker_reply_parts
from tests import test_group_chat as groups
from tests.test_chat_web import tool_result
from tests.test_conversation import CONFIG, event
from tests.test_vision import URL, png


class ActiveGroupTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = groups.GroupTests.asyncSetUp
    asyncTearDown = groups.GroupTests.asyncTearDown
    receive = groups.GroupTests.receive

    async def enable(self):
        self.group.config.update(
            group_proactive_enabled=True,
            group_keywords=["大肥鱼", "肥鱼", "dafeiyu"],
            group_random_probability=0.125,
        )
        self.group.random = lambda: 0.99

    async def test_keywords_trigger_without_at_and_wrong_group_stays_rejected(self):
        await self.enable()
        await self.receive(groups.group_event("肥鱼你怎么看", "name", mentioned=False))
        self.assertEqual(len(self.model.calls), 1)
        self.assertIn('"trigger": "keyword"', str(self.model.calls[0]))
        await self.receive(
            groups.group_event("大肥鱼", "wrong", group="5555555999", mentioned=False)
        )
        await self.receive(groups.group_event("继续说", "miss", mentioned=False))
        self.assertEqual(len(self.model.calls), 1)

    async def test_random_model_can_stay_silent_without_leaking_marker(self):
        await self.enable()
        self.group.random = lambda: 0.1
        self.model.complete = AsyncMock(return_value=ModelResult("[[silent]]"))
        await self.receive(
            groups.group_event("今晚聊点什么", "silent", mentioned=False)
        )
        self.assertEqual(self.model.complete.await_count, 1)
        self.assertEqual(self.group_channel.sent, [])
        self.assertNotIn("[[silent]]", str(self.group.recent))
        self.assertEqual(self.group.store.history(), [])

    async def test_hot_chat_and_recent_bot_reply_do_not_raise_random_probability(self):
        await self.enable()
        for i in range(6):
            await self.receive(
                groups.group_event(
                    f"大家正在聊游戏{i}",
                    f"warm{i}",
                    member=str(12345678 + i % 2),
                    mentioned=False,
                )
            )
        self.group.random = lambda: 0.5
        await self.receive(groups.group_event("这波太抽象了", "busy", mentioned=False))
        self.assertEqual(len(self.model.calls), 0)
        self.group.random = lambda: 0.1
        await self.receive(groups.group_event("偶尔参与", "random", mentioned=False))
        self.assertEqual(len(self.model.calls), 1)
        self.assertIn('"trigger": "random"', str(self.model.calls[0]))
        self.group.random = lambda: 0.5
        await self.receive(
            groups.group_event("继续热聊", "no_followup", mentioned=False)
        )
        self.assertEqual(len(self.model.calls), 1)

    async def test_spontaneous_queue_coalesces_and_stop_cancels_all(self):
        await self.enable()
        self.group.random = lambda: 0
        self.model.block = True
        await self.group.receive(
            groups.group_event("话题一", "first", mentioned=False), CONFIG["bot_qq"]
        )
        await asyncio.wait_for(self.model.started.wait(), 1)
        for i in range(12):
            await self.group.receive(
                groups.group_event(f"新话题{i}", f"pending{i}", mentioned=False),
                CONFIG["bot_qq"],
            )
        self.assertEqual(self.group.queue.qsize(), 1)
        await self.receive(
            groups.group_event(
                "大肥鱼 停止", "stop", member=CONFIG["owner_qq"], mentioned=False
            )
        )
        self.assertEqual(self.group.queue.qsize(), 0)
        self.assertEqual(self.group.requests, {})
        self.assertEqual(len(self.model.calls), 1)
        self.assertEqual(len(self.group_channel.sent), 1)

    async def test_other_members_multiple_pokes_have_no_shared_or_personal_cooldown(
        self,
    ):
        gate = PokeGate(dict(CONFIG, group_ids=[groups.GROUP], poke_enabled=True))
        base = {
            "post_type": "notice",
            "notice_type": "notify",
            "sub_type": "poke",
            "self_id": CONFIG["bot_qq"],
            "target_id": CONFIG["bot_qq"],
            "group_id": groups.GROUP,
            "time": time.time(),
        }
        for i, member in enumerate(["12345678", "87654321", "12345678"]):
            notice = dict(base, user_id=member, time=base["time"] + i / 100)
            converted = gate.convert(notice)
            self.assertIsNotNone(converted)
            await self.group.receive(converted, CONFIG["bot_qq"], program_event=True)
            await asyncio.wait_for(self.group.queue.join(), 2)
            self.assertIsNone(gate.convert(notice))
        self.assertEqual(len(self.model.calls), 3)
        self.assertEqual(len(self.group_channel.sent), 3)
        self.assertTrue(all("戳" in str(call) for call in self.model.calls))

    async def test_sticker_text_interleaving_and_sticker_only_no_cooldown(self):
        lib = StickerLibrary(self.db.parent / "stickers")
        self.group.sticker_library = lib
        token = lib.add_bytes(groups.GROUP, png(), "开心")
        order = []

        async def text(value):
            order.append(("text", value))
            return "receipt"

        async def sticker(value):
            order.append(("sticker", value))
            return "receipt"

        self.group_channel.send_text = text
        self.group_channel.send_sticker = sticker
        self.model.complete = AsyncMock(
            side_effect=[
                ModelResult(f"好耶\n[[sticker:{token}]]\n继续玩\n[[sticker:{token}]]"),
                ModelResult(f"[[sticker:{token}]]"),
            ]
        )
        try:
            await self.receive(groups.group_event(mid="mixed"))
            await self.receive(groups.group_event(mid="only"))
            self.assertEqual(
                order,
                [
                    ("text", "好耶"),
                    ("sticker", token),
                    ("text", "继续玩"),
                    ("sticker", token),
                    ("sticker", token),
                ],
            )
            self.assertNotIn("[[sticker:", str(self.group.recent))
        finally:
            await lib.close()

    async def test_unlimited_ledger_retains_old_usage_and_search_exceeds_old_two(self):
        service = self.private
        service.config.update(
            budget_cny=None, max_requests=None, web_max_requests_per_turn=None
        )
        service.store.reserve("previous", 150, None, None)
        service.web_tools = AsyncMock()
        service.web_tools.execute.return_value = '{"ok":true,"results":[]}'
        self.model.complete = AsyncMock(
            side_effect=[
                tool_result(
                    arguments=json.dumps({"query": f"主题{i}"}), call_id=f"tool{i}"
                )
                for i in range(4)
            ]
            + [ModelResult("综合查到了。")]
        )
        await service.receive(event("查四个问题", "unlimited"), CONFIG["bot_qq"])
        await asyncio.wait_for(service.queue.join(), 3)
        self.assertEqual(service.web_tools.execute.await_count, 4)
        self.assertEqual(self.model.complete.await_count, 5)
        count, amount = service.store.usage()
        self.assertEqual(count, 10)  # prior + five model calls + four web calls
        self.assertGreater(amount, 150)
        await service.receive(event("大肥鱼 状态", "status"), CONFIG["bot_qq"])
        self.assertIn("金额上限：无", self.private_channel.sent[-1])

    async def test_no_progress_tool_loop_gets_one_final_answer_attempt(self):
        service = self.private
        service.config["web_max_requests_per_turn"] = None
        service.web_tools = AsyncMock()
        service.web_tools.execute.return_value = '{"ok":true}'
        self.model.complete = AsyncMock(
            side_effect=[
                tool_result(),
                tool_result(call_id="repeat"),
                ModelResult("使用已有结果。"),
            ]
        )
        await service.receive(event("查一下", "cached"), CONFIG["bot_qq"])
        await asyncio.wait_for(service.queue.join(), 3)
        self.assertEqual(service.web_tools.execute.await_count, 1)
        self.assertFalse(self.model.complete.await_args_list[-1].kwargs["allow_tools"])

    async def test_more_than_three_current_images_have_no_count_quota(self):
        self.private.config.update(budget_cny=None, max_requests=None)
        self.private.image_loader = AsyncMock()
        self.private.image_loader.load.return_value = "data:image/png;base64,synthetic"
        await self.private.receive(
            event(mid="many", message=[{"type": "image", "data": {"url": URL}}] * 5),
            CONFIG["bot_qq"],
        )
        await asyncio.wait_for(self.private.queue.join(), 3)
        self.assertEqual(self.private.image_loader.load.await_count, 5)
        self.assertEqual(
            sum(p["type"] == "image_url" for p in self.model.calls[0][-1]["content"]), 5
        )
        with patch("src.conversation.MAX_IMAGE_REQUEST_BYTES", 10):
            await self.private.receive(
                event(
                    mid="oversize", message=[{"type": "image", "data": {"url": URL}}]
                ),
                CONFIG["bot_qq"],
            )
            await asyncio.wait_for(self.private.queue.join(), 3)
        self.assertEqual(len(self.model.calls), 1)
        self.assertIn("64MiB", self.private_channel.sent[-1])


class MarkerTests(unittest.TestCase):
    def test_unknown_and_fenced_markers_cannot_send_media(self):
        token = "S" + "a" * 16
        self.assertEqual(
            sticker_reply_parts(
                f"```\n[[sticker:{token}]]\n```\n[[sticker:../../file]]", {token}
            ),
            [("text", f"```\n[[sticker:{token}]]\n```")],
        )
