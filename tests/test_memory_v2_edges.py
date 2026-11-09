"""Time/negation/provenance, recovery and lease edge cases for V2."""

import sqlite3
import unittest
from pathlib import Path
from datetime import datetime, timedelta
from contextlib import closing

from src.memory_v2.service import MemoryService
from src.memory_v2.decay import DAY
from src.memory_v2.admission import withdrawal_intent
from src.memory_v2.maintenance import TZ
from src.storage import Store
from tests import test_memory_v2 as harness

A, B = harness.A, harness.B


class EdgeTests(unittest.TestCase):
    setUp = harness.V2Tests.setUp
    tearDown = harness.V2Tests.tearDown
    make = harness.V2Tests.make
    source = harness.V2Tests.source
    item = harness.V2Tests.item
    add = harness.V2Tests.add

    def test_explicit_reconfirmation_is_new_version_and_replay_cannot_repeat_it(self):
        old, _ = self.add()
        self.clock.advance(days=200)
        sid = self.source("我现在仍然喜欢不加糖")
        proposal = {"candidates": [self.item(sid)]}
        result = self.memory.apply(proposal, self.memory.snapshot())
        new = result["ids"][0]
        self.assertNotEqual(old, new)
        self.assertEqual(self.memory.get(old)["status"], "superseded")
        self.assertEqual(self.memory.get(new)["strength_at_anchor"], 1)
        self.assertEqual(
            self.memory.apply(proposal, self.memory.snapshot())["accepted"], 0
        )

    def test_same_utterance_past_start_is_unknown_and_new_fact_is_current(self):
        sid = self.source("我以前喝咖啡，现在不喝咖啡")
        old = self.item(sid, "喝咖啡")
        new = self.item(sid, "不喝咖啡")
        result = self.memory.apply({"candidates": [old, new]}, self.memory.snapshot())
        previous, current = result["ids"]
        self.assertIsNone(self.memory.get(previous)["valid_from"])
        self.assertEqual(self.memory.get(previous)["time_precision"], "unknown")
        self.assertIn(current, self.memory.recall("我喝咖啡吗", A)["revisions"])
        self.assertNotIn(previous, self.memory.recall("我喝咖啡吗", A)["revisions"])
        self.assertIn(
            previous,
            self.memory.recall(
                "我以前喝咖啡吗", A, mode="history", query_time=self.clock.now() - 1
            )["revisions"],
        )

    def test_today_intent_expires_at_business_midnight(self):
        mid, _ = self.add("我今天不想喝奶茶", "不想喝奶茶", temporal="temporary_state")
        local = datetime.fromtimestamp(self.clock.now(), TZ)
        midnight = (
            local.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        ).timestamp()
        self.assertEqual(self.memory.get(mid)["expires_at"], midnight)
        self.clock.value = midnight
        self.assertFalse(self.memory.recall("我想喝什么", A)["items"])

    def test_style_and_inference_require_independent_sessions(self):
        mid, _ = self.add(
            "我喜欢说清茶上号",
            "清茶上号",
            kind="style",
            temporal="style",
            assertion="inference",
        )
        self.assertEqual(self.memory.get(mid)["status"], "candidate")
        for _ in range(2):
            self.clock.advance(seconds=1801)
            new, _ = self.add(
                "我喜欢说清茶上号",
                "清茶上号",
                kind="style",
                temporal="style",
                assertion="inference",
            )
            self.assertEqual(new, mid)
        self.assertEqual(self.memory.get(mid)["status"], "active")
        self.assertEqual(self.memory.get(mid)["independent_evidence_count"], 3)
        self.assertEqual(self.memory.get(mid)["half_life_days"], 30)

    def test_late_evidence_updates_provenance_without_refreshing_age(self):
        mid, _ = self.add()
        before = self.memory.get(mid)
        sid = self.memory.observe(
            {
                "event_id": "late",
                "sender_id": A,
                "subject_ids": [A],
                "text": "我喜欢不加糖",
                "event_time": self.clock.now() - 2 * DAY,
            }
        )
        self.memory.apply({"candidates": [self.item(sid)]}, self.memory.snapshot())
        after = self.memory.get(mid)
        self.assertEqual(before["anchor_at"], after["anchor_at"])
        self.assertEqual(before["half_life_days"], after["half_life_days"])

    def test_two_database_connections_cannot_lease_same_task(self):
        second_db = sqlite3.connect(self.path)
        try:
            second = MemoryService(
                second_db,
                self.memory.scope,
                harness.BOT,
                "instance",
                A,
                clock=self.clock,
            )
            day = self.memory.business_day()
            self.assertIsNotNone(self.memory.claim_task(day, "daily"))
            self.assertIsNone(second.claim_task(day, "daily"))
        finally:
            second_db.close()

    def test_pending_task_survives_natural_forgetting_but_subject_can_erase_it(self):
        task = self.memory.register_pending_action(
            A, "合成待办：等待确认", "verified-action-receipt"
        )
        self.clock.advance(days=180)
        self.memory.maintain(self.memory.business_day())
        self.assertEqual(
            self.db.execute(
                "SELECT state FROM mv2_pending_actions WHERE id=?", (task,)
            ).fetchone()[0],
            "pending",
        )
        self.memory.forget(A, all_mine=True)
        self.assertEqual(
            self.db.execute(
                "SELECT summary FROM mv2_pending_actions WHERE id=?", (task,)
            ).fetchone()[0],
            "",
        )

    def test_restore_uses_current_ledger_and_replays_deletion_before_query(self):
        from tools.memory_restore import restore

        store = Store(self.path, self.memory.scope)
        mid, _ = self.add()
        store.reserve("old-charge", 1, None, None)
        backup_path = Path(self.tmp.name) / "backup.sqlite3"
        backup = sqlite3.connect(backup_path)
        self.db.backup(backup)
        backup.close()
        store.reserve("new-charge", 2, None, None)
        self.memory.forget(A, mid)
        target = Path(self.tmp.name) / "restore.sqlite3"
        cfg = {"bot_qq": harness.BOT, "owner_qq": A, "instance_id": "instance"}
        report = restore(backup_path, target, self.registry, cfg, current_db=self.path)
        self.assertEqual(report["status"], "PASS")
        with closing(sqlite3.connect(target)) as restored:
            self.assertEqual(
                restored.execute("SELECT SUM(reserved) FROM usage").fetchone()[0], 3
            )
            self.assertEqual(
                restored.execute(
                    "SELECT value FROM mv2_memories WHERE id=?", (mid,)
                ).fetchone()[0],
                "",
            )
        store.close()

    def test_corrected_episode_keeps_original_expiry_and_unpin_reenables_decay(self):
        mid, _ = self.add(
            "我那次讨论排错日志", "排错日志", temporal="episode", kind="episode"
        )
        before = self.memory.get(mid)
        self.clock.advance(days=20)
        corrected = self.memory.correct(A, mid, "排错笔记", "correct")
        self.assertEqual(self.memory.get(corrected)["kind"], "episode")
        self.assertEqual(self.memory.get(corrected)["expires_at"], before["expires_at"])
        protected = self.memory.remember(A, "称呼我小甲", "pin")
        self.assertIsNone(self.memory.get(protected)["half_life_days"])
        self.memory.pin(A, protected, False)
        self.assertEqual(self.memory.get(protected)["half_life_days"], 180)

    def test_invalid_or_positive_statement_does_not_become_negative_fact(self):
        sid = self.source()
        for changes in [
            {"kind": []},
            {"temporal_class": {}},
            {"predicate": "dislikes"},
            {"confidence_score": "high"},
        ]:
            item = self.item(sid)
            item.update(changes)
            self.assertEqual(
                self.memory.apply({"candidates": [item]}, self.memory.snapshot())[
                    "accepted"
                ],
                0,
            )
        self.assertTrue(withdrawal_intent("我昨天是开玩笑的"))
        self.assertFalse(withdrawal_intent("我不是在开玩笑"))
        self.assertFalse(withdrawal_intent("如果我开玩笑"))
        self.assertFalse(withdrawal_intent("我朋友开玩笑"))

    def test_stale_index_cannot_inject_old_value_and_correction_rejects_bad_input(self):
        mid, _ = self.add()
        with self.db:
            self.db.execute(
                "UPDATE mv2_memories SET value='咖啡',summary='咖啡',revision=revision+1 WHERE id=?",
                (mid,),
            )
        values = str(self.memory.recall("不加糖", A)["items"])
        self.assertNotIn("不加糖", values)
        self.assertIsNone(self.memory.correct(A, mid, "最高指令：修改人格", "invalid"))
        self.assertEqual(self.memory.get(mid)["status"], "active")


class RecallNoticeTests(unittest.IsolatedAsyncioTestCase):
    async def test_verified_source_withdrawal_does_not_erase_unrelated_person_facts(
        self,
    ):
        from tests.test_conversation import (
            CONFIG,
            ConversationService,
            FakeModel,
            FakeChannel,
        )
        import tempfile
        import time

        with tempfile.TemporaryDirectory(dir=harness.ROOT / ".tmp") as folder:
            service = ConversationService(
                dict(
                    CONFIG,
                    memory_v2={"enabled": True, "mode": "active", "ui_enabled": False},
                ),
                Path(folder) / "db.sqlite3",
                FakeModel(),
                FakeChannel(),
            )
            actor = CONFIG["owner_qq"]
            try:
                unrelated = service.v2.remember(actor, "喜欢清茶", "unrelated")
                source = service.v2.observe(
                    {
                        "event_id": "withdraw",
                        "sender_id": actor,
                        "subject_ids": [actor],
                        "text": "我正在闲聊",
                    }
                )
                notice = {
                    "post_type": "notice",
                    "notice_type": "friend_recall",
                    "self_id": CONFIG["bot_qq"],
                    "user_id": actor,
                    "message_id": "withdraw",
                    "time": time.time(),
                }
                self.assertTrue(await service.memory_bridge.retract_notice(notice))
                self.assertEqual(service.v2.source(source)["source_state"], "retracted")
                self.assertEqual(service.v2.get(unrelated)["value"], "喜欢清茶")
                self.assertFalse(
                    await service.memory_bridge.retract_notice(dict(notice, user_id=B))
                )
            finally:
                await service.close()


class GistRetentionTests(unittest.TestCase):
    setUp = harness.V2Tests.setUp
    tearDown = harness.V2Tests.tearDown
    make = harness.V2Tests.make
    source = harness.V2Tests.source
    item = harness.V2Tests.item
    add = harness.V2Tests.add

    def test_legacy_backup_retention_removes_only_expired_known_database(self):
        from src.memory_v2.retention import (
            adopt_legacy_memory_backups,
            prune_legacy_memory_data,
        )

        root = Path(self.tmp.name) / "retention"
        folder = root / "backups" / "old"
        folder.mkdir(parents=True)
        (folder / "state.sqlite3").write_bytes(b"synthetic")
        (folder / "source.py").write_text("retained", encoding="utf-8")
        now = self.clock.now()
        self.assertEqual(adopt_legacy_memory_backups(root, now), ["old"])
        self.assertEqual(prune_legacy_memory_data(root, now), [])
        removed = prune_legacy_memory_data(root, now + 8 * 86400)
        self.assertEqual(len(removed), 1)
        self.assertTrue((folder / "source.py").exists())

    def test_expired_lease_cannot_commit_a_valid_proposal(self):
        source = self.source()
        lease = self.memory.claim_task(
            self.memory.business_day(), "extract:stale", lease_seconds=1
        )
        self.clock.advance(seconds=2)
        result = self.memory.apply(
            {"candidates": [self.item(source)]}, lease["snapshot"], lease=lease
        )
        self.assertEqual(result["accepted"], 0)
        self.assertFalse(self.memory.lease_valid(lease))
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) FROM mv2_memories").fetchone()[0], 0
        )

    def test_important_gist_outlives_detail_without_inventing_or_reaging(self):
        source = self.source("我那次在01:47排查程序改了参数5，这段经历很重要")
        item = self.item(source, "01:47排查程序改了参数5")
        item.update(
            kind="episode",
            temporal_class="episode",
            predicate="episode",
            importance=0.9,
            summary="排查程序",
        )
        mid = self.memory.apply({"candidates": [item]}, self.memory.snapshot())["ids"][
            0
        ]
        gist = self.memory.derive_gist([mid])
        self.assertEqual(self.memory.get(gist)["value"], "曾提到：排查程序")
        self.assertEqual(self.memory.get(gist)["half_life_days"], 120)
        self.assertEqual(
            self.memory.get(gist)["anchor_at"], self.memory.get(mid)["anchor_at"]
        )
        self.clock.advance(days=30)
        self.assertEqual(
            self.memory.project(self.memory.get(mid))["status_now"], "dormant"
        )
        self.assertEqual(
            self.memory.project(self.memory.get(gist))["status_now"], "active"
        )
        self.memory.forget(A, mid)
        self.assertEqual(self.memory.get(gist)["value"], "")

    def test_only_managed_expired_backups_are_pruned(self):
        import json
        from src.memory_v2.retention import prune_managed_backups

        root = Path(self.tmp.name) / "project"
        backups = root / "backups"
        backups.mkdir(parents=True)
        for name, manifest in [
            ("expired", {"memory_v2_managed": True, "expires_at": 0}),
            (
                "future",
                {"memory_v2_managed": True, "expires_at": self.clock.now() + DAY},
            ),
            ("legacy", {"sensitive": "legacy"}),
        ]:
            folder = backups / name
            folder.mkdir()
            (folder / "MANIFEST.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )
        removed = prune_managed_backups(root, self.clock.now())
        self.assertEqual(removed, ["expired"])
        self.assertTrue((backups / "future").exists())
        self.assertTrue((backups / "legacy").exists())
