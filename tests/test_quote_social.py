"""Quotes, poke, member data, mentions and bounded tools; synthetic only."""

import asyncio
import json
import time
import unittest
from unittest.mock import AsyncMock

import httpx

from src.contracts import ModelResult
from src.qq_profiles import MemberProfiles, ProfileError
from src.reply_context import parse_quoted, QuoteError
from src.social_events import PokeGate, mention_segments
from src.vision import parse_message, image_data_url
from src.storage import Store
from tests import test_conversation as fixtures
from tests import test_group_chat as groupfixtures
from tests.test_chat_web import tool_result
from tests.test_vision import URL, png

OWNER = fixtures.CONFIG["owner_qq"]
BOT = fixtures.CONFIG["bot_qq"]
GROUP, group_event = groupfixtures.GROUP, groupfixtures.group_event


def quoted(
    *, kind="group", author=OWNER, group=GROUP, text="quoted-unique-text", images=False
):
    body = [{"type": "text", "data": {"text": text}}]
    if images:
        body.append({"type": "image", "data": {"url": URL}})
    return {
        "message_id": -17,
        "message_type": kind,
        "group_id": group if kind == "group" else None,
        "sender": {"user_id": author},
        "user_id": author,
        "message": body,
    }


class QuoteParsingTests(unittest.TestCase):
    def read(self, value, **kwargs):
        return parse_quoted(value, "-17", bot_qq=BOT, owner_qq=OWNER, **kwargs)

    def test_one_hop_quote_keeps_author_and_pixels(self):
        raw = quoted(images=True)
        raw["message"].insert(0, {"type": "reply", "data": {"id": "-99"}})
        result = self.read(raw, group_id=GROUP)
        self.assertEqual(result["author_qq"], OWNER)
        self.assertEqual(result["images"], (URL,))
        self.assertEqual(result["text"], "quoted-unique-text")
        parsed = parse_message([{"type": "reply", "data": {"id": -17}}])
        self.assertEqual(parsed.reply_id, "-17")

    def test_foreign_group_and_private_sources_are_rejected(self):
        for raw in (quoted(group="5555555999"), quoted(kind="private")):
            with self.assertRaises(QuoteError):
                self.read(raw, group_id=GROUP)
        with self.assertRaises(QuoteError):
            self.read(quoted(kind="private", author="12345678"))

    def test_private_bot_quote_requires_destination_proof(self):
        raw = quoted(kind="private", author=BOT)
        with self.assertRaises(QuoteError):
            self.read(raw)
        self.assertEqual(self.read(raw, known_private_route=True)["author_qq"], BOT)
        self.assertEqual(self.read(quoted(kind="private"))["author_qq"], OWNER)

    def test_id_mismatch_rejected_before_reading_text(self):
        raw = quoted()
        raw["message_id"] = 18
        with self.assertRaises(QuoteError):
            self.read(raw, group_id=GROUP)

    def test_mention_targets_are_real_segments_only_when_allowed(self):
        parts = mention_segments(
            "@12345678 好呀 @23456789 @34567890 @all [CQ:at,qq=all]",
            {"12345678", "23456789", "34567890"},
        )
        self.assertEqual(
            [p["data"]["qq"] for p in parts if p["type"] == "at"],
            ["12345678", "23456789"],
        )
        self.assertTrue(all(p["type"] in {"at", "text"} for p in parts))
        self.assertEqual(
            mention_segments("@99999999 好", set()),
            [{"type": "text", "data": {"text": "@99999999 好"}}],
        )

    def test_poke_scope_target_exact_dedup_and_self_loop(self):
        gate = PokeGate(dict(fixtures.CONFIG, poke_enabled=True, group_ids=[GROUP]))
        base = {
            "post_type": "notice",
            "notice_type": "notify",
            "sub_type": "poke",
            "self_id": BOT,
            "user_id": OWNER,
            "target_id": BOT,
            "time": time.time(),
        }
        now = base["time"]
        private_gate = PokeGate(dict(fixtures.CONFIG, poke_enabled=True))
        self.assertEqual(
            private_gate.convert(dict(base, user_id=BOT, sender_id=OWNER), now)[
                "user_id"
            ],
            OWNER,
        )
        self.assertIsNotNone(gate.convert(base, now))
        self.assertIsNone(gate.convert(base, now + 1))
        self.assertIsNotNone(gate.convert(dict(base, time=now + 31), now + 31))
        for changed in (
            dict(base, user_id="12345678"),
            dict(base, target_id=OWNER),
            dict(base, user_id=BOT),
            dict(base, group_id="5555555999"),
            dict(base, time=True),
        ):
            self.assertIsNone(gate.convert(changed, now + 100))
        converted = gate.convert(
            dict(base, group_id=GROUP, user_id="12345678", time=now + 100), now + 100
        )
        self.assertEqual(converted["group_id"], GROUP)
        self.assertEqual(converted["message"][0]["type"], "at")


class VoiceQuoteTests(unittest.IsolatedAsyncioTestCase):
    async def test_private_and_group_voice_require_original_scope_before_transcription(self):
        from src.reply_context import resolve_quoted
        transcribe = AsyncMock(return_value={"text": "合成语音测试"})
        raw = quoted(kind="private")
        raw["message"] = [{"type": "record", "data": {"file": "unused"}}]
        result = await resolve_quoted(raw, "-17", bot_qq=BOT, owner_qq=OWNER,
                                      transcribe=transcribe)
        self.assertIn("合成语音测试", result["text"])
        self.assertIn("may_be_inaccurate", result["text_source"])
        transcribe.assert_awaited_once_with("-17")
        transcribe.reset_mock()
        raw["sender"] = {"user_id": "12345678"}
        with self.assertRaises(QuoteError):
            await resolve_quoted(raw, "-17", bot_qq=BOT, owner_qq=OWNER,
                                 transcribe=transcribe)
        transcribe.assert_not_awaited()
        raw = quoted(group="5555555999")
        raw["message"] = [{"type": "record", "data": {"file": "unused"}}]
        with self.assertRaises(QuoteError):
            await resolve_quoted(raw, "-17", bot_qq=BOT, owner_qq=OWNER,
                                 group_id=GROUP, transcribe=transcribe)
        transcribe.assert_not_awaited()
        raw["group_id"] = GROUP
        result = await resolve_quoted(raw, "-17", bot_qq=BOT, owner_qq=OWNER,
                                      group_id=GROUP, transcribe=transcribe)
        self.assertIn("合成语音测试", result["text"])

    async def test_bot_voice_uses_exact_delivered_text_without_asr(self):
        from src.reply_context import resolve_quoted
        raw = quoted(kind="private", author=BOT)
        raw["message"] = [{"type": "record", "data": {"file": "unused"}}]
        transcribe = AsyncMock()
        result = await resolve_quoted(raw, "-17", bot_qq=BOT, owner_qq=OWNER,
                                      known_private_route=True,
                                      known_bot_voice_text="合成朗读内容",
                                      transcribe=transcribe)
        self.assertEqual(result["text"], "合成朗读内容")
        self.assertIn("not_audio_transcription", result["text_source"])
        transcribe.assert_not_awaited()
        with self.assertRaises(QuoteError):
            await resolve_quoted(raw, "-17", bot_qq=BOT, owner_qq=OWNER,
                                 known_bot_voice_text="合成朗读内容", transcribe=transcribe)
        transcribe.assert_not_awaited()

    async def test_failures_and_multiple_records_never_invent_content(self):
        from src.reply_context import resolve_quoted
        raw = quoted(kind="private")
        raw["message"] = [{"type": "record", "data": {"file": "unused"}}]
        params = dict(bot_qq=BOT, owner_qq=OWNER)
        for response in ({"text": ""}, {"text": "x" * 3001}, {}, None):
            with self.assertRaises(QuoteError):
                await resolve_quoted(raw, "-17", transcribe=AsyncMock(return_value=response), **params)
        with self.assertRaises(QuoteError):
            await resolve_quoted(raw, "-17", transcribe=AsyncMock(side_effect=RuntimeError()), **params)
        raw["message"].append({"type": "record", "data": {"file": "unused"}})
        callback = AsyncMock()
        with self.assertRaises(QuoteError):
            await resolve_quoted(raw, "-17", transcribe=callback, **params)
        callback.assert_not_awaited()

class PrivateQuoteTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.ConversationTests.asyncSetUp
    asyncTearDown = fixtures.ConversationTests.asyncTearDown
    receive = fixtures.ConversationTests.receive

    async def idle(self):
        await asyncio.wait_for(self.service.queue.join(), 3)

    def sample(self):
        return fixtures.event(
            mid="quote",
            message=[
                {"type": "reply", "data": {"id": -17}},
                {"type": "text", "data": {"text": "你怎么看？"}},
            ],
        )

    async def test_quote_not_treated_as_owner_memory_or_saved_source_text(self):
        self.channel.fetch_quoted = AsyncMock(
            return_value={
                "message_id": "-17",
                "author_qq": BOT,
                "text": "quoted-unique-text",
                "images": (),
            }
        )
        await self.receive(self.sample())
        await self.idle()
        self.assertIn("quoted-unique-text", str(self.model.calls[0]))
        self.assertNotIn("quoted-unique-text", str(self.service.store.history()))
        bodies = [
            r[0]
            for r in self.service.store.db.execute(
                "SELECT body FROM ltm_journal WHERE scope=?", (self.service.scope,)
            )
        ]
        self.assertNotIn("quoted-unique-text", str(bodies))

    async def test_quoted_image_uses_original_loader_and_source_label(self):
        self.channel.fetch_quoted = AsyncMock(
            return_value={
                "message_id": "-17",
                "author_qq": OWNER,
                "text": "这张原图",
                "images": (URL,),
            }
        )
        self.service.image_loader = AsyncMock()
        self.service.image_loader.load.return_value = image_data_url(png())
        await self.receive(self.sample())
        await self.idle()
        self.service.image_loader.load.assert_awaited_once_with(URL)
        content = self.model.calls[0][-1]["content"]
        self.assertTrue(any(p["type"] == "image_url" for p in content))
        self.assertIn("被引用", str(content))

    async def test_unavailable_quote_does_not_call_paid_model(self):
        self.channel.fetch_quoted = AsyncMock(
            side_effect=QuoteError("引用已撤回，请重发。")
        )
        await self.receive(self.sample())
        await self.idle()
        self.assertEqual(self.model.calls, [])
        self.assertEqual(self.service.store.usage()[0], 0)
        self.assertIn("重发", self.channel.sent[0])

    async def test_combined_quote_images_above_three_are_all_supplied(self):
        self.channel.fetch_quoted = AsyncMock(
            return_value={
                "message_id": "-17",
                "author_qq": OWNER,
                "text": "图片",
                "images": (URL,),
            }
        )
        self.service.image_loader = AsyncMock()
        self.service.image_loader.load.return_value = image_data_url(png())
        sample = self.sample()
        sample["message"].extend([{"type": "image", "data": {"url": URL}}] * 3)
        await self.receive(sample)
        await self.idle()
        self.assertEqual(len(self.model.calls), 1)
        self.assertEqual(self.service.image_loader.load.await_count, 4)
        self.assertEqual(
            sum(p["type"] == "image_url" for p in self.model.calls[0][-1]["content"]), 4
        )

    async def test_quote_routes_survive_restart_and_stay_scoped(self):
        self.service.store.note_route("123")
        self.service.store.close()
        self.service.store = Store(self.db, self.service.scope)
        self.assertTrue(self.service.store.known_route("123"))
        self.assertFalse(self.service.store.known_route("124"))
        self.assertEqual(
            self.service.store.db.execute(
                "SELECT COUNT(*) FROM quote_routes WHERE scope!=?",
                (self.service.scope,),
            ).fetchone()[0],
            0,
        )

    async def test_private_poke_is_not_a_new_user_memory_sample(self):
        gate = PokeGate(dict(fixtures.CONFIG, poke_enabled=True))
        event = gate.convert(
            {
                "post_type": "notice",
                "notice_type": "notify",
                "sub_type": "poke",
                "self_id": BOT,
                "user_id": OWNER,
                "target_id": BOT,
                "time": time.time(),
            }
        )
        await self.service.receive(event, BOT, program_event=True)
        await self.idle()
        self.assertEqual(self.service.store.memory.pending_count(), 0)
        self.assertIn("戳", str(self.model.calls[0]))

    async def test_tool_limit_and_profile_images_are_accounted(self):
        self.service.member_tools = AsyncMock()
        self.service.member_tools.read.return_value = (
            {"ok": True, "profile": {"nickname": "可见昵称"}},
            [{"type": "image_url", "image_url": {"url": image_data_url(png())}}],
        )
        self.model.complete = AsyncMock(
            side_effect=[
                tool_result(
                    "read_member_profile",
                    json.dumps({"qq": OWNER, "include_avatar": True}),
                ),
                ModelResult("头像是个红色方块。"),
            ]
        )
        await self.receive(fixtures.event("看看我的头像"))
        await self.idle()
        self.assertEqual(self.service.store.usage()[0], 2)
        self.assertIn("image_url", str(self.model.complete.await_args_list[-1].args[0]))
        self.assertNotIn("可见昵称", str(self.service.store.history()))

    async def test_search_stops_after_two_tool_calls_and_three_model_rounds(self):
        self.service.web_tools = AsyncMock()
        self.service.web_tools.execute.return_value = '{"ok":true,"results":[]}'
        self.model.complete = AsyncMock(
            side_effect=[
                tool_result(arguments='{"query":"版本一"}', call_id="c1"),
                tool_result(arguments='{"query":"版本二"}', call_id="c2"),
                ModelResult("目前只查到这些。"),
            ]
        )
        await self.receive(fixtures.event("查两个来源"))
        await self.idle()
        self.assertEqual(self.model.complete.await_count, 3)
        self.assertFalse(self.model.complete.await_args.kwargs["allow_tools"])
        self.assertEqual(self.service.web_tools.execute.await_count, 2)
        self.assertEqual(self.service.store.usage()[0], 5)


class GroupQuoteTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = groupfixtures.GroupTests.asyncSetUp
    asyncTearDown = groupfixtures.GroupTests.asyncTearDown
    receive = groupfixtures.GroupTests.receive

    async def test_visible_card_name_is_available_without_a_profile_lookup(self):
        sample = group_event("你好", mid="card-name")
        sample["sender"]["card"] = "群名片示例"
        await self.receive(sample)
        self.assertIn("群名片示例", str(self.model.calls[0]))

    async def test_unmentioned_quote_never_fetches_and_mentioned_quote_does(self):
        self.group_channel.fetch_quoted = AsyncMock(
            return_value={
                "message_id": "-17",
                "author_qq": "23456789",
                "text": "群友原话",
                "images": (),
            }
        )
        sample = group_event("请解释", mid="passive", mentioned=False)
        sample["message"].insert(0, {"type": "reply", "data": {"id": -17}})
        await self.receive(sample)
        self.group_channel.fetch_quoted.assert_not_awaited()
        sample = group_event("请解释", mid="active")
        sample["message"].insert(0, {"type": "reply", "data": {"id": -17}})
        await self.receive(sample)
        self.group_channel.fetch_quoted.assert_awaited_once_with("-17")
        self.assertIn("群友原话", str(self.model.calls[0]))


class ProfileTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.http_calls = []
        self.response_code = 0

        def handler(request):
            self.http_calls.append(request)
            return httpx.Response(
                200,
                text="_preloadCallback("
                + json.dumps(
                    {
                        "code": self.response_code,
                        "msglist": [
                            {
                                "uin": OWNER,
                                "content": "可见动态",
                                "created_time": 123,
                                "commentlist": ["不应导出的评论"],
                            }
                        ]
                        * 4,
                    }
                )
                + ");",
            )

        self.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))

        async def action(name, **kwargs):
            if name == "get_group_member_info":
                return {
                    "group_id": GROUP,
                    "user_id": OWNER,
                    "nickname": "成员昵称",
                    "card": "群名片",
                }
            if name == "get_stranger_info":
                return {
                    "user_id": OWNER,
                    "nickname": "成员昵称",
                    "long_nick": "可见签名",
                    "phone": "not-exported",
                }
            if name == "get_cookies":
                self.assertEqual(kwargs, {"domain": "user.qzone.qq.com"})
                return {
                    "cookies": f"uin=o{BOT}; p_uin=o{BOT}; skey=synthetic-session; p_skey=synthetic-session2"
                }
            raise AssertionError(name)

        self.action = AsyncMock(side_effect=action)
        self.avatar = AsyncMock(
            return_value=[
                {"type": "image_url", "image_url": {"url": image_data_url(png())}}
            ]
        )
        self.reader = MemberProfiles(
            self.action,
            fixtures.CONFIG,
            GROUP,
            qzone_client=self.http,
            avatar_loader=self.avatar,
        )

    async def asyncTearDown(self):
        await self.http.aclose()

    async def test_requested_profile_avatar_and_visible_posts_are_bounded_and_cached(
        self,
    ):
        args = json.dumps({"qq": OWNER, "include_avatar": True, "include_qzone": True})
        data, images = await self.reader.read(args, {OWNER})
        self.assertEqual(data["profile"]["card"], "群名片")
        self.assertNotIn("phone", data["profile"])
        self.assertEqual(len(data["qzone"]["posts"]), 3)
        self.assertNotIn("不应导出", str(data))
        self.assertNotIn("synthetic-session", str(data))
        self.assertTrue(images)
        await self.reader.read(args, {OWNER})
        self.assertEqual(len(self.http_calls), 1)
        self.avatar.assert_awaited_once()
        self.assertEqual(self.http_calls[0].url.host, "user.qzone.qq.com")
        self.assertEqual(self.http_calls[0].url.params["need_private_comment"], "0")

    async def test_unrelated_member_rejected_before_any_network(self):
        with self.assertRaises(ProfileError):
            await self.reader.read('{"qq":"99999999","include_qzone":true}', {OWNER})
        self.action.assert_not_awaited()
        self.assertEqual(self.http_calls, [])

    async def test_wrong_group_membership_stops_before_avatar_or_space(self):
        self.action.side_effect = None
        self.action.return_value = {"group_id": "99999999", "user_id": OWNER}
        with self.assertRaises(ProfileError):
            await self.reader.read(
                json.dumps(
                    {"qq": OWNER, "include_qzone": True, "include_avatar": True}
                ),
                {OWNER},
            )
        self.avatar.assert_not_awaited()
        self.assertEqual(self.http_calls, [])

    async def test_space_limit_or_permission_failure_is_not_empty_success(self):
        self.response_code = -10000
        data, _ = await self.reader.read(
            json.dumps({"qq": OWNER, "include_qzone": True}), {OWNER}
        )
        self.assertEqual(data["qzone"]["status"], "unavailable")
        self.assertEqual(len(self.http_calls), 1)

    async def test_unrequested_avatar_or_space_has_no_network(self):
        data, images = await self.reader.read(json.dumps({"qq": OWNER}), {OWNER})
        self.assertEqual(images, [])
        self.assertNotIn("qzone", data)
        self.avatar.assert_not_awaited()
        self.assertEqual(self.http_calls, [])
