import unittest
import asyncio
from unittest.mock import AsyncMock

from src.contracts import ModelResult
from src.group_dialogue import GroupDialogue
from tests import test_group_chat as groups


class QuestionTrackerTests(unittest.TestCase):
    def test_only_delivered_text_voice_question_and_expiry(self):
        q = GroupDialogue(clock=lambda: 1000)
        q.ask("你用什么工具？", {"12345678"}, [("voice", "v1"), ("sticker", "s1")])
        self.assertIsNone(
            q.match("嗯", "87654321", [{"type": "reply", "data": {"id": "s1"}}])
        )
        hit = q.match("工具", "87654321", [{"type": "reply", "data": {"id": "v1"}}])
        self.assertEqual(hit["confidence"], "quoted")
        q.queued(hit)
        self.assertIsNone(q.match("我觉得用Python", "87654321", []))
        q.finish(hit, False)
        self.assertIsNotNone(
            q.match("嗯", "87654321", [{"type": "reply", "data": {"id": "v1"}}])
        )
        q.clock = lambda: 1121
        self.assertIsNone(q.match("我觉得可以", "12345678", []))

    def test_voice_quote_uses_only_verified_sent_question_not_asr_or_foreign_quote(
        self,
    ):
        from src.reply_context import parse_quoted, QuoteError

        bot, group, member = "1111111111", "3333333333", "12345678"
        q = GroupDialogue(clock=lambda: 1000)
        q.ask("你选什么工具？", {member}, [("voice", "17"), ("text", "18")])
        self.assertEqual(q.known_voice_question("17"), "你选什么工具？")
        self.assertIsNone(q.known_voice_question("18"))
        data = {
            "message_id": 17,
            "message_type": "group",
            "group_id": group,
            "sender": {"user_id": bot},
            "message": [{"type": "record", "data": {"file": "never-read-local-file"}}],
        }
        params = dict(bot_qq=bot, owner_qq=member, group_id=group)
        quote = parse_quoted(
            data, "17", **params, known_bot_voice_text=q.known_voice_question("17")
        )
        self.assertEqual(quote["text"], "你选什么工具？")
        self.assertIn("not_audio_transcription", quote["text_source"])
        with self.assertRaises(QuoteError):
            parse_quoted(data, "17", **params)
        foreign = dict(data, group_id="5555555936")
        with self.assertRaises(QuoteError):
            parse_quoted(foreign, "17", **params, known_bot_voice_text="你选什么工具？")
        stranger = dict(data, sender={"user_id": member})
        with self.assertRaises(QuoteError):
            parse_quoted(
                stranger, "17", **params, known_bot_voice_text="你选什么工具？"
            )

    def test_another_members_brief_option_or_yes_no_answer_is_a_candidate(self):
        for question, answer in [
            ("Python还是MATLAB？", "Python"),
            ("你喜欢猫还是狗？", "猫"),
            ("有人用过这个工具吗？", "没有"),
        ]:
            q = GroupDialogue()
            q.ask(question, {"12345678"}, [("text", "1")])
            self.assertEqual(q.match(answer, "87654321", [])["confidence"], "candidate")

    def test_no_question_or_receipt_never_opens_wait(self):
        q = GroupDialogue()
        q.ask("你好。", {"12345678"}, [("text", "1")])
        self.assertIsNone(q.pending)
        q.ask("你用什么？", {"12345678"}, [])
        self.assertIsNone(q.pending)
        q.ask("```python\nx=?\n```", {"12345678"}, [("text", "1")])
        self.assertIsNone(q.pending)


class GroupQuestionTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = groups.GroupTests.asyncSetUp
    asyncTearDown = groups.GroupTests.asyncTearDown
    receive = groups.GroupTests.receive

    async def ask(self):
        self.group.config.update(
            group_question_replies_enabled=True,
            group_proactive_enabled=True,
            group_random_probability=0.125,
        )
        self.group.random = lambda: 0.99
        self.model.complete = AsyncMock(
            side_effect=[
                ModelResult("你们用Python还是MATLAB？"),
                ModelResult("那就先用Python，写起来方便些。"),
            ]
        )
        await self.receive(groups.group_event("聊聊工具", "ask", member="12345678"))
        self.assertIsNotNone(self.group.dialogue.pending)

    async def test_other_member_answers_without_at_and_one_reaction(self):
        await self.ask()
        await self.receive(
            groups.group_event("Python", "answer", member="87654321", mentioned=False)
        )
        self.assertEqual(self.model.complete.await_count, 2)
        self.assertIn("responding_to_bot_question", str(self.model.complete.await_args))
        self.assertIn("question_reply", str(self.model.complete.await_args))
        self.assertIsNone(self.group.dialogue.pending)
        await self.receive(
            groups.group_event(
                "我觉得都差不多", "after", member="87654321", mentioned=False
            )
        )
        self.assertEqual(self.model.complete.await_count, 2)
        self.assertEqual(self.group.config["group_random_probability"], 0.125)

    async def test_unrelated_wrong_group_and_duplicate_do_not_start_more_turns(self):
        await self.ask()
        await self.receive(
            groups.group_event(
                "今天外面有点热", "unrelated", member="87654321", mentioned=False
            )
        )
        self.assertEqual(self.model.complete.await_count, 1)
        await self.receive(
            groups.group_event(
                "我觉得Python",
                "wrong",
                member="87654321",
                group=groups.OTHER,
                mentioned=False,
            )
        )
        self.assertEqual(self.model.complete.await_count, 1)
        answer = groups.group_event(
            "我用MATLAB", "answer", member="12345678", mentioned=False
        )
        await self.receive(answer)
        await self.receive(answer)
        self.assertEqual(self.model.complete.await_count, 2)

    async def test_undelivered_question_does_not_trigger_answer(self):
        self.group.config.update(
            group_question_replies_enabled=True, group_proactive_enabled=True
        )
        self.group.random = lambda: 0.99
        self.group_channel.fail = True
        self.model.complete = AsyncMock(return_value=ModelResult("你们用什么？"))
        await self.receive(groups.group_event("问问", "ask"))
        self.assertIsNone(self.group.dialogue.pending)
        await self.receive(
            groups.group_event(
                "我觉得Python好", "answer", member="87654321", mentioned=False
            )
        )
        self.assertEqual(self.model.complete.await_count, 1)

    async def test_concurrent_status_receipt_is_not_claimed_as_question(self):
        self.group.config["group_question_replies_enabled"] = True
        entered, release = asyncio.Event(), asyncio.Event()

        async def send(value):
            if value == "你选什么工具？":
                entered.set()
                await release.wait()
                return "question-receipt"
            return "status-receipt"

        self.group_channel.send_text = send
        self.group.current_request = {"trigger": "mention", "speaker_qq": "12345678"}
        question = asyncio.create_task(
            self.group.reply("q", "你选什么工具？", conversational=True)
        )
        await entered.wait()
        await self.group.reply("status", "状态正常。")
        release.set()
        await question
        self.assertEqual(self.group.dialogue.pending["receipts"], {"question-receipt"})

    async def test_unrelated_candidate_can_be_silent_without_repeated_probe(self):
        await self.ask()
        self.model.complete.side_effect = [ModelResult("[[silent]]")]
        await self.receive(
            groups.group_event(
                "我觉得今天太热", "maybe", member="87654321", mentioned=False
            )
        )
        self.assertEqual(len(self.group_channel.sent), 1)
        await self.receive(
            groups.group_event(
                "我觉得好困", "again", member="87654321", mentioned=False
            )
        )
        self.assertEqual(self.model.complete.await_count, 2)
        self.assertEqual(len(self.group_channel.sent), 1)
