"""One normal NapCat password-login attempt; hidden input, never persisted."""

import getpass
import hashlib
import json
import re
import warnings
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]


def main():
    config = json.loads((ROOT / "config/settings.json").read_text(encoding="utf-8-sig"))
    private = json.loads((ROOT / "config/local.json").read_text(encoding="utf-8-sig"))
    report = {"password_attempts": 0, "password_saved": False, "qq_messages_sent": 0}
    with httpx.Client(
        base_url="http://127.0.0.1:6099", timeout=30, trust_env=False
    ) as client:
        auth = client.post(
            "/api/auth/login",
            json={
                "hash": hashlib.sha256(
                    (private["napcat_webui_token"] + ".napcat").encode()
                ).hexdigest()
            },
        ).json()
        if auth.get("code") != 0:
            print("Local authentication failed. No password attempt.")
            return
        client.headers["Authorization"] = "Bearer " + auth["data"]["Credential"]
        before = (
            client.post("/api/QQLogin/CheckLoginStatus", json={}).json().get("data", {})
        )
        if before.get("isLogin"):
            print("Already logged in; no password attempt.")
            return
        with warnings.catch_warnings():
            # Never fall back to an echoed input channel.
            warnings.simplefilter("error", getpass.GetPassWarning)
            password = getpass.getpass("QQ password (hidden, one attempt): ")
        if not password:
            print("No password supplied; no login attempted.")
            return
        password_md5 = hashlib.md5(password.encode()).hexdigest()
        report["password_attempts"] = 1
        try:
            result = client.post(
                "/api/QQLogin/PasswordLogin",
                json={"uin": config["bot_qq"], "passwordMd5": password_md5},
            ).json()
            message = str(result.get("message") or result.get("msg") or "")
            message = message.replace(password, "[redacted]").replace(
                password_md5, "[redacted]"
            )
            message = re.sub(r"https?://\S+", "[URL]", message)
            message = re.sub(r"\b\d{5,}\b", "[number]", message)
            data = result.get("data") or {}
            report.update(
                api_code=result.get("code"),
                message=message[:800],
                need_captcha=bool(data.get("needCaptcha")),
                need_new_device=bool(data.get("needNewDevice")),
            )
        except (httpx.HTTPError, ValueError) as error:
            report.update(
                error_type=type(error).__name__, outcome="unknown_not_retried"
            )
        finally:
            del password, password_md5
        after = (
            client.post("/api/QQLogin/CheckLoginStatus", json={}).json().get("data", {})
        )
        report.update(
            is_login=bool(after.get("isLogin")),
            core_ready=bool(after.get("coreReady")),
            login_phase=after.get("loginPhase"),
        )
    path = ROOT / "evidence/qq-password-login-20261027.json"
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print("Login helper stopped: " + type(error).__name__)
