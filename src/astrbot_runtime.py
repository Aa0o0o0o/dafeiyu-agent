"""Pinned AstrBot 4.29.3 adapters, without loading its agent/tool/plugin pipeline."""

import asyncio
import hmac
import json
import os
from pathlib import Path

from .contracts import ModelResult, WebCall
from .conversation import ConversationService, qq_id, validate_settings
from .web_tools import DeepSeekWebTools, WEB_TOOLS
from .reply_context import resolve_quoted, QuoteError
from .social_events import PokeGate, mention_segments
from .qq_profiles import MemberProfiles, ProfileError
from .stickers import StickerLibrary
from .contact_access import ContactDirectory, PrivateChannel
from .qq_favorites import QQFavorites
from .browser.broker import BrowserBroker
from .tool_catalog import MODEL_TOOLS, MODEL_TOOL_NAMES
from .capabilities import capability_manifest
from .qq_expressions import native_expression
from .memory_v2.astrbot_bridge import verify_delivery


class AstrBotModel:
    def __init__(self, config: dict, key: str):
        from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial

        validate_settings(config)
        self.config = config
        self.provider = ProviderOpenAIOfficial(
            {
                "id": "dafeiyu-deepseek",
                "type": "openai_chat_completion",
                "key": [key],
                "api_base": config["api_base"],
                "model": config["model"],
                "timeout": config["timeout_seconds"],
                "custom_extra_body": {
                    "thinking": {"type": config["thinking"]},
                    "reasoning_effort": config["reasoning_effort"],
                },
            },
            {},
        )

    async def complete_memory(self, messages, *, max_tokens=1200):
        return await self.complete(messages, allow_tools=False, max_tokens=max_tokens)

    async def complete(
        self,
        messages: list[dict],
        *,
        allow_tools: bool = True,
        tool_names: tuple[str, ...] | None = None,
        max_tokens: int | None = None,
    ) -> ModelResult:
        if self.config.get("web_enabled") or tool_names:
            options = {}
            if allow_tools:
                available = MODEL_TOOLS
                names = (
                    tool_names
                    if tool_names is not None
                    else tuple(t["function"]["name"] for t in WEB_TOOLS)
                )
                if set(names) - {t["function"]["name"] for t in available}:
                    raise ValueError("Unsupported tool selection")
                options.update(
                    tools=[t for t in available if t["function"]["name"] in names],
                    tool_choice="auto",
                    parallel_tool_calls=False,
                )
            completion = await self.provider.client.with_options(
                max_retries=0
            ).chat.completions.create(
                model=self.config["model"],
                messages=messages,
                max_tokens=max_tokens or self.config["max_output_tokens"],
                extra_body={
                    "thinking": {"type": self.config["thinking"]},
                    "reasoning_effort": self.config["reasoning_effort"],
                },
                **options,
            )
            if (
                not completion.choices
                or completion.choices[0].message.role != "assistant"
            ):
                raise ValueError("Unexpected provider response")
            message = completion.choices[0].message
            calls = tuple(
                WebCall(c.id, c.function.name, c.function.arguments)
                for c in message.tool_calls or []
            )
            continuation = {"role": "assistant", "content": message.content}
            if calls:
                continuation["tool_calls"] = [
                    c.model_dump(exclude_none=True) for c in message.tool_calls
                ]
                reasoning = getattr(message, "reasoning_content", None)
                if reasoning is not None:
                    continuation["reasoning_content"] = reasoning
            self.last_response_model = completion.model
            usage = completion.usage
            return ModelResult(
                message.content or "",
                usage.prompt_tokens if usage else 0,
                usage.completion_tokens if usage else 0,
                completion.model,
                calls,
                continuation if calls else None,
            )
        # _query is a pinned compatibility seam: bypass text_chat's adaptive ten-pass
        # retry loop; no ToolSet, fallback key, search or agent runner is instantiated.
        result = await self.provider._query(
            {
                "model": self.config["model"],
                "messages": messages,
                "max_tokens": max_tokens or self.config["max_output_tokens"],
            },
            None,
            request_max_retries=1,
        )
        if result.tools_call_name or result.role != "assistant":
            raise ValueError("Unexpected tool response")
        usage = result.usage
        self.last_response_model = getattr(result.raw_completion, "model", "")
        return ModelResult(
            result.completion_text,
            usage.input if usage else 0,
            usage.output if usage else 0,
            self.last_response_model,
        )

    async def close(self):
        await self.provider.client.close()


def build_adapter(config: dict, token: str, model, root: Path, web_tools=None):
    from .group_chat import GroupConversation, GroupChannel
    from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_platform_adapter import (
        AiocqhttpAdapter,
    )
    from quart import abort, websocket

    validate_settings(config)
    if not isinstance(token, str) or len(token) < 32:
        raise ValueError("A random OneBot token of at least 32 characters is required")

    class OwnerOnlyAdapter(AiocqhttpAdapter):
        def __init__(self):
            super().__init__(
                {
                    "id": config["instance_id"],
                    "ws_reverse_host": config["host"],
                    "ws_reverse_port": config["port"],
                    "ws_reverse_token": token,
                },
                {},
                asyncio.Queue(),
            )
            self.shutdown_event = asyncio.Event()
            self.connection = None
            self.login_verified = False
            self.poke_gate = PokeGate(config)
            self.contacts = ContactDirectory(config, self.read_contact_action)
            self.memory_ui_services = {}
            self.bot._api._wsr_api._timeout_sec = 10
            self.service = ConversationService(
                config,
                root / "runtime/state.sqlite3",
                model,
                self,
                web_tools=web_tools,
            )
            self.privates = {config["owner_qq"]: self.service}
            self.groups = {
                gid: GroupConversation(
                    config,
                    root / "runtime/state.sqlite3",
                    model,
                    GroupChannel(self, gid),
                    gid,
                    web_tools=web_tools,
                )
                for gid in config.get("group_ids", [])
            }
            self.stickers = (
                StickerLibrary(
                    root / "runtime/stickers",
                    memory_options=config.get("sticker_memory", {}),
                    account_scope=config["bot_qq"]
                    if config.get("sticker_account_shared", False)
                    else None,
                )
                if config.get("sticker_enabled", False)
                else None
            )
            self.favorites = (
                QQFavorites(self.stickers, self.favorite_action, config["bot_qq"])
                if self.stickers and config.get("sticker_qq_favorites", False)
                else None
            )
            self.browser = (
                BrowserBroker(root) if config.get("browser_enabled", False) else None
            )
            for conversation in self.conversations():
                conversation.browser_tools = self.browser
            self.service.sticker_library = self.stickers
            for group in self.groups.values():
                group.sticker_library = self.stickers
            if config.get("profile_enabled", False):
                self.service.member_tools = MemberProfiles(
                    self.read_profile_action, config
                )
                for gid, group in self.groups.items():
                    group.member_tools = MemberProfiles(
                        self.read_profile_action, config, gid
                    )

            @self.bot.server_app.before_request
            async def no_http_events():
                # aiocqhttp also provides an HTTP event endpoint; explicitly disable it.
                abort(405)

            @self.bot.server_app.before_websocket
            async def only_bound_universal_socket():
                auth = websocket.headers.get("Authorization", "")
                expected = "Bearer " + token
                if not hmac.compare_digest(auth, expected):
                    abort(403)
                if (
                    websocket.path != "/ws"
                    or websocket.headers.get("X-Client-Role", "").lower() != "universal"
                ):
                    abort(403)
                if qq_id(websocket.headers.get("X-Self-ID")) != config["bot_qq"]:
                    abort(403)
                if self.connection is not None:
                    abort(409)
                self.connection = websocket._get_current_object()
                self.login_verified = False

            @self.bot.server_app.after_websocket
            async def disconnected(response):
                if self.connection is websocket._get_current_object():
                    self.connection = None
                    self.login_verified = False
                    self.service.verified = False
                    for conversation in self.conversations():
                        await conversation.cancel()
                return response

        def conversations(self):
            return [*self.privates.values(), *self.groups.values()]

        async def favorite_action(self, action, **params):
            if (
                action not in {"add_custom_face", "fetch_custom_face_detail"}
                or not await self.checked_login()
            ):
                raise ConnectionError("Favorite account not verified")
            if action == "add_custom_face":
                if set(params) != {"file"}:
                    raise ValueError("Invalid favorite fields")
                path = Path(params["file"])
                if (
                    not path.is_file()
                    or path.is_symlink()
                    or not path.resolve().is_relative_to(
                        (root / "runtime/stickers").resolve()
                    )
                ):
                    raise ValueError("Favorite file outside verified library")
                # Resolve by indexed hash; the model never supplies a file path.
                match = self.stickers.db.execute(
                    "SELECT id FROM stickers WHERE filename=? AND group_id=?",
                    (
                        str(path.relative_to(self.stickers.root)).replace("\\", "/"),
                        config["bot_qq"],
                    ),
                ).fetchone()
                if (
                    not match
                    or self.stickers.resolve(match[0], [config["bot_qq"]])[0] != path
                ):
                    raise ValueError("Favorite integrity mismatch")
            elif (
                set(params) != {"count"}
                or not isinstance(params["count"], int)
                or not 1 <= params["count"] <= 6400
            ):
                raise ValueError("Invalid favorite read bound")
            return await self.bot.call_action(
                action, self_id=config["bot_qq"], **params
            )

        async def read_contact_action(self, action):
            if (
                action not in {"get_friend_list", "get_group_list"}
                or not await self.checked_login()
            ):
                raise ConnectionError("Contact list account not verified")
            return await self.bot.call_action(action, self_id=config["bot_qq"])

        async def target_for(self, event):
            kind = event.get("message_type")
            if kind == "group":
                target = qq_id(event.get("group_id"))
                if not await self.contacts.allowed("group", target):
                    return None
                if target not in self.groups:
                    group_config = dict(
                        config, group_ids=[*config.get("group_ids", []), target]
                    )
                    # Broadening chat access does not enable random chatter in new groups.
                    if target not in config.get("group_ids", []):
                        group_config["group_random_probability"] = 0
                    group = GroupConversation(
                        group_config,
                        root / "runtime/state.sqlite3",
                        model,
                        GroupChannel(self, target),
                        target,
                        web_tools=web_tools,
                    )
                    group.sticker_library = self.stickers
                    group.browser_tools = self.browser
                    if config.get("profile_enabled", False):
                        group.member_tools = MemberProfiles(
                            self.read_profile_action, config, target
                        )
                    self.groups[target] = group
                    if group.v2:
                        self.memory_ui_services[group.scope] = group.v2
                return self.groups[target]
            if kind == "private":
                peer = qq_id(event.get("user_id"))
                if event.get("sub_type") != "friend" or not await self.contacts.allowed(
                    "private", peer
                ):
                    return None
                if peer not in self.privates:
                    owner_scope = f"{config['instance_id']}:{config['bot_qq']}:{config['owner_qq']}"
                    service = ConversationService(
                        config,
                        root / "runtime/state.sqlite3",
                        model,
                        PrivateChannel(self, peer),
                        peer_qq=peer,
                        budget_scope=owner_scope,
                        web_tools=web_tools,
                    )
                    service.sticker_library = self.stickers
                    service.browser_tools = self.browser
                    if config.get("profile_enabled", False):
                        service.member_tools = MemberProfiles(
                            self.read_profile_action, config, peer_qq=peer
                        )
                    self.privates[peer] = service
                return self.privates[peer]
            return None

        async def checked_login(self):
            conn = self.bot._wsr_api_clients.get(config["bot_qq"])
            if conn is None or conn is not self.connection:
                self.login_verified = False
                return False
            try:
                info = await self.bot.call_action(
                    "get_login_info", self_id=config["bot_qq"]
                )
                self.login_verified = (
                    qq_id(info.get("user_id")) == config["bot_qq"]
                    and conn is self.connection
                )
            except Exception:
                self.login_verified = False
            return self.login_verified

        async def convert_message(self, event):
            try:
                if (
                    websocket._get_current_object() is not self.connection
                    or not await self.checked_login()
                ):
                    return None
                if event.get("post_type") == "notice" and event.get("notice_type") in {
                    "group_recall",
                    "friend_recall",
                }:
                    probe = dict(
                        event,
                        message_type="group"
                        if event["notice_type"] == "group_recall"
                        else "private",
                        sub_type="friend",
                    )
                    target = await self.target_for(probe)
                    if target and target.memory_bridge:
                        await target.memory_bridge.retract_notice(event)
                    return None
                if event.get("post_type") == "notice" and event.get("notice_type") == "group_upload":
                    target = await self.target_for(dict(event, message_type="group"))
                    if target:
                        target.note_group_upload(event)
                    return None
                is_poke = event.get("post_type") == "notice"
                if is_poke:
                    event = self.poke_gate.convert(event)
                    if event is None:
                        return None
                target = await self.target_for(event)
                if target and target.accepts(event):
                    await target.receive(event, config["bot_qq"], program_event=is_poke)
            except Exception:
                self.service.last_error = "入站消息处理异常，已拒绝"
            return None

        async def send_text(self, text: str) -> str:
            return await self.send_private_text(config["owner_qq"], text)

        async def send_private_text(self, peer, text):
            if (
                peer not in self.privates
                or not await self.checked_login()
                or not await self.contacts.allowed("private", peer)
            ):
                raise ConnectionError("Private recipient not verified")
            verify_delivery()
            result = await self.bot.call_action(
                "send_private_msg",
                self_id=config["bot_qq"],
                user_id=int(peer),
                message=[{"type": "text", "data": {"text": text}}],
            )
            receipt = (
                str(result["message_id"])
                if isinstance(result, dict) and result.get("message_id") is not None
                else ""
            )
            if receipt:
                self.privates[peer].store.note_route(receipt)
            return receipt

        async def read_forward(self, forward_id, *, group_id=None, peer_qq=None):
            peer = peer_qq or config["owner_qq"]
            if not await self.checked_login() or not await self.contacts.allowed(
                "group" if group_id else "private", group_id or peer
            ) or (group_id is not None and group_id not in self.groups) or (
                group_id is None and peer not in self.privates
            ):
                raise ValueError("合并转发会话未通过核验。")
            from .forward_context import read_forward
            return await read_forward(self.bot.call_action, config["bot_qq"], forward_id)

        async def fetch_quoted(self, message_id, *, group_id=None, peer_qq=None):
            if group_id is not None and group_id not in self.groups:
                raise QuoteError("引用群未授权。")
            if not await self.checked_login():
                raise QuoteError("QQ连接未核验，暂时无法读取引用。")
            try:
                data = await self.bot.call_action(
                    "get_msg", self_id=config["bot_qq"], message_id=int(message_id)
                )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                raise QuoteError(
                    "引用已撤回、过期或暂时取不到，请重发原文/图片。"
                ) from error
            peer = peer_qq or config["owner_qq"]
            if group_id is None and (
                peer not in self.privates
                or not await self.contacts.allowed("private", peer)
            ):
                raise QuoteError("私聊引用路由未授权。")
            async def transcribe_quoted_voice(verified_message_id):
                try:
                    response = await self.bot.call_action(
                        "fetch_ptt_text", self_id=config["bot_qq"],
                        message_id=int(verified_message_id),
                    )
                    if isinstance(response, dict) and isinstance(response.get("text"), str) and response["text"].strip():
                        return response
                except asyncio.CancelledError:
                    raise
                except Exception:
                    pass
                # Only use the record token from the already verified message;
                # never accept a user/model supplied path or URL.
                from .voice.local_asr import transcribe_wav, MAX_AUDIO_BYTES
                import base64
                import re
                model = root / "tools/voice/asr-models/small.pt"
                records = [p for p in data.get("message", [])
                           if isinstance(p, dict) and p.get("type") == "record"]
                if len(records) != 1 or not model.is_file():
                    raise ValueError("local ASR unavailable")
                record_data = records[0].get("data", {})
                token = record_data.get("file") if isinstance(record_data, dict) else None
                if (not isinstance(token, str) or not 1 <= len(token) <= 2048
                        or not re.fullmatch(r"[A-Za-z0-9_+./=-]+", token)
                        or ".." in token or token.startswith(("/", "."))):
                    raise ValueError("invalid record token")
                result = await self.bot.call_action("get_record", self_id=config["bot_qq"],
                                                    file=token, out_format="wav")
                encoded = result.get("base64") if isinstance(result, dict) else None
                if not isinstance(encoded, str) or len(encoded) > (MAX_AUDIO_BYTES * 4 // 3 + 8):
                    raise ValueError("invalid audio")
                audio = base64.b64decode(encoded, validate=True)
                return await transcribe_wav(audio, root)

            # QQ get_msg does not always provide a target_id, especially for
            # older bot messages. Verify an exact match in this friend's own QQ
            # history before accepting a missing local delivery receipt.
            route_known = (self.privates[peer].store.known_route(message_id)
                           if group_id is None else False)
            if (group_id is None and not route_known and isinstance(data, dict)
                    and data.get("message_type") == "private" and not data.get("group_id")
                    and str(data.get("sender", {}).get("user_id", "")) == config["bot_qq"]
                    and str(data.get("target_id", "")) != peer):
                from .reply_context import verify_private_history_reference
                async def fetch_peer_history(friend, anchor):
                    return await self.bot.call_action(
                        "get_friend_msg_history", self_id=config["bot_qq"],
                        user_id=friend, message_seq=anchor, count=20,
                        reverse_order=False, disable_get_url=True, parse_mult_msg=False)
                route_known = await verify_private_history_reference(fetch_peer_history, message_id, peer)
            return await resolve_quoted(
                data,
                message_id,
                transcribe=transcribe_quoted_voice,
                bot_qq=config["bot_qq"],
                owner_qq=peer,
                group_id=group_id,
                known_private_route=route_known,
                known_bot_voice_text=(
                    self.groups[group_id].voice.known_quote(message_id)
                    if group_id and self.groups[group_id].voice
                    else self.privates[peer].voice.known_quote(message_id)
                    if not group_id and self.privates[peer].voice
                    else None
                ) or (
                    self.groups[group_id].dialogue.known_voice_question(message_id)
                    if group_id else None
                ),
            )

        async def read_group_media(self, group_id, message_id, expected_sender):
            """Re-fetch and bind every attachment to its QQ group and sender."""
            import base64
            import re
            from .group_media import MAX_FILE, MAX_VIDEO, extract_document, extract_video
            from .vision import ImageInputError
            from .reply_context import message_id as checked_message_id
            if (group_id not in self.groups or not await self.checked_login()
                    or not await self.contacts.allowed("group", group_id)):
                raise ImageInputError("群视频或文件的会话未通过核验。")
            mid = checked_message_id(message_id)
            try:
                msg = await self.bot.call_action("get_msg", self_id=config["bot_qq"], message_id=int(mid))
            except Exception as error:
                raise ImageInputError("群消息已过期或无法读取，请重新发送。") from error
            if (not isinstance(msg, dict) or checked_message_id(msg.get("message_id")) != mid
                    or msg.get("message_type") != "group" or str(msg.get("group_id")) != group_id
                    or str(msg.get("sender", {}).get("user_id", "")) != str(expected_sender)):
                raise ImageInputError("群文件来源与当前会话不符。")
            parts=msg.get("message")
            if not isinstance(parts,list): raise ImageInputError("群文件消息格式无效。")
            attachments=[p for p in parts if isinstance(p,dict) and p.get("type") in {"file","video"}]
            if not 1<=len(attachments)<=3: raise ImageInputError("群消息中没有可处理的文件或视频。")
            return await self._read_verified_group_parts(attachments)

        async def read_group_upload(self, group_id, upload_id):
            from .vision import ImageInputError
            if (group_id not in self.groups or not await self.checked_login()
                    or not await self.contacts.allowed("group", group_id)):
                raise ImageInputError("群文件的会话未通过核验。")
            upload=self.groups[group_id].known_group_upload(upload_id)
            if not upload: raise ImageInputError("群文件记录已过期，请重新发送。")
            part={"type":"file","data":{"file_id":upload["id"],"file":upload["name"],"file_size":upload["size"]}}
            return await self._read_verified_group_parts([part])

        async def _read_verified_group_parts(self, attachments):
            import base64
            import re
            from .group_media import MAX_FILE, MAX_VIDEO, extract_document, extract_video
            from .vision import ImageInputError
            output=[]
            for segment in attachments:
                data=segment.get("data")
                if not isinstance(data,dict): raise ImageInputError("群文件资料不完整。")
                kind=segment["type"]
                upload_name=data.get("file", "")
                video_upload = (kind == "file" and isinstance(upload_name,str)
                                and Path(upload_name).suffix.lower() in {".mp4",".mov",".webm",".mkv",".avi"})
                limit=MAX_VIDEO if kind=="video" or video_upload else MAX_FILE
                size=str(data.get("file_size", ""))
                if not size.isdecimal() or not 0<int(size)<=limit:
                    raise ImageInputError("群文件大小未知或超出处理范围，请压缩后重发。")
                token=data.get("file") if kind=="video" else (data.get("file_id") or data.get("file"))
                if (not isinstance(token,str) or not 1<=len(token)<=2048
                        or not re.fullmatch(r"[A-Za-z0-9_+./=\-]+",token)
                        or ".." in token or token.startswith(("/","."))):
                    raise ImageInputError("群文件标识无效，无法读取。")
                try:
                    result=await self.bot.call_action("get_file",self_id=config["bot_qq"],file=token)
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    raise ImageInputError("群文件已过期或下载失败，请重新发送。") from error
                if not isinstance(result,dict): raise ImageInputError("群文件下载结果无效。")
                encoded=result.get("base64")
                if isinstance(encoded,str) and encoded:
                    if len(encoded)>limit*4//3+8: raise ImageInputError("群文件超过大小限制。")
                    try: raw=base64.b64decode(encoded,validate=True)
                    except ValueError as error: raise ImageInputError("群文件下载结果无效。") from error
                else:
                    name=result.get("file")
                    if not isinstance(name,str) or len(name)>4096: raise ImageInputError("群文件下载结果无效。")
                    path=Path(name)
                    if not path.is_absolute() or path.is_symlink() or not path.is_file():
                        raise ImageInputError("群文件下载位置不可用。")
                    resolved=path.resolve(strict=True)
                    permitted=[(root/"runtime/qq-data").resolve(),(root/"runtime/qq-profile").resolve(),
                               (root/"runtime/napcat/cache").resolve(),(root/".tmp").resolve()]
                    if not any(resolved.is_relative_to(base) for base in permitted) or path.stat().st_size>limit:
                        raise ImageInputError("群文件不在专用QQ允许的缓存目录或过大。")
                    raw=await asyncio.to_thread(path.read_bytes)
                if not raw or len(raw)>limit: raise ImageInputError("群文件为空或超出大小限制。")
                if kind=="video" or video_upload:
                    output.append(await extract_video(raw,root))
                else:
                    name=data.get("file", "")
                    if not isinstance(name,str) or not name or len(name)>255 or '/' in name or chr(92) in name:
                        raise ImageInputError("群文件名称无效。")
                    output.append(await extract_document(raw,name,root))
            return output

        async def read_profile_action(self, action, **params):
            if action not in {
                "get_group_member_info",
                "get_stranger_info",
                "get_cookies",
            }:
                raise ProfileError("资料读取动作未授权。")
            if action == "get_cookies" and params != {"domain": "user.qzone.qq.com"}:
                raise ProfileError("只允许QQ空间的当前账号登录态。")
            if not await self.checked_login():
                raise ProfileError("QQ账号尚未核验。")
            return await self.bot.call_action(
                action, self_id=config["bot_qq"], **params
            )

        def run(self):
            return self.bot.run_task(
                host=config["host"],
                port=config["port"],
                shutdown_trigger=self.shutdown_event.wait,
            )

        async def send_group_text(self, group_id: str, text: str) -> str:
            if (
                group_id not in self.groups
                or not await self.checked_login()
                or not await self.contacts.allowed("group", group_id)
            ):
                raise ConnectionError("Group or account not verified")
            verify_delivery()
            result = await self.bot.call_action(
                "send_group_msg",
                self_id=config["bot_qq"],
                group_id=int(group_id),
                message=mention_segments(
                    text, self.groups[group_id].mentionable_members()
                ),
            )
            return (
                str(result["message_id"])
                if isinstance(result, dict) and result.get("message_id") is not None
                else ""
            )

        async def send_browser_artifact(self, artifact, *, group_id=None, peer_qq=None):
            import hashlib

            peer = peer_qq or config["owner_qq"]
            if not await self.checked_login() or not await self.contacts.allowed(
                "group" if group_id else "private", group_id or peer
            ):
                raise ConnectionError("Browser output destination not verified")
            if (
                group_id
                and group_id not in self.groups
                or not group_id
                and peer not in self.privates
            ):
                raise ValueError("Unknown browser output route")
            path = Path(artifact.get("path", ""))
            if (
                not path.is_file()
                or path.is_symlink()
                or not path.resolve().is_relative_to(
                    (root / "runtime/browser/artifacts").resolve()
                )
                or path.stat().st_size > 32 * 1024 * 1024
                or hashlib.sha256(path.read_bytes()).hexdigest()
                != artifact.get("sha256")
            ):
                raise ValueError("Browser artifact not verified")
            verify_delivery()
            destination = (
                {"group_id": int(group_id)} if group_id else {"user_id": int(peer)}
            )
            if artifact["kind"] == "image":
                result = await self.bot.call_action(
                    "send_group_msg" if group_id else "send_private_msg",
                    self_id=config["bot_qq"],
                    message=[
                        {
                            "type": "image",
                            "data": {"file": path.as_uri(), "summary": "[浏览器截图]"},
                        }
                    ],
                    **destination,
                )
                receipt = (
                    str(result.get("message_id", ""))
                    if isinstance(result, dict)
                    else ""
                )
                if receipt and not group_id:
                    self.privates[peer].store.note_route(receipt)
                return receipt
            result = await self.bot.call_action(
                "upload_group_file" if group_id else "upload_private_file",
                self_id=config["bot_qq"],
                file=str(path),
                name="dafeiyu-" + artifact["id"][:12] + path.suffix,
                **destination,
            )
            return (
                str(result.get("file_id") or result.get("message_id") or "")
                if isinstance(result, dict)
                else ""
            )

        async def send_sticker(self, token, *, group_id=None, peer_qq=None):
            if (
                self.stickers is None
                or group_id is not None
                and group_id not in self.groups
                or not await self.checked_login()
            ):
                raise ConnectionError("Sticker destination not authorized")
            peer = peer_qq or config["owner_qq"]
            if not await self.contacts.allowed(
                "group" if group_id else "private", group_id or peer
            ):
                raise ConnectionError("Sticker recipient not verified")
            if not group_id and peer not in self.privates:
                raise ConnectionError("Unknown private route")
            scopes = (
                [config["bot_qq"]]
                if config.get("sticker_account_shared", False)
                else ([group_id] if group_id else config.get("group_ids", []))
            )
            path, _ = self.stickers.resolve(token, scopes)
            native = self.stickers.native_segment(token, scopes)
            params = {
                "message": [
                    native
                    or {
                        "type": "image",
                        "data": {
                            "file": path.as_uri(),
                            "sub_type": 1,
                            "summary": "[表情包]",
                        },
                    }
                ]
            }
            if group_id:
                params["group_id"] = int(group_id)
            else:
                params["user_id"] = int(peer)
            verify_delivery()
            result = await self.bot.call_action(
                "send_group_msg" if group_id else "send_private_msg",
                self_id=config["bot_qq"],
                **params,
            )
            receipt = (
                str(result.get("message_id", "")) if isinstance(result, dict) else ""
            )
            if receipt and not group_id:
                self.privates[peer].store.note_route(receipt)
            return receipt

        async def send_expression(self, kind, value="", *, group_id=None, peer_qq=None):
            segment = native_expression(kind, value)
            peer = peer_qq or config["owner_qq"]
            if not config.get("sticker_enabled") or not await self.checked_login():
                raise ConnectionError("QQ expression account unavailable")
            if (
                group_id is not None
                and group_id not in self.groups
                or group_id is None
                and peer not in self.privates
                or not await self.contacts.allowed(
                    "group" if group_id else "private", group_id or peer
                )
            ):
                raise ConnectionError("QQ expression destination not authorized")
            params = {"message": [segment]}
            params["group_id" if group_id else "user_id"] = int(group_id or peer)
            verify_delivery()
            result = await self.bot.call_action(
                "send_group_msg" if group_id else "send_private_msg",
                self_id=config["bot_qq"],
                **params,
            )
            receipt = (
                str(result.get("message_id", "")) if isinstance(result, dict) else ""
            )
            if receipt and not group_id:
                self.privates[peer].store.note_route(receipt)
            return receipt

        async def send_record(self, asset, *, group_id=None, peer_qq=None):
            import hashlib
            from .voice.media import validate_wav

            peer = peer_qq or config["owner_qq"]
            service = self.groups.get(group_id) if group_id else self.privates.get(peer)
            if (
                service is None
                or not service.voice
                or not await self.checked_login()
                or not await self.contacts.allowed(
                    "group" if group_id else "private", group_id or peer
                )
            ):
                raise ConnectionError("Voice destination not verified")
            path = Path(asset["path"])
            if (
                path.parent.resolve() != service.voice.directory.resolve()
                or path.is_symlink()
            ):
                raise ValueError("Voice path outside scope")
            raw = path.read_bytes()
            validate_wav(raw)
            if hashlib.sha256(raw).hexdigest() != asset["sha256"]:
                raise ValueError("Voice integrity mismatch")
            row = service.store.db.execute(
                "SELECT state,deadline,actor,snapshot FROM voice_jobs WHERE id=? AND scope=?",
                (asset["job"], service.scope),
            ).fetchone()
            import time

            if (
                not row
                or row[0] != "ready"
                or row[1] < time.time()
                or not service.voice.preferences(row[2])[0]
            ):
                raise asyncio.CancelledError()
            if service.v2 and not service.v2.unchanged(json.loads(row[3])):
                raise asyncio.CancelledError()
            verify_delivery()
            with service.store.db:
                service.store.db.execute(
                    "UPDATE voice_jobs SET state='delivering' WHERE id=?",
                    (asset["job"],),
                )
            result = await self.bot.call_action(
                "send_group_msg" if group_id else "send_private_msg",
                self_id=config["bot_qq"],
                **{
                    "group_id" if group_id else "user_id": int(group_id or peer),
                    "message": [{"type": "record", "data": {"file": path.as_uri()}}],
                },
            )
            receipt = (
                str(result.get("message_id", "")) if isinstance(result, dict) else ""
            )
            if receipt and not group_id:
                service.store.note_route(receipt)
            return receipt

        async def terminate(self):
            for conversation in self.conversations():
                await conversation.close()
            if self.browser:
                await self.browser.close()
            if self.favorites:
                await self.favorites.close()
            if self.stickers and not self.stickers.closed:
                await self.stickers.close()
            await super().terminate()

    return OwnerOnlyAdapter()


async def run(root: Path):
    from filelock import FileLock

    with FileLock(str(root / "runtime/instance.lock"), timeout=0):
        await run_instance(root)


async def run_instance(root: Path):
    import win32crypt

    config = json.loads((root / "config/settings.json").read_text(encoding="utf-8-sig"))
    local_path = root / "config/local.json"
    if not local_path.exists():
        raise ValueError("请先运行 01_配置密钥.cmd，在本地输入 DeepSeek Key。")
    local = json.loads(local_path.read_text(encoding="utf-8-sig"))
    if not local.get("deepseek_key_dpapi"):
        raise ValueError("DeepSeek Key 尚未配置")
    import base64

    key = win32crypt.CryptUnprotectData(
        base64.b64decode(local["deepseek_key_dpapi"]), None, None, None, 0
    )[1].decode()
    token = local["onebot_token"]
    model = AstrBotModel(config, key)
    web_tools = DeepSeekWebTools(key) if config.get("web_enabled") else None
    adapter = build_adapter(config, token, model, root, web_tools=web_tools)
    ui_task = None
    if adapter.service.v2 and adapter.service.v2.options["ui_enabled"]:
        from hypercorn.asyncio import serve
        from hypercorn.config import Config as HypercornConfig
        from .memory_v2.graph_api import create_app, ensure_auth

        ui_config = HypercornConfig()
        ui_config.bind = ["127.0.0.1:" + str(adapter.service.v2.options["ui_port"])]
        ui_config.accesslog = ui_config.errorlog = None
        ui_services = {
            c.scope: c.v2
            for c in adapter.conversations()
            if c.v2 and (":group:" in c.scope or c.peer_qq == config["owner_qq"])
        }
        adapter.memory_ui_services.update(ui_services)
        ui_app = create_app(
            adapter.memory_ui_services,
            ensure_auth(root, config["owner_qq"]),
            root / "memory_ui",
            port=adapter.service.v2.options["ui_port"],
        )
        ui_task = asyncio.create_task(serve(ui_app, ui_config))
    browser_warmup_task = None
    if adapter.browser:

        async def warm_browser():
            try:
                await adapter.browser.start()
            except asyncio.CancelledError:
                raise
            except Exception:
                adapter.browser.last_error = (
                    "浏览器引擎预热未完成，聊天服务继续；请检查项目浏览器组件"
                )

        browser_warmup_task = asyncio.create_task(warm_browser())
    stop_file = root / "runtime/stop.request"
    stop_file.unlink(missing_ok=True)
    status_file = root / "runtime/status.json"

    async def monitor():
        last_maintenance = 0
        while not stop_file.exists():
            if asyncio.get_running_loop().time() - last_maintenance >= 60:
                if not adapter.service.v2:
                    adapter.service.store.memory.expire()
                adapter.service.store.history()  # Also purge expired recent-context text.
                for group in adapter.groups.values():
                    group.prune()
                for conversation in adapter.conversations():
                    if conversation.v2:
                        conversation.v2.expire()
                        conversation.v2.catch_up()
                    conversation.schedule_memory()
                    if (
                        conversation.autonomy.eligible()
                        and conversation.autonomy.state()["enabled"]
                    ):
                        conversation.verified = await adapter.checked_login()
                        if conversation.verified:
                            await conversation.autonomy.social.deliver()
                            conversation.autonomy.schedule()
                if adapter.service.v2:
                    from .memory_v2.retention import (
                        prune_managed_backups,
                        prune_legacy_memory_data,
                    )

                    try:
                        await asyncio.to_thread(
                            prune_managed_backups, root, adapter.service.v2.now()
                        )
                        await asyncio.to_thread(
                            prune_legacy_memory_data, root, adapter.service.v2.now()
                        )
                    except (ValueError, OSError):
                        adapter.service.memory_error = (
                            "受控备份清理未完成，请检查本地备份目录。"
                        )
                if adapter.connection is not None and (
                    config.get("private_all_friends", False)
                    or config.get("group_all_joined", False)
                ):
                    try:
                        await adapter.contacts.refresh()
                    except ConnectionError:
                        pass
                if adapter.stickers:
                    adapter.stickers.memory.maintain()
                if adapter.favorites and adapter.connection is not None:
                    adapter.favorites.schedule()
                adapter.service.schedule_memory()
                for conversation in adapter.conversations():
                    if conversation.voice:
                        await conversation.voice.status()
                        conversation.voice.purge()
                last_maintenance = asyncio.get_running_loop().time()
            count, reserved = adapter.service.store.usage()
            status = {
                "pid": os.getpid(),
                "bot_qq": config["bot_qq"],
                "owner_qq": config["owner_qq"],
                "ws_connected": adapter.connection is not None,
                "login_verified": adapter.login_verified,
                "api_requests_reserved": count,
                "max_requests": config["max_requests"],
                "budget_cny": config["budget_cny"],
                "budget_reserved_cny": round(reserved, 4),
                "last_error": adapter.service.last_error,
                "model": config["model"],
                "model_policy": "official_rolling_flash",
                "voice": {
                    "configured": bool(adapter.service.voice),
                    "ready": bool(
                        adapter.service.voice and adapter.service.voice.ready
                    ),
                    "automatic": bool(
                        adapter.service.voice
                        and adapter.service.voice.options.get("mode") == "automatic"
                    ),
                },
                "autonomy": {
                    "state": adapter.service.autonomy.state(),
                    "permissions": {
                        k: v
                        for k, v in adapter.service.autonomy.permissions().items()
                        if k != "browser_parameters"
                    },
                },
                "capabilities": {
                    "revision": capability_manifest()["revision"],
                    "loaded_each_turn": True,
                    "forgetting": False,
                    "registered_tools": sorted(MODEL_TOOL_NAMES),
                },
                "last_response_model": getattr(model, "last_response_model", None),
                "custom_persona": True,
                "chat_style": "dafeiyu-natural-20261028",
                "web_enabled": bool(web_tools),
                "group_chat": {
                    gid: {
                        "autonomy": group.autonomy.public_status(),
                        "queue": group.queue.qsize(),
                        "context_messages": len(group.recent),
                        "context_limit": group.context_limit,
                        "context_seconds": group.context_seconds,
                        "bot_master_group_role": group.group_roles.get(
                            config["owner_qq"], "unknown"
                        ),
                        "proactive_enabled": config.get(
                            "group_proactive_enabled", False
                        ),
                        "keywords": config.get("group_keywords", ["大肥鱼"]),
                        "reply_opportunity_probabilities": {
                            "random": group.config.get("group_random_probability", 0),
                        },
                        "poke_cooldown_seconds": 0,
                        "last_error": group.last_error,
                    }
                    for gid, group in adapter.groups.items()
                },
                "browser": adapter.browser.status()
                if adapter.browser
                else {"enabled": False},
                "sticker_memory": adapter.stickers.memory.stats(
                    adapter.service.sticker_scopes()
                )
                if adapter.stickers
                else None,
                "qq_favorites": adapter.favorites.status()
                if adapter.favorites
                else None,
                "chat_access": {
                    "all_friends": config.get("private_all_friends", False),
                    "all_joined_groups": config.get("group_all_joined", False),
                    "private_sessions": len(adapter.privates),
                    "known_friends": len(adapter.contacts.friends),
                    "joined_groups": len(adapter.contacts.groups),
                    "contact_error": adapter.contacts.last_error,
                },
                "typing_delay_enabled": config.get("typing_delay_enabled", False),
                "sticker_library": {
                    "count": adapter.stickers.stats(
                        [config["bot_qq"]]
                        if config.get("sticker_account_shared", False)
                        else config.get("group_ids", [])
                    )[0],
                    "pending": adapter.stickers.queue.qsize(),
                    "last_error": adapter.stickers.last_error,
                }
                if adapter.stickers
                else None,
                "profile_enabled": config.get("profile_enabled", False),
                "profile_last_diagnostic": adapter.service.member_tools.last_diagnostic
                if adapter.service.member_tools
                else None,
                "auto_memory_enabled": config.get("auto_memory_enabled", False),
                "memory_pending_messages": len(adapter.service.v2.pending(1000))
                if adapter.service.v2
                else adapter.service.store.memory.pending_count(),
                "memory_paused": not adapter.service.v2.policy(config["owner_qq"])[
                    "enabled"
                ]
                if adapter.service.v2
                else adapter.service.store.memory.controls()["paused"],
                "memory_last_error": adapter.service.memory_error,
                "memory_v2": {
                    "enabled": True,
                    "single_injection": True,
                    "embedding": "none_keyword_fallback",
                    "scopes": {
                        c.scope: c.v2.maintenance_status()
                        for c in adapter.conversations()
                        if c.v2
                    },
                    "ui_port": adapter.service.v2.options["ui_port"],
                    "ui_running": bool(ui_task and not ui_task.done()),
                }
                if adapter.service.v2
                else {"enabled": False},
                "independent_affect_enabled": True,
                "vision_enabled": True,
                "thinking": config["thinking"],
                "reasoning_effort": config["reasoning_effort"],
                "image_download_diagnostic": getattr(
                    adapter.service.image_loader, "last_diagnostic", None
                ),
            }
            tmp = status_file.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            tmp.replace(status_file)
            await asyncio.sleep(1)
        await adapter.terminate()

    monitor_task = asyncio.create_task(monitor())
    try:
        await adapter.run()
    finally:
        if browser_warmup_task:
            browser_warmup_task.cancel()
            await asyncio.gather(browser_warmup_task, return_exceptions=True)
        if ui_task:
            ui_task.cancel()
            await asyncio.gather(ui_task, return_exceptions=True)
        monitor_task.cancel()
        await asyncio.gather(monitor_task, return_exceptions=True)
        if not adapter.service.closed:
            await adapter.terminate()
        await model.close()
        if web_tools:
            await web_tools.close()
        status_file.unlink(missing_ok=True)
