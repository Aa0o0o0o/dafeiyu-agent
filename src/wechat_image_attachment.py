"""Target-conversation attachment lookup and in-memory image decoding.

Trusted local callers must independently authenticate the owner and bind the
account directory and target username. This is not a model tool. Candidate
digests come from message metadata and remain unverified associations.
"""
from __future__ import annotations

import hashlib
import io
import os
import re
import struct
from dataclasses import dataclass, field
from pathlib import Path

_V1 = b"\x07\x08\x05\x56\x02\x05"
_V2 = b"\x07\x08\x56\x32\x08\x07"
_DIGEST = re.compile(r"^[0-9a-f]{32}$")
_MONTH = re.compile(r"^20[0-9]{2}-(0[1-9]|1[0-2])$")
_FILE = re.compile(r"^([0-9a-f]{32})(_h|_t)?\.dat$", re.IGNORECASE)
MAX_IMAGE_BYTES = 32 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000


@dataclass(frozen=True)
class ImageCandidate:
    path: Path = field(repr=False)
    tier: str
    size: int
    association: str = "heuristic_unverified"


def _ordinary(path: Path, *, directory: bool) -> bool:
    try:
        return (not path.is_symlink() and not path.is_junction()
                and (path.is_dir() if directory else path.is_file()))
    except OSError:
        return False


def index_candidates(account_dir: Path, username: str, digests: set[str]):
    """List only direct Img children of the one supplied conversation.

    No image is opened, downloaded, decrypted or exposed to a model. Duplicate
    same-tier candidates are marked ambiguous; callers may not pick one.
    """
    if (not isinstance(username, str) or not username or username.endswith('@chatroom')
            or '\x00' in username or not isinstance(digests, set)
            or len(digests) > 100_000 or any(not _DIGEST.fullmatch(x) for x in digests)):
        raise ValueError('invalid target input')
    account = Path(account_dir)
    if not _ordinary(account, directory=True):
        raise ValueError('account directory unavailable')
    base = account
    for component in ('msg', 'attach', hashlib.md5(username.encode(), usedforsecurity=False).hexdigest()):
        base = base / component
        if not _ordinary(base, directory=True):
            return {key: {} for key in digests}
    found = {key: {} for key in digests}
    with os.scandir(base) as months:
        for month in months:
            if not _MONTH.fullmatch(month.name) or not month.is_dir(follow_symlinks=False):
                continue
            month_path = Path(month.path)
            if not _ordinary(month_path, directory=True):
                continue
            folder = month_path / 'Img'
            if not _ordinary(folder, directory=True):
                continue
            with os.scandir(folder) as files:
                for entry in files:
                    match = _FILE.fullmatch(entry.name)
                    if not match or match[1].lower() not in found:
                        continue
                    p = Path(entry.path)
                    if not _ordinary(p, directory=False):
                        continue
                    info = entry.stat(follow_symlinks=False)
                    if not 0 < info.st_size <= MAX_IMAGE_BYTES:
                        continue
                    tier = {'_h': 'original', '_t': 'thumbnail', None: 'normal'}[match[2].lower() if match[2] else None]
                    choices = found[match[1].lower()]
                    candidate = ImageCandidate(p, tier, info.st_size)
                    choices[tier] = candidate if tier not in choices else None
    return found


def decode_image(data: bytes, *, aes_key: bytes | None = None, xor_key: int | None = None) -> bytes:
    """Decode supported local .dat formats in memory, without discovering keys."""
    if not isinstance(data, bytes) or not 0 < len(data) <= MAX_IMAGE_BYTES:
        raise ValueError('invalid image data')
    if xor_key is not None and (type(xor_key) is not int or not 0 <= xor_key <= 255):
        raise ValueError('invalid image key')
    if data.startswith(_V2):
        if aes_key is None or xor_key is None or len(aes_key) != 16 or len(data) < 31:
            raise ValueError('image key unavailable')
        aes_len, xor_len = struct.unpack_from('<II', data, 6)
        encrypted_len = aes_len + (16 - aes_len % 16)
        end = 15 + encrypted_len
        if end > len(data) or xor_len > len(data) - end:
            raise ValueError('truncated image data')
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        dec = Cipher(algorithms.AES(aes_key), modes.ECB()).decryptor()
        prefix = dec.update(data[15:end]) + dec.finalize()
        pad = prefix[-1]
        if not 1 <= pad <= 16 or prefix[-pad:] != bytes([pad]) * pad or len(prefix) - pad != aes_len:
            raise ValueError('invalid image padding')
        split = len(data) - xor_len
        result = prefix[:-pad] + data[end:split] + bytes(b ^ xor_key for b in data[split:])
    elif data.startswith(_V1):
        if xor_key is None or len(data) <= 22:
            raise ValueError('image key unavailable')
        result = bytes(b ^ xor_key for b in data[22:])
    else:
        possibilities = (xor_key,) if xor_key is not None else (0x88, 0x30, 0xFF, 0xE9)
        result = b''
        for candidate in possibilities:
            decoded = bytes(b ^ candidate for b in data)
            if decoded.startswith((b'\xff\xd8\xff', b'\x89PNG\r\n\x1a\n', b'GIF87a', b'GIF89a')):
                result = decoded
                break
        if not result:
            raise ValueError('unsupported image format')
    # Validate the image itself; no claims of original resolution or authorship.
    try:
        from PIL import Image
        with Image.open(io.BytesIO(result)) as im:
            if im.width * im.height > MAX_IMAGE_PIXELS:
                raise ValueError('image dimensions exceed limit')
            im.verify()
    except (OSError, SyntaxError) as exc:
        raise ValueError('invalid decoded image') from exc
    return result
