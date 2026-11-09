"""Deterministic speech surface; never paraphrase facts or truncate silently."""

import re
from ..memory_state import safe_text
from ..message_surface import strip_delivery_annotations


def spoken_text(text, maximum=200):
    text = strip_delivery_annotations(text)
    if (
        not text
        or safe_text(text) is None
        or re.search(
            r"```|https?://|file:|[A-Za-z]:[\\/]|\[\[|<[^>]+>|\|.*\||[$=]", text
        )
    ):
        raise ValueError("text_only_content")
    result = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    result = (
        re.sub(r"(?m)^\s{0,3}#{1,6}\s+", "", result).replace("**", "").replace("__", "")
    )
    result = re.sub(r"[\U0001F000-\U0001FAFF\u2600-\u27BF]", "", result).strip()
    if not result or len(result) > maximum:
        raise ValueError("speech_too_long")
    return result


def voice_refused(text):
    return bool(
        re.search(
            r"不要.{0,5}语音|不用.{0,5}语音|别.{0,5}语音|只(?:用|发)?文字|用文字(?:回复|回答)|不方便听",
            text,
        )
    )


def voice_requested(text):
    if voice_refused(text):
        return False
    return bool(
        re.search(
            r"用语音|发(?:一条|个)?语音|语音(?:说|回复|回答|读)|朗读|念给我听", text
        )
    )
