"""Local-only login status and QR presentation. No QQ passwords or CAPTCHA handling."""

import argparse
import hashlib
import json
import time
from datetime import datetime
from pathlib import Path

import httpx
import qrcode

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument("--refresh", action="store_true")
args = parser.parse_args()
private = json.loads((root / "config/local.json").read_text(encoding="utf-8"))
with httpx.Client(
    base_url="http://127.0.0.1:6099", timeout=10, trust_env=False
) as client:
    response = client.post(
        "/api/auth/login",
        json={
            "hash": hashlib.sha256(
                (private["napcat_webui_token"] + ".napcat").encode()
            ).hexdigest()
        },
    ).json()
    if response.get("code") != 0:
        raise SystemExit("本机 NapCat WebUI 鉴权失败，请检查配置。")
    client.headers["Authorization"] = "Bearer " + response["data"]["Credential"]
    if args.refresh:
        before = (
            client.post("/api/QQLogin/CheckLoginStatus", json={})
            .json()
            .get("data", {})
            .get("qrcodeurl", "")
        )
        refreshed = client.post("/api/QQLogin/RefreshQRcode", json={}).json()
        if refreshed.get("code") != 0:
            raise SystemExit("协议端暂未接受刷新，请稍后再试。")
        for _ in range(25):
            current = (
                client.post("/api/QQLogin/CheckLoginStatus", json={})
                .json()
                .get("data", {})
                .get("qrcodeurl", "")
            )
            if current and current != before:
                break
            time.sleep(0.2)
        else:
            raise SystemExit("未等到新的二维码，未展示旧码。")
    status = client.post("/api/QQLogin/CheckLoginStatus", json={}).json()
    data = status.get("data", {})
    account = client.post("/api/QQLogin/GetQQLoginInfo", json={}).json().get("data", {})
    qr_url = data.pop("qrcodeurl", "")
    report = {
        "isLogin": data.get("isLogin"),
        "isOffline": data.get("isOffline"),
        "loginPhase": data.get("loginPhase"),
        "coreReady": data.get("coreReady"),
        "login_error_present": bool(data.get("loginError")),
        "qr_available": bool(qr_url),
        "account_qq": str(account.get("uin", "")),
        "account_online": account.get("online"),
        "qq_messages_sent": 0,
    }
    if qr_url and not data.get("isLogin"):
        image = qrcode.make(qr_url)
        image_path = root / (
            "runtime/qq-login-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".png"
        )
        image.save(image_path)
        image.save(root / "runtime/qq-login.png")
        report["qr_image"] = str(image_path)
    (root / "evidence/qq-login-status.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
