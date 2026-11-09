"""Real import orchestration tested with in-memory mock sources only."""

import sqlite3
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

from src.wechat_import_fence import ImportFence
from src.wechat_local_binding import read_binding
from src.wechat_private_context import config_for_owner
from src.wechat_private_store import WeChatPrivateStore
from src.wechat_real_import import _RealStage, import_selected_conversation
from src.wechat_single_session import conversation_table

ROOT = Path(__file__).resolve().parents[1]
SCOPE = "fixture:1111111111:2222222222"
AUTH = dict(scope=SCOPE, actor="2222222222", verified=True)


class FakeSession:
    def __init__(self, contact, shards, *, fail_at=0):
        self.contact, self.shards = contact, shards
        self.calls, self.fail_at = 0, fail_at

    def revalidate(self):
        self.calls += 1
        return self.calls != self.fail_at


class RealOrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / ".tmp")
        self.root = Path(self.temp.name).resolve()
        self.fence = ImportFence(self.root)
        self.fence.enable()
        self.account = self.root / "wxid_fakeowner_abcd"
        self.contact_path = str(self.account / "db_storage/contact/contact.db")
        self.message_paths = [
            str(self.account / f"db_storage/message/message_{i}.db") for i in range(2)
        ]
        self.contact = sqlite3.connect(":memory:")
        self.contact.execute(
            "CREATE TABLE contact(username TEXT,alias TEXT,nick_name TEXT,remark TEXT)"
        )
        self.contact.executemany(
            "INSERT INTO contact VALUES(?,?,?,?)",
            [
                ("wxid_fakeowner", "fake-owner-display", "owner", ""),
                ("wxid_faketarget", "fake-target-display", "target", "fake-label"),
            ],
        )
        self.contact.commit()
        self.contact.execute("PRAGMA query_only=ON")
        self.shards = []
        table = conversation_table("wxid_faketarget")
        for i in range(2):
            conn = sqlite3.connect(":memory:")
            conn.execute("CREATE TABLE Name2Id(user_name TEXT)")
            conn.execute(
                "INSERT INTO Name2Id(rowid,user_name) VALUES(1,?)",
                ("wxid_fakeowner" if i == 0 else "wxid_faketarget",),
            )
            conn.execute(
                f'CREATE TABLE "{table}" (local_id INTEGER,local_type INTEGER,server_id INTEGER,real_sender_id INTEGER,create_time INTEGER,message_content TEXT,packed_info_data BLOB,sort_seq INTEGER)'
            )
            conn.execute(
                f'CREATE INDEX ord ON "{table}" (sort_seq,create_time,local_id)'
            )
            conn.execute(
                f'INSERT INTO "{table}" VALUES(1,1,?,1,1600000000,?,NULL,?)',
                (101 + i, "synthetic text", i + 1),
            )
            conn.commit()
            conn.execute("PRAGMA query_only=ON")
            self.shards.append((f"message_{i}.db", conn))
        self.session = FakeSession(self.contact, self.shards)
        self.runner_called = 0

    def tearDown(self):
        self.contact.close()
        for _, conn in self.shards:
            conn.close()
        self.temp.cleanup()

    def runner(self, hwnd, contact, paths, thumbprint, *, operation):
        self.runner_called += 1
        result = operation(self.session)
        if self.session.revalidate() is not True:
            raise RuntimeError("synthetic postcheck failed")
        return result

    def run_action(self, **overrides):
        args = dict(
            project_root=self.root,
            owner_scope=SCOPE,
            hwnd=1,
            contact_path=self.contact_path,
            message_paths=self.message_paths,
            account_dir=str(self.account),
            signer_thumbprint="A" * 40,
            owner_display_id="fake-owner-display",
            target_display_id="fake-target-display",
            window_confirmed=True,
            target_label="fake-label",
            initialize_new_store=True,
            _test_source_runner=self.runner,
        )
        args.update(overrides)
        return import_selected_conversation(**args)

    def read_destination(self):
        return WeChatPrivateStore(
            self.root,
            owner_scope=SCOPE,
            owner_qq="2222222222",
            account_id="wxid_fakeowner",
            conversation_id="wxid_faketarget",
            owner_sender_id="wxid_fakeowner",
            contact_sender_id="wxid_faketarget",
        )

    def test_success_returns_only_counts_and_records_actual_roles(self):
        result = self.run_action()
        self.assertEqual(result["status"], "imported")
        self.assertEqual(result["counts"]["imported"], 2)
        self.assertEqual(result["directory_check"], "matched")
        self.assertFalse(result["history_complete"])
        self.assertFalse(result["media_inspected"])
        self.assertGreaterEqual(self.session.calls, 3)
        self.assertNotIn("fakeowner", repr(result))
        binding = read_binding(self.root, SCOPE)
        self.assertEqual(binding["account_id"], "wxid_fakeowner")
        self.assertEqual(binding["conversation_id"], "wxid_faketarget")
        store = self.read_destination()
        try:
            rows = store.search(**AUTH, query="", snapshot=store.snapshot(**AUTH))
            self.assertEqual({r["sender_role"] for r in rows}, {"owner", "contact"})
        finally:
            store.close()

    def test_import_binding_is_visible_only_in_owner_private_scope(self):
        self.assertEqual(self.run_action()["status"], "imported")
        service = SimpleNamespace(
            config={"instance_id": "fixture", "bot_qq": "1111111111", "owner_qq": "2222222222"},
            peer_qq="2222222222", scope=SCOPE, current_request=None,
            wechat_project_root=self.root, diagnostics_directory=self.root / "runtime",
        )
        self.assertEqual(config_for_owner(service)["conversation_id"], "wxid_faketarget")
        service.peer_qq = "2900000000"
        self.assertIsNone(config_for_owner(service))
        service.peer_qq = "2222222222"
        service.scope = "fixture:1111111111:group:123"
        self.assertIsNone(config_for_owner(service))

    def test_missing_confirmation_and_invalid_paths_do_not_call_source(self):
        self.assertEqual(
            self.run_action(window_confirmed=False)["status"], "needs_confirmation"
        )
        self.assertEqual(
            self.run_action(message_paths=[str(self.root / "wrong.db")])["status"],
            "needs_confirmation",
        )
        self.assertEqual(self.runner_called, 0)
        self.assertFalse((self.root / "runtime/owner-private/wechat.sqlite3").exists())

    def test_duplicate_identity_and_directory_conflict_are_not_imported(self):
        self.contact.execute("PRAGMA query_only=OFF")
        self.contact.execute(
            "INSERT INTO contact VALUES(?,?,?,?)",
            ("fake-other", "fake-target-display", "other", ""),
        )
        self.contact.commit()
        self.contact.execute("PRAGMA query_only=ON")
        self.assertEqual(self.run_action()["status"], "needs_confirmation")
        self.assertFalse((self.root / "runtime/owner-private/wechat.sqlite3").exists())
        self.contact.execute("PRAGMA query_only=OFF")
        self.contact.execute("DELETE FROM contact WHERE username=?", ("fake-other",))
        self.contact.commit()
        self.contact.execute("PRAGMA query_only=ON")
        other_dir = self.root / "wxid_someone_abcd"
        self.assertEqual(
            self.run_action(
                account_dir=str(other_dir),
                contact_path=str(other_dir / "db_storage/contact/contact.db"),
                message_paths=[str(other_dir / "db_storage/message/message_0.db")],
            )["status"],
            "needs_confirmation",
        )

    def test_change_before_publish_rolls_back(self):
        self.session.fail_at = 2
        result = self.run_action()
        self.assertNotEqual(result["status"], "imported")
        store = self.read_destination()
        try:
            self.assertEqual(
                store.search(**AUTH, query="", snapshot=store.snapshot(**AUTH)), []
            )
        finally:
            store.close()

    def test_post_commit_source_failure_is_unknown_not_false_rollback(self):
        self.session.fail_at = 3
        result = self.run_action()
        self.assertEqual(result["status"], "not_confirmed")
        self.assertEqual(result["commit_status"], "unknown")
        self.assertIsNone(read_binding(self.root, SCOPE))
        store = self.read_destination()
        try:
            self.assertEqual(
                len(store.search(**AUTH, query="", snapshot=store.snapshot(**AUTH))), 2
            )
        finally:
            store.close()

    def test_existing_binding_is_not_reimported_without_suspension_protocol(self):
        self.assertEqual(self.run_action()["status"], "imported")
        self.assertEqual(self.run_action()["status"], "needs_setup")
        self.assertEqual(self.runner_called, 1)

    def test_revocation_before_binding_publish_prevents_exposure(self):
        original = self.runner

        def revoked_runner(*args, **kwargs):
            result = original(*args, **kwargs)
            self.fence.request_revoke(lambda: None).result(timeout=5)
            return result

        result = self.run_action(_test_source_runner=revoked_runner)
        self.assertNotEqual(result["status"], "imported")
        self.assertIsNone(read_binding(self.root, SCOPE))

    def test_cancel_during_import_and_existing_revoked_store_not_reenabled(self):
        original = _RealStage.write_page

        def cancel(stage, page):
            original(stage, page)
            stage.token.cancel()

        with patch.object(_RealStage, "write_page", cancel):
            self.assertEqual(
                self.run_action()["status"], "cancelled_or_access_disabled"
            )
        store = self.read_destination()
        try:
            self.assertEqual(
                store.search(**AUTH, query="", snapshot=store.snapshot(**AUTH)), []
            )
            store.set_access(**AUTH, enabled=False)
        finally:
            store.close()
        self.assertEqual(self.run_action()["status"], "needs_setup")


if __name__ == "__main__":
    unittest.main()
