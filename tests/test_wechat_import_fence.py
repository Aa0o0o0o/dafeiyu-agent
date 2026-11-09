"""Cross-process and cancellation ordering tests with synthetic control data."""

import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from pathlib import Path

from src.wechat_import_fence import ImportCancelled, ImportFence

ROOT = Path(__file__).resolve().parents[1]


class ImportFenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT / ".tmp")
        self.fence = ImportFence(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_disabled_by_default_and_token_cannot_cross_control_files(self):
        with self.assertRaises(ImportCancelled):
            self.fence.token()
        self.fence.enable()
        token = self.fence.token()
        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as other:
            other_fence = ImportFence(other)
            other_fence.enable()
            self.assertFalse(other_fence.valid(token))
            with self.assertRaises(ImportCancelled):
                other_fence.publish(token, lambda: None)

    def test_cancel_signal_is_immediate_cleanup_completion_is_separate(self):
        self.fence.enable()
        token = self.fence.token()
        entered, release = threading.Event(), threading.Event()

        def cleanup():
            entered.set()
            if not release.wait(3):
                raise RuntimeError("synthetic cleanup timeout")

        start = time.monotonic()
        future = self.fence.request_revoke(cleanup)
        self.assertLess(time.monotonic() - start, 0.5)
        self.assertTrue(token.cancelled.is_set())
        self.assertTrue(entered.wait(2))
        self.assertFalse(future.done())
        self.assertFalse(self.fence.valid(token))
        release.set()
        future.result(3)
        self.fence.enable()
        self.assertTrue(self.fence.valid(self.fence.token()))

    def test_token_created_concurrently_with_revoke_is_already_cancelled(self):
        self.fence.enable()
        observed, release = threading.Event(), threading.Event()
        original = ImportFence._row
        holder, errors = [], []

        def blocked(db):
            row = original(db)
            if threading.current_thread().name == "synthetic-token":
                observed.set()
                if not release.wait(3):
                    raise RuntimeError("synthetic token timeout")
            return row

        def obtain():
            try:
                holder.append(self.fence.token())
            except Exception as exc:
                errors.append(exc)

        with patch.object(ImportFence, "_row", side_effect=blocked):
            thread = threading.Thread(target=obtain, name="synthetic-token")
            thread.start()
            self.assertTrue(observed.wait(2))
            future = self.fence.request_revoke(lambda: None)
            release.set()
            thread.join(3)
            future.result(3)
        self.assertEqual(errors, [])
        self.assertEqual(len(holder), 1)
        self.assertTrue(holder[0].cancelled.is_set())
        self.assertFalse(self.fence.valid(holder[0]))

    def test_other_control_instance_revokes_before_publish(self):
        self.fence.enable()
        token = self.fence.token()
        other = ImportFence(self.tmp.name)
        other.request_revoke(lambda: None).result(3)
        called = []
        with self.assertRaises(ImportCancelled):
            self.fence.publish(token, lambda: called.append(True))
        self.assertEqual(called, [])

    def test_publish_barrier_orders_commit_before_later_revoke_cleanup(self):
        self.fence.enable()
        token = self.fence.token()
        other = ImportFence(self.tmp.name)
        entered, release = threading.Event(), threading.Event()
        order, errors = [], []

        def commit():
            entered.set()
            if not release.wait(3):
                raise RuntimeError("synthetic commit timeout")
            order.append("committed")

        def publish():
            try:
                self.fence.publish(token, commit)
            except Exception as exc:
                errors.append(exc)

        worker = threading.Thread(target=publish)
        worker.start()
        self.assertTrue(entered.wait(2))
        future = other.request_revoke(lambda: order.append("cleaned"))
        self.assertFalse(future.done())
        release.set()
        worker.join(3)
        future.result(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(order, ["committed", "cleaned"])
        self.assertFalse(self.fence.valid(token))

    def test_failed_cleanup_stays_disabled_until_successful_retry(self):
        self.fence.enable()
        token = self.fence.token()

        def fail():
            raise RuntimeError("synthetic cleanup failed")

        with self.assertRaises(RuntimeError):
            self.fence.request_revoke(fail).result(3)
        self.assertFalse(self.fence.valid(token))
        restarted = ImportFence(self.tmp.name)
        with self.assertRaises(ImportCancelled):
            restarted.enable()
        restarted.request_revoke(lambda: None).result(3)
        restarted.enable()
        self.assertTrue(restarted.valid(restarted.token()))

    def test_cross_process_revocation_and_crash_pending_recovery(self):
        self.fence.enable()
        token = self.fence.token()
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        code = "from src.wechat_import_fence import ImportFence; import os,sys; f=ImportFence(sys.argv[1]); f.request_revoke(lambda: os._exit(17)).result(5)"
        proc = subprocess.run(
            [sys.executable, "-c", code, self.tmp.name],
            cwd=ROOT,
            env=env,
            capture_output=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(proc.returncode, 17)
        self.assertFalse(self.fence.valid(token))
        restarted = ImportFence(self.tmp.name)
        with self.assertRaises(ImportCancelled):
            restarted.enable()
        restarted.request_revoke(lambda: None).result(3)
        restarted.enable()
        self.assertTrue(restarted.valid(restarted.token()))


if __name__ == "__main__":
    unittest.main()
