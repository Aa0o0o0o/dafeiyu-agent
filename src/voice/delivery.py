"""Bounded delivery hints from this scope only; never rewrite the spoken reply."""

import math
import re

STYLES = frozenset({"plain", "playful", "gentle", "serious", "curious"})


def plan_delivery(text, incoming="", affect=None, previous_factor=1.0, dialogue=None):
    # Deliberately conservative lexical cues, not an emotion diagnosis or LLM call.
    context = incoming + "\n" + text
    hint = dialogue.get("delivery_hint") if isinstance(dialogue, dict) else None
    if re.search(r"正经|认真|严肃|药物|剂量|急救|法律|公式|代码|论文", context):
        style = "serious"
    elif hint in {"gentle", "playful", "serious", "curious"}:
        style = hint
    elif hint == "plain":
        if re.search(r"别着急|慢慢说|陪着你", text):
            style = "gentle"
        elif re.search(r"哈哈|嘿嘿|太棒啦|太好啦|好耶", text):
            style = "playful"
        elif re.search(r"后来呢|然后呢|怎么想到|为什么呀|好奇", text):
            style = "curious"
        else:
            style = "plain"
    elif re.search(
        r"难过|伤心|害怕|焦虑|难受|委屈|好累|睡不着|别着急|慢慢说|陪着你", context
    ):
        style = "gentle"
    elif re.search(r"哈哈|嘿嘿|太棒啦|太好啦|好耶", text):
        style = "playful"
    elif re.search(r"后来呢|然后呢|怎么想到|为什么呀|好奇", text):
        style = "curious"
    else:
        style = "plain"
    target = {
        "plain": 1.0,
        "gentle": 1.03,
        "serious": 1.01,
        "playful": 0.98,
        "curious": 1.0,
    }[style]
    energy = (affect or {}).get("energy", 0.5)
    if type(energy) in (float, int) and math.isfinite(energy) and 0 <= energy <= 1:
        target += (0.5 - energy) * 0.01
    if type(previous_factor) not in (float, int) or not math.isfinite(previous_factor):
        previous_factor = 1.0
    factor = max(
        0.98,
        min(1.04, max(previous_factor - 0.02, min(previous_factor + 0.02, target))),
    )
    return {"style": style, "duration_factor": round(factor, 3)}
