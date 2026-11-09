"""Small, scoped dialogue hints derived from already available conversation data.

These are fallible observations, not personality labels or persisted memories.
"""

import re


_FOLLOWUP = re.compile(
    r"^(?:(?:那|所以|然后|后来|之前|刚才|这个|那个|它|这件事|那件事|还有|继续|为什么|怎么办).{0,16}|嗯[？?]?|然后呢[？?]?|后来呢[？?]?)$"
)
_NEGATED = re.compile(r"(?:不|没|并不|没有|不再|无需|未|别|不用|从不|从未)(?:太|再|那么|很|怎么)?$")
_QUOTES = re.compile(r"[“\"]([^”\"]*)[”\"]|‘([^’]*)’|「([^」]*)」|『([^』]*)』")
_REPORTED = re.compile(r"(?:他说|她说|他们说|别人说|新闻说|书里说|引用|假如|假设|例如|比如|小说里)")


def _unquoted(text):
    return _QUOTES.sub(" ", text if isinstance(text, str) else "")


def _affirmed(text, pattern):
    for match in re.finditer(pattern, text):
        prefix = text[max(0, match.start() - 6) : match.start()]
        other_subject = re.search(
            r"(?:她|他|它|你|他们|她们|别人|朋友|同学|大肥鱼)(?:现在|今天|觉得|感到|感觉|有点|非常|很|好|也)?$",
            prefix,
        )
        if not _NEGATED.search(prefix) and not other_subject:
            return True
    return False


def appraise(text):
    """Return cautious communication hints, without claiming knowledge of feelings."""
    body = _unquoted(text).strip()
    reported = bool(_REPORTED.search(body))
    if reported:
        clauses = re.split(r"[，,。；;！？!?]|但是|不过|然而|但", body)
        own = [
            clause for clause in clauses
            if not _REPORTED.search(clause)
            and re.search(r"(?:我|自己)(?:现在|今天|觉得|感到|感觉|有点|很|好|也)", clause)
        ]
        if not own:
            return {"event": "unclear", "style": "plain", "delta": (0, 0, 0)}
        body = "，".join(own)
    if _affirmed(body, r"难过|伤心|害怕|焦虑|难受|委屈|睡不着"):
        return {"event": "possible_difficulty", "style": "gentle", "delta": (-0.01, -0.03, -0.01)}
    if _affirmed(body, r"开心|高兴|成功了|谢谢|太棒|哈哈"):
        return {"event": "positive", "style": "playful", "delta": (0.06, 0.04, 0.02)}
    if _affirmed(body, r"生气|烦躁|气死|讨厌"):
        return {"event": "possible_frustration", "style": "gentle", "delta": (-0.01, 0.02, -0.01)}
    if _affirmed(body, r"困了|好累|熬夜"):
        return {"event": "possible_tiredness", "style": "gentle", "delta": (-0.01, -0.04, -0.02)}
    if re.search(r"原来如此|误会了|说清楚了|没事了|解决了|放心了", body):
        return {"event": "possible_resolution", "style": "plain", "delta": (0.02, -0.02, 0)}
    return {"event": "unclear", "style": "plain", "delta": (0, 0, 0)}


def followup_query(text, history=(), recent=(), actor=None):
    """Expand a short follow-up with a preceding message from the same speaker only."""
    current = text.strip() if isinstance(text, str) else ""
    if not current or len(current) > 24 or not _FOLLOWUP.fullmatch(current):
        return current
    candidates = []
    if recent:
        for entry in reversed(recent):
            if not isinstance(entry, dict) or str(entry.get("member_qq", "")) != str(actor):
                continue
            candidates.append(entry.get("text", ""))
    else:
        for entry in reversed(history):
            if isinstance(entry, dict) and entry.get("role") == "user":
                candidates.append(entry.get("content", ""))
    from .memory_state import safe_text

    for candidate in candidates:
        if not isinstance(candidate, str) or not candidate.strip():
            continue
        if not safe_text(candidate) or candidate.startswith("["):
            continue
        previous = candidate.strip()[:240]
        if previous != current and not _FOLLOWUP.fullmatch(previous):
            return current + " " + previous
    return current


def dialogue_context(text, history=(), recent=(), actor=None):
    """Describe the present turn; prior messages remain the source of truth."""
    event = appraise(text)
    body = _unquoted(text)
    if re.search(r"(?:别|不要|不用|先别).{0,5}(?:建议|分析|解决|说教)", body):
        intent = "explicit_no_advice"
    elif re.search(r"(?:怎么办|怎么解决|帮我|帮忙|查一下|查找|解释|分析|研究|写代码)", body):
        intent = "possible_request"
    elif event["event"] in {"possible_difficulty", "possible_frustration", "possible_tiredness"}:
        intent = "possible_support"
    elif _FOLLOWUP.fullmatch(body.strip()):
        intent = "possible_followup"
    else:
        intent = "open"
    detail_body = re.sub(r"(?:先别|不要|不用|别).{0,5}分析", "", body)
    detailed = bool(re.search(r"代码|公式|表格|详细|完整|严肃|正经|论文|报告|教程|分析", detail_body))
    return {
        "intent_hint": intent,
        "event_hint": event["event"],
        "delivery_hint": "serious" if detailed else event["style"],
        "media_hint": "text" if detailed or re.search(r"只(?:发|用|给我)?文字|(?<!不)(?:用|发|以)文字(?:回答|回复|就好)|(?:别|不要|不用).{0,3}语音", body) else "contextual",
        "is_contextual_followup": followup_query(text, history, recent, actor) != text.strip(),
        "notice": "仅是当前语句的低置信提示，不是心理诊断或长期事实；结合真实上下文自行判断，可修正、追问或结束话题。用户明确的交流要求优先。",
    }


def experience_links(items):
    """Link related retrieved facts transiently, without creating a new fact."""
    from .memory_v2.repository import terms

    stop = {"喜欢", "觉得", "事情", "以前", "今天", "自己", "现在", "我们", "他们"}
    prepared = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("value"), str):
            continue
        if not item.get("id") or not item.get("subject_id"):
            continue
        words = {word for word in terms(item["value"]) if len(word) >= 2} - stop
        prepared.append((item, words))
    links = []
    for index, (left, left_words) in enumerate(prepared):
        for right, right_words in prepared[index + 1 :]:
            common = left_words.intersection(right_words)
            if left["subject_id"] != right["subject_id"] or not common:
                continue
            links.append({
                "subject_id": left["subject_id"],
                "memory_ids": [left["id"], right["id"]],
                "notice": "按共同词项临时关联，含义需要结合各自来源判断；这不是新事实或永久画像。",
            })
            if len(links) >= 3:
                return links
    return links
