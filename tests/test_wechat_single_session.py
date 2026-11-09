import hashlib
import secrets
import sqlite3
import tempfile
import unittest
from pathlib import Path

from src.wechat_single_session import Shard, conversation_table, read_pages, stage_all


class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.username = 'wxid_synthetic_alice'
        self.table = 'Msg_' + hashlib.md5(self.username.encode(), usedforsecurity=False).hexdigest()
        self.connections = []

    def tearDown(self):
        for conn in self.connections:
            conn.close()

    def make(self, target=True):
        conn = sqlite3.connect(':memory:')
        self.connections.append(conn)
        table = self.table if target else conversation_table('wxid_other')
        conn.execute(f'CREATE TABLE "{table}" (local_id INTEGER, local_type INTEGER, server_id INTEGER, real_sender_id INTEGER, create_time INTEGER, message_content TEXT, packed_info_data BLOB, sort_seq INTEGER)')
        conn.execute(f'CREATE INDEX ordering ON "{table}" (sort_seq, create_time, local_id)')
        conn.commit()
        return conn

    def add(self, conn, local_id, server_id, seq, content):
        conn.execute(f'INSERT INTO "{self.table}" VALUES (?, 1, ?, 42, ?, ?, NULL, ?)', (local_id, server_id, seq, content, seq))
        conn.commit()

    def test_exact_name_and_invalid_targets(self):
        self.assertEqual(conversation_table(self.username), self.table)
        for target in ('', '  ', 'x@chatroom', ' x', 'x\x00'):
            with self.subTest(target=target), self.assertRaises(ValueError):
                list(read_pages([Shard('a', self.make())], target))
        with self.assertRaises(ValueError):
            list(read_pages([Shard('only-other', self.make(False))], self.username))

    def test_pagination_dedup_and_distinct_zero_ids(self):
        a, b = self.make(), self.make()
        self.add(a, 1, 100, 1, 'one')
        self.add(b, 7, 100, 1, 'one')
        self.add(a, 2, 0, 2, 'two')
        self.add(b, 2, 0, 3, 'three')
        pages = list(read_pages([Shard('a', a, {42: 'sender'}), Shard('b', b, {42: 'sender'})], self.username, 2))
        self.assertEqual([len(p) for p in pages], [2, 1])
        self.assertEqual([m.message_content for p in pages for m in p], ['one', 'two', 'three'])

    def test_conflict_rolls_back_already_staged_page(self):
        class Sink:
            def __init__(self):
                self.pending = []
                self.published = []
                self.writes = 0
                self.rolled_back = False

            def begin(self):
                pass

            def write_page(self, page):
                self.pending.extend(page)
                self.writes += 1

            def commit(self):
                self.published = self.pending
                self.pending = []

            def rollback(self):
                self.pending = []
                self.rolled_back = True

        a, b = self.make(), self.make()
        self.add(a, 1, 5, 1, 'one')
        self.add(b, 2, 5, 1, 'different')
        sink = Sink()
        with self.assertRaises(ValueError):
            stage_all([Shard('a', a), Shard('b', b)], self.username, sink, 1)
        self.assertEqual(sink.writes, 1)
        self.assertTrue(sink.rolled_back)
        self.assertEqual(sink.pending, [])
        self.assertEqual(sink.published, [])
        self.assertFalse(a.in_transaction)
        self.assertFalse(b.in_transaction)

    def test_duplicate_requires_matching_shard_specific_sender(self):
        a, b = self.make(), self.make()
        self.add(a, 1, 5, 1, 'same')
        self.add(b, 2, 5, 1, 'same')
        for left, right in ((None, None), ({42: 'first'}, {42: 'second'})):
            with self.subTest(left=left, right=right), self.assertRaisesRegex(ValueError, 'sender identity'):
                list(read_pages([Shard('a', a, left), Shard('b', b, right)], self.username))
        self.assertFalse(a.in_transaction)
        self.assertFalse(b.in_transaction)

    def test_temp_shadow_cannot_replace_main_table(self):
        conn = self.make()
        self.add(conn, 1, 0, 1, 'main')
        conn.execute(f'CREATE TEMP TABLE "{self.table}" (local_id INTEGER, local_type INTEGER, server_id INTEGER, real_sender_id INTEGER, create_time INTEGER, message_content TEXT, packed_info_data BLOB, sort_seq INTEGER)')
        conn.execute(f'INSERT INTO temp."{self.table}" VALUES (1, 1, 0, 1, 1, ?, NULL, 1)', ('shadow',))
        conn.commit()
        pages = list(read_pages([Shard('a', conn)], self.username))
        self.assertEqual(pages[0][0].message_content, 'main')

    def test_sender_lookup_and_staging_use_same_pinned_snapshot(self):
        with tempfile.TemporaryDirectory(dir='.tmp') as directory:
            path = Path(directory) / 'synthetic_snapshot.sqlite'
            writer = sqlite3.connect(path)
            try:
                writer.execute('PRAGMA journal_mode=WAL')
                writer.execute(f'CREATE TABLE "{self.table}" (local_id INTEGER, local_type INTEGER, server_id INTEGER, real_sender_id INTEGER, create_time INTEGER, message_content TEXT, packed_info_data BLOB, sort_seq INTEGER)')
                writer.execute(f'CREATE INDEX ordering ON "{self.table}" (sort_seq, create_time, local_id)')
                writer.execute('CREATE TABLE Name2Id (user_name TEXT)')
                writer.execute('INSERT INTO Name2Id(rowid, user_name) VALUES (42, ?)', ('before',))
                writer.execute(f'INSERT INTO "{self.table}" VALUES (1, 1, 7, 42, 1, ?, NULL, 1)', ('synthetic',))
                writer.commit()
                reader = sqlite3.connect(f'{path.as_uri()}?mode=ro', uri=True)
                try:
                    class SnapshotLookup:
                        changed = False

                        def get(self, rowid):
                            if not self.changed:
                                self.changed = True
                                writer.execute('UPDATE Name2Id SET user_name=? WHERE rowid=42', ('after',))
                                writer.commit()
                            row = reader.execute('SELECT user_name FROM main.Name2Id WHERE rowid=?', (rowid,)).fetchone()
                            return row[0] if row else None

                    class Sink:
                        staged_name = None
                        committed = False

                        def begin(self):
                            pass

                        def write_page(self, page):
                            self.staged_name = lookup.get(page[0].real_sender_id)

                        def commit(self):
                            self.committed = True

                        def rollback(self):
                            self.staged_name = None

                    lookup, sink = SnapshotLookup(), Sink()
                    stage_all([Shard('a', reader, lookup)], self.username, sink, 1)
                    self.assertTrue(sink.committed)
                    self.assertEqual(sink.staged_name, 'before')
                    self.assertEqual(writer.execute('SELECT user_name FROM Name2Id WHERE rowid=42').fetchone()[0], 'after')
                    self.assertFalse(reader.in_transaction)
                finally:
                    reader.close()
            finally:
                writer.close()

    def test_unindexed_shard_streams_without_temporary_sort_or_omissions(self):
        indexed, unindexed = self.make(), self.make()
        unindexed.execute('DROP INDEX ordering')
        unindexed.commit()
        self.add(indexed, 1, 10, 7, 'indexed newer')
        self.add(indexed, 2, 11, 2, 'indexed older')
        self.add(unindexed, 1, 12, 9, 'unindexed newer')
        self.add(unindexed, 2, 13, 1, 'unindexed older')
        queries = []
        indexed.set_trace_callback(queries.append)
        unindexed.set_trace_callback(queries.append)
        rows = [m for page in read_pages([Shard('a', indexed), Shard('b', unindexed)], self.username, 1) for m in page]
        self.assertEqual({m.server_id for m in rows}, {10, 11, 12, 13})
        self.assertEqual(len(rows), 4)
        self.assertFalse(any('ORDER BY' in q.upper() for q in queries if q.upper().startswith('SELECT') and 'MSG_' in q.upper()))
        self.assertFalse(indexed.in_transaction)
        self.assertFalse(unindexed.in_transaction)

    def test_unindexed_duplicate_conflicts_still_fail_and_rollback(self):
        a, b = self.make(), self.make()
        a.execute('DROP INDEX ordering')
        a.commit()
        self.add(a, 1, 5, 1, 'first')
        self.add(b, 1, 5, 1, 'different')
        with self.assertRaisesRegex(ValueError, 'conflicting cross-shard'):
            list(read_pages([Shard('a', a, {42: 'same'}), Shard('b', b, {42: 'same'})], self.username, 1))
        self.assertFalse(a.in_transaction)
        self.assertFalse(b.in_transaction)

    def test_large_synthetic_pages_are_staged_then_committed(self):
        class Sink:
            def __init__(self):
                self.pending = 0
                self.committed = False
                self.maximum = 0

            def begin(self):
                pass

            def write_page(self, page):
                self.maximum = max(self.maximum, len(page))
                self.pending += len(page)

            def commit(self):
                self.committed = True

            def rollback(self):
                self.pending = 0

        conn = self.make()
        conn.executemany(f'INSERT INTO "{self.table}" VALUES (?, 1, ?, 42, ?, ?, NULL, ?)',
                         ((i, i + 1, i, 'synthetic', i) for i in range(2000)))
        conn.commit()
        sink = Sink()
        stage_all([Shard('a', conn)], self.username, sink, 37)
        self.assertTrue(sink.committed)
        self.assertEqual(sink.pending, 2000)
        self.assertLessEqual(sink.maximum, 37)

    def test_optional_compressed_column_and_conflicting_duplicates(self):
        a, b = self.make(), self.make()
        a.execute(f'ALTER TABLE "{self.table}" ADD COLUMN compress_content BLOB')
        a.execute(f'INSERT INTO "{self.table}" VALUES (1, 1, 8, 42, 1, NULL, NULL, 1, ?)', (b'compressed-blob',))
        a.commit()
        b.execute(f'INSERT INTO "{self.table}" VALUES (2, 1, 8, 42, 1, NULL, NULL, 1)')
        b.commit()
        ma = list(read_pages([Shard('a', a)], self.username))[0][0]
        mb = list(read_pages([Shard('b', b)], self.username))[0][0]
        self.assertTrue(ma.has_compress_content)
        self.assertEqual(ma.compress_content, b'compressed-blob')
        self.assertFalse(mb.has_compress_content)
        self.assertIsNone(mb.compress_content)
        with self.assertRaisesRegex(ValueError, 'conflicting'):
            list(read_pages([Shard('a', a, {42: 'sender'}), Shard('b', b, {42: 'sender'})], self.username))

    def test_bad_schema_and_open_transaction_fail_closed(self):
        a = self.make()
        b = sqlite3.connect(':memory:')
        self.connections.append(b)
        b.execute(f'CREATE TABLE "{self.table}" (local_id INTEGER)')
        with self.assertRaises(ValueError):
            list(read_pages([Shard('a', a), Shard('b', b)], self.username))
        a.execute('BEGIN')
        with self.assertRaises(ValueError):
            list(read_pages([Shard('a', a)], self.username))
        a.rollback()

    def test_encrypted_sqlcipher_scope_wrong_key_and_wal(self):
        try:
            import sqlcipher3
        except ImportError:
            self.skipTest('optional project-local SQLCipher environment unavailable')
        with tempfile.TemporaryDirectory(dir='.tmp') as directory:
            path = Path(directory) / 'synthetic_encrypted.sqlite'
            key = secrets.token_hex(32)
            wrong = secrets.token_hex(32)
            writer = sqlcipher3.connect(str(path))
            try:
                writer.execute(f"PRAGMA key = \"x'{key}'\"")
                writer.execute('PRAGMA journal_mode=WAL')
                writer.execute('PRAGMA wal_autocheckpoint=0')
                columns = '(local_id INTEGER, local_type INTEGER, server_id INTEGER, real_sender_id INTEGER, create_time INTEGER, message_content TEXT, packed_info_data BLOB, sort_seq INTEGER)'
                other = conversation_table('wxid_synthetic_other')
                writer.execute(f'CREATE TABLE "{self.table}" {columns}')
                writer.execute(f'CREATE TABLE "{other}" {columns}')
                writer.execute(f'CREATE INDEX ordering ON "{self.table}" (sort_seq, create_time, local_id)')
                writer.execute(f'INSERT INTO "{self.table}" VALUES (1, 1, 10, 1, 1, ?, NULL, 1)', ('target synthetic',))
                writer.execute(f'INSERT INTO "{other}" VALUES (1, 1, 20, 1, 1, ?, NULL, 1)', (secrets.token_hex(32),))
                writer.commit()
                self.assertGreater(path.with_name(path.name + '-wal').stat().st_size, 0)
                reader = sqlcipher3.connect(f'{path.as_uri()}?mode=ro', uri=True)
                bad = sqlcipher3.connect(f'{path.as_uri()}?mode=ro', uri=True)
                try:
                    reader.execute(f"PRAGMA key = \"x'{key}'\"")
                    bad.execute(f"PRAGMA key = \"x'{wrong}'\"")
                    writer.execute(f'INSERT INTO "{self.table}" VALUES (2, 1, 11, 1, 2, ?, NULL, 2)', ('uncommitted',))
                    output = [m for p in read_pages([Shard('encrypted', reader)], self.username) for m in p]
                    self.assertEqual(len(output), 1)
                    self.assertTrue(output[0].message_content == 'target synthetic')
                    with self.assertRaises(sqlcipher3.DatabaseError):
                        list(read_pages([Shard('bad-key', bad)], self.username))
                finally:
                    reader.close()
                    bad.close()
                    writer.rollback()
            finally:
                writer.close()

    def test_committed_wal_visible_uncommitted_hidden(self):
        with tempfile.TemporaryDirectory(dir='.tmp') as directory:
            path = Path(directory) / 'synthetic.sqlite'
            writer = sqlite3.connect(path)
            try:
                writer.execute('PRAGMA journal_mode=WAL')
                writer.execute('PRAGMA wal_autocheckpoint=0')
                writer.execute(f'CREATE TABLE "{self.table}" (local_id INTEGER, local_type INTEGER, server_id INTEGER, real_sender_id INTEGER, create_time INTEGER, message_content TEXT, packed_info_data BLOB, sort_seq INTEGER)')
                writer.execute(f'CREATE INDEX ordering ON "{self.table}" (sort_seq, create_time, local_id)')
                writer.execute(f'INSERT INTO "{self.table}" VALUES (1, 1, 10, 1, 1, "committed", NULL, 1)')
                writer.commit()
                self.assertGreater(path.with_name(path.name + '-wal').stat().st_size, 0)
                reader = sqlite3.connect(f'{path.as_uri()}?mode=ro', uri=True)
                try:
                    writer.execute(f'INSERT INTO "{self.table}" VALUES (2, 1, 11, 1, 2, "uncommitted", NULL, 2)')
                    pages = list(read_pages([Shard('wal', reader)], self.username))
                    self.assertEqual([m.message_content for p in pages for m in p], ['committed'])
                finally:
                    reader.close()
                    writer.rollback()
            finally:
                writer.close()


if __name__ == '__main__':
    unittest.main()
