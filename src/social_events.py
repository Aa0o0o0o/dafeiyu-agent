"""Validated poke triggers and safe, bounded group mention rendering."""

import re
import time
import hashlib
import json
from collections import deque

from .conversation import qq_id


class PokeGate:
    def __init__(self, config):
        self.config = config
        self.seen = deque(maxlen=512)

    def convert(self, event, now=None):
        now = time.time() if now is None else now
        c = self.config
        if not c.get("poke_enabled", False):
            return None
        if (
            event.get("post_type") != "notice"
            or event.get("notice_type") != "notify"
            or event.get("sub_type") != "poke"
            or qq_id(event.get("self_id")) != c["bot_qq"]
            or qq_id(event.get("target_id")) != c["bot_qq"]
        ):
            return None
        group = qq_id(event.get("group_id"))
        # NapCat private poke separates conversation peer (user_id) from operator.
        sender = qq_id(
            event.get("sender_id")
            if not group and "sender_id" in event
            else event.get("user_id")
        )
        stamp = event.get("time")
        if (
            not sender
            or sender == c["bot_qq"]
            or isinstance(stamp, bool)
            or not isinstance(stamp, (int, float))
            or not abs(now - stamp) <= 300
        ):
            return None
        if group:
            if group not in c.get("group_ids", []) and not c.get(
                "group_all_joined", False
            ):
                return None
        elif sender != c["owner_qq"] and not c.get("private_all_friends", False):
            return None
        key = group or "private"
        # Exact transport replays are duplicates, not a per-person/group cooldown.
        fingerprint = hashlib.sha256(
            json.dumps(event, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()[:24]
        if fingerprint in self.seen:
            return None
        self.seen.append(fingerprint)
        text = "[QQ系统事件：这位真实发言者刚戳了戳大肥鱼。请结合当前上下文自然接话，通常简短一两句，不要固定套话。]"
        parts = [{"type": "text", "data": {"text": text}}]
        if group:
            parts.insert(0, {"type": "at", "data": {"qq": c["bot_qq"]}})
        return {
            "post_type": "message",
            "message_type": "group" if group else "private",
            "sub_type": "normal" if group else "friend",
            "self_id": c["bot_qq"],
            "user_id": sender,
            "sender": {"user_id": sender},
            "time": stamp,
            "message_id": f"poke:{key}:{sender}:{fingerprint}",
            "message": parts,
            **({"group_id": group} if group else {}),
        }


def mention_segments(text, allowed):
    """Only actual known members become real @, at most two per outgoing bubble."""
    parts, start, mentioned = [], 0, set()
    for match in re.finditer(r"@([1-9][0-9]{4,19})(?![0-9])", text):
        member = match[1]
        if member not in allowed or member in mentioned or len(mentioned) >= 2:
            continue
        if match.start() > start:
            parts.append(
                {"type": "text", "data": {"text": text[start : match.start()]}}
            )
        parts.append({"type": "at", "data": {"qq": member}})
        mentioned.add(member)
        start = match.end()
    if start < len(text):
        parts.append({"type": "text", "data": {"text": text[start:]}})
    return parts
