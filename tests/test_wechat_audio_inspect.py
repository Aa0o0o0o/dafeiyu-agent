import sqlite3
import unittest
from src.wechat_audio_inspect import inspect_voices

class Session:
    def __init__(self, shards):
        self.shards = [(str(i), conn) for i, conn in enumerate(shards)]
    def revalidate(self):
        return True

class AudioTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.execute('CREATE TABLE Name2Id(user_name TEXT)')
        self.db.execute('CREATE TABLE VoiceInfo(chat_name_id INTEGER,svr_id INTEGER,voice_data BLOB)')
        self.db.executemany('INSERT INTO Name2Id(user_name) VALUES (?)', [('wanted',), ('other',)])
        self.db.executemany('INSERT INTO VoiceInfo VALUES (?,?,?)', [(1, 10, b'voice'), (2, 11, b'private-other')])
        self.db.commit()
        self.db.execute('PRAGMA query_only=ON')
    def tearDown(self):
        self.db.close()
    def test_target_scope_and_missing(self):
        seen=[]
        counts=inspect_voices(Session([self.db]), 'wanted', [10,11], lambda value: seen.append(value) or True)
        self.assertEqual(seen,[b'voice'])
        self.assertEqual((counts['available'],counts['decodable'],counts['missing']),(1,1,1))
    def test_duplicate_rows_fail_closed(self):
        self.db.execute('PRAGMA query_only=OFF')
        self.db.execute('INSERT INTO VoiceInfo VALUES (1,10,?)',(b'changed',))
        self.db.commit()
        self.db.execute('PRAGMA query_only=ON')
        result=inspect_voices(Session([self.db]),'wanted',[10],lambda _: True)
        self.assertEqual(result['conflicting'],1)
        self.assertEqual(result['available'],0)
    def test_reject_nonpositive_or_duplicate_ids(self):
        with self.assertRaises(ValueError):
            inspect_voices(Session([self.db]),'wanted',[0],lambda _: True)
        with self.assertRaises(ValueError):
            inspect_voices(Session([self.db]),'wanted',[10,10],lambda _: True)
