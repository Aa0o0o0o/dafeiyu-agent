"""Mocked Windows identities and random local synthetic encrypted databases."""
from __future__ import annotations

import contextlib
import hashlib
import io
import ntpath
import secrets
import struct
import tempfile
import unittest
from pathlib import Path

from tools.wechat_readonly_probe import metadata_review as metadata, probe
from tools.wechat_readonly_probe.real_source_session import SourceSessionError, with_real_sources


CONTACT = r'E:\synthetic\account\db_storage\contact\contact.db'
MESSAGES = (r'E:\synthetic\account\db_storage\message\message_0.db',
            r'E:\synthetic\account\db_storage\message\message_1.db')
ROOT = Path(__file__).resolve().parents[2]


class FakeMemory:
    def __init__(self, keys, *, timeout=False):
        self.block = bytearray(0x10000)
        self.timeout = timeout
        base = 0x10000
        for i, key in enumerate(keys):
            marker = base + 0x100 + i * 0x100
            pair = base + 0x1000 + i * 0x100
            config = base + 0x3000 + i * 0x100
            payload = base + 0x6000 + i * 0x200
            self.block[marker-base:marker-base+len(probe._NAME)] = probe._NAME
            struct.pack_into('<QQ', self.block, pair-base, marker, len(probe._NAME))
            struct.pack_into('<Q', self.block, pair-base-16+0x28, config)
            literal = b"x'" + key.hex().encode('ascii') + b"'"
            encoded = bytes(v ^ probe._MASK[j % len(probe._MASK)] for j, v in enumerate(literal))
            struct.pack_into('<QQ', self.block, config-base+0x88+8, payload, len(encoded))
            self.block[payload-base:payload-base+len(encoded)] = encoded
    def regions(self):
        yield 0x10000, len(self.block)
    def read(self, address, size):
        if self.timeout:
            raise TimeoutError
        start = address - 0x10000
        return bytes(self.block[start:start+size]) if 0 <= start <= len(self.block)-size else None


class FakeAPI:
    def __init__(self, pages):
        self.pages = pages
        self.opened, self.closed = [], []
        self.changed = False
    def window_pid(self, hwnd):
        return 77
    def open_process(self, pid):
        self.opened.append('process')
        return 'process'
    def process_metadata(self, handle):
        return 77, 1000, True, metadata._EXPECTED_EXE
    def open_metadata(self, path):
        self.opened.append(path)
        return path
    def open_target(self, path):
        return self.open_metadata(path)
    def file_metadata(self, handle):
        attrs = 0 if handle.lower().endswith('.db') else metadata._DIRECTORY
        return metadata.FileMetadata(handle, 5, hashlib.sha256(handle.encode()).digest()[:16], attrs)
    def close(self, handle):
        self.closed.append(handle)
    def verify_signature(self, thumbprint):
        if thumbprint != 'A' * 40:
            raise ValueError
    def first_page(self, handle):
        page = self.pages[ntpath.normcase(handle)]
        if self.changed and ntpath.normcase(handle) == ntpath.normcase(CONTACT):
            return bytes([page[0] ^ 1]) + page[1:]
        return page


class SourceSessionTests(unittest.TestCase):
    def setUp(self):
        try:
            import sqlcipher3
        except ImportError:
            self.skipTest('isolated SQLCipher unavailable')
        self.sqlcipher = sqlcipher3
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT / '.tmp')
        self.keys = [secrets.token_bytes(32) for _ in range(3)]
        self.local = {}
        self.pages = {}
        for index, (virtual, key) in enumerate(zip((CONTACT, *MESSAGES), self.keys, strict=True)):
            path = Path(self.tmp.name) / f'synthetic-{index}.db'
            conn = sqlcipher3.connect(str(path))
            conn.set_key(b"x'" + key.hex().encode('ascii') + b"'")
            conn.execute('CREATE TABLE synthetic (id INTEGER)')
            conn.execute('INSERT INTO synthetic VALUES (1)')
            conn.commit()
            conn.close()
            self.local[Path(virtual).as_uri() + '?mode=ro'] = path
            with path.open('rb') as file:
                self.pages[ntpath.normcase(virtual)] = file.read(4096)
        self.fake = FakeAPI(self.pages)
        self.opened_connections = []

    def tearDown(self):
        if hasattr(self, 'tmp'):
            self.tmp.cleanup()

    def connect(self, filename, *, uri=False):
        self.assertTrue(uri)
        conn = self.sqlcipher.connect(self.local[filename].as_uri() + '?mode=ro', uri=True)
        self.opened_connections.append(conn)
        return conn

    def run_source(self, operation, keys=None, **overrides):
        fake_memory = FakeMemory(self.keys if keys is None else keys)
        args = dict(api=self.fake, reader_factory=lambda api, h: fake_memory,
                    connection_factory=self.connect, sidecars_fn=lambda path: (False, False))
        args.update(overrides)
        return with_real_sources(12, CONTACT, MESSAGES, 'A' * 40, operation=operation, **args)

    def test_all_sources_match_without_output_and_close_on_exit(self):
        held = []
        def operation(session):
            self.assertIs(session.revalidate(), True)
            self.assertEqual(len(session.shards), 2)
            for connection in (session.contact, *(c for _, c in session.shards)):
                self.assertEqual(connection.execute('PRAGMA query_only').fetchone(), (1,))
                held.append(connection)
            return 'ok'
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(self.run_source(operation), 'ok')
        self.assertEqual((out.getvalue(), err.getvalue()), ('', ''))
        for connection in held:
            with self.assertRaises(self.sqlcipher.Error):
                connection.execute('SELECT 1')
        self.assertCountEqual(self.fake.opened, self.fake.closed)

    def test_shard_labels_stay_stable_when_order_changes(self):
        def labels(session):
            return {label for label, _ in session.shards}
        first = self.run_source(labels)
        memory = FakeMemory(self.keys)
        second = with_real_sources(
            12, CONTACT, tuple(reversed(MESSAGES)), 'A' * 40, operation=labels,
            api=self.fake, reader_factory=lambda api, handle: memory,
            connection_factory=self.connect, sidecars_fn=lambda path: (False, False),
        )
        self.assertEqual(first, {'message_0.db', 'message_1.db'})
        self.assertEqual(second, first)

    def test_missing_key_and_budget_close_without_operation(self):
        calls = []
        with self.assertRaisesRegex(SourceSessionError, 'KEYS_NOT_VERIFIED'):
            self.run_source(lambda s: calls.append(True), keys=self.keys[:2])
        self.assertEqual(calls, [])
        self.assertEqual(self.opened_connections, [])
        self.assertCountEqual(self.fake.opened, self.fake.closed)
        self.fake = FakeAPI(self.pages)
        with self.assertRaisesRegex(SourceSessionError, 'SCAN_LIMIT'):
            self.run_source(lambda s: calls.append(True), reader_factory=lambda a, h: FakeMemory(self.keys, timeout=True))
        self.assertCountEqual(self.fake.opened, self.fake.closed)

    def test_source_changes_or_wal_without_shm_fail_closed(self):
        def change(session):
            self.fake.changed = True
            session.revalidate()
        with self.assertRaises(SourceSessionError):
            self.run_source(change)
        self.assertCountEqual(self.fake.opened, self.fake.closed)
        self.fake = FakeAPI(self.pages)
        with self.assertRaises(SourceSessionError):
            self.run_source(lambda s: None, sidecars_fn=lambda p: (True, False))
        self.assertCountEqual(self.fake.opened, self.fake.closed)

    def test_cross_account_and_duplicate_paths_rejected(self):
        for paths in ((MESSAGES[0], MESSAGES[0]),
                      (r'E:\synthetic\other\db_storage\message\message_0.db',),
                      (r'E:\synthetic\account\db_storage\message\..\message\message_0.db',)):
            with self.subTest(count=len(paths)), self.assertRaises(SourceSessionError):
                with_real_sources(12, CONTACT, paths, 'A' * 40, operation=lambda s: None,
                                  api=self.fake, sidecars_fn=lambda p: (False, False))
        self.assertEqual(self.fake.opened, [])


if __name__ == '__main__':
    unittest.main()
