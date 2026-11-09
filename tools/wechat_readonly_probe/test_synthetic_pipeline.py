"""End-to-end synthetic-only multi-source encrypted import tests."""

import contextlib
import io
import secrets
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import probe
from same_process_research import SyntheticMemory
from synthetic_pipeline import SyntheticBinding, run_synthetic_pipeline
from src.wechat_import_fence import ImportFence
from src.wechat_private_store import WeChatPrivateStore
from src.wechat_single_session import conversation_table

ROOT = Path(__file__).resolve().parents[2]
AUTH = dict(scope="fixture:1111111111:2222222222", actor="2222222222", verified=True)


def fake_memory(keys):
    block = bytearray(0x10000)
    base = 0x10000
    for i, key in enumerate(keys):
        marker = base + 0x100 + i * 0x100
        pair = base + 0x1000 + i * 0x100
        config = base + 0x3000 + i * 0x100
        payload = base + 0x6000 + i * 0x200
        block[marker - base : marker - base + len(probe._NAME)] = probe._NAME
        struct.pack_into("<QQ", block, pair - base, marker, len(probe._NAME))
        struct.pack_into("<Q", block, pair - base - 16 + 0x28, config)
        literal = b"x'" + key.hex().encode("ascii") + b"'"
        encoded = bytes(
            value ^ probe._MASK[j % len(probe._MASK)] for j, value in enumerate(literal)
        )
        struct.pack_into("<QQ", block, config - base + 0x88 + 8, payload, len(encoded))
        block[payload - base : payload - base + len(encoded)] = encoded
    return SyntheticMemory(bytes(block))


class SyntheticPipelineTests(unittest.TestCase):
    def setUp(self):
        try:
            import sqlcipher3
        except ImportError:
            self.skipTest("isolated project SQLCipher unavailable")
        self.sqlcipher = sqlcipher3
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT / ".tmp")
        self.root = Path(self.tmp.name).resolve()
        self.keys = [secrets.token_bytes(32) for _ in range(3)]
        self.contact = self.root / "contact.db"
        self.paths = [self.root / "message_0.db", self.root / "message_1.db"]
        self._contact()
        self._shard(
            self.paths[0],
            self.keys[1],
            1,
            2,
            [
                (1, 1, 101, 1, 1600000000, "fake owner message", None, 1),
                (2, 3, 102, 2, 1600000001, b"uninspected-media", None, 2),
            ],
        )
        self._shard(
            self.paths[1],
            self.keys[2],
            2,
            1,
            [
                (1, 1, 103, 1, 1600000002, "fake contact message", None, 3),
                (2, 1, 104, 2, 1600000003, "密码:synthetic_secret", None, 4),
            ],
        )
        self.store = WeChatPrivateStore(
            self.root,
            owner_scope=AUTH["scope"],
            owner_qq=AUTH["actor"],
            account_id="fake-self",
            conversation_id="fake-contact",
            owner_sender_id="fake-self",
            contact_sender_id="fake-contact",
        )
        self.store.set_access(**AUTH, enabled=True)
        self.fence = ImportFence(self.root)
        self.fence.enable()

    def tearDown(self):
        if hasattr(self, "store"):
            self.store.close()
        if hasattr(self, "tmp"):
            self.tmp.cleanup()

    def _db(self, path, key):
        db = self.sqlcipher.connect(str(path))
        db.set_key(b"x'" + key.hex().encode("ascii") + b"'")
        return db

    def _contact(self):
        db = self._db(self.contact, self.keys[0])
        db.execute(
            "CREATE TABLE contact(username TEXT,alias TEXT,nick_name TEXT,remark TEXT)"
        )
        db.executemany(
            "INSERT INTO contact VALUES(?,?,?,?)",
            [
                ("fake-self", "fake-display-self", "self", ""),
                ("fake-contact", "fake-display-contact", "contact", "fake-label"),
                ("fake-other", "fake-display-other", "other", ""),
            ],
        )
        db.commit()
        db.close()

    def _shard(self, path, key, self_id, contact_id, rows):
        db = self._db(path, key)
        db.execute("CREATE TABLE Name2Id(user_name TEXT)")
        db.execute(
            "INSERT INTO Name2Id(rowid,user_name) VALUES(?,?)", (self_id, "fake-self")
        )
        db.execute(
            "INSERT INTO Name2Id(rowid,user_name) VALUES(?,?)",
            (contact_id, "fake-contact"),
        )
        table = conversation_table("fake-contact")
        db.execute(
            f'CREATE TABLE "{table}" (local_id INTEGER,local_type INTEGER,server_id INTEGER,real_sender_id INTEGER,create_time INTEGER,message_content TEXT,packed_info_data BLOB,sort_seq INTEGER)'
        )
        db.execute(f'CREATE INDEX ord ON "{table}" (sort_seq,create_time,local_id)')
        db.executemany(f'INSERT INTO "{table}" VALUES(?,?,?,?,?,?,?,?)', rows)
        other = conversation_table("fake-other")
        db.execute(f'CREATE TABLE "{other}" (private TEXT)')
        db.execute(f'INSERT INTO "{other}" VALUES (?)', ("synthetic-decoy-secret",))
        db.commit()
        db.close()

    def run_fake(self, **overrides):
        args = dict(
            memory=fake_memory(self.keys),
            contact_db=self.contact,
            message_dbs=self.paths,
            fixture_root=self.root,
            binding=SyntheticBinding("fake-self", "fake-contact", True, True),
            contact_label="fake-label",
            owner_display_id="fake-display-self",
            target_display_id="fake-display-contact",
            store=self.store,
            auth=AUTH,
            fence=self.fence,
            token=self.fence.token(),
            batch_id="fake-run",
        )
        args.update(overrides)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            result = run_synthetic_pipeline(**args)
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(err.getvalue(), "")
        return result

    def test_end_to_end_only_target_rows_and_safe_counts(self):
        result = self.run_fake()
        self.assertEqual(result["outcome"], "synthetic_imported")
        self.assertEqual(result["counts"]["imported"], 4)
        self.assertEqual(result["counts"]["redacted"], 1)
        self.assertEqual(result["counts"]["media_uninspected"], 1)
        self.assertFalse(result["history_complete"])
        self.assertFalse(result["media_inspected"])
        self.assertNotIn("fake-contact", repr(result))
        rows = self.store.search(**AUTH, query="", snapshot=self.store.snapshot(**AUTH))
        self.assertNotIn("synthetic-decoy-secret", repr(rows))
        self.assertNotIn("synthetic_secret", repr(rows))
        self.assertEqual({r["sender_role"] for r in rows}, {"owner", "contact"})

    def test_repeat_after_confirmed_publish_uses_fresh_batch_without_duplicates(self):
        first = self.run_fake(batch_id="first")
        self.assertEqual(first["counts"]["imported"], 4)
        repeated = self.run_fake(batch_id="second")
        self.assertEqual(repeated["outcome"], "synthetic_imported")
        self.assertEqual(repeated["counts"]["imported"], 0)
        self.assertEqual(repeated["counts"]["duplicates"], 4)
        rows = self.store.search(**AUTH, query="", snapshot=self.store.snapshot(**AUTH))
        self.assertEqual(len(rows), 4)

    def test_unverified_binding_or_missing_key_does_not_import(self):
        self.assertEqual(
            self.run_fake(
                binding=SyntheticBinding("fake-self", "fake-contact", True, False)
            )["outcome"],
            "not_confirmed",
        )
        self.assertEqual(
            self.run_fake(memory=fake_memory(self.keys[:2]))["outcome"], "not_confirmed"
        )
        self.assertEqual(
            self.store.search(**AUTH, query="", snapshot=self.store.snapshot(**AUTH)),
            [],
        )

    def test_cancel_and_midstream_error_rollback_all(self):
        token = self.fence.token()
        token.cancel()
        self.assertEqual(self.run_fake(token=token)["outcome"], "not_confirmed")
        from src.wechat_synthetic_import import _AtomicStoreStage

        original = _AtomicStoreStage.write_page

        def bad(stage, page):
            original(stage, page)
            raise ValueError("synthetic late failure")

        with patch.object(_AtomicStoreStage, "write_page", bad):
            self.assertEqual(self.run_fake()["outcome"], "not_confirmed")
        self.assertEqual(
            self.store.search(**AUTH, query="", snapshot=self.store.snapshot(**AUTH)),
            [],
        )

    def test_fake_boundary_rejects_untrusted_store_without_accessing_it(self):
        class BadStore:
            @property
            def db(self):
                raise AssertionError("untrusted store was accessed")

        result = self.run_fake(store=BadStore())
        self.assertEqual(result["outcome"], "not_confirmed")
        self.assertEqual(
            self.store.search(**AUTH, query="", snapshot=self.store.snapshot(**AUTH)),
            [],
        )

    def test_fake_boundary_rejects_nonfixture_memory_or_paths(self):
        self.assertEqual(self.run_fake(memory=object())["outcome"], "not_confirmed")
        self.assertEqual(
            self.run_fake(contact_db=ROOT / "README.md")["outcome"], "not_confirmed"
        )
        self.assertEqual(
            self.store.search(**AUTH, query="", snapshot=self.store.snapshot(**AUTH)),
            [],
        )


if __name__ == "__main__":
    unittest.main()
