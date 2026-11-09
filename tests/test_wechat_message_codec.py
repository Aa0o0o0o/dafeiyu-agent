import unittest
from dataclasses import replace

from src.wechat_message_codec import (
    MAX_TEXT_BYTES,
    UnsupportedContent,
    decode_message,
    join_text,
    split_text,
)


class CodecTests(unittest.TestCase):
    def test_plain_unicode_and_long_exact_round_trip(self):
        text = ('中文\n 👩\u200d💻e\u0301   ' * 400) + '末尾  '
        decoded = decode_message(1, text.encode('utf-8'))
        self.assertEqual(decoded.text, text)
        self.assertEqual(decoded.content_status, 'text_decoded')
        chunks = split_text(decoded.text)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(c.text) <= 3000 for c in chunks))
        self.assertEqual(join_text(chunks), text)
        with self.assertRaises(ValueError):
            join_text(chunks[:-1])
        with self.assertRaises(ValueError):
            join_text(tuple(reversed(chunks)))
        with self.assertRaises(ValueError):
            join_text((replace(chunks[0], text='altered'), *chunks[1:]))

    def test_non_text_does_not_claim_media_read_or_expose_payload(self):
        sentinel = b'private raw image bytes'
        for kind, expected in ((3, 'image'), (34, 'audio'), (43, 'video'), (47, 'sticker'), (49, 'attachment'), (99999, 'other')):
            with self.subTest(kind=kind):
                result = decode_message(kind, sentinel, b'metadata')
                self.assertEqual(result.kind, expected)
                self.assertIsNone(result.text)
                self.assertEqual(result.content_status, 'media_not_inspected')
                self.assertEqual(result.packed_metadata_bytes, 8)
                self.assertNotIn('private', repr(result))

    def test_invalid_utf8_and_missing_text_fail_without_guessing(self):
        for value in (b'\xff\xfe', None):
            with self.subTest(value=value), self.assertRaises(UnsupportedContent):
                decode_message(1, value)

    def test_zstd_common_frames_lossless_and_strict(self):
        try:
            import zstandard as zstd
        except ImportError:
            self.skipTest('project isolated zstandard dependency unavailable')
        text = ('中文合成内容🙂\n' * 1000) + ' 尾部保留  '
        raw = text.encode('utf-8')
        for size_in_header in (True, False):
            with self.subTest(size_in_header=size_in_header):
                frame = zstd.ZstdCompressor(write_content_size=size_in_header).compress(raw)
                self.assertEqual(decode_message(1, frame).text, text)
                self.assertEqual(decode_message(1, None, compress_content=frame).text, text)
                for broken in (frame[:-1], frame + b'garbage', frame + frame):
                    with self.assertRaises(UnsupportedContent):
                        decode_message(1, broken)

    def test_zstd_bomb_fails_without_truncating(self):
        try:
            import zstandard as zstd
        except ImportError:
            self.skipTest('project isolated zstandard dependency unavailable')
        for size_in_header in (True, False):
            with self.subTest(size_in_header=size_in_header):
                frame = zstd.ZstdCompressor(write_content_size=size_in_header).compress(b'x' * (MAX_TEXT_BYTES + 1))
                with self.assertRaises(UnsupportedContent):
                    decode_message(1, frame)


if __name__ == '__main__':
    unittest.main()
