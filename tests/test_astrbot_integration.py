"""Real localhost reverse-WS and pinned native-provider tests; no QQ or paid API."""
# ruff: noqa: E402

import asyncio
import json
import os
import socket
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ["ASTRBOT_ROOT"] = str(ROOT / ".tmp/astrbot-test")
sys.path.insert(0, str(ROOT / "vendor/AstrBot"))

import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus
from src.astrbot_runtime import AstrBotModel, build_adapter
from src.contracts import ModelResult
from tests.test_conversation import CONFIG, FakeModel, event

TOKEN = "offline-test-token-000000000000000000000000"


class AstrBotTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_tool_continuation_preserves_reasoning_without_exposing_it(
        self,
    ):
        seen = []

        def handler(request):
            body = json.loads(request.content)
            seen.append(body)
            message = {"role": "assistant", "content": "最终可见回复"}
            if len(seen) == 1:
                message = {
                    "role": "assistant",
                    "content": None,
                    "reasoning_content": "synthetic-private-thinking",
                    "tool_calls": [
                        {
                            "id": "web-1",
                            "type": "function",
                            "function": {
                                "name": "web_search",
                                "arguments": '{"query":"test"}',
                            },
                        }
                    ],
                }
            return httpx.Response(
                200,
                json={
                    "id": "synthetic",
                    "object": "chat.completion",
                    "created": 1,
                    "model": "deepseek-flash",
                    "choices": [
                        {
                            "index": 0,
                            "message": message,
                            "finish_reason": "tool_calls" if len(seen) == 1 else "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 12,
                        "completion_tokens": 8,
                        "total_tokens": 20,
                    },
                },
            )

        model = AstrBotModel(CONFIG, "offline")
        await model.provider.client.close()
        from openai import AsyncOpenAI

        model.provider.client = AsyncOpenAI(
            api_key="offline",
            base_url="https://api.deepseek.com",
            max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        try:
            first = await model.complete(
                [{"role": "user", "content": "test"}],
                tool_names=("web_search", "web_fetch", "read_member_profile"),
            )
            self.assertIn(
                "read_member_profile", [t["function"]["name"] for t in seen[0]["tools"]]
            )
            self.assertEqual(first.tool_calls[0].name, "web_search")
            self.assertEqual(first.text, "")
            second = await model.complete(
                [
                    {"role": "user", "content": "test"},
                    first.assistant_message,
                    {"role": "tool", "tool_call_id": "web-1", "content": '{"ok":true}'},
                ],
                allow_tools=False,
            )
            self.assertEqual(second.text, "最终可见回复")
            self.assertNotIn("tools", seen[-1])
            self.assertEqual(
                seen[-1]["messages"][1]["reasoning_content"],
                "synthetic-private-thinking",
            )
            self.assertEqual(len(seen), 2)
        finally:
            await model.close()

    async def test_provider_payment_error_is_not_retried(self):
        seen = []

        def handler(request):
            seen.append(request.url)
            return httpx.Response(
                402,
                json={
                    "error": {
                        "message": "Insufficient Balance",
                        "type": "balance_error",
                        "code": "402",
                    }
                },
            )

        model = AstrBotModel(CONFIG, "offline")
        await model.provider.client.close()
        from openai import AsyncOpenAI, APIStatusError

        model.provider.client = AsyncOpenAI(
            api_key="offline",
            base_url="https://api.deepseek.com",
            max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        try:
            with self.assertRaises(APIStatusError) as error:
                await model.complete([{"role": "user", "content": "test"}])
            self.assertEqual(error.exception.status_code, 402)
            self.assertEqual(len(seen), 1)
        finally:
            await model.close()

    async def test_provider_wire_quality_thinking_vision_read_only_tools_no_retry(self):
        seen = []

        def handler(request):
            seen.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "id": "test",
                    "object": "chat.completion",
                    "created": 1,
                    "model": "deepseek-flash",
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": "可见文字",
                                "reasoning_content": "不可发送的隐藏内容",
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 5,
                        "total_tokens": 15,
                    },
                },
            )

        model = AstrBotModel(CONFIG, "offline-not-a-real-key")
        await model.provider.client.close()
        from openai import AsyncOpenAI

        model.provider.client = AsyncOpenAI(
            api_key="offline",
            base_url="https://api.deepseek.com",
            max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        try:
            response = await model.complete(
                [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "这是什么？"},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": "data:image/png;base64,c3ludGhldGlj"
                                },
                            },
                        ],
                    }
                ]
            )
            self.assertEqual(response.text, "可见文字")
            self.assertEqual(seen[0]["thinking"], {"type": "enabled"})
            self.assertEqual(seen[0]["reasoning_effort"], "high")
            self.assertEqual(
                {x["function"]["name"] for x in seen[0]["tools"]},
                {"web_search", "web_fetch"},
            )
            self.assertNotIn("reasoning", seen[0])
            self.assertEqual(seen[0]["max_tokens"], 65536)
            self.assertEqual(seen[0]["model"], "deepseek-flash")
            self.assertEqual(seen[0]["messages"][0]["content"][1]["type"], "image_url")
            self.assertFalse(any(m["role"] == "system" for m in seen[0]["messages"]))
            self.assertEqual(response.response_model, "deepseek-flash")
            self.assertEqual(len(seen), 1)
        finally:
            await model.close()

    async def test_reverse_ws_auth_identity_fixed_recipient(self):
        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as directory:
            root = Path(directory)
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            model = FakeModel()
            original_complete = model.complete

            async def with_mention(messages, **kwargs):
                await original_complete(messages, **kwargs)
                return ModelResult("@12345678 好呀。", 100, 20)

            model.complete = with_mention
            adapter = build_adapter(
                dict(CONFIG, port=port, group_ids=["3333333333"], poke_enabled=True),
                TOKEN,
                model,
                root,
            )
            server = asyncio.create_task(adapter.run())
            try:
                for _ in range(100):
                    try:
                        reader, writer = await asyncio.open_connection(
                            "127.0.0.1", port
                        )
                        writer.close()
                        await writer.wait_closed()
                        break
                    except OSError:
                        await asyncio.sleep(0.02)
                headers = {
                    "Authorization": "Bearer " + TOKEN,
                    "X-Self-ID": CONFIG["bot_qq"],
                    "X-Client-Role": "Universal",
                }
                url = f"ws://127.0.0.1:{port}/ws"
                for bad_headers in (
                    dict(headers, Authorization="Bearer wrong"),
                    dict(headers, **{"X-Self-ID": "12345678"}),
                    dict(headers, **{"X-Client-Role": "Event"}),
                ):
                    with self.assertRaises(InvalidStatus):
                        async with connect(url, additional_headers=bad_headers):
                            pass
                async with httpx.AsyncClient() as client:
                    response = await client.post(
                        f"http://127.0.0.1:{port}/", json=event()
                    )
                    self.assertEqual(response.status_code, 405)
                sent = []
                group_sent = []
                actions = []
                wrong_login = True
                async with connect(url, additional_headers=headers) as ws:

                    async def protocol_peer():
                        async for raw in ws:
                            request = json.loads(raw)
                            actions.append(request["action"])
                            if request["action"] == "get_login_info":
                                data = {
                                    "user_id": "12345678"
                                    if wrong_login
                                    else CONFIG["bot_qq"],
                                    "nickname": "fake",
                                }
                            elif request["action"] == "send_private_msg":
                                sent.append(request["params"])
                                data = {"message_id": len(sent)}
                            elif request["action"] == "send_group_msg":
                                group_sent.append(request["params"])
                                data = {"message_id": 100 + len(group_sent)}
                            elif request["action"] == "get_msg":
                                data = {
                                    "message_id": -17,
                                    "message_type": "group",
                                    "group_id": 3333333333,
                                    "sender": {"user_id": 12345678},
                                    "message": [
                                        {
                                            "type": "text",
                                            "data": {"text": "引用原文需要解释"},
                                        }
                                    ],
                                }
                            else:
                                raise AssertionError("Unexpected OneBot action")
                            await ws.send(
                                json.dumps(
                                    {
                                        "status": "ok",
                                        "retcode": 0,
                                        "data": data,
                                        "echo": request["echo"],
                                    }
                                )
                            )

                    peer = asyncio.create_task(protocol_peer())
                    await ws.send(json.dumps(event(mid="wrong-account")))
                    await asyncio.sleep(0.15)
                    self.assertEqual(model.calls, [])
                    wrong_login = False
                    for bad_event in (
                        event(mid="other", user_id=12345678),
                        event(mid="group", message_type="group"),
                        event(mid="self", user_id=CONFIG["bot_qq"]),
                    ):
                        await ws.send(json.dumps(bad_event))
                    await ws.send(json.dumps(event(mid="accepted")))
                    for _ in range(100):
                        if sent:
                            break
                        await asyncio.sleep(0.02)
                    self.assertEqual(len(model.calls), 1)
                    self.assertEqual(len(sent), 1)
                    self.assertEqual(sent[0]["user_id"], int(CONFIG["owner_qq"]))
                    self.assertEqual(sent[0]["self_id"], CONFIG["bot_qq"])
                    self.assertEqual(sent[0]["message"][0]["type"], "text")
                    await ws.send(json.dumps(event(mid="accepted")))
                    await asyncio.sleep(0.1)
                    self.assertEqual(len(model.calls), 1)
                    from tests.test_group_chat import group_event

                    await ws.send(
                        json.dumps(
                            group_event(
                                "群话题：下周答辩", mid="passive", mentioned=False
                            )
                        )
                    )
                    await ws.send(
                        json.dumps(
                            group_event("越界群", mid="not-allowed", group="5555555999")
                        )
                    )
                    await asyncio.sleep(0.05)
                    self.assertEqual(len(model.calls), 1)
                    referenced = group_event("你怎么看", mid="group-accepted")
                    referenced["message"].insert(
                        0, {"type": "reply", "data": {"id": -17}}
                    )
                    await ws.send(json.dumps(referenced))
                    for _ in range(100):
                        if group_sent:
                            break
                        await asyncio.sleep(0.02)
                    self.assertEqual(len(group_sent), 1)
                    self.assertEqual(group_sent[0]["group_id"], 3333333333)
                    self.assertEqual(group_sent[0]["self_id"], CONFIG["bot_qq"])
                    self.assertEqual(len(sent), 1)
                    self.assertEqual(
                        group_sent[0]["message"][0],
                        {"type": "at", "data": {"qq": "12345678"}},
                    )
                    self.assertIn("引用原文需要解释", str(model.calls[1]))
                    self.assertIn(
                        "下周答辩", json.dumps(model.calls[1], ensure_ascii=False)
                    )
                    await ws.send(
                        json.dumps(group_event("你怎么看", mid="group-accepted"))
                    )
                    await asyncio.sleep(0.05)
                    self.assertEqual(len(model.calls), 2)
                    poke = {
                        "post_type": "notice",
                        "notice_type": "notify",
                        "sub_type": "poke",
                        "self_id": CONFIG["bot_qq"],
                        "user_id": 12345678,
                        "target_id": CONFIG["bot_qq"],
                        "group_id": 3333333333,
                        "time": time.time(),
                    }
                    await ws.send(json.dumps(poke))
                    for _ in range(100):
                        if len(group_sent) >= 2:
                            break
                        await asyncio.sleep(0.02)
                    self.assertEqual(len(model.calls), 3)
                    self.assertIn("戳", str(model.calls[2]))
                    await ws.send(json.dumps(poke))
                    await asyncio.sleep(0.05)
                    self.assertEqual(len(model.calls), 3)
                    self.assertTrue(
                        set(actions)
                        <= {
                            "get_login_info",
                            "send_private_msg",
                            "send_group_msg",
                            "get_msg",
                        }
                    )
                    peer.cancel()
                    await asyncio.gather(peer, return_exceptions=True)
            finally:
                await adapter.terminate()
                await asyncio.wait_for(server, 5)

    async def test_sticker_wire_uses_only_a_verified_library_file_and_bound_recipient(
        self,
    ):
        from unittest.mock import AsyncMock
        from tests.test_vision import png

        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as directory:
            root = Path(directory)
            adapter = build_adapter(
                dict(CONFIG, group_ids=["3333333333"], sticker_enabled=True),
                TOKEN,
                FakeModel(),
                root,
            )
            adapter.checked_login = AsyncMock(return_value=True)
            adapter.bot.call_action = AsyncMock(return_value={"message_id": 123})
            try:
                token = adapter.stickers.add_bytes("3333333333", png(), "开心")
                await adapter.send_sticker(token, group_id="3333333333")
                call = adapter.bot.call_action.await_args
                self.assertEqual(call.args, ("send_group_msg",))
                self.assertEqual(call.kwargs["group_id"], 3333333333)
                image = call.kwargs["message"][0]
                self.assertEqual(image["type"], "image")
                self.assertEqual(image["data"]["sub_type"], 1)
                self.assertTrue(
                    image["data"]["file"].startswith(
                        (root / "runtime/stickers").as_uri() + "/"
                    )
                )
                with self.assertRaises(ConnectionError):
                    await adapter.send_sticker(token, group_id="5555555999")
                self.assertEqual(adapter.bot.call_action.await_count, 1)
                await adapter.send_sticker(token)
                self.assertEqual(
                    adapter.bot.call_action.await_args.kwargs["user_id"],
                    int(CONFIG["owner_qq"]),
                )
            finally:
                await adapter.terminate()
