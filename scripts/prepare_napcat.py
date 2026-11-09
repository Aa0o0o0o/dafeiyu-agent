"""Provision the pinned release and a narrow, fail-closed QQ data-path patch."""

import hashlib
import json
import re
from pathlib import Path

root = Path(__file__).resolve().parents[1]
runtime = root / "runtime/napcat"
archive = root / "runtime/NapCat.Shell-v4.18.41.zip"
expected = "6801f93e533846cea05f4f531874a0e84371ab2331c5585b57cd7e3b869cf96b"
if hashlib.sha256(archive.read_bytes()).hexdigest() != expected:
    raise SystemExit("Official archive checksum mismatch; refusing to provision")
entry = runtime / "napcat.mjs"
original = runtime / "napcat.upstream.mjs"
if not original.exists():
    original.write_bytes(entry.read_bytes())
source = original.read_text(encoding="utf-8")
pattern = r"(?:[A-Za-z_$][\w$]*\.)*NodeQQNTWrapperUtil\.getNTUserDataInfoConfig\(\)"
matches = re.findall(pattern, source)
if len(matches) != 2:
    raise SystemExit("Upstream data-path layout changed; refusing to patch")
patched = re.sub(pattern, "process.env.DAFEIYU_QQ_DATA_ROOT", source)
patched = (
    'if(!process.env.DAFEIYU_QQ_DATA_ROOT)throw new Error("Isolated QQ data directory required");\n'
    + patched
)
entry.write_text(patched, encoding="utf-8")

private = json.loads((root / "config/local.json").read_text(encoding="utf-8"))
config = runtime / "config"
config.mkdir(exist_ok=True)
onebot = {
    "network": {
        "httpServers": [],
        "httpSseServers": [],
        "httpClients": [],
        "websocketServers": [],
        "plugins": [],
        "websocketClients": [
            {
                "name": "dafeiyu-owner-only",
                "enable": True,
                "url": "ws://127.0.0.1:6199/ws",
                "messagePostFormat": "array",
                "reportSelfMessage": False,
                "reconnectInterval": 10000,
                "token": private["onebot_token"],
                "debug": False,
                "heartInterval": 30000,
            }
        ],
    },
    "enableLocalFile2Url": False,
    "parseMultMsg": False,
}
(config / "onebot11_1111111111.json").write_text(
    json.dumps(onebot, indent=2), encoding="utf-8"
)
webui = {
    "host": "127.0.0.1",
    "port": 6099,
    "token": private["napcat_webui_token"],
    "autoLoginAccount": "",
    "disableWebUI": False,
    "disableNonLANAccess": True,
}
(config / "webui.json").write_text(json.dumps(webui, indent=2), encoding="utf-8")
(config / "napcat.json").write_text(
    json.dumps(
        {
            "fileLog": False,
            "consoleLog": True,
            "consoleLogLevel": "warn",
            "autoTimeSync": False,
        }
    ),
    encoding="utf-8",
)
report = {
    "version": "4.18.41",
    "source": "Official GitHub release",
    "archive_sha256": expected,
    "upstream_entry_sha256": hashlib.sha256(original.read_bytes()).hexdigest(),
    "patched_entry_sha256": hashlib.sha256(entry.read_bytes()).hexdigest(),
    "data_path_expressions_replaced": matches,
    "data_root": str(root / "runtime/qq-data"),
    "qq_program_root": str(root / "runtime/qq"),
    "original_qq_modified": False,
    "patch_reason": "Native data-path getter otherwise reads the main QQ's configured data directory. Both kernel/login initialization and runtime core.dataPath now require the explicit project-owned path.",
    "live_isolation_verified": False,
}
(root / "evidence/napcat-provision.json").write_text(
    json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
)
print(
    "NapCat provisioned; official archive verified, two QQ data-path expressions isolated."
)
