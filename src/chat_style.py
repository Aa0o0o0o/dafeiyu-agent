"""Chat voice loading and lossless, bounded QQ message bubbles."""

import re
import random
from pathlib import Path


def load_chat_style() -> str:
    return (
        (Path(__file__).resolve().parents[1] / "prompts/DAFEIYU_CHAT_STYLE.md")
        .read_text(encoding="utf-8")
        .strip()
    )


ROLE_SECTION = "【角色扮演指令】"
ROLE_KEYWORDS = ("妈妈", "妈咪", "主人", "主任", "女主人", "女王", "女上司")


COMPAT_BLOCK = re.compile(
    r"(?ms)^NSFW\{[^{}]*\}[ \t]*\r?\nWord_Block[ \t]*\{[^{}]*\}[ \t]*\r?\nCore_Truth[ \t]*\{[^{}]*\}[ \t]*(?:\r?\n|$)"
)


def style_for_message(style: str, incoming: str, *, allow_compat: bool = False) -> str:
    """Choose the optional persona and restrict the separately approved block."""
    blocks = list(COMPAT_BLOCK.finditer(style))
    # Ambiguous duplicate blocks are never injected.
    compat = blocks[0].group().strip() if len(blocks) == 1 else ""
    style = COMPAT_BLOCK.sub("", style)
    # Refuse to pass partially edited control blocks into any conversation.
    malformed = re.search(r"(?:NSFW|Word_Block|Core_Truth)[ \t]*\{", style)
    if malformed:
        return style[:malformed.start()].partition(ROLE_SECTION)[0].rstrip()
    # A role subsection can be restricted to a verified owner private turn.
    # A malformed marker fails closed rather than leaking the subsection.
    start, end = "【主人私聊设定】", "【/主人私聊设定】"
    if style.count(start) != style.count(end) or style.count(start) > 1:
        return style.split(ROLE_SECTION, 1)[0].split(start, 1)[0].rstrip()
    if not allow_compat and start in style:
        before, _, tail = style.partition(start)
        _, _, after = tail.partition(end)
        style = before + after
    ordinary, found, role = style.partition(ROLE_SECTION)
    if not found:
        return style
    if not role.strip():
        return ordinary.rstrip()
    if isinstance(incoming, str) and any(word in incoming for word in ROLE_KEYWORDS):
        selected = (ordinary.rstrip() + "\n\n本轮包含角色扮演相关词语，以下设定的性格与语气优先；"
                    "实际身份、权限和事实边界仍以程序信息为准。\n" + ROLE_SECTION + role)
        return compat + "\n\n" + selected if allow_compat and compat else selected
    return ordinary.rstrip()


def reply_bubbles(text: str, conversational: bool) -> list[str]:
    text = text.replace("\r\n", "\n").strip("\n\r")
    if not text.strip():
        return []
    # Long/structured answers keep their original layout and complete content.
    structured = "```" in text or re.search(
        r"(?m)^\s*(?:[-*] |\d+[.)、] |#{1,6} )", text
    )
    if not conversational or len(text) > 900 or structured:
        return [text[i : i + 3000] for i in range(0, len(text), 3000)]
    paragraphs = re.split(r"\n[ \t]*\n+", text)
    if len(paragraphs) > 1:
        pieces = [piece.strip() for piece in paragraphs if piece.strip()]
        return pieces if len(pieces) <= 4 else pieces[:3] + ["\n".join(pieces[3:])]
    pieces = [text]
    if len(pieces) == 1 and len(text) > 100:
        pieces = re.split(r"(?<=[。！？!?])(?=[^”’\n])", text)
    bubbles, current = [], ""
    for piece in pieces:
        if not piece:
            continue
        if current and len(current) + len(piece) > 100 and len(bubbles) < 3:
            bubbles.append(current)
            current = ""
        current += piece
    if current:
        bubbles.append(current)
    # Layout separators belong between bubbles, never at a bubble's outer edges.
    return bubbles or [text]


def typing_delay(text: str, *, first: bool, elapsed: float = 0) -> float:
    """Bounded display pacing; generation time already counts toward first wait."""
    desired = min(7.0, 1.3 + len(text) / 12) * random.uniform(0.9, 1.1)
    # Even a long model generation retains a small visible pause before sending.
    return max(1.1, desired - max(0, elapsed)) if first else max(1.3, desired)


def casual_media_context(text):
    return (
        bool(text.strip())
        and len(text) <= 120
        and not re.search(
            r"```|https?://|代码|公式|表格|详细|完整|严肃|正经|论文|报告|教程|分析|急救|报警|不要.{0,5}表情|别.{0,5}表情|只(?:用|发)?文字|用文字(?:回复|回答)",
            text,
        )
    )
