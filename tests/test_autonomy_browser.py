"""Autonomous tool orchestration, no external model/QQ or real private data."""

import asyncio
import json
import unittest
from unittest.mock import AsyncMock

from src.contracts import ModelResult
from tests import test_autonomy as legacy
from tests import test_conversation as fixtures


def result(value):
    return ModelResult(json.dumps(value, ensure_ascii=False), 10, 20, "fake-flash")


def finish():
    return result(
        {
            "reflection": "实际查到了候选公开资料；仍有疑问。",
            "next_question": "继续核对公开来源",
            "decision": "continue",
        }
    )


class AutonomyBrowserTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.ConversationTests.asyncSetUp
    asyncTearDown = fixtures.ConversationTests.asyncTearDown
    prepare = legacy.AutonomyTests.prepare

    def browser(self):
        a = self.prepare()
        self.service.config = dict(
            self.service.config, autonomy_extension={"browser": True, "web_fetch": True}
        )
        self.service.browser_tools = type(
            "Browser",
            (),
            {
                "execute": AsyncMock(
                    return_value={
                        "ok": True,
                        "frames": [
                            {
                                "url": "https://example.com",
                                "text": "Synthetic public science",
                                "elements": [{"ref": "r1", "label": "link"}],
                            }
                        ],
                    }
                )
            },
        )()
        return a

    async def test_search_then_browser_video_frames_three_model_calls_no_qq(self):
        a = self.browser()
        image = {
            "type": "image_url",
            "image_url": {"url": "data:image/jpeg;base64,synthetic-frame-only"},
        }

        async def execute(scope, actor, command):
            if command["action"] == "video_observe":
                return {
                    "ok": True,
                    "images": [image],
                    "coverage": "sampled_frames_only",
                    "observations": [{"time": 1.5}],
                }
            return {
                "ok": True,
                "url": "https://example.com/video",
                "videos": [{"index": 0}],
            }

        self.service.browser_tools.execute.side_effect = execute
        self.service.model.complete.side_effect = [
            result({"action": "search", "query": "公开科普"}),
            result(
                {
                    "action": "browser",
                    "commands": [
                        {"action": "open", "url": "https://example.com/video"},
                        {"action": "video_list"},
                        {"action": "video_play", "video": 0},
                        {"action": "video_observe", "video": 0, "seconds": 1},
                    ],
                }
            ),
            finish(),
        ]
        self.assertTrue(await a.run(True))
        self.assertEqual(self.service.model.complete.await_count, 3)
        calls = self.service.browser_tools.execute.await_args_list
        self.assertTrue(
            all(
                c.args[0].startswith(a.scope + ":autonomy:")
                and c.args[1] == self.service.config["bot_qq"]
                for c in calls
            )
        )
        self.assertEqual(calls[-1].args[2]["action"], "close_session")
        receipt = a.context()["experiences"][0][2]
        self.assertIn("sampled_frames_only", receipt)
        self.assertNotIn("base64", receipt)
        messages = self.service.model.complete.await_args_list[-1].args[0]
        self.assertIn(image, messages[1]["content"])
        self.assertEqual(self.channel.sent, [])

    async def test_fetch_uses_real_fetch_receipt(self):
        a = self.browser()
        self.service.web_tools.execute.return_value = json.dumps(
            {"ok": True, "url": "https://example.com", "content": "Synthetic article"}
        )
        self.service.model.complete.side_effect = [
            result({"action": "fetch", "url": "https://example.com"}),
            finish(),
        ]
        self.assertTrue(await a.run(True))
        self.assertEqual(self.service.web_tools.execute.await_args.args[0], "web_fetch")
        self.assertIn("Synthetic article", a.context()["experiences"][0][2])
        self.assertEqual(self.channel.sent, [])

    async def test_public_navigation_ref_followup_and_bookmark(self):
        a = self.browser()
        self.service.model.complete.side_effect = [
            result(
                {
                    "action": "browser",
                    "commands": [{"action": "open", "url": "https://example.com"}],
                }
            ),
            result(
                {
                    "action": "browser",
                    "commands": [
                        {"action": "click", "ref": "r1"},
                        {"action": "bookmark_add"},
                    ],
                }
            ),
            finish(),
        ]
        self.assertTrue(await a.run(True))
        commands = [
            c.args[2] for c in self.service.browser_tools.execute.await_args_list
        ]
        self.assertEqual(
            [c["action"] for c in commands],
            ["open", "click", "bookmark_add", "close_session"],
        )
        self.assertEqual(commands[1]["ref"], "r1")

    async def test_no_share_private_urls_shell_or_unregistered_tools(self):
        for command in [
            {"action": "open", "url": "https://example.com", "share": True},
            {"action": "share", "artifact_id": "a1"},
            {"action": "open", "url": "http://127.0.0.1"},
            {"action": "shell", "text": "whoami"},
        ]:
            with self.subTest(command=command):
                a = self.browser()
                self.service.model.complete.side_effect = [
                    result({"action": "browser", "commands": [command]})
                ]
                self.assertFalse(await a.run(True))
                self.service.browser_tools.execute.assert_not_awaited()
                self.assertEqual(a.context()["experiences"][0][0], "failed")
        self.assertEqual(self.channel.sent, [])

    async def test_disabled_browser_never_executes(self):
        a = self.browser()
        self.service.config["autonomy_extension"]["browser"] = False
        self.service.model.complete.side_effect = [
            result({"action": "browser", "commands": [{"action": "tabs"}]})
        ]
        self.assertFalse(await a.run(True))
        self.service.browser_tools.execute.assert_not_awaited()
        self.assertNotIn("browser", a.permissions()["tools"])

    async def test_partial_receipt_survives_tool_failure_and_closes(self):
        a = self.browser()
        self.service.model.complete.side_effect = [
            result(
                {
                    "action": "browser",
                    "commands": [
                        {"action": "open", "url": "https://example.com"},
                        {"action": "video_list"},
                    ],
                }
            )
        ]
        self.service.browser_tools.execute.side_effect = [
            {"ok": True, "title": "actual-first-step"},
            TimeoutError(),
            {"ok": True},
        ]
        self.assertFalse(await a.run(True))
        row = a.context()["experiences"][0]
        self.assertEqual(row[0], "failed")
        self.assertIn("actual-first-step", row[2])
        self.assertEqual(
            self.service.browser_tools.execute.await_args.args[2]["action"],
            "close_session",
        )
        self.assertEqual(self.service.model.complete.await_count, 1)

    async def test_clear_during_browser_cannot_write_back_and_closes(self):
        a = self.browser()
        entered, release = asyncio.Event(), asyncio.Event()

        async def execute(scope, actor, command):
            if command["action"] != "close_session":
                entered.set()
                await release.wait()
            return {"ok": True}

        self.service.browser_tools.execute.side_effect = execute
        self.service.model.complete.side_effect = [
            result(
                {
                    "action": "browser",
                    "commands": [{"action": "open", "url": "https://example.com"}],
                }
            )
        ]
        task = asyncio.create_task(a.run(True))
        await entered.wait()
        a.clear()
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(a.context()["experiences"], [])
        self.assertEqual(
            self.service.browser_tools.execute.await_args.args[2]["action"],
            "close_session",
        )

    async def test_chat_preempts_browser_and_stops_playback(self):
        a = self.browser()
        entered = asyncio.Event()

        async def execute(scope, actor, command):
            if command["action"] != "close_session":
                entered.set()
                await asyncio.sleep(30)
            return {"ok": True}

        self.service.browser_tools.execute.side_effect = execute
        self.service.model.complete.side_effect = [
            result(
                {
                    "action": "browser",
                    "commands": [{"action": "video_play", "video": 0}],
                }
            )
        ]
        self.assertTrue(a.schedule(True))
        await entered.wait()
        await self.service.receive(
            fixtures.event("大肥鱼 自主状态"), fixtures.CONFIG["bot_qq"]
        )
        self.assertTrue(a.task.done())
        self.assertEqual(a.context()["experiences"][0][0], "cancelled")
        self.assertEqual(
            self.service.browser_tools.execute.await_args.args[2]["action"],
            "close_session",
        )

    async def test_eight_tool_actions_and_three_model_calls_bound(self):
        a = self.browser()
        self.service.model.complete.side_effect = [
            result({"action": "browser", "commands": [{"action": "tabs"}] * 9})
        ]
        self.assertFalse(await a.run(True))
        self.service.browser_tools.execute.assert_not_awaited()
        self.service.model.complete.side_effect = [
            result({"action": "browser", "commands": [{"action": "tabs"}]}),
            result({"action": "browser", "commands": [{"action": "tabs"}]}),
            result({"action": "browser", "commands": [{"action": "tabs"}]}),
        ]
        self.service.model.complete.reset_mock()
        self.assertFalse(await a.run(True))
        self.assertEqual(self.service.model.complete.await_count, 3)
        calls = self.service.browser_tools.execute.await_args_list
        self.assertEqual(sum(c.args[2]["action"] == "tabs" for c in calls), 2)

    async def test_false_tool_receipt_not_saved_as_success(self):
        a = self.browser()
        self.service.model.complete.side_effect = [
            result({"action": "browser", "commands": [{"action": "tabs"}]})
        ]
        self.service.browser_tools.execute.return_value = {
            "ok": False,
            "error": "Synthetic failure",
        }
        self.assertFalse(await a.run(True))
        self.assertEqual(a.context()["experiences"][0][0], "failed")
        self.assertEqual(a.context()["experiences"][0][2], "")

    async def test_long_public_article_not_rejected_by_memory_admission_length(self):
        from src.autonomy_tools import AutonomousTools

        article = "Synthetic public science facts. " * 300
        obs = AutonomousTools.observation({"ok": True, "content": article})
        self.assertNotIn("redacted", obs)
        self.assertIn("Synthetic public science facts", obs["excerpt"])
        raw = json.dumps(
            {
                "actions": [
                    {"tool": "browser", "request": {"action": "read"}, "result": obs},
                    {
                        "tool": "browser",
                        "request": {"action": "video_observe"},
                        "result": {"excerpt": "sampled_frames"},
                    },
                ]
            }
        )
        ctx = self.service.autonomy.receipt_context(raw)
        self.assertIn("video_observe", ctx)
        self.assertIn("context_excerpt", ctx)
        self.assertIn("sampled_frames", ctx)

    async def test_real_chromium_synthetic_autonomous_video_without_qq(self):
        import threading
        from http.server import ThreadingHTTPServer
        from pathlib import Path
        from unittest.mock import patch
        from src.browser.broker import BrowserBroker
        from src.browser.policy import PublicPolicy
        from tests.test_browser_engine import Handler
        from tests.test_conversation import ROOT

        if not (ROOT / "tools/browser/python/Scripts/python.exe").exists():
            self.skipTest("Project browser runtime unavailable")
        a = self.browser()
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        broker = BrowserBroker(
            ROOT,
            test_ports=[server.server_port],
            worker_root=Path(self.tmp.name) / "browser",
        )
        self.service.browser_tools = broker
        self.service.model.complete.side_effect = [
            result(
                {
                    "action": "browser",
                    "commands": [
                        {
                            "action": "open",
                            "url": f"http://127.0.0.1:{server.server_port}/",
                        }
                    ],
                }
            ),
            result(
                {
                    "action": "browser",
                    "commands": [
                        {"action": "wait", "seconds": 3},
                        {"action": "video_list"},
                        {"action": "video_play", "video": 0},
                        {"action": "video_observe", "video": 0, "seconds": 1},
                    ],
                }
            ),
            finish(),
        ]
        try:
            with patch(
                "src.autonomy_tools.PublicPolicy",
                return_value=PublicPolicy(test_ports=[server.server_port]),
            ):
                self.assertTrue(await a.run(True))
            review = self.service.model.complete.await_args.args[0][1]["content"]
            self.assertTrue(any(item["type"] == "image_url" for item in review))
            record = a.db.execute(
                "SELECT receipt FROM autonomy_runs WHERE scope=? ORDER BY created DESC LIMIT 1",
                (a.scope,),
            ).fetchone()[0]
            self.assertIn("sampled_visible_frames", record)
            self.assertNotIn("base64", record)
            self.assertEqual(self.channel.sent, [])
        finally:
            await broker.close()
            await asyncio.to_thread(server.shutdown)
            server.server_close()
            thread.join()
