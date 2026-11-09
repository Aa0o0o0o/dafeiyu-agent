"""Existing adapter compatibility; exactly one active memory injection path."""

import json
import re
import asyncio
from contextvars import ContextVar
from pathlib import Path

from .service import MemoryService
from .config import validate_config
from .admission import withdrawal_intent

DELIVERY_GUARD = ContextVar("dafeiyu_memory_delivery", default=None)


def verify_delivery():
    guard = DELIVERY_GUARD.get()
    if guard and not guard[0].validate_packet(guard[1]):
        raise asyncio.CancelledError("Memory changed before transport commit")


class MemoryBridge:
    def __init__(self, conversation, db_path):
        self.conversation = conversation
        c = conversation.config
        options = validate_config(c.get("memory_v2", {}))
        self.memory = MemoryService(
            conversation.store.db,
            conversation.scope,
            c["bot_qq"],
            c["instance_id"],
            c["owner_qq"],
            options=options,
            registry=Path(db_path).parent / "memory-deletions.jsonl",
        )
        self.memory.registry.touch(exist_ok=True)
        self.memory.replay_registry()
        self.last_snapshot = self.memory.snapshot()

    def sync_context(self):
        current = self.memory.snapshot()
        if current != self.last_snapshot:
            clear_voice = getattr(self.conversation.voice, "clear_quote_references", None)
            if callable(clear_voice):
                clear_voice()
            self.conversation.volatile_history = []
            if hasattr(self.conversation, "recent"):
                self.conversation.recent.clear()
            request = getattr(self.conversation, "current_request", None)
            if request:
                request["recent"] = []
            self.last_snapshot = current

    def observe(self, event, incoming):
        memory = self.memory
        actor = str(event["user_id"])
        self.register_actor(event)
        subjects = {actor}
        for part in event.get("message", []):
            if isinstance(part, dict) and part.get("type") == "at":
                qq = str(part.get("data", {}).get("qq", ""))
                if re.fullmatch(r"[1-9][0-9]{4,19}", qq) and qq != memory.bot_id:
                    subjects.add(qq)
        for row in memory.repo.rows(
            "SELECT entity_id,alias FROM mv2_aliases WHERE scope_id=?", (memory.scope,)
        ):
            if row["alias"] and row["alias"] in incoming.text:
                subjects.add(row["entity_id"])
        sid = memory.observe(
            dict(
                event_id=str(event["message_id"]),
                sender_id=actor,
                subject_ids=list(subjects),
                text=incoming.text,
                event_time=event["time"],
                has_media=bool(incoming.images or incoming.attachments),
                quoted=bool(incoming.reply_id),
                source_kind="program" if incoming.program_event else "user",
                display_name=event.get("sender", {}).get("card")
                or event.get("sender", {}).get("nickname", ""),
            )
        )
        if sid:
            with self.conversation.store.db:
                self.conversation.store.memory.update_affect(
                    incoming.text, memory.now()
                )
            if withdrawal_intent(incoming.text):
                owned = [
                    row
                    for row in memory.list_memories(actor)
                    if row["status_now"] in {"active", "dormant", "frozen"}
                ]
                matches = [row for row in owned if row["value"] in incoming.text]
                target = (
                    matches[0]
                    if len(matches) == 1
                    else owned[0]
                    if len(owned) == 1
                    else None
                )
                if target:
                    memory.forget(actor, target["id"])
                    memory.db.execute(
                        "UPDATE mv2_sources SET processed=1 WHERE id=?", (sid,)
                    )
                    memory.db.commit()
                    self.sync_context()
        return sid

    def register_actor(self, event):
        self.memory.register_alias(
            str(event["user_id"]),
            event.get("sender", {}).get("card")
            or event.get("sender", {}).get("nickname", ""),
        )

    async def retract_notice(self, event):
        import time

        c = self.conversation.config
        expected = "group_recall" if ":group:" in self.memory.scope else "friend_recall"
        stamp = event.get("time")
        if (
            event.get("post_type") != "notice"
            or event.get("notice_type") != expected
            or str(event.get("self_id")) != c["bot_qq"]
            or not isinstance(stamp, (int, float))
            or isinstance(stamp, bool)
            or not abs(time.time() - stamp) <= 300
        ):
            return False
        if expected == "group_recall" and str(event.get("group_id")) != getattr(
            self.conversation, "group_id", None
        ):
            return False
        source = self.memory.repo.one(
            "SELECT id FROM mv2_sources WHERE scope_id=? AND event_id=?",
            (self.memory.scope, str(event.get("message_id"))),
        )
        if not source:
            return False
        full = self.memory.source(source["id"])
        if str(event.get("user_id")) != full["sender_id"]:
            return False
        changed = self.memory.retract_source(full["sender_id"], full["id"])
        if changed:
            self.sync_context()
            await self.conversation.cancel()
        return changed

    def recall(self, query, history=()):
        from ..conversation_state import followup_query

        conv = self.conversation
        request = getattr(conv, "current_request", None) or {}
        actor = request.get("speaker_qq", conv.peer_qq)
        query = followup_query(query, history, request.get("recent", []), actor)
        ambient = request.get("trigger") in {"random"}
        mode = (
            "ambient"
            if ambient
            else "history"
            if re.search(r"以前|上次|回忆|那次|还记得", query)
            else "direct"
        )
        subjects = set(request.get("mentioned_qqs", [])) or {actor}
        query_time = None
        match = re.search(r"(20\d{2})[-年/](\d{1,2})[-月/](\d{1,2})", query)
        if match and mode == "history":
            from datetime import datetime
            from .maintenance import TZ

            try:
                query_time = datetime(
                    *map(int, match.groups()), 12, tzinfo=TZ
                ).timestamp()
            except ValueError:
                pass
        return self.memory.recall(
            query, actor, mode=mode, subject_ids=subjects, query_time=query_time
        )

    @staticmethod
    def command(text):
        if text.startswith("/dafeiyu memory"):
            return text[len("/dafeiyu memory") :].strip()
        cmd = text.removeprefix("大肥鱼").strip()
        aliases = {
            "记忆": "list",
            "记忆整理状态": "maintenance_status",
            "不要记我": "off_me",
            "不要记住我": "off_me",
            "请不要记我": "off_me",
            "我不想被记住": "off_me",
            "别记我": "off_me",
            "暂停记忆": "off_me",
            "停止记忆": "off_me",
            "恢复记忆": "on_me",
            "允许记我": "on_me",
            "忘记我": "forget_me",
            "清空记忆": "forget_me",
            "待确认记忆": "review",
        }
        if cmd in aliases:
            return aliases[cmd]
        for prefix, command in [
            ("记住：", "remember "),
            ("记住:", "remember "),
            ("纠正：", "correct "),
            ("纠正:", "correct "),
            ("忘记：", "forget "),
            ("忘记:", "forget "),
            ("记忆来源：", "explain "),
            ("记忆来源:", "explain "),
            ("保护记忆 ", "pin "),
            ("取消保护 ", "unpin "),
            ("撤回记忆 ", "forget "),
        ]:
            if cmd.startswith(prefix):
                return command + cmd[len(prefix) :].strip()
        return None

    async def handle(self, eid, text, actor):
        cmd = self.command(text)
        if cmd is None:
            return False
        memory, conversation = self.memory, self.conversation
        bits = cmd.split(maxsplit=2)
        op = bits[0] if bits else "list"
        target = bits[1] if len(bits) > 1 else ""
        if target and not target.startswith("V"):
            mapped = memory.repo.one(
                "SELECT new_id FROM mv2_migrations WHERE scope_id=? AND legacy_key=?",
                (memory.scope, target),
            )
            target = mapped["new_id"] if mapped else target
        try:
            changed = False
            if op in {"off_me", "forget_me"}:
                memory.forget(actor, all_mine=True, off=op == "off_me")
                changed = True
                result = "已清除当前范围涉及你的记忆和派生内容；" + (
                    "后续也不记，直到你恢复。"
                    if op == "off_me"
                    else "以后有新证据时可从新消息重新了解你。"
                )
            elif op == "on_me":
                memory.on_me(actor)
                changed = True
                result = "已恢复，从新消息开始记录；删除的旧内容不会恢复。"
            elif op == "remember":
                value = cmd.partition(" ")[2]
                result = "已保存明确记忆：" + memory.remember(actor, value, eid)
            elif op == "correct":
                value = bits[2] if len(bits) > 2 else ""
                mid = memory.correct(actor, target, value, eid)
                changed = bool(mid)
                result = (
                    "已作废旧记忆及派生，保存本人纠正：" + mid
                    if mid
                    else "未找到你可纠正的记忆，或内容无效。"
                )
            elif op == "forget":
                changed = memory.forget(actor, target)
                result = (
                    "已删除记忆、关联原文及派生，旧任务/回复会失效。"
                    if changed
                    else "未找到你可删除的记忆。"
                )
            elif op in {"pin", "unpin"}:
                changed = memory.pin(actor, target, op == "pin")
                result = (
                    "已更新自然衰减保护；仍可纠正、删除且不越过绝对期限。"
                    if changed
                    else "未找到你可操作的当前记忆。"
                )
            elif op == "explain":
                detail = memory.explain(actor, target)
                result = (
                    json.dumps(detail, ensure_ascii=False)
                    if detail
                    else "未找到你可查看的来源。"
                )
            elif op == "maintenance_status":
                result = (
                    json.dumps(memory.maintenance_status(), ensure_ascii=False)
                    if actor == conversation.config["owner_qq"]
                    else "整理诊断只对机器人主人开放。"
                )
            elif op in {"list", "review"}:
                items = memory.list_memories(actor, review=op == "review")
                result = (
                    "\n".join(
                        f"{x['id']} [{x['layer']}/{x['status_now']}] {x['value']}"
                        for x in items
                    )
                    or "当前没有你可查看的记忆。"
                )
            else:
                result = "记忆命令：list / explain 编号 / correct 编号 新内容 / forget 编号 / forget_me / off_me / on_me / pin 编号 / unpin 编号 / review / maintenance_status。"
            if changed:
                conversation.volatile_history = []
                if hasattr(conversation, "recent"):
                    conversation.recent.clear()
                await conversation.cancel()
            await conversation.reply(eid, result)
        except ValueError:
            await conversation.reply(
                eid, "记忆操作未执行：内容、来源、本人权限或记忆开关不符合要求。"
            )
        return True
