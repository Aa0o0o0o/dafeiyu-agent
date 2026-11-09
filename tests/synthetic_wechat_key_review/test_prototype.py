"""Randomized synthetic tests; nothing here accesses a user process or database."""
import hashlib
import hmac
import secrets
import unittest

from prototype import FakeMemory, has_valid_candidate, simulate_scan


def page_for(key, *, explicit_salt=None):
    page = bytearray(secrets.token_bytes(4096))
    salt = bytes(page[:16]) if explicit_salt is None else explicit_salt
    mac_salt = bytes(value ^ 0x3A for value in salt)
    mac_key = hashlib.pbkdf2_hmac("sha512", key, mac_salt, 2, dklen=32)
    page[-64:] = hmac.digest(mac_key, bytes(page[16:-64]) + b"\x01\x00\x00\x00", "sha512")
    return bytes(page)


class SyntheticTests(unittest.TestCase):
    def test_random_standard_keys_and_changed_page_fail(self):
        for _ in range(20):
            key = secrets.token_bytes(32)
            page = page_for(key)
            literal = b"x'" + key.hex().encode("ascii") + b"'"
            self.assertTrue(has_valid_candidate(b"prefix" + literal + b"suffix", page))
            changed = page[:100] + bytes([page[100] ^ 1]) + page[101:]
            self.assertFalse(has_valid_candidate(literal, changed))
            self.assertFalse(has_valid_candidate(b"x'" + secrets.token_hex(32).encode() + b"'", page))

    def test_explicit_salt_and_wrong_salt(self):
        key, salt = secrets.token_bytes(32), secrets.token_bytes(16)
        page = page_for(key, explicit_salt=salt)
        literal = b"X'" + (key + salt).hex().encode() + b"'"
        self.assertTrue(has_valid_candidate(literal, page))
        self.assertFalse(has_valid_candidate(b"x'" + key.hex().encode() + b"'", page))
        self.assertFalse(has_valid_candidate(b"x'" + (key + secrets.token_bytes(16)).hex().encode() + b"'", page))

    def test_invalid_literals_and_inputs(self):
        page = page_for(secrets.token_bytes(32))
        for item in (b"", b"x'bad'", b"x'" + b"a" * 63 + b"'", b"x'" + b"a" * 65 + b"'"):
            self.assertFalse(has_valid_candidate(item, page))
        with self.assertRaises(TypeError):
            has_valid_candidate("not bytes", page)
        with self.assertRaises(ValueError):
            has_valid_candidate(b"", b"short")
        with self.assertRaises(ValueError):
            has_valid_candidate(b"a" * 1048577, page)

    def test_identity_is_required_and_fake_only(self):
        key = secrets.token_bytes(32)
        page = page_for(key)
        source = FakeMemory(7, "synthetic-only", (b"x'" + key.hex().encode() + b"'",))
        self.assertTrue(simulate_scan(source, pid=7, trusted_path="synthetic-only", page=page))
        with self.assertRaises(ValueError):
            simulate_scan(source, pid=8, trusted_path="synthetic-only", page=page)
        with self.assertRaises(ValueError):
            simulate_scan(source, pid=7, trusted_path="wrong", page=page)
        with self.assertRaises(TypeError):
            simulate_scan(object(), pid=7, trusted_path="synthetic-only", page=page)
        self.assertFalse(simulate_scan(FakeMemory(7, "synthetic-only", (b"empty",)), pid=7, trusted_path="synthetic-only", page=page))
        source.blocks = [b"mutated"]
        with self.assertRaises(TypeError):
            simulate_scan(source, pid=7, trusted_path="synthetic-only", page=page)


if __name__ == "__main__":
    unittest.main()

