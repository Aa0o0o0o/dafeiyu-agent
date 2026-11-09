"""Extension seams; phase one deliberately exposes no operation capabilities."""

from dataclasses import dataclass, field
from typing import Protocol
from uuid import uuid4
from datetime import datetime, timezone


@dataclass(frozen=True)
class WebCall:
    id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class ModelResult:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    response_model: str = ""
    tool_calls: tuple[WebCall, ...] = ()
    assistant_message: dict | None = field(default=None, repr=False)


class ModelProvider(Protocol):
    async def complete(
        self,
        messages: list[dict],
        *,
        allow_tools: bool = True,
        tool_names: tuple[str, ...] | None = None,
    ) -> ModelResult: ...


class ChannelAdapter(Protocol):
    async def send_text(self, text: str) -> str: ...


class MemoryStore(Protocol):
    def memories(self) -> list[tuple[int, str]]: ...


class EnvironmentAdapter(Protocol):
    capabilities: tuple[str, ...]


@dataclass(frozen=True)
class CapabilityRegistry:
    operations: tuple[str, ...] = ()


@dataclass(frozen=True)
class PresentationEvent:
    conversation_id: str
    turn_id: str
    type: str
    schema_version: int = 1
    event_id: str = field(default_factory=lambda: uuid4().hex)
    time: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    emotion: str = "neutral"

    def __post_init__(self):
        if self.emotion not in {"neutral", "happy", "sad", "thinking"}:
            raise ValueError("Unsupported presentation emotion")
