"""Focused synthetic coverage for metadata, references and transaction boundaries."""
from __future__ import annotations

import unittest

from src.wechat_quote_parser import extract_quote, image_digest_hint
from src.wechat_source_enrichment import enrich_existing
from tests import test_wechat_real_import as fixture_module

SCOPE = fixture_module.SCOPE
AUTH = fixture_module.AUTH
QUOTE = '<msg><appmsg><title>合成回答</title><type>57</type><refermsg><type>1</type><displayname>对方</displayname><content>引用里的蓝色纸飞机</content></refermsg></appmsg></msg>'


class SourceEnrichmentTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture_module.RealOrchestrationTests(methodName='test_success_returns_only_counts_and_records_actual_roles')
        self.fixture.setUp()
        self.digest = 'abcde0123456789fabcde0123456789f'
        conn = self.fixture.shards[0][1]
        conn.execute('PRAGMA query_only=OFF')
        table = fixture_module.conversation_table('wxid_faketarget')
        conn.execute(f'UPDATE "{table}" SET local_type=49,message_content=? WHERE server_id=101', (QUOTE,))
        conn.execute(f'INSERT INTO "{table}" VALUES(3,3,103,1,1600000000,?,NULL,3)', (self.digest,))
        conn.commit()
        conn.execute('PRAGMA query_only=ON')
        result = self.fixture.run_action()
        self.assertEqual(result['status'], 'imported')

    def tearDown(self):
        self.fixture.tearDown()

    def enrich(self):
        f = self.fixture
        return enrich_existing(project_root=f.root, owner_scope=SCOPE, hwnd=1,
            contact_path=f.contact_path, message_paths=f.message_paths, account_dir=str(f.account),
            signer_thumbprint='A'*40, owner_display_id='fake-owner-display',
            target_display_id='fake-target-display', window_confirmed=True,
            target_label='fake-label', _test_source_runner=f.runner)

    def test_quote_image_hints_visible_to_owner_without_changing_original(self):
        result = self.enrich()
        self.assertEqual(result['status'], 'updated')
        self.assertEqual(result['updated'], 3)
        self.assertEqual(result['quotes'], 1)
        self.assertEqual(result['images_with_hint'], 1)
        store = self.fixture.read_destination()
        try:
            epoch = store.snapshot(**AUTH)
            results = store.search_relevant(**AUTH, query='蓝色纸飞机', snapshot=epoch)
            quote = next(x for x in results if x['message_id'] == 'server:101')
            self.assertEqual(quote['kind'], 'quote')
            self.assertIn('引用里的蓝色纸飞机', quote['text'])
            self.assertEqual(store.db.execute("SELECT kind,body FROM wx_private_message WHERE message='server:101'").fetchone(), ('other', ''))
            row = store.db.execute("SELECT image_digest,image_digest_status FROM wx_private_source_meta WHERE message='server:103'").fetchone()
            self.assertEqual(row, (self.digest, 'heuristic_unverified'))
            store.clear(**AUTH, snapshot=epoch)
            self.assertEqual(store.db.execute('SELECT count(*) FROM wx_private_source_meta').fetchone()[0], 0)
        finally:
            store.close()

    def test_post_read_validation_failure_rolls_back_without_losing_originals(self):
        self.fixture.session.calls = 0
        self.fixture.session.fail_at = 2
        result = self.enrich()
        self.assertNotEqual(result['status'], 'updated')
        store = self.fixture.read_destination()
        try:
            self.assertEqual(store.db.execute('SELECT count(*) FROM wx_private_source_meta').fetchone()[0], 0)
            self.assertEqual(store.db.execute('SELECT count(*) FROM wx_private_message').fetchone()[0], 3)
        finally:
            store.close()

    def test_safe_parser_rejects_entities_and_ambiguous_digest(self):
        self.assertEqual(extract_quote(49, '<!DOCTYPE msg [<!ENTITY x "evil">]><msg>&x;</msg>').status, 'unsupported')
        self.assertEqual(extract_quote((57 << 32) | 49, QUOTE).status, 'decoded')
        self.assertEqual(image_digest_hint(3, self.digest + ' ' + ('0' * 32))[1], 'ambiguous')


if __name__ == '__main__':
    unittest.main()