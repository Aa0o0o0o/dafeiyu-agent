"""Synthetic-only exact alias/username matching against encrypted data."""
import contextlib
import io
import secrets
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sqlcipher3
import schema_probe
from test_controlled_probe import FakeProbeAPI
from test_probe import FakeReader
from test_same_process import fake_block

ROOT = Path(__file__).resolve().parents[2]


class MatchProbeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="dafeiyu-match-fake-", dir=ROOT / ".tmp")
        directory = Path(self.tmp.name) / "account" / "db_storage" / "contact"
        directory.mkdir(parents=True)
        self.path = str(directory / "contact.db")
        self.key = secrets.token_bytes(32)
        self.edit("CREATE TABLE contact(username TEXT, alias TEXT, remark TEXT, nick_name TEXT)")
        self.edit("INSERT INTO contact VALUES (?,?,?,?)", ("fake_owner_internal", "fake_owner", "", ""))
        self.edit("INSERT INTO contact VALUES (?,?,?,?)", ("fake_target_internal", "fake_target", "", ""))

    def tearDown(self):
        self.tmp.cleanup()

    def edit(self, sql, args=()):
        connection = sqlcipher3.connect(self.path)
        connection.set_key(b"x'" + self.key.hex().encode() + b"'")
        connection.execute(sql, args)
        connection.commit()
        connection.close()

    def run_match(self, owner="fake_owner", target="fake_target", fake=None):
        with open(self.path, "rb") as file:
            page = file.read(4096)
        fake = fake or FakeProbeAPI()
        fake.real_synthetic_page = page
        return schema_probe.match_once(12, self.path, "A" * 40, owner, target,
                                       api=fake, reader_factory=lambda *_: FakeReader(fake_block(self.key), ""))

    def test_exact_aliases_unbound_status_no_output_or_row_leak(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            result = self.run_match()
        self.assertEqual(result, "IDENTITIES_PRESENT_UNBOUND")
        self.assertEqual((out.getvalue(), err.getvalue()), ("", ""))
        self.assertNotIn("fake_owner", result)
        self.assertNotIn("fake_target", result)

    def test_username_fallback_and_two_parameters_each(self):
        seen = []
        original = sqlcipher3.connect
        class Spy:
            def __init__(self, connection):
                self.connection = connection
            def execute(self, sql, *args):
                if "from main.contact" in sql.casefold():
                    seen.append((sql, args))
                return self.connection.execute(sql, *args)
            def __getattr__(self, name):
                return getattr(self.connection, name)
        with patch.object(sqlcipher3, "connect", side_effect=lambda *a, **k: Spy(original(*a, **k))):
            self.assertEqual(self.run_match("fake_owner_internal", "fake_target_internal"), "IDENTITIES_PRESENT_UNBOUND")
        self.assertEqual(len(seen), 2)
        self.assertTrue(all("LIMIT 2" in sql and "SELECT username" in sql for sql, _ in seen))
        self.assertTrue(all(len(params[0]) == 2 for _, params in seen))

    def test_missing_same_and_case_mismatch_fail(self):
        for owner, target in (("fake_owner", "fake_owner"), ("FAKE_OWNER", "fake_target"),
                              ("fake_owner", "FAKE_TARGET"), ("", "fake_target")):
            with self.subTest():
                self.assertEqual(self.run_match(owner, target), "IDENTITIES_NOT_VERIFIED")

    def test_duplicate_or_cross_column_collision_fail(self):
        self.edit("INSERT INTO contact VALUES (?,?,?,?)", ("fake_other", "fake_target", "", ""))
        self.assertEqual(self.run_match(), "IDENTITIES_NOT_VERIFIED")
        self.edit("DELETE FROM contact WHERE username=?", ("fake_other",))
        self.edit("INSERT INTO contact VALUES (?,?,?,?)", ("fake_target", "different", "", ""))
        self.assertEqual(self.run_match(), "IDENTITIES_NOT_VERIFIED")

    def test_same_internal_id_and_chatroom_fail(self):
        self.edit("UPDATE contact SET username=? WHERE alias=?", ("fake_owner_internal", "fake_target"))
        self.assertEqual(self.run_match(), "IDENTITIES_NOT_VERIFIED")
        self.edit("UPDATE contact SET username=? WHERE alias=?", ("fake@chatroom", "fake_target"))
        self.assertEqual(self.run_match(), "IDENTITIES_NOT_VERIFIED")
        self.edit("UPDATE contact SET username=? WHERE alias=?", ("fake" + chr(0) + "bad", "fake_target"))
        self.assertEqual(self.run_match(), "IDENTITIES_NOT_VERIFIED")

    def test_signature_or_process_change_never_matches(self):
        fake = FakeProbeAPI()
        fake.signature_bad = True
        self.assertEqual(self.run_match(fake=fake), "SIGNATURE_NOT_VERIFIED")
        fake = FakeProbeAPI()
        fake.window_change = True
        self.assertEqual(self.run_match(fake=fake), "UNLOCK_NOT_VERIFIED")


if __name__ == "__main__":
    unittest.main()

