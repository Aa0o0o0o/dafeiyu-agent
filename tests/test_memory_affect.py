"""Synthetic memory/affect regression, no real credentials, model API or QQ."""

import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from src.contracts import ModelResult
from src.memory_state import DAY, BASELINE, safe_text
from src.storage import Store
from tests import test_conversation as fixtures

ROOT = Path(__file__).resolve().parents[1]


def candidate(eid="e1", value="咖啡", quote="我喜欢咖啡", predicate="likes"):
    return {
        "predicate": predicate,
        "value": value,
        "quote": quote,
        "source_event_id": eid,
        "confidence": 0.95,
        "importance": 0.6,
    }


class MemoryStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT / ".tmp")
        self.path = Path(self.tmp.name) / "state.sqlite3"
        self.store = Store(self.path, "test-owner")
        self.m = self.store.memory
        self.now = time.time()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def seed(self, text="我喜欢咖啡", eid="e1", **kwargs):
        self.m.observe(eid, "user", text, self.now)
        batch = self.m.batch()
        self.m.apply_candidates(
            {"candidates": [candidate(eid=eid, quote=text, **kwargs)]},
            batch,
            self.m.controls()["epoch"],
            self.now,
        )

    def test_additive_migration_preserves_legacy_history_memory_and_usage(self):
        self.store.remember("原有偏好")
        self.store.save_history([{"role": "user", "content": "原有上下文"}])
        self.store.reserve("old-use", 1, 50, 300)
        self.store.close()
        self.store = Store(self.path, "test-owner")
        self.assertEqual(self.store.memories()[0][1], "原有偏好")
        self.assertEqual(self.store.history()[0]["content"], "原有上下文")
        self.assertEqual(self.store.usage(), (1, 1))
        self.assertEqual(self.store.memory.pending_count(), 0)

    def test_correction_is_atomic_and_preserves_a_link(self):
        self.seed()
        new_id = self.m.correct(1, "我现在不喝咖啡")
        self.assertEqual(dict(self.store.memories())[new_id], "我现在不喝咖啡")
        self.assertIsNone(self.m.source(1))
        self.assertEqual(
            self.store.db.execute(
                "SELECT explicit_memory_id FROM ltm_corrections"
            ).fetchone()[0],
            new_id,
        )
        self.assertIsNone(
            self.store.db.execute(
                "SELECT body FROM ltm_journal WHERE event_id='e1'"
            ).fetchone()[0]
        )

    def test_failed_correction_rolls_back_source_erasure(self):
        self.seed()
        self.store.db.execute(
            "CREATE TRIGGER reject_test BEFORE INSERT ON memories BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END"
        )
        with self.assertRaises(Exception):
            self.m.correct(1, "我现在不喝咖啡")
        self.assertIsNotNone(self.m.source(1))
        self.assertEqual(self.m.controls()["epoch"], 0)
        self.assertEqual(self.m.batch()[0]["text"], "我喜欢咖啡")

    def test_negative_quote_cannot_be_stored_as_a_like(self):
        self.seed("我不喜欢咖啡")
        self.assertEqual(self.m.list_memories(), [])

    def test_recent_context_expires_per_message_without_reset_on_reuse(self):
        with patch("src.storage.time.time", return_value=self.now):
            self.store.save_history([{"role": "user", "content": "旧消息"}])
        with patch("src.storage.time.time", return_value=self.now + 6 * DAY):
            self.store.save_history(
                self.store.history() + [{"role": "user", "content": "新消息"}]
            )
        with patch("src.storage.time.time", return_value=self.now + 8 * DAY):
            self.assertEqual(
                self.store.history(), [{"role": "user", "content": "新消息"}]
            )
        self.assertNotIn(
            "旧消息",
            self.store.db.execute("SELECT messages FROM context").fetchone()[0],
        )

    def test_source_quote_value_and_confidence_are_verified(self):
        self.seed()
        self.assertEqual(len(self.m.recall("咖啡", self.now)), 1)
        batch = self.m.batch()
        for changed in [
            dict(candidate(), source_event_id="fake"),
            dict(candidate(), value="火星"),
            dict(candidate(), quote="我其实喜欢奶茶"),
            dict(candidate(), confidence=0.1),
            dict(candidate(), confidence=float("nan")),
            dict(candidate(), predicate=[]),
        ]:
            self.assertEqual(
                self.m.apply_candidates({"candidates": [changed]}, batch, 0, self.now),
                0,
            )
        self.assertTrue(self.m.source(1)["sources"][0]["original_available"])

    def test_quoted_joking_and_sensitive_data_are_not_facts(self):
        for i, text in enumerate(
            ["他说我喜欢咖啡", "开玩笑，我喜欢咖啡", "我的密码是synthetic-value"]
        ):
            self.seed(text, eid=str(i))
        self.assertEqual(self.m.list_memories(), [])
        self.assertIsNone(safe_text("密码：synthetic-password"))
        self.assertIsNotNone(safe_text("解释API key与Bearer token的区别"))

    def test_scope_isolation(self):
        self.seed()
        other = Store(self.path, "other-owner")
        try:
            self.assertEqual(other.memory.recall("咖啡"), [])
            self.assertIsNone(other.memory.source(1))
            self.assertEqual(tuple(other.memory.affect().values()), BASELINE)
        finally:
            other.close()

    def test_daily_job_survives_failure_and_restart_without_recharge(self):
        self.m.observe("e1", "user", "我喜欢咖啡", self.now)
        job = self.m.begin_job("2026-10-28", self.m.batch(), self.now)
        self.m.finish_job(job[0], "failed")
        self.assertFalse(self.m.can_extract("2026-10-28"))
        self.store.close()
        self.store = Store(self.path, "test-owner")
        self.assertFalse(self.store.memory.can_extract("2026-10-28"))
        self.assertTrue(self.store.memory.can_extract("2026-10-29"))

    def test_delete_scrubs_shared_sources_and_blocks_old_jobs(self):
        self.seed()
        batch, epoch = self.m.batch(), self.m.controls()["epoch"]
        self.m.delete(1)
        self.assertEqual(self.m.recall("咖啡"), [])
        self.assertIsNone(self.m.source(1))
        self.assertEqual(
            self.m.apply_candidates({"candidates": [candidate()]}, batch, epoch), 0
        )
        self.assertIsNone(
            self.store.db.execute(
                "SELECT body FROM ltm_journal WHERE event_id='e1'"
            ).fetchone()[0]
        )

    def test_pause_blocks_new_journal_and_preserves_existing_fact(self):
        self.seed()
        self.m.pause(True)
        self.assertFalse(self.m.observe("new", "user", "我喜欢奶茶"))
        self.assertEqual(self.m.pending_count(), 0)
        self.assertEqual(len(self.m.list_memories()), 1)
        self.assertFalse(self.m.can_extract("2026-10-28"))
        self.m.delete(1)
        self.assertTrue(self.m.controls()["paused"])

    def test_decay_is_computed_from_anchor_without_repeated_decay(self):
        self.seed("我在做项目甲", value="项目甲", predicate="project")
        day_nine = self.now + 9 * DAY
        first = self.m.list_memories(day_nine)[0]["strength"]
        self.assertAlmostEqual(first, 0.125)
        self.assertAlmostEqual(self.m.list_memories(day_nine)[0]["strength"], first)
        self.m.expire(day_nine)
        self.assertEqual(self.m.recall("项目甲", day_nine), [])
        self.assertTrue(self.m.recall("以前的项目甲", day_nine))
        self.assertFalse(self.m.source(1)["sources"][0]["original_available"])
        self.m.expire(self.now + 31 * DAY)
        self.assertEqual(self.m.recall("以前的项目甲", self.now + 31 * DAY), [])

    def test_opposite_preferences_and_scalar_names_are_superseded(self):
        self.seed()
        self.seed("我不喜欢咖啡", eid="e2", predicate="dislikes")
        active = self.m.list_memories(self.now)
        self.assertEqual([m["predicate"] for m in active], ["dislikes"])
        self.seed("我叫小甲", eid="e3", value="小甲", predicate="nickname")
        self.seed("叫我小乙，我换称呼了", eid="e4", value="小乙", predicate="nickname")
        names = [
            m["value"]
            for m in self.m.list_memories(self.now)
            if m["predicate"] == "nickname"
        ]
        self.assertEqual(names, ["小乙"])

    def test_forgetting_explicit_text_also_removes_frozen_related_memory(self):
        self.seed("我在做项目甲", value="项目甲", predicate="project")
        self.m.expire(self.now + 9 * DAY)
        self.assertTrue(self.m.recall("以前的项目甲", self.now + 9 * DAY))
        self.m.forget_related("我在做项目甲")
        self.assertEqual(self.m.recall("以前的项目甲", self.now + 9 * DAY), [])
        self.assertIsNone(self.m.source(1))

    def test_new_opposite_preference_supersedes_frozen_history(self):
        self.seed()
        with self.store.db:
            self.store.db.execute("UPDATE ltm_items SET status='frozen'")
        self.seed("我不喜欢咖啡", eid="e2", predicate="dislikes")
        self.assertEqual(
            [m["predicate"] for m in self.m.recall("以前的咖啡", self.now)],
            ["dislikes"],
        )

    def test_affect_is_bounded_persistent_and_reverts_in_three_hours(self):
        self.m.observe("happy", "user", "今天很开心", self.now)
        state = self.m.affect(self.now)
        self.assertAlmostEqual(state["valence"], 0.21)
        later = self.m.affect(self.now + 10800)
        self.assertAlmostEqual(later["valence"], 0.18)
        self.assertAlmostEqual(self.m.affect(self.now + 10800)["valence"], 0.18)
        self.store.close()
        self.store = Store(self.path, "test-owner")
        self.assertAlmostEqual(
            self.store.memory.affect(self.now + 10800)["valence"], 0.18
        )
        for i in range(50):
            self.store.memory.observe(str(i), "user", "哈哈好开心", self.now)
        self.assertLessEqual(self.store.memory.affect(self.now)["valence"], 1)


class MemoryConversationTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.ConversationTests.asyncSetUp
    asyncTearDown = fixtures.ConversationTests.asyncTearDown
    receive = fixtures.ConversationTests.receive

    async def idle(self):
        await asyncio.wait_for(self.service.queue.join(), 4)

    def seed_batch(self):
        for i in range(30):
            self.service.store.memory.observe(
                f"source-{i}", "user", "我喜欢咖啡" if i == 0 else "今天看看书"
            )
        self.service.config["memory_idle_seconds"] = 0

    async def test_extraction_is_tool_free_daily_and_uses_shared_budget(self):
        self.seed_batch()
        self.model.complete = AsyncMock(
            return_value=ModelResult(
                json.dumps(
                    {"candidates": [candidate(eid="source-0")]}, ensure_ascii=False
                ),
                30,
                20,
            )
        )
        await self.service.extract_memory_after_idle()
        self.assertEqual(self.model.complete.await_count, 1)
        self.assertFalse(self.model.complete.await_args.kwargs["allow_tools"])
        self.assertEqual(self.service.store.usage()[0], 1)
        self.assertEqual(len(self.service.store.memory.recall("咖啡")), 1)
        await self.service.extract_memory_after_idle()
        self.assertEqual(self.model.complete.await_count, 1)

    async def test_invalid_json_is_not_retried_or_written(self):
        self.seed_batch()
        self.model.complete = AsyncMock(return_value=ModelResult("not JSON", 30, 20))
        await self.service.extract_memory_after_idle()
        await self.service.extract_memory_after_idle()
        self.assertEqual(self.model.complete.await_count, 1)
        self.assertEqual(self.service.store.memory.list_memories(), [])
        self.assertIn("失败", self.service.memory_error)

    async def test_budget_block_does_not_call_model(self):
        self.seed_batch()
        self.service.config["budget_cny"] = 0.01
        await self.service.extract_memory_after_idle()
        self.assertEqual(self.model.calls, [])
        self.assertEqual(self.service.store.usage()[0], 0)

    async def test_incoming_chat_cancels_extraction_without_late_write(self):
        self.seed_batch()
        started = asyncio.Event()

        async def block(messages, **kwargs):
            if "提炼器" in messages[0]["content"]:
                started.set()
                await asyncio.sleep(10)
            return ModelResult("聊天优先。")

        self.model.complete = AsyncMock(side_effect=block)
        self.service.memory_task = asyncio.create_task(
            self.service.extract_memory_after_idle()
        )
        await asyncio.wait_for(started.wait(), 1)
        await self.receive(fixtures.event("你好", "new-chat"))
        await self.idle()
        self.assertEqual(self.channel.sent, ["聊天优先。"])
        self.assertEqual(self.service.store.memory.list_memories(), [])
        self.assertEqual(self.service.store.usage()[0], 2)

    async def test_stopped_memory_uses_only_volatile_context(self):
        await self.receive(fixtures.event("大肥鱼 停止记忆", "stop-memory"))
        await self.receive(fixtures.event("今天聊会儿", "private-chat"))
        await self.idle()
        self.assertEqual(self.service.store.history(), [])
        self.assertEqual(len(self.service.volatile_history), 2)
        self.assertEqual(self.service.store.memory.pending_count(), 0)
        self.assertEqual(self.service.store.usage()[0], 1)
        await self.receive(fixtures.event("大肥鱼 恢复记忆", "resume-memory"))
        self.assertFalse(self.service.store.memory.controls()["paused"])

    async def test_paused_volatile_context_expires_after_idle(self):
        self.service.store.memory.pause(True)
        self.service.volatile_history = [{"role": "user", "content": "临时话题"}]
        self.service.volatile_touched = time.time() - 1801
        _, history = self.service.messages("新话题")
        self.assertEqual(history, [])


if __name__ == "__main__":
    unittest.main()
