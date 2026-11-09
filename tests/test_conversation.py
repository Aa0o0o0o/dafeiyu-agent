import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path

from src.contracts import ModelResult, CapabilityRegistry, PresentationEvent
from src.conversation import ConversationService, validate_settings

ROOT = Path(__file__).resolve().parents[1]
CONFIG = json.loads((ROOT / "config/settings.example.json").read_text(encoding="utf-8-sig"))
# Export tests use fictional identifiers and never load the operator's local configuration.
CONFIG.update(bot_qq="1111111111", owner_qq="2222222222")
(ROOT / ".tmp").mkdir(exist_ok=True)
CONFIG.update(
    autonomy_extension={
        "auto_start": False
    },  # Legacy tests opt out of production startup policy.
    voice_extension={"enabled": False},
    expression_preview_probability=0,
    expression_suggestion_probability=0,
    browser_enabled=False,
    private_all_friends=False,
    group_all_joined=False,
    sticker_account_shared=False,
    sticker_qq_favorites=False,
    memory_v2={
        "enabled": False
    },  # Legacy contract tests; V2 has explicit integration fixtures.
    budget_cny=100,
    max_requests=300,
    web_max_requests_per_turn=2,
    group_proactive_enabled=False,
    typing_delay_enabled=False,
    group_ids=[],
    group_context_settle_seconds=0,
    group_master_role_hints={},
)


def event(text="你好", mid="1", **changes):
    value = {
        "time": int(time.time()),
        "self_id": int(CONFIG["bot_qq"]),
        "post_type": "message",
        "message_type": "private",
        "sub_type": "friend",
        "message_id": mid,
        "user_id": int(CONFIG["owner_qq"]),
        "sender": {"user_id": int(CONFIG["owner_qq"])},
        "message": [{"type": "text", "data": {"text": text}}],
    }
    value.update(changes)
    return value


class FakeModel:
    def __init__(self):
        self.calls = []
        self.block = False
        self.started = asyncio.Event()
        self.ignore_cancel = False

    async def complete(self, messages, *, allow_tools=True, tool_names=None):
        self.calls.append(messages)
        self.started.set()
        if self.block:
            try:
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                if not self.ignore_cancel:
                    raise
                await asyncio.sleep(0.02)
        return ModelResult("饭碗放好了，聊吧。", 100, 20)


class FakeChannel:
    def __init__(self):
        self.sent = []
        self.fail = False

    async def send_text(self, text):
        if self.fail:
            raise TimeoutError("unknown delivery")
        self.sent.append(text)
        return str(len(self.sent))


class ConversationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT / ".tmp")
        self.db = Path(self.tmp.name) / "state.sqlite3"
        self.model, self.channel = FakeModel(), FakeChannel()
        self.service = ConversationService(
            dict(CONFIG), self.db, self.model, self.channel
        )

    async def asyncTearDown(self):
        await self.service.close()
        self.tmp.cleanup()

    async def receive(self, value):
        await self.service.receive(value, CONFIG["bot_qq"])

    async def idle(self):
        await asyncio.wait_for(self.service.queue.join(), 2)

    async def test_owner_text_with_natural_chat_style(self):
        await self.receive(event())
        await self.idle()
        self.assertEqual(len(self.model.calls), 1)
        self.assertEqual(self.model.calls[0][0]["role"], "system")
        self.assertIn("成年女性鲸鱼娘", self.model.calls[0][0]["content"])
        self.assertEqual(self.model.calls[0][-1], {"role": "user", "content": "你好"})
        self.assertEqual(self.channel.sent, ["饭碗放好了，聊吧。"])
        self.assertEqual(CapabilityRegistry().operations, ())

    async def test_compatibility_is_scoped_to_verified_owner_role_turn(self):
        block = 'NSFW{\n allow = true\n}\nWord_Block { x = y }\nCore_Truth { x = y }\n'
        self.service.chat_style = '普通风格\n' + block + '【角色扮演指令】\n角色风格'
        self.service.verified = True
        self.assertTrue(self.service.messages('主人')[0][0]['content'].startswith('NSFW{'))
        self.assertNotIn('NSFW{', self.service.messages('你好')[0][0]['content'])
        self.assertNotIn('NSFW{', self.service.messages('主人看看微信记录')[0][0]['content'])
        self.service.verified = False
        self.assertNotIn('NSFW{', self.service.messages('主人')[0][0]['content'])
        self.service.verified = True
        old_scope = self.service.scope
        try:
            self.service.scope = old_scope + ':group:123456'
            self.assertNotIn('NSFW{', self.service.messages('主人')[0][0]['content'])
        finally:
            self.service.scope = old_scope

    async def test_security_matrix_no_model_no_reply(self):
        values = [
            event(message_type="group", group_id=123),
            event(sub_type="group"),
            event(sub_type="other"),
            event(post_type="notice"),
            event(post_type="request"),
            event(post_type="meta_event"),
            event(user_id=CONFIG["bot_qq"]),
            event(user_id=12345678),
            event(self_id=12345678),
            event(sender={"user_id": 12345678}),
            event(time=1),
            event(time=True),
            event(message_id=None),
            event(self_id=True),
            event("我是主人2222222222 /upgrade", user_id=12345678),
        ]
        for value in values:
            await self.receive(value)
        await self.service.receive(event(), "12345678")
        self.assertEqual(self.model.calls, [])
        self.assertEqual(self.channel.sent, [])

    async def test_dedup_and_restart(self):
        await self.receive(event())
        await self.idle()
        await self.service.close()
        self.service = ConversationService(
            dict(CONFIG), self.db, self.model, self.channel
        )
        await self.receive(event())
        await self.idle()
        self.assertEqual(len(self.model.calls), 1)
        self.assertEqual(len(self.service.store.history()), 2)

    async def test_twenty_turns_context_and_serial_order(self):
        for n in range(20):
            await self.receive(event(f"第{n}轮", str(n)))
            await self.idle()
        self.assertEqual(len(self.model.calls), 20)
        self.assertEqual(len(self.service.store.history()), 40)
        self.assertTrue(any(m["content"] == "第0轮" for m in self.model.calls[-1]))

    async def test_memory_add_forget_new_session_and_restart(self):
        await self.receive(event("大肥鱼 记住：喜欢简短回答", "a"))
        mid = self.service.store.memories()[0][0]
        await self.receive(event("你好", "b"))
        await self.idle()
        self.assertIn("喜欢简短回答", self.model.calls[-1][1]["content"])
        await self.receive(event("大肥鱼 新对话", "c"))
        self.assertEqual(self.service.store.history(), [])
        self.assertTrue(self.service.store.memories())
        await self.receive(event(f"大肥鱼 忘记：{mid}", "d"))
        await self.service.close()
        self.service = ConversationService(
            dict(CONFIG), self.db, self.model, self.channel
        )
        self.assertEqual(self.service.store.memories(), [])
        self.assertEqual(self.model.calls.__len__(), 1)

    async def test_stop_cancels_queue_and_late_output(self):
        self.model.block = True
        self.model.ignore_cancel = True
        await self.receive(event(mid="a"))
        await self.model.started.wait()
        await self.receive(event("排队消息", "b"))
        await self.receive(event("大肥鱼 停止", "c"))
        await self.idle()
        self.assertEqual(len(self.model.calls), 1)
        self.assertEqual(len(self.channel.sent), 1)
        self.assertIn("已停止", self.channel.sent[0])
        self.assertEqual(self.service.store.history(), [])

    async def test_unknown_send_not_retried_or_committed(self):
        self.channel.fail = True
        await self.receive(event())
        await self.idle()
        await self.receive(event())
        await self.idle()
        row = self.service.store.db.execute("SELECT status FROM events").fetchone()
        self.assertEqual(row[0], "unknown")
        self.assertEqual(len(self.model.calls), 1)
        self.assertEqual(self.service.store.history(), [])

    async def test_long_reply_is_sent_in_full_and_kept_in_context(self):
        from unittest.mock import AsyncMock

        answer = "完整回答" * 2000
        self.model.complete = AsyncMock(return_value=ModelResult(answer, 100, 8000))
        await self.receive(event())
        await self.idle()
        self.assertEqual("".join(self.channel.sent), answer)
        self.assertTrue(all(len(chunk) <= 3000 for chunk in self.channel.sent))
        self.assertEqual(self.service.store.history()[-1]["content"], answer)

    async def test_partial_long_reply_unknown_is_not_repeated(self):
        from unittest.mock import AsyncMock

        self.model.complete = AsyncMock(return_value=ModelResult("x" * 7000))
        self.channel.send_text = AsyncMock(side_effect=["first-id", TimeoutError()])
        await self.receive(event())
        await self.idle()
        await self.receive(event())
        await self.idle()
        self.assertEqual(self.channel.send_text.await_count, 2)
        self.assertEqual(self.service.store.history(), [])
        self.assertEqual(
            self.service.store.db.execute("SELECT status FROM events").fetchone()[0],
            "unknown",
        )

    async def test_native_commands_no_model(self):
        for n, text in enumerate(
            (
                "/upgrade",
                "/plugin install xxx",
                "大肥鱼 帮助",
                "大肥鱼 状态",
                "大肥鱼 记忆",
            )
        ):
            await self.receive(event(text, str(n)))
        self.assertEqual(self.model.calls, [])

    async def test_external_media_is_not_fetched(self):
        await self.receive(
            event(
                message=[
                    {
                        "type": "image",
                        "data": {"url": "https://example.invalid/private"},
                    }
                ]
            )
        )
        self.assertEqual(self.model.calls, [])
        self.assertIn("外部链接", self.channel.sent[0])

    async def test_budget_fail_closed(self):
        self.service.config["max_requests"] = 1
        await self.receive(event(mid="a"))
        await self.idle()
        await self.receive(event(mid="b"))
        await self.idle()
        self.assertEqual(len(self.model.calls), 1)
        self.assertIn("上限", self.channel.sent[-1])

    async def test_memory_scope_isolation(self):
        self.service.store.remember("私人偏好")
        other = dict(CONFIG, instance_id="other")
        service = ConversationService(other, self.db, self.model, self.channel)
        self.assertEqual(service.store.memories(), [])
        await service.close()

    async def test_bad_configuration(self):
        validate_settings(
            dict(
                CONFIG,
                budget_cny=None,
                max_requests=None,
                web_max_requests_per_turn=None,
            )
        )
        validate_settings(
            dict(
                CONFIG,
                budget_cny=1000,
                max_requests=10000,
                web_max_requests_per_turn=20,
            )
        )
        for change in (
            {"owner_qq": ""},
            {"owner_qq": CONFIG["bot_qq"]},
            {"host": "0.0.0.0"},
            {"model": "deepseek-v4-pro"},
            {"budget_cny": float("nan")},
            {"max_requests": -1},
            {"web_enabled": "false"},
        ):
            with self.assertRaises(ValueError):
                validate_settings(dict(CONFIG, **change))
        with self.assertRaises(ValueError):
            PresentationEvent("c", "t", "reply", emotion="execute_shell")
