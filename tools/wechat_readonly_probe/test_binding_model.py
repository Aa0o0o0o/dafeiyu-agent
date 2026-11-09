"""Only made-up process/window/account/file identities are used here."""
import unittest
from dataclasses import replace

from binding_model import DatabaseObservation, FileIdentity, Process, Window, synthetic_consistent


class SyntheticBindingTests(unittest.TestCase):
    def setUp(self):
        self.process = Process(123, 999, True)
        self.window = Window(456, 123, 999, "wxid_fake_account")
        self.database = DatabaseObservation(
            123, 999, FileIdentity(4, 1), FileIdentity(4, 2), FileIdentity(4, 3),
            "wxid_fake_account", True, True, True,
        )

    def consistent(self, p=None, w=None, d=None, p2=None, w2=None, d2=None):
        first_p, first_w, first_d = p or self.process, w or self.window, d or self.database
        return synthetic_consistent(first_p, first_w, first_d,
                                    p2 or first_p, w2 or first_w, d2 or first_d)

    def test_complete_fake_evidence_is_only_consistent(self):
        self.assertTrue(self.consistent())

    def test_wrong_account_nickname_and_circular_evidence_fail(self):
        self.assertFalse(self.consistent(d=replace(self.database, self_db_account_id="wxid_other")))
        self.assertFalse(self.consistent(w=replace(self.window, visible_stable_account_id="nickname")))
        self.assertFalse(self.consistent(d=replace(self.database, account_from_independent_record=False)))

    def test_pid_reuse_window_switch_and_signature_fail(self):
        self.assertFalse(self.consistent(p2=replace(self.process, created=1000)))
        self.assertFalse(self.consistent(w2=replace(self.window, hwnd=9999)))
        self.assertFalse(self.consistent(p=replace(self.process, signature_verified=False)))
        self.assertFalse(self.consistent(w=replace(self.window, pid=124)))
        self.assertFalse(self.consistent(d=replace(self.database, holder_created=1000)))

    def test_file_swap_cross_volume_unbound_handle_and_missing_root_fail(self):
        self.assertFalse(self.consistent(d2=replace(self.database, target_db_id=FileIdentity(4, 99))))
        self.assertFalse(self.consistent(d=replace(self.database, target_db_id=FileIdentity(5, 3))))
        self.assertFalse(self.consistent(d=replace(self.database, target_db_id=self.database.self_db_id)))
        self.assertFalse(self.consistent(d=replace(self.database, process_holds_expected_files=False)))
        self.assertFalse(self.consistent(d=replace(self.database, root_membership_verified=False)))


if __name__ == "__main__":
    unittest.main()

