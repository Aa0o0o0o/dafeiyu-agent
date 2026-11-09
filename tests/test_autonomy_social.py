import asyncio
import json
import unittest
from unittest.mock import AsyncMock
from src.autonomy import Autonomy
from src.autonomy_social import split_interest
from src.contracts import ModelResult
from src.memory_v2.astrbot_bridge import verify_delivery
from tests import test_conversation as fixtures
from tests import test_autonomy as autonomy_fixtures


class SocialTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.ConversationTests.asyncSetUp
    asyncTearDown = fixtures.ConversationTests.asyncTearDown
    prepare = autonomy_fixtures.AutonomyTests.prepare

    def setup_social(self):
        self.service.config = dict(
            self.service.config,
            autonomy_extension=dict(share_owner=True, chat_interests=True),
        )
        a = self.prepare()
        a.configure(6)
        return a

    async def deliver_day(self, a):
        return await a.social.deliver()

    async def test_new_success_share_once_and_restart_no_repeat(self):
        a = self.setup_social()
        await a.run(True)
        self.assertTrue(await self.deliver_day(a))
        self.assertIn("https://example.com/science", self.channel.sent[0])
        self.assertEqual(len(self.channel.sent), 1)
        self.assertFalse(await self.deliver_day(Autonomy(self.service)))
        self.assertEqual(len(self.channel.sent), 1)
        self.assertEqual(self.model.complete.await_count, 2)

    async def test_no_share_for_rest_failure_or_no_observed_source(self):
        a = self.setup_social()
        self.model.complete = AsyncMock(return_value=ModelResult('{"action":"rest"}'))
        await a.run(True)
        self.assertFalse(await self.deliver_day(a))
        self.assertEqual(self.channel.sent, [])

    async def test_cancelled_delivery_consumed_before_io_no_next_day_retry(self):
        a = self.setup_social()
        await a.run(True)
        self.channel.send_text = AsyncMock(side_effect=asyncio.CancelledError())
        with self.assertRaises(asyncio.CancelledError):
            await self.deliver_day(a)
        self.assertEqual(
            a.db.execute("SELECT COUNT(*) FROM autonomy_share_queue").fetchone()[0], 0
        )
        fresh = Autonomy(self.service)
        self.assertFalse(await fresh.social.deliver())

    async def test_unknown_delivery_not_retried(self):
        a = self.setup_social()
        await a.run(True)
        self.channel.fail = True
        self.assertFalse(await self.deliver_day(a))
        self.channel.fail = False
        self.assertFalse(await self.deliver_day(a))
        state = a.db.execute("SELECT state FROM autonomy_owner_messages").fetchone()[0]
        self.assertEqual(state, "unknown")
        self.assertEqual(self.channel.sent, [])

    async def test_guard_clear_before_transport_commit_and_scope(self):
        a = self.setup_social()
        await a.run(True)

        async def changed(_):
            a.clear()
            verify_delivery()

        self.channel.send_text = changed
        with self.assertRaises(asyncio.CancelledError):
            await self.deliver_day(a)
        self.assertEqual(
            a.db.execute("SELECT COUNT(*) FROM autonomy_share_queue").fetchone()[0], 0
        )
        self.service.peer_qq = "123456789"
        self.assertFalse(await self.deliver_day(a))
        self.assertFalse(a.social.capture("海洋动物的颜色"))

    async def test_busy_defers_but_no_daytime_gate(self):
        a = self.setup_social()
        await a.run(True)
        self.service.active = True
        self.assertFalse(await a.social.deliver())
        self.service.active = None
        self.assertEqual(
            a.db.execute(
                "SELECT COUNT(*) FROM autonomy_owner_messages WHERE state='pending'"
            ).fetchone()[0],
            1,
        )
        self.assertTrue(await a.social.deliver())

    async def test_multiple_thoughts_same_day_and_no_duplicates(self):
        a = self.setup_social()
        self.assertTrue(
            a.social.enqueue_message("thought:first", "突然对章鱼的运动方式有点好奇。")
        )
        self.assertTrue(await a.social.deliver())
        self.assertFalse(
            a.social.enqueue_message("thought:dup", "突然对章鱼的运动方式有点好奇。")
        )
        self.assertTrue(
            a.social.enqueue_message(
                "thought:second", "想歇一会儿，也想听听你今天怎么样。"
            )
        )
        self.assertFalse(await a.social.deliver())
        with a.db:
            a.db.execute("UPDATE autonomy_owner_messages SET attempted=0")
        self.assertTrue(await a.social.deliver())
        self.assertEqual(len(self.channel.sent), 2)

    async def test_rest_can_express_thought_without_fake_discovery(self):
        a = self.setup_social()
        self.model.complete = AsyncMock(
            return_value=ModelResult(
                '{"action":"rest","message_to_owner":"想休息一下，等会儿再继续。"}'
            )
        )
        await a.run(True)
        self.assertTrue(await a.social.deliver())
        self.assertEqual(self.channel.sent, ["想休息一下，等会儿再继续。"])
        self.service.web_tools.execute.assert_not_awaited()

    async def test_no_background_quota_does_not_block_existing_thought(self):
        a = self.setup_social()
        a.configure(1)
        await a.run(True)
        self.assertEqual(a.state()["remaining_today"], 0)
        self.assertTrue(await a.social.deliver())
        self.assertEqual(len(self.channel.sent), 1)

    async def test_real_remaining_count_and_cooldown_not_exhaustion(self):
        a = self.setup_social()
        await a.run(True)
        status = a.public_status()
        self.assertEqual(status["global_state"]["attempts_today"], 1)
        self.assertEqual(status["global_state"]["remaining_today"], 5)
        self.assertEqual(status["runtime_reason"], "enabled")
        self.assertGreater(status["background_cooldown_seconds"], 0)

    async def test_interest_dedup_limit_privacy_and_clear(self):
        a = self.setup_social()
        self.assertTrue(a.social.capture("鱼类颜色与环境"))
        self.assertFalse(a.social.capture("鱼类颜色与环境"))
        for s in [
            "微信好友王某的喜好",
            "我的公司",
            "QQ号123456",
            "https://example.com",
            "密码是abc",
        ]:
            self.assertFalse(a.social.capture(s), s)
        for s in [
            "航空发动机",
            "语音合成",
            "网络谐音梗",
            "动物迁徙",
            "海洋生态",
            "机器人视觉",
        ]:
            a.social.capture(s)
        n = a.db.execute(
            "SELECT COUNT(*) FROM autonomy_chat_interests i JOIN autonomy_goals g ON i.goal=g.id WHERE i.scope=? AND g.status='active'",
            (a.scope,),
        ).fetchone()[0]
        self.assertEqual(n, 5)
        a.clear()
        self.assertEqual(
            a.db.execute("SELECT COUNT(*) FROM autonomy_chat_interests").fetchone()[0],
            0,
        )

    async def test_marker_hidden_and_capture_only_after_owner_reply(self):
        a = self.setup_social()
        self.model.complete = AsyncMock(
            return_value=ModelResult(
                "这个挺有意思，我也想弄明白。[[explore:鱼类颜色与环境]]"
            )
        )
        await self.service.receive(
            fixtures.event("聊聊鱼的颜色", "interest"), fixtures.CONFIG["bot_qq"]
        )
        await asyncio.wait_for(self.service.queue.join(), 3)
        self.assertNotIn("[[explore:", "".join(self.channel.sent))
        self.assertNotIn(
            "[[explore:", json.dumps(self.service.store.history(), ensure_ascii=False)
        )
        self.assertTrue(
            a.db.execute(
                "SELECT 1 FROM autonomy_chat_interests WHERE topic='鱼类颜色与环境'"
            ).fetchone()
        )

    async def test_no_backfill_old_success(self):
        a = self.prepare()
        await a.run(True)
        self.service.config = dict(
            self.service.config, autonomy_extension=dict(share_owner=True)
        )
        a.configure(6)
        self.assertFalse(await self.deliver_day(a))


class MarkerTests(unittest.TestCase):
    def test_code_and_invalid_markers(self):
        self.assertEqual(split_interest("你好[[explore:科普]]"), ("你好", ["科普"]))
        self.assertEqual(split_interest("`[[explore:科普]]`")[1], [])
        self.assertEqual(split_interest("你好[[explore:未闭合")[0], "你好")
