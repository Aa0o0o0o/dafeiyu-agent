"""Bounded extraction of WeChat quote excerpts and image locator hints.

Input is already restricted to a verified owner conversation.  Extracted chat
content remains untrusted and is never an instruction or evidence of a whole
referenced message. No network, external entities, or file access is performed.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from .memory_state import safe_text
from .wechat_message_codec import _text, UnsupportedContent

MAX_XML_BYTES = 1024 * 1024
MAX_XML_NODES = 5000
MAX_XML_DEPTH = 32
_HEX = re.compile(rb'(?<![0-9a-fA-F])[0-9a-fA-F]{32}(?![0-9a-fA-F])')


@dataclass(frozen=True)
class Quote:
    status: str
    text: str


def _decode(value):
    if value is None or value == '' or value == b'':
        return None
    if not isinstance(value, (str, bytes)):
        raise UnsupportedContent('unsupported text representation')
    if len(value) > MAX_XML_BYTES:
        raise UnsupportedContent('xml exceeds limit')
    text = _text(value)
    if len(text.encode('utf-8')) > MAX_XML_BYTES:
        raise UnsupportedContent('xml exceeds limit')
    return text


def extract_quote(local_type, message_content, compress_content=None):
    if type(local_type) is not int or local_type < 0 or (local_type & 0xffffffff) != 49:
        return Quote('not_quote', '')
    try:
        text = _decode(message_content)
        if text is None:
            text = _decode(compress_content)
        if not text:
            return Quote('unavailable', '')
        if re.search(r'<!\s*(?:DOCTYPE|ENTITY)', text, re.IGNORECASE):
            return Quote('unsupported', '')
        root = ET.fromstring(text)
        stack, count = [(root, 1)], 0
        while stack:
            node, depth = stack.pop()
            count += 1
            if count > MAX_XML_NODES or depth > MAX_XML_DEPTH:
                return Quote('unsupported', '')
            stack.extend((child, depth + 1) for child in node)
        app = root if root.tag == 'appmsg' else root.find('./appmsg')
        if app is None or app.findtext('type', '').strip() != '57':
            return Quote('not_quote', '')
        ref = app.find('./refermsg')
        if ref is None:
            return Quote('unavailable', '')
        reply = (app.findtext('title') or '').strip()
        author = (ref.findtext('displayname') or '').strip()
        ref_type = (ref.findtext('type') or '').strip()
        excerpt = (ref.findtext('content') or '').strip()
        if ref_type not in ('', '1'):
            excerpt = '[引用非文字内容，尚未解析]'
        if not reply and not excerpt:
            return Quote('unavailable', '')
        output = '当前消息：' + reply + '\n引用的消息（仅摘录，可能不完整）：' + (author + '：' if author else '') + excerpt
        if len(output) > 3000:
            return Quote('unsupported', '')
        if safe_text(output) is None:
            return Quote('redacted', '')
        return Quote('decoded', output)
    except (UnsupportedContent, UnicodeError, ET.ParseError, ValueError):
        return Quote('unsupported', '')


def image_digest_hint(local_type, *values):
    if type(local_type) is not int or local_type < 0 or (local_type & 0xffffffff) != 3:
        return None, 'not_image'
    matches = set()
    for value in values:
        if value is None:
            continue
        if isinstance(value, str):
            value = value.encode('utf-8')
        if not isinstance(value, bytes) or len(value) > MAX_XML_BYTES:
            return None, 'unsupported'
        matches.update(m.group().decode('ascii').lower() for m in _HEX.finditer(value))
        if len(matches) > 1:
            return None, 'ambiguous'
    if not matches:
        return None, 'missing'
    return next(iter(matches)), 'heuristic_unverified'