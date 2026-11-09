"""Run locally in an interactive console; never accept credentials on argv."""

import base64
import getpass
import json
import secrets
import subprocess
from pathlib import Path

import win32crypt

root = Path(__file__).resolve().parents[1]
private = root / "config/local.json"
print("专用机器人 QQ：1111111111；你的 QQ：2222222222。")
print("Key 只保存在本机，使用当前 Windows 用户的 DPAPI 加密。")
key = getpass.getpass("请输入 DeepSeek API Key（不显示）：").strip()
if len(key) < 16 or any(c.isspace() for c in key):
    raise SystemExit("Key 格式不完整，未保存。")
existing = json.loads(private.read_text(encoding="utf-8")) if private.exists() else {}
value = {
    "onebot_token": existing.get("onebot_token") or secrets.token_urlsafe(32),
    "napcat_webui_token": existing.get("napcat_webui_token")
    or secrets.token_urlsafe(32),
    "deepseek_key_dpapi": base64.b64encode(
        win32crypt.CryptProtectData(
            key.encode(), "dafeiyu-deepseek", None, None, None, 0
        )
    ).decode(),
}
tmp = private.with_suffix(".tmp")
tmp.write_text(json.dumps(value, indent=2), encoding="utf-8")
tmp.replace(private)
# Limit access to this user; no credentials are printed or passed as argv.
sid = (
    subprocess.check_output(["whoami", "/user", "/fo", "csv", "/nh"], text=True)
    .split(",")[-1]
    .strip()
    .strip('"')
)
result = subprocess.run(
    ["icacls", str(private), "/inheritance:r", "/grant:r", f"*{sid}:(F)"],
    capture_output=True,
)
if result.returncode:
    print("密钥已加密保存，但文件权限设置未成功，请检查 config/local.json 的访问权限。")
else:
    print("已保存本机加密 Key 和独立 OneBot Token。没有调用 API。")
print("接下来按 docs/运行说明.md 完成独立 NapCat 登录与反向 WS 配置。")
