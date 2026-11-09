"""Injected-clock memory, privacy, leases and restore; synthetic identities only."""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from src.memory_v2.service import MemoryService
from src.memory_v2.config import validate_config
from src.memory_v2.decay import TestClock, DAY, strength, state_at

ROOT = Path(__file__).resolve().parents[1]
A, B, BOT = "12345001", "12345002", "1111111111"


class V2Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT / ".tmp")
        self.path = Path(self.tmp.name) / "state.sqlite3"
        self.db = sqlite3.connect(self.path)
        self.clock = TestClock()
        self.registry = Path(self.tmp.name) / "deletions.jsonl"
        self.memory = self.make("instance:bot:group:111111")
        self.seq = 0

    def make(self, scope):
        return MemoryService(
            self.db,
            scope,
            BOT,
            "instance",
            A,
            clock=self.clock,
            options=validate_config({"enabled": True, "mode": "active"}),
            registry=self.registry,
        )

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def source(
        self, text="我喜欢不加糖的奶茶", actor=A, subjects=None, eid=None, **kwargs
    ):
        self.seq += 1
        return self.memory.observe(
            dict(
                event_id=eid or str(self.seq),
                sender_id=actor,
                text=text,
                subject_ids=subjects or [actor],
                event_time=self.clock.now(),
                **kwargs,
            )
        )

    def item(self, source, value="不加糖", **changes):
        return dict(
            kind="semantic",
            subject_ids=[A],
            predicate="drink_preference",
            value=value,
            polarity="positive",
            assertion_mode="self_report",
            temporal_class="stable_preference",
            source_event_ids=[source],
            confidence_score=0.95,
            importance=0.6,
            **changes,
        )

    def add(
        self,
        text="我喜欢不加糖的奶茶",
        value="不加糖",
        *,
        actor=A,
        temporal="stable_preference",
        kind="semantic",
        predicate="drink_preference",
        assertion="self_report",
    ):
        source = self.source(text, actor)
        item = self.item(source, value)
        item.update(
            subject_ids=[actor],
            temporal_class=temporal,
            kind=kind,
            predicate=predicate,
            assertion_mode=assertion,
        )
        result = self.memory.apply(
            {"candidates": [item]}, self.memory.snapshot(), allowed_source_ids={source}
        )
        self.assertEqual(result["accepted"], 1)
        return result["ids"][0], source

    def test_curve_queries_and_maintenance_frequency_do_not_reanchor(self):
        mid, _ = self.add(
            "我那次在讨论排错", "讨论排错", temporal="episode", kind="episode"
        )
        row = self.memory.get(mid)
        for d, expected in [(7, 0.70710678), (14, 0.5), (28, 0.25)]:
            self.assertAlmostEqual(
                strength(row, row["anchor_at"] + d * DAY), expected, places=6
            )
        anchor = row["anchor_at"]
        for i in range(30):
            self.clock.advance(days=1)
            self.memory.maintain(self.memory.business_day())
        self.assertEqual(self.memory.get(mid)["anchor_at"], anchor)
        self.assertAlmostEqual(
            strength(self.memory.get(mid), self.clock.now()), 2 ** (-30 / 14)
        )

    def test_independent_evidence_grows_half_life_not_same_session_repetition(self):
        mid, source = self.add()
        for _ in range(100):
            self.memory.apply(
                {"candidates": [self.item(source)]}, self.memory.snapshot()
            )
        self.assertEqual(self.memory.get(mid)["independent_evidence_count"], 1)
        self.assertEqual(self.memory.get(mid)["half_life_days"], 180)
        self.clock.advance(days=2)
        same, _ = self.add()
        self.assertEqual(same, mid)
        row = self.memory.get(mid)
        self.assertAlmostEqual(row["half_life_days"], 216)
        self.assertEqual(row["independent_evidence_count"], 2)
        snapshot = row["anchor_at"], row["half_life_days"], row["confidence_score"]
        for _ in range(10):
            packet = self.memory.recall("我喜欢什么奶茶", A)
            self.memory.record_use(packet, "real-synthetic-receipt")
        row = self.memory.get(mid)
        self.assertEqual(
            snapshot, (row["anchor_at"], row["half_life_days"], row["confidence_score"])
        )

    def test_raw_expires_without_erasing_legal_preference_or_showing_old_quote(self):
        mid, sid = self.add()
        self.clock.advance(days=8)
        self.assertIsNone(self.memory.source(sid)["body"])
        self.memory.expire()
        self.assertIsNone(
            self.memory.db.execute(
                "SELECT body FROM mv2_sources WHERE id=?", (sid,)
            ).fetchone()[0]
        )
        self.assertTrue(self.memory.recall("我喜欢什么奶茶", A)["items"])
        self.assertEqual(self.memory.explain(A, mid)["source_state"], "expired")
        self.assertIsNone(self.memory.explain(A, mid)["evidence"][0]["excerpt"])

    def test_current_and_historical_versions_and_temporary_state(self):
        old, _ = self.add("我平时喝咖啡", "喝咖啡", predicate="drink_habit")
        old_time = self.clock.now()
        self.clock.advance(days=2)
        new, _ = self.add("我现在不喝咖啡", "不喝咖啡", predicate="drink_habit")
        self.assertEqual(self.memory.get(old)["status"], "superseded")
        current = self.memory.recall("我喝咖啡吗", A)
        self.assertIn(new, current["revisions"])
        self.assertNotIn(old, current["revisions"])
        history = self.memory.recall(
            "以前我喝咖啡吗", A, mode="history", query_time=old_time + 1
        )
        self.assertIn(old, history["revisions"])
        self.assertNotIn(new, history["revisions"])
        stable, _ = self.add()
        self.add("我今天不想喝奶茶", "不想喝奶茶", temporal="temporary_state")
        self.assertEqual(self.memory.get(stable)["status"], "active")

    def test_no_cross_scope_or_same_nickname_identity_merge(self):
        mid, _ = self.add()
        other = self.make("instance:bot:private:12345001")
        self.assertIsNone(other.get(mid))
        self.assertEqual(other.recall("我喜欢什么奶茶", A)["items"], [])
        self.assertIsNone(other.explain(A, mid, admin=True))
        self.assertIsNone(self.memory.explain(B, mid))

    def test_off_me_blocks_other_mentions_and_media_not_just_sender(self):
        self.source(actor=A, display_name="合成小甲")
        mid, _ = self.add()
        self.memory.forget(A, all_mine=True, off=True)
        self.assertIsNone(self.source("我认为甲喜欢咖啡", B, subjects=[B, A]))
        self.assertIsNone(self.source("我听到合成小甲说喜欢咖啡", B))
        self.assertIsNone(self.source("我在图片里看见甲", B, has_media=True))
        self.assertEqual(self.memory.get(mid)["value"], "")
        self.memory.on_me(A)
        self.assertIsNotNone(self.source())

    def test_stale_proposal_and_reply_are_invalid_after_delete(self):
        mid, sid = self.add()
        packet = self.memory.recall("我喜欢什么奶茶", A)
        snapshot = self.memory.snapshot()
        self.assertTrue(self.memory.validate_packet(packet))
        self.memory.forget(A, mid)
        self.assertFalse(self.memory.validate_packet(packet))
        self.assertFalse(self.memory.record_use(packet, "late"))
        self.assertEqual(
            self.memory.apply({"candidates": [self.item(sid)]}, snapshot)["accepted"], 0
        )
        self.assertIsNone(self.source(eid="1"))

    def test_deletion_propagates_gist_edges_index_traces_and_restore(self):
        mid, _ = self.add(
            "我那次讨论奶茶排错", "奶茶排错", temporal="episode", kind="episode"
        )
        gist = self.memory.derive_gist([mid])
        self.memory.recall("我记得那次奶茶排错", A, mode="history")
        backup = sqlite3.connect(Path(self.tmp.name) / "backup.sqlite3")
        self.db.backup(backup)
        self.memory.forget(A, mid)
        self.assertEqual(self.memory.get(gist)["value"], "")
        self.assertFalse(self.memory.traces())
        self.assertFalse(
            self.memory.db.execute(
                "SELECT 1 FROM mv2_fts WHERE scope_id=?", (self.memory.scope,)
            ).fetchone()
        )
        restored = MemoryService(
            backup,
            self.memory.scope,
            BOT,
            "instance",
            A,
            clock=self.clock,
            registry=self.registry,
        )
        restored.replay_registry()
        self.assertEqual(restored.get(mid)["value"], "")
        self.assertEqual(restored.get(gist)["value"], "")
        version = restored.snapshot()
        restored.replay_registry()
        self.assertEqual(restored.snapshot(), version)
        backup.close()
        self.assertNotIn("奶茶排错", self.registry.read_text(encoding="utf-8"))

    def test_invalid_model_candidates_rejected_without_affecting_chat_state(self):
        sid = self.source()
        changes = [
            {"subject_ids": [B]},
            {"source_event_ids": ["invented"]},
            {"value": "虚构经历"},
            {"confidence_score": float("nan")},
            {"protected": True},
            {"event_time": self.clock.now() + DAY},
            {"assertion_mode": "verified_action"},
        ]
        for change in changes:
            item = self.item(sid)
            item.update(change)
            self.assertEqual(
                self.memory.apply({"candidates": [item]}, self.memory.snapshot())[
                    "accepted"
                ],
                0,
            )
        self.assertIsNone(self.source("我的密码是 abcdef123456"))
        self.assertIsNone(self.source(source_kind="bot"))
        self.assertIsNone(self.source(quoted=True))

    def test_daily_tasks_leases_catchup_and_budget_no_llm_required(self):
        day = self.memory.business_day()
        first = self.memory.claim_task(day, "daily")
        self.assertIsNotNone(first)
        self.assertIsNone(self.memory.claim_task(day, "daily"))
        self.clock.advance(seconds=301)
        self.assertIsNotNone(self.memory.claim_task(day, "daily"))
        self.assertTrue(self.memory.reserve_learning("paid", 19000))
        self.assertFalse(self.memory.reserve_learning("over", 2000))
        self.add("我今天不想奶茶", "不想奶茶", temporal="temporary_state")
        self.clock.advance(days=8)
        self.assertEqual(len(self.memory.catch_up()), 7)
        self.assertEqual(self.memory.pending(), [])

    def test_history_dormant_and_deep_recall_expiry(self):
        mid, _ = self.add(
            "我那次写了排错日志", "排错日志", temporal="episode", kind="episode"
        )
        self.clock.advance(days=30)
        self.assertEqual(state_at(self.memory.get(mid), self.clock.now()), "dormant")
        self.assertNotIn(mid, self.memory.recall("排错日志", A)["revisions"])
        self.assertIn(
            mid, self.memory.recall("以前排错日志", A, mode="history")["revisions"]
        )
        self.clock.advance(days=30)
        self.assertNotIn(
            mid, self.memory.recall("排错日志", A, mode="history")["revisions"]
        )
        self.assertIn(
            mid,
            self.memory.recall("以前那次排错日志", A, mode="deep_recall")["revisions"],
        )
        self.clock.advance(days=31)
        self.assertNotIn(
            mid,
            self.memory.recall("以前那次排错日志", A, mode="deep_recall")["revisions"],
        )

    def test_graph_bounds_views_and_pin_do_not_change_curve(self):
        for i in range(16):
            self.add(f"我喜欢奶茶口味{i}", f"奶茶口味{i}", predicate=f"flavor{i}")
        degree = self.db.execute(
            "SELECT MAX(n) FROM (SELECT COUNT(*) n FROM mv2_edges GROUP BY src)"
        ).fetchone()[0]
        self.assertLessEqual(degree, 12)
        before = self.db.execute(
            "SELECT SUM(half_life_days),SUM(strength_at_anchor) FROM mv2_memories"
        ).fetchone()
        packet = self.memory.recall("我以前奶茶口味", A, graph_enabled=True)
        self.assertLessEqual(len(packet["items"]), 8)
        self.assertLessEqual(len(self.memory.traces()[0]["packet"]["seeds"]), 60)
        after = self.db.execute(
            "SELECT SUM(half_life_days),SUM(strength_at_anchor) FROM mv2_memories"
        ).fetchone()
        self.assertEqual(before, after)

    def test_config_rejects_unknown_and_nonfinite_settings(self):
        for bad in [
            {"imaginary": 1},
            {"output_tokens": float("inf")},
            {"embedding": "paid_external"},
            {"daily_learning_tokens": -1},
            {"graph_enabled": 1},
        ]:
            with self.assertRaises(ValueError):
                validate_config(bad)

    def test_third_party_does_not_override_person_and_inferences_need_sessions(self):
        mid, _ = self.add()
        sid = self.source("甲喜欢加糖咖啡", B, subjects=[A, B])
        item = self.item(sid, value="加糖咖啡")
        item.update(assertion_mode="third_party")
        result = self.memory.apply({"candidates": [item]}, self.memory.snapshot())
        self.assertEqual(self.memory.get(result["ids"][0])["status"], "candidate")
        self.assertEqual(self.memory.get(mid)["status"], "active")


if __name__ == "__main__":
    unittest.main()
