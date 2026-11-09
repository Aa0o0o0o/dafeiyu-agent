"""Focused rollback and redaction tests with synthetic records only."""
from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from src.wechat_local_binding import read_binding
from src.wechat_real_import import _RealStage
from tests.test_wechat_real_import import RealOrchestrationTests, SCOPE, AUTH
from tools.wechat_readonly_probe.import_diagnostic import diagnose


class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.fixture = RealOrchestrationTests(methodName="test_success_returns_only_counts_and_records_actual_roles")
        self.fixture.setUp()

    def tearDown(self):
        self.fixture.tearDown()

    def test_first_page_is_rolled_back_and_never_bound(self):
        result = diagnose(self.fixture.run_action)
        self.assertIn(["FIRST_PAGE", "DECODED_AND_STAGED"], result["events"])
        self.assertIn(["ROLLBACK", "EXPECTED"], result["events"])
        self.assertIsNone(read_binding(self.fixture.root, SCOPE))
        store = self.fixture.read_destination()
        try:
            self.assertEqual(store.search(**AUTH, query="", snapshot=store.snapshot(**AUTH)), [])
        finally:
            store.close()
        self.assertNotIn("synthetic text", json.dumps(result))
        self.assertNotIn("fakeowner", json.dumps(result))

    def test_exception_content_is_not_emitted(self):
        def fail(*args, **kwargs):
            raise ValueError("private-example-text")
        with patch.object(_RealStage, "write_page", fail):
            result = diagnose(self.fixture.run_action)
        self.assertIn(["FIRST_PAGE", "VALUE_ERROR"], result["events"])
        self.assertNotIn("private-example-text", json.dumps(result))
        self.assertIsNone(read_binding(self.fixture.root, SCOPE))


if __name__ == "__main__":
    unittest.main()