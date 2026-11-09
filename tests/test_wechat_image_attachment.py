from __future__ import annotations

import hashlib
import io
import struct
import tempfile
import unittest
from pathlib import Path

from PIL import Image
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from src.wechat_image_attachment import _V1, _V2, decode_image, index_candidates


class ImageAttachmentTests(unittest.TestCase):
    def png(self):
        output = io.BytesIO()
        Image.new('RGB', (3, 3), 'blue').save(output, 'PNG')
        return output.getvalue()

    def test_target_only_and_duplicate_ambiguity(self):
        with tempfile.TemporaryDirectory(dir=Path('.tmp')) as tmp:
            root = Path(tmp)
            user = 'synthetic_target'
            folder = root / 'msg' / 'attach' / hashlib.md5(user.encode(), usedforsecurity=False).hexdigest()
            digest = 'a' * 32
            for month in ('2026-01', '2026-02'):
                p = folder / month / 'Img'
                p.mkdir(parents=True)
                (p / (digest + '.dat')).write_bytes(b'dummy')
            (folder / '2026-01' / 'Img' / (digest + '_t.dat')).write_bytes(b'dummy')
            result = index_candidates(root, user, {digest, 'b' * 32})
            self.assertIsNone(result[digest]['normal'])
            self.assertEqual(result[digest]['thumbnail'].association, 'heuristic_unverified')
            self.assertEqual(result['b' * 32], {})
            self.assertEqual(index_candidates(root, 'different', {digest})[digest], {})
            self.assertNotIn(str(root), repr(result[digest]['thumbnail']))

    def test_legacy_xor_and_v1(self):
        raw = self.png()
        key = 0x88
        encoded = bytes(value ^ key for value in raw)
        self.assertEqual(decode_image(encoded), raw)
        self.assertEqual(decode_image(_V1 + b'x' * 16 + encoded, xor_key=key), raw)
        with self.assertRaises(ValueError):
            decode_image(_V1 + b'x' * 16 + encoded)

    def test_v2_and_reject_wrong_or_truncated(self):
        raw = self.png()
        aes_key, xor_key = b'0123456789abcdef', 0x42
        prefix, middle, tail = raw[:19], raw[19:-9], raw[-9:]
        pad = 16 - len(prefix) % 16
        cipher = Cipher(algorithms.AES(aes_key), modes.ECB()).encryptor()
        encrypted = cipher.update(prefix + bytes([pad]) * pad) + cipher.finalize()
        data = _V2 + struct.pack('<II', len(prefix), len(tail)) + b'\0' + encrypted + middle + bytes(x ^ xor_key for x in tail)
        self.assertEqual(decode_image(data, aes_key=aes_key, xor_key=xor_key), raw)
        with self.assertRaises(ValueError):
            decode_image(data, aes_key=b'wrongwrongwrong1', xor_key=xor_key)
        with self.assertRaises(ValueError):
            decode_image(data[:20], aes_key=aes_key, xor_key=xor_key)


if __name__ == '__main__':
    unittest.main()
