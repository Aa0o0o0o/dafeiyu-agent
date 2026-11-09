"""Synthetic owner-scope/import/revocation contracts; no real WeChat data."""

import tempfile
import time
import unittest
from pathlib import Path
from src.wechat_private_store import AccessDenied, StaleSnapshot, WeChatPrivateStore

ROOT = Path(__file__).resolve().parents[1]
SCOPE = "fixture:1111111111:2222222222"
AUTH = {"scope": SCOPE, "actor": "2222222222", "verified": True}


class WeChatPrivateStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT / ".tmp")
        self.root = Path(self.tmp.name)
        self.args = dict(
            owner_scope=SCOPE,
            owner_qq="2222222222",
            account_id="synthetic-owner",
            conversation_id="synthetic-conversation",
            owner_sender_id="self-wx",
            contact_sender_id="other-wx",
        )
        self.store = WeChatPrivateStore(self.root, **self.args)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def enable(self):
        self.store.set_access(**AUTH, enabled=True)
        return self.store.snapshot(**AUTH)

    def msg(
        self, mid="m1", sender="self-wx", text="我喜欢吃海苔", days=20, kind="text"
    ):
        return {
            "conversation_id": "synthetic-conversation",
            "message_id": mid,
            "sender_id": sender,
            "occurred_at": time.time() - days * 86400,
            "kind": kind,
            "text": text,
        }

    def test_default_disabled_and_verified_owner_only(self):
        with self.assertRaises(AccessDenied):
            self.store.snapshot(**AUTH)
        for bad in [
            dict(AUTH, actor="4444444444"),
            dict(AUTH, scope="fixture:1111111111:group:3333333333"),
            dict(AUTH, verified=False),
            dict(AUTH, verified=1),
        ]:
            with self.assertRaises(AccessDenied):
                self.store.set_access(**bad, enabled=True)
        e = self.enable()
        for bad in [
            dict(AUTH, actor="4444444444"),
            dict(AUTH, scope="fixture:1111111111:group:3333333333"),
            dict(AUTH, verified=False),
        ]:
            with self.assertRaises(AccessDenied):
                self.store.search(**bad, query="", snapshot=e)

    def test_import_old_messages_preserves_subject_time_and_media_limitations(self):
        e = self.enable()
        first = self.msg()
        other = self.msg("m2", "other-wx", "我喜欢听歌", days=40)
        image = self.msg("m3", "other-wx", "", days=3, kind="image")
        result = self.store.import_batch(
            **AUTH, batch_id="batch-a", messages=[first, other, image], snapshot=e
        )
        self.assertEqual(result["imported"], 3)
        self.assertFalse(self.store.validate_snapshot(**AUTH, snapshot=e))
        e = self.store.snapshot(**AUTH)
        rows = self.store.search(**AUTH, query="", snapshot=e)
        self.assertEqual({m["sender_role"] for m in rows}, {"owner", "contact"})
        self.assertEqual(
            next(m for m in rows if m["message_id"] == "m1")["occurred_at"],
            first["occurred_at"],
        )
        self.assertEqual(next(m for m in rows if m["message_id"] == "m3")["text"], "")
        self.assertEqual(len(self.store.search(**AUTH, query="海苔", snapshot=e)), 1)

    def test_reject_untrusted_conversation_unknown_sender_and_bad_times_atomically(
        self,
    ):
        e = self.enable()
        good = self.msg()
        other = dict(self.msg("m2"), conversation_id="different-conversation")
        for bad in [
            other,
            dict(self.msg("m2"), sender_id="third-party"),
            dict(self.msg("m2"), occurred_at=float("nan")),
            dict(self.msg("m2"), kind="audio", text="imagined transcript"),
        ]:
            with self.assertRaises(ValueError):
                self.store.import_batch(
                    **AUTH, batch_id="bad", messages=[good, bad], snapshot=e
                )
            self.assertEqual(self.store.search(**AUTH, query="", snapshot=e), [])

    def test_dedup_changed_source_rejection_and_bounded_query(self):
        e = self.enable()
        m = self.msg()
        self.store.import_batch(**AUTH, batch_id="a", messages=[m], snapshot=e)
        e = self.store.snapshot(**AUTH)
        self.assertEqual(
            self.store.import_batch(**AUTH, batch_id="b", messages=[m], snapshot=e)[
                "duplicates"
            ],
            1,
        )
        with self.assertRaises(ValueError):
            self.store.import_batch(
                **AUTH, batch_id="c", messages=[dict(m, text="changed")], snapshot=e
            )
        for n in [True, 0, 21]:
            with self.assertRaises(ValueError):
                self.store.search(**AUTH, query="", snapshot=e, limit=n)

    def test_redaction_revoke_and_no_resurrection(self):
        e = self.enable()
        secret = self.msg(text="密码：abc123")
        self.assertEqual(
            self.store.import_batch(
                **AUTH, batch_id="a", messages=[secret], snapshot=e
            )["redacted"],
            1,
        )
        e = self.store.snapshot(**AUTH)
        rows = self.store.search(**AUTH, query="", snapshot=e)
        self.assertEqual(rows[0]["text"], "[敏感内容已排除]")
        self.assertNotIn("abc123", str(rows))
        self.assertEqual(self.store.revoke_batch(**AUTH, batch_id="a", snapshot=e), 1)
        self.assertFalse(self.store.validate_snapshot(**AUTH, snapshot=e))
        with self.assertRaises(StaleSnapshot):
            self.store.search(**AUTH, query="", snapshot=e)
        e = self.store.snapshot(**AUTH)
        self.assertEqual(self.store.search(**AUTH, query="", snapshot=e), [])
        with self.assertRaises(ValueError):
            self.store.import_batch(**AUTH, batch_id="b", messages=[secret], snapshot=e)

    def test_disable_across_connections_and_clear_invalidate_guards(self):
        e = self.enable()
        self.store.import_batch(**AUTH, batch_id="a", messages=[self.msg()], snapshot=e)
        e = self.store.snapshot(**AUTH)
        other = WeChatPrivateStore(self.root, **self.args)
        try:
            other.set_access(**AUTH, enabled=False)
            self.assertFalse(self.store.validate_snapshot(**AUTH, snapshot=e))
            with self.assertRaises(AccessDenied):
                self.store.search(**AUTH, query="", snapshot=e)
            other.set_access(**AUTH, enabled=True)
            new = self.store.snapshot(**AUTH)
            self.assertNotEqual(new, e)
            self.assertEqual(self.store.clear(**AUTH, snapshot=new), 1)
            self.assertFalse(other.validate_snapshot(**AUTH, snapshot=new))
        finally:
            other.close()

    def test_constructor_cannot_rebind_conversation_to_other_senders(self):
        with self.assertRaises(ValueError):
            WeChatPrivateStore(
                self.root, **dict(self.args, contact_sender_id="different")
            )
        with self.assertRaises(ValueError):
            WeChatPrivateStore(
                self.root, **dict(self.args, owner_scope="fixture:group:2222222222")
            )

    def test_other_conversation_cannot_be_added_to_same_store(self):
        with self.assertRaises(ValueError):
            WeChatPrivateStore(
                self.root, **dict(self.args, conversation_id="different-conversation")
            )
        self.assertEqual(
            self.store.db.execute("SELECT COUNT(*) FROM wx_private_control").fetchone()[
                0
            ],
            1,
        )

    def test_unattributed_ocr_has_unknown_sender_and_time(self):
        e = self.enable()
        msg = dict(
            self.msg(), sender_id="unknown", occurred_at=None, text="普通可见文字"
        )
        self.store.import_batch(
            **AUTH, batch_id="ocr-batch", messages=[msg], snapshot=e
        )
        e = self.store.snapshot(**AUTH)
        row = self.store.search(**AUTH, query="", snapshot=e)[0]
        self.assertEqual(row["sender_role"], "unknown")
        self.assertIsNone(row["occurred_at"])

    def test_source_kind_is_per_batch_and_old_schema_migrates_as_ocr(self):
        epoch = self.enable()
        self.store.import_batch(
            **AUTH, batch_id="old", messages=[self.msg()], snapshot=epoch
        )
        self.store.close()
        import sqlite3

        db = sqlite3.connect(self.root / "runtime/owner-private/wechat.sqlite3")
        db.execute("ALTER TABLE wx_private_batch RENAME TO wx_private_batch_new")
        db.execute(
            "CREATE TABLE wx_private_batch(scope TEXT,conversation TEXT,batch TEXT,state TEXT,PRIMARY KEY(scope,conversation,batch))"
        )
        db.execute(
            "INSERT INTO wx_private_batch SELECT scope,conversation,batch,state FROM wx_private_batch_new"
        )
        db.execute("DROP TABLE wx_private_batch_new")
        db.commit()
        db.close()
        self.store = WeChatPrivateStore(self.root, **self.args)
        epoch = self.store.snapshot(**AUTH)
        self.assertEqual(
            self.store.search(**AUTH, query="", snapshot=epoch)[0]["source_kind"],
            "visible_screen_ocr",
        )
        with self.assertRaises(ValueError):
            self.store.import_batch(
                **AUTH,
                batch_id="bad",
                messages=[self.msg("bad")],
                snapshot=epoch,
                source_kind="other",
            )
        self.store.import_batch(
            **AUTH,
            batch_id="db",
            messages=[self.msg("db")],
            snapshot=epoch,
            source_kind="verified_local_db",
        )
        epoch = self.store.snapshot(**AUTH)
        by_id = {
            r["message_id"]: r["source_kind"]
            for r in self.store.search_relevant(**AUTH, query="海苔", snapshot=epoch)
        }
        self.assertEqual(by_id, {"m1": "visible_screen_ocr", "db": "verified_local_db"})

    def test_long_text_chunk_roundtrip_boundary_search_and_revoke(self):
        epoch = self.enable()
        text = "甲" * 2998 + "芝麻糖" + "乙" * 4200 + "尾部  "
        msg = self.msg("long", "other-wx", text)
        self.store.import_batch(
            **AUTH,
            batch_id="long-batch",
            messages=[msg],
            snapshot=epoch,
            source_kind="verified_local_db",
        )
        epoch = self.store.snapshot(**AUTH)
        row = self.store.search(**AUTH, query="芝麻糖", snapshot=epoch)[0]
        self.assertEqual(row["text"], text)
        ranked = self.store.search_relevant(**AUTH, query="芝麻糖", snapshot=epoch)
        self.assertEqual(ranked[0]["text"], text)
        self.assertGreater(
            self.store.db.execute("SELECT COUNT(*) FROM wx_private_chunk").fetchone()[
                0
            ],
            1,
        )
        self.store.revoke_batch(**AUTH, batch_id="long-batch", snapshot=epoch)
        self.assertEqual(
            self.store.db.execute("SELECT COUNT(*) FROM wx_private_chunk").fetchone()[
                0
            ],
            0,
        )
        epoch = self.store.snapshot(**AUTH)
        self.assertEqual(self.store.search(**AUTH, query="", snapshot=epoch), [])
        with self.assertRaises(ValueError):
            self.store.import_batch(
                **AUTH, batch_id="again", messages=[msg], snapshot=epoch
            )

    def test_sensitive_marker_crossing_chunk_boundary_redacts_whole_text(self):
        epoch = self.enable()
        text = "甲" * 2999 + "密" + "码:synthetic_secret " + "乙" * 3100
        result = self.store.import_batch(
            **AUTH,
            batch_id="sensitive",
            messages=[self.msg("long", text=text)],
            snapshot=epoch,
        )
        self.assertEqual(result["redacted"], 1)
        epoch = self.store.snapshot(**AUTH)
        row = self.store.search(**AUTH, query="", snapshot=epoch)[0]
        self.assertEqual(row["text"], "[敏感内容已排除]")
        self.assertEqual(
            self.store.db.execute("SELECT COUNT(*) FROM wx_private_chunk").fetchone()[
                0
            ],
            0,
        )
        self.assertNotIn(
            "synthetic_secret",
            repr(
                self.store.db.execute("SELECT body FROM wx_private_message").fetchall()
            ),
        )

    def test_corrupt_or_missing_long_text_part_fails_closed(self):
        epoch = self.enable()
        self.store.import_batch(
            **AUTH,
            batch_id="long",
            messages=[self.msg("long", text="甲" * 3100)],
            snapshot=epoch,
        )
        epoch = self.store.snapshot(**AUTH)
        self.store.db.execute("DELETE FROM wx_private_chunk WHERE chunk_index=1")
        self.store.db.commit()
        with self.assertRaises(ValueError):
            self.store.search(**AUTH, query="", snapshot=epoch)

    def test_search_relevant_can_find_old_record_beyond_recent_twenty(self):
        epoch = self.enable()
        old = self.msg("old", "other-wx", "我特别喜欢吃芝麻糖", days=90)
        recent = [
            self.msg(f"new-{i}", "self-wx", f"无关记录{i}", days=1) for i in range(35)
        ]
        self.store.import_batch(
            **AUTH, batch_id="large", messages=[old, *recent], snapshot=epoch
        )
        epoch = self.store.snapshot(**AUTH)
        self.assertNotIn(
            "old",
            [
                m["message_id"]
                for m in self.store.search(**AUTH, query="", snapshot=epoch, limit=20)
            ],
        )
        rows = self.store.search_relevant(
            **AUTH,
            query="合成联系人以前提过芝麻糖吗",
            aliases=("合成联系人",),
            snapshot=epoch,
        )
        self.assertEqual(len(rows), 20)
        self.assertEqual(rows[0]["message_id"], "old")
        self.assertEqual(rows[0]["sender_role"], "contact")
        with self.assertRaises(AccessDenied):
            self.store.search_relevant(
                **dict(AUTH, scope="fixture:1111111111:group:3333333333"),
                query="芝麻糖",
                snapshot=epoch,
            )
        self.store.revoke_batch(**AUTH, batch_id="large", snapshot=epoch)
        with self.assertRaises(StaleSnapshot):
            self.store.search_relevant(**AUTH, query="芝麻糖", snapshot=epoch)

    def test_overview_and_topic_context_use_full_import_and_preserve_scope(self):
        epoch = self.enable()
        now = time.time() - 90 * 86400
        earlier = dict(self.msg("before", "self-wx", "你刚才说什么"), occurred_at=now)
        match = dict(self.msg("match", "other-wx", "我想吃芝麻糖"), occurred_at=now + 4)
        after = dict(self.msg("after", "self-wx", "下次一起去"), occurred_at=now + 8)
        distant = dict(self.msg("distant", "self-wx", "不相关旧话题"), occurred_at=now + 7200)
        newer = [self.msg(f"new-{i}", text=f"无关记录{i}", days=1) for i in range(30)]
        self.store.import_batch(**AUTH, batch_id="full", snapshot=epoch,
                                source_kind="verified_local_db",
                                messages=[earlier, match, after, distant, *newer])
        epoch = self.store.snapshot(**AUTH)
        overview = self.store.history_overview(**AUTH, snapshot=epoch)
        self.assertEqual(overview["total"], 34)
        self.assertEqual(overview["types"]["text"], 34)
        result = self.store.search_with_context(**AUTH, query="合成联系人提过芝麻糖吗",
                                                aliases=("合成联系人",), snapshot=epoch)
        items = {row["message_id"]: row for row in result}
        self.assertEqual(items["match"]["selection"], "match")
        self.assertEqual(items["before"]["selection"], "nearby")
        self.assertEqual(items["after"]["selection"], "nearby")
        self.assertNotIn("distant", items)
        self.assertEqual(items["before"]["context_group"], items["match"]["context_group"])
        with self.assertRaises(AccessDenied):
            self.store.history_overview(**dict(AUTH, actor="other"), snapshot=epoch)
        self.store.revoke_batch(**AUTH, batch_id="full", snapshot=epoch)
        with self.assertRaises(StaleSnapshot):
            self.store.search_with_context(**AUTH, query="芝麻糖", snapshot=epoch)

    def test_empty_generic_query_falls_back_to_recent_with_bounded_limit(self):
        epoch = self.enable()
        self.store.import_batch(
            **AUTH,
            batch_id="small",
            messages=[self.msg("a"), self.msg("b")],
            snapshot=epoch,
        )
        epoch = self.store.snapshot(**AUTH)
        self.assertEqual(
            len(
                self.store.search_relevant(
                    **AUTH,
                    query="合成联系人最近说了什么",
                    aliases=("合成联系人",),
                    snapshot=epoch,
                    limit=1,
                    recent=1,
                )
            ),
            1,
        )
