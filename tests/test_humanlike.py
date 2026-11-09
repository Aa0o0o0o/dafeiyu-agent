"""Synthetic checks for dialogue continuity, provenance and exploration feedback."""

import asyncio
import json
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from src.autonomy import Autonomy
from src.contracts import ModelResult
from src.conversation import ConversationService
from src.conversation_state import appraise, dialogue_context, experience_links, followup_query
from src.voice.delivery import plan_delivery
from tests import test_autonomy as autonomous_fixtures
from tests import test_conversation as fixtures
from tools.humanlike_eval import prepare, score


class DialogueContextTests(unittest.TestCase):
    def test_blind_evaluation_uses_60_distinct_synthetic_cases(self):
        fixture = Path(__file__).resolve().parents[1] / "fixtures/humanlike_scenarios_20261104.json"
        cases = json.loads(fixture.read_text(encoding="utf-8"))
        self.assertEqual(len(cases), 60)
        self.assertEqual(len({case["id"] for case in cases}), 60)
        self.assertTrue(all(case["context"] and case["criteria"] for case in cases))
        sample = {case["id"]: "旧回答" for case in cases[:3]}
        newer = {case["id"]: "新回答" for case in cases[:3]}
        blind, key = prepare(sample, newer, cases, seed=7)
        self.assertTrue(all("candidate" not in item and "baseline" not in item for item in blind["cases"]))
        ratings = [{"id": item["id"], "preference": key[item["id"]]} for item in blind["cases"]]
        self.assertEqual(score(ratings, key)["candidate_win"], 3)
        with self.assertRaises(ValueError):
            score(ratings + ratings[:1], key)

    def test_negated_quoted_and_reported_emotion_is_not_speaker_fact(self):
        for utterance in ["我不难过了", "我并不害怕", "她说‘我很难过’", "新闻说大家很开心"]:
            with self.subTest(utterance=utterance):
                self.assertEqual(appraise(utterance)["event"], "unclear")
        self.assertEqual(appraise("我今天很难过")["event"], "possible_difficulty")
        self.assertEqual(appraise("其实我不难过，现在很开心")["event"], "positive")
        self.assertEqual(appraise("她说她很难过，但我很开心")["event"], "positive")
        self.assertEqual(appraise("她很伤心，我很开心")["event"], "positive")
        self.assertEqual(appraise("事情终于解决了")["event"], "possible_resolution")

    def test_dialogue_hints_are_not_diagnoses_or_forced_actions(self):
        state = dialogue_context("先别分析，我只是想说说")
        self.assertEqual(state["intent_hint"], "explicit_no_advice")
        self.assertEqual(state["media_hint"], "contextual")
        self.assertIn("不是心理诊断", state["notice"])
        state = dialogue_context("你能帮我详细分析吗")
        self.assertEqual(state["media_hint"], "text")
        self.assertEqual(state["delivery_hint"], "serious")
        self.assertEqual(dialogue_context("不用文字，你讲吧")["media_hint"], "contextual")
        self.assertEqual(dialogue_context("这次用文字回复")["media_hint"], "text")
        self.assertEqual(plan_delivery("我在这里。", dialogue={"delivery_hint": "gentle"})["style"], "gentle")
        self.assertEqual(plan_delivery("我在，慢慢说。", dialogue={"delivery_hint": "plain"})["style"], "gentle")

    def test_ambiguous_followup_uses_only_same_speaker_and_safe_history(self):
        history = [
            {"role": "user", "content": "我在准备鲸鱼科普汇报"},
            {"role": "assistant", "content": "讲到哪里了？"},
        ]
        self.assertIn("鲸鱼科普汇报", followup_query("后来呢？", history))
        group = [
            {"member_qq": "111", "text": "我正在做鲸鱼科普"},
            {"member_qq": "222", "text": "我喜欢私人代号"},
        ]
        query = followup_query("然后呢？", recent=group, actor="111")
        self.assertIn("鲸鱼科普", query)
        self.assertNotIn("私人代号", query)
        self.assertEqual(followup_query("然后呢？", recent=group, actor="333"), "然后呢？")
        self.assertEqual(
            followup_query("后来呢？", [{"role": "user", "content": "密码：synthetic-secret"}]),
            "后来呢？",
        )

    def test_experience_links_preserve_subject_and_never_create_new_facts(self):
        items = [
            {"id": "m1", "subject_id": "a", "value": "正在准备鲸鱼科普"},
            {"id": "m2", "subject_id": "a", "value": "鲸鱼科普讲稿完成"},
            {"id": "m3", "subject_id": "b", "value": "鲸鱼科普也很有趣"},
        ]
        links = experience_links(items)
        self.assertEqual([link["memory_ids"] for link in links], [["m1", "m2"]])
        self.assertIn("不是新事实", links[0]["notice"])


class MemoryContinuityTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.ConversationTests.asyncSetUp
    asyncTearDown = fixtures.ConversationTests.asyncTearDown

    async def test_followup_recall_obeys_forget_and_does_not_duplicate_storage(self):
        await self.service.close()
        cfg = dict(fixtures.CONFIG, memory_v2={"enabled": True, "mode": "active"})
        self.service = ConversationService(cfg, self.db, self.model, self.channel)
        actor = cfg["owner_qq"]
        mid = self.service.v2.remember(actor, "我喜欢鲸鱼标本", "synthetic-source")
        history = [
            {"role": "user", "content": "我之前聊到鲸鱼标本"},
            {"role": "assistant", "content": "嗯，我记得。"},
        ]
        packet = self.service.memory_bridge.recall("后来呢？", history=history)
        self.assertIn(mid, packet["revisions"])
        self.service.v2.forget(actor, mid)
        self.assertFalse(self.service.v2.validate_packet(packet))
        self.assertNotIn(mid, self.service.memory_bridge.recall("后来呢？", history=history)["revisions"])

    async def test_dialogue_state_reaches_same_model_call_without_extra_requests(self):
        await self.service.receive(fixtures.event("今天好难过，先别分析", "state"), fixtures.CONFIG["bot_qq"])
        await asyncio.wait_for(self.service.queue.join(), 3)
        self.assertEqual(len(self.model.calls), 1)
        messages = self.model.calls[0]
        payload = next(m["content"] for m in messages if isinstance(m["content"], str) and m["content"].startswith("<memory_and_affect_data>"))
        self.assertIn('"intent_hint": "explicit_no_advice"', payload)
        self.assertEqual(len(self.channel.sent), 1)


class AutonomousContinuityTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.ConversationTests.asyncSetUp
    asyncTearDown = fixtures.ConversationTests.asyncTearDown
    prepare = autonomous_fixtures.AutonomyTests.prepare

    async def test_root_goal_and_evidence_backed_feedback_survive_restart(self):
        agent = self.prepare()
        self.assertTrue(await agent.run(True))
        thread = agent.context()["interest_threads"][0]
        self.assertEqual(thread["root_topic"], "公开科普中的鱼类颜色")
        self.assertEqual(thread["current_question"], "鱼类颜色与环境的关系")
        row = agent.db.execute(
            "SELECT execution,assessment,attempted_tools,observed_tools FROM autonomy_run_feedback WHERE scope=?",
            (agent.scope,),
        ).fetchone()
        self.assertEqual(row[:2], ("observed", "unknown"))
        self.assertEqual(json.loads(row[2]), ["web_search"])
        self.assertEqual(json.loads(row[3]), ["web_search"])
        counts = agent.context()["tool_experience"]["by_tool"]["web_search"]
        self.assertEqual(counts["runs_attempted"], 1)
        self.assertEqual(counts["runs_with_observation"], 1)
        self.assertEqual(Autonomy(self.service).context()["interest_threads"][0], thread)
        self.assertEqual(self.service.store.usage()[0], 3)

    async def test_existing_goal_migration_does_not_invent_an_original_topic(self):
        agent = self.prepare()
        with agent.db:
            agent.db.execute("DELETE FROM autonomy_goal_threads WHERE scope=?", (agent.scope,))
        restarted = Autonomy(self.service)
        thread = restarted.context()["interest_threads"][0]
        self.assertEqual(thread["origin"], "migrated_current")
        self.assertEqual(thread["root_topic"], "公开科普中的鱼类颜色")

    async def test_older_relevant_experience_needs_successful_observation(self):
        agent = self.prepare()
        await agent.run(True)
        older_id = agent.db.execute("SELECT id FROM autonomy_runs WHERE scope=?", (agent.scope,)).fetchone()[0]
        goal_id = agent.db.execute("SELECT id FROM autonomy_goals WHERE scope=?", (agent.scope,)).fetchone()[0]
        with agent.db:
            agent.db.execute("UPDATE autonomy_runs SET created=? WHERE id=?", (time.time() - 100, older_id))
            for index in range(4):
                agent.db.execute(
                    "INSERT INTO autonomy_runs(id,scope,goal,day,status,query,receipt,reflection,created) VALUES(?,?,?,?,?,?,?,?,?)",
                    (f"recent-{index}", agent.scope, goal_id, "2026-11-04", "rested", "", "", "", time.time() + index),
                )
        related = agent.context("鱼类颜色")["related_experiences"]
        self.assertEqual([item["run_id"] for item in related], [older_id])
        self.assertIn("example.com", related[0]["observation"])
        with agent.db:
            agent.db.execute("UPDATE autonomy_runs SET receipt=? WHERE id=?", ('{"actions":[]}', older_id))
        self.assertEqual(agent.context("鱼类颜色")["related_experiences"], [])

    async def test_failure_classification_and_clear_erase_derived_experience(self):
        agent = self.prepare()
        self.service.web_tools.execute.side_effect = TimeoutError()
        self.assertFalse(await agent.run(True))
        row = agent.db.execute(
            "SELECT execution,assessment,failure_kind,attempted_tools,observed_tools FROM autonomy_run_feedback WHERE scope=?",
            (agent.scope,),
        ).fetchone()
        self.assertEqual(row[:3], ("failed", "unknown", "timeout"))
        self.assertEqual(json.loads(row[3]), ["web_search"])
        self.assertEqual(json.loads(row[4]), [])
        agent.clear()
        self.assertEqual(agent.context()["interest_threads"], [])
        self.assertEqual(agent.context()["tool_experience"]["by_tool"], {})
        self.assertEqual(agent.db.execute("SELECT COUNT(*) FROM autonomy_run_feedback WHERE scope=?", (agent.scope,)).fetchone()[0], 0)

    async def test_sent_context_and_punctuation_dedup_do_not_block_new_fact(self):
        self.service.config = dict(self.service.config, autonomy_extension={"share_owner": True, "chat_interests": True})
        agent = self.prepare()
        agent.configure(6)
        self.assertTrue(agent.social.enqueue_message("first", "我对海洋颜色有个新想法。"))
        self.assertTrue(await agent.social.deliver())
        self.assertEqual(agent.social.recent_delivered()[0]["body"], "我对海洋颜色有个新想法。")
        self.assertFalse(agent.social.enqueue_message("duplicate", "我对海洋颜色有个新想法！"))
        self.assertTrue(agent.social.enqueue_message("new", "我对海洋声音有个新想法！"))
        agent.clear()
        self.assertEqual(agent.social.recent_delivered(), [])

    async def test_model_assessment_is_marked_as_unverified(self):
        agent = self.prepare()
        self.service.model.complete = AsyncMock(side_effect=[
            ModelResult('{"action":"search","query":"鲸鱼","success_criteria":"找到原始来源"}'),
            ModelResult('{"reflection":"找到了一个候选来源。","next_question":"继续核验","decision":"continue","assessment":"partial"}'),
        ])
        self.assertTrue(await agent.run(True))
        row = agent.db.execute("SELECT criteria,assessment FROM autonomy_run_feedback WHERE scope=?", (agent.scope,)).fetchone()
        self.assertEqual(row, ("找到原始来源", "partial"))
        self.assertIn("不是独立核验", agent.context()["notice"])

    async def test_goal_without_completion_criteria_cannot_be_marked_complete(self):
        agent = self.prepare()
        self.service.model.complete = AsyncMock(side_effect=[
            ModelResult('{"action":"search","query":"鲸鱼"}'),
            ModelResult('{"reflection":"找到一个候选来源。","next_question":"","decision":"done","assessment":"complete"}'),
        ])
        self.assertTrue(await agent.run(True))
        row = agent.db.execute("SELECT assessment FROM autonomy_run_feedback WHERE scope=?", (agent.scope,)).fetchone()
        self.assertEqual(row[0], "unknown")

    async def test_viewpoint_updates_are_linked_to_actual_run_and_removed_on_clear(self):
        agent = self.prepare()
        self.service.model.complete = AsyncMock(side_effect=[
            ModelResult('{"action":"search","query":"鱼类颜色"}'),
            ModelResult('{"reflection":"找到一个来源。","next_question":"继续查鱼类颜色","decision":"continue","viewpoint":"我暂时觉得颜色与环境有关"}'),
            ModelResult('{"action":"search","query":"鱼类颜色与环境"}'),
            ModelResult('{"reflection":"又找到一个来源。","next_question":"继续核验","decision":"continue","viewpoint":"我现在觉得还需要考虑环境以外的因素","viewpoint_update_reason":"发现另一种解释"}'),
        ])
        self.assertTrue(await agent.run(True))
        first = agent.context()["tentative_viewpoints"][0]
        self.assertIn("暂时觉得", first["content"])
        self.assertTrue(await agent.run(True))
        second = agent.context()["tentative_viewpoints"][0]
        self.assertEqual(second["supersedes"], first["id"])
        self.assertNotEqual(second["source_run_id"], first["source_run_id"])
        self.assertEqual(second["update_reason"], "发现另一种解释")
        self.assertIn("example.com", second["observation"])
        with agent.db:
            agent.db.execute("UPDATE autonomy_runs SET receipt='{}' WHERE id=?", (second["source_run_id"],))
        self.assertEqual(agent.context()["tentative_viewpoints"], [])
        agent.clear()
        self.assertEqual(agent.db.execute("SELECT COUNT(*) FROM autonomy_viewpoints WHERE scope=?", (agent.scope,)).fetchone()[0], 0)

    async def test_reusable_method_requires_two_distinct_observed_search_fetch_chains(self):
        agent = self.prepare()
        self.service.config = dict(self.service.config, autonomy_extension={"web_fetch": True})
        urls = ["https://example.com/one", "https://example.com/two"]
        self.service.web_tools.execute = AsyncMock(side_effect=[
            json.dumps({"ok": True, "results": [{"url": urls[0]}]}),
            json.dumps({"ok": True, "url": urls[0], "content": "first actual page"}),
            json.dumps({"ok": True, "results": [{"url": urls[1]}]}),
            json.dumps({"ok": True, "url": urls[1], "content": "second actual page"}),
        ])
        self.service.model.complete = AsyncMock(side_effect=[
            ModelResult('{"action":"search","query":"鱼类颜色"}'),
            ModelResult(json.dumps({"action": "fetch", "url": urls[0]})),
            ModelResult('{"reflection":"已取得第一份来源。","next_question":"继续查鱼类","decision":"continue"}'),
            ModelResult('{"action":"search","query":"海洋色彩"}'),
            ModelResult(json.dumps({"action": "fetch", "url": urls[1]})),
            ModelResult('{"reflection":"已取得第二份来源。","next_question":"继续查鱼类","decision":"continue"}'),
        ])
        self.assertTrue(await agent.run(True))
        self.assertEqual(agent.context()["tool_experience"]["methods"][0]["status"], "candidate")
        self.assertTrue(await agent.run(True))
        method = agent.context()["tool_experience"]["methods"][0]
        self.assertEqual(method["status"], "observed_in_two_queries")
        self.assertEqual(method["independent_queries"], 2)
        self.assertIn("仍需核对", method["description"])
        agent.clear()
        self.assertEqual(agent.context()["tool_experience"]["methods"], [])
