"""Browser policy and conversation integration use no real account or paid model."""

import asyncio
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from src.browser.policy import PublicPolicy, BrowserError, validate_command
from src.browser.broker import BrowserBroker
from src.conversation import ConversationService
from src.contracts import ModelResult, WebCall
from tests.test_conversation import CONFIG, ROOT, FakeModel, FakeChannel, event


class PolicyTests(unittest.IsolatedAsyncioTestCase):
    def test_public_url_rejects_private_file_credentials_and_nonweb_schemes(self):
        policy = PublicPolicy()
        for url in [
            "http://127.0.0.1",
            "http://10.0.0.1",
            "http://169.254.169.254/latest",
            "http://192.168.1.1",
            "http://100.64.0.1",
            "http://[::1]",
            "http://2130706433",
            "http://localhost",
            "file:///C:/synthetic/project/config/local.json",
            "javascript:alert(1)",
            "data:text/html,hello",
            "https://user:password@example.com",
            "https://example.com:6200",
            "https://example.com?access_token=secret",
        ]:
            with self.subTest(url=url), self.assertRaises(BrowserError):
                policy.url(url)
        self.assertEqual(
            policy.url("https://github.com/microsoft/playwright"),
            "https://github.com/microsoft/playwright",
        )

    async def test_dns_with_any_private_answer_is_rejected(self):
        mixed = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
        ]
        loop = asyncio.get_running_loop()
        with (
            patch.object(loop, "getaddrinfo", AsyncMock(return_value=mixed)),
            self.assertRaises(BrowserError),
        ):
            await PublicPolicy().resolve("example.com", 443)

    async def test_resolver_returns_pinned_public_ip_and_test_port_is_exact(self):
        loop = asyncio.get_running_loop()
        with patch.object(
            loop,
            "getaddrinfo",
            AsyncMock(
                return_value=[
                    (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
                ]
            ),
        ):
            self.assertEqual(
                await PublicPolicy().resolve("example.com", 443), "93.184.216.34"
            )
        test = PublicPolicy([15678])
        self.assertEqual(await test.resolve("127.0.0.1", 15678), "127.0.0.1")
        with self.assertRaises(BrowserError):
            test.url("http://127.0.0.1:6200")

    def test_no_model_js_paths_scope_override_or_secret_input(self):
        for cmd in [
            {"action": "evaluate", "code": "fetch(...)"},
            {"action": "open", "url": "https://example.com", "scope": "other"},
            {"action": "type", "ref": "r1", "text": "password=private"},
            {"action": "wait", "seconds": float("nan")},
            {"action": "press", "key": "Control+L"},
            {"action": "video_seek", "time": -1},
        ]:
            with self.subTest(command=cmd), self.assertRaises(BrowserError):
                validate_command(cmd)

    def test_actor_key_and_artifact_lookup_cannot_cross_scopes(self):
        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as folder:
            broker = BrowserBroker(ROOT, worker_root=folder)
            self.assertNotEqual(
                broker.key("scope1", "12345001"), broker.key("scope2", "12345001")
            )
            self.assertNotEqual(
                broker.key("scope1", "12345001"), broker.key("scope1", "12345002")
            )
            with self.assertRaises(BrowserError):
                broker.artifact("scope1", "12345002", "a-private")


class BrowserConversationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT / ".tmp")
        self.model = FakeModel()
        self.channel = FakeChannel()
        self.service = ConversationService(
            dict(CONFIG, browser_enabled=True),
            Path(self.tmp.name) / "state.sqlite3",
            self.model,
            self.channel,
        )
        self.broker = AsyncMock()
        self.service.browser_tools = self.broker

    async def asyncTearDown(self):
        await self.service.close()
        self.tmp.cleanup()

    async def test_model_tool_uses_actual_scope_actor_and_receives_real_images(self):
        self.broker.execute.return_value = {
            "ok": True,
            "images": [
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/jpeg;base64,c3ludGhldGlj"},
                }
            ],
            "title": "合成视频",
        }
        calls = []

        async def complete(messages, **kwargs):
            calls.append((list(messages), kwargs))
            if len(calls) == 1:
                call = WebCall(
                    "browser-call", "browser", '{"action":"video_observe","seconds":1}'
                )
                return ModelResult(
                    "",
                    20,
                    10,
                    "deepseek-flash",
                    (call,),
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": call.id,
                                "type": "function",
                                "function": {
                                    "name": call.name,
                                    "arguments": call.arguments,
                                },
                            }
                        ],
                    },
                )
            return ModelResult("画面里有合成内容。", 20, 10)

        self.model.complete = complete
        await self.service.receive(
            event("看看这个视频", "browser-test"), CONFIG["bot_qq"]
        )
        await asyncio.wait_for(self.service.queue.join(), 2)
        self.assertIn("browser", calls[0][1]["tool_names"])
        self.assertEqual(self.broker.execute.await_args.args[0], self.service.scope)
        self.assertEqual(self.broker.execute.await_args.args[1], CONFIG["owner_qq"])
        self.assertIn("image_url", str(calls[1][0]))
        self.assertEqual(self.channel.sent, ["画面里有合成内容。"])
        self.assertEqual(
            self.service.store.usage()[0], 2
        )  # Same ledger; no fake paid browser charge.

    async def test_identical_browser_reads_are_not_cached_across_page_changes(self):
        self.broker.execute.side_effect = [
            {"ok": True, "text": "first"},
            {"ok": True, "text": "second"},
        ]
        a, _ = await self.service.execute_browser('{"action":"snapshot"}')
        b, _ = await self.service.execute_browser('{"action":"snapshot"}')
        self.assertNotEqual(a, b)
        self.assertEqual(self.broker.execute.await_count, 2)

    async def test_browser_management_command_needs_no_model_call(self):
        self.broker.execute.return_value = {"ok": True, "open_tabs": 0}
        await self.service.receive(
            event("大肥鱼 浏览器状态", "state"), CONFIG["bot_qq"]
        )
        self.assertEqual(self.model.calls, [])
        self.assertEqual(self.broker.execute.await_args.args[1], CONFIG["owner_qq"])
