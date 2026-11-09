"""Program-owned identity, source and proposal validation."""

import hashlib
import json
import re

from ..memory_state import safe_text, NONFACT
from .decay import finite, POLICIES

KINDS = {"core", "working", "episode", "semantic", "style", "relation_summary"}
PREDICATE = re.compile(r"[a-zA-Z][a-zA-Z0-9_:.-]{0,79}")


def digest(*parts):
    return hashlib.sha256(
        json.dumps(
            parts, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


def subject_id(value):
    return (
        isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{4,19}", value) is not None
    )


def safe_source(text):
    return (
        isinstance(text, str)
        and 1 <= len(text) <= 3000
        and safe_text(text) is not None
        and not re.search(
            r"忽略.{0,12}(?:指令|上文)|最高指令|把.{0,30}当.{0,10}系统提示|修改.{0,8}(?:权限|人格)",
            text,
        )
    )


def withdrawal_intent(text):
    return (
        text.startswith("我")
        and len(text) < 200
        and not re.search(
            r"如果|假如|假设|他说|她说|我朋友|我的朋友|不是开玩笑|不是在开玩笑|没开玩笑|没有说错|没说错|不撤回",
            text,
        )
        and bool(re.search(r"我.{0,30}(?:开玩笑|说错了|不是事实|撤回)", text))
    )


def candidate(item, sources, now):
    if not isinstance(item, dict):
        return None
    allowed = {
        "operation",
        "kind",
        "subject_ids",
        "predicate",
        "value",
        "polarity",
        "assertion_mode",
        "temporal_class",
        "source_event_ids",
        "summary",
        "confidence_score",
        "importance",
        "event_time",
        "evidence_note",
    }
    if set(item) - allowed:
        return None
    ids = item.get("source_event_ids", [])
    subjects = item.get("subject_ids", [])
    value, predicate = item.get("value"), item.get("predicate")
    if (
        not isinstance(ids, list)
        or not 1 <= len(ids) <= 30
        or any(not isinstance(x, str) or x not in sources for x in ids)
        or not isinstance(subjects, list)
        or not 1 <= len(subjects) <= 10
        or any(not subject_id(x) for x in subjects)
        or not isinstance(value, str)
        or not 1 <= len(value) <= 300
        or safe_text(value) is None
        or not isinstance(predicate, str)
        or not PREDICATE.fullmatch(predicate)
        or item.get("kind") not in KINDS - {"core"}
        or item.get("temporal_class") not in POLICIES.keys() - {"core"}
        or item.get("operation", "ADD")
        not in {"ADD", "MERGE_EVIDENCE", "SUPERSEDE", "IGNORE"}
        or item.get("assertion_mode")
        not in {"self_report", "third_party", "observation", "inference"}
        or item.get("polarity", "positive") not in {"positive", "negative"}
        or not finite(item.get("confidence_score"), 0.75, 1)
        or not finite(item.get("importance", 0.5))
    ):
        return None
    if predicate == "meme_explanation" and (
        item["kind"] != "semantic"
        or item["assertion_mode"] != "observation"
        or item["temporal_class"] != "episode"
        or not re.search(r"谐音|缩写|意思|意为|指的是|读作", value)
        or not re.search(r"是|意为|表示|指|读作|=", value)
        or re.search(r"可能|好像|猜|不确定|不知道|[？?]", value)
    ):
        return None
    selected = [sources[x] for x in ids]
    if item.get("operation") == "IGNORE" or any(
        s["source_kind"] != "user"
        or s["body"] is None
        or s["expires_at"] <= now
        or s["source_state"] != "available"
        for s in selected
    ):
        return None
    if any(not set(subjects) <= set(s["subjects"]) for s in selected):
        return None
    if item["assertion_mode"] == "self_report" and any(
        s["sender_id"] != subjects[0] for s in selected
    ):
        return None
    # Exact grounded value; summary is composed by code, not trusted free prose.
    if not any(value in s["body"] for s in selected) or any(
        NONFACT.search(s["body"]) for s in selected
    ):
        return None
    if item["assertion_mode"] == "self_report" and not any(
        "我" in s["body"] for s in selected
    ):
        return None
    clauses = []
    for source in selected:
        matching = [
            clause
            for clause in re.split(r"[，,。；;！？\n]|但是|不过|现在", source["body"])
            if value in clause
        ]
        if matching:
            clauses.append(matching[0])
    negative = any(
        re.search(r"不喜欢|不爱|讨厌|不喝|不想", clause) for clause in clauses
    )
    if predicate == "likes" and negative:
        return None
    if predicate == "dislikes" and not negative:
        return None
    if (
        negative
        and item.get("polarity", "positive") == "positive"
        and predicate != "dislikes"
        and not re.search(r"不喜欢|不爱|讨厌|不喝|不想", value)
    ):
        return None
    if item["kind"] == "style" and re.search(
        r"傻[逼比]|操你|去死|仇恨|杀光|管理员命令", value
    ):
        return None
    if item.get("event_time") is not None and item["event_time"] not in [
        s["event_time"] for s in selected
    ]:
        return None
    item = dict(item)
    item.update(
        subject_ids=sorted(set(subjects)),
        source_event_ids=sorted(set(ids)),
        importance=item.get("importance", 0.5),
        polarity=item.get("polarity", "positive"),
    )
    return item
