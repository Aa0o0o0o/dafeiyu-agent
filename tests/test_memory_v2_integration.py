"""Existing QQ adapter + V2 bridge, fake provider and receipts only."""

import asyncio
import hashlib
import json
import unittest
from unittest.mock import AsyncMock

from src.conversation import ConversationService
from src.group_chat import GroupConversation
from src.memory_v2.extraction import extract_batch
from src.memory_v2.graph_api import create_app
from src.memory_v2.migration import migrate_legacy
from src.contracts import ModelResult
from tests import test_group_chat as groups
from tests.test_conversation import CONFIG, event
from tests.test_memory_v2 import A, B, ROOT
from tests import test_memory_v2 as v2_cases


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = groups.GroupTests.asyncSetUp
    asyncTearDown = groups.GroupTests.asyncTearDown

    async def active(self):
        await self.private.close()
        await self.group.close()
        cfg = dict(
            self.config,
            memory_v2={
                "enabled": True,
                "mode": "active",
                "batch_size": 30,
                "ui_enabled": False,
            },
        )
        self.private = ConversationService(
            cfg, self.db, self.model, self.private_channel
        )
        self.group = GroupConversation(
            cfg, self.db, self.model, self.group_channel, groups.GROUP
        )

    async def test_single_injection_explicit_command_and_group_isolation(self):
        await self.active()
        await self.private.receive(
            event("大肥鱼 记住：喜欢无糖苏打水", "remember"), CONFIG["bot_qq"]
        )
        await self.private.receive(event("我喜欢什么水", "ask"), CONFIG["bot_qq"])
        await asyncio.wait_for(self.private.queue.join(), 2)
        body = str(self.model.calls[0])
        self.assertIn("喜欢无糖苏打水", body)
        self.assertNotIn("<confirmed_memory>", body)
        self.assertEqual(body.count("'memory_v2'"), 0)  # JSON serialized, one V2 field
        self.assertEqual(body.count('"memory_v2"'), 1)
        self.assertEqual(self.private.store.memories(), [])
        self.assertEqual(self.private.store.memory.pending_count(), 0)
        await self.group.receive(
            groups.group_event("他私聊喜欢什么水", "group"), CONFIG["bot_qq"]
        )
        await asyncio.wait_for(self.group.queue.join(), 2)
        self.assertNotIn("喜欢无糖苏打水", str(self.model.calls[1]))

    async def test_other_group_members_off_me_without_admin_and_no_old_writer(self):
        await self.active()
        await self.group.receive(
            groups.group_event("我喜欢草莓汽水", "source", member=A, mentioned=False),
            CONFIG["bot_qq"],
        )
        self.assertEqual(len(self.group.v2.pending()), 1)
        await self.group.receive(
            groups.group_event("大肥鱼 不要记我", "off", member=A, mentioned=False),
            CONFIG["bot_qq"],
        )
        self.assertFalse(self.group.v2.policy(A)["enabled"])
        mentioned = groups.group_event(
            "我说甲喜欢草莓汽水", "ref", member=B, mentioned=False
        )
        mentioned["message"].append({"type": "at", "data": {"qq": A}})
        await self.group.receive(mentioned, CONFIG["bot_qq"])
        self.assertFalse(self.group.v2.pending())
        self.assertEqual(self.group.store.memory.pending_count(), 0)

    async def test_deleted_memory_during_model_generation_prevents_send(self):
        await self.active()
        actor = CONFIG["owner_qq"]
        mid = self.private.v2.remember(actor, "喜欢无糖汽水", "source")
        self.model.block = True
        await self.private.receive(
            event("我喜欢什么汽水", "question"), CONFIG["bot_qq"]
        )
        await asyncio.wait_for(self.model.started.wait(), 1)
        self.private.v2.forget(actor, mid)
        self.model.ignore_cancel = True
        self.private.active.cancel()
        await asyncio.wait_for(self.private.queue.join(), 2)
        self.assertEqual(self.private_channel.sent, [])

    async def test_extraction_reuses_provider_json_and_shared_usage(self):
        await self.active()
        actor = CONFIG["owner_qq"]
        sid = self.private.v2.observe(
            {
                "event_id": "source",
                "sender_id": actor,
                "subject_ids": [actor],
                "text": "我喜欢无糖汽水",
            }
        )
        candidate = {
            "kind": "semantic",
            "subject_ids": [actor],
            "predicate": "likes",
            "value": "无糖汽水",
            "polarity": "positive",
            "assertion_mode": "self_report",
            "temporal_class": "stable_preference",
            "source_event_ids": [sid],
            "confidence_score": 0.95,
            "importance": 0.5,
        }
        self.model.complete_memory = AsyncMock(
            return_value=ModelResult(json.dumps({"candidates": [candidate]}), 250, 120)
        )
        await extract_batch(self.private)
        self.model.complete_memory.assert_awaited_once()
        self.assertEqual(self.private.store.usage()[0], 1)
        self.assertEqual(self.private.v2.pending(), [])
        self.assertEqual(len(self.private.v2.list_memories(actor)), 1)
        self.assertEqual(self.private.store.memory.pending_count(), 0)
        self.assertEqual(
            self.private.v2.maintenance_status()["today_learning_reserved_tokens"] > 0,
            True,
        )


class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.base = v2_cases.V2Tests()
        self.base.setUp()
        self.m = self.base.memory
        self.mid, self.source_id = self.base.add()
        salt = "00" * 16
        self.auth = {
            "viewer_id": A,
            "salt": salt,
            "hash": hashlib.pbkdf2_hmac(
                "sha256", b"offline", bytes.fromhex(salt), 300000
            ).hex(),
        }
        self.app = create_app(
            {self.m.scope: self.m},
            self.auth,
            ROOT / "memory_ui",
            port=6201,
            demo_mode=True,
        )
        self.client = self.app.test_client()
        self.headers = {"Host": "127.0.0.1:6201", "Origin": "http://127.0.0.1:6201"}

    async def asyncTearDown(self):
        self.base.tearDown()

    async def login(self):
        response = await self.client.post(
            "/login", json={"viewer_id": A, "password": "offline"}, headers=self.headers
        )
        self.assertEqual(response.status_code, 200)
        info = await (
            await self.client.get("/memory-ui/session", headers=self.headers)
        ).get_json()
        self.headers["X-CSRF-Token"] = info["csrf"]

    async def test_auth_csrf_origin_host_and_scope_tampering(self):
        self.assertEqual(
            (
                await self.client.get("/memory-ui/scopes", headers=self.headers)
            ).status_code,
            401,
        )
        await self.login()
        self.assertEqual(
            (
                await self.client.get(
                    "/memory-ui/graph?scope_id=another-private-scope",
                    headers=self.headers,
                )
            ).status_code,
            404,
        )
        wrong = dict(self.headers, Host="evil.example")
        self.assertEqual(
            (await self.client.get("/memory-ui/scopes", headers=wrong)).status_code, 403
        )
        no_csrf = {
            key: value for key, value in self.headers.items() if key != "X-CSRF-Token"
        }
        self.assertEqual(
            (
                await self.client.put(
                    "/memory-ui/layout",
                    json={"scope_id": self.m.scope},
                    headers=no_csrf,
                )
            ).status_code,
            403,
        )
        self.assertEqual(
            (
                await self.client.put(
                    "/memory-ui/layout",
                    json={"scope_id": self.m.scope, "viewer_id": B},
                    headers=self.headers,
                )
            ).status_code,
            400,
        )

    async def test_last_scope_preferences_are_authenticated_and_server_bound(self):
        await self.login()
        response = await self.client.put(
            "/memory-ui/preferences",
            json={"scope_id": self.m.scope},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 200)
        scopes = await (
            await self.client.get("/memory-ui/scopes", headers=self.headers)
        ).get_json()
        self.assertEqual(scopes["default_scope"], self.m.scope)
        response = await self.client.put(
            "/memory-ui/preferences",
            json={"scope_id": "unrelated"},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 404)
        response = await self.client.put(
            "/memory-ui/preferences",
            json={"scope_id": self.m.scope, "viewer_id": B},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 400)

    async def test_graph_details_layout_future_are_real_and_do_not_reinforce(self):
        await self.login()
        before = self.m.db.execute(
            "SELECT * FROM mv2_memories WHERE id=?", (self.mid,)
        ).fetchone()
        graph = await self.client.get(
            "/memory-ui/graph",
            query_string={"scope_id": self.m.scope, "future_days": "30"},
            headers=self.headers,
        )
        self.assertEqual(graph.status_code, 200)
        data = await graph.get_json()
        self.assertAlmostEqual(data["nodes"][0]["strength_now"], 2 ** (-30 / 180))
        self.assertEqual(graph.headers["Cache-Control"], "no-store")
        detail = await self.client.get(
            "/memory-ui/memories/" + self.mid,
            query_string={"scope_id": self.m.scope},
            headers=self.headers,
        )
        self.assertEqual((await detail.get_json())["value"], "不加糖")
        layout = {
            "scope_id": self.m.scope,
            "positions": {self.mid: {"x": 5, "y": -4, "locked": True}},
            "zoom": 1,
            "pan": {"x": 0, "y": 0},
        }
        self.assertEqual(
            (
                await self.client.put(
                    "/memory-ui/layout", json=layout, headers=self.headers
                )
            ).status_code,
            200,
        )
        loaded = await (
            await self.client.get(
                "/memory-ui/layout",
                query_string={"scope_id": self.m.scope},
                headers=self.headers,
            )
        ).get_json()
        self.assertEqual(loaded["layout"], layout)
        after = self.m.db.execute(
            "SELECT * FROM mv2_memories WHERE id=?", (self.mid,)
        ).fetchone()
        self.assertEqual(before, after)
        layout["positions"] = {"unknown-private-id": {"x": 1, "y": 1, "locked": False}}
        self.assertEqual(
            (
                await self.client.put(
                    "/memory-ui/layout", json=layout, headers=self.headers
                )
            ).status_code,
            400,
        )
        self.assertIn(
            "演示数据，未接入机器人",
            (
                await (await self.client.get("/", headers=self.headers)).get_data()
            ).decode(),
        )

    async def test_delete_removes_nodes_layout_detail_and_trace_and_bad_cursor(self):
        await self.login()
        self.m.recall("我喜欢什么奶茶", A)
        self.m.forget(A, self.mid)
        detail = await self.client.get(
            "/memory-ui/memories/" + self.mid,
            query_string={"scope_id": self.m.scope},
            headers=self.headers,
        )
        self.assertEqual(detail.status_code, 404)
        data = await (
            await self.client.get(
                "/memory-ui/graph",
                query_string={"scope_id": self.m.scope},
                headers=self.headers,
            )
        ).get_json()
        self.assertEqual(data["nodes"], [])
        self.assertEqual(data["edges"], [])
        trace = await (
            await self.client.get(
                "/memory-ui/traces",
                query_string={"scope_id": self.m.scope},
                headers=self.headers,
            )
        ).get_json()
        self.assertEqual(trace["traces"], [])
        self.assertEqual(
            (
                await self.client.get(
                    "/memory-ui/graph",
                    query_string={"scope_id": self.m.scope, "cursor": "1:0"},
                    headers=self.headers,
                )
            ).status_code,
            409,
        )


class MigrationTests(unittest.TestCase):
    setUp = v2_cases.V2Tests.setUp
    tearDown = v2_cases.V2Tests.tearDown
    make = v2_cases.V2Tests.make

    def test_unknown_legacy_provenance_is_candidate_and_idempotent(self):
        from src.storage import Store

        store = Store(self.path, self.memory.scope)
        store.remember("合成旧偏好")
        report = migrate_legacy(self.memory)
        self.assertEqual(report["explicit_quarantined"], 1)
        self.assertEqual(
            self.db.execute("SELECT status FROM mv2_memories").fetchone()[0],
            "candidate",
        )
        migrate_legacy(self.memory)
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) FROM mv2_memories").fetchone()[0], 1
        )
        self.assertEqual(
            self.db.execute(
                "SELECT COUNT(*) FROM memories WHERE scope=?", (self.memory.scope,)
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.db.execute(
                "SELECT COUNT(*) FROM ltm_journal WHERE scope=? AND body IS NOT NULL",
                (self.memory.scope,),
            ).fetchone()[0],
            0,
        )
        store.close()
