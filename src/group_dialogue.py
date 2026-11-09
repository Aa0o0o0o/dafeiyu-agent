"""One delivered question can invite an answer without heat-based follow-up spam."""

import re
import time
from uuid import uuid4
from .memory_state import safe_text


class GroupDialogue:
    def __init__(self, seconds=120, clock=time.time):
        if type(seconds) is not int or not 10 <= seconds <= 600:
            raise ValueError("Invalid question reply window")
        self.seconds, self.clock, self.pending = seconds, clock, None

    def clear(self):
        self.pending = None

    def ask(self, text, targets, receipts):
        if (
            "```" in text
            or "http" in text
            or len(text) > 500
            or safe_text(text) is None
        ):
            return
        questions = re.findall(r"[^。！？!?\n]{1,160}[？?]", text)
        ids = {
            str(receipt)
            for kind, receipt in receipts
            if kind in {"text", "voice"} and receipt
        }
        if questions and ids:
            self.pending = {
                "key": uuid4().hex,
                "question": questions[-1].strip(),
                "targets": set(targets),
                "receipts": ids,
                "voice_receipts": {
                    str(receipt)
                    for kind, receipt in receipts
                    if kind == "voice" and receipt
                },
                "created": self.clock(),
                "probed": False,
                "queued": False,
            }

    def known_voice_question(self, receipt):
        p = self.pending
        if (
            p
            and self.clock() - p["created"] <= self.seconds
            and str(receipt) in p["voice_receipts"]
        ):
            return p["question"]
        return None

    def match(self, text, sender, parts):
        p = self.pending
        if not p:
            return None
        if self.clock() - p["created"] > self.seconds:
            self.clear()
            return None
        if p["queued"]:
            return None
        quoted = {
            str(x.get("data", {}).get("id", ""))
            for x in parts
            if isinstance(x, dict) and x.get("type") == "reply"
        }
        confidence = "quoted" if quoted & p["receipts"] else None
        if not confidence and not p["probed"] and 1 <= len(text.strip()) <= 240:
            if not re.fullmatch(r"[哈啊嗯哦~～！!。\s]+|收到|路过", text.strip()):
                if sender in p["targets"]:
                    confidence = "speaker"
                elif re.search(
                    r"^(?:是的|不是|有啊|有的|没有|会啊|不会|我觉得|我认为|我用|我选|我更|我不|我喜欢|应该|因为|可以|不可以|都可以|不行|当然|对呀|对啊|不对|答案是|选[ABCD一二三四])",
                    text.strip(),
                ):
                    confidence = "candidate"
                else:
                    terms = {
                        p["question"][i : i + 2]
                        for i in range(len(p["question"]) - 1)
                        if re.fullmatch(r"[\u4e00-\u9fff]{2}", p["question"][i : i + 2])
                    }
                    words = {
                        word.casefold()
                        for word in re.findall(
                            r"[A-Za-z][A-Za-z0-9_+.#-]{1,30}", p["question"]
                        )
                    }
                    answer_words = {
                        word.casefold()
                        for word in re.findall(r"[A-Za-z][A-Za-z0-9_+.#-]{1,30}", text)
                    }
                    brief = text.strip(" 。！!?？~～")
                    short_option = (
                        1 <= len(brief) <= 4
                        and brief in p["question"]
                        and brief
                        not in {"你", "我", "是", "吗", "呢", "么", "什么", "大肥鱼"}
                    )
                    if (
                        sum(term in text for term in terms) >= 2
                        or words & answer_words
                        or short_option
                    ):
                        confidence = "candidate"
        if not confidence:
            return None
        return {"key": p["key"], "question": p["question"], "confidence": confidence}

    def queued(self, answer):
        if self.pending and answer and self.pending["key"] == answer["key"]:
            self.pending["queued"] = True
            self.pending["probed"] = True

    def finish(self, answer, delivered):
        if self.pending and answer and self.pending["key"] == answer["key"]:
            if delivered:
                self.clear()
            else:
                self.pending["queued"] = False
