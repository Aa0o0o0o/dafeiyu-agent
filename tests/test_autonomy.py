import asyncio
import json
import unittest
from unittest.mock import AsyncMock

from src.autonomy import Autonomy
from src.contracts import ModelResult
from tests import test_conversation as fixtures


class AutonomyTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.ConversationTests.asyncSetUp
    asyncTearDown = fixtures.ConversationTests.asyncTearDown

    def prepare(self, decision="continue"):
        self.service.verified = True
        self.service.web_tools = type(
            "Web",
            (),
            {
                "execute": AsyncMock(
                    return_value=json.dumps(
                        {
                            "ok": True,
                            "retrieved_at": "synthetic-time",
                            "results": [
                                {
                                    "url": "https://example.com/science",
                                    "title": "Synthetic science",
                                }
                            ],
                        }
                    )
                )
            },
        )()
        self.service.model.complete = AsyncMock(
            side_effect=[
                ModelResult(
                    json.dumps({"action": "search", "query": "公开科普 鱼类颜色"}),
                    10,
                    20,
                    "fake-flash",
                ),
                ModelResult(
                    json.dumps(
                        {
                            "reflection": "找到一个候选来源，还需进一步核验。我对鱼类颜色有点好奇。",
                            "next_question": "鱼类颜色与环境的关系",
                            "decision": decision,
                        }
                    ),
                    20,
                    30,
                    "fake-flash",
                ),
            ]
        )
        self.service.autonomy.add_goal("公开科普中的鱼类颜色")
        return self.service.autonomy

    async def test_real_tool_receipt_persistence_next_goal_no_qq(self):
        a = self.prepare()
        self.assertTrue(await a.run(True))
        ctx = a.context()
        self.assertEqual(ctx["experiences"][0][0], "succeeded")
        self.assertIn("example.com", ctx["experiences"][0][2])
        self.assertEqual(ctx["goals"][0][1], "鱼类颜色与环境的关系")
        self.assertEqual(self.channel.sent, [])
        fresh = Autonomy(self.service)
        self.assertEqual(fresh.context()["experiences"], ctx["experiences"])
        usage = self.service.store.db.execute(
            "SELECT COUNT(*) FROM usage WHERE event_id LIKE 'autonomy%' AND status='completed'"
        ).fetchone()[0]
        self.assertEqual(usage, 3)

    async def test_default_no_background_call(self):
        a = self.prepare()
        self.assertFalse(await a.run())
        self.service.model.complete.assert_not_awaited()

    async def test_rest_is_not_research_success(self):
        a = self.prepare()
        self.service.model.complete = AsyncMock(
            return_value=ModelResult('{"action":"rest"}')
        )
        self.assertTrue(await a.run(True))
        self.service.web_tools.execute.assert_not_awaited()
        self.assertEqual(a.context()["experiences"][0][0], "rested")

    async def test_failure_is_not_success_no_retry(self):
        a = self.prepare()
        self.service.web_tools.execute.side_effect = TimeoutError()
        self.assertFalse(await a.run(True))
        self.assertEqual(a.context()["experiences"][0][0], "failed")
        self.assertEqual(a.context()["experiences"][0][2], "")
        self.assertEqual(self.service.web_tools.execute.await_count, 1)

    async def test_reflection_failure_keeps_actual_receipt(self):
        a = self.prepare()
        self.service.model.complete.side_effect = [
            ModelResult('{"action":"search","query":"科普"}'),
            ValueError("bad JSON"),
        ]
        self.assertFalse(await a.run(True))
        row = a.context()["experiences"][0]
        self.assertEqual(row[0], "failed")
        self.assertIn("example.com", row[2])
        self.assertEqual(row[3], "")

    async def test_clear_during_model_prevents_reappearance(self):
        a = self.prepare()
        ready, release = asyncio.Event(), asyncio.Event()

        async def blocked(*args, **kwargs):
            ready.set()
            await release.wait()
            return ModelResult('{"action":"search","query":"科普"}')

        self.service.model.complete = blocked
        task = asyncio.create_task(a.run(True))
        await ready.wait()
        a.clear()
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(a.context()["goals"], [])
        self.assertEqual(a.context()["experiences"], [])
        self.service.web_tools.execute.assert_not_awaited()

    async def test_scope_isolation(self):
        a = self.prepare()
        await a.run(True)
        original = self.service.scope
        self.service.scope = original + ":other"
        b = Autonomy(self.service)
        self.assertEqual(b.context()["goals"], [])
        self.assertEqual(b.context()["experiences"], [])
        self.service.scope = original

    async def test_unauthorized_no_model(self):
        a = self.prepare()
        self.service.peer_qq = "4444444444"
        self.assertFalse(await a.run(True))
        self.assertIsNone(a.context())
        with self.assertRaises(ValueError):
            a.add_goal("科普")
        self.service.model.complete.assert_not_awaited()

    async def test_unsupported_actions_never_execute(self):
        a = self.prepare()
        self.service.model.complete = AsyncMock(
            return_value=ModelResult('{"action":"shell","query":"whoami"}')
        )
        self.assertFalse(await a.run(True))
        self.service.web_tools.execute.assert_not_awaited()

    async def test_daily_limit_counts_failure_and_no_repeat_on_restart(self):
        a = self.prepare()
        a.configure(1)
        self.service.web_tools.execute.side_effect = TimeoutError()
        self.assertFalse(await a.run())
        b = Autonomy(self.service)
        self.assertFalse(await b.run())
        self.assertEqual(self.service.model.complete.await_count, 1)

    async def test_done_goal_stops_loop(self):
        a = self.prepare("done")
        self.assertTrue(await a.run(True))
        self.assertFalse(await a.run(True))
        self.assertEqual(self.service.model.complete.await_count, 2)

    async def test_invalid_secret_and_limits(self):
        a = self.service.autonomy
        for value in [True, -1, 25, "2"]:
            with self.assertRaises(ValueError):
                a.configure(value)
        with self.assertRaises(ValueError):
            a.add_goal("密码：abcdef123456")

    async def test_private_command_and_chat_injection(self):
        a = self.prepare()
        await a.run(True)
        await self.service.receive(
            fixtures.event(
                message=[{"type": "text", "data": {"text": "/dafeiyu autonomy status"}}]
            ),
            fixtures.CONFIG["bot_qq"],
        )
        self.assertIn("自主状态", self.channel.sent[-1])
        self.service.model.complete = AsyncMock(
            return_value=ModelResult("我还在琢磨那个问题。")
        )
        await self.service.receive(
            fixtures.event(
                message_id="second",
                message=[{"type": "text", "data": {"text": "在做什么"}}],
            ),
            fixtures.CONFIG["bot_qq"],
        )
        await asyncio.wait_for(self.service.queue.join(), 3)
        payload = str(self.service.model.complete.call_args)
        self.assertIn("autonomous_self_state", payload)
        self.assertIn("鱼类颜色与环境的关系", payload)

    async def test_v2_epoch_erases_derived_state(self):
        from src.memory_v2.service import MemoryService

        c = self.service
        c.v2 = MemoryService(
            c.store.db,
            c.scope,
            c.config["bot_qq"],
            c.config["instance_id"],
            c.config["owner_qq"],
        )
        a = self.prepare()
        await a.run(True)
        with c.store.db:
            c.store.db.execute(
                "UPDATE mv2_scopes SET epoch=epoch+1 WHERE scope_id=?", (c.scope,)
            )
        self.assertEqual(a.context()["experiences"], [])
        self.assertEqual(a.context()["goals"], [])
        self.assertFalse(a.state()["enabled"])

    async def test_actual_chat_preempts_background_without_qq_side_effect(self):
        a = self.prepare()
        ready = asyncio.Event()

        async def wait(*args, **kwargs):
            ready.set()
            await asyncio.sleep(30)

        self.service.model.complete = wait
        self.assertTrue(a.schedule(True))
        await ready.wait()
        await self.service.receive(
            fixtures.event(
                message=[{"type": "text", "data": {"text": "/dafeiyu autonomy status"}}]
            ),
            fixtures.CONFIG["bot_qq"],
        )
        self.assertTrue(a.task.done())
        self.assertEqual(a.context()["experiences"][0][0], "cancelled")
        self.assertEqual(len(self.channel.sent), 1)
        self.service.web_tools.execute.assert_not_awaited()

    async def test_unverified_login_blocks_even_manual(self):
        a = self.prepare()
        self.service.verified = False
        self.assertFalse(await a.run(True))
        self.service.model.complete.assert_not_awaited()

    def automatic(self):
        self.service.config = dict(
            self.service.config,
            autonomy_extension={"auto_start": True, "daily_runs": 6},
        )
        self.service.autonomy.online()
        return self.service.autonomy

    async def test_online_default_and_pause_resumes_next_start(self):
        a = self.automatic()
        self.assertEqual(a.state()["daily"], 6)
        self.assertTrue(a.state()["enabled"])
        self.assertTrue(a.context()["goals"])
        a.pause()
        self.assertFalse(a.state()["enabled"])
        restarted = Autonomy(self.service)
        self.assertTrue(restarted.state()["enabled"])
        self.assertEqual(restarted.state()["daily"], 6)
        self.assertEqual(self.model.calls, [])

    async def test_clear_restart_cannot_reset_daily_allowance(self):
        from src.autonomy import TZ
        from datetime import datetime
        import time

        a = self.prepare()
        self.automatic()
        day = datetime.now(TZ).date().isoformat()
        with a.db:
            for i in range(6):
                a.db.execute(
                    "INSERT INTO autonomy_attempts VALUES(?,?,?,?)",
                    (f"limit-{i}", a.scope, day, time.time() - 7200),
                )
        a.clear()
        restarted = Autonomy(self.service)
        self.assertFalse(await restarted.run())
        self.service.model.complete.assert_not_awaited()
        self.assertEqual(restarted.state()["daily"], 6)
        self.assertEqual(
            restarted.db.execute(
                "SELECT COUNT(*) FROM autonomy_attempts WHERE scope=?", (a.scope,)
            ).fetchone()[0],
            6,
        )

    async def test_new_calendar_day_gets_allowance_without_approval(self):
        a = self.prepare()
        self.automatic()
        with a.db:
            for i in range(6):
                a.db.execute(
                    "INSERT INTO autonomy_attempts VALUES(?,?,?,?)",
                    (f"old-{i}", a.scope, "2000-01-01", 0),
                )
        self.service.model.complete = AsyncMock(
            return_value=ModelResult('{"action":"rest"}')
        )
        self.assertTrue(await a.run())
        self.assertEqual(a.context()["experiences"][0][0], "rested")
        self.assertTrue(a.state()["enabled"])

    async def test_finished_projects_auto_offer_new_public_question(self):
        a = self.prepare("done")
        self.automatic()
        with a.db:
            a.db.execute(
                "UPDATE autonomy_goals SET status='done' WHERE scope=?", (a.scope,)
            )
        self.service.model.complete = AsyncMock(
            return_value=ModelResult('{"action":"rest"}')
        )
        self.assertTrue(await a.run())
        self.assertTrue(any(g[2] == "active" for g in a.context()["goals"]))
        self.assertEqual(self.channel.sent, [])

    async def test_startup_policy_does_not_enable_other_peers(self):
        self.service.peer_qq = "4444444444"
        self.automatic()
        self.assertFalse(self.service.autonomy.state()["enabled"])
        self.assertIsNone(self.service.autonomy.context())
