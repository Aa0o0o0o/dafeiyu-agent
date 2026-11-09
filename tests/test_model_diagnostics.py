"""Offline provider errors: classification, privacy, ledger and recovery."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
from openai import APIConnectionError, APIStatusError

from src.contracts import ModelResult
from src.model_diagnostics import model_error_details, record_model_error
from tests import test_conversation as fixtures


class ErrorMetadataTests(unittest.TestCase):
    def test_network_timeout_and_http_categories_do_not_store_provider_body(self):
        request = httpx.Request("POST", "https://api.deepseek.com/chat/completions")
        errors = [
            (TimeoutError("PRIVATE_PROMPT_AND_SECRET"), "timeout", None),
            (APIConnectionError(request=request), "connection", None),
            (httpx.ReadTimeout("PRIVATE_PROMPT_AND_SECRET"), "timeout", None),
        ]
        for status, kind in [
            (400, "request"),
            (402, "balance"),
            (429, "rate_limit"),
            (503, "upstream_service"),
        ]:
            response = httpx.Response(status, request=request)
            errors.append(
                (
                    APIStatusError(
                        "PRIVATE_PROMPT_AND_SECRET",
                        response=response,
                        body={"secret": "PRIVATE_PROMPT_AND_SECRET"},
                    ),
                    kind,
                    status,
                )
            )
        with tempfile.TemporaryDirectory() as directory:
            for error, kind, status in errors:
                record = record_model_error(
                    Path(directory), error, usage_id=1, round_index=0, elapsed=2.1
                )
                self.assertEqual(record["kind"], kind)
                self.assertEqual(record["http_status"], status)
            content = (Path(directory) / "model-errors.jsonl").read_text(
                encoding="utf-8"
            )
            self.assertNotIn("PRIVATE_PROMPT_AND_SECRET", content)
            self.assertNotIn("deepseek.com", content)
            self.assertEqual(len(content.splitlines()), len(errors))

    def test_rotation_and_write_failure_cannot_mask_original_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "model-errors.jsonl").write_text("x" * 262144)
            record_model_error(
                root, TimeoutError(), usage_id=2, round_index=0, elapsed=1
            )
            self.assertTrue((root / "model-errors.previous.jsonl").exists())
            self.assertLess((root / "model-errors.jsonl").stat().st_size, 1000)
            result = record_model_error(
                root / "absent", TimeoutError(), usage_id=3, round_index=0, elapsed=1
            )
            self.assertEqual(result["kind"], "timeout")

    def test_unknown_exception_name_and_message_are_not_logged(self):
        error = type("PrivateSecret", (Exception,), {})("PRIVATE")
        details = model_error_details(error)
        self.assertEqual(details["exception_type"], "OtherError")
        self.assertNotIn("Private", str(details))


class ConversationErrorTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.ConversationTests.asyncSetUp
    asyncTearDown = fixtures.ConversationTests.asyncTearDown
    receive = fixtures.ConversationTests.receive

    async def test_failure_is_not_retried_or_dropped_and_next_turn_recovers(self):
        self.model.complete = AsyncMock(
            side_effect=[TimeoutError("PRIVATE_SECRET"), ModelResult("恢复了。", 12, 8)]
        )
        await self.receive(fixtures.event("合成消息", "failed"))
        await asyncio.wait_for(self.service.queue.join(), 2)
        self.assertEqual(self.model.complete.await_count, 1)
        self.assertIn("模型接口响应超时", self.channel.sent[-1])
        log = self.db.parent / "model-errors.jsonl"
        record = json.loads(log.read_text(encoding="utf-8"))
        self.assertEqual(record["phase"], "model_request")
        self.assertNotIn("合成消息", log.read_text(encoding="utf-8"))
        rows = list(self.service.store.db.execute("select status,reserved from usage"))
        self.assertEqual(rows[0][0], "unknown_cancelled_or_error")
        self.assertGreater(rows[0][1], 0)
        await self.receive(fixtures.event("下一条", "recovered"))
        await asyncio.wait_for(self.service.queue.join(), 2)
        self.assertEqual(self.model.complete.await_count, 2)
        self.assertEqual(self.channel.sent[-1], "恢复了。")
        self.assertIsNone(self.service.last_error)

    async def test_explicit_cancel_is_not_a_provider_failure(self):
        self.model.complete = AsyncMock(side_effect=asyncio.CancelledError())
        await self.receive(fixtures.event("合成取消", "cancelled"))
        await asyncio.wait_for(self.service.queue.join(), 2)
        self.assertFalse((self.db.parent / "model-errors.jsonl").exists())
        self.assertEqual(self.channel.sent, [])
