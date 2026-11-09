"""Pure in-memory WeChat source fixtures and temporary synthetic destination."""

import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from src.wechat_import_fence import ImportCancelled, ImportFence
from src.wechat_private_store import WeChatPrivateStore
from src.wechat_single_session import conversation_table
from src.wechat_synthetic_import import (
    SyntheticShard,
    _AtomicStoreStage,
    import_single_synthetic_session,
)

ROOT = Path(__file__).resolve().parents[1]
AUTH = dict(scope="fixture:1111111111:2222222222", actor="2222222222", verified=True)


class SyntheticImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT / ".tmp")
        self.store = WeChatPrivateStore(
            self.tmp.name,
            owner_scope=AUTH["scope"],
            owner_qq=AUTH["actor"],
            account_id="fake-self",
            conversation_id="fake-contact",
            owner_sender_id="fake-self",
            contact_sender_id="fake-contact",
        )
        self.store.set_access(**AUTH, enabled=True)
        self.fence = ImportFence(self.tmp.name)
        self.fence.enable()
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute(
            "CREATE TABLE contact(username TEXT,alias TEXT,nick_name TEXT,remark TEXT)"
        )
        self.conn.executemany(
            "INSERT INTO contact VALUES (?,?,?,?)",
            [
                ("fake-self", "display-self", "me", ""),
                ("fake-contact", "display-contact", "friend", "exact label"),
            ],
        )
        self.conn.commit()
        self.conn.execute("PRAGMA query_only=ON")
        self.sources = []

    def tearDown(self):
        self.store.close()
        self.conn.close()
        for conn in self.sources:
            conn.close()
        self.tmp.cleanup()

    def shard(self, name, owner_id, contact_id, rows):
        conn = sqlite3.connect(":memory:")
        self.sources.append(conn)
        conn.execute("CREATE TABLE Name2Id(user_name TEXT)")
        conn.execute(
            "INSERT INTO Name2Id(rowid,user_name) VALUES(?,?)", (owner_id, "fake-self")
        )
        conn.execute(
            "INSERT INTO Name2Id(rowid,user_name) VALUES(?,?)",
            (contact_id, "fake-contact"),
        )
        table = conversation_table("fake-contact")
        conn.execute(
            f'CREATE TABLE "{table}" (local_id INTEGER,local_type INTEGER,server_id INTEGER,real_sender_id INTEGER,create_time INTEGER,message_content TEXT,packed_info_data BLOB,sort_seq INTEGER)'
        )
        conn.execute(
            f'CREATE INDEX ordering ON "{table}" (sort_seq,create_time,local_id)'
        )
        conn.executemany(f'INSERT INTO "{table}" VALUES (?,?,?,?,?,?,?,?)', rows)
        other = conversation_table("unrelated")
        conn.execute(f'CREATE TABLE "{other}" (private TEXT)')
        conn.execute(f'INSERT INTO "{other}" VALUES (?)', ("must-never-import",))
        conn.commit()
        conn.execute("PRAGMA query_only=ON")
        return SyntheticShard(name, conn)

    def run_import(self, shards, **overrides):
        args = dict(
            contact_conn=self.conn,
            shards=shards,
            contact_label="exact label",
            verified_owner="fake-self",
            owner_display_id="display-self",
            target_display_id="display-contact",
            store=self.store,
            auth=AUTH,
            snapshot=self.store.snapshot(**AUTH),
            batch_id="synthetic",
            fence=self.fence,
            token=self.fence.token(),
            page_size=1,
        )
        args.update(overrides)
        import_single_synthetic_session(**args)

    def test_per_shard_mapping_unknown_and_no_unrelated_import(self):
        a = self.shard("a", 1, 2, [(1, 1, 100, 1, 1600000000, "self text", None, 1)])
        b = self.shard(
            "b",
            2,
            1,
            [
                (1, 1, 101, 1, 1600000001, "contact text", None, 2),
                (2, 1, 102, 99, 1600000002, "unknown text", None, 3),
            ],
        )
        self.run_import([a, b])
        rows = self.store.search(**AUTH, query="", snapshot=self.store.snapshot(**AUTH))
        self.assertEqual(
            {r["text"]: r["sender_role"] for r in rows},
            {
                "self text": "owner",
                "contact text": "contact",
                "unknown text": "unknown",
            },
        )
        self.assertTrue(all(r["source_kind"] == "verified_local_db" for r in rows))
        self.assertNotIn("must-never-import", repr(rows))

    def test_duplicate_contact_rejected_and_no_write(self):
        self.conn.execute("PRAGMA query_only=OFF")
        self.conn.execute(
            "INSERT INTO contact VALUES (?,?,?,?)",
            ("other-id", "display-contact", "", "exact label"),
        )
        self.conn.commit()
        self.conn.execute("PRAGMA query_only=ON")
        shard = self.shard("a", 1, 2, [(1, 1, 100, 1, 1600000000, "one", None, 1)])
        with self.assertRaisesRegex(ValueError, "not unique"):
            self.run_import([shard])
        self.assertEqual(
            self.store.search(**AUTH, query="", snapshot=self.store.snapshot(**AUTH)),
            [],
        )

    def update_contacts(self, sql, params=()):
        self.conn.execute("PRAGMA query_only=OFF")
        self.conn.execute(sql, params)
        self.conn.commit()
        self.conn.execute("PRAGMA query_only=ON")

    def test_same_label_different_alias_still_selects_unique_target(self):
        self.update_contacts(
            "INSERT INTO contact VALUES (?,?,?,?)",
            ("other-id", "other-display", "", "exact label"),
        )
        shard = self.shard("a", 1, 2, [(1, 1, 100, 2, 1600000000, "ok", None, 1)])
        self.run_import([shard])
        self.assertEqual(
            self.store.search(**AUTH, query="", snapshot=self.store.snapshot(**AUTH))[
                0
            ]["sender_role"],
            "contact",
        )

    def test_username_fallback_when_alias_missing(self):
        self.update_contacts(
            "UPDATE contact SET alias=NULL WHERE username=?", ("fake-contact",)
        )
        shard = self.shard("a", 1, 2, [(1, 1, 100, 2, 1600000000, "ok", None, 1)])
        self.run_import([shard], target_display_id="fake-contact")
        self.assertEqual(
            len(
                self.store.search(
                    **AUTH, query="", snapshot=self.store.snapshot(**AUTH)
                )
            ),
            1,
        )

    def test_cross_column_collision_owner_mismatch_and_case_fail_closed(self):
        shard = self.shard("a", 1, 2, [(1, 1, 100, 2, 1600000000, "ok", None, 1)])
        with self.assertRaises(ValueError):
            self.run_import([shard], owner_display_id="DISPLAY-SELF")
        with self.assertRaises(ValueError):
            self.run_import([shard], target_display_id="DISPLAY-CONTACT")
        with self.assertRaises(ValueError):
            self.run_import([shard], owner_display_id="display-contact")
        with self.assertRaises(ValueError):
            self.run_import([shard], target_display_id="display-contact' OR 1=1 --")
        self.update_contacts(
            "INSERT INTO contact VALUES (?,?,?,?)",
            ("display-contact", "unused", "", ""),
        )
        with self.assertRaisesRegex(ValueError, "not unique"):
            self.run_import([shard])
        self.assertEqual(
            self.store.search(**AUTH, query="", snapshot=self.store.snapshot(**AUTH)),
            [],
        )

    def test_label_mismatch_missing_owner_and_missing_alias_column_rejected(self):
        shard = self.shard("a", 1, 2, [(1, 1, 100, 2, 1600000000, "ok", None, 1)])
        with self.assertRaisesRegex(ValueError, "label mismatch"):
            self.run_import([shard], contact_label="different")
        self.update_contacts("DELETE FROM contact WHERE username=?", ("fake-self",))
        with self.assertRaisesRegex(ValueError, "account identity"):
            self.run_import([shard])
        missing = sqlite3.connect(":memory:")
        try:
            missing.execute(
                "CREATE TABLE contact(username TEXT,nick_name TEXT,remark TEXT)"
            )
            missing.execute("PRAGMA query_only=ON")
            with self.assertRaises(sqlite3.OperationalError):
                self.run_import([shard], contact_conn=missing)
        finally:
            missing.close()
        self.assertEqual(
            self.store.search(**AUTH, query="", snapshot=self.store.snapshot(**AUTH)),
            [],
        )

    def test_zstd_longtext_end_to_end_redaction_and_atomic_failure(self):
        try:
            import zstandard
        except ImportError:
            self.skipTest("isolated project zstandard is unavailable")
        text = "甲" * 2998 + "芝麻糖" + "乙" * 4000 + " 尾部  "
        frame = zstandard.ZstdCompressor().compress(text.encode("utf-8"))
        shard = self.shard("a", 1, 2, [(1, 1, 100, 2, 1600000000, frame, None, 1)])
        self.run_import([shard])
        epoch = self.store.snapshot(**AUTH)
        self.assertEqual(
            self.store.search(**AUTH, query="芝麻糖", snapshot=epoch)[0]["text"], text
        )
        self.assertEqual(
            self.store.revoke_batch(**AUTH, batch_id="synthetic:0", snapshot=epoch), 1
        )
        self.assertEqual(
            self.store.db.execute("SELECT COUNT(*) FROM wx_private_chunk").fetchone()[
                0
            ],
            0,
        )
        sensitive = "甲" * 2999 + "密" + "码:synthetic_secret " + "乙" * 3100
        frame2 = zstandard.ZstdCompressor().compress(sensitive.encode("utf-8"))
        b = self.shard("b", 1, 2, [(1, 1, 200, 2, 1600000000, frame2, None, 1)])
        self.run_import([b], batch_id="redacted")
        epoch = self.store.snapshot(**AUTH)
        self.assertEqual(
            self.store.search(**AUTH, query="", snapshot=epoch)[0]["text"],
            "[敏感内容已排除]",
        )
        c = self.shard(
            "c",
            1,
            2,
            [
                (1, 1, 300, 2, 1600000000, frame, None, 1),
                (2, 1, 301, 2, 1600000001, frame[:-2], None, 2),
            ],
        )
        with self.assertRaises(ValueError):
            self.run_import([c], batch_id="fail")
        self.assertEqual(self.store.snapshot(**AUTH), epoch)
        self.assertEqual(len(self.store.search(**AUTH, query="", snapshot=epoch)), 1)
        self.assertEqual(
            self.store.db.execute("SELECT COUNT(*) FROM wx_private_chunk").fetchone()[
                0
            ],
            0,
        )

    def test_revoke_during_staging_rolls_back_then_cleans_destination(self):
        shard = self.shard(
            "a",
            1,
            2,
            [
                (1, 1, 401, 2, 1600000000, "first", None, 1),
                (2, 1, 402, 2, 1600000001, "second", None, 2),
            ],
        )
        original = _AtomicStoreStage.write_page
        futures = []

        def cleanup():
            other = WeChatPrivateStore(
                self.tmp.name,
                owner_scope=AUTH["scope"],
                owner_qq=AUTH["actor"],
                account_id="fake-self",
                conversation_id="fake-contact",
                owner_sender_id="fake-self",
                contact_sender_id="fake-contact",
            )
            try:
                epoch = other.snapshot(**AUTH)
                other.clear(**AUTH, snapshot=epoch)
            finally:
                other.close()

        def inject(stage, page):
            original(stage, page)
            if not futures:
                futures.append(self.fence.request_revoke(cleanup))

        with patch.object(_AtomicStoreStage, "write_page", inject):
            with self.assertRaises(ImportCancelled):
                self.run_import([shard])
        futures[0].result(5)
        epoch = self.store.snapshot(**AUTH)
        self.assertEqual(self.store.search(**AUTH, query="", snapshot=epoch), [])
        self.assertEqual(
            self.store.db.execute("SELECT COUNT(*) FROM wx_private_batch").fetchone(),
            (0,),
        )
        with self.assertRaises(ImportCancelled):
            self.fence.token()

    def test_late_failure_rolls_back_all_previously_staged_pages(self):
        shard = self.shard(
            "a",
            1,
            2,
            [
                (1, 1, 100, 1, 1600000000, "good", None, 1),
                (2, 1, 101, 1, 1600000001, bytes([255]), None, 2),
            ],
        )
        old_epoch = self.store.snapshot(**AUTH)
        with self.assertRaises(ValueError):
            self.run_import([shard])
        self.assertEqual(self.store.snapshot(**AUTH), old_epoch)
        self.assertEqual(self.store.search(**AUTH, query="", snapshot=old_epoch), [])
        self.assertEqual(
            self.store.db.execute("SELECT COUNT(*) FROM wx_private_batch").fetchone(),
            (0,),
        )

    def test_identity_and_readonly_mismatch_fail_closed(self):
        shard = self.shard("a", 1, 2, [(1, 1, 100, 1, 1600000000, "one", None, 1)])
        with self.assertRaises(ValueError):
            self.run_import([shard], verified_owner="wrong-owner")
        shard.connection.execute("PRAGMA query_only=OFF")
        with self.assertRaises(ValueError):
            self.run_import([shard])
        self.assertEqual(
            self.store.search(**AUTH, query="", snapshot=self.store.snapshot(**AUTH)),
            [],
        )


if __name__ == "__main__":
    unittest.main()
