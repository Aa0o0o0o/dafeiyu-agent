"""Global autonomy facts must not be confused with per-conversation management rights."""

import json
import time
import unittest
from datetime import datetime

from src.autonomy import TZ
from src.capabilities import capability_snapshot
from tests import test_group_chat as groups


class AutonomyVisibilityTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = groups.GroupTests.asyncSetUp
    asyncTearDown = groups.GroupTests.asyncTearDown
    receive = groups.GroupTests.receive

    def prepare(self):
        self.private.autonomy.configure(6)
        self.private.autonomy.add_goal("private-autonomy-goal-sentinel")
        self.private.store.save_history(
            [{"role": "user", "content": "private-chat-sentinel"}]
        )

    def feature(self, service):
        return next(
            x for x in capability_snapshot(service)["features"] if x["id"] == "autonomy"
        )

    async def test_group_sees_global_feature_on_without_management_or_private_context(
        self,
    ):
        self.prepare()
        entry = self.feature(self.group)
        self.assertTrue(entry["enabled"])
        self.assertFalse(entry["management_allowed_in_current_scope"])
        self.assertTrue(entry["global_state"]["enabled"])
        self.assertEqual(entry["global_state"]["daily"], 6)
        messages, _ = self.group.messages("你开启自由探索了吗？")
        encoded = json.dumps(messages, ensure_ascii=False)
        self.assertNotIn("private-autonomy-goal-sentinel", encoded)
        self.assertNotIn("private-chat-sentinel", encoded)
        self.assertIn("global_state", encoded)
        self.assertFalse(self.group.autonomy.eligible())
        self.assertFalse(await self.group.autonomy.run(True))

    async def test_exhausted_daily_quota_is_not_missing_permission(self):
        self.prepare()
        day = datetime.now(TZ).date().isoformat()
        with self.private.store.db:
            for i in range(6):
                self.private.store.db.execute(
                    "INSERT INTO autonomy_attempts VALUES(?,?,?,?)",
                    (f"used-{i}", self.private.scope, day, time.time()),
                )
        group = self.feature(self.group)
        owner = self.feature(self.private)
        self.assertTrue(group["enabled"])
        self.assertTrue(group["authorization_granted"])
        self.assertEqual(group["global_state"]["remaining_today"], 0)
        self.assertEqual(group["runtime_reason"], "daily_limit_reached")
        self.assertEqual(group["global_state"], owner["global_state"])

    async def test_paused_is_a_state_not_authorization_revoked(self):
        self.prepare()
        self.private.autonomy.pause()
        entry = self.feature(self.group)
        self.assertTrue(entry["enabled"])
        self.assertTrue(entry["authorization_granted"])
        self.assertFalse(entry["global_state"]["enabled"])
        self.assertEqual(entry["runtime_reason"], "paused")

    async def test_group_status_readonly_no_model_and_no_private_goal(self):
        self.prepare()
        await self.receive(
            groups.group_event("大肥鱼 自主状态", mid="public-autonomy-status")
        )
        self.assertEqual(self.model.calls, [])
        self.assertIn("自主探索已开启", self.group_channel.sent[-1])
        self.assertNotIn("private-autonomy-goal-sentinel", self.group_channel.sent[-1])
        self.assertEqual(self.private_channel.sent, [])

    async def test_group_cannot_manage_despite_global_feature_on(self):
        self.prepare()
        await self.group.autonomy.command(
            "manage", "/dafeiyu autonomy pause", self.config["owner_qq"]
        )
        self.assertTrue(self.private.autonomy.state()["enabled"])
        self.assertIn("主人私聊", self.group_channel.sent[-1])
        self.assertFalse(
            self.feature(self.group)["management_allowed_in_current_scope"]
        )
