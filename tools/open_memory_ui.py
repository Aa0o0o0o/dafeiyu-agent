"""Owner-run launcher. Password is displayed only in this local terminal."""

import argparse
import base64
import getpass
import hashlib
import json
import secrets
import webbrowser
from pathlib import Path

import win32crypt

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument("--change-password", action="store_true")
args = parser.parse_args()
path = root / "config/memory_ui_auth.json"
if not path.exists():
    raise SystemExit("面板尚未启动。请先运行02_启动聊天.cmd，再打开记忆图。")
auth = json.loads(path.read_text(encoding="utf-8"))
if args.change_password:
    password = getpass.getpass("设置新的面板密码（至少12字符）：")
    if len(password) < 12 or password != getpass.getpass("再输入一次："):
        raise SystemExit("密码未修改。")
    salt = secrets.token_bytes(16)
    auth.update(
        salt=salt.hex(),
        hash=hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 300000).hex(),
        password_dpapi=base64.b64encode(
            win32crypt.CryptProtectData(
                password.encode(), "Dafeiyu memory panel", None, None, None, 0
            )
        ).decode(),
    )
    path.write_text(json.dumps(auth, indent=2), encoding="utf-8")
    print("密码已修改，请停止并重启聊天后端后使用新密码。")
else:
    password = win32crypt.CryptUnprotectData(
        base64.b64decode(auth["password_dpapi"]), None, None, None, 0
    )[1].decode()
    cfg = json.loads((root / "config/settings.json").read_text(encoding="utf-8-sig"))
    port = cfg.get("memory_v2", {}).get("ui_port", 6200)
    url = f"http://127.0.0.1:{port}"
    print("本机记忆图：" + url)
    print("管理账号：" + auth["viewer_id"])
    print("本机面板密码：" + password)
    print("请将密码填到刚打开的登录页。不要把密码发到QQ或聊天窗口。")
    webbrowser.open(url)
