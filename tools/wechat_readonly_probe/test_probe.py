"""Offline tests: every byte and process identity is synthetic."""
import hashlib
import hmac
import os
import secrets
import struct
import unittest
from unittest.mock import patch
from types import SimpleNamespace

import probe


def synthetic_case():
    key = secrets.token_bytes(32)
    page = bytearray(secrets.token_bytes(4096))
    salt = bytes(value ^ 0x3A for value in page[:16])
    mac_key = hashlib.pbkdf2_hmac("sha512", key, salt, 2, dklen=32)
    page[-64:] = hmac.digest(mac_key, bytes(page[16:-64]) + struct.pack("<I", 1), "sha512")
    block = bytearray(0x4000)
    marker = 0x10100
    pair = 0x10300
    config = 0x11000
    data_ptr = 0x12000
    block[0x100:0x100 + len(probe._NAME)] = probe._NAME
    struct.pack_into("<QQ", block, pair - 0x10000, marker, len(probe._NAME))
    struct.pack_into("<Q", block, pair - 0x10000 - 16 + 0x28, config)
    decoded = b"x'" + key.hex().encode("ascii") + b"'"
    encoded = bytes(value ^ probe._MASK[i % len(probe._MASK)] for i, value in enumerate(decoded))
    struct.pack_into("<QQ", block, config - 0x10000 + 0x88 + 8, data_ptr, len(encoded))
    block[data_ptr - 0x10000:data_ptr - 0x10000 + len(encoded)] = encoded
    return bytes(block), bytes(page)


class FakeReader:
    def __init__(self, block, path):
        self.block = block
        self.path = path
        self.closed = False

    def image_path(self):
        return self.path

    def close(self):
        self.closed = True

    def regions(self):
        yield 0x10000, len(self.block)

    def read(self, address, size):
        offset = address - 0x10000
        if offset < 0 or offset + size > len(self.block):
            return None
        return self.block[offset:offset + size]


class ProbeTests(unittest.TestCase):
    def test_matches_only_correct_synthetic_page(self):
        for _ in range(10):
            block, page = synthetic_case()
            fake = FakeReader(block, "synthetic")
            self.assertTrue(probe._scan_for_page_match(fake, page))
            bad = page[:100] + bytes([page[100] ^ 1]) + page[101:]
            self.assertFalse(probe._scan_for_page_match(fake, bad))

    def test_wrong_marker_config_pointer_and_size_fail(self):
        block, page = synthetic_case()
        for offset in (0x100, 0x300, 0x318, 0x1090):
            mutated = bytearray(block)
            mutated[offset:offset + 8] = b"\0" * 8
            self.assertFalse(probe._scan_for_page_match(FakeReader(bytes(mutated), ""), page))
        with self.assertRaises(ValueError):
            probe._scan_for_page_match(FakeReader(block, ""), b"short")

    def test_explicit_identity_signature_and_close(self):
        block, page = synthetic_case()
        path = os.path.abspath(os.path.join("synthetic", "Weixin.exe"))
        fake = FakeReader(block, path)
        with patch.object(probe, "_WindowsReader", return_value=fake) as ctor, patch.object(probe, "_verify_signature") as signer:
            self.assertTrue(probe.probe_selected_process(pid=7, expected_path=path, trusted_signer_thumbprint="A" * 40, first_page=page))
            ctor.assert_called_once_with(7)
            signer.assert_called_once_with(path, "A" * 40)
            self.assertTrue(fake.closed)

    def test_path_mismatch_prevents_signature_and_scan(self):
        block, page = synthetic_case()
        path = os.path.abspath(os.path.join("synthetic", "Weixin.exe"))
        fake = FakeReader(block, os.path.abspath(os.path.join("other", "Weixin.exe")))
        with patch.object(probe, "_WindowsReader", return_value=fake), patch.object(probe, "_verify_signature") as signer:
            with self.assertRaises(PermissionError):
                probe.probe_selected_process(pid=7, expected_path=path, trusted_signer_thumbprint="A" * 40, first_page=page)
            signer.assert_not_called()
            self.assertTrue(fake.closed)

    def test_invalid_inputs_do_not_open_process(self):
        path = os.path.abspath(os.path.join("synthetic", "Weixin.exe"))
        with patch.object(probe, "_WindowsReader") as ctor:
            for pid, exe, page in ((0, path, b"x" * 4096), (1, path, b"short"), (1, "relative", b"x" * 4096)):
                with self.assertRaises(ValueError):
                    probe.probe_selected_process(pid=pid, expected_path=exe, trusted_signer_thumbprint="A" * 40, first_page=page)
            ctor.assert_not_called()

    def test_signature_imports_system_module_and_compares_raw_bytes(self):
        result = SimpleNamespace(returncode=0, stdout=b"A" * 40)
        with patch.dict(probe.os.environ, {"SystemRoot": r"C:\Windows"}, clear=False), patch.object(probe.subprocess, "run", return_value=result) as run:
            probe._verify_signature("synthetic-path", "A" * 40)
        self.assertIn("Microsoft.PowerShell.Security.psd1", run.call_args.args[0][-1])
        self.assertNotIn("text", run.call_args.kwargs)

    def test_signature_wrong_bytes_fail_closed(self):
        result = SimpleNamespace(returncode=0, stdout=b"B" * 40)
        with patch.dict(probe.os.environ, {"SystemRoot": r"C:\Windows"}, clear=False), patch.object(probe.subprocess, "run", return_value=result):
            with self.assertRaises(PermissionError):
                probe._verify_signature("synthetic-path", "A" * 40)
    def test_signature_rejection_closes_without_scanning(self):
        block, page = synthetic_case()
        path = os.path.abspath(os.path.join("synthetic", "Weixin.exe"))
        fake = FakeReader(block, path)
        with patch.object(probe, "_WindowsReader", return_value=fake), patch.object(probe, "_verify_signature", side_effect=PermissionError), patch.object(probe, "_scan_for_page_match") as scan:
            with self.assertRaises(PermissionError):
                probe.probe_selected_process(pid=7, expected_path=path, trusted_signer_thumbprint="A" * 40, first_page=page)
            scan.assert_not_called()
            self.assertTrue(fake.closed)


if __name__ == "__main__":
    unittest.main()

