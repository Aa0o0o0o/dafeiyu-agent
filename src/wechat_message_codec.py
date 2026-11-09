"""Synthetic-only WeChat text decoding and metadata, not production integration.

No network, file I/O, model call, media interpretation, or WeChat discovery.
Text bytes are strict UTF-8 or a single complete Zstandard frame. Unsupported
encodings fail; no silent replacement, trimming, guessing, or truncated output.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from io import BytesIO
from typing import Literal

ZSTD_MAGIC = b'\x28\xb5\x2f\xfd'
MAX_COMPRESSED_BYTES = 8 * 1024 * 1024
MAX_TEXT_BYTES = 8 * 1024 * 1024


class UnsupportedContent(ValueError):
    """Source content cannot be faithfully decoded with this prototype."""


@dataclass(frozen=True)
class DecodedMessage:
    kind: str
    type_code: int
    text: str | None
    content_status: Literal['text_decoded', 'media_not_inspected']
    packed_metadata_bytes: int


@dataclass(frozen=True)
class TextChunk:
    index: int
    total: int
    digest: str
    text: str


def _decode_zstd(payload: bytes) -> bytes:
    if len(payload) > MAX_COMPRESSED_BYTES:
        raise UnsupportedContent('compressed content exceeds supported size')
    try:
        import zstandard
    except ImportError as error:
        raise UnsupportedContent('zstandard dependency unavailable') from error
    try:
        params = zstandard.get_frame_parameters(payload)
        if params.dict_id:
            raise UnsupportedContent('zstd dictionary required')
        if params.window_size > MAX_TEXT_BYTES:
            raise UnsupportedContent('zstd window exceeds supported size')
        size = params.content_size
        if size not in (zstandard.CONTENTSIZE_UNKNOWN, zstandard.CONTENTSIZE_ERROR) and size > MAX_TEXT_BYTES:
            raise UnsupportedContent('text exceeds supported size')
        # Stream with an output cap, including frames without a declared size.
        # A second bounded decode verifies exactly one complete frame and no tail.
        context = zstandard.ZstdDecompressor(max_window_size=MAX_TEXT_BYTES)
        with context.stream_reader(BytesIO(payload), read_across_frames=False) as reader:
            output = reader.read(MAX_TEXT_BYTES + 1)
            if len(output) > MAX_TEXT_BYTES or reader.read(1):
                raise UnsupportedContent('text exceeds supported size')
        obj = zstandard.ZstdDecompressor(max_window_size=MAX_TEXT_BYTES).decompressobj()
        checked = obj.decompress(payload)
        if not obj.eof or obj.unused_data or checked != output:
            raise UnsupportedContent('incomplete or ambiguous zstd frame')
        return checked
    except UnsupportedContent:
        raise
    except zstandard.ZstdError as error:
        raise UnsupportedContent('invalid or unsupported zstd frame') from error


def _text(value: str | bytes) -> str:
    if isinstance(value, str):
        if len(value.encode('utf-8')) > MAX_TEXT_BYTES:
            raise UnsupportedContent('text exceeds supported size')
        return value
    if not isinstance(value, bytes):
        raise UnsupportedContent('unsupported text representation')
    raw = _decode_zstd(value) if value.startswith(ZSTD_MAGIC) else value
    if len(raw) > MAX_TEXT_BYTES:
        raise UnsupportedContent('text exceeds supported size')
    try:
        return raw.decode('utf-8', errors='strict')
    except UnicodeDecodeError as error:
        raise UnsupportedContent('text is not valid UTF-8') from error


def decode_message(local_type: int, message_content: str | bytes | None,
                   packed_info_data: str | bytes | None = None,
                   compress_content: str | bytes | None = None) -> DecodedMessage:
    """Decode text or describe the existence of media, without reading media.

    Compressed fallback is used only when the message field is absent or empty;
    this policy is a prototype assumption that requires real version validation.
    """
    if type(local_type) is not int or local_type < 0:
        raise ValueError('invalid message type')
    if packed_info_data is None:
        packed_size = 0
    elif isinstance(packed_info_data, bytes):
        packed_size = len(packed_info_data)
    elif isinstance(packed_info_data, str):
        packed_size = len(packed_info_data.encode('utf-8'))
    else:
        raise ValueError('unsupported metadata type')
    if local_type != 1:
        kind = {3: 'image', 34: 'audio', 43: 'video', 47: 'sticker', 48: 'location', 49: 'attachment'}.get(local_type, 'other')
        return DecodedMessage(kind, local_type, None, 'media_not_inspected', packed_size)
    source = compress_content if message_content is None or message_content == '' or message_content == b'' else message_content
    if source is None:
        raise UnsupportedContent('text content missing')
    return DecodedMessage('text', local_type, _text(source), 'text_decoded', packed_size)


def split_text(text: str, max_chars: int = 3000) -> tuple[TextChunk, ...]:
    """Lossless chunking for later atomic storage; does not itself persist data."""
    if not isinstance(text, str) or type(max_chars) is not int or max_chars < 1:
        raise ValueError('invalid text or chunk size')
    data = text.encode('utf-8')
    if len(data) > MAX_TEXT_BYTES:
        raise UnsupportedContent('text exceeds supported size')
    digest = hashlib.sha256(data).hexdigest()
    total = max(1, (len(text) + max_chars - 1) // max_chars)
    return tuple(TextChunk(i, total, digest, text[i * max_chars:(i + 1) * max_chars]) for i in range(total))


def join_text(chunks: tuple[TextChunk, ...]) -> str:
    """Reject incomplete, reordered, mixed or modified chunks; do not guess."""
    if not chunks or any(not isinstance(chunk, TextChunk) for chunk in chunks):
        raise ValueError('invalid chunks')
    total, digest = chunks[0].total, chunks[0].digest
    if total != len(chunks) or any(c.index != i or c.total != total or c.digest != digest for i, c in enumerate(chunks)):
        raise ValueError('incomplete or reordered chunks')
    text = ''.join(c.text for c in chunks)
    if hashlib.sha256(text.encode('utf-8')).hexdigest() != digest:
        raise ValueError('modified chunks')
    return text
