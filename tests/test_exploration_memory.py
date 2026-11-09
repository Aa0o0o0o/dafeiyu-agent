"""Recall actual exploration receipts without extra models or scope expansion."""
import json
import time
import unittest
from unittest.mock import AsyncMock

from src.autonomy import Autonomy
from src.contracts import ModelResult
from tests import test_conversation as fixtures
from tests import test_autonomy as autonomous_fixtures


class ExplorationMemoryTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.ConversationTests.asyncSetUp
    asyncTearDown = fixtures.ConversationTests.asyncTearDown
    prepare = autonomous_fixtures.AutonomyTests.prepare

    def bury_with_failures(self, agent):
        with agent.db:
            for i in range(4):
                agent.db.execute(
                    "INSERT INTO autonomy_runs(id,scope,status,created) VALUES(?,?,'failed',?)",
                    (f"failure-{i}", agent.scope, time.time() + i + 1),
                )

    async def test_generic_question_keeps_older_observation_after_failures_and_restart(self):
        agent = self.prepare()
        self.assertTrue(await agent.run(True))
        self.bury_with_failures(agent)
        restarted = Autonomy(self.service)
        ctx = restarted.context("你之前都探索过什么？")
        self.assertTrue(all(row[0] == "failed" for row in ctx["experiences"]))
        record = ctx["exploration_memory"]["recent_observed_records"][0]
        self.assertIn("example.com", record["observation"])
        self.assertTrue(record["summary_available"])
        calls_before = self.service.model.complete.await_count
        self.service.messages("你之前还记得查过什么？")
        self.assertEqual(self.service.model.complete.await_count, calls_before)
        self.assertIn("exploration_memory", json.dumps(self.service.messages("之前呢？")[0]))

    async def test_reflection_failure_remains_recallable_without_fabricated_summary(self):
        agent = self.prepare()
        self.service.model.complete = AsyncMock(side_effect=[
            ModelResult('{"action":"search","query":"鱼类颜色"}'),
            TimeoutError(),
        ])
        self.assertFalse(await agent.run(True))
        self.bury_with_failures(agent)
        ctx = agent.context("鱼类颜色")
        record = ctx["exploration_memory"]["recent_observed_records"][0]
        self.assertEqual(record["run_status"], "failed")
        self.assertFalse(record["summary_available"])
        self.assertEqual(record["model_summary"], "")
        self.assertIn("example.com", record["observation"])
        self.assertEqual(ctx["related_experiences"][0]["run_status"], "failed")

    async def test_legacy_results_are_readable_only_as_completed_search_receipts(self):
        agent = self.prepare()
        legacy = json.dumps({"query": "海洋声音", "retrieved_at": "2026-10-31",
                             "results": [{"url": "https://example.com/legacy", "title": "旧来源"}]})
        with agent.db:
            agent.db.execute(
                "INSERT INTO autonomy_runs(id,scope,status,query,receipt,created) VALUES(?,?,'succeeded',?,?,?)",
                ("legacy", agent.scope, "海洋声音", legacy, time.time() - 100),
            )
        self.bury_with_failures(agent)
        self.assertIn("legacy_search_results", agent.context()["exploration_memory"]["recent_observed_records"][0]["observation"])
        self.assertIn("example.com/legacy", agent.context("海洋声音")["related_experiences"][0]["observation"])
        self.assertEqual(agent.observed_receipt(legacy, "failed"), "")
        self.assertEqual(agent.observed_receipt('{"results":[]}', "succeeded"), "")

    async def test_other_scope_and_no_observation_never_become_memories(self):
        agent = self.prepare()
        self.service.web_tools.execute.side_effect = TimeoutError()
        self.assertFalse(await agent.run(True))
        with agent.db:
            agent.db.execute(
                "INSERT INTO autonomy_runs(id,scope,status,receipt,created) VALUES('other','other','succeeded',?,?)",
                (json.dumps({"query": "private-sentinel", "retrieved_at": "old", "results": [{"url": "https://example.com/private"}]}), time.time()),
            )
        self.assertEqual(agent.context()["exploration_memory"]["recent_observed_records"], [])
        self.assertEqual(agent.context("private-sentinel")["related_experiences"], [])
        self.service.peer_qq = "other"
        self.assertIsNone(agent.exploration_memory())

    async def test_clear_removes_recall_but_keeps_attempt_ledger(self):
        agent = self.prepare()
        await agent.run(True)
        used = agent.state()["attempts_today"]
        agent.clear()
        memory = Autonomy(self.service).context()["exploration_memory"]
        self.assertEqual(memory["stored_runs"], 0)
        self.assertEqual(memory["recent_observed_records"], [])
        self.assertEqual(agent.state()["attempts_today"], used)
