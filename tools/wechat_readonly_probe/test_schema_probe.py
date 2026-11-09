"""Schema-only tests against random encrypted databases and fake handles."""
import contextlib
import io
import secrets
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sqlcipher3
import schema_probe
import metadata_review
from test_controlled_probe import FakeProbeAPI
from test_probe import FakeReader
from test_same_process import fake_block

ROOT = Path(__file__).resolve().parents[2]


class SchemaProbeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="dafeiyu-schema-", dir=ROOT / ".tmp")
        directory = Path(self.tmp.name) / "account" / "db_storage" / "contact"
        directory.mkdir(parents=True)
        self.path = str(directory / "contact.db")
        self.key = secrets.token_bytes(32)

    def tearDown(self):
        self.tmp.cleanup()

    def create(self, columns):
        conn = sqlcipher3.connect(self.path)
        conn.set_key(b"x'" + self.key.hex().encode("ascii") + b"'")
        if columns is not None:
            conn.execute("CREATE TABLE contact (" + ",".join(name + " TEXT" for name in columns) + ")")
            if columns:
                conn.execute("INSERT INTO contact DEFAULT VALUES")
        conn.execute("CREATE TABLE unrelated(sensitive TEXT)")
        conn.execute("INSERT INTO unrelated VALUES ('synthetic-sensitive')")
        conn.commit()
        conn.close()

    def run_probe(self, fake=None):
        with open(self.path, "rb") as file:
            page = file.read(4096)
        fake = fake or FakeProbeAPI()
        fake.real_synthetic_page = page
        block = fake_block(self.key)
        return schema_probe.schema_once(12, self.path, "A" * 40, api=fake,
                                        reader_factory=lambda *_: FakeReader(block, ""))

    def test_all_columns_and_no_contact_rows_read(self):
        self.create(("username", "alias", "remark", "nick_name"))
        statements = []
        original = sqlcipher3.connect
        class Spy:
            def __init__(self, connection):
                self.connection = connection
            def execute(self, sql, *args):
                statements.append(sql)
                if "from main.contact" in sql.casefold():
                    raise AssertionError("contact row read")
                return self.connection.execute(sql, *args)
            def __getattr__(self, name):
                return getattr(self.connection, name)
        with patch.object(sqlcipher3, "connect", side_effect=lambda *a, **k: Spy(original(*a, **k))):
            self.assertEqual(self.run_probe(), "SCHEMA_COLS_F")
        self.assertTrue(any("pragma_table_info" in sql for sql in statements))
        self.assertFalse(any("from main.contact" in sql.casefold() for sql in statements))

    def test_missing_columns_fixed_mask(self):
        self.create(("username", "alias"))
        self.assertEqual(self.run_probe(), "SCHEMA_COLS_3")

    def test_missing_table(self):
        self.create(None)
        self.assertEqual(self.run_probe(), "SCHEMA_TABLE_NOT_FOUND")

    def test_wal_without_shm_rejected_before_page(self):
        self.create(("username",))
        with open(self.path + "-wal", "wb") as file:
            file.write(b"synthetic")
        fake = FakeProbeAPI()
        self.assertEqual(self.run_probe(fake), "WAL_SHM_NOT_VERIFIED")
        self.assertFalse(any(event[0] in ("page", "scan") for event in fake.events))

    def test_synthetic_wal_and_shm_present(self):
        self.create(("username", "alias"))
        writer = sqlcipher3.connect(self.path)
        try:
            writer.set_key(b"x'" + self.key.hex().encode("ascii") + b"'")
            self.assertEqual(writer.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower(), "wal")
            writer.execute("INSERT INTO unrelated VALUES ('synthetic-wal')")
            writer.commit()
            self.assertTrue(Path(self.path + "-wal").exists())
            self.assertTrue(Path(self.path + "-shm").exists())
            class Sidecars(FakeProbeAPI):
                def file_metadata(self, handle):
                    info = super().file_metadata(handle)
                    if handle.endswith(("-wal", "-shm")):
                        return metadata_review.FileMetadata(info.final_path, info.volume, info.file_id, 0)
                    return info
            self.assertEqual(self.run_probe(Sidecars()), "SCHEMA_COLS_3")
        finally:
            writer.close()

    def test_reparse_sidecar_rejected_before_sqlite(self):
        self.create(("username",))
        for suffix in ("-wal", "-shm"):
            with open(self.path + suffix, "wb") as file:
                file.write(b"synthetic")
        class ReparseSidecar(FakeProbeAPI):
            def file_metadata(self, handle):
                info = super().file_metadata(handle)
                if handle.endswith("-shm"):
                    return metadata_review.FileMetadata(info.final_path, info.volume, info.file_id, metadata_review._REPARSE)
                if handle.endswith("-wal"):
                    return metadata_review.FileMetadata(info.final_path, info.volume, info.file_id, 0)
                return info
        with patch.object(sqlcipher3, "connect", side_effect=AssertionError("unexpected open")):
            self.assertEqual(self.run_probe(ReparseSidecar()), "SOURCE_CHANGED")
    def test_signature_and_post_page_change_fail_closed(self):
        self.create(("username",))
        fake = FakeProbeAPI()
        fake.signature_bad = True
        self.assertEqual(self.run_probe(fake), "SIGNATURE_NOT_VERIFIED")
        class ChangedPage(FakeProbeAPI):
            def first_page(self, handle):
                page = super().first_page(handle)
                count = sum(event[0] == "page" for event in self.events)
                return page if count == 1 else bytes([page[0] ^ 1]) + page[1:]
        self.assertEqual(self.run_probe(ChangedPage()), "SOURCE_CHANGED")

    def test_cipher_status_failures_abort_before_schema_query(self):
        self.create(("username",))
        original = sqlcipher3.connect
        for value in ((0,), ("0",), None, (True,)):
            seen = []
            class Cursor:
                def fetchone(self):
                    return value
            class StatusSpy:
                def __init__(self, connection):
                    self.connection = connection
                def execute(self, sql, *args):
                    seen.append(sql)
                    if sql == "PRAGMA cipher_status":
                        return Cursor()
                    return self.connection.execute(sql, *args)
                def __getattr__(self, name):
                    return getattr(self.connection, name)
            with patch.object(sqlcipher3, "connect", side_effect=lambda *a, **k: StatusSpy(original(*a, **k))):
                self.assertEqual(self.run_probe(), "UNLOCK_NOT_VERIFIED")
            self.assertFalse(any("sqlite_schema" in sql for sql in seen))

    def test_output_and_wal_state_change(self):
        self.create(("username",))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(self.run_probe(), "SCHEMA_COLS_1")
        self.assertEqual((out.getvalue(), err.getvalue()), ("", ""))
        with patch.object(schema_probe, "_sidecars", side_effect=[(True, True), (True, False)]):
            self.assertEqual(self.run_probe(), "SOURCE_CHANGED")
    def test_identity_mismatch_cancels_schema(self):
        self.create(("username",))
        fake = FakeProbeAPI()
        fake.window_change = True
        self.assertEqual(self.run_probe(fake), "UNLOCK_NOT_VERIFIED")
        self.assertFalse(any(event[0] == "page" for event in fake.events))


if __name__ == "__main__":
    unittest.main()



