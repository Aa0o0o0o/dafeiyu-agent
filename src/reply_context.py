"""One-hop quoted-message parsing; never follows nested replies or foreign chats."""

import asyncio
import re

from .vision import parse_message, ImageInputError


class QuoteError(ValueError):
    pass


def message_id(value):
    if (
        isinstance(value, bool)
        or not isinstance(value, (str, int))
        or not re.fullmatch(r"-?[0-9]{1,20}", str(value))
    ):
        raise QuoteError("引用标识无效，请重新引用。")
    return str(int(value))


def parse_quoted(
    data,
    expected_id,
    *,
    bot_qq,
    owner_qq,
    group_id=None,
    known_private_route=False,
    known_bot_voice_text=None,
    allow_voice_reference=False,
):
    if not isinstance(data, dict) or message_id(data.get("message_id")) != message_id(
        expected_id
    ):
        raise QuoteError("引用消息对应关系无法核验，请重新引用或重发原文。")
    sender = str(data.get("sender", {}).get("user_id", ""))
    if not re.fullmatch(r"[1-9][0-9]{4,19}", sender):
        raise QuoteError("引用发言者身份缺失，请重发原文。")
    if group_id:
        valid = (
            data.get("message_type") == "group"
            and str(data.get("group_id")) == group_id
        )
    else:
        valid = (
            data.get("message_type") == "private"
            and not data.get("group_id")
            and (
                sender == owner_qq
                or sender == bot_qq
                and (known_private_route or str(data.get("target_id", "")) == owner_qq)
            )
        )
    if not valid:
        raise QuoteError(
            "引用不属于当前会话或无法核验归属，未读取内容，请在当前会话重发。"
        )
    parts = data.get("message")
    if not isinstance(parts, list) or not 1 <= len(parts) <= 100:
        raise QuoteError("引用消息格式暂不支持，请重发文字或图片。")
    clean = [
        p for p in parts if isinstance(p, dict) and p.get("type") not in {"reply", "at"}
    ]
    verified_voice_question = bool(
        sender == bot_qq
        and isinstance(known_bot_voice_text, str)
        and 1 <= len(known_bot_voice_text) <= 400
        and any(p.get("type") == "record" for p in clean)
        and all(p.get("type") == "record" for p in clean)
    )
    voice_count = sum(p.get("type") == "record" for p in clean)
    needs_voice = bool(voice_count and not verified_voice_question)
    if voice_count > 1:
        raise QuoteError("引用含多条语音，暂无法对应转写，请单独引用一条。")
    if needs_voice and not allow_voice_reference:
        raise QuoteError("引用含语音，请使用支持语音转写的引用入口。")
    if verified_voice_question:
        clean = [{"type": "text", "data": {"text": known_bot_voice_text}}]
    elif needs_voice:
        clean = [p for p in clean if p.get("type") != "record"]
        if not clean:
            clean = [{"type": "text", "data": {"text": "[被引用的语音]"}}]
    try:
        parsed = parse_message(clean, allow_group_media=bool(group_id))
    except ImageInputError as error:
        raise QuoteError(
            "引用含不支持的内容或图片，请直接重发所需文字/图片。"
        ) from error
    name = (
        data.get("sender", {}).get("card")
        or data.get("sender", {}).get("nickname")
        or ""
    )
    return {
        "message_id": message_id(expected_id),
        "author_qq": sender,
        "author_name": name[:50] if isinstance(name, str) else "",
        "text": parsed.text,
        "images": parsed.images,
        **({"attachments": parsed.attachments} if parsed.attachments else {}),
        **({"forwards": parsed.forwards} if parsed.forwards else {}),
        **({"needs_voice_transcription": True} if needs_voice else {}),
        **(
            {
                "text_source": "verified_delivered_bot_speech_not_audio_transcription"
            }
            if verified_voice_question
            else {}
        ),
    }


async def verify_private_history_reference(fetch_history, expected_id, peer):
    """Prove a message occurs in the queried friend's history; never expose it."""
    try:
        result = await asyncio.wait_for(fetch_history(str(peer), message_id(expected_id)), 10)
        if not isinstance(result, dict) or not isinstance(result.get("messages"), list):
            return False
        for item in result["messages"]:
            if not isinstance(item, dict) or item.get("message_type") != "private" or item.get("group_id"):
                continue
            try:
                if message_id(item.get("message_id")) == message_id(expected_id):
                    return True
            except QuoteError:
                continue
    except asyncio.CancelledError:
        raise
    except Exception:
        return False
    return False


async def resolve_quoted(data, expected_id, *, transcribe, **kwargs):
    """Validate the conversation before asking QQ to transcribe its voice."""
    result = parse_quoted(data, expected_id, allow_voice_reference=True, **kwargs)
    if not result.pop("needs_voice_transcription", False):
        return result
    try:
        transcription = await asyncio.wait_for(transcribe(message_id(expected_id)), 150)
    except asyncio.CancelledError:
        raise
    except Exception as error:
        raise QuoteError("引用语音转文字失败或已过期，请重新发送语音或文字。") from error
    text = transcription.get("text") if isinstance(transcription, dict) else None
    if not isinstance(text, str) or not text.strip() or len(text) > 3000:
        raise QuoteError("引用语音没有得到有效转写，请重新发送语音或文字。")
    local = transcription.get("source") == "local_whisper"
    result["text"] = result["text"] + ("\n[本地语音自动转写，可能有误] " if local else "\n[QQ语音自动转写，可能有误] ") + text.strip()
    result["text_source"] = "local_whisper_transcription_may_be_inaccurate" if local else "qq_voice_transcription_may_be_inaccurate"
    return result
