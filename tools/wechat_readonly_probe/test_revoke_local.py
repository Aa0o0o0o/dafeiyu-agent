"""Revoke tests using generated project roots only, never the real project."""
from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from src.wechat_import_fence import ImportFence
from src.wechat_private_store import WeChatPrivateStore
from tools.wechat_readonly_probe import revoke_local

ROOT = Path(__file__).resolve().parents[2]
SCOPE = 'fixture:1111111111:2222222222'
AUTH = dict(scope=SCOPE, actor='2222222222', verified=True)


class RevokeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT / '.tmp')
        self.root = Path(self.tmp.name)
        (self.root / 'config').mkdir()
        (self.root / 'config' / 'settings.json').write_text(json.dumps({
            'instance_id': 'fixture', 'bot_qq': '1111111111', 'owner_qq': '2222222222'
        }), encoding='utf-8')
        self.store = None

    def tearDown(self):
        if self.store is not None:
            self.store.close()
        self.tmp.cleanup()

    def prepare(self):
        self.store = WeChatPrivateStore(self.root, owner_scope=SCOPE,
            owner_qq='2222222222', account_id='fake-owner',
            conversation_id='fake-contact', owner_sender_id='fake-owner',
            contact_sender_id='fake-contact')
        self.store.set_access(**AUTH, enabled=True)
        epoch = self.store.snapshot(**AUTH)
        self.store.import_batch(**AUTH, batch_id='fake-batch', snapshot=epoch, messages=[{
            'conversation_id': 'fake-contact', 'message_id': 'fake-message',
            'sender_id': 'fake-owner', 'occurred_at': time.time(),
            'kind': 'text', 'text': '合成内容' * 1200,
        }])
        self.fence = ImportFence(self.root)
        self.fence.enable()
        self.directory = self.root / 'runtime' / 'owner-private'
        self.binding = self.directory / 'binding.json'
        self.binding.write_text(json.dumps({'schema': 1, 'owner_scope': SCOPE}), encoding='utf-8')

    def test_missing_is_idempotent_and_no_files_created(self):
        self.assertEqual(revoke_local._run_for_root(self.root), 'NO_DATA')
        self.assertFalse((self.root / 'runtime').exists())

    def test_committed_revoke_and_repeat_tombstones_only_owner(self):
        self.prepare()
        self.assertGreater(self.store.db.execute('SELECT count(*) FROM wx_private_chunk').fetchone()[0], 0)
        self.store.db.execute("INSERT INTO wx_private_message VALUES ('other-scope','other','other-message','other-batch','sender','unknown',0,'text','synthetic',0)")
        self.store.db.commit()
        self.assertEqual(revoke_local._run_for_root(self.root), 'REVOKED')
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM wx_private_message WHERE scope=?', (SCOPE,)).fetchone()[0], 0)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM wx_private_chunk WHERE scope=?', (SCOPE,)).fetchone()[0], 0)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM wx_private_tombstone WHERE scope=?', (SCOPE,)).fetchone()[0], 1)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM wx_private_message WHERE scope='other-scope'").fetchone()[0], 1)
        self.assertFalse(self.binding.exists())
        self.assertEqual(revoke_local._run_for_root(self.root), 'REVOKED')
        self.assertFalse(self.fence.valid(self.fence.token()) if self._can_token() else False)

    def _can_token(self):
        try:
            self.fence.token()
            return True
        except Exception:
            return False

    def test_failed_cleanup_stays_pending_and_retry_succeeds(self):
        self.prepare()
        with patch.object(revoke_local, '_clean_existing', side_effect=RuntimeError('synthetic')):
            self.assertEqual(revoke_local._run_for_root(self.root), 'PENDING')
        with closing(sqlite3.connect(self.fence.path)) as db:
            self.assertEqual(db.execute('SELECT enabled,pending FROM import_control').fetchone(), (0, 1))
        self.assertEqual(revoke_local._run_for_root(self.root), 'REVOKED')
        self.assertFalse(self.binding.exists())

    def test_bad_binding_rejects_and_does_not_remove_data(self):
        self.prepare()
        self.binding.write_text(json.dumps({'schema': 1, 'owner_scope': 'other'}), encoding='utf-8')
        self.assertEqual(revoke_local._run_for_root(self.root), 'PENDING')
        self.assertTrue(self.binding.exists())
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM wx_private_message').fetchone()[0], 1)
        with closing(sqlite3.connect(self.fence.path)) as db:
            self.assertEqual(db.execute('SELECT enabled,pending FROM import_control').fetchone(), (0, 1))

    def test_waits_for_active_destination_transaction(self):
        self.prepare()
        locker = sqlite3.connect(self.directory / 'wechat.sqlite3', timeout=1)
        locker.execute('BEGIN IMMEDIATE')
        result = []
        thread = threading.Thread(target=lambda: result.append(revoke_local._run_for_root(self.root)))
        try:
            thread.start()
            deadline = time.monotonic() + 3
            state = None
            while time.monotonic() < deadline:
                with closing(sqlite3.connect(self.fence.path)) as db:
                    state = db.execute('SELECT enabled,pending FROM import_control').fetchone()
                if state == (0, 1):
                    break
                time.sleep(0.02)
            self.assertEqual(state, (0, 1))
            self.assertTrue(thread.is_alive())
        finally:
            locker.rollback()
            locker.close()
            thread.join(timeout=10)
        self.assertEqual(result, ['REVOKED'])
        self.assertFalse(self.binding.exists())

    def test_reparse_component_fails_without_changes(self):
        self.prepare()
        original = Path.is_junction
        def fake_junction(path):
            return path == self.directory or original(path)
        with patch.object(Path, 'is_junction', fake_junction):
            self.assertEqual(revoke_local._run_for_root(self.root), 'PENDING')
        self.assertTrue(self.binding.exists())
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM wx_private_message').fetchone()[0], 1)


if __name__ == '__main__':
    unittest.main()
