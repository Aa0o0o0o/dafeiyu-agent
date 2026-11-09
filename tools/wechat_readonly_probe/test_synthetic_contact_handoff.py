"""Encrypted synthetic DB and fake memory only; no WeChat or real identity."""
import contextlib
import io
import os
import secrets
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dataclasses import replace

import sqlcipher3
import binding_model
import synthetic_contact_handoff as handoff
import synthetic_scan_comparison as comparison
from test_same_process import fake_block

ROOT = Path(__file__).resolve().parents[2]
OWNER = "synthetic-owner-display"
CONTACT = "synthetic-contact-display"
OWNER_INTERNAL = "wxid_synthetic_owner"


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="dafeiyu-contact-fake-", dir=ROOT / ".tmp")
        self.path = os.path.join(self.tmp.name, "synthetic.db")
        self.key = secrets.token_bytes(32)
        conn = sqlcipher3.connect(self.path)
        conn.set_key(b"x'" + self.key.hex().encode("ascii") + b"'")
        conn.execute("CREATE TABLE contact(username TEXT, alias TEXT, nick_name TEXT, remark TEXT)")
        conn.executemany("INSERT INTO contact VALUES (?, ?, ?, ?)", [
            (OWNER_INTERNAL, OWNER, "fake-owner", ""),
            ("wxid_synthetic_contact", CONTACT, "fake-contact", ""),
            ("wxid_decoy", "synthetic-decoy-display", "decoy", ""),
        ])
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def change(self, sql, params):
        conn = sqlcipher3.connect(self.path)
        conn.set_key(b"x'" + self.key.hex().encode("ascii") + b"'")
        conn.execute(sql, params)
        conn.commit()
        conn.close()

    def call(self, **overrides):
        process = binding_model.Process(100, 200, True)
        window = binding_model.Window(300, 100, 200, OWNER_INTERNAL)
        ids = (binding_model.FileIdentity(1, 10), binding_model.FileIdentity(1, 11),
               binding_model.FileIdentity(1, 12))
        database = binding_model.DatabaseObservation(100, 200, *ids, OWNER_INTERNAL, True, True, True)
        args = dict(memory=comparison.SyntheticMemory(((0x10000, fake_block(self.key)),)),
                    db_path=self.path, owner_display=OWNER, contact_display=CONTACT,
                    verified_owner=OWNER_INTERNAL,
                    identity_fixture=(process, window, database, process, window, database))
        args.update(overrides)
        return handoff.synthetic_match(**args)

    def test_random_synthetic_key_handoff_and_no_output(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            result = self.call()
        self.assertEqual(result, "SYNTHETIC_IDENTITIES_MATCHED")
        self.assertEqual((out.getvalue(), err.getvalue()), ("", ""))
        self.assertNotIn(OWNER, result)
        self.assertNotIn(CONTACT, result)
        self.assertNotIn(self.key.hex(), result)

    def test_distinct_exact_aliases_and_owner_are_required(self):
        for change in (dict(owner_display=CONTACT), dict(owner_display=OWNER.upper()),
                       dict(contact_display=CONTACT.upper()), dict(verified_owner="wxid_other"),
                       dict(owner_display=""), dict(memory=object())):
            with self.subTest(change=change):
                self.assertEqual(self.call(**change), "NOT_VERIFIED")

    def test_duplicate_and_missing_aliases_fail(self):
        self.change("INSERT INTO contact VALUES (?, ?, ?, ?)",
                    ("wxid_other", CONTACT, "", ""))
        self.assertEqual(self.call(), "NOT_VERIFIED")
        self.change("DELETE FROM contact WHERE username = ?", ("wxid_other",))
        self.change("UPDATE contact SET alias = NULL WHERE username = ?", ("wxid_synthetic_contact",))
        self.assertEqual(self.call(), "NOT_VERIFIED")

    def test_wrong_synthetic_key_and_chatroom_fail(self):
        fake = comparison.SyntheticMemory(((0x10000, fake_block(secrets.token_bytes(32))),))
        self.assertEqual(self.call(memory=fake), "NOT_VERIFIED")
        self.change("UPDATE contact SET username = ? WHERE alias = ?", ("fake@chatroom", CONTACT))
        self.assertEqual(self.call(), "NOT_VERIFIED")

    def test_synthetic_identity_or_signature_change_rejected(self):
        process = binding_model.Process(100, 200, True)
        window = binding_model.Window(300, 100, 200, OWNER_INTERNAL)
        ids = (binding_model.FileIdentity(1, 10), binding_model.FileIdentity(1, 11),
               binding_model.FileIdentity(1, 12))
        database = binding_model.DatabaseObservation(100, 200, *ids, OWNER_INTERNAL, True, True, True)
        changed = (process, window, database, replace(process, created=201), window, database)
        self.assertEqual(self.call(identity_fixture=changed), "NOT_VERIFIED")
        invalid = replace(process, signature_verified=False)
        self.assertEqual(self.call(identity_fixture=(invalid, window, database, invalid, window, database)), "NOT_VERIFIED")
    def test_file_identity_change_rejects_result(self):
        with patch.object(handoff, "_same_stat", side_effect=[True, True, False]):
            self.assertEqual(self.call(), "NOT_VERIFIED")

    def test_trace_and_nonfixture_paths_are_rejected(self):
        with patch.object(handoff.sys, "gettrace", return_value=lambda *args: None):
            self.assertEqual(self.call(), "NOT_VERIFIED")
        self.assertEqual(self.call(db_path=os.path.abspath("outside.db")), "NOT_VERIFIED")


if __name__ == "__main__":
    unittest.main()

