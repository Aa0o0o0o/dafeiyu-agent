"""Scope checks for optional local WeChat media source paths."""
import unittest
from tools.wechat_readonly_probe.real_source_session import _message_components, SourceSessionError

class MediaScopeTests(unittest.TestCase):
    def test_media_path_requires_explicit_mode_and_same_account(self):
        contact = r"E:\\synthetic\\account\\db_storage\\contact\\contact.db".replace("\\\\", "\\")
        media = contact.replace(r"contact\\contact.db".replace("\\\\", "\\"), r"message\\media_0.db".replace("\\\\", "\\"))
        self.assertEqual(len(_message_components(contact, (media,), shard_kind="media")), 1)
        with self.assertRaises(SourceSessionError):
            _message_components(contact, (media,))
        with self.assertRaises(SourceSessionError):
            _message_components(contact, (media.replace("account", "different"),), shard_kind="media")
        with self.assertRaises(SourceSessionError):
            _message_components(contact, (media.replace("media_0.db", "message_0.db"),), shard_kind="media")
        with self.assertRaises(SourceSessionError):
            _message_components(contact, (media,), shard_kind="anything")
