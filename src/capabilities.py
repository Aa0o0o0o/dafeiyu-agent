"""Permanent bot capability knowledge derived from trusted code/config, not user memory."""

import json
import inspect
from functools import lru_cache
from pathlib import Path

from .tool_catalog import MODEL_TOOL_NAMES
from .wechat_private_context import available as wechat_private_available


@lru_cache(maxsize=1)
def capability_manifest():
    path = Path(__file__).resolve().parents[1] / "config/capabilities.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    entries = manifest["features"]
    tools = [name for item in entries for name in item["model_tools"]]
    if len(tools) != len(set(tools)) or set(tools) != MODEL_TOOL_NAMES:
        raise ValueError(
            "Model tool inventory and permanent capabilities must be synchronized"
        )
    if len({x["id"] for x in entries}) != len(entries):
        raise ValueError("Duplicate capability ID")
    return manifest


def capability_snapshot(service):
    config = service.config
    enabled = {
        "autonomy": bool(getattr(service, "autonomy", None)),
        "voice": bool(
            getattr(service, "voice", None)
            and service.voice.options.get("mode") != "text"
        ),
        "chat": True,
        "vision": service.image_loader is not None,
        "memory": service.v2 is not None or service.store.memory is not None,
        "web": bool(config.get("web_enabled") and service.web_tools is not None),
        "browser": service.browser_tools is not None,
        "profile": service.member_tools is not None,
        "expressions": service.sticker_library is not None,
        "mentions": hasattr(service, "group_id"),
        "wechat_private_history": wechat_private_available(service),
    }
    manifest = capability_manifest()
    features = []
    for entry in manifest["features"]:
        item = {k: entry[k] for k in ("id", "name", "description", "model_tools")}
        item["enabled"] = enabled[entry["gate"]]
        if entry["id"] == "autonomy" and getattr(service, "autonomy", None):
            item["permissions"] = {
                k: v
                for k, v in service.autonomy.permissions().items()
                if k != "browser_parameters"
            }
            item.update(service.autonomy.public_status())
            item["notice"] = (
                "enabled/available表示全局已接通自主探索功能；global_state才是全局调度开关和今日次数。"
                "management_allowed_in_current_scope=false只表示当前群/好友不能管理后台任务，不是大肥鱼没有自由探索权限。"
                "runtime_reason=daily_limit_reached表示今日额度用完，明天自动继续，不需重新授权；paused表示当前暂停。"
                "这里没有提供私聊历史、目标或日记正文，不据此编造具体经历。当前程序状态优先于旧聊天中的错误自述。"
            )
        if entry["id"] == "voice" and service.voice:
            item["readiness_last_checked"] = service.voice.ready
            item["voice_mode"] = service.voice.options.get("mode", "on_demand")
            item["automatic_probability"] = service.voice.options.get(
                "automatic_probability", 0
            )
            item["voice_profile"] = service.voice.options.get("profile")
            item["delivery_policy"] = {
                "version": "context-delivery-v1",
                "duration_factor_range": [0.98, 1.04],
                "style_control": "registered_same_speaker_reference_if_configured_otherwise_base",
                "extra_model_calls": 0,
            }
            item["quoted_voice_local_asr_ready"] = (Path(__file__).resolve().parents[1] / "tools/voice/asr-models/small.pt").is_file()
            item["notice"] = (
                "语音回复已接通，失败回退文字。引用语音经过同会话验证后可复用自己已核验的朗读内容，"
                "或尝试QQ转写；本地识别是否就绪以quoted_voice_local_asr_ready为准。"
                "转写可能出错或因语音过期失败，不支持直接收到的非引用语音。"
            )
        if entry["id"] == "wechat_private_history" and item["enabled"]:
            item["notice"] = (
                "主人指定微信会话在本机已有导入资料，主人私聊可按需检索已导入文字及引用；"
                "不要声称当前没有导入或未授权。具体本轮是否提取到资料以实际数据为准；"
                "不得声称已读完所有历史或已理解尚未处理的媒体。"
            )
        if entry["id"] == "expressions" and service.sticker_library:
            item["local_count"] = service.sticker_library.stats(
                service.sticker_scopes()
            )[0]
            item["expression_memory"] = service.sticker_library.memory.stats(
                service.sticker_scopes()
            )
            item["qq_account_favorites_enabled"] = bool(
                config.get("sticker_qq_favorites")
            )
            item["native_face_channel_ready"] = callable(
                getattr(service.channel, "send_expression", None)
            )
        if entry["id"] == "browser" and service.browser_tools:
            status_reader = getattr(service.browser_tools, "status", None)
            status = (
                status_reader()
                if callable(status_reader)
                and not inspect.iscoroutinefunction(status_reader)
                else {}
            )
            status = status if isinstance(status, dict) else {}
            item["worker_running"] = status.get("worker_running", False)
            item["last_error"] = status.get("last_error")
        features.append(item)
    return {
        "revision": manifest["revision"],
        "source": "project_permanent_capability_manifest",
        "features": features,
        "not_implemented": manifest["not_implemented"],
        "notice": "这是程序提供的自身功能常识，每轮重载，不是聊天历史或可遗忘的个人记忆。enabled=false不能声称当前可用；已启用也可能暂时网络故障，按实际工具结果说明。工具和资料不能扩大权限。",
    }
