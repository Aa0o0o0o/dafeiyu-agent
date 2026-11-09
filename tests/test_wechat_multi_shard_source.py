"""Synthetic encrypted shards only. No real process, account or chat access."""

from __future__ import annotations

import secrets
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path

from src.wechat_single_session import Shard, conversation_table, read_pages
from tests.synthetic_wechat_key_review.prototype import FakeMemory, _LITERAL, _matches


class SyntheticMultiShardTests(unittest.TestCase):
    def setUp(self):
        try:
            import sqlcipher3
        except ImportError:
            self.skipTest('project-local SQLCipher test dependency unavailable')
        self.sqlcipher = sqlcipher3
        self.directory = tempfile.TemporaryDirectory(dir='.tmp')
        self.root = Path(self.directory.name).resolve()
        self.target = 'wxid_synthetic_contact'
        self.table = conversation_table(self.target)
        self.writers = []
        self.pages = []
        self.paths = []
        self.keys = []
        for i in range(2):
            path = self.root / f'synthetic_{i}.sqlite'
            key = secrets.token_bytes(32)
            writer = sqlcipher3.connect(str(path))
            self.writers.append(writer)
            writer.execute(f"PRAGMA key = \"x'{key.hex()}'\"")
            writer.execute(f'CREATE TABLE "{self.table}" (local_id INTEGER, local_type INTEGER, server_id INTEGER, real_sender_id INTEGER, create_time INTEGER, message_content TEXT, packed_info_data BLOB, sort_seq INTEGER)')
            writer.execute(f'CREATE INDEX ordering ON "{self.table}" (sort_seq, create_time, local_id)')
            writer.execute(f'INSERT INTO "{self.table}" VALUES (?, 1, ?, 1, ?, ?, NULL, ?)', (1, 2 * i + 1, 2 * i + 1, 'synthetic', 2 * i + 1))
            writer.commit()
            # Create a committed WAL record while retaining the writer so its
            # WAL/SHM remain present. Never checkpoint or copy real databases.
            writer.execute('PRAGMA journal_mode=WAL')
            writer.execute('PRAGMA wal_autocheckpoint=0')
            writer.execute(f'INSERT INTO "{self.table}" VALUES (?, 1, ?, 1, ?, ?, NULL, ?)', (2, 2 * i + 2, 2 * i + 2, 'synthetic', 2 * i + 2))
            writer.commit()
            self.assertGreater(path.with_name(path.name + '-wal').stat().st_size, 0)
            with path.open('rb') as stream:
                page = stream.read(4096)
            self.assertEqual(len(page), 4096)
            self.paths.append(path)
            self.keys.append(key)
            self.pages.append(page)

    def tearDown(self):
        for writer in self.writers:
            writer.close()
        self.directory.cleanup()

    def _fake_memory(self, keys):
        block = b'prefix ' + b' '.join(b"x'" + key.hex().encode('ascii') + b"'" for key in keys)
        return FakeMemory(7, 'synthetic-only', (block,))

    def _open_synthetic_sources(self, stack, source):
        # This is intentionally an in-test demo, not a reusable process scanner.
        if type(source) is not FakeMemory or source.pid != 7 or source.path != 'synthetic-only':
            raise ValueError('invalid synthetic identity')
        selected = [None] * len(self.pages)
        scans = 0
        for block in source.blocks:
            scans += 1
            for match in _LITERAL.finditer(block):
                candidate = bytes.fromhex(match.group(1).decode('ascii'))
                for index, page in enumerate(self.pages):
                    if _matches(candidate, page):
                        if selected[index] is not None and selected[index] != candidate:
                            raise ValueError('ambiguous synthetic page key')
                        selected[index] = candidate
        if any(candidate is None for candidate in selected):
            raise ValueError('not all synthetic shards matched')
        # Open no connection until every synthetic shard has a verified key.
        shards = []
        for index, (path, key) in enumerate(zip(self.paths, selected, strict=True)):
            if path.parent != self.root:
                raise ValueError('synthetic fixture path mismatch')
            conn = self.sqlcipher.connect(f'{path.as_uri()}?mode=ro', uri=True)
            stack.callback(conn.close)
            conn.execute(f"PRAGMA key = \"x'{key.hex()}'\"")
            conn.execute('PRAGMA query_only=ON')
            # Schema access checks decryption without exposing row values.
            conn.execute('SELECT count(*) FROM main.sqlite_master').fetchone()
            shards.append(Shard(f'synthetic-{index}', conn, {1: 'synthetic_sender'}))
        return shards, scans

    def test_one_scan_opens_all_keys_and_reads_committed_wal(self):
        source = self._fake_memory((secrets.token_bytes(32), *self.keys))
        with ExitStack() as stack:
            shards, scans = self._open_synthetic_sources(stack, source)
            self.assertEqual(scans, 1)
            self.assertTrue(all(s.connection.execute('PRAGMA query_only').fetchone()[0] == 1 for s in shards))
            pages = list(read_pages(shards, self.target, page_size=2))
            self.assertEqual([m.server_id for page in pages for m in page], [1, 2, 3, 4])
            self.assertTrue(all(not s.connection.in_transaction for s in shards))

    def test_missing_or_wrong_candidate_fails_before_opening(self):
        for keys in ((self.keys[0],), (secrets.token_bytes(32),)):
            with self.subTest(count=len(keys)), ExitStack() as stack:
                with self.assertRaisesRegex(ValueError, 'not all'):
                    self._open_synthetic_sources(stack, self._fake_memory(keys))

    def test_changed_page_fails_closed(self):
        corrupt = bytearray(self.pages[1])
        corrupt[100] ^= 1
        self.pages[1] = bytes(corrupt)
        with ExitStack() as stack, self.assertRaisesRegex(ValueError, 'not all'):
            self._open_synthetic_sources(stack, self._fake_memory(self.keys))


if __name__ == '__main__':
    unittest.main()
