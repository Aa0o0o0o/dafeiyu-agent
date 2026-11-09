"""Exercise the actual interactive setup script against a temporary local folder."""

import base64
import contextlib
import io
import json
import runpy
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import win32crypt

ROOT = Path(__file__).resolve().parents[1]


class CredentialsTests(unittest.TestCase):
    def test_setup_encrypts_roundtrip_and_never_prints_key(self):
        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as directory:
            root = Path(directory)
            (root / "scripts").mkdir()
            (root / "config").mkdir()
            script = root / "scripts/setup_local.py"
            shutil.copy(ROOT / "scripts/setup_local.py", script)
            output = io.StringIO()
            fake_key = "offline-credential-roundtrip-only"
            with (
                patch("getpass.getpass", return_value=fake_key),
                contextlib.redirect_stdout(output),
            ):
                runpy.run_path(str(script), run_name="__main__")
            private = (root / "config/local.json").read_text()
            self.assertNotIn(fake_key, private)
            self.assertNotIn(fake_key, output.getvalue())
            data = json.loads(private)
            self.assertEqual(
                win32crypt.CryptUnprotectData(
                    base64.b64decode(data["deepseek_key_dpapi"]), None, None, None, 0
                )[1].decode(),
                fake_key,
            )
            self.assertGreaterEqual(len(data["onebot_token"]), 32)
            self.assertNotEqual(data["onebot_token"], data["napcat_webui_token"])

    def test_memory_ui_auth_uses_actual_windows_dpapi_and_is_idempotent(self):
        from src.memory_v2.graph_api import ensure_auth
        import hashlib

        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as directory:
            root = Path(directory)
            (root / "config").mkdir()
            data = ensure_auth(root, "12345001")
            password = win32crypt.CryptUnprotectData(
                base64.b64decode(data["password_dpapi"]), None, None, None, 0
            )[1]
            self.assertEqual(
                hashlib.pbkdf2_hmac(
                    "sha256", password, bytes.fromhex(data["salt"]), 300000
                ).hex(),
                data["hash"],
            )
            self.assertEqual(ensure_auth(root, "12345001"), data)
            self.assertNotIn(
                password.decode(),
                (root / "config/memory_ui_auth.json").read_text(encoding="utf-8"),
            )
