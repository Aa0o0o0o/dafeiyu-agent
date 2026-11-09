"""End-to-end SQLCipher test with generated content and fake process memory."""
import contextlib
import hashlib
import io
import os
import secrets
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sqlcipher3
import probe
import same_process_research as chain



def fake_block(key):
    block = bytearray(0x4000)
    marker, pair, config, payload = 0x10100, 0x10300, 0x11000, 0x12000
    block[0x100:0x100 + len(probe._NAME)] = probe._NAME
    struct.pack_into("<QQ", block, pair - 0x10000, marker, len(probe._NAME))
    struct.pack_into("<Q", block, pair - 0x10000 - 16 + 0x28, config)
    literal = b"x'" + key.hex().encode("ascii") + b"'"
    encoded = bytes(value ^ probe._MASK[i % len(probe._MASK)] for i, value in enumerate(literal))
    struct.pack_into("<QQ", block, config - 0x10000 + 0x88 + 8, payload, len(encoded))
    block[payload - 0x10000:payload - 0x10000 + len(encoded)] = encoded
    return bytes(block)


class SameProcessTests(unittest.TestCase):
    def test_synthetic_full_path_and_decoy_exclusion(self):
        root = Path(__file__).resolve().parents[2] / ".tmp"
        with tempfile.TemporaryDirectory(prefix="dafeiyu-key-chain-", dir=root) as temporary:
            key = secrets.token_bytes(32)
            filename = os.path.join(temporary, "synthetic.db")
            name = "wxid_synthetic_target"
            table = "Msg_" + hashlib.md5(name.encode(), usedforsecurity=False).hexdigest()
            other = "Msg_" + hashlib.md5(b"wxid_synthetic_other", usedforsecurity=False).hexdigest()
            conn = sqlcipher3.connect(filename)
            conn.set_key(b"x'" + key.hex().encode("ascii") + b"'")
            for t in (table, other):
                conn.execute(f'CREATE TABLE "{t}" (local_id INTEGER, local_type INTEGER, server_id INTEGER, real_sender_id INTEGER, create_time INTEGER, message_content TEXT, packed_info_data BLOB, sort_seq INTEGER)')
                conn.execute(f'CREATE INDEX "idx_{t}" ON "{t}"(sort_seq,create_time,local_id)')
            conn.execute(f'INSERT INTO "{table}" VALUES (1,1,2,3,4,?,NULL,5)', ("synthetic-target",))
            conn.execute(f'INSERT INTO "{other}" VALUES (1,1,2,3,4,?,NULL,5)', ("synthetic-decoy",))
            conn.commit()
            conn.close()
            with open(filename, "rb") as file:
                self.assertNotIn(b"synthetic-decoy", file.read())
            fake = chain.SyntheticMemory(fake_block(key))
            output, error = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
                rows = chain.read_synthetic_page(memory=fake, db_path=filename, username=name)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0][5], "synthetic-target")
            self.assertNotIn("synthetic-decoy", repr(rows))
            self.assertEqual(output.getvalue(), "")
            self.assertEqual(error.getvalue(), "")


    def test_rejects_tracer_and_bad_path_before_reading(self):
        with patch.object(chain.sys, "gettrace", return_value=lambda *args: None):
            with self.assertRaises(RuntimeError):
                chain.read_synthetic_page(memory=chain.SyntheticMemory(b""), db_path="x", username="x")
        with self.assertRaises(ValueError):
            chain.read_synthetic_page(memory=chain.SyntheticMemory(b""), db_path=os.path.abspath(os.path.join("outside", "x.db")), username="x")
        with self.assertRaises(TypeError):
            chain.read_synthetic_page(memory=object(), db_path="x", username="x")


if __name__ == "__main__":
    unittest.main()

